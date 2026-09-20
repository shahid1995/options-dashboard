"""Issue #80 — Research Phase 4: prospective end-of-session chain capture.

Research-only module that accumulates genuine (observed) end-of-session
option-chain snapshots for the Overnight Gap Intelligence study.  It reuses
the EXISTING authorized market-data path (broker adapter via the Day-11
gateway) and the EXISTING Phase-1 research persistence
(``ingest_session_snapshots``); it creates no new market-data architecture,
no scheduler, and no signal.  There is deliberately **no UI and no
user-facing forecast** — the module exists so that future sessions
accumulate real ``observed`` data (IV, bid/ask, Greeks) that the historical
backups cannot provide (Phase 3 finding).

What "prospective capture" means here
-------------------------------------
A caller (operator-invoked CLI or a future explicitly-flagged background
loop, mirroring the GEX capture precedent) fetches the front-expiry option
chain from the customer's own authorized broker session *at or after* the
declared end-of-session research cutoff, and this module converts that
canonical observation into the Phase-1 research schema with per-value
provenance.  Every field is either:

* ``observed``       — genuinely present in the broker payload (LTP,
  volume, OI, bid/ask/quantities, and broker-reported IV/delta/gamma when
  the payload carries them);
* ``unavailable``    — the authorized source does not provide it
  (futures fields, India VIX, vega/theta at the chain contract);
* never reconstructed — capture does NOT compute anything.  Unlike the
  Phase 2/3 *historical* adapter (which reconstructs IV/Greeks via the
  canonical Black-Scholes engine for dates that no longer have a live
  market), a live capture needs no reconstruction: observed-or-missing.

Hard rules enforced here (unchanged from the research contract):

* missing stays missing — never converted to zero (a None bid is stored
  as NULL, not 0.0);
* observed is never relabelled: only broker-supplied values are marked
  ``observed``; no Black-Scholes value is ever written by this module;
* OI change is ``derived`` (difference of two stored OI observations) and
  only when the prior session's snapshot for the same expiry/strike/type
  already exists in the research DB — never carried from anywhere else;
* cutoff integrity: the caller supplies the cutoff; ``capture_session``
  rejects a chain observation whose market/event timestamp is after it.
  A chain observation with NO trustworthy event timestamp is ALSO refused:
  receive time proves only when the application fetched — never that the
  observed quote/book/Greeks state existed at or before the declared
  cutoff.  Persisted ``source_timestamp`` values are the payload's actual
  observation times — never receive time, never back-dated to the cutoff.

Session classification / expiry selection / DTE
-----------------------------------------------
* front expiry is selected as the minimum expiry strictly >= session_date
  from the chain observation itself (never a future-expiry fallback);
* DTE = (expiry - session_date).days (calendar-day convention identical
  to the Phase 3 loader);
* the session is classified ``expiry``/``non-expiry`` from that expiry
  metadata, never from data absence.

Idempotency / duplicate handling
--------------------------------
``capture_session`` writes through ``ingest_session_snapshots``, whose
immutability rule applies unchanged: re-capturing the same session_date
raises ``SessionExistsError`` unless ``replace=True`` is passed
explicitly.  Replay of the same broker payload is therefore a no-op by
default and deterministic under ``replace=True``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.market_data.contracts import OptionChainObservation
from app.research.gap_pipeline import ingest_session_snapshots

logger = logging.getLogger(__name__)

# Provenance labels for the research dataset (consumed by Phase 2/3
# reporting conventions).  Capture NEVER writes "reconstructed".
PROVENANCE_CAPTURE_LABEL = "PROSPECTIVE_CAPTURE"

# IST is imported from the single canonical definition
# (``app.utils.market_time``) — never redefined locally (project invariant).
# Used only to pin a naive operator-supplied cutoff to the documented
# research convention (15:30 IST); broker event timestamps are never pinned
# (naive broker times are refused).
from app.utils.market_time import IST as _IST

_CHAIN_VALUE_PROVENANCE = {
    "ltp": "observed",
    "volume": "observed",
    "open_interest": "observed",
    "change_in_oi": "derived",
    "bid": "observed",
    "ask": "observed",
    "bid_qty": "observed",
    "ask_qty": "observed",
    "iv": "observed",
    "delta": "observed",
    "gamma": "observed",
    "vega": "unavailable",
    "theta": "unavailable",
}

_UNDERLYING_VALUE_PROVENANCE = {
    "spot_ltp": "observed",
    "spot_ohlc": "unavailable",
    "futures_ltp": "unavailable",
    "futures_oi": "unavailable",
    "futures_volume": "unavailable",
    "india_vix": "unavailable",
}


def resolve_capture_token(
    user_id: str | None,
    connection_id: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve an authorized UPSTOX read token for prospective capture.

    Applies EXACTLY the same ownership rules as the production GEX capture
    loop (app/main.py): the user's explicitly-default connected UPSTOX
    connection with active data, preferring its 1-year read-only Analytics
    Token, falling back to the default BrokerAuthorization's OAuth session
    token.  No arbitrary connection selection, no platform-wide tokens.

    Returns ``(token, connection_id, source)`` where source is
    ``analytics_token`` / ``broker_oauth``, or ``(None, None, None)`` when
    no authorized session is available.  Read-only: never refreshes, never
    writes credentials.
    """
    if not user_id:
        return (None, None, None)
    from app.db import SessionLocal
    from app.identity import BrokerConnection, get_analytics_token
    from app.services.broker_authorization import (
        resolve_default_broker_authorization,
    )

    conn_id = connection_id
    if conn_id is None:
        db = SessionLocal()
        try:
            conn = (
                db.query(BrokerConnection)
                .filter(
                    BrokerConnection.user_id == user_id,
                    BrokerConnection.status == "connected",
                    BrokerConnection.broker == "UPSTOX",
                    BrokerConnection.data_status == "active",
                    BrokerConnection.broker_analytics_token_encrypted.isnot(None),
                    BrokerConnection.is_default == True,  # noqa: E712
                )
                .first()
            )
            conn_id = conn.id if conn else None
        finally:
            db.close()
    if conn_id:
        db = SessionLocal()
        try:
            token = get_analytics_token(db, user_id, "UPSTOX", connection_id=conn_id)
        finally:
            db.close()
        if token:
            return (token, conn_id, "analytics_token")
    db = SessionLocal()
    try:
        conn, authz = resolve_default_broker_authorization(db, user_id)
        if conn is not None and authz is not None:
            token = getattr(authz, "access_token", None)
            if token:
                return (token, conn.id, "broker_oauth")
    finally:
        db.close()
    return (None, None, None)


