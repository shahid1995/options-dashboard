"""Strategy-leg attribution (Phase 6.5.0.1).

The netted ``Position`` remains the authoritative portfolio exposure. This
service maintains ``StrategyLegExposure`` rows — one per FILLED entry
order — that preserve WHICH execution owns HOW MUCH of a position, so
future strategy-scoped exits can target BUY CE / SELL CE / BUY PE /
SELL PE / individual legs without deriving side from ``sign(net_quantity)``
(which is impossible once multiple executions trade one instrument).

Rules
-----
- ``action`` is the strategy-leg action as executed, never derived from the
  position sign.
- Reconciliation invariant: for every position, the signed sum of its
  exposures' remaining_quantity (buy = +, sell = −) equals the position's
  net_quantity.
- An exit reduces the position's DOMINANT side — the side whose quantity
  actually shrinks: a net-long position reduces buy-action exposures, a
  net-short position reduces sell-action exposures — deterministically FIFO
  by exposure id (creation order, matching the journal FIFO convention).
- The system never executes more quantity than the position supports:
  ``allocate_exit`` rejects quantity > abs(net_quantity), and rejects any
  allocation the exposure ledger cannot cover (corrupt / stale rows) instead
  of guessing.
- Positions whose exposure ledger does not reconcile (legacy rows created
  before this phase, or mixed legacy + new attribution) simply skip
  attribution maintenance: the position engine is authoritative either way
  and exits are never blocked or altered.

Pure functions (``allocate_exit``, ``reconcile_position_exposures``) are
deterministic and side-effect free; the DB helpers are the only writers.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PaperOrder, Position, StrategyLegExposure

EXPOSURE_STATUSES = frozenset({"open", "closed"})


class LegExposureError(Exception):
    """A structured attribution failure.

    ``code`` is INSUFFICIENT_POSITION_CAPACITY or INSUFFICIENT_EXPOSURE_CAPACITY;
    ``message`` is human-readable.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---- Creation ---------------------------------------------------------------


def create_exposures_for_orders(
    db: Session,
    user_id: str,
    execution_id: str,
    orders: list[PaperOrder],
    now: datetime,
) -> list[StrategyLegExposure]:
    """Create one attribution row per FILLED entry order (idempotent).

    ``orders`` must already have ``position_id`` assigned. Rows are unique
    per ``(user_id, order_id)`` — replaying an execution (or re-running
    backfill) never duplicates attribution. Returns ONLY the rows newly
    created (existing rows are left untouched and not counted).
    """
    created: list[StrategyLegExposure] = []
    for order in orders:
        existing = db.scalar(
            select(StrategyLegExposure).where(
                StrategyLegExposure.user_id == user_id,
                StrategyLegExposure.order_id == order.id,
            )
        )
        if existing is not None:
            continue
        exposure = StrategyLegExposure(
            user_id=user_id,
            execution_id=execution_id,
            position_id=order.position_id,
            order_id=order.id,
            symbol=order.symbol,
            expiry=order.expiry,
            strike=order.strike,
            option_type=order.option_type,
            action=order.action,
            original_quantity=order.quantity,
            remaining_quantity=order.quantity,
            status="open",
            created_at=now,
            updated_at=now,
        )
        db.add(exposure)
        db.flush()
        created.append(exposure)
    return created


# ---- Read helpers -----------------------------------------------------------


def exposures_for_position(db: Session, user_id: str, position_id: int) -> list[StrategyLegExposure]:
    """All attribution rows for one netted position, deterministic order."""
    return list(
        db.scalars(
            select(StrategyLegExposure)
            .where(
                StrategyLegExposure.user_id == user_id,
                StrategyLegExposure.position_id == position_id,
            )
            .order_by(StrategyLegExposure.id.asc())
        ).all()
    )


def exposures_for_execution(db: Session, user_id: str, execution_id: str) -> list[StrategyLegExposure]:
    """All attribution rows of one strategy execution, deterministic order."""
    return list(
        db.scalars(
            select(StrategyLegExposure)
            .where(
                StrategyLegExposure.user_id == user_id,
                StrategyLegExposure.execution_id == execution_id,
            )
            .order_by(StrategyLegExposure.id.asc())
        ).all()
    )


# ---- Pure allocation / reconciliation --------------------------------------


