"""Issue #17 — historical sample loader (Phase 1).

Reconstructs research sessions from the repository's own authorized local
candle store (``nifty_candles`` / ``option_candles`` / ``contract_specs`` —
Phase 7.7/7.8/7.13 backfill data captured via the Upstox expired-instruments
APIs already used by this project). No new data vendor, no scraping.

Extraction rules (documented, deterministic):

* A session is a date that has BOTH NIFTY index candles (``nifty_candles``)
  and option candles (``option_candles``).
* **Chain continuity:** a session is only ingested when the previous
  captured session exists within ``MAX_CANDIDATE_GAP_DAYS`` calendar days.
  Change-style features (OI change, IV skew change, …) compare against that
  previous captured session.  This store captures weekly expiry days
  (Tuesday→Thursday over the years), so the natural cadence is ~7 days;
  longer gaps (14–20 days in this store) would make "change" spans
  misleading and are skipped, never interpolated.
* ``prior_close`` for session T is **T's own close** at the cutoff — the
  close that precedes the predicted T+1 open (``gap = next_open − T_close``).
  The previous session's close is never used as the gap base.
* The research cutoff for session T is the last option-candle timestamp of
  that date (terminal candle; typically 15:27 IST).
* The chain snapshot is the front expiry (nearest expiry >= T) restricted
  to NIFTY CE/PE contracts, taking the last candle at-or-before the cutoff
  per instrument.  ``iv``/greeks/bid-ask are absent from the candle store
  and stay None (missing-is-not-zero is preserved downstream).
* Futures and India VIX tables do not exist in the candle store; those
  underlying fields stay None and their features degrade explicitly.

Target construction stays in the pipeline (``attach_realized_target``).
Processing is strictly chronological **per session**: features(T) →
predictions(T) → attach realized target(T).  Predictions for T query only
sessions strictly earlier with attached targets, so T's own target never
exists at prediction time, while T+1's prediction may legitimately use
T's realized target as historical information.  Nothing here can leak a
target into feature generation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import ContractSpec, NiftyCandle, OptionCandle
from app.research.gap_pipeline import (
    BASELINE,
    POS_STYLE,
    SOS,
    attach_realized_target,
    build_and_store_features,
    generate_and_store_predictions,
    ingest_session_snapshots,
    run_comparison_backtest,
    store_backtest_result,
)

logger = logging.getLogger(__name__)

# Maximum calendar distance between consecutive captured sessions for
# change-style features to remain meaningful (weekly expiry cadence ≈ 7d).
MAX_CANDIDATE_GAP_DAYS = 12

# Source tables the historical loader requires in the candle store. The
# CLI validates these via schema inspection and NEVER creates them — the
# source DB is an input, not an application database.
REQUIRED_SOURCE_TABLES = ("nifty_candles", "option_candles", "contract_specs")

# Candle-store timestamps are IST wall-clock; the canonical
# HistoricalGreeksEngine expects UTC valuation timestamps. IST = UTC + 5:30.
IST_TO_UTC = timedelta(hours=5, minutes=30)

# Chain-row option types vs the engine's CE/PE convention.
_TYPE_TO_ENGINE = {"CALL": "CE", "PUT": "PE"}

# Enrichment provenance marker (recorded per session in completeness_detail).
GREEKS_ENGINE_LABEL = "HISTORICAL_GREEKS_ENGINE"

__all__ = [
    "HistoricalSession",
    "extract_historical_sessions",
    "run_historical_sample",
]


@dataclass
class HistoricalSession:
    """One extracted, still-target-free research session."""

    session_date: str
    cutoff: datetime
    prior_close: float
    underlying: dict[str, Any]
    chain: list[dict[str, Any]] = field(default_factory=list)
    next_session_date: str | None = None
    next_open: float | None = None
    next_open_ts: datetime | None = None
    enrichment: dict[str, Any] | None = None  # Phase-2 greeks metadata


def _index_sessions(db: Session, symbol: str = "NIFTY") -> dict[str, list[NiftyCandle]]:
    """All index session dates -> that date's candles ordered by open_time."""
    rows = (
        db.query(NiftyCandle)
        .filter(NiftyCandle.symbol == symbol, NiftyCandle.interval == "3min")
        .order_by(NiftyCandle.open_time)
        .all()
    )
    out: dict[str, list[NiftyCandle]] = {}
    for r in rows:
        out.setdefault(r.open_time.strftime("%Y-%m-%d"), []).append(r)
    return out


