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

# Phase-3 DTE buckets (reported as measured; never forced).
DTE_BUCKETS = ((0, "DTE0"), (2, "DTE1-2"), (7, "DTE3-7"))


def dte_bucket(dte: int | None) -> str:
    """Assign the measured DTE bucket (DTE>7 is the residual)."""
    if dte is None:
        return "unknown"
    for upper, label in DTE_BUCKETS:
        if dte <= upper:
            return label
    return "DTE>7"


def chain_value_provenance(enriched: bool) -> dict[str, str]:
    """Per-value provenance for one enriched/candle-only session (Phase 3).

    The candle store carries observed LTP/volume/OI; IV/Greeks are always
    engine-reconstructed (never observed); bid/ask are unavailable; OI
    change is causally derived from the prior session's stored snapshot.
    """
    greeks_class = "reconstructed" if enriched else "unavailable"
    return {
        "ltp": "observed",
        "volume": "observed",
        "open_interest": "observed",
        "change_in_oi": "derived",
        "bid": "unavailable",
        "ask": "unavailable",
        "bid_qty": "unavailable",
        "ask_qty": "unavailable",
        "iv": greeks_class,
        "delta": greeks_class,
        "gamma": greeks_class,
        "vega": greeks_class,
        "theta": greeks_class,
    }

# Enrichment provenance marker (recorded per session in completeness_detail).
GREEKS_ENGINE_LABEL = "HISTORICAL_GREEKS_ENGINE"

__all__ = [
    "HistoricalSession",
    "extract_historical_sessions",
    "run_historical_sample",
    "build_merged_store",
]


