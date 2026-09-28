"""Server-authoritative paper trading engine (Phase 5.0/5.2).

The backend is the single source of truth for paper orders, fills,
positions, cash and P&L. This service implements:

- the order status lifecycle + pure transition validator
- netted positions (weighted-average entry, realized P&L on reduction,
  partial/full exits, reversal)
- the auditable cash ledger (``PaperTransaction``): available cash is
  derived as ``starting_capital + SUM(amount)``
- idempotent strategy executions (``client_order_id`` unique per user) and
  idempotent exits
- idempotent BULK exits (Phase 5.2): EXIT STRATEGY (one
  ``strategy_execution_id``) and EXIT ALL (the whole account) reuse the
  exact same trusted position-exit path, one commit, one result record
- strategy grouping (all legs of one execution share ``execution_id``)
- portfolio summary, strategy-grouped view and a reconciliation check

Execution is ATOMIC: every validation (market gate, chain data, prices)
happens BEFORE any row is written, so a successful execution is FILLED with
all orders filled and a failed one writes nothing. No fake async fills, no
partial-success ambiguity. Bulk exits follow the same rule: all positions
and prices are validated up front, and only then does the mutation phase
run inside ONE database transaction.

All quantities are LOTS. Rupee exposure scales by ``lot_size``
(contracts per lot). Position quantity convention: BUY = +, SELL = −.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    BulkExitRecord,
    Leg,
    PaperAccount,
    PaperOrder,
    PaperTransaction,
    Position,
    StrategyExecution,
    StrategyLegExposure,
    Trade,
)
from app.schemas import (
    BulkExitGroupOut,
    BulkExitOut,
    BulkExitPositionOut,
    ExitRequestIn,
    ExecutionOut,
    ExitOut,
    OrderOut,
    PortfolioGroupOut,
    PortfolioOut,
    PositionOut,
    PortfolioSummaryOut,
    ReconcileOut,
)

DEFAULT_STARTING_CAPITAL = 500000

# ---- Option tick-size normalization (Phase 5.2.1) ---------------------------
#
# NSE specifies the option price step for NIFTY index options as ₹0.05. The
# authoritative paper FILL price is normalized to this tick, and the same
# canonical helper is used at every fill boundary (strategy entry, single
# position exit, bulk exit) so the server and the UI display agree. Raw
# broker market-data LTPs used for analytics are NEVER overwritten — only
# prices treated as tradable option prices cross the boundary tick-aligned.

DEFAULT_OPTION_TICK_SIZE = 0.05


def round_option_price(price: float | None, tick_size: float = DEFAULT_OPTION_TICK_SIZE) -> float | None:
    """Round an option trading price to the nearest valid tick.

    Numerically safe: works in tick units and re-normalizes with a final
    10-decimal rounding so floating-point artifacts like
    ``125.25000000000001`` never escape (125.23 → 125.25, 125.28 → 125.30).
    Missing/invalid prices are never coerced to 0: ``None``/NaN stay
    unavailable, and a negative (invalid) price passes through unchanged so
    upstream validation can reject it explicitly.
    """
    if price is None or not isinstance(price, (int, float)):
        return price
    if isinstance(price, float) and price != price:  # NaN is not a valid price
        return None
    if tick_size is None or tick_size <= 0 or price < 0:
        return price
    ticks = round(price / tick_size)
    return round(ticks * tick_size, 10)

# ---- Order status lifecycle (§5) -------------------------------------------

ORDER_STATUSES = frozenset({"PENDING", "FILLED", "PARTIALLY_FILLED", "CANCELLED", "REJECTED"})
ORDER_TRANSITIONS = {
    "PENDING": {"FILLED", "PARTIALLY_FILLED", "CANCELLED", "REJECTED"},
    "PARTIALLY_FILLED": {"FILLED", "CANCELLED", "REJECTED"},
    "FILLED": set(),
    "CANCELLED": set(),
    "REJECTED": set(),
}

EXECUTION_STATUSES = frozenset({"PENDING", "FILLED", "PARTIAL", "FAILED", "CANCELLED"})

# ---- Structured errors (§32) ------------------------------------------------


class PaperExecutionError(Exception):
    """A structured paper-execution failure.

    ``code`` is one of the documented error codes (MARKET_CLOSED,
    CHAIN_DATA_MISSING, INVALID_QUANTITY, POSITION_NOT_FOUND,
    INSUFFICIENT_POSITION, DUPLICATE_REQUEST, INVALID_STATE_TRANSITION,
    EXECUTION_FAILED). ``message`` is human-readable.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---- Pure helpers -----------------------------------------------------------


def can_transition(current: str, target: str) -> bool:
    """Whether ``current → target`` is a valid order-status transition."""
    return target in ORDER_TRANSITIONS.get(current, set())


def transition(current: str, target: str) -> str:
    """Validate and apply an order-status transition (pure)."""
    if not can_transition(current, target):
        raise PaperExecutionError(
            "INVALID_STATE_TRANSITION",
            f"Invalid order status transition: {current} → {target}",
        )
    return target


def cash_flow(action: str, price: float, quantity: int, lot_size: int) -> float:
    """Signed rupee cash flow for a fill: buy pays out (−), sell receives (+)."""
    direction = -1 if action == "buy" else 1
    return round(direction * price * quantity * lot_size, 2)


def _journal_dir(action: str) -> int:
    """Premium-flow direction matching the legacy journal: buy = +1, sell = −1."""
    return -1 if action == "sell" else 1


def apply_fill(
    net_quantity: int,
    average_price: float,
    action: str,
    quantity: int,
    price: float,
    lot_size: int,
) -> tuple[int, float, float]:
    """Apply one fill to a netted position.

    Returns ``(new_net_quantity, new_average_price, realized_pnl_rupees)``.

    - Same direction (or a fresh position): weighted-average entry price.
    - Opposite direction, partial/full: realized P&L against the existing
      average; the average is unchanged for the remaining quantity.
    - Opposite direction, overshooting (reversal): the covered part realizes,
      the leftover flips the position at the fill price.
    """
    signed = quantity if action == "buy" else -quantity
    new_net = net_quantity + signed

    if net_quantity == 0 or net_quantity * signed > 0:
        if new_net == 0:
            return (0, average_price, 0.0)
        new_avg = (abs(net_quantity) * average_price + quantity * price) / abs(new_net)
        return (new_net, new_avg, 0.0)

    covered = min(abs(signed), abs(net_quantity))
    if net_quantity > 0:  # long position reduced/closed by a sell
        realized = (price - average_price) * covered * lot_size
    else:  # short position reduced/closed by a buy
        realized = (average_price - price) * covered * lot_size

    if abs(signed) <= abs(net_quantity):
        return (new_net, average_price, realized)
    return (new_net, price, realized)