def _option_cutoffs(db: Session) -> dict[str, datetime]:
    """Option-data session date -> last option-candle timestamp that day."""
    days = (
        db.query(
            func.date(OptionCandle.open_time).label("d"),
            func.max(OptionCandle.open_time).label("last"),
        )
        .group_by(func.date(OptionCandle.open_time))
        .all()
    )
    return {str(d): last for d, last in days}


def _front_expiry_chain(
    db: Session,
    session_date: str,
    cutoff: datetime,
) -> list[dict[str, Any]]:
    """Last-at-or-before-cutoff candle per NIFTY CE/PE instrument, front expiry.

    Front expiry = minimum ``expiry`` (YYYY-MM-DD strings sort correctly)
    among the NIFTY option contracts that actually traded ``session_date``.
    """
    day_rows = (
        db.query(OptionCandle, ContractSpec)
        .join(ContractSpec, ContractSpec.instrument_key == OptionCandle.instrument_key)
        .filter(
            func.date(OptionCandle.open_time) == session_date,
            OptionCandle.open_time <= cutoff,
            ContractSpec.underlying == "NIFTY",
            ContractSpec.instrument_type.in_(("CE", "PE")),
        )
        .all()
    )
    if not day_rows:
        return []
    front_expiry = min(spec.expiry for _, spec in day_rows if spec.expiry >= session_date)
    # index 0 of each row tuple is the OptionCandle
    last_by_key: dict[str, tuple[OptionCandle, ContractSpec]] = {}
    for candle, spec in day_rows:
        if spec.expiry != front_expiry:
            continue
        prev = last_by_key.get(candle.instrument_key)
        if prev is None or candle.open_time >= prev[0].open_time:
            last_by_key[candle.instrument_key] = (candle, spec)
    chain: list[dict[str, Any]] = []
    for candle, spec in last_by_key.values():
        otype = "CALL" if spec.instrument_type == "CE" else "PUT"
        chain.append(
            {
                "strike": float(spec.strike_price),
                "option_type": otype,
                "expiry": spec.expiry,
                "ltp": float(candle.close),
                "bid": None,
                "ask": None,
                "bid_qty": None,
                "ask_qty": None,
                "volume": float(candle.volume or 0.0),
                "open_interest": float(candle.open_interest or 0.0),
                "change_in_oi": None,  # derived causally from the prior session
                "iv": None,  # candle store has no IV — stays missing, never 0
                "delta": None,
                "gamma": None,
                "vega": None,
                "theta": None,
                "timestamp": candle.open_time.isoformat(),
            }
        )
    chain.sort(key=lambda r: (r["strike"], r["option_type"]))
    return chain


