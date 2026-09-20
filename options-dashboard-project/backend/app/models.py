from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaperAccount(Base):
    """Per-user simulated account (starting capital) backing the journal."""

    __tablename__ = "paper_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    starting_capital: Mapped[float] = mapped_column(Float, default=500000)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class Trade(Base):
    """A paper trade: one strategy execution, made up of one or more legs.

    Phase 5.0: the authoritative execution layer lives in
    ``StrategyExecution`` / ``PaperOrder`` / ``Position`` /
    ``PaperTransaction``; this table remains the user-facing JOURNAL record
    (account stats, trade log). Every journal record created by the
    authoritative engine carries the same ``strategy_execution_id`` as its
    execution so journal and portfolio stay reconcilable, and
    ``client_order_id`` guards against duplicate journal writes on retries.
    """

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(16))
    strategy_tag: Mapped[str] = mapped_column(String(64), default="Custom")
    status: Mapped[str] = mapped_column(String(8), default="open")  # open | closed
    # Net entry money flow in rupees (negative = net credit received, e.g. a
    # short vertical spread; positive = net debit paid, e.g. a long spread).
    entry_net: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    exit_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    # Phase 5.0 linkage to the authoritative execution layer (nullable so the
    # legacy journal path keeps working unchanged). Columns added to existing
    # databases via the Alembic baseline migration (Phase 10.1A/B).
    strategy_execution_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    client_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    legs: Mapped[list["Leg"]] = relationship(
        back_populates="trade", cascade="all, delete-orphan", order_by="Leg.id"
    )


class Leg(Base):
    """One option leg of a paper trade."""

    __tablename__ = "legs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16))
    expiration_date: Mapped[str] = mapped_column(String(10))
    strike_price: Mapped[float] = mapped_column(Float)
    option_type: Mapped[str] = mapped_column(String(8))  # call | put
    action: Mapped[str] = mapped_column(String(8))  # buy | sell
    premium: Mapped[float] = mapped_column(Float)  # simulated premium per unit
    quantity: Mapped[int] = mapped_column(Integer)  # number of lots
    lot_size: Mapped[int] = mapped_column(Integer)  # contracts per lot
    entry_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    exit_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)

    trade: Mapped[Trade] = relationship(back_populates="legs")


# ---- Phase 5.0: server-authoritative paper trading domain ----------------
#
# The backend is the single source of truth for orders, fills, positions,
# cash and P&L. The four tables below are the authoritative layer; the
# legacy ``Trade``/``Leg`` journal rows are written by the same engine (and
# carry the same ``strategy_execution_id``) so the journal UI keeps working.
# Quantities are LOTS everywhere; rupee exposure scales by lot_size.


class StrategyExecution(Base):
    """One strategy execution: a grouped multi-leg paper trade.

    ``client_order_id`` is the idempotency key at the execution boundary:
    the unique per-user constraint means a retried request can never create
    a second execution, a second set of orders, double-counted cash or
    duplicate journal records.

    ``status`` uses the execution states PENDING / FILLED / PARTIAL /
    FAILED / CANCELLED. The current engine pre-validates everything (market
    gate, chain data, prices) before writing, so it executes atomically:
    a successful request is FILLED with every order filled; any failure
    writes nothing (no misleading "fully executed" results).
    """

    __tablename__ = "strategy_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    execution_id: Mapped[str] = mapped_column(String(40), index=True)
    client_order_id: Mapped[str] = mapped_column(String(64))
    strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy_tag: Mapped[str] = mapped_column(String(64), default="Custom")
    symbol: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(12), default="PENDING")
    # Net entry money flow in rupees — same convention as ``Trade.entry_net``:
    # positive = net debit paid, negative = net credit received.
    entry_net: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    exit_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    # Phase 6.10: V2 execution audit trail (JSON in Text for SQLite compat)
    execution_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Phase 7.0: trade annotations
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array of strings
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (UniqueConstraint("user_id", "client_order_id", name="uq_execution_client_order"),)


class PaperOrder(Base):
    """One paper order (one leg of an entry, or one exit fill).

    Status lifecycle: PENDING → FILLED / PARTIALLY_FILLED / CANCELLED /
    REJECTED. The current engine fills atomically (PENDING → FILLED), but the
    full state model and transition validator exist so future async/partial
    fills cannot violate the lifecycle.

    ``client_order_id`` is the per-order idempotency key (unique per user);
    exits reuse it so duplicate exit requests return the original result.
    """

    __tablename__ = "paper_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    client_order_id: Mapped[str] = mapped_column(String(64))
    execution_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # entry (part of a strategy execution) | exit (closes a position)
    kind: Mapped[str] = mapped_column(String(8), default="entry")
    symbol: Mapped[str] = mapped_column(String(16))
    expiry: Mapped[str] = mapped_column(String(10))
    strike: Mapped[float] = mapped_column(Float)
    option_type: Mapped[str] = mapped_column(String(8))  # call | put
    action: Mapped[str] = mapped_column(String(8))  # buy | sell (fill direction)
    quantity: Mapped[int] = mapped_column(Integer)  # lots
    lot_size: Mapped[int] = mapped_column(Integer)  # contracts per lot
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)  # lots filled
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_source: Mapped[str] = mapped_column(String(16), default="market")
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)  # exits only
    rejected_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    journal_leg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # legacy Leg row
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (UniqueConstraint("user_id", "client_order_id", name="uq_order_client_order"),)


