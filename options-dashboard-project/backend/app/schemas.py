from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator

from app.utils.symbols import normalize_underlying

# ---------------------------------------------------------------------------
# UTC-aware datetime serialization (Phase: timestamp standardization)
# ---------------------------------------------------------------------------

def _serialize_utc_datetime(dt: datetime) -> str | None:
    """Serialize a datetime to ISO 8601 with explicit UTC offset.

    SQLite strips timezone info on storage, so datetimes read back are naive
    UTC.  This serializer re-attaches UTC and produces ``+00:00`` offset,
    ensuring the frontend correctly interprets the instant.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# Custom Pydantic type: naive datetimes from SQLite are treated as UTC
UtcDatetime = Annotated[
    datetime,
    PlainSerializer(_serialize_utc_datetime, return_type=str, when_used="always"),
]



class LegOrderIn(BaseModel):
    """One executed option leg, as filled by the paper trading engine."""

    symbol: str = Field(..., min_length=1)
    expiration_date: str = Field(..., min_length=1)
    strike_price: float = Field(..., gt=0)
    option_type: Literal["call", "put"]
    action: Literal["buy", "sell"]
    premium: float = Field(..., ge=0)  # simulated premium per unit
    quantity: int = Field(..., ge=1)  # number of lots
    lot_size: int = Field(..., ge=1)  # contracts per lot


class OrderFillIn(BaseModel):
    """An executed paper order (one strategy, one or more legs)."""

    symbol: str = Field(..., min_length=1)
    strategy_tag: str = "Custom"
    starting_capital: float | None = Field(default=None, gt=0)
    legs: list[LegOrderIn] = Field(..., min_length=1)


class LegCloseIn(BaseModel):
    exit_price: float = Field(..., ge=0)


# ---- Phase 5.0: server-authoritative paper trading schemas ------------------


class ExecutionLegIn(BaseModel):
    """One leg of a strategy execution request.

    Fill prices are NOT taken from the client: the backend resolves the
    authoritative market price from the required expiry chain at execution
    time. ``quantity`` is LOTS.
    """

    symbol: str = Field(..., min_length=1)
    expiration_date: str = Field(..., min_length=1)
    strike_price: float = Field(..., gt=0)
    option_type: Literal["call", "put"]
    action: Literal["buy", "sell"]
    quantity: int = Field(..., ge=1)  # lots
    lot_size: int = Field(..., ge=1)  # contracts per lot


class ExecutionRequestIn(BaseModel):
    """Strategy execution request (idempotent via ``client_order_id``)."""

    client_order_id: str = Field(..., min_length=8, max_length=64)
    symbol: str = Field(..., min_length=1)
    strategy_tag: str = "Custom"
    strategy_id: str | None = None
    starting_capital: float | None = Field(default=None, gt=0)
    legs: list[ExecutionLegIn] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _bind_every_leg_to_the_request_underlying(self):
        """Day-50 Fix A: one underlying per request, at the earliest boundary.

        Each ``ExecutionLegIn`` carries its own ``symbol``, while the fill
        prices, the candidate evidence and the risk verdict are all resolved
        against the TOP-LEVEL ``symbol`` only.  A leg naming a different
        underlying could therefore be priced off the wrong chain and
        persisted under a foreign symbol.  Both levels are normalized
        identically (strip + upper, the execution service's own convention)
        and any leg that disagrees with the approved top-level underlying is
        rejected here — before any broker, risk or database work.  The same
        invariant is re-enforced server-side in the candidate producer, the
        sanctioned bridge and the execution choke point, so no service-level
        caller can bypass it either.
        """
        approved = normalize_underlying(self.symbol)
        self.symbol = approved
        for leg in self.legs:
            leg.symbol = normalize_underlying(leg.symbol)
            if leg.symbol != approved:
                raise ValueError(
                    f"leg underlying {leg.symbol!r} does not match the "
                    f"request underlying {approved!r}; every execution leg "
                    "must name the request's own underlying")
        return self


class ExitRequestIn(BaseModel):
    """Position exit request (idempotent via ``client_order_id``).

    ``quantity`` defaults to the full position. ``exit_price`` is optional;
    when omitted (or when the backend has a fresher chain quote) the
    authoritative market price is used.
    """

    client_order_id: str = Field(..., min_length=8, max_length=64)
    quantity: int | None = Field(default=None, ge=1)  # lots
    exit_price: float | None = Field(default=None, ge=0)


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_order_id: str
    execution_id: str | None
    position_id: int | None
    kind: str
    symbol: str
    expiry: str
    strike: float
    option_type: str
    action: str
    quantity: int
    lot_size: int
    status: str
    filled_quantity: int
    fill_price: float | None
    price_source: str
    realized_pnl: float | None
    rejected_reason: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    # Strategy attribution (resolved by the service layer, not stored on PaperOrder)
    strategy_tag: str | None = None
    strategy_execution_id: str | None = None


class ExecutionOut(BaseModel):
    """Result of a strategy execution (a group of orders)."""

    execution_id: str
    status: str
    symbol: str
    strategy_tag: str
    entry_net: float
    orders: list[OrderOut]
    filled_count: int
    failed_count: int
    errors: list[str]
    duplicated: bool = False  # true when this is an idempotent replay
    # Phase 6.10: V2 execution audit trail (null for V1 executions)
    execution_metadata: dict | None = None


class PositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    expiry: str
    strike: float
    option_type: str
    net_quantity: int
    average_entry_price: float
    lot_size: int
    realized_pnl: float
    status: str
    strategy_execution_id: str | None
    # Phase 5.2.1: the strategy's displayed name rides on the position so the
    # UI never falls back to "Custom" for a named strategy. The authoritative
    # relationship stays strategy_execution_id → StrategyExecution.strategy_tag
    # (the position itself never duplicates strategy logic). Null/legacy rows
    # fall back to "Custom" at the boundary.
    strategy_tag: str | None = None
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None
    # Unrealized P&L requires a market mark. The backend never fabricates it:
    # it stays null here and the UI applies the existing chain-cache mark.
    unrealized_pnl: float | None = None
    current_price: float | None = None


class PortfolioSummaryOut(BaseModel):
    starting_cash: float
    available_cash: float
    invested_value: float
    realized_pnl: float
    unrealized_pnl: float | None = None  # requires market marks (UI applies them)
    total_pnl: float
    open_position_count: int
    open_strategy_count: int


class PortfolioGroupOut(BaseModel):
    """Strategy-grouped portfolio view (one row per strategy execution)."""

    execution_id: str
    strategy_tag: str
    symbol: str
    status: str
    entry_net: float
    realized_pnl: float
    legs: list[OrderOut]
    entry_at: datetime


class ExitOut(BaseModel):
    """Result of a position exit (one exit order + the updated position)."""

    order: OrderOut
    position: PositionOut
    duplicated: bool = False  # true when this is an idempotent replay


# ---- Phase 5.2: bulk paper position exit ------------------------------------


class ExitIntentRequestIn(BaseModel):
    """Server-authoritative exit intent request (Phase 6.5.0.4).

    The selector identifies WHAT exposure should be exited. The server
    independently resolves this against the authenticated user's current
    StrategyLegExposure and Position data — the client does NOT get to
    dictate which exposure is actually targeted.

    ``scope`` identifies the resolution scope:
    - PORTFOLIO: all matching open exposures of the user
    - STRATEGY: only exposures within one strategy_execution_id
    - POSITION: only exposures within one position_id

    ``option_type`` and ``action`` filter the source exposure (NOT the
    execution side). ``action`` = "BUY" means the original strategy-leg
    action was BUY; the server will exit with SELL.

    ``exposure_id`` optionally targets a single StrategyLegExposure.
    """

    client_order_id: str = Field(..., min_length=8, max_length=64)
    scope: Literal["PORTFOLIO", "STRATEGY", "POSITION"] = "STRATEGY"
    strategy_execution_id: str | None = None
    position_id: int | None = None
    exposure_id: int | None = None
    option_type: Literal["CALL", "PUT", "call", "put", "CE", "PE", "ce", "pe"] | None = None
    action: Literal["BUY", "SELL", "buy", "sell"] | None = None
    quantity_mode: Literal["ALL", "QUANTITY"] = "ALL"
    quantity: int | None = Field(default=None, ge=1)


class ExitIntentTargetOut(BaseModel):
    """One resolved execution target from the server-side resolver."""

    position_id: int
    strategy_leg_exposure_id: int | None = None
    strategy_execution_id: str | None = None
    symbol: str
    expiry: str
    strike: float
    option_type: str
    source_action: str  # the original strategy-leg action
    exit_side: str      # the inverse transaction side
    quantity: int       # lots to exit
    remaining_quantity: int
    lot_size: int


class ExitIntentPreviewOut(BaseModel):
    """Server-authoritative exit preview (Phase 6.6.5).

    Resolves targets WITHOUT mutating any state. The frontend uses this
    to display the confirmation dialog before the user confirms.
    """

    status: str  # PREVIEW | NO_MATCHING_TARGETS | REJECTED
    targets: list[ExitIntentTargetOut] = []
    errors: list[str] = []
    warnings: list[str] = []


class ExitIntentOut(BaseModel):
    """Result of a server-authoritative exit intent resolution + execution."""

    status: str  # SUCCESS | PARTIAL | FAILED | DUPLICATE | DISABLED | REJECTED | NO_MATCHING_TARGETS
    intent_id: str | None = None
    duplicated: bool = False
    targets_resolved: int = 0
    targets_executed: int = 0
    targets: list[ExitIntentTargetOut] = []
    orders: list[OrderOut] = []
    positions: list[PositionOut] = []
    errors: list[str] = []
    warnings: list[str] = []


class BulkExitRequestIn(BaseModel):
    """Bulk exit request (idempotent via ``client_order_id``).

    One key covers the WHOLE bulk operation: EXIT STRATEGY closes every open
    position of one ``strategy_execution_id``; EXIT ALL closes every open
    position of the authenticated user. Replaying the same key returns the
    original result without closing anything twice.
    """

    client_order_id: str = Field(..., min_length=8, max_length=64)


class BulkExitPositionOut(BaseModel):
    """One position's outcome within a bulk exit.

    ``status`` is EXITED (closed by this operation), ALREADY_CLOSED (lost a
    genuine execution-time race to another request — reported, never
    re-closed) or FAILED (execution-time error, with ``error``).
    """

    position_id: int
    symbol: str
    expiry: str
    strike: float
    option_type: str
    strategy_execution_id: str | None = None
    strategy_tag: str | None = None
    status: str  # EXITED | ALREADY_CLOSED | FAILED
    realized_pnl: float | None = None
    fill_price: float | None = None
    error: str | None = None


class BulkExitGroupOut(BaseModel):
    """Bulk outcome grouped by strategy execution where possible."""

    strategy_execution_id: str | None = None
    strategy_tag: str
    requested: int
    exited: int
    failed: int
    realized_pnl: float
    status: str  # EXITED | PARTIAL | FAILED | SKIPPED


class BulkExitOut(BaseModel):
    """Result of a bulk exit (EXIT STRATEGY or EXIT ALL).

    ``status`` is SUCCESS (every requested position exited), NO_POSITIONS
    (nothing was open to exit), FAILED (every requested position failed) or
    PARTIAL (some exited, some did not — only possible for a true
    execution-time failure after all pre-validation passed, e.g. a
    concurrent individual exit winning the race for one position).
    ``duplicated`` is true when the same ``client_order_id`` is replayed;
    the returned numbers are then the ORIGINAL result, never a re-run.
    """

    execution_id: str  # the bulk operation's own id (== client_order_id)
    scope: Literal["STRATEGY", "ACCOUNT"]
    status: Literal["SUCCESS", "NO_POSITIONS", "FAILED", "PARTIAL"]
    requested_count: int
    exited_count: int
    failed_count: int
    total_realized_pnl: float
    cash_change: float
    positions: list[BulkExitPositionOut]
    groups: list[BulkExitGroupOut]
    errors: list[str] = []
    duplicated: bool = False


class PortfolioOut(BaseModel):
    """Portfolio: summary numbers + strategy-grouped view."""

    summary: PortfolioSummaryOut
    groups: list[PortfolioGroupOut]


class ReconcileOut(BaseModel):
    valid: bool
    discrepancies: list[dict]


class LegOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    expiration_date: str
    strike_price: float
    option_type: str
    action: str
    premium: float
    quantity: int
    lot_size: int
    entry_at: UtcDatetime
    exit_at: UtcDatetime | None
    exit_price: float | None
    realized_pnl: float | None


class TradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    strategy_tag: str
    status: str
    entry_net: float
    realized_pnl: float | None
    entry_at: UtcDatetime
    exit_at: UtcDatetime | None
    legs: list[LegOut]


class AccountOut(BaseModel):
    starting_capital: float
    balance: float
    net_pnl: float


class JournalStatsOut(BaseModel):
    total_trades: int
    open_trades: int
    closed_trades: int
    wins: int
    win_rate: float | None
    profit_factor: float | None
    gross_profit: float
    gross_loss: float


class JournalOut(BaseModel):
    account: AccountOut
    stats: JournalStatsOut
    trades: list[TradeOut]


class MarketStatusOut(BaseModel):
    """Current market status for the paper-trading UI badge (Phase 5.2.1).

    Segment-aware: the status is resolved for ONE segment (default
    INDEX_DERIVATIVES — the product's NIFTY index-options segment), so a
    cash-segment closing auction can never be mistaken for index-derivatives
    trading. ``session_state`` carries the explicit session the gate resolved
    (OPEN | CLOSING_AUCTION | TRANSITION | CLOSED | UNKNOWN); only OPEN
    authorizes orders. The backend remains the final authority — the badge
    is informational.
    """

    status: Literal["open", "closed", "unknown"]
    source: str
    trade_date: str | None = None
    checked_at: str
    message: str
    open: bool
    segment: str = "INDEX_DERIVATIVES"
    session_state: str = "UNKNOWN"  # OPEN | CLOSING_AUCTION | TRANSITION | CLOSED | UNKNOWN
    timezone: str = "Asia/Kolkata"
    trading_allowed: bool = False


# ---- Phase 5.1: portfolio & journal analytics schemas -----------------------


class PortfolioSummaryAnalyticsOut(BaseModel):
    """Canonical portfolio summary. ``unrealized_pnl`` stays ``None`` until a
    market mark exists (the frontend chain cache supplies it)."""

    starting_capital: float
    available_cash: float
    invested_value: float
    realized_pnl: float
    unrealized_pnl: float | None = None
    total_pnl: float
    return_pct: float | None = None  # totalPnl / startingCapital × 100 (not ROI/margin)
    open_position_count: int
    open_strategy_count: int


class PerformanceOut(BaseModel):
    """Performance metrics over COMPLETED strategy trades. Every statistic
    that needs data returns ``None`` (never 0/Infinity) when data is missing."""

    total_completed_trades: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    win_rate: float | None = None
    average_winner: float | None = None
    average_loser: float | None = None
    profit_factor: float | None = None
    expectancy: float | None = None
    largest_winner: float | None = None
    largest_loser: float | None = None
    current_win_streak: int = 0
    current_loss_streak: int = 0
    max_win_streak: int = 0
    max_loss_streak: int = 0
    average_holding_duration: float | None = None  # seconds
    median_holding_duration: float | None = None
    shortest_holding_duration: float | None = None
    longest_holding_duration: float | None = None


class EquityPointOut(BaseModel):
    """One point of the REALIZED equity curve."""

    date: str  # YYYY-MM-DD of the realization
    pnl: float
    cumulative_pnl: float
    equity: float


class DrawdownOut(BaseModel):
    current_drawdown: float | None = None
    current_drawdown_pct: float | None = None
    max_drawdown: float | None = None
    max_drawdown_pct: float | None = None


class DailyPnlOut(BaseModel):
    """Daily realized P&L. Historical unrealized marks are never stored, so
    ``unrealized_pnl`` is always ``None`` (unavailable), never fabricated."""

    date: str
    realized_pnl: float
    unrealized_pnl: float | None = None
    total_pnl: float


class StrategyPerformanceOut(BaseModel):
    """One strategy's completed-trade performance (grouped by tag)."""

    strategy: str
    trades: int
    wins: int
    losses: int
    win_rate: float | None = None
    total_pnl: float
    average_pnl: float
    profit_factor: float | None = None
    expectancy: float | None = None


class PositionAnalyticsItemOut(BaseModel):
    """One OPEN position. Current-price fields stay ``None`` until the
    frontend overlays a market mark (backend has no live marks)."""

    symbol: str
    expiry: str
    strike: float
    option_type: str
    net_quantity: int
    average_entry: float
    current_price: float | None = None
    unrealized_pnl: float | None = None
    market_value: float | None = None
    strategy_execution_id: str | None = None


class PositionAnalyticsOut(BaseModel):
    """Open-position exposure. Entry-value exposure, NOT margin required."""

    long_exposure: float
    short_exposure: float
    total_exposure: float
    items: list[PositionAnalyticsItemOut]


class JournalRowOut(BaseModel):
    """ONE completed strategy trade for the grouped journal view (multi-leg
    strategies appear as a single row; individual legs ride underneath)."""

    execution_id: str
    strategy: str
    symbol: str
    entry_at: UtcDatetime
    exit_at: UtcDatetime
    duration_seconds: float | None = None
    duration_label: str | None = None
    realized_pnl: float
    result: str  # WIN | LOSS | BREAKEVEN
    legs: list[dict]
    tags: list[str] | None = None
    notes: str | None = None


class TradeAnnotationsIn(BaseModel):
    """PUT /paper/analytics/trades/{execution_id}/annotations — update trade annotations."""

    tags: list[str] | None = Field(default=None)
    notes: str | None = Field(default=None, max_length=2000)

class TradeAnnotationsOut(BaseModel):
    """Response after updating trade annotations."""

    execution_id: str
    tags: list[str] | None = None
    notes: str | None = None


class TradeLegDetailOut(BaseModel):
    """One leg of a strategy execution with entry/exit details."""

    symbol: str
    expiry: str
    strike: float
    option_type: str
    action: str
    quantity: int
    lot_size: int
    entry_price: float | None = None
    exit_price: float | None = None
    entry_status: str | None = None
    realized_pnl: float | None = None
    remaining_quantity: int | None = None


class TradeDetailOut(BaseModel):
    """GET /paper/analytics/trades/:id — complete trade detail."""

    execution_id: str
    strategy: str
    symbol: str
    status: str
    result: str | None = None
    entry_at: UtcDatetime
    exit_at: UtcDatetime | None = None
    duration_seconds: float | None = None
    duration_label: str | None = None
    entry_net: float
    realized_pnl: float | None = None
    total_quantity: int = 0
    total_exposure: float = 0.0
    open_position_count: int = 0
    closed_position_count: int = 0
    entry_order_count: int = 0
    exit_order_count: int = 0
    legs: list[TradeLegDetailOut] = []
    execution_metadata: dict | None = None
    tags: list[str] | None = None
    notes: str | None = None


class StrategyTradeRowOut(BaseModel):
    """One execution row in a strategy's trade list."""

    execution_id: str
    symbol: str
    status: str
    result: str | None = None
    entry_at: UtcDatetime
    exit_at: UtcDatetime | None = None
    realized_pnl: float | None = None
    duration_seconds: float | None = None
    duration_label: str | None = None
    tags: list[str] | None = None


class StrategyDetailOut(BaseModel):
    """GET /paper/analytics/strategies/:name — strategy detail with aggregate metrics."""

    strategy: str
    total_executions: int
    open_executions: int
    closed_executions: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    win_rate: float | None = None
    gross_profit: float
    gross_loss: float
    net_realized_pnl: float
    profit_factor: float | None = None
    expectancy: float | None = None
    average_winner: float | None = None
    average_loser: float | None = None
    largest_winner: float | None = None
    largest_loser: float | None = None
    current_win_streak: int = 0
    current_loss_streak: int = 0
    max_win_streak: int = 0
    max_loss_streak: int = 0
    average_holding_duration: float | None = None
    trades: list[StrategyTradeRowOut] = []


class AnalyticsWarningOut(BaseModel):
    code: str
    discrepancies: list[dict] = []


class DataQualityOut(BaseModel):
    """Every analytics block reports availability: available | partial |
    unavailable. Values are never fabricated to complete a chart."""

    historical_unrealized: str = "unavailable"
    current_marks: str = "unavailable"
    completed_trades: str = "available"  # available | none
    warnings: list[AnalyticsWarningOut] = []


class AnalyticsOut(BaseModel):
    """GET /paper/analytics — ONE authoritative analytics response."""

    summary: PortfolioSummaryAnalyticsOut
    performance: PerformanceOut
    equity_curve: list[EquityPointOut]
    drawdown: DrawdownOut
    daily_pnl: list[DailyPnlOut]
    strategies: list[StrategyPerformanceOut]
    positions: PositionAnalyticsOut
    journal: list[JournalRowOut]
    data_quality: DataQualityOut
    filters: dict


# ---- Phase 6.0/6.1: capital & margin foundation + broker integration -------


class CapitalValueOut(BaseModel):
    """One capital figure with its source and availability.

    Source is one of BROKER_REPORTED | ESTIMATED | CALCULATED | UNAVAILABLE.
    Status is available | partial | unavailable. A missing value is ``None``
    (never 0), so unavailable is never displayed as a fabricated zero.
    ``timestamp`` marks when a BROKER_REPORTED figure was captured (null
    when unavailable) so stale broker funds are never presented as real-time.
    """

    value: float | None = None
    source: str
    status: str = "unavailable"
    timestamp: str | None = None


class CapitalStrategyOut(BaseModel):
    """Whole-strategy capital context for ONE open execution.

    Multi-leg strategies are analysed as ONE unit (never per-leg margin
    numbers summed together). ``estimated_capital`` is ``None`` for credit
    strategies — premium received is not capital required.

    Phase 6.1 adds the whole-strategy BROKER margin (``broker_margin``) when
    the authenticated broker margin API succeeds; it stays ``None`` with a
    structured ``broker_margin_error`` (e.g. MISSING_INSTRUMENT_KEY,
    MARGIN_REQUEST_TOO_LARGE) otherwise. Broker margin is never replaced by
    the estimated capital figure.
    """

    execution_id: str
    strategy_tag: str
    symbol: str
    entry_net: float  # +debit paid / −credit received
    premium_outlay: float  # gross premium paid on long legs (0 is valid)
    estimated_capital: float | None = None
    estimated_capital_basis: str | None = None  # "premium" in Phase 6.0
    # ---- Phase 6.1: broker-reported whole-strategy margin ----
    broker_margin: float | None = None
    broker_margin_status: str = "unavailable"  # available | partial | unavailable
    broker_margin_error: str | None = None  # structured code, e.g. MISSING_INSTRUMENT_KEY
    broker_margin_timestamp: str | None = None
    broker_margin_detail: dict | None = None  # raw broker rows preserved


class RocInputsOut(BaseModel):
    """Future Return-on-Capital INPUTS ONLY (the metric is NOT computed).

    ``available`` is False until both P&L and a capital figure exist, so a
    future phase can never divide by an unknown denominator. Phase 6.1
    preserves broker margin separately (``broker_margin``) for a future
    Return-on-Margin / Capital Efficiency metric.
    """

    pnl: float | None = None
    capital_used: float | None = None
    available: bool = False


class BrokerFundsDetailOut(BaseModel):
    """Mapped V3 funds breakdown (broker terminology preserved).

    ``raw`` keeps the complete broker payload so no broker field is lost;
    ``generated_at`` / ``expires_at`` mark when the snapshot was captured and
    when it must be refreshed — stale broker funds are never real-time.
    """

    available_to_trade: float | None = None
    cash_available_to_trade: float | None = None
    margin_used: float | None = None
    span_exposure: float | None = None
    cash_margin_var_elm: float | None = None
    premium_present: float | None = None
    delivery_margin: float | None = None
    pledge_available_to_trade: float | None = None
    margin_from_pledge: float | None = None
    pledge_margin_used: float | None = None
    unsettled_profit: float | None = None
    raw: dict | None = None
    error: str | None = None  # structured code (e.g. BROKER_MAINTENANCE)
    message: str | None = None
    generated_at: str | None = None
    expires_at: str | None = None


class BrokerMarginRowOut(BaseModel):
    """One per-instrument row from the broker margin response (kept raw)."""

    instrument_key: str | None = None
    span_margin: float | None = None
    exposure_margin: float | None = None
    equity_margin: float | None = None
    net_buy_premium: float | None = None
    additional_margin: float | None = None
    total_margin: float | None = None
    tender_margin: float | None = None


class BrokerMarginStrategyOut(BaseModel):
    """Whole-strategy broker margin for ONE open execution.

    ``required_margin`` is the broker-reported figure for the COMPLETE
    multi-leg request (never a platform sum of per-leg margins). ``error``
    carries a structured code when the broker result is unavailable.
    """

    execution_id: str | None = None
    strategy_tag: str | None = None
    status: str = "unavailable"
    error: str | None = None
    message: str | None = None
    required_margin: float | None = None
    final_margin: float | None = None
    instrument_count: int = 0
    timestamp: str | None = None
    expires_at: str | None = None
    rows: list[BrokerMarginRowOut] = Field(default_factory=list)


class BrokerMarginDetailOut(BaseModel):
    """Aggregate broker margin across the user's open strategies."""

    per_strategy: list[BrokerMarginStrategyOut] = Field(default_factory=list)
    aggregate_required_margin: float | None = None
    aggregate_status: str = "unavailable"
    generated_at: str | None = None
    expires_at: str | None = None


class BrokerProfileOut(BaseModel):
    """GET /paper/broker/profile — broker connection diagnostics (Phase 6.4.1).

    Read-only: verifies the authenticated customer's Upstox connection and
    returns the NORMALIZED safe profile only (user name, email, user id,
    exchanges, products, order types, POA/DDPI...). Credentials are never
    returned. ``profile`` is ``None`` when the broker profile is unavailable
    — paper account values are never substituted. ``cached`` marks a
    user-scoped cache hit (stale profile data is never presented as
    real-time); a manual refresh bypasses the cache.
    """

    status: Literal["available", "unavailable"]
    source: str = "BROKER_REPORTED"
    broker: str = "UPSTOX"
    profile: dict | None = None
    generated_at: str | None = None
    cached: bool = False
    error: str | None = None  # structured code: BROKER_TOKEN_EXPIRED, ...
    message: str | None = None  # human-readable, never a raw provider error


class CapitalOut(BaseModel):
    """GET /paper/capital — server-authoritative capital summary.

    Premium outlay, broker margin, estimated capital and available funds are
    kept strictly separate and each carries its source/status. Paper capital
    is labeled paper capital; it is never renamed as broker funds.

    Phase 6.1: ``broker_margin`` is the aggregate whole-strategy margin
    reported by the broker; ``broker_available_funds`` / ``broker_margin_used``
    / ``broker_cash_available`` / ``broker_pledge_available`` are account
    funds figures (BROKER_REPORTED, never replaced by paper cash).
    ``broker_funds_detail`` and ``broker_margin_detail`` preserve the raw
    broker payloads with capture/expiry timestamps.
    """

    premium_outlay: CapitalValueOut
    broker_margin: CapitalValueOut
    estimated_capital: CapitalValueOut
    estimated_capital_basis: str | None = None
    broker_available_funds: CapitalValueOut
    broker_cash_available: CapitalValueOut
    broker_margin_used: CapitalValueOut
    broker_pledge_available: CapitalValueOut
    broker_funds_detail: BrokerFundsDetailOut
    broker_margin_detail: BrokerMarginDetailOut
    broker_errors: dict = Field(default_factory=dict)
    broker_generated_at: str | None = None
    expires_at: str | None = None
    paper_starting_capital: CapitalValueOut
    paper_available_cash: CapitalValueOut
    capital_used: CapitalValueOut
    remaining_capital: CapitalValueOut
    roc_inputs: RocInputsOut
    strategies: list[CapitalStrategyOut]
    generated_at: str
    status: str  # available | partial | unavailable


# ---- Phase 6.7: custom strategy templates ----------------------------------


class StrategyTemplateLegIn(BaseModel):
    """One leg of a user-created strategy template.

    V1 (fixed-leg): provide ``strike`` + ``expiry``.
    V2 (dynamic formula): additionally provide ``strike_mode``, ``expiry_mode``,
    and the relevant formula parameters.

    All V2 fields are optional — omitted fields default to V1 fixed behavior
    for backward compatibility with existing clients.
    """

    action: Literal["buy", "sell"]
    option_type: Literal["call", "put"]
    strike: float = Field(..., gt=0)
    expiry: str = Field(..., min_length=1)
    quantity: int = Field(..., ge=1)
    lot_size: int = Field(..., ge=1)
    price: float | None = Field(default=None, ge=0)  # informational only
    position: int = Field(default=0)  # ordering

    # Phase 6.8B: dynamic formula fields (all optional, V1 defaults)
    strike_mode: Literal["fixed", "atm", "atm_offset_steps", "atm_offset", "spot_offset", "delta"] = "fixed"
    strike_offset: int | None = None      # atm_offset_steps: integer step offset
    strike_offset_pct: float | None = None  # percentage offset (reserved)
    target_delta: float | None = None     # delta mode: target delta value
    expiry_mode: Literal["fixed", "current_week", "next_week", "monthly", "dte_range"] = "fixed"
    expiry_dte_min: int | None = None     # dte_range: minimum DTE
    expiry_dte_max: int | None = None     # dte_range: maximum DTE
    formula_version: int = Field(default=1, ge=1, le=2)  # 1=fixed, 2=dynamic

    @model_validator(mode="after")
    def _validate_formula(self) -> "StrategyTemplateLegIn":
        """Validate formula field consistency without market-data lookups."""
        sm = self.strike_mode
        em = self.expiry_mode

        # --- Strike formula validation ---
        if sm == "atm_offset_steps" and self.strike_offset is None:
            raise ValueError("strike_offset is required for strike_mode='atm_offset_steps'")
        if sm == "atm_offset" and self.strike_offset is None:
            raise ValueError("strike_offset is required for strike_mode='atm_offset'")
        if sm == "spot_offset" and self.strike_offset is None:
            raise ValueError("strike_offset is required for strike_mode='spot_offset'")
        if sm == "delta" and self.target_delta is None:
            raise ValueError("target_delta is required for strike_mode='delta'")

        # Clear irrelevant strike params for clean state
        if sm not in ("atm_offset_steps", "atm_offset", "spot_offset"):
            # These modes don't use strike_offset — allow but don't require it
            pass
        if sm != "delta":
            self.target_delta = None  # clear stale delta param

        # --- Expiry formula validation ---
        if em == "dte_range":
            if self.expiry_dte_min is None or self.expiry_dte_max is None:
                raise ValueError("expiry_dte_min and expiry_dte_max are required for expiry_mode='dte_range'")
            if self.expiry_dte_min > self.expiry_dte_max:
                raise ValueError(f"expiry_dte_min ({self.expiry_dte_min}) must be <= expiry_dte_max ({self.expiry_dte_max})")

        # Clear irrelevant expiry params
        if em != "dte_range":
            self.expiry_dte_min = None
            self.expiry_dte_max = None

        # --- Auto-set formula_version ---
        if sm != "fixed" or em != "fixed":
            self.formula_version = 2

        return self


class StrategyTemplateCreateIn(BaseModel):
    """Create a new strategy template."""

    name: str = Field(..., min_length=1, max_length=128)
    symbol: str = Field(default="NIFTY", min_length=1)
    legs: list[StrategyTemplateLegIn] = Field(..., min_length=1)


class StrategyTemplateUpdateIn(BaseModel):
    """Update an existing strategy template (partial update)."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    symbol: str | None = Field(default=None, min_length=1)
    legs: list[StrategyTemplateLegIn] | None = None


class StrategyTemplateLegOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    position: int
    action: str
    option_type: str
    strike: float
    expiry: str
    quantity: int
    lot_size: int
    price: float | None = None
    # Phase 6.8B: dynamic formula fields
    strike_mode: str = "fixed"
    strike_offset: int | None = None
    strike_offset_pct: float | None = None
    target_delta: float | None = None
    expiry_mode: str = "fixed"
    expiry_dte_min: int | None = None
    expiry_dte_max: int | None = None
    formula_version: int = 1


class StrategyTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    symbol: str
    legs: list[StrategyTemplateLegOut]
    created_at: UtcDatetime
    updated_at: UtcDatetime


# ---- Phase 6.6.6: live position valuation ----------------------------------


class LegValuationOut(BaseModel):
    """One StrategyLegExposure's live valuation."""

    exposure_id: int
    execution_id: str
    action: str  # buy | sell
    remaining_quantity: int  # lots
    lot_size: int
    entry_price: float | None = None  # authoritative per-leg entry (PaperOrder.fill_price)
    current_price: float | None = None
    market_value: float | None = None
    live_pnl: float | None = None
    price_status: str = "unavailable"  # available | stale | unavailable


class StrategyValuationOut(BaseModel):
    """Aggregated live valuation for one strategy execution within a position."""

    execution_id: str | None = None
    strategy_tag: str = "Custom"
    live_pnl: float | None = None
    market_value: float | None = None
    legs: list[LegValuationOut] = Field(default_factory=list)
    price_status: str = "unavailable"


class PositionValuationOut(BaseModel):
    """One open position's complete live valuation."""

    position_id: int
    symbol: str
    expiry: str
    strike: float
    option_type: str
    side: str  # LONG | SHORT
    net_quantity: int
    average_entry_price: float
    lot_size: int
    realized_pnl: float
    current_price: float | None = None
    market_value: float | None = None
    live_pnl: float | None = None
    live_pnl_pct: float | None = None
    price_status: str = "unavailable"  # available | stale | unavailable
    strategies: list[StrategyValuationOut] = Field(default_factory=list)


class ValuationSummaryOut(BaseModel):
    """Aggregate portfolio valuation across all open positions."""

    total_live_pnl: float | None = None
    total_market_value: float | None = None
    total_realized_pnl: float = 0.0
    open_position_count: int = 0
    positions_with_price: int = 0
    positions_unavailable: int = 0
    generated_at: str = ""
    status: str = "available"  # available | partial | unavailable


class PositionValuationResponseOut(BaseModel):
    """GET /paper/positions/valuation response."""

    positions: list[PositionValuationOut]
    summary: ValuationSummaryOut


# ---- Phase 6.8C: strategy resolution API ------------------------------------


class ResolutionInlineLegIn(BaseModel):
    """One leg for inline resolution (POST /paper/resolve).

    V1 (fixed-leg): provide ``strike`` + ``expiry``.
    V2 (dynamic formula): additionally provide ``strike_mode``, ``expiry_mode``,
    and the relevant formula parameters.
    """

    action: Literal["buy", "sell"]
    option_type: Literal["call", "put"]
    strike: float = Field(..., gt=0)
    expiry: str = Field(..., min_length=1)
    quantity: int = Field(..., ge=1)
    lot_size: int = Field(..., ge=1)

    # Phase 6.8B: dynamic formula fields (all optional, V1 defaults)
    strike_mode: Literal["fixed", "atm", "atm_offset_steps", "atm_offset", "spot_offset", "delta"] = "fixed"
    strike_offset: int | None = None
    strike_offset_pct: float | None = None
    target_delta: float | None = None
    expiry_mode: Literal["fixed", "current_week", "next_week", "monthly", "dte_range"] = "fixed"
    expiry_dte_min: int | None = None
    expiry_dte_max: int | None = None
    formula_version: int = Field(default=1, ge=1, le=2)

    @model_validator(mode="after")
    def _validate_formula(self) -> "ResolutionInlineLegIn":
        """Validate formula field consistency (structural only, no market lookup)."""
        sm = self.strike_mode
        em = self.expiry_mode

        if sm == "atm_offset_steps" and self.strike_offset is None:
            raise ValueError("strike_offset is required for strike_mode='atm_offset_steps'")
        if sm == "atm_offset" and self.strike_offset is None:
            raise ValueError("strike_offset is required for strike_mode='atm_offset'")
        if sm == "spot_offset" and self.strike_offset is None:
            raise ValueError("strike_offset is required for strike_mode='spot_offset'")
        if sm == "delta" and self.target_delta is None:
            raise ValueError("target_delta is required for strike_mode='delta'")

        if sm != "delta":
            self.target_delta = None

        if em == "dte_range":
            if self.expiry_dte_min is None or self.expiry_dte_max is None:
                raise ValueError("expiry_dte_min and expiry_dte_max are required for expiry_mode='dte_range'")
            if self.expiry_dte_min > self.expiry_dte_max:
                raise ValueError(f"expiry_dte_min ({self.expiry_dte_min}) must be <= expiry_dte_max ({self.expiry_dte_max})")

        if em != "dte_range":
            self.expiry_dte_min = None
            self.expiry_dte_max = None

        if sm != "fixed" or em != "fixed":
            self.formula_version = 2

        return self


class ResolutionInlineRequestIn(BaseModel):
    """POST /paper/resolve — inline leg resolution request."""

    symbol: str = Field(default="NIFTY", min_length=1)
    legs: list[ResolutionInlineLegIn] = Field(..., min_length=1)


class TemplateResolutionRequestIn(BaseModel):
    """POST /paper/templates/:id/resolve — optional override body.

    Currently unused in V1 but the schema is in place for future
    parameter overrides (e.g. symbol override, market-price overrides).
    """

    pass


class ResolutionLegOut(BaseModel):
    """One resolved leg in the resolution response."""

    position: int
    action: str
    option_type: str
    quantity: int
    lot_size: int
    resolved_strike: float
    resolved_expiry: str  # YYYY-MM-DD
    strike_mode_used: str
    expiry_mode_used: str
    current_price: float | None = None
    price_status: str = "unavailable"  # available | stale | unavailable
    quote_timestamp: str | None = None
    ltp: float | None = None
    warnings: list[str] = Field(default_factory=list)
    # ExecutionLegIn-compatible fields for seamless handoff
    symbol: str
    expiration_date: str
    strike_price: float


class ResolutionOut(BaseModel):
    """Response for both POST /paper/resolve and POST /paper/templates/:id/resolve."""

    status: str  # RESOLVED | RESOLVED_WITH_WARNINGS | PARTIAL | NO_PRICES | FAILED
    symbol: str
    legs: list[ResolutionLegOut] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    template_id: int | None = None
    template_name: str | None = None


# ---- Phase 6.9: Dynamic template execution bridge --------------------------------


class TemplateExecuteRequestIn(BaseModel):
    """POST /paper/templates/:id/execute — Execute a V2 dynamic template.

    Server re-resolves all legs against live broker data, validates the
    resolution, and executes atomically. The ``client_order_id`` provides
    idempotency: a retry returns the original execution.
    """

    client_order_id: str = Field(..., min_length=8, max_length=64)
    starting_capital: float | None = Field(default=None, gt=0)
    # When the preview detected changes, the frontend sends the values
    # the user explicitly confirmed so the server can verify they still
    # match the current resolution.
    confirmed_strikes: dict[int, float] | None = None   # position → accepted strike
    confirmed_expiries: dict[int, str] | None = None    # position → accepted expiry


class ResolutionChangeOut(BaseModel):
    """One detected change between preview and fresh execution-time resolution."""

    position: int
    field: str  # "strike" | "expiry"
    preview_value: str | float
    fresh_value: str | float


class TemplateExecutePreviewOut(BaseModel):
    """POST /paper/templates/:id/execute/preview — Pre-execution resolution.

    Read-only: resolves all legs against live chain and compares against
    the last preview. Returns the fresh resolution plus any detected changes.
    """

    status: str  # UNCHANGED | CHANGED_STRIKE | CHANGED_EXPIRY | CHANGED_BOTH | FAILED
    symbol: str
    template_id: int
    legs: list[ResolutionLegOut] = Field(default_factory=list)
    changes: list[ResolutionChangeOut] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