def _enrich_chain_with_greeks(
    chain: list[dict[str, Any]],
    spot: float,
    cutoff_ist: datetime,
) -> dict[str, Any]:
    """Phase-2 enrichment: attach IV + Black-Scholes Greeks to chain rows.

    Reuses the canonical Phase 7.19B engine (``HistoricalGreeksEngine``
    math — ``calculate_greeks_for_candle`` / ``compute_time_to_expiry``)
    WITHOUT duplicating or modifying it:

    * valuation timestamp = the session's research cutoff, converted
      IST→UTC (−5:30) per the engine's UTC contract;
    * S = the index close aligned at the cutoff (same value as the
      session's ``spot_close`` — no EOD value is ever used);
    * market price = the cutoff candle close (LTP proxy);
    * T is computed per row against that row's own expiry;
    * rows where the IV solver fails (e.g. deep-ITM quotes below
      intrinsic) keep ``iv``/Greeks = None — missing stays missing, and
      the failure is counted, never patched.

    Returns per-session enrichment metadata (provenance + coverage).
    """
    from app.services.historical_greeks import (
        DEFAULT_CALC_VERSION,
        calculate_greeks_for_candle,
        compute_time_to_expiry,
    )

    cutoff_utc = cutoff_ist - IST_TO_UTC
    iv_ok = delta_ok = gamma_ok = vega_ok = theta_ok = 0
    t_expired = no_iv = 0
    for row in chain:
        strike = row.get("strike")
        ltp = row.get("ltp")
        expiry = row.get("expiry")
        otype = _TYPE_TO_ENGINE.get(row.get("option_type") or "")
        if otype is None or strike is None or ltp is None or expiry is None:
            continue
        t_years = compute_time_to_expiry(cutoff_utc, expiry)
        result = calculate_greeks_for_candle(
            option_type=otype,
            S=float(spot),
            K=float(strike),
            T=t_years,
            market_price=float(ltp),
        )
        row["iv"] = result.implied_volatility
        row["delta"] = result.delta
        row["gamma"] = result.gamma
        row["vega"] = result.vega
        row["theta"] = result.theta
        if result.implied_volatility is not None:
            iv_ok += 1
        if result.delta is not None:
            delta_ok += 1
        if result.gamma is not None:
            gamma_ok += 1
        if result.vega is not None:
            vega_ok += 1
        if result.theta is not None:
            theta_ok += 1
        if t_years <= 0:
            t_expired += 1
        elif result.implied_volatility is None:
            no_iv += 1
    n = len(chain)
    return {
        "engine": GREEKS_ENGINE_LABEL,
        "calc_version": DEFAULT_CALC_VERSION,
        "tz_rule": "candle store holds IST wall-clock; converted IST→UTC (−5:30) for the canonical engine",
        "valuation": "research cutoff candle only (no EOD alignment)",
        "rows": n,
        "iv_rows": iv_ok,
        "delta_rows": delta_ok,
        "gamma_rows": gamma_ok,
        "vega_rows": vega_ok,
        "theta_rows": theta_ok,
        "expired_at_cutoff": t_expired,
        "no_iv": no_iv,
    }


def extract_historical_sessions(
    store: Session,
    symbol: str = "NIFTY",
    start: str | None = None,
    end: str | None = None,
    enrich_greeks: bool = False,
) -> list[HistoricalSession]:
    """Extract all eligible sessions (see module docstring rules)."""
    index = _index_sessions(store, symbol)
    cutoffs = _option_cutoffs(store)
    dates = sorted(set(index) & set(cutoffs))
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]

    sessions: list[HistoricalSession] = []
    prev_candidate: str | None = None
    for day in dates:
        if prev_candidate is None:
            # First candidate: change features need a prior captured session.
            prev_candidate = day
            continue
        gap_days = (date.fromisoformat(day) - date.fromisoformat(prev_candidate)).days
        too_far = gap_days > MAX_CANDIDATE_GAP_DAYS
        prev_candidate = day
        if too_far:
            continue
        prev_index_dates = sorted(index)
        j = prev_index_dates.index(day)
        # Next index session (for the realized-open target).
        next_index_date = prev_index_dates[j + 1] if j + 1 < len(prev_index_dates) else None
        if next_index_date is None:
            continue

        cutoff = cutoffs[day]
        day_candles = [c for c in index[day] if c.open_time <= cutoff]
        if not day_candles:
            continue

        underlying = {
            "spot_open": float(day_candles[0].open),
            "spot_high": float(max(c.high for c in day_candles)),
            "spot_low": float(min(c.low for c in day_candles)),
            "spot_close": float(day_candles[-1].close),
            "spot_ltp": float(day_candles[-1].close),
            "futures_ltp": None,
            "futures_oi": None,
            "futures_volume": None,
            "india_vix": None,
        }
        chain = _front_expiry_chain(store, day, cutoff)
        if not chain:
            continue
        enrichment: dict[str, Any] | None = None
        if enrich_greeks:
            enrichment = _enrich_chain_with_greeks(
                chain, underlying["spot_close"], cutoff
            )
        sessions.append(
            HistoricalSession(
                session_date=day,
                cutoff=cutoff,
                # The close PRIOR to the predicted gap is session T's own
                # final close at the cutoff — never the previous day's.
                prior_close=float(day_candles[-1].close),
                underlying=underlying,
                chain=chain,
                next_session_date=next_index_date,
                next_open=float(next_candles[0].open) if (next_candles := index[next_index_date]) else None,
                next_open_ts=next_candles[0].open_time if next_candles else None,
                enrichment=enrichment,
            )
        )
    return sessions