class Position(Base):
    """Netted paper position for one tradable instrument.

    Identity: user + symbol + expiry + strike + option_type (unique).
    ``net_quantity`` is signed LOTS: BUY = +, SELL = −. Same-direction fills
    update the weighted-average entry price; opposite-direction fills realize
    P&L against that average. A zero net quantity marks the position CLOSED
    but keeps the record queryable (history is never silently overwritten).
    """

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(16))
    expiry: Mapped[str] = mapped_column(String(10))
    strike: Mapped[float] = mapped_column(Float)
    option_type: Mapped[str] = mapped_column(String(8))  # call | put
    net_quantity: Mapped[int] = mapped_column(Integer, default=0)  # signed lots
    average_entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    lot_size: Mapped[int] = mapped_column(Integer, default=0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)  # rupees
    status: Mapped[str] = mapped_column(String(8), default="open")  # open | closed
    strategy_execution_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "symbol", "expiry", "strike", "option_type", name="uq_position_instrument"
        ),
    )


class PaperTransaction(Base):
    """Auditable cash-ledger record for every cash-affecting paper execution.

    ``amount`` is the signed rupee change applied to available cash
    (buy pays out = negative, sell receives = positive). Available cash is
    derived as ``starting_capital + SUM(amount)`` — the ledger is the only
    writer, so cash can always be reconciled to recorded transactions.
    """

    __tablename__ = "paper_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    execution_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    type: Mapped[str] = mapped_column(String(20))  # ENTRY_DEBIT | ENTRY_CREDIT | EXIT_DEBIT | EXIT_CREDIT
    amount: Mapped[float] = mapped_column(Float)  # signed rupees applied to cash
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class StrategyLegExposure(Base):
    """Per-execution strategy-leg attribution (Phase 6.5.0.1).

    The netted ``Position`` remains the AUTHORITATIVE portfolio exposure
    (one row per instrument, signed ``net_quantity``). Because multiple
    executions can trade the same instrument, the position alone cannot
    answer "how much of WHICH execution's leg is still open" — this table
    preserves exactly that attribution so future strategy-scoped exits can
    target BUY CE / SELL CE / BUY PE / SELL PE / individual legs without
    guessing.

    One row per FILLED entry ``PaperOrder`` (unique per ``order_id``).
    ``action`` is the strategy-leg action as executed — NEVER derived from
    ``Position.net_quantity``. ``original_quantity`` / ``remaining_quantity``
    are LOTS.

    Reconciliation invariant: for every position, the signed sum of its
    exposures' remaining_quantity (buy = +, sell = −) equals the position's
    net_quantity. Exits reduce the position's dominant side (long →
    buy-action legs, short → sell-action legs) deterministically FIFO so the
    invariant holds; when it cannot be maintained, exits fail safely or
    leave attribution untouched — the position engine is authoritative
    either way. No LTP / average entry price / realized P&L / cash / margin
    is duplicated here — those stay owned by the execution / position /
    accounting layer.
    """

    __tablename__ = "strategy_leg_exposures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    execution_id: Mapped[str] = mapped_column(String(40), index=True)
    position_id: Mapped[int] = mapped_column(Integer, index=True)  # netted positions.id
    order_id: Mapped[int] = mapped_column(Integer)  # source entry PaperOrder.id
    symbol: Mapped[str] = mapped_column(String(16))
    expiry: Mapped[str] = mapped_column(String(10))
    strike: Mapped[float] = mapped_column(Float)
    option_type: Mapped[str] = mapped_column(String(8))  # call | put
    action: Mapped[str] = mapped_column(String(8))  # buy | sell (strategy leg action)
    original_quantity: Mapped[int] = mapped_column(Integer)  # lots
    remaining_quantity: Mapped[int] = mapped_column(Integer)  # lots still attributed open
    status: Mapped[str] = mapped_column(String(8), default="open")  # open | closed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "order_id", name="uq_exposure_source_order"),
    )


