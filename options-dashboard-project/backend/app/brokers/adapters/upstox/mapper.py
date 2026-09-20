"""Upstox-specific mappings (Phase 6.5.0.2 → Day 10).

EVERY Upstox-specific concept lives here or in ``adapter.py`` — instrument
keys, transaction types, product codes, V3 order field names, chain payload
field names, order status strings. Nothing in this module is imported by
domain/application code except through the adapter boundary (compat
re-exports in the pre-existing broker services are documented there).

Pure functions only: no HTTP, no side effects, deterministic.

Day 10 adds the canonical market-data boundary: raw Upstox quote and
option-chain payloads are normalized HERE into the Day-9 canonical
contracts (``NormalizedInstrument`` / ``PriceQuote`` /
``QuoteObservation`` / ``OptionChainObservation`` /
``GreeksObservation``) so broker-specific structures stop at the adapter.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.brokers.domain.enums import (
    ExecutionPolicy,
    InstrumentType,
    OptionType,
    OrderStatus,
    OrderType,
    Product,
    Segment,
    Side,
    Validity,
)
from app.brokers.domain.errors import BrokerError, BrokerErrorCode
from app.brokers.domain.models import BrokerOrderRequest, BrokerOrderResult, InstrumentIdentity
from app.market_data.contracts import (
    ContractVersion,
    DataMode,
    GreeksObservation,
    NormalizedInstrument,
    OptionChainObservation,
    OptionChainRow,
    PriceQuote,
    Provenance,
    QuoteObservation,
    Side as MarketDataSide,  # market-data CALL/PUT — distinct from broker Side (BUY/SELL)
)

# ---- Instrument master (canonical identity + Upstox keys) --------------------
#
# The platform's canonical symbol → identity + Upstox instrument_key. The
# Upstox key is a BROKER mapping and lives only here (and in the adapter);
# it is never the platform's universal instrument ID.
UPSTOX_INSTRUMENTS: dict[str, dict] = {
    "NIFTY": {
        "exchange": "NSE",
        "segment": Segment.INDEX_DERIVATIVES.value,
        "underlying": "NIFTY",
        "instrument_type": InstrumentType.INDEX.value,
        "broker_instrument_id": "NSE_INDEX|Nifty 50",
    },
    "BANKNIFTY": {
        "exchange": "NSE",
        "segment": Segment.INDEX_DERIVATIVES.value,
        "underlying": "BANKNIFTY",
        "instrument_type": InstrumentType.INDEX.value,
        "broker_instrument_id": "NSE_INDEX|Nifty Bank",
    },
    # Note: Upstox uses older index names for these two ("Nifty Fin
    # Service", "NIFTY MID SELECT"), not the current official NSE names.
    "FINNIFTY": {
        "exchange": "NSE",
        "segment": Segment.INDEX_DERIVATIVES.value,
        "underlying": "FINNIFTY",
        "instrument_type": InstrumentType.INDEX.value,
        "broker_instrument_id": "NSE_INDEX|Nifty Fin Service",
    },
    "MIDCPNIFTY": {
        "exchange": "NSE",
        "segment": Segment.INDEX_DERIVATIVES.value,
        "underlying": "MIDCPNIFTY",
        "instrument_type": InstrumentType.INDEX.value,
        "broker_instrument_id": "NSE_INDEX|NIFTY MID SELECT",
    },
    "NIFTYNXT50": {
        "exchange": "NSE",
        "segment": Segment.INDEX_DERIVATIVES.value,
        "underlying": "NIFTYNXT50",
        "instrument_type": InstrumentType.INDEX.value,
        "broker_instrument_id": "NSE_INDEX|Nifty Next 50",
    },
    "SENSEX": {
        "exchange": "BSE",
        "segment": Segment.INDEX_DERIVATIVES.value,
        "underlying": "SENSEX",
        "instrument_type": InstrumentType.INDEX.value,
        "broker_instrument_id": "BSE_INDEX|SENSEX",
    },
    "BANKEX": {
        "exchange": "BSE",
        "segment": Segment.INDEX_DERIVATIVES.value,
        "underlying": "BANKEX",
        "instrument_type": InstrumentType.INDEX.value,
        "broker_instrument_id": "BSE_INDEX|BANKEX",
    },
    "SENSEX50": {
        "exchange": "BSE",
        "segment": Segment.INDEX_DERIVATIVES.value,
        "underlying": "SENSEX50",
        "instrument_type": InstrumentType.INDEX.value,
        "broker_instrument_id": "BSE_INDEX|SENSEX50",
    },
}

# symbol → Upstox instrument_key (kept under this exact name for the
# pre-existing chain router compatibility re-export).
UPSTOX_INSTRUMENT_KEYS: dict[str, str] = {
    symbol: info["broker_instrument_id"] for symbol, info in UPSTOX_INSTRUMENTS.items()
}

# Margin basket size limit for the Upstox charges/margin endpoint.
UPSTOX_MAX_MARGIN_INSTRUMENTS = 20

# The paper engine simulates HELD positions (no intraday square-off), so F&O
# margin requests use the delivery product. Kept here, broker-side.
UPSTOX_PRODUCT_DEFAULT = "D"


def resolve_instrument_identity(symbol: str) -> InstrumentIdentity:
    """Canonical identity for a known platform symbol (pure lookup).

    Raises BrokerError(INVALID_INSTRUMENT) for unknown symbols. The
    returned identity is the UNDERLYING/index level: expiry/strike/option
    type are filled by callers when a concrete contract is resolved.
    lot_size/tick_size stay None when the platform has no authoritative
    value — never fabricated.
    """
    info = UPSTOX_INSTRUMENTS.get((symbol or "").upper())
    if info is None:
        raise BrokerError(
            BrokerErrorCode.INVALID_INSTRUMENT,
            f"Unknown symbol '{symbol}' — no broker mapping exists.",
        )
    return InstrumentIdentity(
        exchange=info["exchange"],
        segment=info["segment"],
        underlying=info["underlying"],
        symbol=(symbol or "").upper(),
        instrument_type=info["instrument_type"],
    )


def broker_key_for(symbol: str) -> str:
    """Upstox instrument_key for a symbol (raises INVALID_INSTRUMENT when
    unknown). Adapter-internal — never call from domain code."""
    identity = resolve_instrument_identity(symbol)
    return UPSTOX_INSTRUMENT_KEYS[identity.symbol]


# ---- Option-type / side / order-type / validity / product / status maps -------


def option_type_to_domain(option_type) -> OptionType | None:
    """Map a platform/internal option type (call|put, lower or upper, or
    CE/PE) to the canonical CALL | PUT. None/unknown → None (never guessed)."""
    if option_type is None:
        return None
    value = str(getattr(option_type, "value", option_type)).strip().upper()
    if value in ("CALL", "CE", "C"):
        return OptionType.CALL
    if value in ("PUT", "PE", "P"):
        return OptionType.PUT
    return None


def option_type_to_upstox(option_type: str | OptionType | None) -> str | None:
    """Canonical CALL|PUT → the Upstox chain side key (``call`` / ``put``)."""
    mapped = option_type_to_domain(option_type)
    if mapped is None:
        return None
    return "call" if mapped is OptionType.CALL else "put"


def side_to_upstox(side: Side | str) -> str:
    """Canonical BUY/SELL → Upstox ``transaction_type`` (BUY/SELL)."""
    value = Side(side)
    return "BUY" if value is Side.BUY else "SELL"


def order_type_to_upstox(order_type: OrderType | str) -> str:
    """Canonical order type → Upstox V3 ``order_type`` value."""
    value = OrderType(order_type)
    return {
        OrderType.MARKET: "MARKET",
        OrderType.LIMIT: "LIMIT",
        OrderType.STOP_LOSS: "SL",
        OrderType.STOP_LOSS_MARKET: "SL-M",
    }[value]


def validity_to_upstox(validity: Validity | str) -> str:
    """Canonical validity → Upstox V3 ``validity`` (DAY | IOC)."""
    value = Validity(validity)
    return "DAY" if value is Validity.DAY else "IOC"


def product_to_upstox(product: Product | str | None) -> str:
    """Canonical product → Upstox product code (I | D | CO | MTF)."""
    if product is None:
        return UPSTOX_PRODUCT_DEFAULT
    value = Product(product)
    return {
        Product.INTRADAY: "I",
        Product.DELIVERY: "D",
        Product.CO: "CO",
        Product.MTF: "MTF",
    }[value]


# Upstox V3 order status strings → canonical lifecycle.
UPSTOX_ORDER_STATUS_MAP: dict[str, OrderStatus] = {
    "complete": OrderStatus.FILLED,
    "filled": OrderStatus.FILLED,
    "pending": OrderStatus.PENDING,
    "trigger_pending": OrderStatus.PENDING,
    "open": OrderStatus.OPEN,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "partial_fill": OrderStatus.PARTIALLY_FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.EXPIRED,
}


def upstox_status_to_domain(status: str | None) -> OrderStatus:
    """Map an Upstox order status string to the canonical lifecycle.
    Unknown/missing → UNKNOWN (never guessed)."""
    if not status:
        return OrderStatus.UNKNOWN
    return UPSTOX_ORDER_STATUS_MAP.get(str(status).strip().lower(), OrderStatus.UNKNOWN)


# ---- Quantity conversion -------------------------------------------------------


def lots_to_contracts(quantity_lots: int, lot_size: int) -> int:
    """Platform LOTS → broker contract quantity (lots × lot_size).

    Upstox order/margin APIs expect contract units: 1 lot of NIFTY
    (lot_size 65) → 65 contracts. Never pass lots directly to the broker.
    """
    return int(quantity_lots) * int(lot_size)


# ---- Margin request/response payloads (moved from broker_margin service) ------


def build_margin_request_instruments(
    instruments: list[dict], product: str = UPSTOX_PRODUCT_DEFAULT
) -> list[dict]:
    """Build the ``POST /v2/charges/margin`` payload instruments.

    Every instrument carries the resolved Upstox key, the contract quantity
    (lots → contracts), ``transaction_type`` and the product. The full
    multi-leg strategy set goes in ONE request.
    """
    return [
        {
            "instrument_key": inst["instrument_key"],
            "quantity": lots_to_contracts(inst["quantity"], inst["lot_size"]),
            "transaction_type": "BUY" if inst["action"] == "buy" else "SELL",
            "product": product,
        }
        for inst in instruments
    ]


def map_funds_payload(data: dict | None) -> dict:
    """Map the V3 funds response (``data`` object) to the capital contract.

    Keeps broker terminology: available_to_trade, cash available, margin
    used, SPAN+exposure, premium present, pledge available. Missing fields
    stay ``None`` — never fabricated 0. The full raw payload is preserved in
    ``raw`` so future phases never lose broker fields.
    """
    data = data or {}
    available = data.get("available_to_trade") or {}
    cash = available.get("cash_available_to_trade") or {}
    margin_used = cash.get("margin_used") or {}
    pledge = available.get("pledge_available_to_trade") or {}
    pledge_margin_used = pledge.get("margin_used") or {}
    pledge_from = pledge.get("margin_from_pledge") or {}
    unavailable = data.get("unavailable_to_trade") or {}
    unsettled = (unavailable.get("cash_unavailable_to_trade") or {}).get("unsettled_profit") or {}
    delivery = margin_used.get("delivery_margin") or {}
    return {
        "available_to_trade": available.get("total"),
        "cash_available_to_trade": cash.get("total"),
        "margin_used": margin_used.get("total"),
        "span_exposure": margin_used.get("span_exposure"),
        "cash_margin_var_elm": margin_used.get("cash_margin_var_elm"),
        "premium_present": margin_used.get("premium_present"),
        "delivery_margin": delivery.get("total"),
        "pledge_available_to_trade": pledge.get("total"),
        "margin_from_pledge": pledge_from.get("total"),
        "pledge_margin_used": pledge_margin_used.get("total"),
        "unsettled_profit": unsettled.get("todays_profit"),
        "raw": data,
    }


def map_margin_payload(data: dict | None) -> dict:
    """Map the V2 margin response to the capital contract.

    ``required_margin`` (the broker's whole-request figure) is authoritative
    and preserved as-is — the platform never sums SPAN + exposure itself.
    Per-instrument rows are kept separately in ``rows``.
    """
    data = data or {}
    inner = data.get("data") or {}
    rows = inner.get("margins") or []
    return {
        "required_margin": inner.get("required_margin"),
        "final_margin": inner.get("final_margin"),
        "rows": rows,
        "raw": data,
    }


# ---- Option chain canonicalization (moved from the chains router) -------------


def transform_chain(symbol: str, expiry_date: str, raw: dict) -> dict:
    """Canonicalize a raw Upstox chain payload into platform chain rows.

    Reads the Upstox payload shape (``call_options`` / ``put_options`` /
    ``market_data`` / ``option_greeks``) HERE — inside the adapter — and
    returns the canonical chain contract consumed by the app and UI:

        {"symbol", "expiry_date", "underlying_spot_price", "chain": [...]}
    """
    rows = []
    underlying_spot = None

    for item in raw.get("data", []):
        strike = item.get("strike_price")
        if underlying_spot is None:
            underlying_spot = item.get("underlying_spot_price")

        def leg(side_key):
            side = item.get(side_key) or {}
            market = side.get("market_data") or {}
            greeks = side.get("option_greeks") or {}

            oi = market.get("oi")
            prev_oi = market.get("prev_oi")
            chg_oi = (oi - prev_oi) if (oi is not None and prev_oi is not None) else None

            return {
                "ltp": market.get("ltp"),
                "oi": oi,
                "chg_oi": chg_oi,
                "volume": market.get("volume"),
                "quote_timestamp": market.get("last_trade_time") or market.get("last_update_time"),
                "iv": greeks.get("iv"),
                "delta": greeks.get("delta"),
                "theta": greeks.get("theta"),
                "gamma": greeks.get("gamma"),
                "vega": greeks.get("vega"),
                "pop": greeks.get("pop"),
            }

        rows.append({
            "strike": strike,
            "call": leg("call_options"),
            "put": leg("put_options"),
        })

    rows.sort(key=lambda r: r["strike"])

    return {
        "symbol": symbol,
        "expiry_date": expiry_date,
        "underlying_spot_price": underlying_spot,
        "chain": rows,
    }


def contracts_from_payload(raw: dict) -> list[str]:
    """Extract sorted, deduplicated expiries from the option/contract payload."""
    return sorted({c["expiry"] for c in raw.get("data", []) if "expiry" in c})


# ---- V3 order request payload (prepared, NOT wired) ---------------------------


def build_order_request_payload(request: BrokerOrderRequest) -> dict:
    """Build the Upstox V3 place-order payload from a canonical request.

    PURE preparation for the future execution phase — the adapter does NOT
    submit it in this phase. All Upstox field names (``instrument_token``,
    ``transaction_type``, ``is_amo``, ...) appear only here.

    Raises BrokerError(INVALID_QUANTITY) when the instrument identity lacks
    the lot size needed for the lots → contracts conversion.
    """
    if request.instrument.lot_size is None:
        raise BrokerError(
            BrokerErrorCode.INVALID_QUANTITY,
            "Instrument identity has no lot size — cannot convert lots to broker contracts.",
        )
    broker_key = broker_key_for(request.instrument.symbol)
    payload = {
        "instrument_token": broker_key,
        "transaction_type": side_to_upstox(request.side),
        "quantity": lots_to_contracts(request.quantity, request.instrument.lot_size),
        "order_type": order_type_to_upstox(request.order_type),
        "product": product_to_upstox(request.product),
        "validity": validity_to_upstox(request.validity),
        "is_amo": bool(request.after_market),
    }
    if request.price is not None:
        payload["price"] = request.price
    if request.trigger_price is not None:
        payload["trigger_price"] = request.trigger_price
    if request.disclosed_quantity is not None:
        payload["disclosed_quantity"] = request.disclosed_quantity
    if request.market_protection:
        payload["market_protection"] = 1
    if request.client_order_tag:
        payload["tag"] = request.client_order_tag
    # The canonical execution policy is never sent to the broker verbatim;
    # slicing stays a platform-side concern represented by capability +
    # result shape (multiple broker_order_ids), never a payload field.
    return payload


# ---- V3 order response mapping (prepared, NOT wired) --------------------------


def map_order_result(payload: dict | None, broker: str = "UPSTOX") -> BrokerOrderResult:
    """Map an Upstox V3 order response to the canonical order result.

    Supports ONE logical request producing MULTIPLE broker order ids
    (broker-native slicing): a single ``order_id`` string, or a list of
    order objects / ids, or ``data`` shaped either way — all normalize into
    ``broker_order_ids``. Status strings map through
    ``upstox_status_to_domain``; unknown stays UNKNOWN.
    """
    payload = payload or {}
    data = payload.get("data")
    if isinstance(data, dict):
        ids = _extract_order_ids(data.get("order_id"))
        status = upstox_status_to_domain(data.get("status"))
        message = data.get("message")
    elif isinstance(data, list):
        ids = tuple(
            order_id
            for item in data
            for order_id in _extract_order_ids(
                item.get("order_id") if isinstance(item, dict) else item
            )
        )
        status = OrderStatus.UNKNOWN
        message = None
    else:
        ids = _extract_order_ids(payload.get("order_id"))
        status = upstox_status_to_domain(payload.get("status"))
        message = payload.get("message")

    return BrokerOrderResult(
        broker=broker,
        broker_order_ids=ids,
        status=status,
        client_order_id=data.get("client_order_id") if isinstance(data, dict) else payload.get("client_order_id"),
        message=message,
        accepted_at=data.get("timestamp") if isinstance(data, dict) else None,
    )


def _extract_order_ids(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if v is not None)
    return (str(value),)


# ---------------------------------------------------------------------------
# Day 10 — Canonical market-data normalization (REST quote + option chain)
# ---------------------------------------------------------------------------
#
# The functions below are the adapter's canonicalization boundary: they read
# Upstox payload field names ONLY here and return Day-9 canonical market-data
# contracts. Everything below this line is pure (no HTTP, no side effects).

# Normalization/transformation version for payloads produced by this mapper.
NORMALIZATION_VERSION = "1.0.0"

# Canonical label applied to every normalized price payload's ``source``.
SOURCE_LABEL = "UPSTOX"

# Upstox quote timestamp keys, in preference order (first present wins).
_QUOTE_MARKET_TS_KEYS = ("last_trade_time", "timestamp")

# Upstox option-chain leg event-timestamp keys (Issue #80 cutoff integrity):
# ``market_data.last_trade_time`` (epoch-ms) is the exchange event time of
# the leg's quote state; ``last_update_time`` is the feed's last update.
# First present wins; both normalize to UTC via ``upstox_timestamp_to_datetime``.
_CHAIN_LEG_TS_KEYS = ("last_trade_time", "last_update_time")


def instrument_identity_to_normalized(
    identity: InstrumentIdentity,
) -> NormalizedInstrument:
    """Bridge the broker-layer :class:`InstrumentIdentity` (used by order /
    chain code) to the market-data :class:`NormalizedInstrument` (Day 9).

    No third identity model is created: this is a pure conversion between
    the two existing canonical vocabularies. ``option_type`` is mapped to
    the market-data ``Side`` enum; missing values stay ``None``.
    """
    option_type = None
    if identity.option_type is not None:
        mapped = option_type_to_domain(identity.option_type)
        if mapped is OptionType.CALL:
            option_type = MarketDataSide.CALL
        elif mapped is OptionType.PUT:
            option_type = MarketDataSide.PUT
    return NormalizedInstrument(
        exchange=identity.exchange,
        segment=identity.segment,
        underlying=identity.underlying,
        symbol=identity.symbol,
        instrument_type=identity.instrument_type,
        expiry=identity.expiry,
        strike=identity.strike,
        option_type=option_type,
        lot_size=identity.lot_size,
        tick_size=identity.tick_size,
    )


def upstox_timestamp_to_datetime(value: str | int | None) -> datetime | None:
    """Parse an Upstox timestamp into a UTC :class:`datetime`.

    Upstox returns timestamps in two shapes:

    * epoch milliseconds (``last_trade_time``), e.g. ``"1697624972130"``;
    * ISO-8601 with an offset (``timestamp``), e.g.
      ``"2023-10-19T05:21:51.099+05:30"``.

    Both normalize deterministically to UTC. Unparseable / missing input
    returns ``None`` (never raises, never guessed).
    """
    if value is None or value == "":
        return None
    text = str(value).strip()

    # Epoch milliseconds: purely numeric string.
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    # ISO-8601 (optionally with +NN:NN offset / Z).
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def upstox_quote_to_price_quote(raw: dict) -> PriceQuote:
    """Map a raw Upstox market-quote payload to a canonical :class:`PriceQuote`.

    Field mapping (Upstox names on the left):

    * ``last_price`` → ``ltp``
    * ``ohlc.open/high/low/close`` → OHLC
    * ``depth.buy[0].price/quantity`` → best bid / bid quantity
    * ``depth.sell[0].price/quantity`` → best ask / ask quantity
    * ``volume``, ``oi`` → preserved as-is (OI is in contracts, never
      converted to lots)

    Missing fields stay ``None`` — never fabricated to zero. A payload with
    no ``last_price`` is malformed and raises
    ``BrokerError(INVALID_MARKET_DATA)``.
    """
    ltp = raw.get("last_price")
    if ltp is None:
        raise BrokerError(
            BrokerErrorCode.INVALID_MARKET_DATA,
            "Upstox quote payload has no last_price — cannot normalize.",
        )

    ohlc = raw.get("ohlc") or {}
    depth = raw.get("depth") or {}
    buy = depth.get("buy") or []
    sell = depth.get("sell") or []
    best_bid = buy[0] if buy else {}
    best_ask = sell[0] if sell else {}

    bid_price = best_bid.get("price")
    ask_price = best_ask.get("price")

    return PriceQuote(
        ltp=float(ltp),
        open=_optional_float(ohlc.get("open")),
        high=_optional_float(ohlc.get("high")),
        low=_optional_float(ohlc.get("low")),
        close=_optional_float(ohlc.get("close")),
        bid=_optional_float(bid_price),
        ask=_optional_float(ask_price),
        bid_quantity=_optional_int(best_bid.get("quantity")),
        ask_quantity=_optional_int(best_ask.get("quantity")),
        volume=_optional_float(raw.get("volume")),
        oi=_optional_float(raw.get("oi")),
        source=SOURCE_LABEL,
    )


def upstox_quote_to_observation(
    raw_quote: dict,
    instrument: NormalizedInstrument,
    *,
    received_at: datetime,
) -> QuoteObservation:
    """Wrap a single raw Upstox market quote into a canonical
    :class:`QuoteObservation` with dual timestamps and provenance.

    ``market_timestamp`` prefers the exchange event time (``last_trade_time``
    epoch-ms, falling back to the feed ``timestamp`` ISO). ``received_at`` is
    the application receive time — never conflated with the market time.

    Raises ``BrokerError(INVALID_MARKET_DATA)`` for malformed quotes (no
    price).
    """
    price = upstox_quote_to_price_quote(raw_quote)

    market_timestamp = None
    for key in _QUOTE_MARKET_TS_KEYS:
        candidate = upstox_timestamp_to_datetime(raw_quote.get(key))
        if candidate is not None:
            market_timestamp = candidate
            break

    provenance = Provenance(
        source=SOURCE_LABEL,
        collection_mode=DataMode.BROKER_SNAPSHOT.value,
        received_at=received_at,
        normalization_version=NORMALIZATION_VERSION,
        contract_version=ContractVersion.v1_0_0.value,
        transformation_id="upstox_market_quote_v1",
    )

    return QuoteObservation(
        instrument=instrument,
        quote=price,
        market_timestamp=market_timestamp,
        received_timestamp=received_at,
        source=SOURCE_LABEL,
        data_mode=DataMode.BROKER_SNAPSHOT,
        provenance=provenance,
        contract_version=ContractVersion.v1_0_0,
    )


def _optional_float(value) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_int(value) -> int | None:
    if value is None:
        return None
    return int(value)


def _chain_leg_to_price_quote(side_key: str, item: dict) -> PriceQuote | None:
    """Map one Upstox chain leg (call/put side) to a canonical PriceQuote.

    The leg is ``item[side_key]`` with ``market_data`` and ``option_greeks``
    nested objects. ``ltp`` is the required canonical price field, so a leg
    with no LTP is represented as absent (``None``) rather than fabricated
    with a zero price — the broker payload cannot be canonically priced.

    Issue #80: best bid/ask + quantities (``market_data``) and broker-reported
    IV/delta/gamma (``option_greeks``) are mapped when present. Broker IV is
    reported as a percentage (e.g. 12.5 = 12.5%) and is stored canonically as
    a decimal fraction (0.125); delta/gamma are dimensionless pass-through.
    Vega/theta are deliberately NOT mapped (unverified broker unit
    conventions — silently converting units would fabricate semantics).
    Missing values stay ``None`` — never fabricated to 0.

    The leg's exchange event timestamp (``market_data.last_trade_time``
    epoch-ms, falling back to ``last_update_time``) maps to
    ``event_timestamp`` — research capture refuses snapshots that cannot
    prove their observation time, so a leg with no parseable event time
    makes the whole chain observation ineligible for capture (see
    ``upstox_chain_to_observation``).
    """
    side = item.get(side_key) or {}
    market = side.get("market_data") or {}
    greeks = side.get("option_greeks") or {}
    ltp = market.get("ltp")
    if ltp is None:
        return None
    iv = _optional_float(greeks.get("iv"))
    event_timestamp = None
    for ts_key in _CHAIN_LEG_TS_KEYS:
        event_timestamp = upstox_timestamp_to_datetime(market.get(ts_key))
        if event_timestamp is not None:
            break
    return PriceQuote(
        ltp=float(ltp),
        volume=_optional_float(market.get("volume")),
        oi=_optional_float(market.get("oi")),
        bid=_optional_float(market.get("bid_price")),
        ask=_optional_float(market.get("ask_price")),
        bid_quantity=_optional_int(market.get("bid_qty")),
        ask_quantity=_optional_int(market.get("ask_qty")),
        # percentage -> decimal fraction (see docstring)
        iv=(iv / 100.0) if iv is not None else None,
        delta=_optional_float(greeks.get("delta")),
        gamma=_optional_float(greeks.get("gamma")),
        event_timestamp=event_timestamp,
        source=SOURCE_LABEL,
    )


def upstox_chain_to_observation(
    symbol: str,
    expiry_date: str,
    raw: dict,
    *,
    received_at: datetime,
) -> OptionChainObservation:
    """Normalize a raw Upstox option-chain payload into a canonical
    :class:`OptionChainObservation` (Day 9).

    CE and PE legs are independent: a row with only one populated side keeps
    the other ``None``. OI is preserved as reported (contracts, not lots).
    Rows without a ``strike_price`` are skipped (malformed row, not fatal).
    The chain is sorted by strike ascending.

    Issue #80 cutoff integrity: the observation-level ``market_timestamp``
    is the latest exchange event time actually carried by the payload's
    legs (``market_data.last_trade_time`` / ``last_update_time``), NOT the
    receive time.  When the payload carries no parseable event time the
    observation is returned with ``market_timestamp=None`` and no leg
    timestamps — research capture then refuses it, because receive time
    cannot prove the observed state existed at or before a research
    cutoff.
    """
    rows: list[OptionChainRow] = []
    underlying_spot = None
    leg_event_times: list[datetime] = []

    for item in raw.get("data", []):
        if not isinstance(item, dict):
            continue
        strike = item.get("strike_price")
        if strike is None:
            continue
        if underlying_spot is None:
            spot = item.get("underlying_spot_price")
            underlying_spot = _optional_float(spot)
        call_leg = _chain_leg_to_price_quote("call_options", item)
        put_leg = _chain_leg_to_price_quote("put_options", item)
        for leg in (call_leg, put_leg):
            if leg is not None and leg.event_timestamp is not None:
                leg_event_times.append(leg.event_timestamp)
        rows.append(
            OptionChainRow(
                strike=float(strike),
                call=call_leg,
                put=put_leg,
            )
        )

    rows.sort(key=lambda row: row.strike)

    return OptionChainObservation(
        symbol=symbol,
        expiry_date=expiry_date,
        underlying_spot_price=underlying_spot,
        chain=rows,
        # broker event time (max across legs) — never the receive time
        market_timestamp=max(leg_event_times) if leg_event_times else None,
        received_timestamp=received_at,
        source=SOURCE_LABEL,
        data_mode=DataMode.BROKER_SNAPSHOT,
        contract_version=ContractVersion.v1_0_0,
    )


def upstox_chain_to_broker_greeks(
    raw: dict,
) -> dict[tuple[float, str], GreeksObservation]:
    """Extract broker-provided option Greeks from a raw Upstox chain payload.

    Returns a mapping ``(strike, "CALL"|"PUT") → GreeksObservation`` with
    ``source="BROKER"``. Broker Greeks are preserved as broker values — they
    are never relabelled as model values, never merged with a model
    calculation. Only IV is commonly supplied by Upstox; other fields stay
    ``None`` when absent.
    """
    result: dict[tuple[float, str], GreeksObservation] = {}
    for item in raw.get("data", []):
        if not isinstance(item, dict):
            continue
        strike = item.get("strike_price")
        if strike is None:
            continue
        for side_key, label in (("call_options", "CALL"), ("put_options", "PUT")):
            side = item.get(side_key) or {}
            greeks = side.get("option_greeks") or {}
            if not greeks:
                continue
            result[(float(strike), label)] = GreeksObservation(
                iv=_optional_float(greeks.get("iv")),
                delta=_optional_float(greeks.get("delta")),
                gamma=_optional_float(greeks.get("gamma")),
                theta=_optional_float(greeks.get("theta")),
                vega=_optional_float(greeks.get("vega")),
                source="BROKER",
                calc_model=None,
                calc_version=None,
            )
    return result