def reconcile_position_exposures(position: Position, exposures: list[StrategyLegExposure]) -> dict:
    """Pure: does the exposure ledger match the netted position?

    Returns ``status`` OK when the signed sum of remaining_quantity
    (buy = +, sell = −) equals ``position.net_quantity``, else MISMATCH.
    """
    signed_sum = sum(
        e.remaining_quantity if e.action == "buy" else -e.remaining_quantity
        for e in exposures
    )
    return {
        "position_id": position.id,
        "net_quantity": position.net_quantity,
        "signed_exposure_sum": signed_sum,
        "status": "OK" if signed_sum == position.net_quantity else "MISMATCH",
    }


def allocate_exit(
    exposures: list[StrategyLegExposure],
    prior_net_quantity: int,
    quantity: int,
) -> list[tuple[StrategyLegExposure, int]]:
    """Pure: deterministic FIFO allocation of an exit across exposures.

    ``prior_net_quantity`` is the position's signed net BEFORE the exit
    (the side being reduced). Reduces the position's dominant side — long →
    buy-action legs, short → sell-action legs — in exposure-id order, each
    capped at its own remaining_quantity.

    Raises ``LegExposureError``:
    - INSUFFICIENT_POSITION_CAPACITY when ``quantity`` exceeds what the
      actual position supports (abs of the pre-exit net);
    - INSUFFICIENT_EXPOSURE_CAPACITY when the dominant-side ledger cannot
      cover the quantity (stale/corrupt attribution — never guessed).

    Never mutates anything; returns ``[(exposure, lots), ...]`` summing to
    ``quantity``.
    """
    if quantity <= 0:
        raise LegExposureError("INVALID_QUANTITY", "Exit quantity must be positive.")
    if quantity > abs(prior_net_quantity):
        raise LegExposureError(
            "INSUFFICIENT_POSITION_CAPACITY",
            f"Position only supports {abs(prior_net_quantity)} lot(s) of exit; "
            f"requested {quantity}.",
        )
    side = "buy" if prior_net_quantity > 0 else "sell"
    candidates = [
        e
        for e in exposures
        if e.action == side and e.status == "open" and e.remaining_quantity > 0
    ]
    candidates.sort(key=lambda e: e.id)  # deterministic FIFO regardless of input order
    available = sum(e.remaining_quantity for e in candidates)
    if available < quantity:
        raise LegExposureError(
            "INSUFFICIENT_EXPOSURE_CAPACITY",
            f"Only {available} lot(s) of {side}-side attribution available for "
            f"this position; requested {quantity}.",
        )
    allocations: list[tuple[StrategyLegExposure, int]] = []
    remaining = quantity
    for exposure in candidates:
        if remaining <= 0:
            break
        take = min(exposure.remaining_quantity, remaining)
        allocations.append((exposure, take))
        remaining -= take
    return allocations


# ---- Exit maintenance -------------------------------------------------------


def apply_exit_allocations(
    db: Session,
    allocations: list[tuple[StrategyLegExposure, int]],
    now: datetime,
    *,
    exit_order_id: int | None = None,
    user_id: str | None = None,
) -> None:
    """Persist an exit's exposure decrements (same transaction as the fill).

    When ``exit_order_id`` and ``user_id`` are provided, also persists
    ``ExitExposureAllocation`` rows so analytics can unambiguously
    attribute each exit to the exposures it reduced.
    """
    from app.models import ExitExposureAllocation

    for exposure, take in allocations:
        exposure.remaining_quantity -= take
        if exposure.remaining_quantity == 0:
            exposure.status = "closed"
        exposure.updated_at = now
        if exit_order_id is not None and user_id is not None:
            db.add(ExitExposureAllocation(
                user_id=user_id,
                exit_order_id=exit_order_id,
                exposure_id=exposure.id,
                quantity=take,
            ))
    db.flush()