class ExitExposureAllocation(Base):
    """Junction table: maps exit orders to the exposure reductions they caused.

    One exit can reduce multiple exposures (FIFO spanning exposures).
    One exposure can be reduced by multiple partial exits.
    This is a many-to-many relationship.

    ``exit_order_id`` → PaperOrder (exit, kind='exit')
    ``exposure_id``   → StrategyLegExposure (the entry exposure reduced)
    ``quantity``      → lots removed from the exposure by this exit

    Written in the SAME transaction as the exit fill by
    ``apply_exit_allocations()``.  Historical exits (before Phase 7.2A)
    have no allocation rows — analytics falls back to the legacy dict
    lookup for those.
    """

    __tablename__ = "exit_exposure_allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    exit_order_id: Mapped[int] = mapped_column(Integer, index=True)
    exposure_id: Mapped[int] = mapped_column(Integer, index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class BulkExitRecord(Base):
    """Idempotency record for ONE bulk exit operation (Phase 5.2).

    Bulk exits (EXIT STRATEGY / EXIT ALL) are atomic operations that can
    close many positions at once. The same ``client_order_id`` is never
    executed twice: the FIRST result is stored here (positions, groups,
    totals) and every replay returns the ORIGINAL result with
    ``duplicated=True`` — no second exit orders, no double cash-ledger
    entries, no duplicate journal records.

    ``scope`` is STRATEGY (one strategy execution) or ACCOUNT (every open
    position of the user).
    """

    __tablename__ = "bulk_exit_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    client_order_id: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(12))  # STRATEGY | ACCOUNT
    strategy_execution_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(12))  # SUCCESS | NO_POSITIONS | FAILED | PARTIAL
    requested_count: Mapped[int] = mapped_column(Integer, default=0)
    exited_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    total_realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    cash_change: Mapped[float] = mapped_column(Float, default=0.0)
    positions_json: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of position outcomes
    groups_json: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of group outcomes
    errors_json: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of error strings
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (UniqueConstraint("user_id", "client_order_id", name="uq_bulk_exit_client_order"),)


class StrategyTemplate(Base):
    """A user-created reusable strategy template (Phase 6.7).

    Stores the named leg configuration so the user can save, edit, duplicate,
    and re-use custom strategies without re-building them from scratch.

    ``user_id`` enforces strict ownership — one user can never see or modify
    another user's templates. Editing, renaming, duplicating or deleting a
    template NEVER affects historical executions, positions, exposures,
    orders, journal or P&L because there is NO foreign key from
    ``StrategyExecution`` to ``StrategyTemplate`` — the template is a
    reusable blueprint, not a live reference.
    """

    __tablename__ = "strategy_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(128))
    symbol: Mapped[str] = mapped_column(String(16), default="NIFTY")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    legs: Mapped[list["StrategyTemplateLeg"]] = relationship(
        back_populates="template", cascade="all, delete-orphan", order_by="StrategyTemplateLeg.position"
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_template_user_name"),
    )


class StrategyTemplateLeg(Base):
    """One leg of a user-created strategy template (Phase 6.7 / 6.8B).

    Each leg stores the strike/expiry definition for a strategy leg.

    V1 (Phase 6.7 — fixed-leg templates):
        ``strike_mode = "fixed"``, ``strike`` is the absolute strike.
        ``expiry_mode = "fixed"``, ``expiry`` is the YYYY-MM-DD date.
        ``formula_version = 1``.

    V2 (Phase 6.8B — dynamic formula templates):
        ``strike_mode`` selects the strike resolution algorithm.
        ``expiry_mode`` selects the expiry resolution algorithm.
        ``formula_version = 2``.

    ``strike`` and ``expiry`` remain stored even for V2 legs — they hold
    the last-resolved or preview value and serve as backward-compatible
    display fields, but they are NOT authoritative formula input when
    ``strike_mode != "fixed"`` or ``expiry_mode != "fixed"``.

    ``price`` is informational only — never used for execution; the
    authoritative fill price is resolved from the live option chain at
    execution time.
    """

    __tablename__ = "strategy_template_legs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("strategy_templates.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)  # ordering
    action: Mapped[str] = mapped_column(String(8))  # buy | sell
    option_type: Mapped[str] = mapped_column(String(8))  # call | put
    strike: Mapped[float] = mapped_column(Float)  # fixed strike (V1) or last-resolved (V2)
    expiry: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD (V1) or last-resolved (V2)
    quantity: Mapped[int] = mapped_column(Integer, default=1)  # lots
    lot_size: Mapped[int] = mapped_column(Integer, default=50)  # contracts per lot
    price: Mapped[float | None] = mapped_column(Float, nullable=True)  # informational only
    # Phase 6.8B: dynamic formula fields
    strike_mode: Mapped[str] = mapped_column(String(20), default="fixed")  # fixed | atm | atm_offset_steps | atm_offset | spot_offset | delta
    strike_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)  # atm_offset_steps: integer step offset
    strike_offset_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # percentage offset (reserved for future use)
    target_delta: Mapped[float | None] = mapped_column(Float, nullable=True)  # delta mode: target delta value
    expiry_mode: Mapped[str] = mapped_column(String(20), default="fixed")  # fixed | current_week | next_week | monthly | dte_range
    expiry_dte_min: Mapped[int | None] = mapped_column(Integer, nullable=True)  # dte_range mode: minimum days-to-expiry
    expiry_dte_max: Mapped[int | None] = mapped_column(Integer, nullable=True)  # dte_range mode: maximum days-to-expiry
    formula_version: Mapped[int] = mapped_column(Integer, default=1)  # 1 = legacy fixed, 2 = dynamic formula
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    template: Mapped["StrategyTemplate"] = relationship(back_populates="legs")


