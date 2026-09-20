"""FYERS-specific mappings (adapter-boundary, pure functions).

EVERY FYERS-specific concept lives here or in ``adapter.py`` / the raw
client (``app.services.fyers``) — FYERS symbol grammar, short-field quote
payloads, ``callOILtP``/``putOILtP`` chain keys, order status strings,
product codes. Nothing in this module is imported by domain/application
code except through the adapter boundary.

Pure functions only: no HTTP, no side effects, deterministic. Missing
values stay ``None`` — never fabricated into 0.

FYERS payload notes (implemented mappings)
------------------------------------------
* Quotes (``POST /data/quotes``): ``d`` is keyed by FYERS symbol; each
  quote uses SHORT field names — ``v`` volume, ``lp`` last traded price,
  ``pc`` % change, ``bid``/``ask``, ``open``/``high``/``low``/``prev_close_price``.
* Option chain (``POST /data/options-chain-v3``): each row carries
  ``strike_price``, ``putLtp``/``callLtp``, ``putOICoynt``/``callOICoynt``
  (contracts), ``putSym``/``callSym`` and Greek pairs
  (``putIV``/``callIV``, ``putDelta``/``callDelta``, ...) plus
  ``underlying``/``fyers_ord_live_price`` spot fields.
* Identity: the customer Login ID mapping is isolated in
  ``app.brokers.adapters.fyers.profile`` — never here, never the App ID.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.brokers.adapters.upstox.mapper import (  # canonical shared helpers
    instrument_identity_to_normalized,
)
from app.brokers.domain.capabilities import CapabilityState
from app.brokers.domain.enums import (
    InstrumentType,
    OrderStatus,
    OptionType,
    Product,
    Segment,
    Side,
)
from app.brokers.domain.errors import BrokerError, BrokerErrorCode
from app.brokers.domain.models import BrokerOrderRequest, InstrumentIdentity
from app.market_data.contracts import (
    ContractVersion,
    DataMode,
    OptionChainObservation,
    OptionChainRow,
    PriceQuote,
    Provenance,
    QuoteObservation,
)

# ---- Instrument master (canonical identity + FYERS symbols) -------------------
#
# The FYERS symbol grammar (NSE:NIFTY50-INDEX, NSE:NIFTY25OCT24500CE) is a
# BROKER mapping and lives only here — never the platform's universal
# instrument ID. Index names differ from Upstox's (FYERS uses the official
# NSE names where Upstox uses legacy ones).
FYERS_INSTRUMENTS: dict[str, dict] = {
    "NIFTY": {
        "exchange": "NSE",
        "segment": Segment.INDEX_DERIVATIVES.value,
        "underlying": "NIFTY",
        "instrument_type": InstrumentType.INDEX.value,
        "broker_instrument_id": "NSE:NIFTY50-INDEX",
    },
    "BANKNIFTY": {
        "exchange": "NSE",
        "segment": Segment.INDEX_DERIVATIVES.value,
        "underlying": "BANKNIFTY",
        "instrument_type": InstrumentType.INDEX.value,
        "broker_instrument_id": "NSE:NIFTYBANK-INDEX",
    },
    "FINNIFTY": {
        "exchange": "NSE",
        "segment": Segment.INDEX_DERIVATIVES.value,
        "underlying": "FINNIFTY",
        "instrument_type": InstrumentType.INDEX.value,
        "broker_instrument_id": "NSE:FINNIFTY-INDEX",
    },
    "MIDCPNIFTY": {
        "exchange": "NSE",
        "segment": Segment.INDEX_DERIVATIVES.value,
        "underlying": "MIDCPNIFTY",
        "instrument_type": InstrumentType.INDEX.value,
        "broker_instrument_id": "NSE:MIDCPNIFTY-INDEX",
    },
    "SENSEX": {
        "exchange": "BSE",
        "segment": Segment.INDEX_DERIVATIVES.value,
        "underlying": "SENSEX",
        "instrument_type": InstrumentType.INDEX.value,
        "broker_instrument_id": "BSE:SENSEX-INDEX",
    },
    "BANKEX": {
        "exchange": "BSE",
        "segment": Segment.INDEX_DERIVATIVES.value,
        "underlying": "BANKEX",
        "instrument_type": InstrumentType.INDEX.value,
        "broker_instrument_id": "BSE:BANKEX-INDEX",
    },
}

# Canonical option-type → FYERS option-side letter (CE/PE in the symbol).
OPTION_TYPE_TO_FYERS = {OptionType.CALL: "CE", OptionType.PUT: "PE"}

# Canonical month number → FYERS contract-month label (1-based).
_FYERS_MONTHS = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)


def resolve_instrument_identity(symbol: str) -> InstrumentIdentity:
    """Canonical identity for a known platform symbol (pure lookup).

    Raises BrokerError(INVALID_INSTRUMENT) for unknown symbols — the
    FYERS adapter keeps its own static master so a wrong Upstox mapping
    can never leak in.
    """
    info = FYERS_INSTRUMENTS.get((symbol or "").upper())
    if info is None:
        raise BrokerError(
            BrokerErrorCode.INVALID_INSTRUMENT,
            f"Unknown symbol '{symbol}' — no FYERS mapping exists.",
        )
    return InstrumentIdentity(
        exchange=info["exchange"],
        segment=info["segment"],
        underlying=info["underlying"],
        symbol=(symbol or "").upper(),
        instrument_type=info["instrument_type"],
    )


def fyers_symbol_for(identity: InstrumentIdentity) -> str:
    """FYERS symbol for a canonical identity.

    Underlying/index identities use the static master. Concrete option
    contracts build the FYERS grammar
    ``{EXCHANGE}:{SYM}{YY}{MMM}{STRIKE}{CE|PE}`` (monthly-contract
    shape). A concrete contract missing expiry/strike/type components is
    INVALID_INSTRUMENT — never a fabricated symbol.
    """
    info = FYERS_INSTRUMENTS.get(identity.symbol)
    if info is None:
        raise BrokerError(
            BrokerErrorCode.INVALID_INSTRUMENT,
            f"Unknown symbol '{identity.symbol}' — no FYERS mapping exists.",
        )
    if not identity.is_concrete_contract:
        return info["broker_instrument_id"]

    expiry = (identity.expiry or "").strip()
    if len(expiry) < 10 or expiry[4] != "-" or expiry[7] != "-":
        raise BrokerError(
            BrokerErrorCode.INVALID_INSTRUMENT,
            f"Invalid expiry '{identity.expiry}' — expected YYYY-MM-DD.",
        )
    year_two = expiry[2:4]
    month = _FYERS_MONTHS[int(expiry[5:7]) - 1]
    option_letter = OPTION_TYPE_TO_FYERS.get(
        OptionType(str(identity.option_type).upper())
        if str(identity.option_type).upper() in OptionType.__members__
        else OptionType(identity.option_type)
    )
    if option_letter is None:
        raise BrokerError(
            BrokerErrorCode.INVALID_INSTRUMENT,
            f"Invalid option type '{identity.option_type}' — expected CALL/PUT.",
        )
    strike = int(identity.strike) if float(identity.strike).is_integer() else identity.strike
    return f"{identity.exchange}:{identity.symbol}{year_two}{month}{strike}{option_letter}"


def fyers_symbol_for_platform_symbol(symbol: str) -> str:
    """FYERS symbol for a platform underlying symbol (pure convenience)."""
    return fyers_symbol_for(resolve_instrument_identity(symbol))


def instrument_symbol_from_fyers(fyers_symbol: str) -> str | None:
    """Reverse-map a FYERS symbol to its platform symbol when known.

    Underlying/index symbols resolve through the static master; option
    symbols parse the ``{SYM}{YY}{MMM}{STRIKE}{CE|PE}`` tail against the
    known underlyings. Unknown → ``None`` (never guessed).
    """
    text = (fyers_symbol or "").strip().upper()
    if not text:
        return None
    for symbol, info in FYERS_INSTRUMENTS.items():
        if text == info["broker_instrument_id"].upper():
            return symbol
    # Option-symbol tail parse: SYM + YYMMM + strike + CE/PE.
    tail = text.split(":", 1)[-1]
    for length in (10, 9):  # MIDCPNIFTY first, then shorter underlyings
        if len(tail) <= length:
            continue
        candidate = tail[:length]
        if candidate not in FYERS_INSTRUMENTS:
            continue
        remainder = tail[length:]
        if len(remainder) < 8:
            continue
        month_label = remainder[2:5]
        if month_label not in _FYERS_MONTHS:
            continue
        return candidate
    return None


# ---- Order-side / type / validity / product / status maps ---------------------


def side_to_fyers(side: Side | str) -> int:
    """Canonical BUY/SELL → FYERS numeric side (1 = BUY, -1 = SELL)."""
    value = Side(side)
    return 1 if value is Side.BUY else -1


def product_to_fyers(product: Product | str | None) -> str:
    """Canonical product → FYERS product code (M = margin/intraday,
    C = CNC/delivery, B = BO). Default M (margin) matches the platform's
    intraday-oriented F&O usage."""
    if product is None:
        return "M"
    value = Product(product)
    return {
        Product.INTRADAY: "M",
        Product.DELIVERY: "C",
        Product.CO: "B",
        Product.MTF: "M",
    }.get(value, "M")


def validity_to_fyers(validity: str | None) -> str:
    """Canonical validity → FYERS validity code (DAY/IOC)."""
    return "IOC" if str(validity or "").upper() == "IOC" else "DAY"


def order_type_to_fyers(order_type) -> int:
    """Canonical order type → FYERS numeric type
    (1 = LIMIT, 2 = MARKET, 3 = STOP (SL), 4 = STOP-MARKET (SL-M))."""
    value = order_type
    name = str(getattr(value, "value", value)).upper()
    return {
        "LIMIT": 1,
        "MARKET": 2,
        "SL": 3,
        "STOP_LOSS": 3,
        "SL-M": 4,
        "STOP_LOSS_MARKET": 4,
    }.get(name, 2)


FYERS_ORDER_STATUS_MAP: dict[str, OrderStatus] = {
    "1": OrderStatus.CANCELLED,
    "1.5": OrderStatus.PENDING,          # trig pending
    "2": OrderStatus.PENDING,            # pending / new
    "3": OrderStatus.UNKNOWN,            # not-confirmed edge
    "4": OrderStatus.PENDING,            # pending for confirmations
    "5": OrderStatus.PENDING,            # part confirmed
    "6": OrderStatus.FILLED,             # fully filled (6 = 2 in equities docs)
    "7": OrderStatus.REJECTED,
    "8": OrderStatus.PENDING,
    "9": OrderStatus.UNKNOWN,
    "10": OrderStatus.UNKNOWN,
    "filled": OrderStatus.FILLED,
    "complete": OrderStatus.FILLED,
    "rejected": OrderStatus.REJECTED,
    "cancelled": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
    "part_filled": OrderStatus.PARTIALLY_FILLED,
}


def fyers_status_to_domain(status) -> OrderStatus:
    """Map a FYERS order status code/string to the canonical lifecycle.

    Unknown/missing → UNKNOWN (never guessed).
    """
    if status is None:
        return OrderStatus.UNKNOWN
    key = str(status).strip()
    if not key:
        return OrderStatus.UNKNOWN
    mapped = FYERS_ORDER_STATUS_MAP.get(key)
    if mapped is not None:
        return mapped
    lowered = key.lower()
    if "part" in lowered:
        return OrderStatus.PARTIALLY_FILLED
    if "fill" in lowered or "complete" in lowered:
        return OrderStatus.FILLED
    if "reject" in lowered:
        return OrderStatus.REJECTED
    if "cancel" in lowered:
        return OrderStatus.CANCELLED
    if "pend" in lowered or "trig" in lowered or "open" in lowered:
        return OrderStatus.PENDING
    return OrderStatus.UNKNOWN


# ---- Payload normalization -----------------------------------------------------


def _optional_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def map_funds_payload(body: dict | None) -> dict:
    """Map the FYERS funds response to the capital contract shape.

    FYERS ``fund_limit`` rows are short-keyed: ``equityAmount`` /
    ``id``-labeled entries carry available balance, utilized amounts
    (span, option premium, exposure), realized/unrealized profits and
    clearing amounts. Each row is normalized into the canonical keys;
    missing values stay ``None``; the raw body is preserved under ``raw``.
    """
    body = body or {}
    rows = body.get("fund_limit") if isinstance(body.get("fund_limit"), list) else []
    by_id: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("id") or "").strip().lower()
        if key:
            by_id[key] = row

    def _amount(row_id: str, field: str = "equityAmount") -> float | None:
        row = by_id.get(row_id)
        if not row:
            return None
        return _optional_float(row.get(field))

    return {
        "available_balance": _amount("total_balance"),
        "available_to_trade": _amount("total_balance"),
        "margin_used": _amount("utilized_amount"),
        "span_exposure": _amount("utilized_amount"),
        "realized_profit": _amount("realized_profit"),
        "unrealized_profit": _amount("unrealized_profit"),
        "clearing_balance": _amount("clearing_balance"),
        "collateral": _amount("payin_amount"),
        "rows": rows,
        "raw": body,
    }


def normalize_fyers_position(raw_item: dict) -> dict:
    """Normalize one FYERS position row into broker-neutral form.

    FYERS position short fields: ``netQty`` (signed), ``avgPrice``,
    ``ltp``, ``pl``, ``buyAvg``/``sellAvg``, ``buyQty``/``sellQty``,
    ``productType``, ``sym``/``symbol``, ``exchange``, ``side``.
    Quantity is in broker contract units (NOT platform LOTS); lot-size
    conversion is the caller's concern via the instrument master.
    Non-position rows (no quantity and no symbol) normalize to ``{}``.
    """
    if not isinstance(raw_item, dict) or not raw_item:
        return {}
    if raw_item.get("netQty") is None and not (raw_item.get("sym") or raw_item.get("symbol")):
        return {}
    return {
        "broker_id": "FYERS",
        "instrument_token": raw_item.get("fyToken") or raw_item.get("id"),
        "exchange": raw_item.get("exchange"),
        "product": raw_item.get("productType"),
        "quantity": _optional_int(raw_item.get("netQty")),
        "average_price": _optional_float(raw_item.get("avgPrice") or raw_item.get("buyAvg")),
        "last_price": _optional_float(raw_item.get("ltp")),
        "pnl": _optional_float(raw_item.get("pl") or raw_item.get("orgPnl")),
        "unrealised": _optional_float(raw_item.get("pl")),
        "realised": _optional_float(raw_item.get("realized_profit")),
        "buy_value": _optional_float(raw_item.get("buyVal")),
        "sell_value": _optional_float(raw_item.get("sellVal")),
        "tradingsymbol": raw_item.get("sym") or raw_item.get("symbol"),
        "day_buy_quantity": _optional_int(raw_item.get("buyQty")),
        "day_sell_quantity": _optional_int(raw_item.get("sellQty")),
        "raw_side": raw_item.get("side"),
    }


def normalize_fyers_holding(raw_item: dict) -> dict:
    """Normalize one FYERS holdings row (``holdingType``, ``quantity``,
    ``costPrice``, ``ltp``, ``isy`, ``symbol``) into broker-neutral form."""
    if not isinstance(raw_item, dict) or not raw_item:
        return {}
    return {
        "broker_id": "FYERS",
        "exchange": raw_item.get("exchange"),
        "quantity": _optional_int(raw_item.get("quantity")),
        "average_price": _optional_float(raw_item.get("costPrice")),
        "last_price": _optional_float(raw_item.get("ltp")),
        "pnl": _optional_float(raw_item.get("pl")),
        "holding_type": raw_item.get("holdingType"),
        "isin": raw_item.get("isin"),
        "tradingsymbol": raw_item.get("symbol") or raw_item.get("sym"),
    }


def normalize_fyers_trade(raw_item: dict) -> dict:
    """Normalize one FYERS tradebook row into broker-neutral form."""
    if not isinstance(raw_item, dict) or not raw_item:
        return {}
    return {
        "broker_id": "FYERS",
        "trade_id": raw_item.get("id"),
        "order_id": raw_item.get("ordId"),
        "exchange": raw_item.get("exchange"),
        "instrument_token": raw_item.get("fyToken"),
        "side": "BUY" if str(raw_item.get("side")) == "1" else ("SELL" if str(raw_item.get("side")) == "-1" else raw_item.get("side")),
        "quantity": _optional_int(raw_item.get("tradedQty") or raw_item.get("qty")),
        "trade_price": _optional_float(raw_item.get("tradedPrice") or raw_item.get("trdPrice")),
        "trade_value": _optional_float(raw_item.get("tradeValue")),
        "trade_date": raw_item.get("tradeDate") or raw_item.get("orderDateTime"),
        "tradingsymbol": raw_item.get("sym") or raw_item.get("symbol"),
    }


def map_order_result_rows(body: dict | None, broker: str = "FYERS") -> list:
    """Map FYERS order-book payloads to canonical order results.

    Handles the ``orderBook`` list shape and single-order responses.
    One FYERS order → one canonical result; status strings map through
    ``fyers_status_to_domain``; unknown stays UNKNOWN.
    """
    from app.brokers.domain.models import BrokerOrderResult

    body = body or {}
    rows = body.get("orderBook") if isinstance(body.get("orderBook"), list) else []
    if not rows and isinstance(body.get("data"), list):
        rows = body["data"]
    results: list[BrokerOrderResult] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        results.append(
            BrokerOrderResult(
                broker=broker,
                broker_order_ids=(str(row.get("id")),) if row.get("id") is not None else (),
                status=fyers_status_to_domain(row.get("status")),
                client_order_id=row.get("orderNumTag") if isinstance(row.get("orderNumTag"), str) else None,
                message=row.get("message"),
                accepted_at=row.get("orderDateTime"),
                metadata={"raw_status": row.get("status")},
            )
        )
    return results


# ---- Quotes / chain normalization (canonical Day-9 contracts) ------------------

# Normalization/transformation version for payloads produced by this mapper.
NORMALIZATION_VERSION = "1.0.0"

# Canonical label applied to every normalized price payload's ``source``.
SOURCE_LABEL = "FYERS"


def fyers_quote_to_price_quote(raw: dict) -> PriceQuote:
    """Map a raw FYERS quote (short fields) to a canonical PriceQuote.

    ``lp`` is the required canonical price field; a payload without it is
    malformed → ``BrokerError(INVALID_MARKET_DATA)``.
    """
    ltp = raw.get("lp")
    if ltp is None:
        raise BrokerError(
            BrokerErrorCode.INVALID_MARKET_DATA,
            "FYERS quote payload has no lp (last traded price) — cannot normalize.",
        )
    return PriceQuote(
        ltp=float(ltp),
        open=_optional_float(raw.get("open")),
        high=_optional_float(raw.get("high")),
        low=_optional_float(raw.get("low")),
        close=_optional_float(raw.get("prev_close_price")),
        bid=_optional_float(raw.get("bid")),
        ask=_optional_float(raw.get("ask")),
        bid_quantity=_optional_int(raw.get("bidQty")),
        ask_quantity=_optional_int(raw.get("askQty")),
        volume=_optional_float(raw.get("v")),
        oi=_optional_float(raw.get("oi")),
        source=SOURCE_LABEL,
    )


def fyers_quote_to_observation(
    raw_quote: dict,
    instrument,
    *,
    received_at: datetime,
) -> QuoteObservation:
    """Wrap one raw FYERS quote into a canonical QuoteObservation.

    FYERS v3 quote payloads do not expose an exchange event timestamp in
    the REST body, so ``market_timestamp`` stays ``None`` (never
    synthesized); ``received_timestamp`` is the application receive time.
    """
    price = fyers_quote_to_price_quote(raw_quote)
    provenance = Provenance(
        source=SOURCE_LABEL,
        collection_mode=DataMode.BROKER_SNAPSHOT.value,
        received_at=received_at,
        normalization_version=NORMALIZATION_VERSION,
        contract_version=ContractVersion.v1_0_0.value,
        transformation_id="fyers_market_quote_v1",
    )
    return QuoteObservation(
        instrument=instrument,
        quote=price,
        market_timestamp=None,
        received_timestamp=received_at,
        source=SOURCE_LABEL,
        data_mode=DataMode.BROKER_SNAPSHOT,
        provenance=provenance,
        contract_version=ContractVersion.v1_0_0,
    )


def fyers_chain_to_observation(
    symbol: str,
    expiry_date: str,
    raw: dict,
    *,
    received_at: datetime,
) -> OptionChainObservation:
    """Normalize a FYERS options-chain-v3 payload into a canonical
    OptionChainObservation (Day-9 contract).

    Row keys: ``strike_price``, ``callLtp``/``putLtp``, volume/OI pairs
    (``callVolume``/``putVolume``, ``callOICoynt``/``putOICoynt`` —
    contracts, never converted to lots), best-quote pairs
    (``callBidPrice``/``callAskPrice``, ``callBidQty``/``callAskQty"",
    and the ``put`` equivalents) and analytics pairs
    (``callIV``/``putIV``, ``callDelta``/``putDelta``,
    ``callGamma``/``putGamma`` — mapped when the payload carries them;
    FYERS has shipped chain payloads both with and without these fields,
    so every analytics field is optional and stays ``None`` when absent —
    missing is never fabricated). CE and PE legs are independent: a leg
    without LTP is absent (``None``), never zero.
    Rows without a strike are skipped (malformed row, not fatal).
    """
    rows: list[OptionChainRow] = []
    underlying_spot = None

    data = raw.get("data") if isinstance(raw, dict) else None
    data = data if isinstance(data, dict) else raw if isinstance(raw, dict) else {}
    body_rows = data.get("callputltp") if isinstance(data, dict) else None
    body_rows = body_rows if isinstance(body_rows, list) else []

    if underlying_spot is None:
        underlying_spot = _optional_float(
            data.get("underlying") if isinstance(data, dict) else None
        )

    def _leg(prefix: str, item: dict) -> PriceQuote | None:
        ltp = item.get(f"{prefix}Ltp")
        if ltp is None:
            return None
        iv = _optional_float(item.get(f"{prefix}IV"))
        return PriceQuote(
            ltp=float(ltp),
            volume=_optional_float(item.get(f"{prefix}Volume")),
            oi=_optional_float(item.get(f"{prefix}OICoynt")),
            bid=_optional_float(item.get(f"{prefix}BidPrice")),
            ask=_optional_float(item.get(f"{prefix}AskPrice")),
            bid_quantity=_optional_int(item.get(f"{prefix}BidQty")),
            ask_quantity=_optional_int(item.get(f"{prefix}AskQty")),
            # Broker IV is reported as a percentage (e.g. 12.5 = 12.5%);
            # the canonical contract stores a decimal fraction (0.125).
            iv=(iv / 100.0) if iv is not None else None,
            delta=_optional_float(item.get(f"{prefix}Delta")),
            gamma=_optional_float(item.get(f"{prefix}Gamma")),
            source=SOURCE_LABEL,
        )

    for item in body_rows:
        if not isinstance(item, dict):
            continue
        strike = item.get("strike_price")
        if strike is None:
            continue
        if underlying_spot is None:
            underlying_spot = _optional_float(item.get("underlying")) or _optional_float(
                item.get("fyers_ord_live_price")
            )
        rows.append(
            OptionChainRow(
                strike=float(strike),
                call=_leg("call", item),
                put=_leg("put", item),
            )
        )

    rows.sort(key=lambda row: row.strike)
    return OptionChainObservation(
        symbol=symbol,
        expiry_date=expiry_date,
        underlying_spot_price=underlying_spot,
        chain=rows,
        received_timestamp=received_at,
        source=SOURCE_LABEL,
        data_mode=DataMode.BROKER_SNAPSHOT,
        contract_version=ContractVersion.v1_0_0,
    )


def contracts_from_payload(raw: dict) -> list[str]:
    """Extract sorted, deduplicated expiries from a FYERS chain payload.

    Reads the same ``data.callputltp`` rows the chain normalizer reads;
    ``expiry``/``date`` row fields are normalized to ``YYYY-MM-DD``.
    """
    data = raw.get("data") if isinstance(raw, dict) else None
    data = data if isinstance(data, dict) else raw if isinstance(raw, dict) else {}
    rows = data.get("callputltp") if isinstance(data, dict) else None
    rows = rows if isinstance(rows, list) else []
    expiries: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        value = item.get("expiry") or item.get("date")
        if value is None:
            continue
        text = str(value).strip()
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            expiries.add(text[:10])
    return sorted(expiries)


def fyers_capability_matrix(
    app_type: str | None = None,
) -> list[tuple[str, CapabilityState, bool, str | None]]:
    """Static FYERS capability matrix, app-type aware.

    ``(name, state, wired, detail)`` — ``wired`` is the PLATFORM
    dimension (read-only data wired in this phase; orders/streaming
    prepared, not wired). The trading family is NEVER unconditionally
    SUPPORTED: FYERS order execution in 2026 is gated by app generation
    (compliant ``-200`` app + activated trading + static IP + order
    permission). A data-only/legacy app (``-100`` or unknown) reports
    trading as UNSUPPORTED; a compliant app reports SUPPORTED with the
    gating detail, and the adapter's session-aware view still gates by
    session state.
    """
    app_type = (app_type or "").strip().upper()
    trading_capable = orders_capable(app_type)
    trading_state = CapabilityState.SUPPORTED if trading_capable else CapabilityState.UNSUPPORTED
    trading_detail = (
        "Compliant -200 app — order placement additionally requires activated "
        "trading, a static IP and order-placement permission."
        if trading_capable
        else f"App type '{app_type or 'unknown'}' is data-only/legacy — FYERS order "
        "placement requires a compliant -200 app with activated trading."
    )
    data_detail = "FYERS v3 REST data API"
    return [
        # ---- read-only data ----
        ("profile", CapabilityState.SUPPORTED, True, "GET /api/v3/profile"),
        ("funds", CapabilityState.SUPPORTED, True, "GET /api/v3/funds"),
        ("market_status", CapabilityState.UNSUPPORTED, False, "No FYERS v3 market-status endpoint mapped"),
        ("option_chain", CapabilityState.SUPPORTED, True, "POST /data/options-chain-v3"),
        ("option_contracts", CapabilityState.SUPPORTED, True, "POST /data/options-chain-v3 (expiry discovery)"),
        ("quotes", CapabilityState.SUPPORTED, True, "POST /data/quotes"),
        ("websocket_market_data", CapabilityState.SUPPORTED, False, "FYERS market-data socket — platform uses HTTP polling"),
        ("positions", CapabilityState.SUPPORTED, True, "GET /api/v3/positions"),
        ("holdings", CapabilityState.SUPPORTED, True, "GET /api/v3/holdings"),
        ("trades", CapabilityState.SUPPORTED, True, "GET /api/v3/tradebook (normalized; not wired into execution)"),
        ("orders", trading_state, False, trading_detail),
        ("market_orders", trading_state, False, trading_detail),
        ("limit_orders", trading_state, False, trading_detail),
        ("stop_loss", trading_state, False, trading_detail),
        ("stop_loss_market", trading_state, False, trading_detail),
        ("modify_order", trading_state, False, trading_detail),
        ("cancel_order", trading_state, False, trading_detail),
        ("websocket_order_events", CapabilityState.SUPPORTED, False, "FYERS order/trade events socket — not wired"),
        ("margin", CapabilityState.UNSUPPORTED, False, "No FYERS multi-leg margin endpoint mapped"),
        ("market_protection", CapabilityState.UNSUPPORTED, False, "Not offered by FYERS v3 orders"),
    ]


def orders_capable(app_type: str | None = None) -> bool:
    """True only when the app type is the compliant trading-capable ``-200``
    generation (``-200`` / ``200`` accepted spellings). A data-only/legacy
    ``-100`` app — or an unknown app type — is NEVER trading-capable."""
    normalized = (app_type or "").strip().upper().replace("-", "").replace("_", "")
    return normalized.endswith("200") and not normalized.endswith("100")