def _completeness_summary(sessions: Sequence[HistoricalSession]) -> dict[str, Any]:
    """Which feature families the store actually supports, per session."""
    n = len(sessions)
    if n == 0:
        return {"sessions": 0}
    def frac(pred) -> float:
        return round(sum(1 for s in sessions if pred(s)) / n, 3)

    total_rows = sum(len(s.chain) for s in sessions)

    def row_pct(field: str) -> float | None:
        if total_rows == 0:
            return None
        have = sum(
            1
            for s in sessions
            for r in s.chain
            if r.get(field) is not None
        )
        return round(have / total_rows, 3)

    summary: dict[str, Any] = {
        "sessions": n,
        "spot_ohlc_present": frac(lambda s: s.underlying["spot_close"] is not None),
        "chain_present": frac(lambda s: len(s.chain) > 0),
        "oi_present": frac(
            lambda s: any(r["open_interest"] is not None for r in s.chain)
        ),
        "volume_present": frac(lambda s: any((r["volume"] or 0) > 0 for r in s.chain)),
        "iv_present": frac(lambda s: any(r["iv"] is not None for r in s.chain)),
        "greeks_present": frac(
            lambda s: any(r["delta"] is not None or r["gamma"] is not None for r in s.chain)
        ),
        "bid_ask_present": frac(lambda s: any(r["bid"] is not None for r in s.chain)),
        "futures_present": frac(lambda s: s.underlying["futures_ltp"] is not None),
        "india_vix_present": frac(lambda s: s.underlying["india_vix"] is not None),
        "chain_rows": total_rows,
    }
    # Per-row coverage percentages (Phase 2 completeness audit). A family
    # that is present in zero rows reports 0.0 — present-but-empty is
    # distinguishable from not-applicable only via the *_present flags.
    for field in ("iv", "delta", "gamma", "vega", "theta"):
        summary[f"{field}_rows_pct"] = row_pct(field)
    # Eligibility profile from MEASURED data (never assumed):
    #   CORE          — spot + chain OI/volume (Phase 1 candle-store baseline)
    #   CORE+GREEKS   — CORE + IV/Greeks/GEX activatable from the same store
    #   FULL          — additionally futures + India VIX + bid/ask (not
    #                   available in any authorized historical source; kept
    #                   for documentation, never claimed)
    has_greeks = bool(summary["iv_present"])
    has_fut = bool(summary["futures_present"])
    has_vix = bool(summary["india_vix_present"])
    has_ba = bool(summary["bid_ask_present"])
    if has_fut and has_vix and has_ba and has_greeks:
        summary["profile"] = "FULL"
    elif has_greeks:
        summary["profile"] = "CORE+GREEKS"
    else:
        summary["profile"] = "CORE"
    return summary


