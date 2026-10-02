"""Phase 6.7: Strategy Template CRUD endpoints.

Persistent user-created strategy templates with strict user ownership.
Editing, renaming, duplicating or deleting a template NEVER affects
historical executions, positions, exposures, orders, journal or P&L.
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.models import StrategyTemplate, StrategyTemplateLeg
from app.routers.deps import AuthenticatedUser, CurrentUser
from app.routers.paper import require_session
from app.schemas import (
    ExecutionOut,
    ResolutionLegOut,
    ResolutionOut,
    ResolutionChangeOut,
    StrategyTemplateCreateIn,
    StrategyTemplateLegOut,
    StrategyTemplateOut,
    StrategyTemplateUpdateIn,
    TemplateExecutePreviewOut,
    TemplateExecuteRequestIn,
)

router = APIRouter()


def _get_user_template(
    db: Session, user_id: str, template_id: int
) -> StrategyTemplate:
    """Fetch a template owned by the user, or raise 404."""
    template = db.scalar(
        select(StrategyTemplate)
        .options(joinedload(StrategyTemplate.legs))
        .where(
            StrategyTemplate.id == template_id,
            StrategyTemplate.user_id == user_id,
        )
    )
    if template is not None:
        # Force eager-load evaluation
        _ = template.legs
    if template is None:
        raise HTTPException(status_code=404, detail="Strategy template not found.")
    return template


def _template_out(template: StrategyTemplate) -> dict:
    """Serialize a StrategyTemplate to the response dict."""
    return {
        "id": template.id,
        "name": template.name,
        "symbol": template.symbol,
        "legs": [
            {
                "id": leg.id,
                "position": leg.position,
                "action": leg.action,
                "option_type": leg.option_type,
                "strike": leg.strike,
                "expiry": leg.expiry,
                "quantity": leg.quantity,
                "lot_size": leg.lot_size,
                "price": leg.price,
                # Phase 6.8B: dynamic formula fields
                "strike_mode": getattr(leg, "strike_mode", "fixed"),
                "strike_offset": getattr(leg, "strike_offset", None),
                "strike_offset_pct": getattr(leg, "strike_offset_pct", None),
                "target_delta": getattr(leg, "target_delta", None),
                "expiry_mode": getattr(leg, "expiry_mode", "fixed"),
                "expiry_dte_min": getattr(leg, "expiry_dte_min", None),
                "expiry_dte_max": getattr(leg, "expiry_dte_max", None),
                "formula_version": getattr(leg, "formula_version", 1),
            }
            for leg in sorted(template.legs, key=lambda l: l.position)
        ],
        "created_at": template.created_at,
        "updated_at": template.updated_at,
    }


@router.get("/templates", response_model=list[StrategyTemplateOut])
def list_templates(
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """GET /paper/templates — List the authenticated user's strategy templates."""
    user_id, _access_token = require_session(user)
    templates = db.scalars(
        select(StrategyTemplate)
        .options(joinedload(StrategyTemplate.legs))
        .where(StrategyTemplate.user_id == user_id)
        .order_by(StrategyTemplate.updated_at.desc())
    ).unique().all()
    return [_template_out(t) for t in templates]


