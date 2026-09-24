"""Day 46 — backend-authoritative notification service (Issue #92).

One canonical notification pipeline:

    NotificationEvent  ->  policy/filter (severity + scope)  ->  channel adapters

The EVENT is the platform's notification authority: it is persisted,
tenant-scoped, sanitized (no credential material ever enters a payload),
deduplicated per (tenant scope, dedup key) inside a bounded window, and
delivered through provider-neutral channels. Notifications never carry
authorization semantics: publishing or reading one grants no role, and
delivery never bypasses tenant isolation — a user-scoped event is
readable only within its tenant scope.

Channels (provider-neutral, mirroring the ``EmailSender`` protocol
pattern already used for transactional email):

* :class:`InMemoryNotificationChannel` — deterministic capture sink
  (default; tests read captured deliveries).
* :class:`LogNotificationChannel` — structured-log delivery (always
  available, free, secret-safe via the shared sanitizer).

No paid vendor is required; adding one later means implementing the
same three-method adapter protocol.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import event

from app.identity import NotificationEvent
from app.structlog_config import sanitize

# Bounded dedup window: identical (scope, dedup_key) events inside this
# window collapse to the original event (idempotency for retries/loops).
DEDUP_WINDOW_SECONDS = 300

VALID_SEVERITIES = ("info", "warning", "error", "critical")


# ---------------------------------------------------------------------------
# Channel boundary
# ---------------------------------------------------------------------------


class NotificationChannel:
    """Provider-neutral delivery protocol (structural)."""

    name = "abstract"

    def deliver(self, event: dict) -> None:  # pragma: no cover - protocol
        raise NotImplementedError


class InMemoryNotificationChannel(NotificationChannel):
    """Deterministic capture sink (default; tests/development)."""

    name = "inmemory"

    def __init__(self) -> None:
        self.delivered: list[dict] = []

    def deliver(self, event: dict) -> None:
        self.delivered.append(event)


class LogNotificationChannel(NotificationChannel):
    """Deliver by structured log — always available, secret-safe."""

    name = "log"

    def deliver(self, event: dict) -> None:
        from app.structlog_config import emit_structured
        import logging

        emit_structured(
            logging.getLogger("strikenova.notifications"),
            logging.WARNING
            if event.get("severity") in ("warning",)
            else logging.ERROR
            if event.get("severity") in ("error", "critical")
            else logging.INFO,
            f"notification {event.get('event_type')}: {event.get('summary')}",
            notification=event,
        )


_CHANNELS: list[NotificationChannel] = [LogNotificationChannel()]
_capture_channel = InMemoryNotificationChannel()
_CHANNELS.append(_capture_channel)


def captured_deliveries() -> list[dict]:
    """Test/development view of the in-memory channel's deliveries."""
    return list(_capture_channel.delivered)


def clear_captured_deliveries() -> None:
    _capture_channel.delivered.clear()


def _deliver(event_row: NotificationEvent) -> None:
    """Fan the persisted event out through every configured channel."""
    payload = display(event_row)
    for channel in _CHANNELS:
        try:
            channel.deliver(payload)
        except Exception:  # a channel failure never breaks the publisher
            continue


# ---------------------------------------------------------------------------
# Transaction-aware delivery staging (F17)
# ---------------------------------------------------------------------------


