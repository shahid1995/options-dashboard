"""Day 45 — admin audit service (Issue #90).

Durable, append-only audit trail for material admin actions. Every record
identifies actor/action/target/result/time and is sanitized so no secret
material (broker credentials, Analytics Tokens, session identifiers) is
ever persisted:

  - keys that look like secrets (token/secret/credential/password/api_key/
    authorization/cookie/session) are DROPPED from target/detail;
  - string values shaped like the platform's credential material are
    replaced with a fixed placeholder before storage.

``record_admin_action`` is the single write path; ``list_admin_audit`` is
the single admin-facing read path. Both are imported lazily by the admin
router so the dependency stays explicit.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.identity import AdminAuditEvent

SECRETISH_KEY = re.compile(
    r"(token|secret|credential|password|passwd|api_key|apikey|authorization|cookie|session)",
    re.IGNORECASE,
)

# Shapes that indicate secret material in a value (best-effort, defense in
# depth beyond key redaction): long unbroken bearer-ish strings.
SECRETISH_VALUE = re.compile(r"\b[A-Za-z0-9_\-]{28,}\b")

REDACTED = "<redacted>"


def _sanitize(value: Any, depth: int = 0) -> Any:
    """Recursively redact secret-shaped material from audit payloads."""
    if depth > 6:  # bounded
        return None
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for k, v in value.items():
            if SECRETISH_KEY.search(str(k)):
                continue  # secret-keyed material is dropped entirely
            clean[str(k)] = _sanitize(v, depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [_sanitize(v, depth + 1) for v in value[:20]]
    if isinstance(value, str):
        if SECRETISH_KEY.search(value) and len(value) >= 12:
            return REDACTED
        return SECRETISH_VALUE.sub(REDACTED, value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def record_admin_action(
    db: Session,
    *,
    actor_user_id: str | None,
    action: str,
    target: dict | None = None,
    result: str = "success",
    detail: dict | None = None,
) -> AdminAuditEvent:
    """Append one sanitized audit record for a material admin action."""
    event = AdminAuditEvent(
        id=str(uuid4()),
        actor_user_id=actor_user_id,
        action=action,
        target=_sanitize(target or {}),
        result=result if result in {"success", "denied", "failed"} else "failed",
        detail=_sanitize(detail or {}),
        occurred_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(event)
    db.commit()
    return event


def list_admin_audit(db: Session, *, limit: int = 200) -> list[dict]:
    """Newest-first audit records for the admin operational view."""
    rows = (
        db.query(AdminAuditEvent)
        .order_by(AdminAuditEvent.occurred_at.desc(), AdminAuditEvent.id.desc())
        .limit(min(max(limit, 1), 500))
        .all()
    )
    return [
        {
            "id": r.id,
            "actor_user_id": r.actor_user_id,
            "action": r.action,
            "target": r.target,
            "result": r.result,
            "detail": r.detail,
            "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
        }
        for r in rows
    ]