def unrealized_pnl(net_quantity: int, average_price: float, price: float, lot_size: int) -> float:
    """Unrealized P&L of a position marked at ``price`` (rupees).

    Long: (price − avg) × qty × lot. Short: (avg − price) × qty × lot.
    A zero/closed position has zero unrealized P&L.
    """
    if net_quantity == 0:
        return 0.0
    if net_quantity > 0:
        return round((price - average_price) * net_quantity * lot_size, 2)
    return round((average_price - price) * abs(net_quantity) * lot_size, 2)


# ---- DB helpers -------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_execution_id() -> str:
    return secrets.token_hex(16)


def _get_or_create_account(user_id: str, starting_capital: float | None, db: Session) -> PaperAccount:
    account = db.scalar(select(PaperAccount).where(PaperAccount.user_id == user_id))
    if account is None:
        account = PaperAccount(
            user_id=user_id,
            starting_capital=starting_capital or DEFAULT_STARTING_CAPITAL,
        )
        db.add(account)
        db.flush()
    elif starting_capital is not None:
        account.starting_capital = starting_capital
        account.updated_at = _now()
    return account


def _get_position(
    db: Session,
    user_id: str,
    symbol: str,
    expiry: str,
    strike: float,
    option_type: str,
    *,
    for_update: bool = False,
) -> Position | None:
    """Load one position, optionally taking its row lock before mutation."""
    statement = select(Position).where(
        Position.user_id == user_id,
        Position.symbol == symbol,
        Position.expiry == expiry,
        Position.strike == strike,
        Position.option_type == option_type,
    )
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)


def _get_or_create_position(
    db: Session,
    *,
    user_id: str,
    symbol: str,
    expiry: str,
    strike: float,
    option_type: str,
    lot_size: int,
    execution_id: str,
    opened_at: datetime,
) -> Position:
    """Get the instrument position under lock, creating it safely when absent.

    Existing rows are locked with SELECT ... FOR UPDATE. For the absent-row
    case, the insert is isolated in a savepoint so two concurrent transactions
    racing to create the same unique position can recover from the loser-side
    uniqueness conflict, re-load the now-existing row under lock, and continue
    without corrupting the outer execution transaction.
    """
    position = _get_position(
        db, user_id, symbol, expiry, strike, option_type, for_update=True
    )
    if position is not None:
        return position

    try:
        with db.begin_nested():
            position = Position(
                user_id=user_id,
                symbol=symbol,
                expiry=expiry,
                strike=strike,
                option_type=option_type,
                net_quantity=0,
                average_entry_price=0.0,
                lot_size=lot_size,
                realized_pnl=0.0,
                status="open",
                strategy_execution_id=execution_id,
                opened_at=opened_at,
            )
            db.add(position)
            db.flush()
    except IntegrityError:
        position = _get_position(
            db, user_id, symbol, expiry, strike, option_type, for_update=True
        )
        if position is None:
            raise PaperExecutionError(
                "POSITION_CONCURRENCY",
                "Position creation raced another execution and could not be re-read safely.",
            )

    return position


def _instrument_orders(db: Session, user_id: str, position: Position) -> list[PaperOrder]:
    """All FILLED orders (entries + exits) for the position's instrument."""
    return list(
        db.scalars(
            select(PaperOrder)
            .where(
                PaperOrder.user_id == user_id,
                PaperOrder.symbol == position.symbol,
                PaperOrder.expiry == position.expiry,
                PaperOrder.strike == position.strike,
                PaperOrder.option_type == position.option_type,
                PaperOrder.status == "FILLED",
            )
            .order_by(PaperOrder.id.asc())
        ).all()
    )


def _transaction_type(kind: str, action: str) -> str:
    if kind == "entry":
        return "ENTRY_DEBIT" if action == "buy" else "ENTRY_CREDIT"
    return "EXIT_DEBIT" if action == "buy" else "EXIT_CREDIT"


def _serialize_order(order: PaperOrder) -> dict:
    return OrderOut.model_validate(order).model_dump(mode="json")


def _serialize_position(position: Position) -> dict:
    return PositionOut.model_validate(position).model_dump(mode="json")


def _strategy_tag_for(db: Session, user_id: str, execution_id: str | None) -> str:
    """Resolve the execution's displayed strategy name (fallback "Custom")."""
    if not execution_id:
        return "Custom"
    tag = db.scalar(
        select(StrategyExecution.strategy_tag).where(
            StrategyExecution.user_id == user_id,
            StrategyExecution.execution_id == execution_id,
        )
    )
    return tag or "Custom"


def _attach_strategy_tags(db: Session, user_id: str, rows: list[dict]) -> list[dict]:
    """Attach ``strategy_tag`` to serialized positions in one batched lookup.

    The authoritative relationship stays strategy_execution_id →
    StrategyExecution.strategy_tag; positions never duplicate strategy
    logic. Legacy/missing executions fall back to "Custom".
    """
    ids = {r.get("strategy_execution_id") for r in rows if r.get("strategy_execution_id")}
    if ids:
        mapping = dict(
            db.execute(
                select(StrategyExecution.execution_id, StrategyExecution.strategy_tag).where(
                    StrategyExecution.user_id == user_id,
                    StrategyExecution.execution_id.in_(ids),
                )
            ).all()
        )
        for r in rows:
            eid = r.get("strategy_execution_id")
            r["strategy_tag"] = mapping.get(eid) or "Custom"
    else:
        for r in rows:
            r["strategy_tag"] = "Custom"
    return rows


# ---- Strategy execution (§7/§8) ---------------------------------------------