def classify_session(session_date: str, expiry: str) -> dict[str, Any]:
    """Classify a session from contract metadata (never data absence).

    Returns the front-expiry, DTE (calendar days), and expiry/non-expiry
    classification used by the Phase 3 reporting conventions.
    """
    exp = date.fromisoformat(expiry)
    ses = date.fromisoformat(session_date)
    dte = (exp - ses).days
    return {
        "expiry": expiry,
        "dte": dte,
        "kind": "expiry" if dte == 0 else "non_expiry",
    }


def _finite(value: Any) -> float | None:
    """Pass-through finite float; missing/NaN/inf stay missing (None)."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def _observation_event_times(observation: OptionChainObservation) -> list[datetime]:
    """Trustworthy observation/event timestamps carried by the observation.

    Only broker-sourced times count: the observation-level market timestamp
    and per-leg event timestamps embedded in the canonical rows (Issue #80
    cutoff integrity).  Receive time is deliberately NOT a member — it
    proves when the application fetched, never when the market state
    existed.
    """
    times: list[datetime] = []
    if observation.market_timestamp is not None:
        times.append(observation.market_timestamp)
    for row in observation.chain:
        for quote in (row.call, row.put):
            if quote is None:
                continue
            leg_time = getattr(quote, "event_timestamp", None)
            if leg_time is not None:
                times.append(leg_time)
    return times


def observation_to_research_rows(
    observation: OptionChainObservation,
    *,
    session_date: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Convert a canonical OptionChainObservation into the (underlying,
    chain-row) dicts accepted by ``ingest_session_snapshots``.

    Pure and deterministic: identical observations produce identical rows.
    Only observed values are copied; absent broker fields remain absent.
    """
    underlying: dict[str, Any] = {
        "spot_ltp": _finite(observation.underlying_spot_price),
        # futures / VIX: the authorized chain path does not provide them
        # (Phase 3 audit); they stay absent, never zero.
        "futures_ltp": None,
        "futures_oi": None,
        "futures_volume": None,
        "india_vix": None,
    }

    chain_rows: list[dict[str, Any]] = []
    for row in observation.chain:  # already strike-sorted by the contract
        for otype, quote in (("CALL", row.call), ("PUT", row.put)):
            if quote is None:
                continue
            chain_rows.append(
                {
                    "expiry": observation.expiry_date,
                    "strike": row.strike,
                    "option_type": otype,
                    "ltp": _finite(quote.ltp),
                    "bid": _finite(quote.bid),
                    "ask": _finite(quote.ask),
                    "bid_qty": _finite(quote.bid_quantity),
                    "ask_qty": _finite(quote.ask_quantity),
                    "volume": _finite(quote.volume),
                    "open_interest": _finite(quote.oi),
                    # observed broker analytics when the payload carried them
                    "iv": _finite(quote.iv),
                    "delta": _finite(quote.delta),
                    "gamma": _finite(quote.gamma),
                    # Issue #80 cutoff integrity: the row's source timestamp
                    # is the payload's own observation/event time — the
                    # capture receive time is NOT acceptable evidence and is
                    # never back-dated to the cutoff.  capture_session has
                    # already proven an event-time basis exists.
                    "source_timestamp": quote.event_timestamp
                    or observation.market_timestamp,
                }
            )
    return underlying, chain_rows