class GexSnapshot(Base):
    """One stored GEX snapshot (Phase 7.3 data foundation).

    Persists the complete GEX state at one instant so that historical ΔGEX,
    migration, and decomposition can be computed without re-fetching the
    chain.  Every field that was used to compute GEX is preserved so the
    calculation is reproducible.

    ``spot``, ``call_gex``, ``put_gex``, ``net_gex`` are chain-level totals.
    ``strike_data`` is a JSON array containing per-strike broker-observed
    gamma, OI, IV and the resulting GEX — enough to reconstruct canonical
    chain rows and reproduce the exact GEX calculation.

    ``expiry_data`` is a JSON array of per-expiry totals.
    ``methodology_metadata`` records the formula, sign convention, and
    unit contract used at capture time.
    """

    __tablename__ = "gex_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Ownership — Phase 8F: every snapshot belongs to one user-scoped capture
    owner_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    data_source: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "analytics_token" | "broker_oauth"
    # Identity
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    expiry: Mapped[str] = mapped_column(String(10))
    # Market state at capture
    spot: Mapped[float] = mapped_column(Float)
    # Methodology
    methodology: Mapped[str] = mapped_column(String(32), default="GEX_STANDARD_V1")
    sign_convention: Mapped[str] = mapped_column(String(32), default="NAIVE_DEALER_CONVENTION")
    # Chain-level totals (computed at capture time)
    call_gex: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_gex: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_gex: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Data quality
    availability_status: Mapped[str] = mapped_column(String(16))  # available|partial|unavailable|invalid
    valid_strike_count: Mapped[int] = mapped_column(Integer, default=0)
    total_strike_count: Mapped[int] = mapped_column(Integer, default=0)
    # Capture metadata
    chain_age_ms: Mapped[float | None] = mapped_column(Float, nullable=True)  # ms since broker quote
    captured_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # Strike-level detail (JSON for SQLite compat)
    strike_data: Mapped[str] = mapped_column(Text, default="[]")
    # Expiry-level detail (JSON)
    expiry_data: Mapped[str] = mapped_column(Text, default="[]")
    # Methodology metadata (JSON)
    methodology_metadata: Mapped[str] = mapped_column(Text, default="{}")
    # Phase 7.2 sweep enrichment (JSON, nullable)
    sweep_data: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)


class IVObservation(Base):
    """One stored implied-volatility observation (Phase 4.1 data foundation).

    Nothing records observations yet — the collection job is deliberately NOT
    implemented in Phase 4.1 (no uncontrolled database growth). This table and
    the service in ``app/services/iv_history.py`` are the persistence
    interfaces a FUTURE collector will use, and it must honour the
    configurable sampling interval / retention in ``app/config.py``.

    ``iv`` is stored as a CANONICAL DECIMAL FRACTION (0.1824 = 18.24%) — the
    exact unit contract used by the frontend calculation layer. Never store
    the raw broker percent here.
    """

    __tablename__ = "iv_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    expiry: Mapped[str] = mapped_column(String(10))
    strike: Mapped[float] = mapped_column(Float)
    option_type: Mapped[str] = mapped_column(String(8))  # call | put
    iv: Mapped[float] = mapped_column(Float)  # canonical decimal (0.1824 = 18.24%)
    spot: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="upstox")
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class NiftyCandle(Base):
    """One historical NIFTY OHLCV candle (Phase 7.7 research foundation).

    Stores intraday candle data for research: constructing forward outcomes,
    detecting swing levels, and computing baseline price features.

    ``interval" is the candle granularity (e.g. "3min", "5min", "1day").
    ``open_time" is the candle open timestamp in UTC — the canonical
    identity for deduplication via the unique constraint.
    """

    __tablename__ = "nifty_candles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    interval: Mapped[str] = mapped_column(String(8), default="3min")
    open_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)

    __table_args__ = (
        UniqueConstraint("symbol", "interval", "open_time", name="uq_candle_identity"),
    )