def execute_strategy(
    user_id: str,
    request,
    db: Session,
    prices: dict,
    *,
    risk_candidate=None,
    risk_policy=None,
    reference_timestamp=None,
) -> ExecutionOut:
    """Create one strategy execution atomically.

    ``prices`` maps ``(expiry, strike, option_type) -> fill price`` and is
    resolved by the router from the authoritative chain data BEFORE any
    writes happen. Every leg must resolve; otherwise ``CHAIN_DATA_MISSING``
    is raised and nothing is written (never a misleading partial success).

    Idempotency: a retried ``client_order_id`` returns the ORIGINAL
    execution untouched — no second execution, no second orders, no
    double-counted cash, no duplicate journal record.

    Day 34 enforcement (centralized risk integration):
    ``risk_candidate`` must be a GENUINE eligible Day-32 ``StrategyCandidate``
    whose legs EXACTLY match the request legs.  A new entry with no genuine
    candidate is rejected (``STRATEGY_CANDIDATE_REQUIRED``) before any write;
    the candidate's Day-33 ``CentralRiskResult`` must be PASS or the entry is
    rejected (``RISK_BLOCKED`` / ``RISK_PARTIAL`` / ``RISK_UNAVAILABLE`` /
    ``RISK_INVALID``) with ZERO mutation.  The replay branch above stays
    FIRST, so a previously successful ``client_order_id`` returns its
    original execution even under a changed policy.  On PASS the audit
    reference is stored on ``execution_metadata`` in the SAME transaction.
    """
    now = _now()
    symbol = request.symbol.upper()

    existing = db.scalar(
        select(StrategyExecution).where(
            StrategyExecution.user_id == user_id,
            StrategyExecution.client_order_id == request.client_order_id,
        )
    )
    if existing is not None:
        return _execution_out(existing, db, duplicated=True)

    # ---- Day 34: centralized-risk enforcement at the mutation choke point --
    # Replay is handled above; everything below runs ONLY for a NEW entry.
    # Every validation here precedes the first DB write (including
    # _get_or_create_account), so any rejection leaves zero rows.
    risk_metadata: str | None = None
    if risk_candidate is None:
        raise PaperExecutionError(
            "STRATEGY_CANDIDATE_REQUIRED",
            "A paper strategy entry requires a genuine Strategy Candidate "
            "from the approved intelligence chain (Opportunity -> Day-32 "
            "Gate -> Day-33 Central Risk). Manual/custom entries without a "
            "candidate are rejected. Paper order was not executed.",
        )
    # The risked legs must be the executed legs (multiset equality).
    from app.services.paper_risk import legs_match_request

    if not legs_match_request(risk_candidate.legs, list(request.legs)):
        raise PaperExecutionError(
            "CANDIDATE_LEG_MISMATCH",
            "The requested execution legs do not exactly match the genuine "
            "strategy candidate legs. Paper order was not executed.",
        )
    if risk_policy is None:
        from app.services.paper_risk import PAPER_ENTRY_POLICY

        risk_policy = PAPER_ENTRY_POLICY
    from app.central_risk.contracts import (
        CENTRAL_RISK_CALCULATION_VERSION,
        CentralRiskStatus,
    )
    from app.central_risk.engine import assess_candidate_risk

    decision = assess_candidate_risk(
        risk_candidate, risk_policy,
        reference_timestamp=reference_timestamp)
    if decision.status is not CentralRiskStatus.PASS:
        detail = "; ".join(r.message for r in decision.blocking_reasons)
        if not detail:
            detail = "; ".join(i.message for i in decision.issues)
        raise PaperExecutionError(
            f"RISK_{decision.status.value}",
            f"Central risk {decision.status.value}: {detail} "
            "Paper order was not executed.",
        )
    risk_metadata = json.dumps({
        "risk_status": decision.status.value,
        "risk_policy_version": risk_policy.policy_version,
        "risk_assessment_id":
            f"{risk_candidate.candidate_id}@{risk_policy.policy_version}",
        "risk_reference_timestamp": decision.reference_timestamp.isoformat(),
        "risk_calculation_version": CENTRAL_RISK_CALCULATION_VERSION,
        "candidate_id": risk_candidate.candidate_id,
        "opportunity_id": risk_candidate.opportunity_id,
    })

    # Pre-validate every leg resolves to a market price before writing.
    missing = []
    for leg in request.legs:
        key = (leg.expiration_date, leg.strike_price, leg.option_type)
        price = prices.get(key)
        if price is None or price <= 0:
            missing.append(
                f"Leg {leg.option_type.upper()} {leg.strike_price:g}: market data "
                f"unavailable for expiry {leg.expiration_date}."
            )
    if missing:
        raise PaperExecutionError(
            "CHAIN_DATA_MISSING",
            " ".join(missing) + " Paper order was not executed.",
        )

    execution_id = _new_execution_id()
    _get_or_create_account(user_id, request.starting_capital, db)

    execution = StrategyExecution(
        user_id=user_id,
        execution_id=execution_id,
        client_order_id=request.client_order_id,
        strategy_id=request.strategy_id,
        strategy_tag=request.strategy_tag or "Custom",
        symbol=symbol,
        status="FILLED",
        entry_net=0.0,
        entry_at=now,
        execution_metadata=risk_metadata,
    )
    db.add(execution)
    db.flush()

    # Legacy journal record (same execution id + idempotency key so the
    # journal UI stays consistent and retries can never duplicate it).
    trade = Trade(
        user_id=user_id,
        symbol=symbol,
        strategy_tag=request.strategy_tag or "Custom",
        status="open",
        entry_net=0.0,
        entry_at=now,
        strategy_execution_id=execution_id,
        client_order_id=request.client_order_id,
    )
    db.add(trade)
    db.flush()

    entry_net = 0.0
    orders: list[PaperOrder] = []
    for i, leg in enumerate(request.legs):
        # Phase 5.2.1: the authoritative fill price is already tick-aligned
        # by the router; the belt-and-braces normalization below guarantees
        # no legacy caller can write an off-tick fill.
        fill_price = round_option_price(prices[(leg.expiration_date, leg.strike_price, leg.option_type)])
        leg_symbol = leg.symbol.upper()

        order = PaperOrder(
            user_id=user_id,
            client_order_id=f"{request.client_order_id}:{i}",
            execution_id=execution_id,
            kind="entry",
            symbol=leg_symbol,
            expiry=leg.expiration_date,
            strike=leg.strike_price,
            option_type=leg.option_type,
            action=leg.action,
            quantity=leg.quantity,
            lot_size=leg.lot_size,
            status="FILLED",
            filled_quantity=leg.quantity,
            fill_price=fill_price,
            price_source="market",
        )
        db.add(order)
        db.flush()

        # Netted position (BUY = +, SELL = −). Same instrument nets into the
        # same row; the first opening execution stays the group identity.
        position = _get_or_create_position(
            db,
            user_id=user_id,
            symbol=leg_symbol,
            expiry=leg.expiration_date,
            strike=leg.strike_price,
            option_type=leg.option_type,
            lot_size=leg.lot_size,
            execution_id=execution_id,
            opened_at=now,
        )
        if position.status == "closed":
            # Reopening a previously closed instrument: the same row resumes.
            # Lifetime realized P&L for the instrument is kept (never erased)
            # and the new entry starts a fresh net quantity at the new price.
            position.status = "open"
            position.closed_at = None
            position.updated_at = now

        new_net, new_avg, realized = apply_fill(
            position.net_quantity, position.average_entry_price,
            leg.action, leg.quantity, fill_price, leg.lot_size,
        )
        position.net_quantity = new_net
        position.average_entry_price = new_avg
        position.lot_size = leg.lot_size
        position.realized_pnl = round(position.realized_pnl + realized, 2)
        position.updated_at = now

        amount = cash_flow(leg.action, fill_price, leg.quantity, leg.lot_size)
        db.add(
            PaperTransaction(
                user_id=user_id,
                execution_id=execution_id,
                order_id=order.id,
                type=_transaction_type("entry", leg.action),
                amount=amount,
                created_at=now,
            )
        )
        entry_net += _journal_dir(leg.action) * fill_price * leg.quantity * leg.lot_size

        # Legacy journal leg mirroring the order (premium = authoritative fill).
        jleg = Leg(
            trade_id=trade.id,
            symbol=leg_symbol,
            expiration_date=leg.expiration_date,
            strike_price=leg.strike_price,
            option_type=leg.option_type,
            action=leg.action,
            premium=fill_price,
            quantity=leg.quantity,
            lot_size=leg.lot_size,
            entry_at=now,
        )
        db.add(jleg)
        db.flush()
        order.journal_leg_id = jleg.id
        order.position_id = position.id
        orders.append(order)

    # Phase 6.5.0.1: persist per-execution leg attribution (one row per
    # entry order) so future strategy-scoped exits can distinguish
    # BUY CE / SELL CE / BUY PE / SELL PE even when several executions
    # trade the same instrument. Idempotent per (user_id, order_id).
    from app.services.leg_exposure import create_exposures_for_orders

    create_exposures_for_orders(db, user_id, execution_id, orders, now)

    execution.entry_net = round(entry_net, 2)
    trade.entry_net = round(entry_net, 2)
    db.commit()
    db.refresh(execution)
    return _execution_out(execution, db, duplicated=False)


