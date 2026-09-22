"""Day 45 — admin control-plane API (``/api/v1/admin/*``, Issue #90).

Every route resolves through ``AdminUser()`` — the explicit, server-side
admin authorization boundary — BEFORE any admin work runs. Material admin
actions (and rejected attempts by non-admins) are audited with actor,
action, target, result and time; payloads are sanitized so no secret
material is ever persisted or returned.

Historical-data acquisition is admin-only: the run always uses the
PLATFORM's own credential bridge (``UpstoxTokenManager`` — the same source
the CLI backfill uses, ``TokenBridge``'s persistent cache), never a
customer broker connection. This preserves the architecture rule that
customer broker connections are not the platform's historical-ingestion
mechanism.

Operational views are read-only and admin-scoped:
  - ingestion health   — latest rows from the existing ``ingestion_log``
  - adapters           — the registered BrokerAdapter set (ids only; no
                         credential material exists at the registry layer)
  - feature flags      — ``feature_flags`` admin controls
  - model metadata     — registered quant/intelligence model descriptors
  - audit activity     — ``admin_audit_events`` (sanitized)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.db import get_db
from app.routers.deps import AdminUser, AuthenticatedUser
from app.services.admin_audit import list_admin_audit, record_admin_action
from app.services.admin_controls import (
    CONTROL_DOMAINS,
    UnknownControlDomain,
    list_controls,
    require_platform_admin,
    set_control_and_audit,
)

router = APIRouter()


def _audit_denied_admin_attempt(
    db: Session,
    request: Request,
    action: str,
    target: dict,
) -> None:
    """Audit a rejected admin attempt (403 at the AdminUser dependency).

    Issue #90 security requirement: sensitive-action rejections are audited
    too. The handler runs before the response is returned, so actor
    identity is re-resolved here (401 callers stay anonymous — recorded
    with actor None).
    """
    from app.routers.deps import SESSION_COOKIE_NAME, _canonical_session_id
    from app.identity import get_active_session, User

    sid = _canonical_session_id(
        request.headers.get("x-session-id"), request.cookies.get(SESSION_COOKIE_NAME)
    )
    actor = None
    if sid:
        session = get_active_session(db, sid)
        if session is not None:
            row = db.query(User).filter(User.id == session.user_id).one_or_none()
            if row is not None:
                actor = row.id
    record_admin_action(
        db,
        actor_user_id=actor,
        action=action,
        target=target,
        result="denied",
        detail={"reason": "admin_required"},
    )


def _admin_guarded(action: str):
    """Route factory for sensitive admin actions.

    Returns (router, dependency) where the dependency audits a rejection
    before AdminUser's 403 propagates. Implemented as a dependency so the
    audit shares the request's DB session and runs in the same DI chain.
    """

    def _guard(
        request: Request,
        db: Session = Depends(get_db),
    ):
        from fastapi import HTTPException

        from app.routers.deps import SESSION_COOKIE_NAME

        try:
            principal = AdminUser()(
                db=db,
                x_session_id=request.headers.get("x-session-id"),
                session_id_cookie=request.cookies.get(SESSION_COOKIE_NAME),
            )
        except HTTPException:
            _audit_denied_admin_attempt(db, request, action, {})
            raise
        return principal

    return _guard


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class ControlSetIn(BaseModel):
    domain: str
    key: str
    value: Any

    @field_validator("domain")
    @classmethod
    def _domain_known(cls, v: str) -> str:
        if v not in CONTROL_DOMAINS:
            raise ValueError(f"domain must be one of {', '.join(CONTROL_DOMAINS)}")
        return v

    @field_validator("key")
    @classmethod
    def _key_shape(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 128:
            raise ValueError("key must be 1-128 characters")
        return v


class AcquisitionRunIn(BaseModel):
    operation: str
    dry_run: bool = False
    stages: list[str] | None = None

    @field_validator("operation")
    @classmethod
    def _operation_supported(cls, v: str) -> str:
        if v not in {"dry_run", "contracts", "nifty", "options", "all"}:
            raise ValueError("operation must be one of dry_run, contracts, nifty, options, all")
        return v


def _result_shape(r) -> dict:
    """Display shape of a BackfillResult (numbers only, no credential)."""
    return {
        "operation": r.operation,
        "status": r.status,
        "rows_fetched": r.rows_fetched,
        "rows_inserted": r.rows_inserted,
        "errors": (r.errors or [])[:10],
    }


# ---------------------------------------------------------------------------
# 2. Historical-data acquisition (admin-only control surface)
# ---------------------------------------------------------------------------


def _authorize_acquisition_principal(db: Session, user_id: str) -> None:
    """Domain-level backstop for historical acquisition (PR #91 F4).

    Re-derives admin authority from the DURABLE ``users.is_admin`` flag
    for the authenticated principal's user_id and refuses non-admins at
    the domain boundary — so the production path enforces authorization
    even if HTTP routing were misconfigured or bypassed. The decision is
    never hard-coded and never inferred from tenant ownership.
    """
    from app.identity import User

    row = db.query(User).filter(User.id == user_id).one_or_none()
    require_platform_admin(is_admin=bool(row.is_admin) if row is not None else False)


@router.post("/acquisition/run")
async def run_acquisition(
    body: AcquisitionRunIn,
    request: Request,
    user: AuthenticatedUser = Depends(_admin_guarded("acquisition.run")),
    db: Session = Depends(get_db),
):
    """Admin-controlled historical-data acquisition trigger.

    Authorization is enforced twice: 403 at the ``AdminUser`` HTTP
    boundary, then again at the acquisition DOMAIN boundary via
    ``require_platform_admin`` backed by the persisted admin state.
    The orchestrator is constructed with the PLATFORM token bridge
    (``UpstoxTokenManager`` persistent cache), never the caller's broker
    connection or the caller's session token. Both dry-run forms
    (``dry_run: true`` and ``operation: "dry_run"``) require no platform
    credential; a real acquisition without one is refused with 503.
    """
    from app.services.backfill_orchestrator import BackfillOrchestrator, TokenBridge
    from app.services.upstox_client import UpstoxClient
    from app.services.upstox_token_manager import UpstoxTokenManager

    # --- Domain-level authorization backstop (before any admin work) ---
    try:
        _authorize_acquisition_principal(db, user.user_id)
    except PermissionError:
        record_admin_action(
            db,
            actor_user_id=user.user_id,
            action="acquisition.run",
            target={"operation": body.operation},
            result="denied",
            detail={"reason": "admin_required"},
        )
        raise HTTPException(status_code=403, detail="Admin privileges required.")

    # Both dry-run forms are credential-free (PR #91 F1).
    is_dry_run = bool(body.dry_run) or body.operation == "dry_run"

    # Platform-owned credential source ONLY (never a customer connection).
    platform_bridge = TokenBridge()
    if platform_bridge.get_token() is None and not is_dry_run:
        record_admin_action(
            db,
            actor_user_id=user.user_id,
            action="acquisition.run",
            target={"operation": body.operation, "dry_run": body.dry_run},
            result="failed",
            detail={"reason": "PLATFORM_INGESTION_TOKEN_UNAVAILABLE"},
        )
        raise HTTPException(
            status_code=503,
            detail="Platform ingestion credential unavailable; acquisition not started.",
        )

    client = UpstoxClient(token_provider=platform_bridge)
    orchestrator = BackfillOrchestrator(db, client, dry_run=is_dry_run)

    try:
        if is_dry_run:
            result = await orchestrator.run_dry_run()
            payload = {"operation": body.operation, "status": "DRY_RUN", "result": result}
        else:
            if body.operation == "contracts":
                r = await orchestrator.run_contracts()
            elif body.operation == "nifty":
                r = await orchestrator.run_nifty()
            elif body.operation == "options":
                r = await orchestrator.run_options()
            else:
                r = await orchestrator.run_all(stages=body.stages)
            payload = _result_shape(r)
    except Exception as exc:  # noqa: BLE001 — audited, then surfaced as 502
        record_admin_action(
            db,
            actor_user_id=user.user_id,
            action="acquisition.run",
            target={"operation": body.operation},
            result="failed",
            detail={"error_class": type(exc).__name__},
        )
        raise HTTPException(status_code=502, detail="Historical acquisition failed.") from exc

    record_admin_action(
        db,
        actor_user_id=user.user_id,
        action="acquisition.run",
        target={"operation": body.operation, "dry_run": body.dry_run},
        result="success",
        detail={"status": payload.get("status")},
    )
    return {"status": "accepted", **payload}


# ---------------------------------------------------------------------------
# 3. Instrument / configuration / retention / feature-flag controls
# ---------------------------------------------------------------------------


@router.post("/controls")
def create_or_update_control(
    body: ControlSetIn,
    request: Request,
    user: AuthenticatedUser = Depends(_admin_guarded("controls.set")),
    db: Session = Depends(get_db),
):
    """Set (or version-bump) one admin control. Audited with the actor."""
    # Control mutation + audit record commit ATOMICALLY (PR #91 F2): the
    # material mutation can never become durable without its audit entry.
    try:
        row = set_control_and_audit(
            db,
            domain=body.domain,
            key=body.key,
            value=body.value,
            updated_by=user.user_id,
            audit_action="controls.set",
        )
    except UnknownControlDomain as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "ok", "control": row}


@router.get("/controls/{domain}")
def read_controls(
    domain: str,
    user: AuthenticatedUser = Depends(AdminUser()),
    db: Session = Depends(get_db),
):
    """List controls in one domain (admin view)."""
    try:
        return list_controls(db, domain)
    except UnknownControlDomain as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 4. Operational views (read-only, admin-scoped)
# ---------------------------------------------------------------------------


@router.get("/ingestion-health")
def ingestion_health(
    user: AuthenticatedUser = Depends(AdminUser()),
    db: Session = Depends(get_db),
):
    """Latest ingestion runs from the existing ``ingestion_log`` table."""
    from app.models import IngestionLog

    rows = (
        db.query(IngestionLog)
        .order_by(desc(IngestionLog.started_at))
        .limit(50)
        .all()
    )
    runs = [
        {
            "run_id": r.run_id,
            "operation": r.operation,
            "instrument_key": r.instrument_key,
            "started_at": r.started_at,
            "completed_at": r.completed_at,
            "status": r.status,
            "rows_fetched": r.rows_fetched,
            "rows_inserted": r.rows_inserted,
            "error_category": r.error_category,
        }
        for r in rows
    ]
    failed = sum(1 for r in runs if r["status"] not in {"SUCCESS", "COMPLETED", "DRY_RUN"})
    return {"runs": runs, "total": len(runs), "failed": failed}


@router.get("/adapters")
def adapters(
    user: AuthenticatedUser = Depends(AdminUser()),
):
    """Registered broker/data adapters (ids + registration state only).

    The registry layer holds adapter classes, not credentials, so this
    view cannot expose secret material by construction.
    """
    from app.brokers.gateway import BrokerGateway

    gateway = BrokerGateway()
    entries = []
    for broker_id, factory in sorted(gateway.registry._factories.items()):
        entries.append(
            {
                "broker": broker_id,
                "adapter": getattr(factory, "__name__", str(factory)),
                "registered": True,
            }
        )
    return {"adapters": entries}


@router.get("/feature-flags")
def feature_flags(
    user: AuthenticatedUser = Depends(AdminUser()),
    db: Session = Depends(get_db),
):
    """Feature-flag controls (the ``feature_flags`` admin domain)."""
    return list_controls(db, "feature_flags")


@router.get("/model-metadata")
def model_metadata(
    user: AuthenticatedUser = Depends(AdminUser()),
):
    """Descriptors of the platform's registered model surfaces.

    Metadata only (name, module, responsibility class) — no weights, no
    credentials, no market data.
    """
    from app import intelligence, opportunity, quant, strike_ranking

    models = []
    for name, module, role in [
        ("strike_ranking", strike_ranking, "opportunity ranking"),
        ("opportunity", opportunity, "opportunity domain"),
        ("quant", quant, "quantitative core"),
        ("intelligence", intelligence, "intelligence domain"),
    ]:
        models.append(
            {
                "name": name,
                "module": module.__name__,
                "role": role,
            }
        )
    return {"models": models}


# ---------------------------------------------------------------------------
# 5. Audit activity view
# ---------------------------------------------------------------------------


@router.get("/audit")
def audit(
    user: AuthenticatedUser = Depends(AdminUser()),
    db: Session = Depends(get_db),
):
    """Sanitized admin-audit activity (newest first)."""
    return {"events": list_admin_audit(db)}
