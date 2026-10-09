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
    instrument key.  The production persistence prerequisite is currently
    UNSATISFIED: ``OptionCandle`` — the only OI-bearing per-key series —
    is populated exclusively from the Upstox EXPIRED-instruments API: see
    the ``OptionCandle`` docstring in ``app/models.py``, the module docstring
    of ``app/services/option_candles.py``, ``daily_ingestion.
    _ingest_option_candles`` (selects ``ContractSpec.expiry <= today`` and
    calls ``get_expired_historical_candles``), ``backfill_orchestrator`` and
    ``app/tools/option_candle_backfill`` (same expired path).  No production
    persistence path stores prior OI for a still-unexpired contract, so in
    production this producer cannot compute ΔOI and fails closed
    (``EVIDENCE_INSUFFICIENT`` / ``CHAIN_DATA_MISSING``) with zero writes.
    UPSTREAM UPSTOX CAPABILITY IS NOW VERIFIED: an authenticated live probe
    (the PR #125 probe, exposed read-only through the Day-50 admin
    verification seam) returned 129 historical 3-minute candles for the active
    NIFTY 22400 CE, instrument ``NSE_FO|40687``, authoritative expiry
    ``2026-10-06``, with all 129 open-interest values non-null.  The broker can
    therefore serve the observation D1 requires.  PRODUCTION LIVE-OI
    PERSISTENCE IS NOT IMPLEMENTED: a probe result is read-only and persists
    nothing, so D1 still has no stored prior-OI observation and production
    paper-entry capability remains blocked at that gate.  Repository absence
    and upstream incapability are different claims: only the first holds now.
    That stays true until live option-OI persistence lands as separate
    architecture work —
    never widen the window, never reuse expired-contract history as if it
    were live, never match by strike text, never substitute current OI for
    prior OI, and never coerce missing history to zero.

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

from dataclasses import dataclass, replace
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
    evaluate_portfolio,
)
# The repository's SINGLE owned GEX convention (docs/GEX_V1_0_SPEC.md,
# Invariant 16).  Reused verbatim so the Day-30 GEX factor is a measurement
# of real gamma/OI rather than a fabricated placeholder; no second GEX
# formula is introduced here.
from app.services.historical_gex import compute_raw_gex, compute_signed_gex
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
from app.utils.market_time import MARKET_OPEN, to_ist_naive

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
    market_side: str  # "call" | "put" — the broker DATA side, not the trade side
    ltp: float
    oi: float | None
    volume: float | None
    iv: float | None
    gamma: float | None
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


def _quality(observation, *, received_at: datetime) -> QualityResult:
    """Measure chain quality with the REAL Day-12 engine.

    The previous implementation returned a hard-coded EXCELLENT/100 with no
    dimensions, which is fabricated evidence: it asserted measured freshness,
    completeness, validity and provenance that were never measured, and no
    quality requirement could ever fail because of it.

    Freshness is evaluated against the SERVER RECEIPT time, not against the
    broker's own quote timestamp.  The Day-12 engine computes
    ``age = reference_time - observation.market_timestamp``, so bounding the
    engine by the broker timestamp would make every snapshot age 0 by
    construction — a stale snapshot could never be detected as stale.  Two
    meanings are kept distinct here:

    * ``reference_timestamp`` (the candidate's authoritative market/evidence
      time) remains the broker's own quote timestamp;
    * ``received_at`` (captured once per request) is the freshness clock.

    No second wall-clock read is introduced: the engine is bounded by the
    request's single captured receipt time.
    """
    from app.market_data.quality import MarketDataQualityEngine

    return MarketDataQualityEngine().evaluate(
        observation, reference_time=received_at)


