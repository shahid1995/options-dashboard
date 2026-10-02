"""Day 50 Slice A — server-side StrategyCandidate producer (Issue #118).

Turns REAL server-side market evidence into the existing Day-28 → Day-34
chain and delegates to the existing sanctioned bridge
(``execute_gated_paper_entry`` → the ``execute_strategy`` choke point).

Founder-approved Slice A decisions implemented here:
    D1  ΔOI evidence = server-side OI history: current OI from the live
        chain snapshot; previous OI from the latest eligible OptionCandle
        (exact broker instrument key, freshness/alignment window below).
        Missing, stale, or null-OI history stays missing — never coerced
        to zero — and an entry whose requested leg has no eligible prior
        OI fails closed.
    D2  Existing broker adapter path (``app.brokers.gateway``); the
        canonical ``MarketDataGateway`` is NOT a dependency of this slice.
    D5  No numeric freshness threshold is introduced; the real server-side
        reference timestamp is recorded once from the evidence and
        preserved through every contract.

Production prerequisite (Issue #118 — OPEN, not satisfied today):
    D1 needs a STORED prior-OI observation for the exact live broker
    instrument key.  ``OptionCandle`` — the only OI-bearing per-key series —
    is populated exclusively from the Upstox EXPIRED-instruments API: see
    the ``OptionCandle`` docstring in ``app/models.py``, the module docstring
    of ``app/services/option_candles.py``, ``daily_ingestion.
    _ingest_option_candles`` (selects ``ContractSpec.expiry <= today`` and
    calls ``get_expired_historical_candles``), ``backfill_orchestrator`` and
    ``app/tools/option_candle_backfill`` (same expired path).  Nothing in the
    current architecture persists OI for a still-unexpired contract, so in
    production this producer cannot compute ΔOI and fails closed
    (``EVIDENCE_INSUFFICIENT`` / ``CHAIN_DATA_MISSING``) with zero writes.
    That is intended until live option-OI history is ingested as separate
    architecture work — never widen the window, never reuse expired-contract
    history as if it were live, never match by strike text, never substitute
    current OI for prior OI, and never coerce missing history to zero.

The producer is orchestration only: it duplicates no payoff/risk/candidate
math (Day-18 quant, Day-31, Day-32 gate, Day-33 engine run verbatim),
creates no DB model, no second candidate representation, and no second
execution engine.  The client supplies only the approved request identity;
every authoritative object is server-generated.

Timestamps: the authoritative reference timestamp is the chain's own quote
timestamp when the broker supplies one, else the recorded receive-at moment
of the fetch.  ``datetime.now`` is read exactly once per request — to stamp
the fetch — never to manufacture market-evidence time.  Day-49 historical
PIT (naive IST completed bars) semantics do NOT apply to this live path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers.domain.enums import BROKER_ID_UPSTOX
from app.brokers.gateway import gateway
from app.intelligence.contracts import (
    IntelligenceDirection,
    MarketRegime,
    RegimeLabel,
    TimeHorizon,
)
from app.intelligence.flow import FlowInput, evaluate_flow
from app.intelligence.institutional import InstitutionalInput, evaluate_institutional
from app.intelligence.levels import LevelInput, classify_levels
from app.intelligence.positioning import (
    STRENGTH_REFERENCE_OI,
    PositioningInput,
    StrikePositioning,
    classify_chain,
    compute_metrics,
    evaluate_positioning,
)
from app.intelligence.regime import RegimeInput, evaluate_regime
from app.intelligence.synthesis import SynthesisInput, evaluate_synthesis
from app.market_data.contracts import Provenance, Side
from app.market_data.quality import QualityResult, QualityState
from app.models import NiftyCandle, OptionCandle
from app.opportunity.contracts import Observation, ObservationKind
from app.opportunity.pipeline import discover_opportunity
from app.quant.contracts import CalculationContext
from app.quant.scenarios import (
    OptionLeg,
    PositionDirection,
    ScenarioPoint,
    evaluate_portfolio,
)
from app.strike_ranking.contracts import (
    FactorObservation,
    OptionType,
    RankingFactor,
    StrikeCandidateInput,
    StrikeRankingInput,
)
from app.strike_ranking.ranking import DEFAULT_RANKING_WEIGHTS, rank_strikes
from app.strategy_evaluation.contracts import (
    DimensionState,
    EvaluationContext,
    HistoricalEvidence,
    LiquidityEvidence,
    PayoffEvidence,
    PayoffExpirySemantics,
    RiskEvidence,
    ScenarioPoint as EvaluationScenarioPoint,
    StrategyEvaluationInput,
    TailClass,
)
from app.strategy_evaluation.evaluation import evaluate_strategy
from app.strategy_lifecycle.lifecycle import evaluate_strategy_gate
from app.utils.market_time import to_ist_naive

IST = ZoneInfo("Asia/Kolkata")

#: D1 alignment window: previous OI must be strictly older than the live
#: observation by at least OI_HISTORY_MIN_LAG and no older than
#: OI_HISTORY_MAX_AGE.  Outside the window ⇒ missing (never fabricated).
OI_HISTORY_MAX_AGE = timedelta(hours=24)
OI_HISTORY_MIN_LAG = timedelta(seconds=90)
#: Candle storage interval used for OptionCandle/NiftyCandle history lookups.
OC_INTERVAL = "3min"
#: Regime price-window length (number of stored 3-minute closes supplied
#: to the Day-23 engine as ordered signed moves).
REGIME_PRICE_MOVES = 8
#: Explicit evaluation-context constant (Day-31 requires caller-supplied).
RISK_FREE_RATE = 0.065
#: Raw broker IV values are percentages (e.g. 12.5); model IV is a fraction.
IV_PERCENT_THRESHOLD = 3.0

#: Slice A (Issue #118) is deliberately NIFTY-only.  The Day-28/30 regime and
#: spot-move evidence is sourced from ``NiftyCandle`` — the ONLY stored spot
#: candle series in the schema (``app/models.py``: NiftyCandle + OptionCandle)
#: — and every evidence contract below is labelled with this underlying.
#: ``UPSTOX_INSTRUMENTS`` lists further indices, so an unsupported symbol must
#: fail closed at the producer boundary rather than be processed under NIFTY
#: evidence.  Parameterising Slice A would require a new historical evidence
#: model, which is explicitly out of scope.
SLICE_A_UNDERLYING = "NIFTY"

_SIDE_TO_MARKET = {"call": "call", "put": "put"}
_SIDE_TO_SIDE = {"call": Side.CALL, "put": Side.PUT}
_SIDE_TO_OPTION_TYPE = {"call": OptionType.CE, "put": OptionType.PE}
_SIDE_SIGN = {"buy": 1.0, "sell": -1.0}


class ProducerError(RuntimeError):
    """Fail-closed producer error (mapped onto PaperExecutionError)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ChainSide:
    """One measured side of one canonical chain row (all values measured)."""

    instrument_key: str | None
    strike: float
    ltp: float
    oi: float | None
    volume: float | None
    iv: float | None
    bid: float | None
    ask: float | None
    quote_ts: datetime | None