# ---- Position exit (§28) -----------------------------------------------------


def find_exit_replay(user_id: str, position: Position, client_order_id: str, db: Session) -> ExitOut | None:
    """Idempotent replay lookup for a position exit.

    Must run BEFORE any open-position/price validation so a retried exit
    returns the ORIGINAL result even after the position has closed and even
    if market data is currently unavailable.
    """
    existing = db.scalar(
        select(PaperOrder).where(
            PaperOrder.user_id == user_id,
            PaperOrder.client_order_id == client_order_id,
            PaperOrder.kind == "exit",
            PaperOrder.position_id == position.id,
        )
    )
    if existing is None:
        return None
    position_out = _serialize_position(position)
    position_out["strategy_tag"] = _strategy_tag_for(db, user_id, position.strategy_execution_id)
    return ExitOut(order=_serialize_order(existing), position=position_out, duplicated=True)


def exit_position(
    user_id: str, position_id: int, request, db: Session, fill_price: float,
    *, commit: bool = True, exit_side: str | None = None,
    target_exposure_id: int | None = None,
) -> ExitOut:
    """Exit (partially or fully) a paper position at the authoritative price.

    Validates: position exists + belongs to the user, quantity is valid and
    available, then applies the fill, records the exit order + ledger
    transaction, closes the journal legs FIFO and marks the position CLOSED
    when no residual quantity remains (the record is kept, never deleted).

    Idempotent: retrying the same ``client_order_id`` for the same position
    returns the original exit order + the current position.

    ``commit=False`` lets the Phase 5.2 bulk-exit engine run many exits and
    commit ONCE, so a bulk operation is one atomic transaction; the
    single-position path keeps committing per request (unchanged).

    ``exit_side``: when provided (Phase 6.6.5), overrides the automatic
    side derivation from position.net_quantity. Required for leg-aware
    exits where the exit transaction must match a specific exposure
    (e.g. BUY CE exposure → SELL CE exit, even on a net-long position).

    ``target_exposure_id``: when provided (Phase 6.6.5), the exposure
    maintenance step will reduce THIS specific StrategyLegExposure
    instead of using FIFO across the dominant side.
    """
    now = _now()

    position = db.scalar(
        select(Position)
        .where(Position.id == position_id, Position.user_id == user_id)
        .with_for_update()
    )
    if position is None:
        raise PaperExecutionError("POSITION_NOT_FOUND", "Position not found.")

    existing = find_exit_replay(user_id, position, request.client_order_id, db)
    if existing is not None:
        return existing

    if position.status != "open" or position.net_quantity == 0:
        raise PaperExecutionError(
            "INSUFFICIENT_POSITION",
            "Position is closed — no quantity available to exit.",
        )

    qty = request.quantity or abs(position.net_quantity)
    if qty <= 0:
        raise PaperExecutionError("INVALID_QUANTITY", "Exit quantity must be positive.")
    if qty > abs(position.net_quantity):
        raise PaperExecutionError(
            "INSUFFICIENT_POSITION",
            f"Only {abs(position.net_quantity)} lot(s) available to exit.",
        )

    # Phase 6.5.0.1: the pre-exit net determines which exposure side an exit
    # reduces (the dominant side of the position that actually shrinks).
    # Phase 6.6.5: when exit_side is explicitly provided (leg-aware exit),
    # use it instead of deriving from position.net_quantity.
    prior_net_quantity = position.net_quantity
    if exit_side and exit_side in ("buy", "sell"):
        action = exit_side
    else:
        action = "sell" if position.net_quantity > 0 else "buy"
    new_net, new_avg, realized = apply_fill(
        position.net_quantity, position.average_entry_price,
        action, qty, fill_price, position.lot_size,
    )

    order = PaperOrder(
        user_id=user_id,
        client_order_id=request.client_order_id,
        execution_id=None,
        position_id=position.id,
        kind="exit",
        symbol=position.symbol,
        expiry=position.expiry,
        strike=position.strike,
        option_type=position.option_type,
        action=action,
        quantity=qty,
        lot_size=position.lot_size,
        status="FILLED",
        filled_quantity=qty,
        # Phase 5.2.1: exits fill on the option tick, matching the entry
        # boundary so the canonical fill/display price agrees.
        fill_price=round_option_price(fill_price),
        price_source="market",
        realized_pnl=round(realized, 2),
    )
    db.add(order)
    db.flush()

    position.net_quantity = new_net
    position.average_entry_price = new_avg
    position.realized_pnl = round(position.realized_pnl + realized, 2)
    position.updated_at = now
    was_open = True
    if new_net == 0:
        position.status = "closed"
        position.closed_at = now

    db.add(
        PaperTransaction(
            user_id=user_id,
            execution_id=position.strategy_execution_id,
            order_id=order.id,
            type=_transaction_type("exit", action),
            amount=cash_flow(action, fill_price, qty, position.lot_size),
            created_at=now,
        )
    )

    _close_journal_legs(user_id, position, qty, fill_price, db, now)
    # Phase 6.5.0.1: keep strategy-leg attribution in sync with the fill
    # (best-effort, never blocks or alters the authoritative exit).
    # Phase 6.6.5: pass target_exposure_id for leg-aware targeted allocation.
    from app.services.leg_exposure import maintain_exposure_on_exit

    maintain_exposure_on_exit(
        db, user_id, position, prior_net_quantity, qty, now,
        target_exposure_id=target_exposure_id,
        exit_order_id=order.id,
    )

    if position.strategy_execution_id:
        execution = db.scalar(
            select(StrategyExecution).where(
                StrategyExecution.user_id == user_id,
                StrategyExecution.execution_id == position.strategy_execution_id,
            )
        )
        if execution is not None:
            # Accumulate realized P&L on EVERY exit (partial OR full) so the
            # execution's total equals the sum of its positions' realizations.
            execution.realized_pnl = round((execution.realized_pnl or 0.0) + realized, 2)
            execution.updated_at = now
            if new_net == 0:
                # exit_at marks the moment the STRATEGY fully closes: only
                # when no position of the execution is still open (a two-leg
                # spread is not "exited" when just its first leg closes).
                db.flush()  # persist this position's CLOSED state first
                siblings_open = db.scalar(
                    select(func.count(Position.id)).where(
                        Position.user_id == user_id,
                        Position.strategy_execution_id == position.strategy_execution_id,
                        Position.status == "open",
                    )
                )
                if not siblings_open:
                    execution.exit_at = now

    if commit:
        db.commit()
        db.refresh(position)
    position_out = _serialize_position(position)
    position_out["strategy_tag"] = _strategy_tag_for(db, user_id, position.strategy_execution_id)
    return ExitOut(order=_serialize_order(order), position=position_out, duplicated=False)