class ContractSpec(Base):
    """Historical contract metadata for an expired option instrument (Phase 7.8).

    Populated from the Upstox Get Expired Option Contracts API.  Each row
    stores the authoritative per-instrument metadata for one specific
    expired contract, identified by ``instrument_key``.

    **Immutability rule:**  Once a valid ``lot_size`` is stored for an
    ``instrument_key``, it is NEVER overwritten — not by a later API
    response, not by the current lot size, not by any inferred value.
    This guarantees reproducibility of any future GEX / exposure
    calculation that consumes this metadata.

    ``lot_size`` and ``minimum_lot`` are stored separately — they may
    differ and must never be assumed equal.

    The candle pipeline (nifty_candles) is completely independent of this
    table.  This table is consumed ONLY by future phases (7.9+) that
    reconstruct historical option chains and compute GEX.
    """

    __tablename__ = "contract_specs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Identity — instrument_key is the unique lookup key
    instrument_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    underlying: Mapped[str] = mapped_column(String(16), index=True)
    underlying_key: Mapped[str] = mapped_column(String(32))
    expiry: Mapped[str] = mapped_column(String(10), index=True)
    strike_price: Mapped[float] = mapped_column(Float)
    instrument_type: Mapped[str] = mapped_column(String(8))  # CE or PE

    # Contract specifications — exact values from Upstox API
    # lot_size: authoritative historical value, nullable to represent unknown
    lot_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimum_lot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    freeze_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tick_size: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Descriptive metadata
    trading_symbol: Mapped[str] = mapped_column(String(64))
    segment: Mapped[str] = mapped_column(String(16))
    exchange: Mapped[str] = mapped_column(String(8))
    weekly: Mapped[bool] = mapped_column(default=False)

    # Provenance — every row traces back to its source
    source: Mapped[str] = mapped_column(String(32))
    source_reference: Mapped[str] = mapped_column(String(255))
    fetched_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("instrument_key", name="uq_contract_spec_key"),
    )


class OptionCandle(Base):
    """One historical OHLCV candle for an expired option/future contract (Phase 7.13).

    Stores intraday candle data for individual expired instruments.
    Populated from the Upstox Expired Historical Candle Data API.

    ``instrument_key`` is the canonical Upstox identity (e.g.
    ``NSE_FO|48891|31-10-2024``) and links to ``contract_specs``
    for metadata (lot_size, strike, CE/PE, etc.).

    **Identity:** (instrument_key, interval, open_time) uniquely
    identifies one candle.  This prevents duplicates while allowing
    different expiries, strikes, CE/PE, and instruments to coexist.

    **Raw data is immutable:**  OHLCV/OI values are stored exactly
    as returned by Upstox.  Derived analytics (IV, Greeks, GEX)
    are computed separately and never overwrite raw market data.

    **Lot-size independence:**  This table does NOT contain lot_size.
    Lot_size is in ``contract_specs`` and is looked up by instrument_key
    when needed for GEX/exposure calculations.
    """

    __tablename__ = "option_candles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Identity — instrument_key links to contract_specs for metadata
    instrument_key: Mapped[str] = mapped_column(String(64), index=True)
    interval: Mapped[str] = mapped_column(String(8), default="3min")
    open_time: Mapped[datetime] = mapped_column(DateTime, index=True)

    # OHLCV from Upstox (raw, immutable)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    open_interest: Mapped[float] = mapped_column(Float, default=0.0)

    # Provenance
    source: Mapped[str] = mapped_column(String(32), default="UPSTOX_EXPIRED_CANDLE")
    fetched_at: Mapped[datetime] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint(
            "instrument_key", "interval", "open_time",
            name="uq_option_candle_identity",
        ),
    )


class OptionGreeks(Base):
    """Reconstructed historical option Greeks (Phase 7.19B).

    Stores IV + per-unit Black-Scholes Greeks calculated from raw
    ``option_candles`` + ``contract_specs`` + ``nifty_candles``.

    **Three-layer architecture:**
      - RAW (immutable): option_candles, nifty_candles, contract_specs
      - MODEL (derived): option_greeks (this table)
      - ANALYTICS (consumed): GEX, vega/delta exposure, IV research

    **Calculation versioning:**  Each record carries a ``calc_version``
    so different model configurations can coexist for the same raw data.

    **Immutability:**  This table is never used to overwrite raw
    market data.  OHLCV/OI in option_candles remains untouched.
    """

    __tablename__ = "option_greeks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Identity (matches option_candles)
    instrument_key: Mapped[str] = mapped_column(String(64), index=True)
    interval: Mapped[str] = mapped_column(String(8), default="3min")
    open_time: Mapped[datetime] = mapped_column(DateTime, index=True)

    # Market state at calculation time
    spot: Mapped[float] = mapped_column(Float)           # NIFTY index close
    strike: Mapped[float] = mapped_column(Float)         # from contract_specs
    expiry: Mapped[str] = mapped_column(String(10))      # from contract_specs
    option_type: Mapped[str] = mapped_column(String(4))  # CE or PE
    option_price: Mapped[float] = mapped_column(Float)   # close price from option candle
    lot_size: Mapped[int | None] = mapped_column(Integer, nullable=True)  # from contract_specs

    # Calculation inputs
    time_to_expiry: Mapped[float] = mapped_column(Float)     # year fraction
    risk_free_rate: Mapped[float] = mapped_column(Float)     # decimal (0.065 = 6.5%)
    intrinsic_value: Mapped[float] = mapped_column(Float)    # max(S-K,0) or max(K-S,0)

    # Output: implied volatility
    implied_volatility: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Output: per-unit Greeks
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    gamma: Mapped[float | None] = mapped_column(Float, nullable=True)
    vega: Mapped[float | None] = mapped_column(Float, nullable=True)    # per 1.00 vol
    theta: Mapped[float | None] = mapped_column(Float, nullable=True)   # annualized

    # Calculation metadata
    calc_model: Mapped[str] = mapped_column(String(32), default="BLACK_SCHOLES_EUROPEAN")
    calc_version: Mapped[str] = mapped_column(String(16), default="1.0.0")
    calculated_at: Mapped[datetime] = mapped_column(DateTime)

    # Quality
    status: Mapped[str] = mapped_column(String(16), default="SUCCESS")
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "instrument_key", "interval", "open_time", "calc_version",
            name="uq_option_greeks_identity",
        ),
    )


