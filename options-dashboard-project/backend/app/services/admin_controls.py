"""Day 45 — admin platform-control store (Issue #90).

Versioned admin-owned platform controls: instrument, configuration,
retention, and feature-flag domains. Closed domain set; every write bumps
``version`` and appends to the row's history ledger. The store is consumed
ONLY by the admin router (ordinary users have no read or write path) and
by the historical-acquisition gate, which reads the enabled/disabled state
without granting admin authority to anyone.

Control-value boundary (PR #91 F9): authoritative control values are
VALIDATED, never transformed.  A submitted value is either stored exactly
as given or the write is rejected — audit redaction is never reused as a
persistence mechanism, so a control can never be accepted as one value and
persisted as another.  Credential-bearing values (broker credentials,
Analytics/refresh/access tokens, passwords, API keys, authorization or
session secrets) are refused outright; ordinary configuration keys such as
``session_timeout`` remain valid.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app.identity import AdminControl
from app.services.admin_audit import record_admin_action

# Closed control domains (Day 45 §3). Unknown domains are rejected (422).
CONTROL_DOMAINS = ("instrument", "configuration", "retention", "feature_flags")


class UnknownControlDomain(ValueError):
    """Raised when a control write/read targets a domain outside the closed set."""


class ControlValueRejected(ValueError):
    """Raised when a control value carries credential material (PR #91 F9).

    The write is refused — never transformed — so a control can never be
    accepted as one value and persisted as another.
    """


# Credential-SHAPED detection for authoritative control values.  This is
# guided by the same credential vocabulary the audit trail redacts, but it
# is a narrow VALIDATION gate — never a transformation: ordinary
# configuration such as ``session_timeout``, ``max_sessions`` or
# ``access_log_retention_days`` stays valid because ``session``/``access``
# alone are not credential words.  A key is credential-bearing when its
# NAME ENDS with a credential word (``analytics_token``, ``access_token``,
# ``admin_password``) or names an API key (``api_key``/``apikey``); a
# string value is credential-bearing when it is an opaque credential-sized
# blob whose own text carries credential vocabulary
# (``tok-super-secret-analytics-token``).
_CREDENTIAL_TAIL_WORDS = frozenset(
    ("token", "secret", "credential", "password", "passwd", "authorization", "apikey")
)
_API_KEY = re.compile(r"api[-_]?key", re.IGNORECASE)
_CREDENTIAL_VALUE_VOCAB = re.compile(
    r"(token|secret|credential|password|passwd|api[-_]?key|apikey|authorization|bearer)",
    re.IGNORECASE,
)
_WORD_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
)  # ``_``/``-`` are SEPARATORS, not word characters (PR #91 F12): compound
# credential keys such as ``analytics_token``/``access-token`` must split
# into words so the credential-tail check can see them.


def _key_segments(key: str) -> list[str]:
    """Split a key into word segments on every non-word separator.

    ``analytics_token`` -> ["analytics", "token"];
    ``session_timeout`` -> ["session", "timeout"].
    """
    segments: list[str] = []
    buf: list[str] = []
    for ch in str(key).lower():
        if ch in _WORD_CHARS:
            buf.append(ch)
        else:
            if buf:
                segments.append("".join(buf))
                buf = []
    if buf:
        segments.append("".join(buf))
    return segments


def _is_credential_key(key: str) -> bool:
    """True when a key NAME is credential-bearing (narrow, positional match).

    ``analytics_token``/``access_token``/``admin_password`` -> True;
    ``session_timeout``/``session_cache_limit``/``cache_key_size`` -> False.
    """
    name = str(key).lower()
    if _API_KEY.search(name):
        return True
    segments = _key_segments(key)
    return bool(segments) and segments[-1] in _CREDENTIAL_TAIL_WORDS


def _is_credential_value(value: str) -> bool:
    """True when a string VALUE looks like credential material.

    Narrow: an opaque (whitespace-free, credential-sized) blob whose own
    text carries credential vocabulary.  Ordinary prose, URLs and short
    configuration strings are unaffected.
    """
    text = str(value)
    return (
        bool(_CREDENTIAL_VALUE_VOCAB.search(text))
        and len(text) >= 24
        and not any(ch.isspace() for ch in text)
    )


def _validate_control_value(value, depth: int = 0) -> None:
    """Reject credential-bearing control values outright (PR #91 F9).

    Validation only — the value is never mutated.  Credential-shaped keys
    anywhere in the structure, and opaque credential-vocabulary strings,
    are refused; everything else passes through untouched.  The audit
    trail remains the place where residual secret-shaped material is
    redacted; this boundary simply refuses the write.
    """
    if depth > 6:  # bounded, mirroring the audit sanitizer's depth limit
        raise ControlValueRejected("Control value nesting is too deep.")
    if isinstance(value, dict):
        for k, v in value.items():
            if _is_credential_key(k):
                raise ControlValueRejected(
                    f"Control value contains credential material in key {str(k)!r}; "
                    "the write was rejected (never transformed)."
                )
            _validate_control_value(v, depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for v in value:
            _validate_control_value(v, depth + 1)
        return
    if isinstance(value, str) and _is_credential_value(value):
        raise ControlValueRejected(
            "Control value contains credential material; "
            "the write was rejected (never transformed)."
        )


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
    # F9 boundary: validate the authoritative value BEFORE persistence —
    # credential-bearing input is refused outright; everything else is
    # stored exactly as submitted (never audit-redacted into another value).
    _validate_control_value(value)
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
            value={"v": value},
            version=1,
            updated_by=updated_by,
            updated_at=now,
            created_at=now,
            history=[{"version": 1, "value": value, "by": updated_by, "at": now.isoformat()}],
        )
        db.add(row)
    else:
        row.version = (row.version or 0) + 1
        row.value = {"v": value}
        row.updated_by = updated_by
        row.updated_at = now
        history = list(row.history or [])
        history.append(
            {"version": row.version, "value": value, "by": updated_by, "at": now.isoformat()}
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