def maintain_exposure_on_exit(
    db: Session,
    user_id: str,
    position: Position,
    prior_net_quantity: int,
    quantity: int,
    now: datetime,
    *,
    target_exposure_id: int | None = None,
    exit_order_id: int | None = None,
) -> bool:
    """Best-effort attribution maintenance after an exit fill.

    Returns True when attribution was updated, False when the position's
    exposure ledger does not reconcile (legacy / mixed data) or the
    allocation is impossible — in every case the exit itself is already
    authoritative and never blocked or altered.

    ``target_exposure_id``: when provided (Phase 6.6.5), reduces THIS
    specific StrategyLegExposure instead of FIFO across the dominant side.
    This ensures a leg-aware exit targets the exact exposure the user
    selected (e.g. Strategy B's BUY CE), not the first available exposure
    on the dominant side (which might belong to Strategy A).
    """
    exposures = exposures_for_position(db, user_id, position.id)
    if not exposures:
        return False
    # Reconcile against the PRE-exit net: the ledger still reflects the
    # pre-exit state at this point (the fill already mutated the position),
    # so the invariant to verify is ``signed_sum == prior_net_quantity``.
    signed = reconcile_position_exposures(position, exposures)["signed_exposure_sum"]
    if signed != prior_net_quantity:
        return False

    # Phase 6.6.5: targeted exposure allocation when target_exposure_id is provided.
    if target_exposure_id is not None:
        target = None
        for e in exposures:
            if e.id == target_exposure_id:
                target = e
                break
        if target is None or target.user_id != user_id:
            return False
        if target.status != "open" or target.remaining_quantity <= 0:
            return False
        if quantity > target.remaining_quantity:
            return False
        # Direct allocation: reduce the specific exposure.
        apply_exit_allocations(db, [(target, quantity)], now,
                               exit_order_id=exit_order_id, user_id=user_id)
        return True

    try:
        allocations = allocate_exit(exposures, prior_net_quantity, quantity)
    except LegExposureError:
        return False
    apply_exit_allocations(db, allocations, now,
                           exit_order_id=exit_order_id, user_id=user_id)
    return True


# ---- Conservative backfill (existing databases) -----------------------------


def backfill_exposures(db: Session, user_id: str) -> int:
    """Create attribution rows for PRE-EXISTING unambiguous executions.

    Existing data created before Phase 6.5.0.1 has no exposure rows, and
    per-execution remaining quantities can NOT be reconstructed in general.
    This backfill only creates rows where correctness is PROVABLE:

    - the position carries a ``strategy_execution_id``;
    - EVERY FILLED entry order for the position's instrument belongs to
      that same execution (no multi-execution sharing, no opposing fills);
    - the instrument has never been exited (no exit orders), so
      ``remaining = original`` is exact.

    Anything else is skipped (the exposure ledger for it stays empty and
    ``maintain_exposure_on_exit`` leaves it untouched — safe). Idempotent:
    rows already present for an order are never duplicated. Returns the
    number of rows created.
    """
    created = 0
    positions = list(
        db.scalars(
            select(Position).where(
                Position.user_id == user_id,
                Position.strategy_execution_id.isnot(None),
            )
        ).all()
    )
    for position in positions:
        entry_orders = list(
            db.scalars(
                select(PaperOrder)
                .where(
                    PaperOrder.user_id == user_id,
                    PaperOrder.kind == "entry",
                    PaperOrder.symbol == position.symbol,
                    PaperOrder.expiry == position.expiry,
                    PaperOrder.strike == position.strike,
                    PaperOrder.option_type == position.option_type,
                    PaperOrder.status == "FILLED",
                )
                .order_by(PaperOrder.id.asc())
            ).all()
        )
        if not entry_orders:
            continue
        executions = {o.execution_id for o in entry_orders}
        if executions != {position.strategy_execution_id}:
            continue  # shared instrument — remaining per execution unknowable
        has_exit = db.scalar(
            select(PaperOrder.id)
            .where(
                PaperOrder.user_id == user_id,
                PaperOrder.kind == "exit",
                PaperOrder.symbol == position.symbol,
                PaperOrder.expiry == position.expiry,
                PaperOrder.strike == position.strike,
                PaperOrder.option_type == position.option_type,
            )
            .limit(1)
        )
        if has_exit is not None:
            continue  # partially exited — remaining unknowable
        new_rows = create_exposures_for_orders(
            db, user_id, position.strategy_execution_id, entry_orders, position.updated_at or position.opened_at
        )
        created += len(new_rows)
    # The transaction boundary belongs to the caller (ADR §7.3). On PostgreSQL
    # the startup path wraps each user in one protected transaction whose first
    # statement is lease entry and whose last statement before COMMIT refreshes
    # the lease; a commit here would break that contract.
    db.flush()
    return created


def backfill_all_exposures(db: Session) -> int:
    """Run the conservative backfill for every user (SQLite startup path).

    Commits once per user. The PostgreSQL startup path does not use this
    helper: it drives the same ``backfill_exposures`` per-user function inside
    lease-protected transactions (see ``app.db.run_startup_backfill``).
    """
    user_ids = list(db.scalars(select(Position.user_id).distinct()).all())
    total = 0
    for user_id in user_ids:
        total += backfill_exposures(db, user_id)
        db.commit()
    return total
