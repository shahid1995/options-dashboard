from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Request as FastAPIRequest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.brokers.domain.enums import BROKER_ID_UPSTOX
from app.brokers.domain.errors import BrokerError
from app.brokers.gateway import gateway
from app.routers.deps import AuthenticatedUser, CurrentUser
from app.schemas import (
    AnalyticsOut,
    BrokerProfileOut,
    BulkExitOut,
    BulkExitRequestIn,
    CapitalOut,
    ExecutionOut,
    ExecutionRequestIn,
    ExitIntentOut,
    ExitIntentPreviewOut,
    ExitIntentRequestIn,
    ExitIntentTargetOut,
    ExitOut,
    ExitRequestIn,
    LegCloseIn,
    MarketStatusOut,
    OrderFillIn,
    PortfolioOut,
    PositionOut,
    PositionValuationResponseOut,
    ReconcileOut,
    TradeOut,
)
from app.services import token_store
from app.services.journal import (
    LegNotFoundError,
    TradeClosedError,
    TradeNotFoundError,
    close_leg,
    get_journal,
    handlePaperOrderFill,
)
from app.services.capital import get_capital_summary
from app.services.market_status import get_market_status
from app.services.performance import get_analytics
from app.services.paper_execution import (
    PaperExecutionError,
    bulk_exit,
    execute_strategy,
    exit_position,
    find_exit_replay,
    get_open_positions,
    get_order_history,
    get_portfolio,
    reconcile,
    reset_portfolio,
    round_option_price,
)
router = APIRouter()

MARKET_CLOSED_MSG = "Market is closed. Paper order was not executed."
MARKET_UNKNOWN_MSG = "Unable to verify market status. Order was not executed."


def require_session(user: AuthenticatedUser) -> tuple[str, str]:
    """Validates the Upstox session and returns (user.id, access token).

    Phase 10.2A: user_id is now the canonical ``users.id`` (UUID), not the
    session_id.  Session IDs are transport-only and never used for data
    isolation."""
    return user.user_id, user.access_token


async def require_market_open(access_token: str) -> None:
    """Centralized market-hours execution gate.

    Every paper order — manual BUY/SELL, strategy-generated, or automated —
    must pass through here before anything is executed, and the check runs at
    the exact moment of execution. A closed or unverifiable market rejects the
    order; an unknown status is never treated as open.
    """
    status = await get_market_status(access_token)
    if status.status == "open":
        return
    detail = MARKET_UNKNOWN_MSG if status.status == "unknown" else MARKET_CLOSED_MSG
    raise HTTPException(status_code=409, detail=detail)


@router.get("/broker/profile", response_model=BrokerProfileOut)
async def broker_profile(
    user: AuthenticatedUser = Depends(CurrentUser()),
    refresh: bool = False,
):
    """GET /paper/broker/profile — broker connection diagnostics (Phase 6.4.1).

    Read-only: verifies the authenticated customer's Upstox connection via
    the broker's profile endpoint (server-side, reusing the existing session
    and Upstox HTTP client) and returns the NORMALIZED safe profile. No
    mutation; user-scoped; never returns credentials or raw broker payloads.
    ``refresh=true`` bypasses the short user-scoped TTL cache (manual
    refresh). Profile is NOT tick data — the page never polls it.

    Always available regardless of market status: connection diagnostics
    must work even while the market is closed.
    """
    from app.services.broker_profile import get_broker_profile_summary

    user_id, access_token = require_session(user)
    return await get_broker_profile_summary(user_id, access_token, refresh=refresh)


@router.get("/market-status", response_model=MarketStatusOut)
async def market_status(
    user: AuthenticatedUser = Depends(CurrentUser()),
    segment: str = "INDEX_DERIVATIVES",
):
    """Current market status for the paper-trading UI badge (segment-aware).

    Phase 5.2.1: the status is resolved for ONE segment (default
    INDEX_DERIVATIVES — the product's NIFTY index-options segment), so a
    cash-segment closing auction can never be mistaken for index-options
    trading. The badge is informational; the execution gate re-resolves the
    same segment at the exact moment of execution.
    """
    _, access_token = require_session(user)
    status = await get_market_status(access_token, segment=segment)
    return MarketStatusOut(
        status=status.status,
        source=status.source,
        trade_date=status.trade_date,
        checked_at=status.checked_at,
        message=status.message,
        open=status.status == "open",
        segment=status.segment,
        session_state=status.session_state,
        timezone=status.timezone,
        trading_allowed=status.trading_allowed,
    )


