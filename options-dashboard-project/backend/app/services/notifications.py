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
# Publish / read
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    """Create (or deduplicate) one notification event and deliver it.

    Sanitization happens BEFORE persistence and delivery: credential
    material can never reach the durable store, a channel, or a reader.
    """
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"Invalid notification severity: {severity!r}")

    clean_details = sanitize(details if details is not None else {})

    if dedup_key and user_scope:
        window_start = _now() - timedelta(seconds=DEDUP_WINDOW_SECONDS)
        existing = (
            db.query(NotificationEvent)
            .filter(
                NotificationEvent.user_scope == user_scope,
                NotificationEvent.dedup_key == dedup_key,
                NotificationEvent.occurred_at >= window_start,
            )
            .first()
        )
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
    _deliver(row)
    return display(row)


def force_expire_dedup_window(db, *, user_scope: str) -> None:
    """Age every dedup-keyed event for a scope out of the window (tests)."""
    cutoff = _now() - timedelta(seconds=DEDUP_WINDOW_SECONDS + 1)
    db.query(NotificationEvent).filter(
        NotificationEvent.user_scope == user_scope,
        NotificationEvent.dedup_key.isnot(None),
    ).update({NotificationEvent.occurred_at: cutoff}, synchronize_session=False)


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