@router.post("/templates", status_code=201, response_model=StrategyTemplateOut)
def create_template(
    body: StrategyTemplateCreateIn,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """POST /paper/templates — Create a new strategy template."""
    user_id, _access_token = require_session(user)

    # Check for duplicate name within user
    existing = db.scalar(
        select(StrategyTemplate).where(
            StrategyTemplate.user_id == user_id,
            StrategyTemplate.name == body.name,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A strategy template named '{body.name}' already exists.",
        )

    template = StrategyTemplate(
        user_id=user_id,
        name=body.name,
        symbol=body.symbol,
    )
    db.add(template)
    db.flush()  # get template.id

    for i, leg_in in enumerate(body.legs):
        leg = StrategyTemplateLeg(
            template_id=template.id,
            position=leg_in.position if leg_in.position else i,
            action=leg_in.action,
            option_type=leg_in.option_type,
            strike=leg_in.strike,
            expiry=leg_in.expiry,
            quantity=leg_in.quantity,
            lot_size=leg_in.lot_size,
            price=leg_in.price,
            # Phase 6.8B: dynamic formula fields
            strike_mode=leg_in.strike_mode,
            strike_offset=leg_in.strike_offset,
            strike_offset_pct=leg_in.strike_offset_pct,
            target_delta=leg_in.target_delta,
            expiry_mode=leg_in.expiry_mode,
            expiry_dte_min=leg_in.expiry_dte_min,
            expiry_dte_max=leg_in.expiry_dte_max,
            formula_version=leg_in.formula_version,
        )
        db.add(leg)

    db.commit()
    db.refresh(template)
    # Re-fetch with legs eagerly loaded
    db.refresh(template, ["legs"])
    return _template_out(template)


@router.get(
    "/templates/{template_id}", response_model=StrategyTemplateOut
)
def get_template(
    template_id: int,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """GET /paper/templates/:id — Retrieve one strategy template."""
    user_id, _access_token = require_session(user)
    template = _get_user_template(db, user_id, template_id)
    return _template_out(template)


@router.put(
    "/templates/{template_id}", response_model=StrategyTemplateOut
)
def update_template(
    template_id: int,
    body: StrategyTemplateUpdateIn,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """PUT /paper/templates/:id — Update a strategy template.

    Partial update: only provided fields are changed.
    When ``legs`` is provided, the entire leg set is replaced (idempotent
    full replacement, not merge).
    """
    user_id, _access_token = require_session(user)
    template = _get_user_template(db, user_id, template_id)

    if body.name is not None:
        # Check for duplicate name (excluding this template)
        dup = db.scalar(
            select(StrategyTemplate).where(
                StrategyTemplate.user_id == user_id,
                StrategyTemplate.name == body.name,
                StrategyTemplate.id != template_id,
            )
        )
        if dup is not None:
            raise HTTPException(
                status_code=409,
                detail=f"A strategy template named '{body.name}' already exists.",
            )
        template.name = body.name

    if body.symbol is not None:
        template.symbol = body.symbol

    if body.legs is not None:
        # Full replacement of legs
        for leg in template.legs:
            db.delete(leg)
        db.flush()
        for i, leg_in in enumerate(body.legs):
            leg = StrategyTemplateLeg(
                template_id=template.id,
                position=leg_in.position if leg_in.position else i,
                action=leg_in.action,
                option_type=leg_in.option_type,
                strike=leg_in.strike,
                expiry=leg_in.expiry,
                quantity=leg_in.quantity,
                lot_size=leg_in.lot_size,
                price=leg_in.price,
                # Phase 6.8B: dynamic formula fields
                strike_mode=leg_in.strike_mode,
                strike_offset=leg_in.strike_offset,
                strike_offset_pct=leg_in.strike_offset_pct,
                target_delta=leg_in.target_delta,
                expiry_mode=leg_in.expiry_mode,
                expiry_dte_min=leg_in.expiry_dte_min,
                expiry_dte_max=leg_in.expiry_dte_max,
                formula_version=leg_in.formula_version,
            )
            db.add(leg)

    db.commit()
    db.refresh(template, ["legs"])
    return _template_out(template)


@router.post(
    "/templates/{template_id}/duplicate",
    status_code=201,
    response_model=StrategyTemplateOut,
)
def duplicate_template(
    template_id: int,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
    new_name: str | None = Query(default=None, description="Optional new name for the duplicate"),
):
    """POST /paper/templates/:id/duplicate — Duplicate a strategy template.

    Creates a deep copy with a new name (defaults to ``<original> (Copy)``).
    """
    user_id, _access_token = require_session(user)
    source = _get_user_template(db, user_id, template_id)

    dup_name = new_name or f"{source.name} (Copy)"
    # Check for duplicate name
    existing = db.scalar(
        select(StrategyTemplate).where(
            StrategyTemplate.user_id == user_id,
            StrategyTemplate.name == dup_name,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A strategy template named '{dup_name}' already exists.",
        )

    template = StrategyTemplate(
        user_id=user_id,
        name=dup_name,
        symbol=source.symbol,
    )
    db.add(template)
    db.flush()

    for leg in sorted(source.legs, key=lambda l: l.position):
        new_leg = StrategyTemplateLeg(
            template_id=template.id,
            position=leg.position,
            action=leg.action,
            option_type=leg.option_type,
            strike=leg.strike,
            expiry=leg.expiry,
            quantity=leg.quantity,
            lot_size=leg.lot_size,
            price=leg.price,
            # Phase 6.8B: copy all formula fields
            strike_mode=getattr(leg, "strike_mode", "fixed"),
            strike_offset=getattr(leg, "strike_offset", None),
            strike_offset_pct=getattr(leg, "strike_offset_pct", None),
            target_delta=getattr(leg, "target_delta", None),
            expiry_mode=getattr(leg, "expiry_mode", "fixed"),
            expiry_dte_min=getattr(leg, "expiry_dte_min", None),
            expiry_dte_max=getattr(leg, "expiry_dte_max", None),
            formula_version=getattr(leg, "formula_version", 1),
        )
        db.add(new_leg)

    db.commit()
    db.refresh(template, ["legs"])
    return _template_out(template)


@router.delete("/templates/{template_id}", status_code=204)
def delete_template(
    template_id: int,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """DELETE /paper/templates/:id — Delete a strategy template.

    Only deletes the template and its legs. NEVER affects historical
    executions, positions, exposures, orders, journal or P&L.
    """
    user_id, _access_token = require_session(user)
    template = _get_user_template(db, user_id, template_id)
    db.delete(template)
    db.commit()
    return None


@router.post(
    "/templates/{template_id}/resolve",
    response_model=ResolutionOut,
)
async def resolve_template(
    template_id: int,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """POST /paper/templates/:id/resolve — Resolve a saved template against live chain.

    Resolves every leg's formula (strike_mode, expiry_mode, etc.) against the
    live broker option chain and returns fully resolved legs with current
    market prices. Template metadata is preserved in the response.

    CRITICAL: This NEVER creates execution records. It is read-only.
    """
    from app.services.template_resolution import resolve_legs

    user_id, access_token = require_session(user)
    template = _get_user_template(db, user_id, template_id)

    # Convert template legs to dicts
    legs = []
    for i, leg in enumerate(template.legs):
        legs.append({
            "position": leg.position,
            "action": leg.action,
            "option_type": leg.option_type,
            "strike": leg.strike,
            "expiry": leg.expiry,
            "quantity": leg.quantity,
            "lot_size": leg.lot_size,
            "strike_mode": getattr(leg, "strike_mode", "fixed"),
            "strike_offset": getattr(leg, "strike_offset", None),
            "target_delta": getattr(leg, "target_delta", None),
            "expiry_mode": getattr(leg, "expiry_mode", "fixed"),
            "expiry_dte_min": getattr(leg, "expiry_dte_min", None),
            "expiry_dte_max": getattr(leg, "expiry_dte_max", None),
        })

    result = await resolve_legs(
        access_token=access_token,
        symbol=template.symbol,
        legs=legs,
        template_id=template.id,
        template_name=template.name,
    )

    # Convert to response schema
    leg_outs = []
    for leg in result.legs:
        leg_outs.append(ResolutionLegOut(
            position=leg.position,
            action=leg.action,
            option_type=leg.option_type,
            quantity=leg.quantity,
            lot_size=leg.lot_size,
            resolved_strike=leg.resolved_strike,
            resolved_expiry=leg.resolved_expiry,
            strike_mode_used=leg.strike_mode_used,
            expiry_mode_used=leg.expiry_mode_used,
            current_price=leg.current_price,
            price_status=leg.price_status,
            quote_timestamp=leg.quote_timestamp,
            ltp=leg.ltp,
            warnings=leg.warnings,
            symbol=leg.symbol,
            expiration_date=leg.expiration_date,
            strike_price=leg.strike_price,
        ))

    return ResolutionOut(
        status=result.status,
        symbol=result.symbol,
        legs=leg_outs,
        errors=result.errors,
        warnings=result.warnings,
        template_id=result.template_id,
        template_name=result.template_name,
    )


# ---- Phase 6.9: Dynamic template execution bridge ----------------------------

logger = logging.getLogger(__name__)


def _template_to_legs(template: StrategyTemplate) -> list[dict]:
    """Convert a template's legs to the dict format expected by resolve_legs()."""
    return [
        {
            "position": leg.position,
            "action": leg.action,
            "option_type": leg.option_type,
            "strike": leg.strike,
            "expiry": leg.expiry,
            "quantity": leg.quantity,
            "lot_size": leg.lot_size,
            "strike_mode": getattr(leg, "strike_mode", "fixed"),
            "strike_offset": getattr(leg, "strike_offset", None),
            "target_delta": getattr(leg, "target_delta", None),
            "expiry_mode": getattr(leg, "expiry_mode", "fixed"),
            "expiry_dte_min": getattr(leg, "expiry_dte_min", None),
            "expiry_dte_max": getattr(leg, "expiry_dte_max", None),
        }
        for leg in template.legs
    ]


def _resolution_to_leg_outs(result) -> list[ResolutionLegOut]:
    """Convert ResolutionResult legs to ResolutionLegOut schema objects."""
    return [
        ResolutionLegOut(
            position=leg.position,
            action=leg.action,
            option_type=leg.option_type,
            quantity=leg.quantity,
            lot_size=leg.lot_size,
            resolved_strike=leg.resolved_strike,
            resolved_expiry=leg.resolved_expiry,
            strike_mode_used=leg.strike_mode_used,
            expiry_mode_used=leg.expiry_mode_used,
            current_price=leg.current_price,
            price_status=leg.price_status,
            quote_timestamp=leg.quote_timestamp,
            ltp=leg.ltp,
            warnings=leg.warnings,
            symbol=leg.symbol,
            expiration_date=leg.expiration_date,
            strike_price=leg.strike_price,
        )
        for leg in result.legs
    ]


# ---- Phase 6.10: Execution audit trail -----------------------------------------


def _build_execution_metadata(
    template,
    preview_result,
    comparison_changes,
    confirmed_strikes,
    confirmed_expiries,
    exec_result,
    exec_legs,
    prices,
) -> dict:
    """Build the V2 execution audit trail metadata.

    Captures five layers of information for post-hoc analysis:
      A. Formula definition (what the template says)
      B. Preview resolution (what the user saw)
      C. Confirmed values (what the user approved)
      D. Execution resolution (what the server actually used)
      E. Actual fill prices (what was recorded)
    """
    # A. Formula definition from the first template leg
    formula = {}
    formula_version = 1
    if template.legs:
        first_leg = template.legs[0]
        formula_version = getattr(first_leg, "formula_version", 1)
        formula = {
            "strike_mode": getattr(first_leg, "strike_mode", "fixed"),
            "expiry_mode": getattr(first_leg, "expiry_mode", "fixed"),
            "strike_offset": getattr(first_leg, "strike_offset", None),
            "target_delta": getattr(first_leg, "target_delta", None),
            "expiry_dte_min": getattr(first_leg, "expiry_dte_min", None),
            "expiry_dte_max": getattr(first_leg, "expiry_dte_max", None),
        }

    # B. Preview resolution from the fresh resolution result
    preview_legs = []
    for leg in preview_result.legs:
        preview_legs.append({
            "position": leg.position,
            "resolved_strike": leg.resolved_strike,
            "resolved_expiry": leg.resolved_expiry,
            "strike_mode_used": leg.strike_mode_used,
            "expiry_mode_used": leg.expiry_mode_used,
            "current_price": leg.current_price,
            "price_status": leg.price_status,
            "quote_timestamp": leg.quote_timestamp,
        })

    # D+E. Execution resolution from the actual exec_legs + prices
    exec_legs_meta = []
    for leg in exec_legs:
        key = (leg["expiration_date"], leg["strike_price"], leg["option_type"])
        exec_legs_meta.append({
            "resolved_strike": leg["strike_price"],
            "resolved_expiry": leg["expiration_date"],
            "fill_price": prices.get(key),
            "price_source": "market",
        })

    now_iso = datetime.now(timezone.utc).isoformat()

    return {
        "formula_version": formula_version,
        "formula": formula,
        "preview_resolution": {
            "status": preview_result.status,
            "legs": preview_legs,
            "chain_strike_step": preview_result.chain_strike_step,
            "computed_at": now_iso,
        },
        "confirmed_values": {
            "confirmed_strikes": {str(k): v for k, v in (confirmed_strikes or {}).items()},
            "confirmed_expiries": {str(k): v for k, v in (confirmed_expiries or {}).items()},
        },
        "execution_resolution": {
            "legs": exec_legs_meta,
            "changes_from_preview": comparison_changes or [],
            "computed_at": now_iso,
        },
        "broker_data": {
            "spot_price": None,  # not available at router level
            "chain_fetched_at": now_iso,
        },
    }


def _persist_execution_metadata(db: Session, execution_id: str, metadata: dict) -> None:
    """Write execution metadata in a separate transaction (post-commit).

    This runs AFTER execute_strategy() has committed the execution.
    Raises on failure so the caller can decide whether to include
    metadata in the response.
    """
    from app.models import StrategyExecution

    execution = db.scalar(
        select(StrategyExecution).where(StrategyExecution.execution_id == execution_id)
    )
    if execution is not None:
        execution.execution_metadata = json.dumps(metadata)
        db.commit()


@router.post(
    "/templates/{template_id}/execute/preview",
    response_model=TemplateExecutePreviewOut,
)
async def execute_template_preview(
    template_id: int,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """POST /paper/templates/:id/execute/preview — Pre-execution resolution.

    Read-only: resolves all legs against the live chain and returns the
    fresh resolution. The frontend compares this against the displayed
    preview to detect changes before the user confirms execution.
    """
    from app.services.template_resolution import (
        compare_resolutions,
        resolution_changes_status,
        resolve_legs,
    )

    user_id, access_token = require_session(user)
    template = _get_user_template(db, user_id, template_id)
    legs = _template_to_legs(template)

    fresh_result = await resolve_legs(
        access_token=access_token,
        symbol=template.symbol,
        legs=legs,
        template_id=template.id,
        template_name=template.name,
    )

    leg_outs = _resolution_to_leg_outs(fresh_result)

    # For change detection, we compare against the template's stored
    # strike/expiry values (which represent the last preview).
    preview_legs = [
        {"position": leg.position, "resolved_strike": leg.strike, "resolved_expiry": leg.expiry}
        for leg in template.legs
    ]
    changes = compare_resolutions(preview_legs, fresh_result)
    status = resolution_changes_status(changes)
    if fresh_result.status == "FAILED":
        status = "FAILED"

    return TemplateExecutePreviewOut(
        status=status,
        symbol=template.symbol,
        template_id=template.id,
        legs=leg_outs,
        changes=changes,
        errors=fresh_result.errors,
        warnings=fresh_result.warnings,
    )


@router.post(
    "/templates/{template_id}/execute",
    response_model=ExecutionOut,
)
async def execute_template(
    template_id: int,
    request: TemplateExecuteRequestIn,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """POST /paper/templates/:id/execute — Execute a V2 dynamic template.

    Server re-resolves all legs against live broker data, validates the
    resolution (prices available, not stale), and executes atomically via
    the existing paper execution engine.

    CRITICAL: This NEVER resolves from client-supplied strike/expiry.
    Resolution is always fresh from the broker chain.
    """
    from app.routers.paper import require_market_open
    from app.services.template_resolution import (
        build_execution_legs_from_resolution,
        compare_resolutions,
        resolution_changes_status,
        resolve_legs,
        validate_execution_resolution,
    )
    from app.services.paper_execution import execute_strategy

    user_id, access_token = require_session(user)
    await require_market_open(access_token)

    template = _get_user_template(db, user_id, template_id)
    legs = _template_to_legs(template)

    # Fresh resolution against live chain
    fresh_result = await resolve_legs(
        access_token=access_token,
        symbol=template.symbol,
        legs=legs,
        template_id=template.id,
        template_name=template.name,
    )

    # Detect changes from preview
    preview_legs = [
        {"position": leg.position, "resolved_strike": leg.strike, "resolved_expiry": leg.expiry}
        for leg in template.legs
    ]
    changes = compare_resolutions(preview_legs, fresh_result)

    # Validate resolution is safe to execute
    ok, validation_errors = validate_execution_resolution(
        fresh_result,
        confirmed_strikes=request.confirmed_strikes,
        confirmed_expiries=request.confirmed_expiries,
        changes=changes,
    )
    if not ok:
        raise HTTPException(status_code=409, detail=";".join(validation_errors))

    # Build ExecutionLegIn-compatible legs from resolution
    exec_legs = build_execution_legs_from_resolution(fresh_result.legs, template.symbol)

    # Build leg-like objects for resolve_market_prices (needs attribute access)
    class _Leg:
        def __init__(self, d):
            self.expiration_date = d["expiration_date"]
            self.strike_price = d["strike_price"]
            self.option_type = d["option_type"]

    price_legs = [_Leg(leg) for leg in exec_legs]

    # Resolve authoritative fill prices from the resolved expiry chains.
    # Wrapped in PaperExecutionError handling matching the V1 /paper/executions
    # pattern — produces structured 409/502 errors instead of raw 500.
    from app.routers.paper import _paper_error, resolve_market_prices
    from app.services.paper_execution import PaperExecutionError

    try:
        prices = await resolve_market_prices(access_token, template.symbol, price_legs)

        # Build the execution request
        from app.schemas import ExecutionLegIn, ExecutionRequestIn

        exec_request = ExecutionRequestIn(
            client_order_id=request.client_order_id,
            symbol=template.symbol,
            strategy_tag=template.name,
            strategy_id=str(template.id),
            starting_capital=request.starting_capital,
            legs=[ExecutionLegIn(**leg) for leg in exec_legs],
        )

        result = execute_strategy(user_id, exec_request, db, prices)

        # Phase 6.10: Persist execution audit trail metadata.
        # This runs AFTER execute_strategy() has committed in a separate
        # transaction. If metadata persistence fails, the execution remains
        # valid — metadata is optional.
        try:
            metadata = _build_execution_metadata(
                template=template,
                preview_result=fresh_result,
                comparison_changes=changes,
                confirmed_strikes=request.confirmed_strikes,
                confirmed_expiries=request.confirmed_expiries,
                exec_result=result,
                exec_legs=exec_legs,
                prices=prices,
            )
            _persist_execution_metadata(db, result.execution_id, metadata)
            # Only include metadata in response if DB write succeeded
            result = result.model_copy(update={"execution_metadata": metadata})
        except Exception:
            logger.warning(
                "Failed to persist execution metadata for %s",
                result.execution_id,
                exc_info=True,
            )
            try:
                db.rollback()
            except Exception:
                pass

        return result
    except PaperExecutionError as exc:
        raise _paper_error(exc) from exc