@router.post("/fills", status_code=201, response_model=TradeOut)
async def submit_fill(
    order: OrderFillIn,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """LEGACY journal path: auto-logs an executed paper order into trades+legs.

    Phase 5.0 supersedes this with ``POST /paper/executions`` (the
    server-authoritative execution layer). This endpoint is retained for
    backward compatibility with existing clients and tests; it writes only
    the journal tables, never the authoritative orders/positions/ledger.
    """
    user_id, access_token = require_session(user)
    await require_market_open(access_token)
    trade = handlePaperOrderFill(user_id, order, db)
    return trade


@router.post("/trades/{trade_id}/legs/{leg_id}/close", response_model=TradeOut)
async def submit_leg_close(
    trade_id: int,
    leg_id: int,
    body: LegCloseIn,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """LEGACY journal path: records a leg's exit; closes the trade once every
    leg has exited. Phase 5.0 positions exit via ``POST /paper/positions/{id}/exit``.
    """
    user_id, access_token = require_session(user)
    await require_market_open(access_token)
    try:
        trade = close_leg(user_id, trade_id, leg_id, body.exit_price, db)
    except TradeNotFoundError:
        raise HTTPException(status_code=404, detail="Trade not found")
    except LegNotFoundError:
        raise HTTPException(status_code=404, detail="Leg not found")
    except TradeClosedError:
        raise HTTPException(status_code=400, detail="Trade already closed")
    return trade


# ---- Phase 5.0: server-authoritative paper trading -------------------------


async def resolve_market_prices(access_token: str, symbol: str, legs) -> dict:
    """Resolve the authoritative fill price for every leg from market data.

    Fetches each required expiry's chain ONCE and maps each leg to the LTP of
    its own strike/side (Phase 2.1 rule: every expiry uses its own chain; a
    missing chain, strike or quote blocks execution — no fallback pricing
    from another expiry, no stale client values).

    Returns ``{(expiry, strike, option_type): ltp}``.
    """
    symbol = symbol.upper()
    adapter = gateway.create(BROKER_ID_UPSTOX, access_token=access_token)
    by_expiry: dict[str, list] = {}
    for leg in legs:
        by_expiry.setdefault(leg.expiration_date, []).append(leg)

    prices: dict[tuple, float] = {}
    try:
        for expiry, leg_list in by_expiry.items():
            chain = (await adapter.get_option_chain(symbol, expiry))["chain"]
            by_strike = {row["strike"]: row for row in chain}
            for leg in leg_list:
                row = by_strike.get(leg.strike_price)
                side = row.get(leg.option_type) if row else None
                ltp = side.get("ltp") if side else None
                if ltp is None or ltp <= 0:
                    raise PaperExecutionError(
                        "CHAIN_DATA_MISSING",
                        f"Market data unavailable for {symbol} {leg.strike_price:g} "
                        f"{leg.option_type.upper()} ({expiry}). Paper order was not executed.",
                    )
                # Phase 5.2.1: the authoritative FILL price is normalized to
                # the option's tick size (NIFTY index options: ₹0.05). The raw
                # broker LTP is kept for analytics; only the tradable price
                # crosses the fill boundary tick-aligned.
                prices[(expiry, leg.strike_price, leg.option_type)] = round_option_price(ltp)
    except BrokerError as exc:
        raise PaperExecutionError(
            "EXECUTION_FAILED", f"Could not load market data for {symbol}: {exc.message}"
        ) from exc
    return prices


async def resolve_bulk_market_prices(access_token: str, positions) -> dict:
    """Resolve the authoritative fill price for every open position (bulk).

    Fetches each required (symbol, expiry) chain ONCE and maps every position
    to the LTP of its OWN strike/side (Phase 2.1 rule: every expiry uses its
    own chain; no fallback from another expiry, no stale client values). A
    missing chain, strike or quote raises ``BULK_EXIT_CHAIN_DATA_MISSING``
    BEFORE any mutation — the whole bulk request is rejected, never a
    partial closure.

    Returns ``{(symbol, expiry, strike, option_type): ltp}``.
    """
    adapter = gateway.create(BROKER_ID_UPSTOX, access_token=access_token)
    by_chain: dict[tuple[str, str], list] = {}
    for p in positions:
        by_chain.setdefault((p.symbol.upper(), p.expiry), []).append(p)

    prices: dict[tuple, float] = {}
    try:
        for (symbol, expiry), leg_list in by_chain.items():
            chain = (await adapter.get_option_chain(symbol, expiry))["chain"]
            by_strike = {row["strike"]: row for row in chain}
            for p in leg_list:
                row = by_strike.get(p.strike)
                side = row.get(p.option_type) if row else None
                ltp = side.get("ltp") if side else None
                if ltp is None or ltp <= 0:
                    raise PaperExecutionError(
                        "BULK_EXIT_CHAIN_DATA_MISSING",
                        f"Market data unavailable for {symbol} {p.strike:g} "
                        f"{p.option_type.upper()} ({expiry}). No position was closed.",
                    )
                # Phase 5.2.1: authoritative bulk-exit fill prices are
                # normalized to the option tick size (NIFTY: ₹0.05), matching
                # the single-position exit boundary exactly.
                prices[(symbol, p.expiry, p.strike, p.option_type)] = round_option_price(ltp)
    except BrokerError as exc:
        raise PaperExecutionError(
            "EXECUTION_FAILED", f"Could not load market data: {exc.message}"
        ) from exc
    return prices


def _paper_error(
    exc: PaperExecutionError,
    db: Session | None = None,
    user_id: str | None = None,
) -> HTTPException:
    """Map a structured execution error to an HTTP response.

    The detail string carries the error CODE plus a human-readable message
    (e.g. ``CHAIN_DATA_MISSING: ...``) so the UI can show a useful message
    without exposing internal stack traces.
    """
    # Day 46 (F13): the REAL execution failure boundary emits the
    # operational alert — observational only, the mapped HTTP error below
    # is unchanged. The authenticated actor comes from the request's
    # resolved identity facts (safe durable user id — never session or
    # token material), and the alert joins the request's DB session when
    # one is provided so it persists in the same unit of work.
    try:
        from app.routers.deps import current_request_user_facts
        from app.services.operations import record_execution_failure
        from app.db import SessionLocal

        # The endpoint's already-resolved canonical identity is
        # authoritative; the request-facts fallback covers indirect paths
        # (sync dependencies run in the threadpool, so ContextVar writes
        # from them do not propagate into the endpoint's context).
        actor = user_id or current_request_user_facts().get("user_id")
        if actor:
            owns_db = db is None
            alert_db = SessionLocal() if owns_db else db
            try:
                record_execution_failure(
                    alert_db,
                    user_scope=actor,
                    order_family="paper",
                    reason=f"{exc.code}: {exc.message}",
                )
                alert_db.commit()
            finally:
                if owns_db:
                    alert_db.close()
    except Exception:  # alert recording must never change the outcome
        pass
    status = {
        "CHAIN_DATA_MISSING": 409,
        "BULK_EXIT_CHAIN_DATA_MISSING": 409,
        "INVALID_QUANTITY": 400,
        "POSITION_NOT_FOUND": 404,
        "INSUFFICIENT_POSITION": 400,
        "INVALID_STATE_TRANSITION": 400,
        "EXECUTION_FAILED": 502,
        # Day 34: centralized-risk enforcement rejections.
        "STRATEGY_CANDIDATE_REQUIRED": 409,
        "CANDIDATE_NOT_ELIGIBLE": 409,
        "CANDIDATE_LEG_MISMATCH": 409,
        "RISK_BLOCKED": 409,
        "RISK_PARTIAL": 409,
        "RISK_UNAVAILABLE": 409,
        "RISK_INVALID": 409,
    }.get(exc.code, 409)
    return HTTPException(status_code=status, detail=f"{exc.code}: {exc.message}")


@router.post("/executions", response_model=ExecutionOut)
async def submit_execution(
    request: ExecutionRequestIn,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """Execute a strategy as a group of paper orders (idempotent).

    Guards, in order: session, market OPEN re-checked at execution time,
    required chain data for every leg, then an atomic write of execution +
    orders + positions + ledger + journal record. Replays of the same
    ``client_order_id`` return the original execution without new writes.
    """
    user_id, access_token = require_session(user)
    await require_market_open(access_token)
    try:
        prices = await resolve_market_prices(access_token, request.symbol, request.legs)
        return execute_strategy(user_id, request, db, prices)
    except PaperExecutionError as exc:
        raise _paper_error(exc, db=db, user_id=user_id) from exc


@router.post("/positions/{position_id}/exit", response_model=ExitOut)
async def submit_position_exit(
    position_id: int,
    request: ExitRequestIn,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """Exit a paper position (full or partial) at the authoritative market price.

    Order of guards: session, market OPEN re-checked at execution time,
    idempotent replay (a retried ``client_order_id`` returns the ORIGINAL
    result even after the position closed, without needing market data),
    open-position check, required chain data, current market price, then an
    atomic position/order/ledger/journal update.
    """
    user_id, access_token = require_session(user)
    await require_market_open(access_token)
    try:
        from app.models import Position as PositionModel

        position = db.get(PositionModel, position_id)
        if position is None or position.user_id != user_id:
            raise PaperExecutionError("POSITION_NOT_FOUND", "Position not found.")
        replay = find_exit_replay(user_id, position, request.client_order_id, db)
        if replay is not None:
            return replay
        if position.status != "open" or position.net_quantity == 0:
            raise PaperExecutionError(
                "INSUFFICIENT_POSITION", "Position is closed — no quantity available to exit."
            )
        pseudo_leg = type(
            "ExitLeg",
            (),
            {
                "expiration_date": position.expiry,
                "strike_price": position.strike,
                "option_type": position.option_type,
            },
        )
        prices = await resolve_market_prices(access_token, position.symbol, [pseudo_leg])
        fill_price = prices[(position.expiry, position.strike, position.option_type)]
        return exit_position(user_id, position_id, request, db, fill_price)
    except PaperExecutionError as exc:
        raise _paper_error(exc, db=db, user_id=user_id) from exc


@router.post("/executions/{strategy_execution_id}/exit-all", response_model=BulkExitOut)
async def submit_execution_exit_all(
    strategy_execution_id: str,
    request: BulkExitRequestIn,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """EXIT STRATEGY — close every open position of ONE strategy execution.

    Guards, in order: session, market OPEN re-checked at execution time,
    the strategy execution exists + belongs to the user, then all required
    chain data is resolved BEFORE any mutation. Every position exits through
    the same trusted position-exit path in ONE transaction; the whole
    operation is idempotent via ``client_order_id``.
    """
    from app.models import Position as PositionModel
    from app.models import StrategyExecution as StrategyExecutionModel

    user_id, access_token = require_session(user)
    await require_market_open(access_token)
    execution = db.scalar(
        select(StrategyExecutionModel).where(
            StrategyExecutionModel.user_id == user_id,
            StrategyExecutionModel.execution_id == strategy_execution_id,
        )
    )
    if execution is None:
        raise HTTPException(status_code=404, detail="Strategy execution not found.")
    positions = db.scalars(
        select(PositionModel)
        .where(
            PositionModel.user_id == user_id,
            PositionModel.strategy_execution_id == strategy_execution_id,
            PositionModel.status == "open",
        )
        .order_by(PositionModel.id.asc())
    ).all()
    try:
        prices = await resolve_bulk_market_prices(access_token, positions)
        return bulk_exit(user_id, "STRATEGY", strategy_execution_id, request, db, prices)
    except PaperExecutionError as exc:
        raise _paper_error(exc, db=db, user_id=user_id) from exc


@router.post("/positions/exit-all", response_model=BulkExitOut)
async def submit_exit_all(
    request: BulkExitRequestIn,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """EXIT ALL — close every open paper position of the authenticated user.

    The backend owns the bulk operation (the frontend never loops over
    positions): session, market OPEN re-checked at execution time, then all
    required chain data is resolved BEFORE any mutation, then every position
    exits through the same trusted position-exit path in ONE transaction.
    The whole operation is idempotent via ``client_order_id``.
    """
    from app.models import Position as PositionModel

    user_id, access_token = require_session(user)
    await require_market_open(access_token)
    positions = db.scalars(
        select(PositionModel)
        .where(PositionModel.user_id == user_id, PositionModel.status == "open")
        .order_by(PositionModel.id.asc())
    ).all()
    try:
        prices = await resolve_bulk_market_prices(access_token, positions)
        return bulk_exit(user_id, "ACCOUNT", None, request, db, prices)
    except PaperExecutionError as exc:
        raise _paper_error(exc, db=db, user_id=user_id) from exc


@router.get("/positions", response_model=list[PositionOut])
def positions(
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
    status: str | None = Query(default=None, description="Filter: open, closed, or omit for all"),
    symbol: str | None = Query(default=None, description="Filter by symbol (case-insensitive)"),
    option_type: str | None = Query(default=None, description="Filter by option type (call, put)"),
    strategy_execution_id: str | None = Query(default=None, description="Filter by strategy execution ID"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _all: bool = Query(default=False, alias="all", description="Include closed positions (Positions module)"),
):
    """The user's paper positions (server-authoritative).

    Phase 6.6.4: enriched with strategy_leg_exposures, orders, side, lots.
    Backward-compatible: no params returns open positions (same as before).
    ``all=true`` activates the enriched path without a status filter,
    returning both open and closed positions.
    """
    user_id, _access_token = require_session(user)
    use_enriched = _all or any([status, symbol, option_type, strategy_execution_id])
    if use_enriched:
        from app.services.paper_execution import get_positions_enriched
        return get_positions_enriched(
            user_id, db,
            status=status, symbol=symbol, option_type=option_type,
            strategy_execution_id=strategy_execution_id,
            limit=limit, offset=offset,
            include_closed=_all,
        )
    return get_open_positions(user_id, db)


@router.get("/orders")
def orders(
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
    status: str | None = Query(default=None, description="Filter by status (PENDING, FILLED, REJECTED, etc.)"),
    symbol: str | None = Query(default=None, description="Filter by symbol (case-insensitive)"),
    action: str | None = Query(default=None, description="Filter by side (buy, sell)"),
    option_type: str | None = Query(default=None, description="Filter by option type (call, put)"),
    kind: str | None = Query(default=None, description="Filter by kind (entry, exit)"),
    strategy_execution_id: str | None = Query(default=None, description="Filter by strategy execution ID"),
    limit: int = Query(default=200, ge=1, le=500, description="Max orders to return"),
    offset: int = Query(default=0, ge=0, description="Offset for pagination"),
):
    """The user's paper order history with optional server-side filters.

    Backward-compatible: no parameters returns the same data as before.
    ``status`` is uppercase (PENDING, FILLED, REJECTED, etc.).
    ``symbol`` is case-insensitive.
    ``action`` is lowercase (buy, sell).
    ``option_type`` is lowercase (call, put).
    ``kind`` is lowercase (entry, exit).
    ``strategy_execution_id`` filters to one strategy execution.
    ``limit`` / ``offset`` bound the result set.
    """
    user_id, _access_token = require_session(user)
    return get_order_history(
        user_id,
        db,
        status=status,
        symbol=symbol,
        action=action,
        option_type=option_type,
        kind=kind,
        strategy_execution_id=strategy_execution_id,
        limit=limit,
        offset=offset,
    )


@router.get("/portfolio", response_model=PortfolioOut)
def portfolio(
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """Portfolio summary + strategy-grouped view (server-authoritative)."""
    user_id, _access_token = require_session(user)
    return get_portfolio(user_id, db)


@router.post("/portfolio/reset", response_model=PortfolioOut)
def portfolio_reset(
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """Clear the user's paper portfolio (executions, orders, positions, ledger)."""
    user_id, _access_token = require_session(user)
    return reset_portfolio(user_id, db)


@router.get("/reconcile", response_model=ReconcileOut)
def portfolio_reconcile(
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """Verify orders, positions, cash and executions agree; report discrepancies."""
    user_id, _access_token = require_session(user)
    return reconcile(user_id, db)


@router.get("/analytics", response_model=AnalyticsOut)
def analytics(
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
    date_from: str | None = None,
    date_to: str | None = None,
    strategy: str | None = None,
):
    """ONE authoritative analytics response: summary + performance + equity
    curve + drawdown + strategy performance + positions + journal.

    Read-only (never mutates trading state) and always available regardless
    of market status. ``date_from`` / ``date_to`` (YYYY-MM-DD) and
    ``strategy`` filter the completed-trade set used for performance, equity
    curve, drawdown, strategy groups and journal; the canonical summary
    always reflects the full portfolio.
    """
    user_id, _access_token = require_session(user)
    return get_analytics(
        user_id, db, date_from=date_from, date_to=date_to, strategy=strategy
    )


@router.get("/journal")
def journal(
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """Account, performance stats, and the full trade log for the journal UI.

    Read-only: always available, regardless of market status, so users can
    review positions, P&L and history after the market closes.
    """
    user_id, _access_token = require_session(user)
    return get_journal(user_id, db)


@router.get("/capital", response_model=CapitalOut)
async def capital(
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """GET /paper/capital — server-authoritative capital summary (Phase 6.0/6.1).

    Read-only and always available regardless of market status. Every figure
    carries its source (BROKER_REPORTED | ESTIMATED | CALCULATED) and
    availability status; missing values are null, never 0. Phase 6.1: with an
    authenticated Upstox session the broker's read-only funds + whole-strategy
    margin APIs back the broker figures; on any broker failure (including the
    daily funds maintenance window) they stay UNAVAILABLE with a structured
    code — never estimated, never paper cash, never 0. Paper capital is
    exposed as paper values, never renamed as broker funds. No Return-on-
    Capital metric is computed; only its future inputs are returned.
    """
    user_id, access_token = require_session(user)
    return await get_capital_summary(user_id, db, access_token=access_token)


# ---- Phase 6.6.5: Exit preview + Phase 6.5.0.4: exit intent -------------


@router.post("/exit-intent/preview", response_model=ExitIntentPreviewOut)
def preview_exit_intent(
    request: ExitIntentRequestIn,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """POST /paper/exit-intent/preview — Server-authoritative exit preview (Phase 6.6.5).

    Resolves the exit selector against the authenticated user's current
    StrategyLegExposure and Position data and returns the resolved targets
    WITHOUT mutating any state. The frontend uses this to display the
    confirmation dialog before the user confirms the exit.

    Does NOT:
    - create orders
    - mutate positions
    - change cash or P&L
    - update journal
    - resolve market prices (price_override is None in preview)
    """
    from app.services.exit_selector import ExitSelectorError, resolve_server_exit_targets

    user_id, _access_token = require_session(user)

    try:
        targets = resolve_server_exit_targets(
            db=db,
            user_id=user_id,
            scope=request.scope,
            strategy_execution_id=request.strategy_execution_id,
            position_id=request.position_id,
            exposure_id=request.exposure_id,
            option_type=request.option_type,
            action=request.action,
            quantity_mode=request.quantity_mode,
            quantity=request.quantity,
        )
    except ExitSelectorError as exc:
        return ExitIntentPreviewOut(
            status="NO_MATCHING_TARGETS" if exc.code == "NO_MATCHING_TARGETS" else "REJECTED",
            errors=[f"{exc.code}: {exc.message}"],
        )

    # Build preview targets with full details (no mutation)
    target_outs = []
    for t in targets:
        target_outs.append(ExitIntentTargetOut(
            position_id=t.position_id,
            strategy_leg_exposure_id=t.strategy_leg_exposure_id,
            strategy_execution_id=t.strategy_execution_id,
            symbol=t.symbol, expiry=t.expiry, strike=t.strike,
            option_type=t.option_type, source_action=t.source_action,
            exit_side=t.exit_side, quantity=t.quantity,
            remaining_quantity=t.remaining_quantity, lot_size=t.lot_size,
        ))

    warnings = []
    warnings.append("Preview only — no execution has occurred. Market prices will be resolved at execution time.")

    return ExitIntentPreviewOut(
        status="PREVIEW",
        targets=target_outs,
        warnings=warnings,
    )


@router.post("/exit-intent", response_model=ExitIntentOut)
async def submit_exit_intent(
    request: ExitIntentRequestIn,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """POST /paper/exit-intent — Server-authoritative exit intent (Phase 6.5.0.4).

    The server independently resolves the exit selector against the
    authenticated user's current StrategyLegExposure and Position data, then
    executes through the existing paper engine. The client does NOT dictate
    which exposure is actually targeted.

    Flow:
        authenticate user
        ↓
        validate selector
        ↓
        resolve StrategyLegExposure (server-authoritative)
        ↓
        create ExecutionTarget[]
        ↓
        create ExecutionIntent
        ↓
        ExecutionRouter → PAPER
        ↓
        existing paper execution engine
        ↓
        transactional state update
    """
    from app.services.exit_selector import ExitSelectorError, resolve_server_exit_targets
    from app.services.execution_intent import (
        ExecutionIntent,
        ExecutionMode,
        ExecutionRouter,
        ExecutionSource,
        ExecutionStatus,
    )

    user_id, access_token = require_session(user)
    await require_market_open(access_token)

    try:
        # 1. Server-side resolution against authoritative DB state
        targets = resolve_server_exit_targets(
            db=db,
            user_id=user_id,
            scope=request.scope,
            strategy_execution_id=request.strategy_execution_id,
            position_id=request.position_id,
            exposure_id=request.exposure_id,
            option_type=request.option_type,
            action=request.action,
            quantity_mode=request.quantity_mode,
            quantity=request.quantity,
        )
    except ExitSelectorError as exc:
        return ExitIntentOut(
            status="NO_MATCHING_TARGETS" if exc.code == "NO_MATCHING_TARGETS" else "REJECTED",
            targets_resolved=0,
            errors=[f"{exc.code}: {exc.message}"],
        )

    # 2. Create ExecutionIntent from server-resolved targets
    import secrets as _secrets
    from datetime import datetime as _dt, timezone as _tz
    strategy_exec_id = targets[0].strategy_execution_id if len(targets) == 1 else None
    intent = ExecutionIntent(
        intent_id=_secrets.token_hex(16),
        user_id=user_id,
        execution_mode=ExecutionMode.PAPER,
        source=ExecutionSource.EXIT_SELECTOR,
        targets=targets,
        idempotency_key=request.client_order_id,
        created_at=_dt.now(_tz.utc).isoformat(),
        strategy_execution_id=strategy_exec_id,
    )

    # 3. Resolve market prices for all target positions
    try:
        # Build pseudo-legs for the existing market-price resolver
        from app.routers.paper import resolve_bulk_market_prices

        # Collect unique positions
        seen_positions: set[int] = set()
        position_objects: list = []
        for t in targets:
            if t.position_id not in seen_positions:
                pos = db.get(Position, t.position_id)
                if pos is not None:
                    position_objects.append(pos)
                    seen_positions.add(t.position_id)

        prices = await resolve_bulk_market_prices(access_token, position_objects)
    except PaperExecutionError as exc:
        return ExitIntentOut(
            status="FAILED",
            intent_id=intent.intent_id,
            targets_resolved=len(targets),
            targets=[ExitIntentTargetOut(
                position_id=t.position_id,
                strategy_leg_exposure_id=t.strategy_leg_exposure_id,
                strategy_execution_id=t.strategy_execution_id,
                symbol=t.symbol, expiry=t.expiry, strike=t.strike,
                option_type=t.option_type, source_action=t.source_action,
                exit_side=t.exit_side, quantity=t.quantity,
                remaining_quantity=t.remaining_quantity, lot_size=t.lot_size,
            ) for t in targets],
            errors=[f"{exc.code}: {exc.message}"],
        )
    except BrokerError as exc:
        return ExitIntentOut(
            status="FAILED",
            intent_id=intent.intent_id,
            targets_resolved=len(targets),
            errors=[f"MARKET_DATA_ERROR: {exc.message}"],
        )

    # 4. Set fill prices on targets and execute via ExecutionRouter
    priced_targets = []
    for t in targets:
        price_key = (t.symbol.upper(), t.expiry, t.strike, t.option_type)
        price = prices.get(price_key)
        if price is None:
            return ExitIntentOut(
                status="FAILED",
                intent_id=intent.intent_id,
                targets_resolved=len(targets),
                errors=[f"CHAIN_DATA_MISSING: Market data unavailable for {t.symbol} {t.strike:g} {t.option_type.upper()} ({t.expiry})."],
            )
        from dataclasses import replace
        priced_targets.append(replace(t, price_override=price))

    intent.targets = priced_targets

    # 5. Execute through the ExecutionRouter
    router = ExecutionRouter(db=db)
    result = await router.execute_intent(intent, access_token=access_token)

    # 6. Build response
    target_outs = []
    order_outs = []
    position_outs = []
    for r in result.results:
        if "order" in r and r["order"]:
            order_outs.append(r["order"])
        if "position" in r and r["position"]:
            position_outs.append(r["position"])
    for t in priced_targets:
        target_outs.append(ExitIntentTargetOut(
            position_id=t.position_id,
            strategy_leg_exposure_id=t.strategy_leg_exposure_id,
            strategy_execution_id=t.strategy_execution_id,
            symbol=t.symbol, expiry=t.expiry, strike=t.strike,
            option_type=t.option_type, source_action=t.source_action,
            exit_side=t.exit_side, quantity=t.quantity,
            remaining_quantity=t.remaining_quantity, lot_size=t.lot_size,
        ))

    return ExitIntentOut(
        status=result.status.value,
        intent_id=result.intent_id,
        duplicated=result.duplicated,
        targets_resolved=len(targets),
        targets_executed=result.targets_succeeded,
        targets=target_outs,
        orders=order_outs,
        positions=position_outs,
        errors=result.errors,
    )


# ---- Phase 6.6.6: live position valuation -----------------------------------


@router.get("/positions/valuation", response_model=PositionValuationResponseOut)
async def positions_valuation(
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """GET /paper/positions/valuation — server-authoritative live valuation.

    Resolves current LTP for every open position via the broker adapter and
    computes live/unrealized P&L, market value, and P&L %. Strategy-level
    and leg-level aggregation is included when StrategyLegExposure exists.

    Always available regardless of market status (read-only).
    Missing/stale LTP is explicit (None), never zero.
    LIVE execution is NOT triggered.
    """
    from app.services.valuation import resolve_live_valuation

    user_id, access_token = require_session(user)
    positions, summary = await resolve_live_valuation(db, user_id, access_token)

    from app.services.valuation import (
        LegValuation,
        PositionValuation,
        StrategyValuation,
    )

    def _lv_out(lv: LegValuation) -> dict:
        return {
            "exposure_id": lv.exposure_id,
            "execution_id": lv.execution_id,
            "action": lv.action,
            "remaining_quantity": lv.remaining_quantity,
            "lot_size": lv.lot_size,
            "entry_price": lv.entry_price,
            "current_price": lv.current_price,
            "market_value": lv.market_value,
            "live_pnl": lv.live_pnl,
            "price_status": lv.price_status,
        }

    def _sv_out(sv: StrategyValuation) -> dict:
        return {
            "execution_id": sv.execution_id,
            "strategy_tag": sv.strategy_tag,
            "live_pnl": sv.live_pnl,
            "market_value": sv.market_value,
            "legs": [_lv_out(lv) for lv in sv.legs],
            "price_status": sv.price_status,
        }

    position_outs = []
    for pv in positions:
        position_outs.append({
            "position_id": pv.position_id,
            "symbol": pv.symbol,
            "expiry": pv.expiry,
            "strike": pv.strike,
            "option_type": pv.option_type,
            "side": pv.side,
            "net_quantity": pv.net_quantity,
            "average_entry_price": pv.average_entry_price,
            "lot_size": pv.lot_size,
            "realized_pnl": pv.realized_pnl,
            "current_price": pv.current_price,
            "market_value": pv.market_value,
            "live_pnl": pv.live_pnl,
            "live_pnl_pct": pv.live_pnl_pct,
            "price_status": pv.price_status,
            "strategies": [_sv_out(sv) for sv in pv.strategies],
        })

    summary_out = {
        "total_live_pnl": summary.total_live_pnl,
        "total_market_value": summary.total_market_value,
        "total_realized_pnl": summary.total_realized_pnl,
        "open_position_count": summary.open_position_count,
        "positions_with_price": summary.positions_with_price,
        "positions_unavailable": summary.positions_unavailable,
        "generated_at": summary.generated_at,
        "status": summary.status,
    }

    return PositionValuationResponseOut(positions=position_outs, summary=summary_out)
