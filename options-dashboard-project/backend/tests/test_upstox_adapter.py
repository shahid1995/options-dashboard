"""Phase 6.5.0.2 — UpstoxAdapter contract tests.

The adapter is the broker boundary: raw Upstox failures become canonical
BrokerError, instrument keys stay inside the adapter, chain/contract
methods return canonical structures, order/trade/portfolio operations are
prepared but NOT wired (CAPABILITY_UNSUPPORTED), and tokens never leak.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.brokers.adapters.upstox.adapter import UpstoxAdapter
from app.brokers.adapters.upstox.mapper import UPSTOX_INSTRUMENT_KEYS
from app.brokers.domain.capabilities import CapabilityState
from app.brokers.domain.enums import BROKER_ID_UPSTOX, OptionType, OrderType, Side
from app.brokers.domain.errors import BrokerError, BrokerErrorCode
from app.brokers.domain.models import BrokerOrderRequest, InstrumentIdentity
from app.brokers.gateway import gateway
from app.services.upstox import UpstoxError


def upstox_error(status_code, message="boom"):
    return UpstoxError(status_code, message)


def make_fetcher(body):
    async def _f(*args, **kwargs):
        if isinstance(body, Exception):
            raise body
        return body

    return _f


# ---- Error mapping -----------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, BrokerErrorCode.TOKEN_EXPIRED),
        (403, BrokerErrorCode.TOKEN_EXPIRED),
        (423, BrokerErrorCode.MAINTENANCE),
        (429, BrokerErrorCode.RATE_LIMITED),
        (500, BrokerErrorCode.UPSTREAM_ERROR),
        (502, BrokerErrorCode.UPSTREAM_ERROR),
    ],
)
async def test_adapter_maps_http_errors_to_canonical_codes(status, expected):
    adapter = UpstoxAdapter("tok", profile_fetcher=make_fetcher(upstox_error(status)))
    with pytest.raises(BrokerError) as exc:
        await adapter.get_profile()
    assert exc.value.code is expected
    assert exc.value.status_code == status
    assert isinstance(exc.value, BrokerError)  # never a raw UpstoxError


async def test_adapter_maps_network_error_to_network_error():
    adapter = UpstoxAdapter(
        "tok", profile_fetcher=make_fetcher(upstox_error(502, "Could not reach Upstox: timeout"))
    )
    with pytest.raises(BrokerError) as exc:
        await adapter.get_profile()
    assert exc.value.code is BrokerErrorCode.NETWORK_ERROR


async def test_adapter_requires_token():
    adapter = UpstoxAdapter()
    with pytest.raises(BrokerError) as exc:
        await adapter.get_profile()
    assert exc.value.code is BrokerErrorCode.AUTH_REQUIRED


async def test_auth_exchange_maps_errors():
    adapter = UpstoxAdapter(token_exchanger=make_fetcher(upstox_error(400)))
    with pytest.raises(BrokerError) as exc:
        await adapter.exchange_authorization_code("code")
    assert exc.value.code is BrokerErrorCode.UPSTREAM_ERROR


async def test_funds_and_margin_go_through_the_adapter():
    funds_ok = {"status": "success", "data": {"available_to_trade": {"total": 100.0}}}
    margin_ok = {"status": "success", "data": {"required_margin": 37503.0, "margins": []}}
    adapter = UpstoxAdapter(
        "tok",
        funds_fetcher=make_fetcher(funds_ok),
        margin_fetcher=make_fetcher(margin_ok),
    )
    funds = await adapter.get_funds()
    assert funds == funds_ok
    margin = await adapter.get_margin([{"instrument_key": "k"}])
    assert margin == margin_ok


# ---- Instrument resolution ---------------------------------------------------


def test_adapter_resolves_instrument_via_gateway():
    adapter = gateway.create(BROKER_ID_UPSTOX, access_token="tok")
    identity = adapter.resolve_instrument("BANKNIFTY")
    assert isinstance(identity, InstrumentIdentity)
    assert identity.symbol == "BANKNIFTY"
    assert identity.exchange == "NSE"
    assert identity.segment == "INDEX_DERIVATIVES"
    # The Upstox key lives in the mapper, never on the identity.
    assert UPSTOX_INSTRUMENT_KEYS["BANKNIFTY"] == "NSE_INDEX|Nifty Bank"


def test_adapter_unknown_symbol_raises_invalid_instrument():
    adapter = UpstoxAdapter("tok")
    with pytest.raises(BrokerError) as exc:
        adapter.resolve_instrument("NOPE")
    assert exc.value.code is BrokerErrorCode.INVALID_INSTRUMENT


async def test_option_contracts_return_canonical_contract():
    raw = {"data": [{"expiry": "2026-09-24"}, {"expiry": "2026-08-28"}, {"expiry": "2026-08-28"}]}
    adapter = UpstoxAdapter("tok", contracts_fetcher=make_fetcher(raw))
    result = await adapter.get_option_contracts("NIFTY")
    assert result == {
        "symbol": "NIFTY",
        "expiries": ["2026-08-28", "2026-09-24"],
        "contracts": [],
    }


async def test_option_contracts_preserve_authoritative_lot_size():
    raw = {
        "data": [
            {
                "expiry": "2026-08-28",
                "strike_price": 25000,
                "instrument_type": "CE",
                "lot_size": 65,
                "instrument_key": "NSE_FO|CALL-25000",
            },
            {
                "expiry": "2026-08-28",
                "strike_price": 25000,
                "instrument_type": "PE",
                "lot_size": 65,
                "instrument_key": "NSE_FO|PUT-25000",
            },
        ]
    }
    adapter = UpstoxAdapter("tok", contracts_fetcher=make_fetcher(raw))
    result = await adapter.get_option_contracts("NIFTY")
    assert result["expiries"] == ["2026-08-28"]
    assert result["contracts"] == [
        {
            "expiry": "2026-08-28",
            "strike": 25000.0,
            "option_type": "call",
            "lot_size": 65,
        },
        {
            "expiry": "2026-08-28",
            "strike": 25000.0,
            "option_type": "put",
            "lot_size": 65,
        },
    ]


async def test_option_chain_returns_canonical_transformed_chain():
    raw = {
        "data": [
            {
                "strike_price": 25000,
                "underlying_spot_price": 25010.5,
                "call_options": {"market_data": {"ltp": 160.0}},
                "put_options": {"market_data": {"ltp": 90.0}},
            }
        ]
    }
    adapter = UpstoxAdapter("tok", chain_fetcher=make_fetcher(raw))
    chain = await adapter.get_option_chain("NIFTY", "2026-08-28")
    assert chain["symbol"] == "NIFTY"
    assert chain["chain"][0]["strike"] == 25000
    assert chain["chain"][0]["call"]["ltp"] == 160.0
    # No Upstox payload field names in the canonical chain.
    assert "call_options" not in str(chain)
    assert "instrument_key" not in str(chain)


async def test_resolve_instrument_keys_uses_patched_raw_chain():
    """The margin key resolution must call the raw chain client exactly the
    way the pre-existing resolver did, so existing patches keep working."""
    raw = {
        "data": [
            {
                "strike_price": 24350.0,
                "call_options": {"market_data": {"ltp": 125.25}, "option_greeks": {}},
                "put_options": {"market_data": {"ltp": 90.0}, "option_greeks": {}},
            }
        ]
    }
    with patch("app.services.upstox.get_option_chain", new=AsyncMock(return_value=raw)) as m:
        adapter = UpstoxAdapter("tok")
        instruments = [
            {"symbol": "NIFTY", "expiry": "2026-08-27", "strike": 24350.0,
             "option_type": "call", "quantity": 1, "lot_size": 65, "action": "buy"},
        ]
        resolved = await adapter.resolve_instrument_keys(instruments)

    m.assert_awaited_once_with("tok", UPSTOX_INSTRUMENT_KEYS["NIFTY"], "2026-08-27")
    assert resolved[0]["instrument_key"] is None  # payload has no key → explicit None


async def test_resolve_instrument_keys_missing_side_is_none():
    raw = {"data": [{"strike_price": 24350.0, "call_options": {}}]}
    adapter = UpstoxAdapter("tok", chain_fetcher=make_fetcher(raw))
    instruments = [
        {"symbol": "NIFTY", "expiry": "2026-08-27", "strike": 24350.0,
         "option_type": "put", "quantity": 1, "lot_size": 65, "action": "buy"},
    ]
    resolved = await adapter.resolve_instrument_keys(instruments)
    assert resolved[0]["instrument_key"] is None  # never fabricated


# ---- Market status -----------------------------------------------------------


async def test_market_status_passes_exchange_to_raw_client():
    body = {"data": {"exchange": "NSE_FO", "status": "NORMAL_OPEN"}}
    with patch(
        "app.services.upstox.get_market_status", new=AsyncMock(return_value=body)
    ) as m:
        adapter = UpstoxAdapter("tok")
        result = await adapter.get_market_status(exchange="NSE_FO")
    m.assert_awaited_once_with("tok", exchange="NSE_FO")
    assert result == body


# ---- Capabilities ------------------------------------------------------------


def test_capabilities_matrix_is_complete_and_session_aware():
    adapter = UpstoxAdapter()  # no token → data capabilities AUTH_REQUIRED
    caps = adapter.get_capabilities()
    assert caps.state("option_chain") is CapabilityState.AUTH_REQUIRED
    assert caps.state("orders") is CapabilityState.AUTH_REQUIRED
    assert caps.get("orders").wired is False
    assert caps.state("websocket_market_data") is CapabilityState.SUPPORTED

    adapter_tok = UpstoxAdapter("tok")
    caps_tok = adapter_tok.get_capabilities()
    assert caps_tok.state("option_chain") is CapabilityState.SUPPORTED
    assert caps_tok.state("funds") is CapabilityState.SUPPORTED

    inactive = adapter_tok.get_capabilities(profile={"is_active": False})
    assert inactive.state("funds") is CapabilityState.ACCOUNT_DISABLED
    assert inactive.state("option_chain") is CapabilityState.ACCOUNT_DISABLED


# ---- NOT WIRED operations ----------------------------------------------------


def test_order_operations_raise_capability_unsupported():
    adapter = UpstoxAdapter("tok")
    identity = InstrumentIdentity(
        exchange="NSE", segment="INDEX_DERIVATIVES", underlying="NIFTY",
        symbol="NIFTY", instrument_type="INDEX", lot_size=65,
    )
    request = BrokerOrderRequest(instrument=identity, side=Side.BUY, quantity=1, order_type=OrderType.MARKET)
    for call in (
        lambda: adapter.place_order(request),
        lambda: adapter.place_orders([request]),
        lambda: adapter.modify_order("ord-1", request),
        lambda: adapter.cancel_order("ord-1"),
        lambda: adapter.cancel_orders(["ord-1"]),
        lambda: adapter.get_order("ord-1"),
        lambda: adapter.get_orders(),
        lambda: adapter.get_order_history("ord-1"),
        lambda: adapter.get_trades(),
        lambda: adapter.get_order_trades("ord-1"),
        lambda: adapter.get_trade_history(),
        lambda: adapter.get_positions(),
        lambda: adapter.get_holdings(),
    ):
        with pytest.raises(BrokerError) as exc:
            call()
        assert exc.value.code is BrokerErrorCode.CAPABILITY_UNSUPPORTED
        assert "NOT wired" in exc.value.message


async def test_quote_operations_are_wired_and_fail_safely():
    """Quotes are WIRED (Day 10): the adapter returns canonical market-data
    contracts — and still fails safely with canonical errors when the
    instrument cannot be quoted or the session is missing."""
    # Concrete option identities cannot be quoted directly (no chain-issued
    # broker key on the canonical identity) — never a fabricated quote.
    option_identity = InstrumentIdentity(
        exchange="NSE", segment="INDEX_DERIVATIVES", underlying="NIFTY",
        symbol="NIFTY", instrument_type="OPTION", expiry="2026-09-11",
        strike=24500.0, option_type=OptionType.CALL,
    )
    adapter = UpstoxAdapter("tok")
    with pytest.raises(BrokerError) as exc:
        await adapter.get_quote(option_identity)
    assert exc.value.code is BrokerErrorCode.INVALID_INSTRUMENT

    # No session → AUTH_REQUIRED before any fetch attempt.
    no_token = UpstoxAdapter()
    with pytest.raises(BrokerError) as exc:
        await no_token.get_quotes([InstrumentIdentity(
            exchange="NSE", segment="INDEX_DERIVATIVES", underlying="NIFTY",
            symbol="NIFTY", instrument_type="INDEX",
        )])
    assert exc.value.code is BrokerErrorCode.AUTH_REQUIRED


# ---- Security ----------------------------------------------------------------


def test_adapter_repr_never_leaks_the_token():
    adapter = UpstoxAdapter("super-secret-token-123")
    assert "super-secret-token-123" not in repr(adapter)
    assert "super-secret-token-123" not in str(adapter)


def test_disconnect_forgets_the_token():
    adapter = UpstoxAdapter("tok")
    # Pure lookups keep working after disconnect (they never used the token).
    assert adapter.resolve_instrument("NIFTY").symbol == "NIFTY"
    adapter.disconnect()

    import asyncio

    with pytest.raises(BrokerError) as exc:
        asyncio.run(adapter.get_profile())
    assert exc.value.code is BrokerErrorCode.AUTH_REQUIRED


def test_authorization_url_uses_broker_client():
    url = UpstoxAdapter().get_authorization_url("state-1")
    assert url.startswith("https://api.upstox.com/v2/login/authorization/dialog")
    assert "state=state-1" in url