def build_merged_store(
    output_path: str,
    source_paths: Sequence[str],
) -> dict[str, Any]:
    """Build a Phase-3 working candle store by unioning authorized local
    backups (Issue #78).

    Precedence: the FIRST source wins — rows are copied with
    ``INSERT OR IGNORE`` against the natural unique keys, so where two
    sources contain the same (instrument, interval, open_time) candle the
    earlier-listed source's value is kept and later duplicates are skipped.
    Source ``id`` columns are deliberately NOT copied: two backups reuse
    overlapping ``id`` ranges, and copying them would silently drop source-2
    rows on primary-key collisions instead of deduping on the natural key.

    Timestamp normalization: the authorized backups store ``open_time`` in
    different conventions (the Oct–Nov 2024 daily option backfill is UTC;
    the 2026 captures are IST wall-clock; the 2024-11-01 Muhurat evening
    session is UTC in the older backup).  Each (table, date) is classified
    by evidence, not table-level guessing: both hypotheses (no shift, and
    +330 minutes UTC→IST) are tested against the authoritative IST session
    window for that date taken from the accumulated index candles, and the
    hypothesis that places strictly more of the date's candle opens inside
    the window wins.  A date whose convention cannot be resolved (no index
    anchor, or no separating evidence) is REFUSED — its rows are not copied
    and the refusal is recorded in provenance; nothing is silently guessed.
    Sources are opened read-only; the output is a NEW research working copy
    (never an application database, never one of the sources).

    A ``_store_provenance`` table records, per source: absolute path,
    SHA-256, row counts, and the per-date normalization decisions — making
    the merged dataset's composition auditable and reproducible.
    """
    import hashlib
    import sqlite3
    from pathlib import Path

    def _uri(p: str, mode: str) -> str:
        return f"file:///{Path(p).resolve().as_posix()}?{mode}"

    CANDLE_TABLES = ("nifty_candles", "option_candles")
    IST_SHIFT = "+330 minutes"  # UTC -> IST (IST = UTC+05:30)
    ZERO_FRACTION = ".000000"  # every source open_time carries a zero fraction

    out = sqlite3.connect(_uri(output_path, "mode=rwc"), uri=True)
    try:
        out.execute(
            "CREATE TABLE IF NOT EXISTS _store_provenance ("
            "source_path TEXT PRIMARY KEY, sha256 TEXT, dates TEXT)"
        )
        # Create the three source tables from the first source's schema.
        src0 = sqlite3.connect(_uri(source_paths[0], "mode=ro"), uri=True)
        for t in REQUIRED_SOURCE_TABLES:
            ddl = src0.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (t,),
            ).fetchone()
            if ddl is None:
                raise RuntimeError(f"source {source_paths[0]} lacks table {t}")
            out.execute(ddl[0])
        src0.close()

        def _session_window(d: str, attached_as: str) -> tuple[str, str] | None:
            """Authoritative IST session window (HH:MM:SS bounds) for date
            ``d`` from accumulated index candles (main first, then the
            source's own nifty_candles). None when nothing anchors the date."""
            for db in ("main", attached_as):
                row = out.execute(
                    f"SELECT MIN(substr(open_time,12,8)), MAX(substr(open_time,12,8)) "
                    f"FROM {db}.nifty_candles WHERE substr(open_time,1,10)=?",
                    (d,),
                ).fetchone()
                if row and row[0] and row[1]:
                    return (row[0], row[1])
            return None

        def _in_window_frac(
            attached_as: str, table: str, d: str, shifted: bool, w: tuple[str, str]
        ) -> float:
            """Fraction of the date's rows whose (possibly shifted) time of
            day falls inside the session window. Both sides compare as
            uniform HH:MM:SS strings."""
            total = out.execute(
                f"SELECT COUNT(*) FROM {attached_as}.{table} "
                f"WHERE substr(open_time,1,10)=?",
                (d,),
            ).fetchone()[0]
            if total == 0:
                return 0.0
            if shifted:
                expr = f"substr(datetime(substr(open_time,1,19), '{IST_SHIFT}'),12,8)"
            else:
                expr = "substr(open_time,12,8)"
            inside = out.execute(
                f"SELECT COUNT(*) FROM {attached_as}.{table} "
                f"WHERE substr(open_time,1,10)=? AND ? <= {expr} AND {expr} <= ?",
                (d, w[0], w[1]),
            ).fetchone()[0]
            return inside / total

        for i, sp in enumerate(source_paths):
            sha = hashlib.sha256(open(sp, "rb").read()).hexdigest()
            out.execute("ATTACH DATABASE ? AS src", (_uri(sp, "mode=ro"),))
            before = {
                t: out.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in REQUIRED_SOURCE_TABLES
            }
            tz_decisions: dict[str, list[str]] = {
                "shifted_utc_to_ist": [],
                "kept_ist": [],
                "refused": [],
            }
            for t in CANDLE_TABLES:
                cols = [
                    r[1] for r in out.execute(f"PRAGMA main.table_info({t})")
                    if r[1] != "id"  # never copy source ids (see docstring)
                ]
                dates = [
                    r[0]
                    for r in out.execute(
                        f"SELECT DISTINCT substr(open_time,1,10) FROM src.{t} ORDER BY 1"
                    )
                ]
                for d in dates:
                    w = _session_window(d, "src")
                    if w is None:
                        tz_decisions["refused"].append(f"{t}:{d}:no_index_anchor")
                        continue
                    f0 = _in_window_frac("src", t, d, False, w)
                    f330 = _in_window_frac("src", t, d, True, w)
                    if f0 == 0.0 and f330 == 0.0:
                        tz_decisions["refused"].append(f"{t}:{d}:ambiguous")
                        continue
                    if f330 > f0:
                        shift = True
                        tz_decisions["shifted_utc_to_ist"].append(f"{t}:{d}")
                    elif f0 > f330:
                        shift = False
                        tz_decisions["kept_ist"].append(f"{t}:{d}")
                    else:
                        tz_decisions["refused"].append(f"{t}:{d}:ambiguous")
                        continue
                    sel = []
                    for c in cols:
                        if c == "open_time" and shift:
                            sel.append(
                                f"datetime(substr(open_time,1,19), '{IST_SHIFT}')"
                                f" || '{ZERO_FRACTION}'"
                            )
                        else:
                            sel.append(c)
                    out.execute(
                        f"INSERT OR IGNORE INTO main.{t} ({','.join(cols)}) "
                        f"SELECT {', '.join(sel)} FROM src.{t} "
                        f"WHERE substr(open_time,1,10)=?",
                        (d,),
                    )
            # contract_specs carries no candle timestamps; copy by natural key.
            cols_specs = [
                r[1] for r in out.execute("PRAGMA main.table_info(contract_specs)")
                if r[1] != "id"
            ]
            out.execute(
                f"INSERT OR IGNORE INTO main.contract_specs "
                f"({','.join(cols_specs)}) SELECT {','.join(cols_specs)} "
                f"FROM src.contract_specs"
            )
            after = {
                t: out.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in REQUIRED_SOURCE_TABLES
            }
            dates = [
                r[0]
                for r in out.execute(
                    "SELECT DISTINCT substr(open_time,1,10) FROM option_candles "
                    "ORDER BY 1"
                )
            ]
            prov = {
                "priority": i,
                "row_counts": {
                    t: {"before": before[t], "after": after[t]}
                    for t in REQUIRED_SOURCE_TABLES
                },
                "tz_decisions": tz_decisions,
            }
            out.execute(
                "INSERT OR REPLACE INTO _store_provenance VALUES (?,?,?)",
                (sp, sha, json.dumps({"option_dates": dates, **prov})),
            )
            out.commit()  # release the write lock before DETACH
            out.execute("DETACH DATABASE src")
            logger.info(
                "merged %s (priority %d): added %d nifty, %d option, %d spec rows; "
                "tz: %d shifted, %d kept, %d refused",
                sp,
                i,
                after["nifty_candles"] - before["nifty_candles"],
                after["option_candles"] - before["option_candles"],
                after["contract_specs"] - before["contract_specs"],
                len(tz_decisions["shifted_utc_to_ist"]),
                len(tz_decisions["kept_ist"]),
                len(tz_decisions["refused"]),
            )
        out.commit()
        counts = {
            t: out.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in REQUIRED_SOURCE_TABLES
        }
        dates = [
            r[0]
            for r in out.execute(
                "SELECT DISTINCT substr(open_time,1,10) FROM option_candles ORDER BY 1"
            )
        ]
        return {
            "output": output_path,
            "sources": list(source_paths),
            "row_counts": counts,
            "option_dates": len(dates),
        }
    finally:
        out.close()


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
    # Phase-3 (#78) classification — from actual contract metadata, never
    # from data absence:
    dte_days: int | None = None            # front expiry − session date (days)
    is_expiry_session: bool | None = None  # dte_days == 0
    cutoff_kind: str | None = None         # "end_of_session" | "intraday"


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
        # Phase-3 classification from contract metadata: the chain rows all
        # carry the front expiry, so DTE = front_expiry − session_date.
        front_expiry = chain[0].get("expiry")
        try:
            dte_days = (
                date.fromisoformat(str(front_expiry)) - date.fromisoformat(day)
            ).days
        except (TypeError, ValueError):
            dte_days = None
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
                dte_days=dte_days,
                is_expiry_session=(dte_days == 0) if dte_days is not None else None,
                cutoff_kind=(
                    "end_of_session"
                    if cutoff.hour >= 15
                    else "intraday"
                ),
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

    # Pass 1 — immutable snapshots (ingest), with per-value provenance
    # (Phase 3 #78): the distinction is recorded ON the research dataset.
    provenance = chain_value_provenance(enrich_greeks)
    for s in extracted:
        ingest_session_snapshots(
            db,
            session_date=s.session_date,
            cutoff_timestamp=s.cutoff,
            prior_close=s.prior_close,
            underlying=s.underlying,
            chain=s.chain,
            chain_value_provenance=provenance,
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
                        "dte_days": s.dte_days,
                        "is_expiry_session": s.is_expiry_session,
                        "cutoff_kind": s.cutoff_kind,
                        "value_provenance": provenance,
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
        "phase3_classification": {
            "sessions": len(extracted),
            "expiry_sessions": sum(
                1 for s in extracted if s.is_expiry_session
            ),
            "non_expiry_sessions": sum(
                1 for s in extracted if s.is_expiry_session is False
            ),
            "dte_distribution": {
                b: sum(1 for s in extracted if dte_bucket(s.dte_days) == b)
                for b in ([label for _, label in DTE_BUCKETS] + ["DTE>7", "unknown"])
            },
            "cutoff_kinds": {
                k: sum(1 for s in extracted if s.cutoff_kind == k)
                for k in ("end_of_session", "intraday")
            },
            "value_provenance": provenance,
        },
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