# ---------------------------------------------------------------------------
# Phase 7.24 — Permanent Data Pipeline Infrastructure
# ---------------------------------------------------------------------------


class IngestionLog(Base):
    """Record every data ingestion operation for observability.

    Tracks contract metadata, NIFTY candle, and option candle ingestion
    runs with full audit trail: timing, counts, errors, and metadata.

    Design:
      - One row per ingestion operation (per instrument/expiry/session).
      - Never stores access tokens or secrets.
      - Indexed for operational queries: latest run, failures, by instrument.
    """

    __tablename__ = "ingestion_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Run identity
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    operation: Mapped[str] = mapped_column(String(32), index=True)
    # 'contract_metadata' | 'nifty_candles' | 'option_candles' | 'greeks'

    # Scope (nullable — batch operations may not target a single instrument)
    instrument_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    expiry_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    session_date: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)

    # Timing
    started_at: Mapped[str] = mapped_column(String(32))  # ISO 8601
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Status
    status: Mapped[str] = mapped_column(String(16), index=True)
    # 'RUNNING' | 'SUCCESS' | 'PARTIAL' | 'FAILED'

    # Metrics
    api_calls: Mapped[int] = mapped_column(Integer, default=0)
    rows_fetched: Mapped[int] = mapped_column(Integer, default=0)
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0)
    rows_skipped: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)

    # Error tracking
    error_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 'AUTH_EXPIRED' | 'RATE_LIMIT' | 'API_ERROR' | 'NETWORK' | null
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Additional context (JSON blob, no secrets)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Indexes created via init_db() for idempotent CREATE INDEX IF NOT EXISTS


class DataCompleteness(Base):
    """Track data completeness per instrument/session.

    Enables the system to determine whether a particular dataset is
    complete without querying raw candle tables or the Upstox API.

    Supports states:
      EXPECTED   — data should exist but hasn't been fetched
      PARTIAL    — some candles present, some missing
      COMPLETE   — all expected candles present
      MISSING    — expected but no data found
      UNAVAILABLE — API returned empty/error
      FAILED     — attempted but failed
    """

    __tablename__ = "data_completeness"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Identity
    instrument_key: Mapped[str] = mapped_column(String(64), index=True)
    session_date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    data_type: Mapped[str] = mapped_column(String(32), index=True)
    # 'option_candles' | 'nifty_candles' | 'contract_metadata'

    # Completeness metrics
    expected_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_count: Mapped[int] = mapped_column(Integer, default=0)
    missing_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Status
    status: Mapped[str] = mapped_column(String(16), index=True)
    # 'EXPECTED' | 'PARTIAL' | 'COMPLETE' | 'MISSING' | 'UNAVAILABLE' | 'FAILED'

    # Audit
    last_verified_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_attempted_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "instrument_key", "session_date", "data_type",
            name="uq_data_completeness_identity",
        ),
        # Index on status created via init_db() for idempotent CREATE INDEX IF NOT EXISTS
    )


class IngestionCheckpoint(Base):
    """Durable checkpoint for resumable ingestion operations.

    Provides instrument-level checkpointing for long-running backfill
    and ingestion jobs.  Survives process termination, server restart,
    and machine restart.

    The existing greeks_checkpoint table (Phase 7.23C) serves a similar
    purpose for Greek reconstruction.  This table generalizes the pattern
    for all ingestion pipelines.

    Each (pipeline, instrument_key) pair is unique — re-running an
    operation for the same instrument updates the existing checkpoint
    rather than creating a new one.
    """

    __tablename__ = "ingestion_checkpoint"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Operation identity
    pipeline: Mapped[str] = mapped_column(String(32), index=True)
    # 'greeks' | 'backfill_contracts' | 'backfill_nifty' | 'backfill_options'
    # | 'daily_options' | 'daily_nifty'
    instrument_key: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Progress
    status: Mapped[str] = mapped_column(String(16), index=True)
    # 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED'
    items_processed: Mapped[int] = mapped_column(Integer, default=0)
    items_total: Mapped[int] = mapped_column(Integer, default=0)

    # Error tracking
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timing
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "pipeline", "instrument_key",
            name="uq_ingestion_checkpoint_identity",
        ),
        # Index on pipeline+status created via init_db() for idempotent CREATE INDEX IF NOT EXISTS
    )


# ---------------------------------------------------------------------------
# Phase 7.8A — Historical GEX Foundation
# ---------------------------------------------------------------------------