def _stage_delivery(db, row: NotificationEvent) -> None:
    """Schedule channel delivery on the session's transaction boundary.

    Uses SQLAlchemy's session-level transaction signals (F17). Events
    are keyed to their ENABLING transaction — the innermost active one
    when ``publish()`` runs inside a savepoint, the root otherwise — so
    a savepoint's staged delivery dies with that savepoint while the
    outer transaction's staged delivery survives a savepoint rollback:

    * ``after_commit``        → deliver ALL staged rows, but ONLY when
      the ROOT transaction committed. SQLAlchemy also fires
      ``after_commit`` on savepoint RELEASE (the released savepoint is
      still the innermost active transaction at that moment), so the
      handler gates on ``session.in_nested_transaction()``: at release
      it returns True → no delivery; at a root commit it returns False
      → deliver. A released savepoint's rows stay keyed to its (now
      closed) transaction object and ship at the eventual root commit.
    * ``after_rollback``      → discard ALL staged rows when the ROOT
      transaction rolls back (skipped while a savepoint is still open —
      that cascade case is handled per-savepoint below).
    * ``after_soft_rollback`` → discard the rolled-back savepoint's rows
      PLUS those of any inner (descendant) transaction, because
      releasing an inner savepoint does not protect its rows from a
      parent savepoint rollback.

    ``publish()`` never commits on the caller's behalf, and a commit
    FAILURE (exception out of ``commit()``) never reaches after_commit,
    so the staged delivery is simply lost — no phantom notification.

    ``publish()`` never commits on the caller's behalf, and a commit
    FAILURE (exception out of ``commit()``) never reaches after_commit,
    so the staged delivery is simply lost — no phantom notification.
    """
    staged: dict = getattr(db, "_day46_staged_deliveries", None)
    if staged is None:
        staged = {}
        db._day46_staged_deliveries = staged

        def _release_all(session):
            # Deliver ONLY on a real root-transaction commit. SQLAlchemy
            # fires after_commit for SAVEPOINT RELEASE as well; at release
            # the released transaction is still the session's innermost
            # active transaction (in_nested_transaction() True), while a
            # root commit always completes with the root innermost. An
            # unconditional sweep here would phantom-deliver events whose
            # outer transaction may still roll back.
            if session.in_nested_transaction():
                return
            for pending in list(staged.values()):
                for event_row in pending:
                    _deliver(event_row)
            staged.clear()

        def _discard_on_rollback(session):
            # SQLAlchemy fires after_rollback for savepoint rollbacks too.
            # Only a REAL session/outer rollback may clear everything: when
            # a nested transaction is still active, this was a savepoint
            # rollback handled by _discard_tx below.
            if not session.in_nested_transaction():
                staged.clear()

        def _discard_tx(session, transaction):
            # A savepoint rollback discards that savepoint's staged
            # deliveries AND those of any inner (descendant) transaction:
            # releasing an inner savepoint does not save its staged rows
            # from a parent savepoint rollback.
            for key in list(staged):
                tx = key
                while tx is not None:
                    if tx is transaction:
                        staged.pop(key, None)
                        break
                    tx = tx.parent

        event.listen(db, "after_commit", _release_all)
        event.listen(db, "after_rollback", _discard_on_rollback)
        event.listen(db, "after_soft_rollback", _discard_tx)
        db._day46_unlisten = lambda: [
            event.remove(db, name, handler)
            for name, handler in (
                ("after_commit", _release_all),
                ("after_rollback", _discard_on_rollback),
                ("after_soft_rollback", _discard_tx),
            )
        ]

    # Innermost active transaction: inside a savepoint this is the nested
    # transaction (get_transaction() would return the ROOT and a savepoint
    # rollback could then never discard that savepoint's staged delivery).
    transaction = db.get_nested_transaction() or db.get_transaction()
    staged.setdefault(transaction, []).append(row)


# ---------------------------------------------------------------------------
# Publish / read
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _dedup_exists(db, *, user_scope: str | None, dedup_key: str) -> NotificationEvent | None:
    """Find the in-window duplicate for THIS scope's dedup key (F14).

    The identity is (scope, dedup_key): user-scoped events only collapse
    within the same user; platform events (user_scope None) collapse only
    with other platform events; the two can never cross-deduplicate.
    """
    window_start = _now() - timedelta(seconds=DEDUP_WINDOW_SECONDS)
    query = db.query(NotificationEvent).filter(
        NotificationEvent.dedup_key == dedup_key,
        NotificationEvent.occurred_at >= window_start,
    )
    if user_scope is None:
        return query.filter(NotificationEvent.user_scope.is_(None)).first()
    return query.filter(NotificationEvent.user_scope == user_scope).first()