def capture_session(
    db: Session,
    observation: OptionChainObservation,
    *,
    session_date: str,
    cutoff_timestamp: datetime,
    prior_close: float | None,
    prior_oi: Mapping[tuple[str, float, str], float] | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Persist one prospective end-of-session capture into the research DB.

    ``observation``  — canonical chain observation fetched by the caller
                       through the existing authorized broker path.
    ``session_date`` — trading date this snapshot belongs to.
    ``cutoff_timestamp`` — the declared end-of-session research cutoff;
                       observations without a trustworthy event timestamp,
                       or whose latest event timestamp is strictly after
                       it, are rejected (cutoff integrity).
    ``prior_close``  — session T's own close (the reference for the T+1
                       opening gap); when unknown, pass None and the
                       pipeline keeps prior_close at 0.0 (target attachment
                       remains a separate later step).
    ``prior_oi``     — optional mapping (expiry, strike, type) -> OI from
                       the previously captured session, enabling the only
                       derived field (change_in_oi).  Absent keys leave
                       change_in_oi missing — never zero.

    Returns a capture summary (classification, counts, provenance).
    Raises ``SessionExistsError`` for an existing immutable session unless
    ``replace=True``.
    """
    event_times = _observation_event_times(observation)
    if not event_times:
        # Receive time is not proof the observed state existed at/before the
        # cutoff — refuse rather than store an unprovable snapshot.
        raise ValueError(
            "capture refused: the chain observation carries no trustworthy "
            "market/event timestamp; receive time cannot prove the snapshot "
            "existed at or before the research cutoff"
        )
    cutoff_aware = cutoff_timestamp.tzinfo is not None
    events_aware = all(t.tzinfo is not None for t in event_times)
    if cutoff_aware and not events_aware:
        # A naive/aware mix would silently mis-compare instants; refuse
        # loudly instead of guessing a timezone for the broker event times.
        raise ValueError(
            "capture refused: observation event timestamps are timezone-naive "
            "while the cutoff is timezone-aware — refusing rather than guessing"
        )
    if not cutoff_aware and events_aware:
        # Documented research convention (Phases 1–3): a naive cutoff is the
        # IST exchange wall clock (15:30 IST == 10:00 UTC). Pin it explicitly
        # so aware broker event timestamps compare correctly — this applies
        # the documented convention, it does not redefine the cutoff.
        cutoff_timestamp = cutoff_timestamp.replace(tzinfo=_IST)
        logger.info(
            "capture: naive cutoff pinned to documented IST convention: %s",
            cutoff_timestamp.isoformat(),
        )
    latest_event = max(event_times)
    if latest_event > cutoff_timestamp:
        raise ValueError(
            "chain observation event timestamp is after the research cutoff; "
            "post-cutoff observations may never enter the snapshot"
        )

    underlying, chain_rows = observation_to_research_rows(
        observation, session_date=session_date
    )

    # Cutoff integrity: enforced above against the broker-sourced event
    # timestamps (observation level + per-leg).  Rows carry those payload
    # event times as their ``source_timestamp`` — never receive time, never
    # back-dated to the cutoff.
    kept_rows = chain_rows
    dropped = 0

    prior = dict(prior_oi or {})
    for r in kept_rows:
        key = (r["expiry"], float(r["strike"]), r["option_type"])
        if key in prior and r["open_interest"] is not None:
            r["change_in_oi"] = r["open_interest"] - prior[key]
        else:
            r["change_in_oi"] = None  # missing stays missing

    session = ingest_session_snapshots(
        db,
        session_date,
        cutoff_timestamp,
        prior_close if prior_close is not None else 0.0,
        underlying,
        kept_rows,
        replace=replace,
        chain_value_provenance=_CHAIN_VALUE_PROVENANCE,
    )

    classification = classify_session(session_date, observation.expiry_date)
    summary = {
        "capture": PROVENANCE_CAPTURE_LABEL,
        "session_date": session_date,
        "cutoff_timestamp": cutoff_timestamp,
        "source_timestamp": observation.market_timestamp,
        "rows_captured": len(kept_rows),
        "rows_dropped_post_cutoff": dropped,
        "classification": classification,
        "chain_value_provenance": dict(_CHAIN_VALUE_PROVENANCE),
        "underlying_value_provenance": dict(_UNDERLYING_VALUE_PROVENANCE),
    }
    logger.info(
        "prospective capture stored: session=%s rows=%d dropped=%d dte=%d",
        session_date,
        len(kept_rows),
        dropped,
        classification["dte"],
    )
    return summary


def observed_iv_coverage(db: Session, session_date: str) -> float | None:
    """Fraction of captured chain rows for a session whose IV was observed.

    Returns None when the session has no rows (never a fabricated 0 or 1).
    """
    from app.models import OptionChainSnapshot

    total = (
        db.query(OptionChainSnapshot)
        .filter(
            OptionChainSnapshot.symbol == "NIFTY",
            OptionChainSnapshot.session_date == session_date,
        )
        .count()
    )
    if total == 0:
        return None
    with_iv = (
        db.query(OptionChainSnapshot)
        .filter(
            OptionChainSnapshot.symbol == "NIFTY",
            OptionChainSnapshot.session_date == session_date,
            OptionChainSnapshot.iv.isnot(None),
        )
        .count()
    )
    return with_iv / total