class HistoricalGexSnapshot(Base):
    """Observed historical GEX snapshot at one (instrument_key, open_time).

    One row per option-instrument per timestamp, storing the observed
    gamma exposure contribution before aggregation. The strike-level and
    chain-level GEX are computed by aggregation queries, not stored here.

    **Formula (Phase 7.1 contract):**
        raw_gex = gamma * OI * spot^2 * 0.01
        call -> +raw_gex
        put  -> -raw_gex

    **Eligibility:**
        Requires valid gamma, OI > 0, valid spot, valid strike, valid
        option_type. Rows that fail eligibility are excluded with an
        ``exclusion_reason`` rather than stored with null GEX.

    **Versioning:**
        ``calc_version`` allows different calculation methodologies to
        coexist. "historical_gex_v1" is the initial observed-GEX version.

    **Idempotency:**
        Unique on (instrument_key, interval, open_time, calc_version)
        via ON CONFLICT DO UPDATE.
    """

    __tablename__ = "historical_gex"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Identity
    instrument_key: Mapped[str] = mapped_column(String(64), index=True)
    interval: Mapped[str] = mapped_column(String(8), default="3min")
    open_time: Mapped[datetime] = mapped_column(DateTime, index=True)

    # Market context
    spot: Mapped[float] = mapped_column(Float)          # NIFTY index close at this candle
    strike: Mapped[float] = mapped_column(Float)        # from contract_specs
    expiry: Mapped[str] = mapped_column(String(10))     # from contract_specs
    option_type: Mapped[str] = mapped_column(String(4)) # CE or PE

    # Input components (for audit trail)
    gamma: Mapped[float] = mapped_column(Float)         # per-unit gamma from option_greeks
    open_interest: Mapped[float] = mapped_column(Float) # from option_candles
    option_price: Mapped[float] = mapped_column(Float)  # close from option_candles
    lot_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Computed GEX (Phase 7.1 contract)
    raw_gex: Mapped[float] = mapped_column(Float)       # gamma * OI * spot^2 * 0.01
    signed_gex: Mapped[float] = mapped_column(Float)    # +raw for CE, -raw for PE

    # Calculation metadata
    calc_version: Mapped[str] = mapped_column(String(16), default="h_gex_v1")
    calculated_at: Mapped[datetime] = mapped_column(DateTime)

    # Quality
    status: Mapped[str] = mapped_column(String(16), default="SUCCESS")  # SUCCESS | EXCLUDED
    exclusion_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "instrument_key", "interval", "open_time", "calc_version",
            name="uq_historical_gex_identity",
        ),
    )


# ---------------------------------------------------------------------------
# Issue #17 — Overnight Gap Intelligence (research/backtest only)
# ---------------------------------------------------------------------------
# Persistence for the Phase-1 research pipeline defined by
# docs/STRIKENOVA_OVERNIGHT_GAP_RESEARCH.md. Research-only: nothing here feeds
# the production dashboard, paper execution, brokers, or auth. Raw source
# snapshots are IMMUTABLE once written (append-only; no update path exists).
# Missing data stays None (never silently zero) per the GEX missing-vs-zero
# contract; completeness flags make gaps explicit and auditable.
# ---------------------------------------------------------------------------


class GapPredictionSession(Base):
    """One research session: the EOD snapshot plus the realized next-open gap.

    Predictor-side columns (cutoff snapshot, features, predictions) describe
    session T; ``next_*`` / ``gap_*`` columns describe session T+1's open and
    are attached in a separate, later step (target attachment) so no target
    value can leak into feature generation or normalization.
    """

    __tablename__ = "gap_prediction_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True, default="NIFTY")
    session_date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD (session T)
    cutoff_timestamp: Mapped[datetime] = mapped_column(DateTime)
    prior_close: Mapped[float] = mapped_column(Float)  # NIFTY close of session T

    # Attached later (session T+1). None = target not yet attached.
    next_session_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    next_open_timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_open: Mapped[float | None] = mapped_column(Float, nullable=True)
    gap_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    gap_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    gap_class: Mapped[str | None] = mapped_column(String(12), nullable=True)  # GAP_UP | FLAT | GAP_DOWN | UNCLASSIFIED

    # Data-completeness flags (explicit; missing is never silently zero).
    completeness: Mapped[str] = mapped_column(String(16), default="UNKNOWN")  # COMPLETE | PARTIAL | UNAVAILABLE
    completeness_detail: Mapped[str] = mapped_column(Text, default="{}")  # JSON

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("symbol", "session_date", name="uq_gap_sessions_symbol_session"),
    )