def run_historical_sample(
    db: Session,
    store: Session,
    start: str | None = None,
    end: str | None = None,
    flat_band_pct: float = 0.001,
    enrich_greeks: bool = False,
) -> dict[str, Any]:
    """Full Phase-1 historical sample: ingest → per-session causal processing
    → deterministic backtests.

    Ordering (load-bearing):

    * Pass 1 — all immutable source snapshots are ingested first (raw data
      never depends on derived state).
    * Pass 2 — each session T, chronologically: build features(T) (uses only
      information available by T's cutoff), generate predictions(T) (uses
      only sessions strictly earlier **with attached targets**), then attach
      T's realized T+1 target.  Consequence: T's own target never exists when
      T is predicted, while T+1's prediction may legitimately use T's
      realized target as historical information.
    * Pass 3 — deterministic chronological backtests per model, last.
    """
    extracted = extract_historical_sessions(
        store, start=start, end=end, enrich_greeks=enrich_greeks
    )
    if not extracted:
        return {"sessions": 0, "note": "no eligible sessions in the candle store"}

    # Pass 1 — immutable snapshots (ingest).
    for s in extracted:
        ingest_session_snapshots(
            db,
            session_date=s.session_date,
            cutoff_timestamp=s.cutoff,
            prior_close=s.prior_close,
            underlying=s.underlying,
            chain=s.chain,
        )
    db.commit()

    # Phase-2 separation: enriched sessions carry their provenance and
    # measured eligibility profile ON the session row, so Phase 1 and
    # Phase 2 samples are distinguishable inside any research DB.
    if enrich_greeks:
        from app.models import GapPredictionSession as _GPS

        for s in extracted:
            if s.enrichment is None:
                continue
            row = (
                db.query(_GPS)
                .filter(
                    _GPS.symbol == "NIFTY",
                    _GPS.session_date == s.session_date,
                )
                .one_or_none()
            )
            if row is not None:
                rows = s.enrichment["rows"] or 1
                row.completeness = "ENRICHED_GREEKS"
                row.completeness_detail = json.dumps(
                    {
                        "profile": "CORE+GREEKS",
                        "enrichment": s.enrichment,
                        "coverage_pct": {
                            "iv": round(s.enrichment["iv_rows"] / rows, 3),
                            "delta": round(s.enrichment["delta_rows"] / rows, 3),
                            "gamma": round(s.enrichment["gamma_rows"] / rows, 3),
                            "vega": round(s.enrichment["vega_rows"] / rows, 3),
                            "theta": round(s.enrichment["theta_rows"] / rows, 3),
                        },
                    },
                    default=str,
                )
        db.commit()

    # Pass 2 — per-session features → predictions → realized target, in
    # chronological order. Causality: prediction T may only ever see targets
    # of sessions strictly earlier than T, which is exactly the pipeline's
    # own query; T's target is created only after T has been predicted.
    attached = 0
    for s in extracted:
        build_and_store_features(db, s.session_date)
        generate_and_store_predictions(db, s.session_date)
        if s.next_open is not None and s.next_session_date is not None:
            row = attach_realized_target(
                db,
                s.session_date,
                s.next_session_date,
                s.next_open,
                s.next_open_ts,
                flat_band_pct=flat_band_pct,
            )
            if row is not None:
                attached += 1
    db.commit()

    # Pass 4 — deterministic chronological backtests per model.
    results: dict[str, Any] = {}
    no_edge: dict[str, int] = {}
    for model in (BASELINE, POS_STYLE, SOS):
        bt = run_comparison_backtest(db, model)
        results[model] = bt
        no_edge[model] = int(bt["metrics"].get("n_no_edge") or 0)
        store_backtest_result(
            db,
            model,
            period_start=extracted[0].session_date,
            period_end=extracted[-1].session_date,
            result=bt,
        )
    db.commit()

    # Target distribution.
    from app.models import GapPredictionSession

    dist: dict[str, int] = {}
    for (gc,) in (
        db.query(GapPredictionSession.gap_class)
        .filter(GapPredictionSession.gap_class.isnot(None))
        .all()
    ):
        dist[gc] = dist.get(gc, 0) + 1

    summary_data_completeness = _completeness_summary(extracted)
    summary = {
        "sessions_extracted": len(extracted),
        "targets_attached": attached,
        "period_start": extracted[0].session_date,
        "period_end": extracted[-1].session_date,
        "data_completeness": summary_data_completeness,
        "target_distribution": dist,
        "flat_band_pct": flat_band_pct,
        "no_edge_counts": no_edge,
        "enrichment": enrich_greeks,
        "enrichment_aggregate": (
            {
                "profile": summary_data_completeness.get("profile"),
                "iv_rows_pct": summary_data_completeness.get("iv_rows_pct"),
                "delta_rows_pct": summary_data_completeness.get("delta_rows_pct"),
                "gamma_rows_pct": summary_data_completeness.get("gamma_rows_pct"),
                "vega_rows_pct": summary_data_completeness.get("vega_rows_pct"),
                "theta_rows_pct": summary_data_completeness.get("theta_rows_pct"),
                "expired_at_cutoff_total": sum(
                    (s.enrichment or {}).get("expired_at_cutoff", 0)
                    for s in extracted
                ),
                "no_iv_total": sum(
                    (s.enrichment or {}).get("no_iv", 0) for s in extracted
                ),
            }
            if enrich_greeks
            else None
        ),
        "backtests": results,
    }
    logger.info(
        "historical sample: %s",
        json.dumps({k: v for k, v in summary.items() if k != "backtests"}, default=str),
    )
    return summary
