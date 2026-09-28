"""Historical IV persistence interfaces (Phase 4.1 data foundation).

Deliberately NOT a collector: Phase 4.1 establishes the data model and the
safe repository interface for future IV history, but nothing in the app
records observations yet. When a future phase enables collection it must:

- honour ``IV_HISTORY_ENABLED`` / ``IV_HISTORY_SAMPLE_SECONDS`` /
  ``IV_HISTORY_RETENTION_DAYS`` from ``app/config.py``,
- scope storage to the user's authorized data context,
- avoid recording the entire option chain at high frequency.

Every ``iv`` value written to the ``iv_observations`` table is a CANONICAL
DECIMAL FRACTION (0.1824 = 18.24%) — the same unit contract the frontend
calculation layer uses. Invalid rows (non-finite, <= 0, bad identity) are
skipped at the boundary and never stored.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import IVObservation
from app.utils.market_time import to_ist_naive

VALID_OPTION_TYPES = {"call", "put"}
DEFAULT_SOURCE = "upstox"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def record_iv_observations(db: Session, observations: list[dict]) -> int:
    """Insert valid IV observations; return the number actually stored.

    Each observation is ``{ timestamp, symbol, expiry, strike, optionType, iv,
    spot, source }`` with ``iv`` in canonical decimal. Rows with an invalid iv
    or an invalid identity are skipped — a missing/invalid IV is never stored
    as 0 and never silently corrected.
    """
    rows = []
    for o in observations or []:
        iv = o.get("iv")
        if iv is None or not isinstance(iv, (int, float)) or iv <= 0:
            continue
        symbol = str(o.get("symbol", "")).upper().strip()
        expiry = o.get("expiry")
        # Canonical IVObservation shape uses camelCase `optionType` (matching
        # the frontend contract); snake_case is accepted as an alias.
        option_type = str(o.get("optionType") or o.get("option_type") or "").lower()
        strike = o.get("strike")
        if not symbol or not expiry or option_type not in VALID_OPTION_TYPES:
            continue
        try:
            strike_f = float(strike)
        except (TypeError, ValueError):
            continue
        spot_f = None
        if o.get("spot") is not None:
            try:
                spot_f = float(o["spot"])
            except (TypeError, ValueError):
                spot_f = None
        observed_at = o.get("timestamp")
        if observed_at is None:
            observed_at = _utcnow()
        elif isinstance(observed_at, str):
            observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        normalized_observed_at = to_ist_naive(observed_at)
        if normalized_observed_at is None:
            continue
        rows.append(
            IVObservation(
                symbol=symbol,
                expiry=str(expiry),
                strike=strike_f,
                option_type=option_type,
                iv=float(iv),  # canonical decimal
                spot=spot_f,
                source=str(o.get("source", DEFAULT_SOURCE))[:32] or DEFAULT_SOURCE,
                observed_at=normalized_observed_at,
            )
        )
    if not rows:
        db.commit()
        return 0
    db.add_all(rows)
    db.commit()
    return len(rows)


def get_iv_observations(
    db: Session,
    symbol: str,
    expiry: str | None = None,
    option_type: str | None = None,
    limit: int = 1000,
) -> list[dict]:
    """Query stored observations, newest first, optionally filtered."""
    stmt = select(IVObservation).where(IVObservation.symbol == symbol.upper())
    if expiry:
        stmt = stmt.where(IVObservation.expiry == expiry)
    if option_type:
        stmt = stmt.where(IVObservation.option_type == option_type.lower())
    stmt = stmt.order_by(IVObservation.observed_at.desc()).limit(max(1, limit))
    return [
        {
            "id": row.id,
            "timestamp": row.observed_at.isoformat(),
            "symbol": row.symbol,
            "expiry": row.expiry,
            "strike": row.strike,
            "optionType": row.option_type,
            "iv": row.iv,
            "spot": row.spot,
            "source": row.source,
        }
        for row in db.scalars(stmt)
    ]


def prune_iv_observations(db: Session, retention_days: int = 90) -> int:
    """Delete observations older than `retention_days`; return rows deleted."""
    cutoff = _utcnow() - timedelta(days=max(1, retention_days))
    result = db.execute(delete(IVObservation).where(IVObservation.observed_at < cutoff))
    db.commit()
    return result.rowcount or 0