def publish(
    db,
    *,
    event_type: str,
    severity: str,
    source: str,
    summary: str,
    details: dict | None = None,
    user_scope: str | None = None,
    correlation_id: str | None = None,
    dedup_key: str | None = None,
    occurred_at: datetime | None = None,
) -> dict:
    """Create (or deduplicate) one notification event.

    Sanitization happens BEFORE persistence: credential material can
    never reach the durable store, a channel, or a reader.

    Transaction-aware delivery (F17): the event row is STAGED and its
    channel delivery is scheduled on the session's transaction. Channels
    observe the event ONLY after the surrounding transaction COMMITs —
    a rollback (or a failed commit) discards the staging, so no channel
    ever receives a phantom notification. ``publish()`` never commits
    the caller's transaction, and nested savepoints are honored: a
    rolled-back savepoint discards its own deliveries while the outer
    transaction's commit releases the rest.
    """
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"Invalid notification severity: {severity!r}")

    clean_details = sanitize(details if details is not None else {})

    if dedup_key is not None:
        existing = _dedup_exists(db, user_scope=user_scope, dedup_key=dedup_key)
        if existing is not None:
            return display(existing)

    row = NotificationEvent(
        id=str(uuid4()),
        event_type=event_type,
        severity=severity,
        source=source,
        summary=summary,
        details=clean_details,
        user_scope=user_scope,
        correlation_id=correlation_id,
        dedup_key=dedup_key,
        occurred_at=occurred_at or _now(),
    )
    db.add(row)
    db.flush()
    _stage_delivery(db, row)
    return display(row)


def force_expire_dedup_window(db, *, user_scope: str | None) -> None:
    """Age every dedup-keyed event for a scope out of the window (tests).

    ``user_scope=None`` expires PLATFORM-scoped events (F14 coverage).
    """
    cutoff = _now() - timedelta(seconds=DEDUP_WINDOW_SECONDS + 1)
    query = db.query(NotificationEvent).filter(
        NotificationEvent.dedup_key.isnot(None),
    )
    if user_scope is None:
        query = query.filter(NotificationEvent.user_scope.is_(None))
    else:
        query = query.filter(NotificationEvent.user_scope == user_scope)
    query.update({NotificationEvent.occurred_at: cutoff}, synchronize_session=False)


def _require_scope(db, event_id: str, user_scope: str) -> NotificationEvent:
    row = (
        db.query(NotificationEvent)
        .filter(NotificationEvent.id == event_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Notification not found.")
    # Tenant isolation: a user-scoped event is readable ONLY within its
    # scope. Platform-scoped events (user_scope None) are admin-surface
    # material and are never exposed through the user API.
    if row.user_scope != user_scope:
        raise HTTPException(status_code=403, detail="Not your notification.")
    return row


def get_for_user(db, user_scope: str, event_id: str) -> dict:
    return display(_require_scope(db, event_id, user_scope))


def list_for_user(db, user_scope: str, *, limit: int = 100) -> list[dict]:
    rows = (
        db.query(NotificationEvent)
        .filter(NotificationEvent.user_scope == user_scope)
        .order_by(NotificationEvent.occurred_at.desc())
        .limit(limit)
        .all()
    )
    return [display(r) for r in rows]


def display(row: NotificationEvent) -> dict:
    return {
        "event_id": row.id,
        "event_type": row.event_type,
        "severity": row.severity,
        "source": row.source,
        "summary": row.summary,
        "details": row.details or {},
        "user_scope": row.user_scope,
        "correlation_id": row.correlation_id,
        "dedup_key": row.dedup_key,
        "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
    }


__all__ = [
    "DEDUP_WINDOW_SECONDS",
    "InMemoryNotificationChannel",
    "LogNotificationChannel",
    "NotificationChannel",
    "captured_deliveries",
    "clear_captured_deliveries",
    "display",
    "force_expire_dedup_window",
    "get_for_user",
    "list_for_user",
    "publish",
]