class UnderlyingSnapshot(Base):
    """Immutable spot/futures/VIX observation at the session-T research cutoff."""

    __tablename__ = "gap_underlying_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    session_date: Mapped[str] = mapped_column(String(10), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime)

    spot_ltp: Mapped[float | None] = mapped_column(Float, nullable=True)
    spot_open: Mapped[float | None] = mapped_column(Float, nullable=True)
    spot_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    spot_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    spot_close: Mapped[float | None] = mapped_column(Float, nullable=True)

    futures_ltp: Mapped[float | None] = mapped_column(Float, nullable=True)
    futures_oi: Mapped[float | None] = mapped_column(Float, nullable=True)  # contracts
    futures_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    futures_basis: Mapped[float | None] = mapped_column(Float, nullable=True)

    india_vix: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Raw strike-level option-chain snapshot preserved verbatim (JSON-in-Text,
    # same SQLite-compatible pattern as gex_snapshots.strike_data). Immutable.
    option_chain: Mapped[str] = mapped_column(Text, default="[]")

    __table_args__ = (
        UniqueConstraint("symbol", "session_date", name="uq_gap_underlying_symbol_session"),
    )


class OptionChainSnapshot(Base):
    """Immutable strike-level option-chain row (CE/PE) at the research cutoff."""

    __tablename__ = "gap_option_chain_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    session_date: Mapped[str] = mapped_column(String(10), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    expiry: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    strike: Mapped[float] = mapped_column(Float)
    option_type: Mapped[str] = mapped_column(String(8))  # CALL | PUT

    ltp: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_interest: Mapped[float | None] = mapped_column(Float, nullable=True)  # contracts
    change_in_oi: Mapped[float | None] = mapped_column(Float, nullable=True)
    iv: Mapped[float | None] = mapped_column(Float, nullable=True)  # canonical decimal fraction
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    gamma: Mapped[float | None] = mapped_column(Float, nullable=True)
    vega: Mapped[float | None] = mapped_column(Float, nullable=True)  # per 1.00 vol fraction
    theta: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Phase 3 (#78) — per-value provenance for this row: a JSON object
    # mapping each value field (ltp, volume, open_interest, change_in_oi,
    # bid, ask, iv, delta, gamma, vega, theta) to one of
    #   "observed"       — the value existed in the historical source
    #   "reconstructed"  — computed by StrikeNova (e.g. Black-Scholes)
    #   "derived"        — computed from stored snapshots (causal OI change)
    #   "unavailable"    — the source cannot support the value
    # NULL means pre-Phase-3 row (provenance not recorded; see migration
    # f4a9b8c2d1e7). Never interpret missing values as zero.
    value_provenance: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "symbol", "session_date", "expiry", "strike", "option_type",
            name="uq_gap_chain_symbol_session_expiry_strike_type",
        ),
    )


class GapFeatures(Base):
    """Derived, timestamp-respecting research features for one session.

    Stored separately from the immutable raw snapshots so feature definitions
    can evolve without altering historical source observations. Feature JSON
    carries NaN-safe floats; missing features are absent keys, never zeros.
    """

    __tablename__ = "gap_features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_date: Mapped[str] = mapped_column(String(10), index=True)
    feature_version: Mapped[str] = mapped_column(String(16), default="v1")
    features: Mapped[str] = mapped_column(Text, default="{}")  # JSON
    completeness: Mapped[str] = mapped_column(String(16), default="UNKNOWN")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("session_date", "feature_version", name="uq_gap_features_session_version"),
    )


class GapPrediction(Base):
    """One model's research prediction for one session (immutable once stored).

    Realized outcomes attach later on the parent session, never here, so a
    prediction row can never influence the features that produced it.
    """

    __tablename__ = "gap_predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_date: Mapped[str] = mapped_column(String(10), index=True)
    model_name: Mapped[str] = mapped_column(String(32))  # baseline | pos_style | sos
    model_version: Mapped[str] = mapped_column(String(16), default="v1")
    direction_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # [-1, +1]
    agreement_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # [0, 1]
    dispersion: Mapped[float | None] = mapped_column(Float, nullable=True)  # >= 0
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)  # [0, 1]
    state: Mapped[str] = mapped_column(String(16), default="PREDICTED")  # PREDICTED | NO_EDGE | ABSTAIN
    component_scores: Mapped[str] = mapped_column(Text, default="{}")  # JSON, explainability
    probabilities: Mapped[str] = mapped_column(Text, default="{}")  # JSON: p_up/p_flat/p_down, tails, expected gap
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "session_date", "model_name", "model_version",
            name="uq_gap_predictions_session_model",
        ),
    )


class GapBacktestResult(Base):
    """Aggregate backtest evaluation by model, period and regime."""

    __tablename__ = "gap_backtest_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_name: Mapped[str] = mapped_column(String(32), index=True)
    model_version: Mapped[str] = mapped_column(String(16), default="v1")
    period_start: Mapped[str] = mapped_column(String(10))
    period_end: Mapped[str] = mapped_column(String(10))
    regime: Mapped[str] = mapped_column(String(32), default="ALL")
    metrics: Mapped[str] = mapped_column(Text, default="{}")  # JSON metrics dict
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "model_name", "model_version", "period_start", "period_end", "regime",
            name="uq_gap_backtest_model_period_regime",
        ),
    )