def _close_journal_legs(user_id: str, position: Position, exit_qty: int, exit_price: float, db: Session, now: datetime) -> None:
    """Close the legacy journal legs backing an exit, FIFO across entry orders.

    The authoritative position math is exact regardless; this keeps the
    journal's closed-trade stats consistent. Whole-leg granularity: when an
    exit covers part of a leg, the leg is marked closed with realized P&L
    scaled to the covered quantity (the leg's original quantity is kept for
    history). This is a journal-view approximation for exotic partial
    netting; documented in PROJECT_STATUS.

    Phase 6.5.0.1: entry orders are scoped to the position's own strategy
    execution when it has one, so an exit of a netted position shared by
    multiple executions can NEVER close journal legs belonging to a
    DIFFERENT execution. Positions without an execution id (legacy
    standalone) keep the historical instrument-wide FIFO behaviour.
    """
    filters = [
        PaperOrder.user_id == user_id,
        PaperOrder.kind == "entry",
        PaperOrder.symbol == position.symbol,
        PaperOrder.expiry == position.expiry,
        PaperOrder.strike == position.strike,
        PaperOrder.option_type == position.option_type,
        PaperOrder.status == "FILLED",
    ]
    if position.strategy_execution_id:
        filters.append(PaperOrder.execution_id == position.strategy_execution_id)
    entry_orders = db.scalars(
        select(PaperOrder).where(*filters).order_by(PaperOrder.id.asc())
    ).all()

    remaining = exit_qty
    touched_trades: set[int] = set()
    for order in entry_orders:
        if remaining <= 0:
            break
        cover = min(order.quantity, remaining)
        remaining -= cover
        if order.journal_leg_id is None:
            continue
        leg = db.get(Leg, order.journal_leg_id)
        if leg is None or leg.exit_at is not None:
            continue
        leg.exit_price = exit_price
        leg.exit_at = now
        full_realized = _journal_dir(leg.action) * (exit_price - leg.premium) * leg.quantity * leg.lot_size
        leg.realized_pnl = round(full_realized * (cover / leg.quantity), 2) if leg.quantity else 0.0
        touched_trades.add(leg.trade_id)

    if touched_trades:
        trades = db.scalars(select(Trade).where(Trade.id.in_(touched_trades))).all()
        for t in trades:
            if t.status == "open" and all(l.exit_at is not None for l in t.legs):
                t.status = "closed"
                t.exit_at = now
                t.realized_pnl = round(sum(l.realized_pnl or 0.0 for l in t.legs), 2)
                t.updated_at = now


# ---- Bulk exit (Phase 5.2) ---------------------------------------------------