def _reference_ts_from_index(
    index: dict[tuple[float, str], ChainSide], received_at: datetime,
) -> datetime:
    """Authoritative reference timestamp of the evidence: the broker's own
    quote timestamp when present (latest across the identity-bound index
    sides), else the recorded receive-at moment of the fetch.  No second
    wall-clock read.

    This is the single evidence-clock source for Slice A: the index is built
    once from the canonical chain rows and reused for every lookup, so the
    reference timestamp cannot describe a different row set than the one the
    candidate actually measures.
    """
    stamped = [s.quote_ts for s in index.values() if s.quote_ts is not None]
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
    """Prior stored NIFTY closes of the REFERENCE SESSION, strictly before the
    reference timestamp (oldest→newest), plus the immediately-prior close.

    Real stored candles only — nothing interpolated.  Three bounds are
    applied, all on the stored naive-IST candle clock that
    ``NiftyCandle.open_time`` is written in (``app.utils.market_time``):

    * the explicit underlying predicate ``symbol == "NIFTY"`` — the evidence
      chain is NIFTY-only (Slice A), and a foreign symbol's closes are not
      NIFTY market history;
    * ``open_time < cutoff`` — history is strictly prior to the snapshot;
    * ``open_time >= session open (09:15 IST) of the cutoff's own date`` — the
      repository's own session boundary (``app.utils.market_time.MARKET_OPEN``),
      so a PREVIOUS session's closes can never be presented as this snapshot's
      spot/regime/flow/institutional/synthesis evidence.

    When the reference session holds no (or too little) genuine history the
    result is empty/partial rather than borrowed stale rows: the caller gets
    missing evidence and the chain fails closed instead of silently reusing
    yesterday's closes.
    """
    cutoff = _candle_clock(reference_ts)
    session_open = cutoff.replace(
        hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    candles = db.execute(
        select(NiftyCandle)
        .where(
            NiftyCandle.symbol == SLICE_A_UNDERLYING,
            NiftyCandle.interval == OC_INTERVAL,
            NiftyCandle.open_time < cutoff,
            NiftyCandle.open_time >= session_open,
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


def _contract_quantity(leg: dict) -> float:
    """The leg's CONTRACT quantity (``quantity`` is LOTS × ``lot_size``)."""
    return float(leg["quantity"]) * float(leg["lot_size"])


def _contract_legs(legs: list[dict]) -> list[dict]:
    """Request legs with their CONTRACT quantity resolved.

    ``ExecutionLegIn.quantity`` is LOTS and ``lot_size`` is CONTRACTS PER
    LOT, while the domain ``OptionLeg.quantity`` is CONTRACTS.  Every
    candidate/payoff/risk figure must therefore run on
    ``contracts = lots × lot_size``; passing lots directly would understate
    payoff and risk by the lot size.  The user-facing request contract is
    unchanged.  A leg without a positive quantity/lot size is not riskable and
    fails closed — the lot size is never silently assumed to be 1.
    """
    resolved: list[dict] = []
    for leg in legs:
        lots = _finite(leg.get("quantity"))
        lot_size = _finite(leg.get("lot_size"))
        if lots is None or lots <= 0 or lot_size is None or lot_size <= 0:
            raise ProducerError(
                "CANDIDATE_NOT_ELIGIBLE",
                "every requested leg needs a positive quantity (LOTS) and "
                "lot_size (CONTRACTS PER LOT); entry fails closed")
        resolved.append({**leg, "contract_quantity": lots * lot_size})
    return resolved


def _classify_tail(legs: list[dict]) -> TailClass:
    """Structural payoff tail from the SIGNED CONTRACT quantities.

    Classification only — never a probability and never a second payoff
    engine: the existing Day-18 quant engine remains authoritative for P&L.
    The tail is decided by each option side's NET signed contract exposure, so
    ratio structures are classified correctly; a strike-order-only check is
    not sufficient (it cannot tell 2× short from 1× long):

    * calls, net signed contracts < 0 ⇒ short-call exposure remains above
      every long call ⇒ UNLIMITED_LOSS;
    * calls, net signed contracts > 0 ⇒ uncapped long-call exposure ⇒
      UNLIMITED_GAIN;
    * puts, net signed contracts > 0 ⇒ uncapped long-put exposure ⇒
      UNLIMITED_GAIN.  A net SHORT put is never UNLIMITED_LOSS: a put's
      intrinsic value is capped at its own strike, so short-put loss is
      structurally bounded;
    * net-flat exposure on both sides ⇒ NONE (fully covered / bounded).
    """
    net_calls = 0.0
    net_puts = 0.0
    for leg in legs:
        signed = _SIDE_SIGN[leg["direction"]] * _contract_quantity(leg)
        if leg["option_type"] == "call":
            net_calls += signed
        else:
            net_puts += signed
    if net_calls < 0:
        return TailClass.UNLIMITED_LOSS
    if net_calls > 0 or net_puts > 0:
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

    SAMPLE SEMANTICS (audited Day-50 decision, stated explicitly rather than
    implied): ``max_profit`` / ``max_loss`` are the extremes of THIS sampled
    grid — ±10% of the observed spot about expiry — and are NOT claimed to be
    global structural extremes.  The Day-31 ``PayoffEvidence`` contract this
    feeds declares no global-extreme requirement (it consumes the supplied
    metrics verbatim and never re-derives a curve), and structural
    unboundedness is carried separately and exactly by ``tail``.  No Day-33
    rule consumes a global extreme either: ``PAPER_ENTRY_POLICY`` leaves every
    numeric loss limit unconfigured, and no threshold is invented here.  The
    grid is therefore the approved Day-50 evidence basis, and this docstring
    is its record.
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
        net += _SIDE_SIGN[leg["direction"]] * leg["ltp"] * _contract_quantity(leg)

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

def _spread_measure(side: ChainSide) -> tuple[float, float] | None:
    """Measured ``(score, spread_bps)`` for a two-sided quote, else ``None``.

    ``None`` means genuinely unmeasured: a one-sided or inverted book is not
    a spread, and scoring it neutral would present fabricated liquidity as a
    measurement.
    """
    if side.bid is None or side.ask is None or side.bid <= 0 or side.ask < side.bid:
        return None
    mid = (side.bid + side.ask) / 2.0
    if mid <= 0:
        return None
    spread_bps = (side.ask - side.bid) / mid * 10_000.0
    if not isfinite(spread_bps):
        return None
    return max(0.0, min(1.0, 1.0 - spread_bps / 200.0)), spread_bps


def _side_signed_gex(side: ChainSide, spot: float) -> float | None:
    """The MEASURED signed GEX of one chain side, or ``None``.

    Computed with the repository's single owned convention
    (``app.services.historical_gex`` → ``docs/GEX_V1_0_SPEC.md``): raw GEX =
    gamma × OI × spot² × 0.01, signed ``CE = +raw`` / ``PE = −raw``.  Both
    inputs are real broker measurements already carried by the canonical
    chain (gamma from option Greeks, OI from market data).  When either is
    absent — or when gamma is negative, the same exclusion the repository's
    GEX ingestion applies — the value stays missing, so the GEX factor is
    suppressed rather than fabricated to satisfy the nine-factor contract.
    """
    if spot <= 0 or side.oi is None or side.gamma is None:
        return None
    if side.gamma < 0 or side.oi <= 0:
        return None
    raw = compute_raw_gex(side.gamma, side.oi, spot)
    if not isfinite(raw):
        return None
    return compute_signed_gex(
        "CE" if side.market_side == "call" else "PE", raw)


def _gex_reference(
    index: dict[tuple[float, str], ChainSide], spot: float,
) -> float | None:
    """GEX normalisation reference: the snapshot's largest |signed GEX|.

    The Day-30 contract requires this boundary to supply a normalized
    suitability in [0,1], while the GEX spec forbids inventing a fixed
    magnitude threshold ("thresholds must not be hard-coded without
    historical validation").  Slice A therefore normalizes each strike's
    measured |signed GEX| against the largest |signed GEX| measured in the
    SAME snapshot — deterministic, snapshot-local and threshold-free.
    ``None`` when the snapshot carries no measured GEX at all.
    """
    magnitudes = [abs(value) for value in
                  (_side_signed_gex(side, spot) for side in index.values())
                  if value is not None]
    return max(magnitudes) if magnitudes else None


def _distance_score(strike: float, spot: float) -> float:
    distance_pct = abs(strike - spot) / spot * 100.0
    return max(0.0, 1.0 - distance_pct / 5.0)


def _measured_factor(
    factor: RankingFactor, score: float | None, raw: float | None,
    provenance: Provenance,
) -> FactorObservation:
    """A factor that is usable ONLY when its measurement exists.

    ``score is None`` means the market measurement behind this factor is
    genuinely absent: the factor is emitted in the Day-12 ``INSUFFICIENT``
    state, which the existing Day-30 ``rank_strikes`` mechanism treats as
    unusable and therefore SUPPRESSES the candidate.  A measured value —
    including a measured zero — is emitted as a usable factor.  Missing
    evidence is never converted into a usable score here (no ``0.0`` volume
    score, no neutral spread/GEX, no IV-derived score without a measured IV).
    """
    if score is None:
        return FactorObservation(
            factor=factor, score=0.0, state=QualityState.INSUFFICIENT,
            raw=None, provenance=provenance)
    return FactorObservation(
        factor=factor, score=score, raw=raw, provenance=provenance)


def _strike_factors(
    side: ChainSide, delta_oi: float | None, spot: float,
    provenance: Provenance, gex_reference: float | None = None,
) -> tuple[FactorObservation, ...]:
    """The nine Day-30 factors: each a MEASUREMENT, or INSUFFICIENT.

    Every market factor is emitted only from a measurement actually present
    in this snapshot; an absent measurement yields an INSUFFICIENT factor and
    the candidate is suppressed by the existing Day-30 ranking mechanism
    rather than ranked on invented numbers.
    """
    iv = _scale_iv(side.iv)
    iv_measured = iv if (iv is not None and iv > 0) else None
    spread = _spread_measure(side)
    signed_gex = _side_signed_gex(side, spot)
    gex_score = None
    if signed_gex is not None and gex_reference is not None:
        gex_score = min(abs(signed_gex) / gex_reference, 1.0)
    volume = side.volume
    return (
        _measured_factor(
            RankingFactor.LIQUIDITY,
            min(volume / 100_000.0, 1.0) if volume is not None else None,
            volume, provenance),
        _measured_factor(
            RankingFactor.SPREAD_QUALITY,
            spread[0] if spread is not None else None,
            spread[1] if spread is not None else None, provenance),
        _measured_factor(
            RankingFactor.IV,
            min(iv_measured, 1.0) if iv_measured is not None else None,
            iv_measured, provenance),
        _measured_factor(
            RankingFactor.GREEKS,
            min(iv_measured * 4.0, 1.0) if iv_measured is not None else None,
            iv_measured, provenance),
        _measured_factor(
            RankingFactor.POSITIONING,
            (min(abs(delta_oi) / STRENGTH_REFERENCE_OI, 1.0)
             if delta_oi is not None else None),
            delta_oi, provenance),
        _measured_factor(
            RankingFactor.GEX, gex_score, signed_gex, provenance),
        FactorObservation(
            factor=RankingFactor.DISTANCE_TO_SPOT,
            score=_distance_score(side.strike, spot),
            raw=abs(side.strike - spot), provenance=provenance),
        # Declared components of THIS producer's own ranking objective
        # (``objective_id="day50-candidate-production"``), not market
        # measurements: the Day-30 contract requires the upstream boundary to
        # supply these normalized suitabilities, and Slice A's objective and
        # risk appetite are favourable by declaration (every numeric Day-33
        # limit is unconfigured in PAPER_ENTRY_POLICY).  No market
        # measurement is claimed for either factor.
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


def _resolved_identity_map(
    legs: list[dict], instrument_keys: list[str | None],
) -> dict[tuple[float, str], str | None]:
    """The RESOLVED broker instrument key for each requested (strike, side).

    ``instrument_keys`` is positional against ``legs`` (the adapter's
    ``resolve_instrument_keys`` preserves request order), so this is the one
    place where a requested leg's broker identity — read from the broker's own
    chain payload, never constructed from strike text — is associated with its
    ``(strike, option type)`` identity.  There is no second identity source.
    """
    if len(instrument_keys) != len(legs):
        raise ProducerError(
            "IDENTITY_MISMATCH",
            "the broker returned a different number of instrument identities "
            "than there are requested legs; entry fails closed")
    return {
        (float(leg["strike_price"]), leg["option_type"]): (key or None)
        for leg, key in zip(legs, instrument_keys)
    }


def _apply_identities(
    index: dict[tuple[float, str], ChainSide],
    identity: dict[tuple[float, str], str | None],
) -> dict[tuple[float, str], ChainSide]:
    """Bind each measured chain side to its authoritative broker identity.

    The RESOLVED key wins for a requested leg (the canonical
    ``transform_chain`` shape carries no per-side ``instrument_key`` at all).
    When the snapshot does carry a key for that same side it must AGREE with
    the resolved key: a disagreement means the chain row and the broker
    resolution name different contracts, which fails closed instead of
    silently trusting either one.  A side with no resolved counterpart (a
    strike nobody requested) keeps the broker key its own row carries, and
    stays missing when it carries none — a missing identity suppresses the
    strike through D1 rather than being guessed from strike text.
    """
    bound: dict[tuple[float, str], ChainSide] = {}
    for key, side in index.items():
        resolved = identity.get(key)
        snapshot_key = side.instrument_key
        if resolved is not None and snapshot_key is not None \
                and snapshot_key != resolved:
            raise ProducerError(
                "IDENTITY_MISMATCH",
                f"the canonical chain row for {side.strike:g} "
                f"{side.market_side} names broker instrument "
                f"{snapshot_key!r} while the broker resolved {resolved!r} for "
                "the requested leg; entry fails closed")
        bound[key] = replace(side, instrument_key=resolved or snapshot_key)
    return bound


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
    action / quantity`` (LOTS) and ``lot_size`` (CONTRACTS PER LOT); the
    candidate itself is built on CONTRACTS (``quantity × lot_size``).  Raises
    ``ProducerError`` whenever genuine evidence cannot produce an eligible
    candidate — nothing is fabricated.
    """
    legs = _contract_legs(legs)
    chain_rows = chain.get("chain") or []
    if not chain_rows:
        raise ProducerError(
            "CHAIN_DATA_MISSING", "the option chain carried no usable rows")
    spot = _finite(chain.get("underlying_spot_price"))
    if spot is None or spot <= 0:
        raise ProducerError(
            "CHAIN_DATA_MISSING", "chain carried no underlying spot price")

    # One identity-bound index, built once straight from the canonical chain
    # rows.  The canonical ``transform_chain`` shape carries no per-side
    # ``instrument_key``, so identity is never read from the strike text and
    # never assumed when the canonical payload omits it.
    side_index = _apply_identities(
        _build_side_index(chain),
        _resolved_identity_map(legs, instrument_keys),
    )
    if not side_index:
        raise ProducerError(
            "CHAIN_DATA_MISSING",
            "the canonical chain carried no priceable rows after identity "
            "binding; entry fails closed")

    provenance = _provenance(received_at)
    reference_ts = _reference_ts_from_index(side_index, received_at)
    spot_change = (spot - prev_spot) if prev_spot is not None else None
    expiry = str(legs[0]["expiration_date"]) if legs else None
    # Quality is MEASURED over this snapshot by the real Day-12 engine (never
    # asserted).  Freshness is judged against this request's captured receipt
    # time while ``reference_ts`` stays the broker's own market clock, so no
    # second wall-clock read is added and a stale snapshot cannot appear
    # fresh merely because its own quote stamp was used as "now".
    quality = _quality(
        _chain_observation(
            chain,
            symbol=underlying,
            expiry=expiry or "",
            received_at=received_at,
        ),
        received_at=received_at,
    )
    # D1 ΔOI is only meaningful against a measured GEX scale, so the
    # snapshot-local GEX reference is computed once from the same rows.
    gex_reference = _gex_reference(side_index, spot)
    sides = list(side_index.values())

    # -- measured per-strike rows with D1 ΔOI --------------------------------
    def _delta_for(strike: float, side_name: str) -> float | None:
        """ΔOI for one measured side, keyed by its AUTHORITATIVE identity.

        The prior observation is looked up with the RESOLVED broker
        instrument key (the identity the canonical chain rows carry after
        ``_apply_identities``), never with strike text and never with a key
        the canonical payload may or may not hold.  A side with no identity
        has no ΔOI, so it is suppressed by the existing D1 rule.
        """
        side = side_index.get((strike, side_name))
        if side is None or side.instrument_key is None:
            return None
        return _delta_oi(side.oi, prior_oi_by_key.get(side.instrument_key))

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
            factors=_strike_factors(
                side, delta, spot, provenance, gex_reference),
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
            # Domain quantity is CONTRACTS (lots × lot_size), so payoff, risk
            # and scenario evidence all run on the real position size.
            quantity=float(leg["contract_quantity"]),
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
                market_side=side_name,
                ltp=ltp,
                oi=_finite(side.get("oi")),
                volume=_finite(side.get("volume")),
                iv=_finite(side.get("iv")),
                gamma=_finite(side.get("gamma")),
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
    raw chain payload, never constructed from strike text.

    Known non-blocking follow-up (audited Day-50 finding, deferred on
    purpose): the adapter's ``resolve_instrument_keys`` performs its own raw
    chain read, so a new entry fetches the broker chain twice — once
    canonicalized for evidence/pricing and once raw for identity.  Reusing
    that single read would require the canonical ``transform_chain`` contract
    to carry a per-side ``instrument_key``, i.e. a change to the broker
    adapter's identity contract, which is out of this remediation's scope.
    Identity coherence is NOT affected: both reads are the same broker's
    payload for the same expiry, and any disagreement between a snapshot key
    and a resolved key fails closed in ``_apply_identities``.
    """
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
            "quantity": float(leg.quantity),   # LOTS
            "lot_size": float(leg.lot_size),  # CONTRACTS PER LOT
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

    # 4. Authoritative reference timestamp from the SAME identity-bound index
    #    the candidate core will build.  Slice A no longer reads a separate flat
    #    ``sides`` list for the evidence clock: that would let the wrapper's
    #    ``reference_ts`` be computed from a different row set than the one the
    #    candidate actually measures, so the reference timestamp and the
    #    candidate's evidence could silently diverge.
    side_index = _apply_identities(
        _build_side_index(chain),
        _resolved_identity_map(legs, instrument_keys),
    )
    if not side_index:
        raise _fail(
            "CHAIN_DATA_MISSING",
            "the canonical chain carried no priceable rows after identity "
            "binding; order was not executed")
    reference_ts = _reference_ts_from_index(side_index, received_at)

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
