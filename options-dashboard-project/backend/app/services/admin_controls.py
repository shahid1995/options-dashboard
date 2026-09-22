"""Day 45 — admin platform-control store (Issue #90).

Versioned admin-owned platform controls: instrument, configuration,
retention, and feature-flag domains. Closed domain set; every write bumps
``version`` and appends to the row's history ledger. The store is consumed
ONLY by the admin router (ordinary users have no read or write path) and
by the historical-acquisition gate, which reads the enabled/disabled state
without granting admin authority to anyone.

Sanitization: control values pass through the same secret-redaction rules
as the audit trail before persistence — a control value is never a place
to store a credential.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app.identity import AdminControl
from app.services.admin_audit import _sanitize, record_admin_action

# Closed control domains (Day 45 §3). Unknown domains are rejected (422).
CONTROL_DOMAINS = ("instrument", "configuration", "retention", "feature_flags")


class UnknownControlDomain(ValueError):
    """Raised when a control write/read targets a domain outside the closed set."""


def require_platform_admin(*, is_admin: bool) -> None:
    """Domain-level admin gate for platform operations.

    The orchestrator-level backstop for historical-data acquisition: even
    if HTTP routing were misconfigured, the domain call refuses non-admin
    principals. Admin authority comes ONLY from the durable users.is_admin
    flag resolved server-side.
    """
    if not is_admin:
        raise PermissionError("Admin privileges required.")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _stage_control(
    db: Session,
    *,
    domain: str,
    key: str,
    value,
    updated_by: str | None,
) -> dict:
    """Build (or version-bump) one control WITHOUT committing.

    The caller owns the transaction boundary — this exists so the control
    mutation and its audit record can be committed atomically.
    """
    if domain not in CONTROL_DOMAINS:
        raise UnknownControlDomain(f"Unknown control domain: {domain!r}")
    clean_value = _sanitize(value)
    row = (
        db.query(AdminControl)
        .filter(AdminControl.domain == domain, AdminControl.key == key)
        .one_or_none()
    )
    now = _utcnow()
    if row is None:
        row = AdminControl(
            id=str(uuid4()),
            domain=domain,
            key=key,
            value={"v": clean_value},
            version=1,
            updated_by=updated_by,
            updated_at=now,
            created_at=now,
            history=[{"version": 1, "value": clean_value, "by": updated_by, "at": now.isoformat()}],
        )
        db.add(row)
    else:
        row.version = (row.version or 0) + 1
        row.value = {"v": clean_value}
        row.updated_by = updated_by
        row.updated_at = now
        history = list(row.history or [])
        history.append(
            {"version": row.version, "value": clean_value, "by": updated_by, "at": now.isoformat()}
        )
        # Append-only ledger (PR #91 F3): recorded versions are never
        # truncated away — history grows with every update.
        row.history = history
    return _display(row)


def set_control(
    db: Session,
    *,
    domain: str,
    key: str,
    value,
    updated_by: str | None,
) -> dict:
    """Create or version-bump one control. Returns its display shape."""
    display = _stage_control(db, domain=domain, key=key, value=value, updated_by=updated_by)
    db.commit()
    return display


def set_control_and_audit(
    db: Session,
    *,
    domain: str,
    key: str,
    value,
    updated_by: str | None,
    audit_action: str = "controls.set",
    audit_result: str = "success",
    audit_detail: dict | None = None,
) -> dict:
    """Control mutation + its audit record in ONE transaction (PR #91 F2).

    A material control mutation must never become durable without its
    required audit record: both are staged, then a single commit makes
    them durable together. Any failure (including an audit-write failure)
    rolls the whole transaction back — neither record survives alone.
    """
    try:
        display = _stage_control(db, domain=domain, key=key, value=value, updated_by=updated_by)
        record_admin_action(
            db,
            actor_user_id=updated_by,
            action=audit_action,
            target={"domain": domain, "key": key},
            result=audit_result,
            detail=audit_detail if audit_detail is not None else {"version": display["version"]},
            commit=False,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return display


def list_controls(db: Session, domain: str) -> dict:
    """All controls in one domain (admin view)."""
    if domain not in CONTROL_DOMAINS:
        raise UnknownControlDomain(f"Unknown control domain: {domain!r}")
    rows = (
        db.query(AdminControl)
        .filter(AdminControl.domain == domain)
        .order_by(AdminControl.key.asc())
        .all()
    )
    return {"domain": domain, "controls": [_display(r) for r in rows]}


def get_control_value(db: Session, domain: str, key: str, default=None):
    """Read one control value (gate/feature-flag consumers)."""
    row = (
        db.query(AdminControl)
        .filter(AdminControl.domain == domain, AdminControl.key == key)
        .one_or_none()
    )
    if row is None:
        return default
    return (row.value or {}).get("v", default)


def _display(row: AdminControl) -> dict:
    return {
        "domain": row.domain,
        "key": row.key,
        "value": (row.value or {}).get("v"),
        "version": row.version,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