def bulk_exit(
    user_id: str,
    scope: str,
    strategy_execution_id: str | None,
    request: ExitRequestIn,
    db: Session,
    prices: dict,
) -> BulkExitOut:
    """Exit many positions in ONE atomic, idempotent operation.

    ``scope`` is "STRATEGY" (every open position of one execution) or
    "ACCOUNT" (every open position of the user). ``prices`` maps
    ``(symbol, expiry, strike, option_type) -> authoritative fill price``
    and is resolved by the router from market data BEFORE any write happens
    (a missing chain/quote raises ``BULK_EXIT_CHAIN_DATA_MISSING`` and
    NOTHING is closed).

    Every position exits through the SAME trusted ``exit_position`` path as
    the single-position endpoint (ONE authoritative P&L/cash/position-
    closing implementation), run with ``commit=False`` and committed ONCE at
    the end — the whole operation is one database transaction.

    Idempotency: the same ``client_order_id`` replays the ORIGINAL result
    from ``BulkExitRecord`` (``duplicated=True``) — no second exit orders,
    no duplicate cash-ledger entries, no duplicate journal rows.
    """
    key = request.client_order_id

    # 1. Idempotent replay — return the ORIGINAL result, never re-run.
    record = db.scalar(
        select(BulkExitRecord).where(
            BulkExitRecord.user_id == user_id,
            BulkExitRecord.client_order_id == key,
        )
    )
    if record is not None:
        return _bulk_exit_replay(record)

    # 2. Collect the target open positions (server-authoritative).
    if scope == "STRATEGY":
        positions = db.scalars(
            select(Position)
            .where(
                Position.user_id == user_id,
                Position.strategy_execution_id == strategy_execution_id,
                Position.status == "open",
            )
            .order_by(Position.id.asc())
        ).all()
    else:  # ACCOUNT
        positions = db.scalars(
            select(Position)
            .where(Position.user_id == user_id, Position.status == "open")
            .order_by(Position.id.asc())
        ).all()

    if not positions:
        result = BulkExitOut(
            execution_id=key,
            scope=scope,  # type: ignore[arg-type]
            status="NO_POSITIONS",
            requested_count=0,
            exited_count=0,
            failed_count=0,
            total_realized_pnl=0.0,
            cash_change=0.0,
            positions=[],
            groups=[],
            errors=[],
        )
        _record_bulk_exit(user_id, scope, strategy_execution_id, key, result, db)
        return result

    # 3. Pre-validation BEFORE any mutation: every position must be open and
    #    resolve to an authoritative market price. Any gap rejects the whole
    #    bulk request — no partial closure from a validation failure.
    missing = [
        p for p in positions if prices.get((p.symbol, p.expiry, p.strike, p.option_type)) is None
    ]
    if missing:
        bad = ", ".join(
            f"{p.symbol} {p.strike:g} {p.option_type.upper()} ({p.expiry})" for p in missing[:5]
        )
        raise PaperExecutionError(
            "BULK_EXIT_CHAIN_DATA_MISSING",
            f"Market data unavailable for {bad}. No position was closed.",
        )
    for p in positions:
        if p.status != "open" or p.net_quantity == 0:
            raise PaperExecutionError(
                "INSUFFICIENT_POSITION",
                f"Position {p.id} is already closed — nothing to exit.",
            )

    # Strategy tags for the grouped outcome (Standalone when no execution).
    exec_ids = {p.strategy_execution_id for p in positions if p.strategy_execution_id}
    tags: dict[str, str] = {}
    if exec_ids:
        rows = db.execute(
            select(StrategyExecution.execution_id, StrategyExecution.strategy_tag).where(
                StrategyExecution.user_id == user_id,
                StrategyExecution.execution_id.in_(exec_ids),
            )
        ).all()
        tags = {r[0]: r[1] or "Custom" for r in rows}

    # 4. Execution phase — ONE transaction for the whole operation. A position
    #    that loses a genuine execution-time race (already closed by another
    #    request) is reported ALREADY_CLOSED; nothing is re-closed.
    outcomes: list[BulkExitPositionOut] = []
    errors: list[str] = []
    exited = 0
    failed = 0
    total_realized = 0.0
    cash_change = 0.0
    for p in positions:
        if p.status != "open" or p.net_quantity == 0:
            # Lost a genuine execution-time race (a concurrent individual
            # exit closed it after pre-validation) — reported, never re-closed.
            failed += 1
            errors.append(
                f"Position {p.id} ({p.symbol} {p.strike:g} {p.option_type.upper()}): "
                "already closed — nothing to exit."
            )
            outcomes.append(
                BulkExitPositionOut(
                    position_id=p.id,
                    symbol=p.symbol,
                    expiry=p.expiry,
                    strike=p.strike,
                    option_type=p.option_type,
                    strategy_execution_id=p.strategy_execution_id,
                    strategy_tag=tags.get(p.strategy_execution_id, "Standalone"),
                    status="ALREADY_CLOSED",
                    error="Position already closed — nothing to exit.",
                )
            )
            continue
        fill_price = prices[(p.symbol, p.expiry, p.strike, p.option_type)]
        per_position = ExitRequestIn(
            client_order_id=f"{key}:pos-{p.id}", quantity=abs(p.net_quantity)
        )
        try:
            result = exit_position(
                user_id, p.id, per_position, db, fill_price, commit=False
            )
        except PaperExecutionError as exc:
            failed += 1
            errors.append(f"Position {p.id} ({p.symbol} {p.strike:g} {p.option_type.upper()}): {exc.message}")
            status = "ALREADY_CLOSED" if exc.code == "INSUFFICIENT_POSITION" else "FAILED"
            outcomes.append(
                BulkExitPositionOut(
                    position_id=p.id,
                    symbol=p.symbol,
                    expiry=p.expiry,
                    strike=p.strike,
                    option_type=p.option_type,
                    strategy_execution_id=p.strategy_execution_id,
                    strategy_tag=tags.get(p.strategy_execution_id, "Standalone"),
                    status=status,
                    error=exc.message,
                )
            )
            continue
        order = result.order
        outcomes.append(
            BulkExitPositionOut(
                position_id=p.id,
                symbol=p.symbol,
                expiry=p.expiry,
                strike=p.strike,
                option_type=p.option_type,
                strategy_execution_id=p.strategy_execution_id,
                strategy_tag=tags.get(p.strategy_execution_id, "Standalone"),
                status="EXITED",
                realized_pnl=order.realized_pnl,
                fill_price=order.fill_price,
            )
        )
        exited += 1
        total_realized += order.realized_pnl or 0.0
        cash_change += cash_flow(
            order.action, order.fill_price, order.filled_quantity, order.lot_size
        )

    if exited == 0:
        final_status = "FAILED"
    elif failed == 0:
        final_status = "SUCCESS"
    else:
        final_status = "PARTIAL"

    result = BulkExitOut(
        execution_id=key,
        scope=scope,  # type: ignore[arg-type]
        status=final_status,  # type: ignore[arg-type]
        requested_count=len(positions),
        exited_count=exited,
        failed_count=failed,
        total_realized_pnl=round(total_realized, 2),
        cash_change=round(cash_change, 2),
        positions=outcomes,
        groups=_group_bulk_outcomes(positions, outcomes, tags),
        errors=errors,
    )
    _record_bulk_exit(user_id, scope, strategy_execution_id, key, result, db)
    return result


def _group_bulk_outcomes(
    positions: list, outcomes: list[BulkExitPositionOut], tags: dict[str, str]
) -> list[BulkExitGroupOut]:
    """Group bulk outcomes by strategy execution (standalone = no execution)."""
    counters: dict[str | None, dict] = {}
    for p in positions:
        eid = p.strategy_execution_id
        g = counters.setdefault(eid, {"requested": 0, "exited": 0, "failed": 0, "realized": 0.0})
        g["requested"] += 1
    for o in outcomes:
        g = counters[o.strategy_execution_id]
        if o.status == "EXITED":
            g["exited"] += 1
            g["realized"] += o.realized_pnl or 0.0
        else:
            g["failed"] += 1

    groups = []
    for eid, g in counters.items():
        if g["failed"] == 0:
            status = "EXITED"
        elif g["exited"] == 0:
            status = "FAILED"
        else:
            status = "PARTIAL"
        groups.append(
            BulkExitGroupOut(
                strategy_execution_id=eid,
                strategy_tag=tags.get(eid, "Standalone"),
                requested=g["requested"],
                exited=g["exited"],
                failed=g["failed"],
                realized_pnl=round(g["realized"], 2),
                status=status,
            )
        )
    return groups


def _record_bulk_exit(
    user_id: str, scope: str, strategy_execution_id: str | None, key: str, result: BulkExitOut, db: Session
) -> None:
    """Persist the bulk-exit result (idempotency record) in the SAME
    transaction as the exits, then commit — the whole operation is atomic."""
    db.add(
        BulkExitRecord(
            user_id=user_id,
            client_order_id=key,
            scope=scope,
            strategy_execution_id=strategy_execution_id,
            status=result.status,
            requested_count=result.requested_count,
            exited_count=result.exited_count,
            failed_count=result.failed_count,
            total_realized_pnl=result.total_realized_pnl,
            cash_change=result.cash_change,
            positions_json=json.dumps([p.model_dump() for p in result.positions]),
            groups_json=json.dumps([g.model_dump() for g in result.groups]),
            errors_json=json.dumps(result.errors),
        )
    )
    db.commit()


def _bulk_exit_replay(record: BulkExitRecord) -> BulkExitOut:
    """Rebuild the ORIGINAL bulk-exit result from its idempotency record."""
    return BulkExitOut(
        execution_id=record.client_order_id,
        scope=record.scope,  # type: ignore[arg-type]
        status=record.status,  # type: ignore[arg-type]
        requested_count=record.requested_count,
        exited_count=record.exited_count,
        failed_count=record.failed_count,
        total_realized_pnl=record.total_realized_pnl,
        cash_change=record.cash_change,
        positions=[BulkExitPositionOut(**d) for d in json.loads(record.positions_json)],
        groups=[BulkExitGroupOut(**d) for d in json.loads(record.groups_json)],
        errors=json.loads(record.errors_json),
        duplicated=True,
    )