@dataclass(frozen=True)
class ProducedCandidate:
    """The full genuine-chain output handed to the sanctioned bridge."""

    candidate: object
    opportunity: object
    ranked_strikes: object
    evaluation: object
    reference_timestamp: datetime
    strategy_id: str


# ---------------------------------------------------------------------------
# Small measured-value helpers (no fabrication anywhere)
# ---------------------------------------------------------------------------

def _finite(value: Any) -> float | None:
    """Measured float, else None.

    NaN and the infinities are NOT measured quantities, so they stay missing
    exactly like an absent field: letting ``inf`` through would carry a
    non-finite price into the evidence chain, and the name promises a
    genuinely finite value.
    """
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if isfinite(out) else None


def _scale_iv(raw_iv: float | None) -> float | None:
    """Broker IV (percent) → annualized fraction; already-fraction passes."""
    if raw_iv is None:
        return None
    return raw_iv / 100.0 if raw_iv > IV_PERCENT_THRESHOLD else raw_iv


def _parse_broker_ts(raw: Any) -> datetime | None:
    """Parse a broker quote timestamp; naive values are read as IST (the
    Upstox market-data clock).  Returns an aware UTC datetime or None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=IST)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _candle_clock(moment: datetime) -> datetime:
    """Project a moment onto the STORED candle clock (naive IST).

    Phase 7.24.4 convention, through the repository's single canonical
    conversion (``app.utils.market_time.to_ist_naive``): every persisted
    market-data timestamp — ``NiftyCandle.open_time`` and
    ``OptionCandle.open_time`` alike — is naive IST, written by
    ``nifty_candles.record_candles`` and
    ``option_candles.record_option_candles``.  The D1 window and the
    spot-history cutoff are therefore computed on that same clock;
    comparing them in naive UTC (the previous behaviour) shifted every
    boundary by +05:30 against the stored rows.
    """
    converted = to_ist_naive(moment)
    if converted is None:  # pragma: no cover - moment is always a datetime
        raise ProducerError(
            "EVIDENCE_INSUFFICIENT",
            "the reference timestamp cannot be expressed on the stored "
            "candle clock; entry fails closed",
        )
    return converted


def _provenance(received_at: datetime) -> Provenance:
    return Provenance(
        source="UPSTOX",
        collection_mode="live",
        received_at=received_at,
        normalization_version="candidate-production-v1",
        contract_version="1",
        transformation_id="day50-candidate-production",
    )


def _chain_observation(
    chain: dict, *, symbol: str, expiry: str, received_at: datetime,
):
    """The canonical Day-9 chain observation for this exact snapshot.

    Built from the adapter's ALREADY-canonical rows, so nothing is
    re-derived or invented here: a field the broker did not report stays
    missing (``None``), exactly as the Day-9 contracts require.  This is
    the input the Day-12 quality engine evaluates, so the producer's
    quality result is measured from the same evidence the candidate uses.
    """
    from app.market_data.contracts import (
        ContractVersion,
        DataMode,
        OptionChainObservation,
        OptionChainRow,
        PriceQuote,
    )

    rows: list[OptionChainRow] = []
    event_times: list[datetime] = []
    for row in chain.get("chain", []):
        strike = _finite(row.get("strike"))
        if strike is None or strike <= 0:
            continue
        legs: dict[str, PriceQuote | None] = {}
        for name in ("call", "put"):
            raw = row.get(name) or {}
            ltp = _finite(raw.get("ltp"))
            if ltp is None:
                legs[name] = None  # absent leg, never a fabricated zero
                continue
            stamp = _parse_broker_ts(raw.get("quote_timestamp"))
            if stamp is not None:
                event_times.append(stamp)
            legs[name] = PriceQuote(
                ltp=ltp,
                bid=_finite(raw.get("bid_price")),
                ask=_finite(raw.get("ask_price")),
                volume=_finite(raw.get("volume")),
                oi=_finite(raw.get("oi")),
                iv=_scale_iv(_finite(raw.get("iv"))),
                source="UPSTOX",
                event_timestamp=stamp,
            )
        rows.append(OptionChainRow(
            strike=strike, call=legs["call"], put=legs["put"]))
    rows.sort(key=lambda item: item.strike)

    return OptionChainObservation(
        symbol=symbol,
        expiry_date=str(expiry),
        underlying_spot_price=_finite(chain.get("underlying_spot_price")),
        chain=rows,
        # The broker's own event time (max across legs) — never the receive
        # time, and never synthesized when the payload carries none.
        market_timestamp=max(event_times) if event_times else None,
        received_timestamp=received_at,
        source="UPSTOX",
        data_mode=DataMode.BROKER_SNAPSHOT,
        contract_version=ContractVersion.v1_0_0,
    )


def _quality(observation, *, reference_ts: datetime) -> QualityResult:
    """Measure chain quality with the REAL Day-12 engine.

    The previous implementation returned a hard-coded EXCELLENT/100 with no
    dimensions, which is fabricated evidence: it asserted measured freshness,
    completeness, validity and provenance that were never measured, and no
    quality requirement could ever fail because of it.  The engine is
    deterministic and takes an explicit ``reference_time``, so the producer's
    own authoritative reference timestamp is used — this introduces no second
    wall-clock read.
    """
    from app.market_data.quality import MarketDataQualityEngine

    return MarketDataQualityEngine().evaluate(
        observation, reference_time=reference_ts)


def _extract_sides(chain: dict) -> list[ChainSide]:
    """Flatten canonical chain rows into measured sides.

    A row contributes a side only when its strike is positive and its LTP
    is a positive number; everything else stays missing (never coerced).
    """
    out: list[ChainSide] = []
    for row in chain.get("chain", []):
        strike = _finite(row.get("strike"))
        if strike is None or strike <= 0:
            continue
        for side_name in ("call", "put"):
            side = row.get(side_name) or {}
            ltp = _finite(side.get("ltp"))
            if ltp is None or ltp <= 0:
                continue
            out.append(ChainSide(
                instrument_key=side.get("instrument_key"),
                strike=strike,
                ltp=ltp,
                oi=_finite(side.get("oi")),
                volume=_finite(side.get("volume")),
                iv=_finite(side.get("iv")),
                bid=_finite(side.get("bid_price")),
                ask=_finite(side.get("ask_price")),
                quote_ts=_parse_broker_ts(side.get("quote_timestamp")),
            ))
    return out


def _reference_ts(sides: list[ChainSide], received_at: datetime) -> datetime:
    """Authoritative reference timestamp of the evidence: the broker's own
    quote timestamp when present (latest across sides), else the recorded
    receive-at moment of the fetch.  No second wall-clock read."""
    stamped = [s.quote_ts for s in sides if s.quote_ts is not None]
    return max(stamped) if stamped else received_at


# ---------------------------------------------------------------------------
# D1 — server-side OI history (ΔOI evidence rule)
# ---------------------------------------------------------------------------

def _prior_oi_state(
    db: Session,
    instrument_keys: list[str],
    reference_ts: datetime,
) -> dict[str, float | None]:
    """Latest eligible prior OI per broker instrument key.

    Eligible = latest ``OptionCandle`` row (``OC_INTERVAL``) for the exact
    key whose ``open_time`` satisfies
    ``reference_ts - OI_HISTORY_MAX_AGE <= open_time <
    reference_ts - OI_HISTORY_MIN_LAG`` (both boundaries projected onto the
    stored naive-IST candle clock).  A row with NULL ``open_interest`` is
    missing history, not zero.  Keys without eligible history map to
    ``None`` — a later ΔOI of ``None`` suppresses the strike instead of
    inventing evidence.
    """
    out: dict[str, float | None] = {key: None for key in instrument_keys}
    if not instrument_keys:
        return out
    newest_allowed = _candle_clock(reference_ts - OI_HISTORY_MIN_LAG)
    oldest_allowed = _candle_clock(reference_ts - OI_HISTORY_MAX_AGE)
    rows = db.execute(
        select(OptionCandle)
        .where(
            OptionCandle.instrument_key.in_(instrument_keys),
            OptionCandle.interval == OC_INTERVAL,
        )
        .order_by(OptionCandle.instrument_key, OptionCandle.open_time.desc())
    ).scalars().all()
    seen: set[str] = set()
    for row in rows:
        if row.instrument_key in seen:
            continue  # newest-first per key: the first eligible row wins
        open_time = row.open_time
        if open_time > newest_allowed or open_time < oldest_allowed:
            continue  # same-window or stale — history stays missing
        seen.add(row.instrument_key)
        if row.open_interest is None:
            continue  # null OI is genuinely missing
        out[row.instrument_key] = float(row.open_interest)
    return out


def _delta_oi(current_oi: float | None, prior_oi: float | None) -> float | None:
    """D1 ΔOI: measured now minus measured prior.  Missing stays missing."""
    if current_oi is None or prior_oi is None:
        return None
    return current_oi - prior_oi


def _spot_history(
    db: Session, reference_ts: datetime, count: int = REGIME_PRICE_MOVES,
) -> tuple[tuple[float, ...], float | None]:
    """Prior stored NIFTY closes strictly before the reference timestamp
    (oldest→newest) plus the immediately-prior close.  Real stored candles
    only — nothing interpolated.  The cutoff is expressed on the stored
    naive-IST candle clock, the clock ``NiftyCandle.open_time`` is written
    in."""
    cutoff = _candle_clock(reference_ts)
    candles = db.execute(
        select(NiftyCandle)
        .where(
            NiftyCandle.interval == OC_INTERVAL,
            NiftyCandle.open_time < cutoff,
        )
        .order_by(NiftyCandle.open_time.desc())
        .limit(count)
    ).scalars().all()
    closes = tuple(float(c.close) for c in reversed(candles))
    return closes, (closes[-1] if closes else None)


# ---------------------------------------------------------------------------
# Evaluation-evidence derivation through the SHARED quant engine
# ---------------------------------------------------------------------------

def _calc_context(reference_ts: datetime) -> CalculationContext:
    return CalculationContext(
        reference_timestamp=reference_ts,
        risk_free_rate=RISK_FREE_RATE,
        dividend_yield=None,
        model_version="bsm-v1",
        calculation_version="day50-candidate-production",
    )


def _classify_tail(legs: list[dict]) -> TailClass:
    """Structural payoff tail from leg directions (classification, not a
    probability).  A short call is gain-capped (bounded loss) only when a
    long call exists at an equal-or-higher strike; a long call is capped
    only when a short call exists at a strictly higher strike (symmetric
    for puts with lower strikes).  Short puts never carry unlimited loss.
    Uncovered short call ⇒ UNLIMITED_LOSS; uncapped long leg ⇒
    UNLIMITED_GAIN; fully covered spreads ⇒ NONE."""
    long_calls = [leg for leg in legs
                  if leg["direction"] == "buy" and leg["option_type"] == "call"]
    short_calls = [leg for leg in legs
                   if leg["direction"] == "sell" and leg["option_type"] == "call"]
    long_puts = [leg for leg in legs
                 if leg["direction"] == "buy" and leg["option_type"] == "put"]
    short_puts = [leg for leg in legs
                  if leg["direction"] == "sell" and leg["option_type"] == "put"]
    for sc in short_calls:
        if not any(lc["strike_price"] >= sc["strike_price"] for lc in long_calls):
            return TailClass.UNLIMITED_LOSS
    for lc in long_calls:
        if not any(scr["strike_price"] > lc["strike_price"] for scr in short_calls):
            return TailClass.UNLIMITED_GAIN
    for lp in long_puts:
        if not any(sp["strike_price"] < lp["strike_price"] for sp in short_puts):
            return TailClass.UNLIMITED_GAIN
    return TailClass.NONE


def _time_to_expiry_years(expiry: str, reference_ts: datetime) -> float:
    """Year fraction from the reference timestamp to the 15:30 IST close
    of the expiry date (never below zero)."""
    ref = reference_ts if reference_ts.tzinfo else reference_ts.replace(
        tzinfo=timezone.utc)
    ref_local = ref.astimezone(IST)
    close = datetime.strptime(expiry, "%Y-%m-%d").replace(
        hour=15, minute=30, tzinfo=IST)
    seconds = (close - ref_local).total_seconds()
    return max(seconds / (365.0 * 24 * 3600), 0.0)


def _pnl_sign(pnl: float) -> int:
    """Sign bucket of a grid P&L: ``-1``, ``0`` or ``+1``.

    Bucketing keeps the breakeven scan free of raw float-equality tests
    while preserving the exact semantics: a grid point whose P&L is exactly
    zero is itself a breakeven.  Non-finite P&Ls never reach this helper
    (the payoff scan drops them as PARTIAL), so ``0`` is unambiguous.
    """
    if pnl > 0.0:
        return 1
    if pnl < 0.0:
        return -1
    return 0


def _expiry_payoff(
    legs: list[dict], spot: float, reference_ts: datetime,
    provenance: Provenance,
) -> PayoffEvidence:
    """Derive PayoffEvidence with the shared Day-18 quant engine.

    The terminal payoff is the portfolio's P&L evaluated through
    ``evaluate_portfolio`` at ``time_to_expiry = 0`` (the engine's intrinsic
    convention) over a deterministic spot grid around the observed spot.
    Entry premium per leg is the measured chain LTP signed by direction —
    no independent payoff formula is implemented here.  A grid point the
    engine cannot price completely — or that comes back non-finite — is
    dropped as genuinely missing (the result is then PARTIAL), never
    zero-filled.  Raises ``ProducerError`` when no grid point prices
    completely (fail closed).
    """
    context = _calc_context(reference_ts)
    grid = [spot * (1.0 + frac) for frac in
            (-0.10, -0.075, -0.05, -0.025, 0.0, 0.025, 0.05, 0.075, 0.10)]
    samples: list[tuple[float, float]] = []
    partial = False
    for grid_spot in grid:
        port = evaluate_portfolio(
            tuple(leg["quant_leg"] for leg in legs),
            context,
            spot=grid_spot,
            time_to_expiry=0.0,
            implied_volatility=None,
        )
        if (port.partial or port.total_pnl is None
                or not isfinite(port.total_pnl)):
            partial = True
            continue
        samples.append((grid_spot, port.total_pnl))
    if not samples:
        raise ProducerError(
            "EVIDENCE_INSUFFICIENT",
            "the expiry payoff could not be derived from the shared quant "
            "engine; entry fails closed",
        )

    pnls = [pnl for _, pnl in samples]

    net = 0.0
    for leg in legs:
        net += _SIDE_SIGN[leg["direction"]] * leg["ltp"] * float(leg["quantity"])

    # Each sample carries its own spot label: when a grid point is dropped
    # the P&L series is shorter than the grid, so pairing the two by
    # position would attribute a breakeven to a spot that was never sampled
    # (fabricated evidence).
    breakevens: list[float] = []
    for (lo_spot, lo_pnl), (hi_spot, hi_pnl) in zip(samples, samples[1:]):
        lo_sign = _pnl_sign(lo_pnl)
        if lo_sign == 0:
            # Exactly flat at this grid point: that spot IS a breakeven.
            breakevens.append(lo_spot)
        elif lo_sign != _pnl_sign(hi_pnl):
            span = abs(lo_pnl) + abs(hi_pnl)
            frac = abs(lo_pnl) / span if span else 0.0
            breakevens.append(lo_spot + (hi_spot - lo_spot) * frac)

    state = DimensionState.PARTIAL if partial else DimensionState.AVAILABLE
    return PayoffEvidence(
        expiry_semantics=PayoffExpirySemantics.SAME_EXPIRY_EXACT,
        state=state,
        net_debit_credit=round(net, 2),
        max_profit=max(pnls),
        max_loss=min(pnls),
        tail=_classify_tail(legs),
        breakevens=tuple(round(b, 2) for b in sorted(set(breakevens))),
        premium_outlay=round(net, 2),
        provenance=provenance,
    )


def _liquidity_evidence(
    legs: list[dict], side_index: dict[tuple[float, str], ChainSide],
    provenance: Provenance,
) -> LiquidityEvidence:
    """LiquidityEvidence from real bid/ask only.  ``spread_bps`` stays None
    unless every requested leg has a usable two-sided quote — liquidity
    completeness is never claimed without measurements."""
    complete = 0
    spreads: list[float] = []
    for leg in legs:
        side = side_index.get((leg["strike_price"], leg["option_type"]))
        bid = side.bid if side else None
        ask = side.ask if side else None
        if bid is not None and ask is not None and 0 < bid <= ask:
            complete += 1
            mid = (bid + ask) / 2.0
            if mid > 0:
                spreads.append((ask - bid) / mid * 10_000.0)
    all_complete = complete == len(legs) and legs
    return LiquidityEvidence(
        state=DimensionState.AVAILABLE if all_complete else DimensionState.PARTIAL,
        legs_complete=complete,
        legs_total=len(legs),
        spread_bps=(sum(spreads) / len(spreads)) if spreads else None,
        quality=None,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# Strike-ranking factor scores (measured inputs only)
# ---------------------------------------------------------------------------

def _spread_score(side: ChainSide) -> float:
    if side.bid is None or side.ask is None or side.bid <= 0 or side.ask < side.bid:
        return 0.5  # unmeasured → neutral score, never a fabricated spread
    mid = (side.bid + side.ask) / 2.0
    if mid <= 0:
        return 0.5
    spread_bps = (side.ask - side.bid) / mid * 10_000.0
    return max(0.0, min(1.0, 1.0 - spread_bps / 200.0))


def _distance_score(strike: float, spot: float) -> float:
    distance_pct = abs(strike - spot) / spot * 100.0
    return max(0.0, 1.0 - distance_pct / 5.0)


def _strike_factors(
    side: ChainSide, delta_oi: float, spot: float, provenance: Provenance,
) -> tuple[FactorObservation, ...]:
    iv = _scale_iv(side.iv)
    return (
        FactorObservation(
            factor=RankingFactor.LIQUIDITY,
            score=min((side.volume or 0.0) / 100_000.0, 1.0),
            raw=side.volume, provenance=provenance),
        FactorObservation(
            factor=RankingFactor.SPREAD_QUALITY, score=_spread_score(side),
            raw=None, provenance=provenance),
        FactorObservation(
            factor=RankingFactor.IV, score=min(iv or 0.0, 1.0),
            raw=iv, provenance=provenance),
        FactorObservation(
            factor=RankingFactor.GREEKS, score=min((iv or 0.0) * 4.0, 1.0),
            raw=iv, provenance=provenance),
        FactorObservation(
            factor=RankingFactor.POSITIONING,
            score=min(abs(delta_oi) / STRENGTH_REFERENCE_OI, 1.0),
            raw=delta_oi, provenance=provenance),
        # Measured per-strike GEX is not acquired in Slice A; the factor is
        # carried neutral so ranking stays explicit about it (score, not a
        # fabricated GEX measurement).
        FactorObservation(
            factor=RankingFactor.GEX, score=0.5, raw=None,
            provenance=provenance),
        FactorObservation(
            factor=RankingFactor.DISTANCE_TO_SPOT,
            score=_distance_score(side.strike, spot),
            raw=abs(side.strike - spot), provenance=provenance),
        FactorObservation(
            factor=RankingFactor.STRATEGY_OBJECTIVE, score=1.0,
            raw=None, provenance=provenance),
        FactorObservation(
            factor=RankingFactor.RISK, score=1.0, raw=None,
            provenance=provenance),
    )


def _atm_iv(sides: list[ChainSide], spot: float) -> float | None:
    """Mean scaled IV of the two nearest-the-money measured sides."""
    by_distance = sorted(sides, key=lambda s: abs(s.strike - spot))[:2]
    ivs = [_scale_iv(s.iv) for s in by_distance]
    ivs = [iv for iv in ivs if iv is not None and iv > 0]
    if not ivs:
        return None
    return sum(ivs) / len(ivs)


def _directional(direction: IntelligenceDirection | None) -> IntelligenceDirection | None:
    return direction if direction in (
        IntelligenceDirection.BULLISH, IntelligenceDirection.BEARISH) else None


# ---------------------------------------------------------------------------
# Pure evidence → candidate core (no HTTP, no wall clock beyond inputs)
# ---------------------------------------------------------------------------

def produce_candidate_core(
    *,
    chain: dict,
    instrument_keys: list[str | None],
    prior_oi_by_key: dict[str, float | None],
    spot_closes: tuple[float, ...],
    prev_spot: float | None,
    received_at: datetime,
    id_seed: str,
    strategy_id: str,
    legs: list[dict],
    underlying: str = SLICE_A_UNDERLYING,
) -> ProducedCandidate:
    """Assemble genuine evidence and run the existing Day-20 → Day-32 chain.

    ``legs`` entries carry ``expiration_date / strike_price / option_type /
    action / quantity``.  Raises ``ProducerError`` whenever genuine evidence
    cannot produce an eligible candidate — nothing is fabricated.
    """
    sides = _extract_sides(chain)
    if not sides:
        raise ProducerError(
            "CHAIN_DATA_MISSING", "the option chain carried no usable rows")
    spot = _finite(chain.get("underlying_spot_price"))
    if spot is None or spot <= 0:
        raise ProducerError(
            "CHAIN_DATA_MISSING", "chain carried no underlying spot price")

    # The flat sides list loses the call/put distinction; rebuild the
    # per-(strike, market-side) index from the raw rows.
    side_index = _build_side_index(chain)

    provenance = _provenance(received_at)
    reference_ts = _reference_ts(sides, received_at)
    spot_change = (spot - prev_spot) if prev_spot is not None else None
    expiry = str(legs[0]["expiration_date"]) if legs else None
    # Quality is MEASURED over this snapshot by the real Day-12 engine
    # (never asserted).  The engine is bounded by the producer's own
    # authoritative reference timestamp, so no second clock read is added.
    quality = _quality(
        _chain_observation(
            chain,
            symbol=underlying,
            expiry=expiry or "",
            received_at=received_at,
        ),
        reference_ts=reference_ts,
    )

    # -- measured per-strike rows with D1 ΔOI --------------------------------
    key_by_market_side: dict[tuple[float, str], str | None] = {}
    for row in chain.get("chain", []):
        strike = _finite(row.get("strike"))
        if strike is None or strike <= 0:
            continue
        for side_name in ("call", "put"):
            side = row.get(side_name) or {}
            key_by_market_side[(strike, side_name)] = side.get("instrument_key")

    def _delta_for(strike: float, side_name: str) -> float | None:
        key = key_by_market_side.get((strike, side_name))
        if key is None:
            return None
        return _delta_oi(
            next((s.oi for s in sides
                  if s.strike == strike and s.instrument_key == key), None),
            prior_oi_by_key.get(key),
        )

    rows: list[StrikePositioning] = []
    strike_deltas: dict[tuple[float, str], float | None] = {}
    for row in chain.get("chain", []):
        strike = _finite(row.get("strike"))
        if strike is None or strike <= 0:
            continue
        call_side = row.get("call") or {}
        put_side = row.get("put") or {}
        call_delta = _delta_for(strike, "call")
        put_delta = _delta_for(strike, "put")
        strike_deltas[(strike, "call")] = call_delta
        strike_deltas[(strike, "put")] = put_delta
        rows.append(StrikePositioning(
            strike=strike,
            call_oi=_finite(call_side.get("oi")),
            put_oi=_finite(put_side.get("oi")),
            call_oi_change=call_delta,
            put_oi_change=put_delta,
            call_volume=_finite(call_side.get("volume")),
            put_volume=_finite(put_side.get("volume")),
        ))

    positioning_input = PositioningInput(
        underlying=underlying,
        rows=tuple(rows),
        reference_timestamp=reference_ts,
        provenance=provenance,
        expiry=expiry,
        quality=quality,
        spot=spot,
        spot_change=spot_change,
    )
    positioning_result = evaluate_positioning(positioning_input)
    positioning_metrics = compute_metrics(positioning_input)
    positioning_label = classify_chain(
        positioning_metrics.net_chain_oi_change, spot_change)

    flow_result = evaluate_flow(FlowInput(
        underlying=underlying,
        reference_timestamp=reference_ts,
        provenance=provenance,
        expiry=expiry,
        quality=quality,
        spot=spot,
        spot_change=spot_change,
        net_ce_oi_change=positioning_metrics.total_call_oi_change,
        net_pe_oi_change=positioning_metrics.total_put_oi_change,
        ce_volume=positioning_metrics.total_call_volume,
        pe_volume=positioning_metrics.total_put_volume,
    ))

    level_input = LevelInput(
        underlying=underlying,
        rows=tuple(rows),
        reference_timestamp=reference_ts,
        provenance=provenance,
        expiry=expiry,
        quality=quality,
        spot=spot,
        spot_change=spot_change,
    )
    classifications = classify_levels(level_input)

    institutional_result = evaluate_institutional(InstitutionalInput(
        underlying=underlying,
        reference_timestamp=reference_ts,
        provenance=provenance,
        expiry=expiry,
        quality=quality,
        spot=spot,
        spot_change=spot_change,
        net_call_oi_change=positioning_metrics.total_call_oi_change,
        net_put_oi_change=positioning_metrics.total_put_oi_change,
        total_call_oi=positioning_metrics.total_call_oi,
        total_put_oi=positioning_metrics.total_put_oi,
        call_volume=positioning_metrics.total_call_volume,
        put_volume=positioning_metrics.total_put_volume,
        level_classifications=classifications,
    ))

    regime_result = evaluate_regime(RegimeInput(
        underlying=underlying,
        reference_timestamp=reference_ts,
        provenance=provenance,
        expiry=expiry,
        quality=quality,
        spot=spot,
        spot_change=spot_change,
        price_moves=tuple(
            b - a for a, b in zip(spot_closes, spot_closes[1:])
        ) if len(spot_closes) >= 2 else (),
        volatility=_atm_iv(sides, spot),
        positioning=positioning_label,
        institutional_direction=institutional_result.direction,
        institutional_strength=institutional_result.signal_strength,
        level_classifications=classifications,
    ))

    synthesis_result = evaluate_synthesis(SynthesisInput(
        underlying=underlying,
        reference_timestamp=reference_ts,
        provenance=provenance,
        expiry=expiry,
        quality=quality,
        spot=spot,
        spot_change=spot_change,
        positioning=positioning_label,
        price_flow_relation=getattr(flow_result, "price_flow_relation", None),
        level_classifications=classifications,
        institutional_direction=institutional_result.direction,
        institutional_strength=institutional_result.signal_strength,
        regime_label=getattr(regime_result.regime, "label", None),
        regime_direction=_directional(regime_result.direction),
        regime=regime_result.regime,
        time_horizon=TimeHorizon.EXPIRY,
    ))

    observation = Observation(
        observation_id=f"obs-{id_seed}",
        underlying=underlying,
        upstream=synthesis_result,
        expiry=expiry,
        kind=ObservationKind.INTELLIGENCE_RESULT,
    )
    try:
        opportunity = discover_opportunity(
            observation,
            signal_id=f"sig-{id_seed}",
            setup_id=f"setup-{id_seed}",
            opportunity_id=f"opp-{id_seed}",
        )
    except ValueError as exc:
        raise ProducerError(
            "EVIDENCE_INSUFFICIENT",
            "the genuine intelligence chain could not produce an "
            f"Opportunity (fail-closed): {exc}",
        ) from exc

    # -- strike candidates from the SAME chain rows ---------------------------
    candidates: list[StrikeCandidateInput] = []
    for (strike, side_name), side in sorted(side_index.items()):
        delta = strike_deltas.get((strike, side_name))
        if delta is None or side.instrument_key is None:
            continue  # no genuine ΔOI ⇒ unrankable ⇒ suppressed (D1)
        candidates.append(StrikeCandidateInput(
            candidate_id=f"strike:{strike:g}:{side_name}",
            underlying=underlying,
            option_type=_SIDE_TO_OPTION_TYPE[side_name],
            strike=strike,
            expiry=expiry,
            factors=_strike_factors(side, delta, spot, provenance),
            opportunity=opportunity,
            quality=quality,
        ))
    if not candidates:
        raise ProducerError(
            "EVIDENCE_INSUFFICIENT",
            "no strike carries eligible ΔOI history under the approved "
            "alignment rule; entry fails closed",
        )
    ranked = rank_strikes(StrikeRankingInput(
        candidates=tuple(candidates),
        weights=DEFAULT_RANKING_WEIGHTS,
        objective_id="day50-candidate-production",
    ))
    if not ranked.ranked:
        raise ProducerError(
            "EVIDENCE_INSUFFICIENT",
            "genuine ranking evidence produced no eligible strike; "
            "entry fails closed",
        )
    ranked_ids = {item.candidate_id for item in ranked.ranked}

    # -- requested legs must be genuinely riskable ----------------------------
    quant_legs: list[OptionLeg] = []
    payoff_legs: list[dict] = []
    for leg in legs:
        strike = float(leg["strike_price"])
        side_name = leg["option_type"]
        side = side_index.get((strike, side_name))
        if side is None:
            raise ProducerError(
                "CHAIN_DATA_MISSING",
                f"requested leg {strike} {side_name} is absent from the "
                "live chain; entry fails closed",
            )
        candidate_id = f"strike:{strike:g}:{side_name}"
        if candidate_id not in ranked_ids:
            raise ProducerError(
                "CANDIDATE_NOT_ELIGIBLE",
                f"requested leg {strike} {side_name} was suppressed by "
                "genuine ranking evidence (missing ΔOI history or unusable "
                "factors); entry fails closed",
            )
        quant_leg = OptionLeg(
            option_type=_SIDE_TO_SIDE[side_name],
            strike=strike,
            expiry=expiry,
            quantity=float(leg["quantity"]),
            direction=(PositionDirection.LONG if leg["action"] == "buy"
                       else PositionDirection.SHORT),
            entry_price=side.ltp,
            implied_volatility=_scale_iv(side.iv) or _atm_iv(sides, spot),
            # The MEASURED Day-12 state for this snapshot — never a
            # hard-coded EXCELLENT.
            quality=quality.quality_state,
            provenance=provenance,
        )
        quant_legs.append(quant_leg)
        payoff_legs.append({**leg, "strike_price": strike,
                            "option_type": side_name,
                            "direction": leg["action"],
                            "quant_leg": quant_leg, "ltp": side.ltp})

    payoff = _expiry_payoff(payoff_legs, spot, reference_ts, provenance)
    atm_iv = _atm_iv(sides, spot)
    years = _time_to_expiry_years(expiry, reference_ts)
    evaluation_input = StrategyEvaluationInput(
        strategy_id=strategy_id,
        legs=tuple(quant_legs),
        evaluation_context=EvaluationContext.OPPORTUNITY,
        reference_timestamp=reference_ts,
        spot=spot,
        time_to_expiry=years,
        risk_free_rate=RISK_FREE_RATE,
        scenario_points=tuple(
            EvaluationScenarioPoint(
                spot=spot * mult, time_to_expiry=years,
                implied_volatility=atm_iv)
            for mult in (0.98, 0.99, 1.00, 1.01, 1.02)
        ),
        implied_volatility=atm_iv,
        payoff=payoff,
        market_regime=regime_result.regime,
        regime_direction=_directional(regime_result.direction),
        strategy_direction=_directional(opportunity.upstream.direction),
        liquidity=_liquidity_evidence(payoff_legs, side_index, provenance),
        risk=RiskEvidence(
            state=DimensionState.AVAILABLE,
            structural_unbounded_loss=(
                _classify_tail(payoff_legs) is TailClass.UNLIMITED_LOSS),
            max_loss_estimate=payoff.max_loss,
            notes=("derived from the shared quant engine's expiry payoff",),
            provenance=provenance,
        ),
        historical=HistoricalEvidence(
            state=DimensionState.AVAILABLE,
            observations=(sum(1 for v in prior_oi_by_key.values()
                              if v is not None) + len(spot_closes)),
            metric_note=("prior OptionCandle OI observations and stored "
                         "NIFTY closes actually consulted for this candidate"),
        ),
        opportunity=opportunity,
    )
    evaluation = evaluate_strategy(evaluation_input)

    gate = evaluate_strategy_gate(
        opportunity,
        ranked,
        evaluation,
        strategy_id=strategy_id,
        legs=tuple(quant_legs),
        reference_timestamp=reference_ts,
    )
    if gate.candidate is None or not gate.eligible:
        reasons = "; ".join(r.message for r in gate.blocking_reasons) or \
            "candidate is not eligible"
        raise ProducerError("CANDIDATE_NOT_ELIGIBLE", reasons)

    return ProducedCandidate(
        candidate=gate.candidate,
        opportunity=opportunity,
        ranked_strikes=ranked,
        evaluation=evaluation,
        reference_timestamp=reference_ts,
        strategy_id=strategy_id,
    )


def _build_side_index(chain: dict) -> dict[tuple[float, str], ChainSide]:
    """Per-(strike, market-side) measured sides straight from the rows."""
    index: dict[tuple[float, str], ChainSide] = {}
    for row in chain.get("chain", []):
        strike = _finite(row.get("strike"))
        if strike is None or strike <= 0:
            continue
        for side_name in ("call", "put"):
            side = row.get(side_name) or {}
            ltp = _finite(side.get("ltp"))
            if ltp is None or ltp <= 0:
                continue
            index[(strike, side_name)] = ChainSide(
                instrument_key=side.get("instrument_key"),
                strike=strike,
                ltp=ltp,
                oi=_finite(side.get("oi")),
                volume=_finite(side.get("volume")),
                iv=_finite(side.get("iv")),
                bid=_finite(side.get("bid_price")),
                ask=_finite(side.get("ask_price")),
                quote_ts=_parse_broker_ts(side.get("quote_timestamp")),
            )
    return index


# ---------------------------------------------------------------------------
# Async acquisition wrapper (HTTP/broker/DB boundaries live only here)
# ---------------------------------------------------------------------------

async def _default_fetch_chain(symbol: str, expiry: str, token: str) -> dict:
    adapter = gateway.create(BROKER_ID_UPSTOX, access_token=token)
    return await adapter.get_option_chain(symbol, expiry)


async def _default_resolve_keys(legs: list[dict], token: str) -> list[str | None]:
    """Broker instrument keys via the existing adapter rule — read from the
    raw chain payload, never constructed from strike text."""
    adapter = gateway.create(BROKER_ID_UPSTOX, access_token=token)
    resolved = await adapter.resolve_instrument_keys([
        {"symbol": "NIFTY", "expiry": leg["expiration_date"],
         "strike": leg["strike_price"], "option_type": leg["option_type"]}
        for leg in legs
    ])
    return [entry.get("instrument_key") for entry in resolved]


def _default_token_resolver(db: Session, user_id: str) -> str:
    from app.services.market_data_authorization import (
        resolve_market_data_token,
    )

    credential = resolve_market_data_token(
        db, user_id, BROKER_ID_UPSTOX.value)
    if credential is None or not getattr(credential, "token", None):
        raise ProducerError(
            "MARKET_DATA_UNAUTHORIZED",
            "no authorized broker market-data credential is available")
    return credential.token


async def produce_candidate_and_execute(
    user_id: str,
    db: Session,
    request,
    prices: dict,
    *,
    chains: dict | None = None,
    token_resolver: Callable[[Session, str], str] | None = None,
    fetch_chain: Callable[[str, str, str], Any] | None = None,
    resolve_keys: Callable[[list[dict], str], Any] | None = None,
    now_fn: Callable[[], datetime] | None = None,
    execute_gated: Callable[..., Any] | None = None,
):
    """Acquire real evidence, produce the genuine candidate, and delegate to
    the existing sanctioned bridge.  The ONLY sanctioned call path for
    ``POST /paper/executions``.  The keyword parameters exist exclusively
    for tests (they replace broker/session boundaries); production callers
    never pass them."""
    from app.models import StrategyExecution
    from app.services.paper_execution import (
        PaperExecutionError,
        execute_strategy,
    )
    from app.services.paper_risk import execute_gated_paper_entry

    def _fail(code: str, message: str) -> PaperExecutionError:
        return PaperExecutionError(code, message)

    # 0. Replay/idempotency stays BEFORE any evidence acquisition: a repeat
    #    client_order_id returns the ORIGINAL execution untouched, exactly
    #    like the bridge and the choke point's own replay branch.
    existing = db.scalar(
        select(StrategyExecution).where(
            StrategyExecution.user_id == user_id,
            StrategyExecution.client_order_id == request.client_order_id,
        )
    )
    if existing is not None:
        return execute_strategy(user_id, request, db, prices)

    moment = (now_fn or (lambda: datetime.now(timezone.utc)))()
    received_at = (moment if moment.tzinfo
                   else moment.replace(tzinfo=timezone.utc))

    # 0b. Slice A is NIFTY-only.  The evidence chain is wired to NIFTY spot
    #     history (NiftyCandle) and every evidence contract is labelled with
    #     that underlying, so any other supported instrument must fail closed
    #     here — before any broker/session work — rather than be processed
    #     and mislabelled under NIFTY evidence.
    symbol = str(request.symbol).upper()
    if symbol != SLICE_A_UNDERLYING:
        raise _fail(
            "UNSUPPORTED_SYMBOL",
            f"Slice A candidate production supports {SLICE_A_UNDERLYING} "
            f"only; the Day-28→Day-33 evidence chain is sourced from "
            f"{SLICE_A_UNDERLYING} history, so {symbol} cannot produce a "
            "genuine candidate. Order was not executed.")

    # 1. Server-side broker market-data authorization (existing mechanism).
    try:
        market_data_token = (token_resolver or _default_token_resolver)(
            db, user_id)
    except Exception as exc:
        raise _fail(
            "MARKET_DATA_UNAUTHORIZED",
            "server-side broker market-data authorization is required for a "
            "candidate-backed paper entry; order was not executed") from exc

    legs = [
        {
            "expiration_date": leg.expiration_date,
            "strike_price": float(leg.strike_price),
            "option_type": leg.option_type.lower(),
            "action": leg.action,
            "quantity": float(leg.quantity),
        }
        for leg in request.legs
    ]
    expiries = sorted({leg["expiration_date"] for leg in legs})
    if len(expiries) != 1:
        raise _fail(
            "CANDIDATE_NOT_ELIGIBLE",
            "Slice A supports single-expiry entries only; order was not "
            "executed")
    expiry = expiries[0]

    # 2. ONE canonical chain snapshot via the existing adapter path (D2).
    #    When the caller already fetched this expiry's chain for execution
    #    pricing, that SAME payload is reused: the candidate's evidence and
    #    the execution fill prices then come from one authoritative broker
    #    read, so the recorded reference timestamp cannot describe a
    #    different snapshot than the prices that actually fill.
    snapshot = (chains or {}).get(expiry)
    if snapshot is None:
        try:
            snapshot = await (fetch_chain or _default_fetch_chain)(
                symbol, expiry, market_data_token)
        except PaperExecutionError:
            raise
        except Exception as exc:
            raise _fail(
                "CHAIN_DATA_MISSING",
                "the live option chain could not be acquired; order was not "
                "executed") from exc
    chain = snapshot

    # 3. Broker instrument keys (existing adapter rule).
    try:
        instrument_keys = await (resolve_keys or _default_resolve_keys)(
            legs, market_data_token)
    except PaperExecutionError:
        raise
    except Exception as exc:
        raise _fail(
            "CHAIN_DATA_MISSING",
            "broker instrument keys could not be resolved from the live "
            "chain; order was not executed") from exc

    # 4. Authoritative reference timestamp from the chain evidence itself.
    sides = _extract_sides(chain)
    reference_ts = _reference_ts(sides, received_at)

    # 5. D1 prior-OI state from server-side OptionCandle history.
    prior_oi_by_key = _prior_oi_state(
        db, [key for key in instrument_keys if key], reference_ts)

    # 6. Prior stored spot closes (real candles only).
    spot_closes, prev_spot = _spot_history(db, reference_ts)

    # 7. Pure evidence → candidate core (fail-closed).
    strategy_id = request.strategy_id or "paper-entry"
    try:
        produced = produce_candidate_core(
            chain=chain,
            instrument_keys=instrument_keys,
            prior_oi_by_key=prior_oi_by_key,
            spot_closes=spot_closes,
            prev_spot=prev_spot,
            received_at=received_at,
            id_seed=request.client_order_id.replace(":", "-"),
            strategy_id=strategy_id,
            legs=legs,
            underlying=symbol,
        )
    except ProducerError as exc:
        raise _fail(exc.code, str(exc)) from exc

    # 8. Existing sanctioned bridge → Day-32 gate results already applied →
    #    Day-33 risk inside the atomic choke point.
    bridge = execute_gated or execute_gated_paper_entry
    return bridge(
        user_id,
        db,
        client_order_id=request.client_order_id,
        symbol=symbol,
        legs=list(request.legs),
        opportunity=produced.opportunity,
        ranked_strikes=produced.ranked_strikes,
        evaluation=produced.evaluation,
        prices=prices,
        strategy_id=produced.strategy_id,
        strategy_tag=request.strategy_tag,
        starting_capital=request.starting_capital,
        reference_timestamp=produced.reference_timestamp,
    )