# ---- Read models (§24/§25) ---------------------------------------------------


def get_open_positions(user_id: str, db: Session) -> list[dict]:
    # Phase 5.2.1 §7: an ACTIVE position is status == "open" AND
    # net_quantity != 0 — a zero-quantity row is never active (the engine
    # already marks full exits closed; this server-side invariant is the
    # authoritative backstop the frontend mirrors).
    positions = db.scalars(
        select(Position)
        .where(
            Position.user_id == user_id,
            Position.status == "open",
            Position.net_quantity != 0,
        )
        .order_by(Position.opened_at.desc())
    ).all()
    # Unrealized P&L needs a market mark — never fabricated server-side.
    # Phase 5.2.1: attach the displayed strategy name (never "Custom" for a
    # named strategy; only legacy/missing executions fall back).
    return _attach_strategy_tags(db, user_id, [_serialize_position(p) for p in positions])


def get_positions_enriched(
    user_id: str,
    db: Session,
    *,
    status: str | None = None,
    symbol: str | None = None,
    option_type: str | None = None,
    strategy_execution_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    include_closed: bool = False,
) -> list[dict]:
    """Enriched positions with strategy leg exposures, orders, and derived fields.

    Phase 6.6.4: production-grade positions endpoint supporting:
    - status filter: "open" | "closed" | None (all)
    - symbol filter (case-insensitive)
    - option_type filter (call/put)
    - strategy_execution_id filter
    - pagination via limit/offset
    - batched strategy_leg_exposures per position
    - batched entry/exit orders per position
    - derived side (LONG/SHORT) from net_quantity
    - lot_size information (qty / lot_size = lots)
    """
    stmt = select(Position).where(Position.user_id == user_id)
    if status == "open":
        stmt = stmt.where(Position.status == "open", Position.net_quantity != 0)
    elif status == "closed":
        stmt = stmt.where(Position.status == "closed")
    if symbol:
        stmt = stmt.where(Position.symbol == symbol.upper())
    if option_type:
        stmt = stmt.where(Position.option_type == option_type.lower())
    if strategy_execution_id:
        stmt = stmt.where(Position.strategy_execution_id == strategy_execution_id)
    stmt = stmt.order_by(Position.opened_at.desc()).limit(limit).offset(offset)
    positions = list(db.scalars(stmt).all())

    if not positions:
        return []

    position_ids = [p.id for p in positions]

    # Batched: StrategyLegExposures for all positions
    exposure_rows = list(
        db.scalars(
            select(StrategyLegExposure).where(
                StrategyLegExposure.user_id == user_id,
                StrategyLegExposure.position_id.in_(position_ids),
            ).order_by(StrategyLegExposure.id.asc())
        ).all()
    )
    exposures_by_pos: dict[int, list[dict]] = {}
    for exp in exposure_rows:
        exposures_by_pos.setdefault(exp.position_id, []).append({
            "id": exp.id,
            "execution_id": exp.execution_id,
            "action": exp.action,
            "original_quantity": exp.original_quantity,
            "remaining_quantity": exp.remaining_quantity,
            "status": exp.status,
        })

    # Batched: orders for all positions
    order_rows = list(
        db.scalars(
            select(PaperOrder).where(
                PaperOrder.user_id == user_id,
                PaperOrder.position_id.in_(position_ids),
            ).order_by(PaperOrder.id.asc())
        ).all()
    )
    orders_by_pos: dict[int, list[dict]] = {}
    for o in order_rows:
        orders_by_pos.setdefault(o.position_id, []).append(_serialize_order(o))

    # StrategyExecution tags
    exec_ids = {p.strategy_execution_id for p in positions if p.strategy_execution_id}
    tags: dict[str, str] = {}
    if exec_ids:
        rows = db.execute(
            select(StrategyExecution.execution_id, StrategyExecution.strategy_tag).where(
                StrategyExecution.user_id == user_id,
                StrategyExecution.execution_id.in_(exec_ids),
            )
        ).all()
        tags = {r[0]: r[1] or "Custom" for r in rows}

    result = []
    for p in positions:
        d = _serialize_position(p)
        d["strategy_tag"] = tags.get(p.strategy_execution_id, "Custom")
        d["side"] = "LONG" if p.net_quantity > 0 else "SHORT" if p.net_quantity < 0 else "CLOSED"
        d["strategy_leg_exposures"] = exposures_by_pos.get(p.id, [])
        d["orders"] = orders_by_pos.get(p.id, [])
        result.append(d)
    return result


def get_order_history(
    user_id: str,
    db: Session,
    *,
    status: str | None = None,
    symbol: str | None = None,
    action: str | None = None,
    option_type: str | None = None,
    kind: str | None = None,
    strategy_execution_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """Return the user's paper order history with optional server-side filters.

    ``status`` is uppercase (PENDING, FILLED, REJECTED, etc.).
    ``symbol`` is case-insensitive.
    ``action`` is lowercase (buy, sell).
    ``option_type`` is lowercase (call, put).
    ``kind`` is lowercase (entry, exit).
    ``strategy_execution_id`` filters to one strategy execution.
    ``limit`` / ``offset`` bound the result set (default 200 most recent).

    Backward-compatible: when no filters are passed the behaviour is
    identical to the previous implementation.
    """
    stmt = select(PaperOrder).where(PaperOrder.user_id == user_id)
    if status:
        stmt = stmt.where(PaperOrder.status == status)
    if symbol:
        stmt = stmt.where(PaperOrder.symbol == symbol.upper())
    if action:
        stmt = stmt.where(PaperOrder.action == action.lower())
    if option_type:
        stmt = stmt.where(PaperOrder.option_type == option_type.lower())
    if kind:
        stmt = stmt.where(PaperOrder.kind == kind.lower())
    if strategy_execution_id:
        stmt = stmt.where(PaperOrder.execution_id == strategy_execution_id)
    stmt = stmt.order_by(PaperOrder.created_at.desc()).limit(limit).offset(offset)
    orders = list(db.scalars(stmt).all())
    rows = [_serialize_order(o) for o in orders]
    # Attach strategy_tag in one batched lookup
    exec_ids = {o.get("execution_id") for o in rows if o.get("execution_id")}
    if exec_ids:
        tag_map = dict(
            db.execute(
                select(StrategyExecution.execution_id, StrategyExecution.strategy_tag).where(
                    StrategyExecution.user_id == user_id,
                    StrategyExecution.execution_id.in_(exec_ids),
                )
            ).all()
        )
        for o in rows:
            eid = o.get("execution_id")
            o["strategy_tag"] = tag_map.get(eid) or "Custom"
            o["strategy_execution_id"] = eid
    else:
        for o in rows:
            o["strategy_tag"] = "Custom"
            o["strategy_execution_id"] = o.get("execution_id")
    return rows


def get_portfolio(user_id: str, db: Session) -> PortfolioOut:
    account = db.scalar(select(PaperAccount).where(PaperAccount.user_id == user_id))
    starting = account.starting_capital if account else DEFAULT_STARTING_CAPITAL

    txn_sum = db.scalar(
        select(func.coalesce(func.sum(PaperTransaction.amount), 0.0)).where(
            PaperTransaction.user_id == user_id
        )
    ) or 0.0

    positions = list(db.scalars(select(Position).where(Position.user_id == user_id)).all())
    realized = round(sum(p.realized_pnl for p in positions), 2)
    open_positions = [p for p in positions if p.status == "open"]
    invested = round(
        sum(p.average_entry_price * abs(p.net_quantity) * p.lot_size for p in open_positions), 2
    )
    open_execs = {p.strategy_execution_id for p in open_positions if p.strategy_execution_id}

    summary = PortfolioSummaryOut(
        starting_cash=round(starting, 2),
        available_cash=round(starting + txn_sum, 2),
        invested_value=invested,
        realized_pnl=realized,
        unrealized_pnl=None,  # requires market marks (the UI applies them)
        total_pnl=realized,
        open_position_count=len(open_positions),
        open_strategy_count=len(open_execs),
    )
    return PortfolioOut(summary=summary, groups=get_portfolio_groups(user_id, db))


def get_portfolio_groups(user_id: str, db: Session) -> list[PortfolioGroupOut]:
    """Strategy-grouped view: every execution with its orders and P&L."""
    executions = db.scalars(
        select(StrategyExecution)
        .where(StrategyExecution.user_id == user_id)
        .order_by(StrategyExecution.entry_at.desc())
    ).all()
    positions = list(db.scalars(select(Position).where(Position.user_id == user_id)).all())
    orders = list(db.scalars(select(PaperOrder).where(PaperOrder.user_id == user_id)).all())

    groups = []
    for ex in executions:
        ex_orders = [o for o in orders if o.execution_id == ex.execution_id]
        ex_positions = [p for p in positions if p.strategy_execution_id == ex.execution_id]
        realized = round(sum(p.realized_pnl for p in ex_positions), 2)
        groups.append(
            PortfolioGroupOut(
                execution_id=ex.execution_id,
                strategy_tag=ex.strategy_tag,
                symbol=ex.symbol,
                status=ex.status,
                entry_net=ex.entry_net,
                realized_pnl=realized,
                legs=[_serialize_order(o) for o in ex_orders],
                entry_at=ex.entry_at,
            )
        )
    return groups


def reconcile(user_id: str, db: Session) -> ReconcileOut:
    """Validate that orders, positions, cash and executions agree.

    Returns structured discrepancies; never silently repairs data.
    """
    discrepancies: list[dict] = []
    positions = list(db.scalars(select(Position).where(Position.user_id == user_id)).all())
    orders = list(db.scalars(select(PaperOrder).where(PaperOrder.user_id == user_id)).all())
    executions = list(
        db.scalars(select(StrategyExecution).where(StrategyExecution.user_id == user_id)).all()
    )

    # 1. Position quantities agree with fills; 2. realized agrees with exits.
    for p in positions:
        instr_orders = [
            o for o in orders
            if o.symbol == p.symbol and o.expiry == p.expiry
            and o.strike == p.strike and o.option_type == p.option_type
        ]
        expected_net = sum(
            o.filled_quantity if o.action == "buy" else -o.filled_quantity for o in instr_orders
        )
        if expected_net != p.net_quantity:
            discrepancies.append(
                {
                    "type": "POSITION_QUANTITY_MISMATCH",
                    "position_id": p.id,
                    "expected": expected_net,
                    "actual": p.net_quantity,
                }
            )
        expected_realized = round(sum(o.realized_pnl or 0.0 for o in instr_orders if o.kind == "exit"), 2)
        if abs(expected_realized - p.realized_pnl) > 0.005:
            discrepancies.append(
                {
                    "type": "REALIZED_PNL_MISMATCH",
                    "position_id": p.id,
                    "expected": expected_realized,
                    "actual": p.realized_pnl,
                }
            )

    # 3. Cash agrees with the recorded ledger: the sum of ledger amounts must
    # equal the sum of the cash flows of every filled order (catches missing
    # or double-written transactions).
    txn_sum = db.scalar(
        select(func.coalesce(func.sum(PaperTransaction.amount), 0.0)).where(
            PaperTransaction.user_id == user_id
        )
    ) or 0.0
    expected_cash_flow = round(
        sum(
            cash_flow(o.action, o.fill_price, o.filled_quantity, o.lot_size)
            for o in orders
            if o.status == "FILLED" and o.fill_price is not None
        ),
        2,
    )
    if abs(round(txn_sum, 2) - expected_cash_flow) > 0.005:
        discrepancies.append(
            {
                "type": "CASH_MISMATCH",
                "expected": expected_cash_flow,
                "actual": round(txn_sum, 2),
            }
        )

    # 4. No duplicate executions.
    if len({e.execution_id for e in executions}) != len(executions):
        discrepancies.append({"type": "DUPLICATE_EXECUTION_ID", "expected": len(executions)})
    if len({e.client_order_id for e in executions}) != len(executions):
        discrepancies.append({"type": "DUPLICATE_CLIENT_ORDER_ID", "expected": len(executions)})
    if len({o.client_order_id for o in orders}) != len(orders):
        discrepancies.append({"type": "DUPLICATE_ORDER_CLIENT_ORDER_ID", "expected": len(orders)})

    return ReconcileOut(valid=not discrepancies, discrepancies=discrepancies)


def reset_portfolio(user_id: str, db: Session) -> PortfolioOut:
    """Clear the user's authoritative paper state (keeps the account record)."""
    from sqlalchemy import delete

    for model in (PaperOrder, Position, PaperTransaction, StrategyExecution):
        db.execute(delete(model).where(model.user_id == user_id))
    trades = db.scalars(select(Trade).where(Trade.user_id == user_id)).all()
    for t in trades:
        for leg in list(t.legs):
            db.delete(leg)
        db.delete(t)
    db.commit()
    return get_portfolio(user_id, db)


def _execution_out(execution: StrategyExecution, db: Session, duplicated: bool) -> ExecutionOut:
    orders = list(
        db.scalars(
            select(PaperOrder)
            .where(PaperOrder.execution_id == execution.execution_id)
            .order_by(PaperOrder.id.asc())
        ).all()
    )
    filled = sum(1 for o in orders if o.status == "FILLED")
    return ExecutionOut(
        execution_id=execution.execution_id,
        status=execution.status,
        symbol=execution.symbol,
        strategy_tag=execution.strategy_tag,
        entry_net=execution.entry_net,
        orders=[_serialize_order(o) for o in orders],
        filled_count=filled,
        failed_count=len(orders) - filled,
        errors=[],
        duplicated=duplicated,
    )
