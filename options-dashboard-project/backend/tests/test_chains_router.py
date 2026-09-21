from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.routers.chains import INSTRUMENT_KEYS
from app.routers.deps import SESSION_COOKIE_NAME
from app.services import token_store, upstox
from app.services.upstox import UpstoxError


def upstox_error(status_code, message="error"):
    return UpstoxError(status_code, message)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def logged_in(client):
    session_id = token_store.set_token("tok-xyz")
    # Issue #61: the browser session is transported ONLY by the canonical
    # HttpOnly cookie — tests authenticate exactly like the real browser.
    client.cookies.set(SESSION_COOKIE_NAME, session_id)
    return session_id


def make_chain_item(strike, call_market=None, put_market=None, call_greeks=None, spot=25010.5):
    return {
        "strike_price": strike,
        "underlying_spot_price": spot,
        "call_options": {
            "market_data": call_market or {},
            "option_greeks": call_greeks or {},
        },
        "put_options": {
            "market_data": put_market or {},
        },
    }


def test_expiries_unknown_symbol_returns_404(client, logged_in):
    resp = client.get("/chains/UNKNOWN/expiries")
    assert resp.status_code == 404
    assert "Unknown symbol" in resp.json()["detail"]


def test_expiries_requires_login(client):
    resp = client.get("/chains/NIFTY/expiries")
    assert resp.status_code == 401
    assert "Not logged in" in resp.json()["detail"]


def test_expiries_sorted_and_deduplicated(client, logged_in, monkeypatch):
    mock = AsyncMock(return_value={
        "data": [
            {"expiry": "2026-09-24"},
            {"expiry": "2026-08-28"},
            {"expiry": "2026-08-28"},
            {"no_expiry_key": True},
        ]
    })
    monkeypatch.setattr(upstox, "get_option_contracts", mock)

    resp = client.get("/chains/nifty/expiries")

    assert resp.status_code == 200
    assert resp.json() == {"symbol": "NIFTY", "expiries": ["2026-08-28", "2026-09-24"]}
    mock.assert_awaited_once_with("tok-xyz", INSTRUMENT_KEYS["NIFTY"])


def test_expiries_empty_data(client, logged_in, monkeypatch):
    monkeypatch.setattr(upstox, "get_option_contracts", AsyncMock(return_value={}))
    resp = client.get("/chains/BANKNIFTY/expiries")
    assert resp.status_code == 200
    assert resp.json() == {"symbol": "BANKNIFTY", "expiries": []}


@pytest.mark.parametrize("symbol", sorted(INSTRUMENT_KEYS))
def test_expiries_every_index_symbol_uses_its_instrument_key(client, logged_in, monkeypatch, symbol):
    mock = AsyncMock(return_value={"data": []})
    monkeypatch.setattr(upstox, "get_option_contracts", mock)

    resp = client.get(f"/chains/{symbol.lower()}/expiries")

    assert resp.status_code == 200
    assert resp.json()["symbol"] == symbol
    mock.assert_awaited_once_with("tok-xyz", INSTRUMENT_KEYS[symbol])


def test_chain_unknown_symbol_returns_404(client, logged_in):
    resp = client.get("/chains/UNKNOWN", params={"expiry_date": "2026-08-28"})
    assert resp.status_code == 404


def test_chain_requires_expiry_date(client, logged_in):
    resp = client.get("/chains/NIFTY")
    assert resp.status_code == 422


def test_chain_requires_login(client):
    resp = client.get("/chains/NIFTY", params={"expiry_date": "2026-08-28"})
    assert resp.status_code == 401


def test_chain_accepts_session_header(client, monkeypatch):
    session_id = token_store.set_token("tok-xyz")
    raw = {"data": [make_chain_item(25000)]}
    monkeypatch.setattr(upstox, "get_option_chain", AsyncMock(return_value=raw))
    resp = client.get(
        "/chains/NIFTY",
        params={"expiry_date": "2026-08-28"},
        headers={"X-Session-Id": session_id},
    )
    assert resp.status_code == 200


def test_chain_rejects_wrong_session(client):
    token_store.set_token("tok-xyz")
    client.cookies.set(SESSION_COOKIE_NAME, "wrong-session")
    resp = client.get("/chains/NIFTY", params={"expiry_date": "2026-08-28"})
    assert resp.status_code == 401


def test_chain_rejects_malformed_expiry_date(client, logged_in):
    resp = client.get("/chains/NIFTY", params={"expiry_date": "not-a-date"})
    assert resp.status_code == 422
    assert "YYYY-MM-DD" in resp.json()["detail"]


def test_chain_transforms_and_sorts_rows(client, logged_in, monkeypatch):
    raw = {
        "data": [
            make_chain_item(
                25100,
                call_market={"ltp": 120.5, "oi": 500, "prev_oi": 400, "volume": 1000},
                call_greeks={"iv": 14.2, "delta": 0.55, "theta": -3.1, "gamma": 0.002, "vega": 8.5, "pop": 52.0},
                put_market={"ltp": 95.0, "oi": 300, "prev_oi": 350},
            ),
            make_chain_item(25000, call_market={"ltp": 160.0}),
        ]
    }
    mock = AsyncMock(return_value=raw)
    monkeypatch.setattr(upstox, "get_option_chain", mock)

    resp = client.get("/chains/nifty", params={"expiry_date": "2026-08-28"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "NIFTY"
    assert body["expiry_date"] == "2026-08-28"
    assert body["underlying_spot_price"] == 25010.5
    mock.assert_awaited_once_with("tok-xyz", INSTRUMENT_KEYS["NIFTY"], "2026-08-28")

    strikes = [row["strike"] for row in body["chain"]]
    assert strikes == [25000, 25100]

    row = body["chain"][1]
    assert row["call"]["ltp"] == 120.5
    assert row["call"]["chg_oi"] == 100
    assert row["call"]["iv"] == 14.2
    assert row["call"]["pop"] == 52.0
    assert row["put"]["ltp"] == 95.0
    assert row["put"]["chg_oi"] == -50
    assert row["put"]["iv"] is None


def test_chain_handles_missing_oi_and_sides(client, logged_in, monkeypatch):
    raw = {
        "data": [
            {
                "strike_price": 25000,
                "underlying_spot_price": 25010.5,
                "call_options": {"market_data": {"oi": 500}},
                # put_options entirely missing
            }
        ]
    }
    monkeypatch.setattr(upstox, "get_option_chain", AsyncMock(return_value=raw))

    resp = client.get("/chains/NIFTY", params={"expiry_date": "2026-08-28"})

    assert resp.status_code == 200
    row = resp.json()["chain"][0]
    assert row["call"]["oi"] == 500
    assert row["call"]["chg_oi"] is None  # prev_oi missing
    assert row["put"] == {
        "ltp": None,
        "oi": None,
        "chg_oi": None,
        "volume": None,
        "quote_timestamp": None,
        "iv": None,
        "delta": None,
        "theta": None,
        "gamma": None,
        "vega": None,
        "pop": None,
    }


def test_chain_empty_data(client, logged_in, monkeypatch):
    monkeypatch.setattr(upstox, "get_option_chain", AsyncMock(return_value={}))
    resp = client.get("/chains/NIFTY", params={"expiry_date": "2026-08-28"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["chain"] == []
    assert body["underlying_spot_price"] is None


def test_chain_banknifty_uses_bank_instrument_key(client, logged_in, monkeypatch):
    mock = AsyncMock(return_value={})
    monkeypatch.setattr(upstox, "get_option_chain", mock)

    resp = client.get("/chains/banknifty", params={"expiry_date": "2026-08-28"})

    assert resp.status_code == 200
    assert resp.json()["symbol"] == "BANKNIFTY"
    mock.assert_awaited_once_with("tok-xyz", INSTRUMENT_KEYS["BANKNIFTY"], "2026-08-28")


@pytest.mark.parametrize("status", [401, 403])
def test_chain_upstox_auth_error_clears_token_and_returns_401(client, logged_in, monkeypatch, status):
    monkeypatch.setattr(upstox, "get_option_chain", AsyncMock(side_effect=upstox_error(status)))

    resp = client.get("/chains/NIFTY", params={"expiry_date": "2026-08-28"})

    assert resp.status_code == 401
    assert "session expired" in resp.json()["detail"].lower()
    assert token_store.get_token(logged_in) is None


def test_chain_upstox_server_error_returns_502(client, logged_in, monkeypatch):
    monkeypatch.setattr(upstox, "get_option_chain", AsyncMock(side_effect=upstox_error(500)))

    resp = client.get("/chains/NIFTY", params={"expiry_date": "2026-08-28"})

    assert resp.status_code == 502
    assert "Upstox API error (500)" in resp.json()["detail"]
    assert token_store.get_token(logged_in) == "tok-xyz"


def test_expiries_upstox_auth_error_clears_token_and_returns_401(client, logged_in, monkeypatch):
    monkeypatch.setattr(upstox, "get_option_contracts", AsyncMock(side_effect=upstox_error(401)))

    resp = client.get("/chains/NIFTY/expiries")

    assert resp.status_code == 401
    assert token_store.get_token(logged_in) is None


def ws_close_code(client, path, session_id=None):
    headers = {"cookie": f"{SESSION_COOKIE_NAME}={session_id}"} if session_id else {}
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(path, headers=headers) as ws:
            ws.receive_json()
    return exc_info.value.code


def test_ws_authenticates_via_canonical_cookie(client, monkeypatch):
    """Issue #61: WebSocket auth resolves the platform session from the
    canonical HttpOnly cookie — no session credential in subprotocols."""
    session_id = token_store.set_token("tok-xyz")
    raw = {"data": [make_chain_item(25000)]}
    monkeypatch.setattr(upstox, "get_option_chain", AsyncMock(return_value=raw))

    with client.websocket_connect(
        "/chains/ws/NIFTY?expiry_date=2026-08-28",
        headers={"cookie": f"{SESSION_COOKIE_NAME}={session_id}"},
    ) as ws:
        body = ws.receive_json()

    assert body["symbol"] == "NIFTY"


def test_ws_rejects_wrong_session(client):
    token_store.set_token("tok-xyz")
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/chains/ws/NIFTY?expiry_date=2026-08-28",
            headers={"cookie": f"{SESSION_COOKIE_NAME}=wrong-session"},
        ) as ws:
            ws.receive_json()
    assert exc_info.value.code == 4401


def test_ws_unknown_symbol_closes_4404(client, logged_in):
    assert ws_close_code(client, "/chains/ws/UNKNOWN?expiry_date=2026-08-28", logged_in) == 4404


def test_ws_without_login_closes_4401(client):
    assert ws_close_code(client, "/chains/ws/NIFTY?expiry_date=2026-08-28") == 4401


def test_ws_with_wrong_session_closes_4401(client):
    token_store.set_token("tok-xyz")
    assert ws_close_code(client, "/chains/ws/NIFTY?expiry_date=2026-08-28", "wrong-session") == 4401


def test_ws_malformed_expiry_date_closes_4422(client, logged_in):
    assert ws_close_code(client, "/chains/ws/NIFTY?expiry_date=not-a-date", logged_in) == 4422


def test_ws_upstox_auth_error_clears_token_and_closes_4401(client, logged_in, monkeypatch):
    monkeypatch.setattr(upstox, "get_option_chain", AsyncMock(side_effect=upstox_error(403)))
    assert ws_close_code(client, "/chains/ws/NIFTY?expiry_date=2026-08-28", logged_in) == 4401
    assert token_store.get_token(logged_in) is None


def test_ws_upstox_server_error_closes_4502(client, logged_in, monkeypatch):
    monkeypatch.setattr(upstox, "get_option_chain", AsyncMock(side_effect=upstox_error(500)))
    assert ws_close_code(client, "/chains/ws/NIFTY?expiry_date=2026-08-28", logged_in) == 4502


def test_ws_network_error_closes_4502(client, logged_in, monkeypatch):
    monkeypatch.setattr(upstox, "get_option_chain", AsyncMock(side_effect=upstox_error(502, "Could not reach Upstox")))
    assert ws_close_code(client, "/chains/ws/NIFTY?expiry_date=2026-08-28", logged_in) == 4502


def test_ws_streams_transformed_chain(client, logged_in, monkeypatch):
    raw = {"data": [make_chain_item(25000, call_market={"ltp": 160.0})]}
    monkeypatch.setattr(upstox, "get_option_chain", AsyncMock(return_value=raw))

    with client.websocket_connect(
        "/chains/ws/nifty?expiry_date=2026-08-28",
        headers={"cookie": f"{SESSION_COOKIE_NAME}={logged_in}"},
    ) as ws:
        body = ws.receive_json()

    assert body["symbol"] == "NIFTY"
    assert body["expiry_date"] == "2026-08-28"
    assert body["chain"][0]["strike"] == 25000
    assert body["chain"][0]["call"]["ltp"] == 160.0


# ---------------------------------------------------------------------------
# Phase A: Broker token vs StrikeNova session distinction
# ---------------------------------------------------------------------------


def test_expiries_returns_403_for_email_session(client):
    """Email session (identity token, no broker token) gets 403, not 401."""
    import secrets
    from uuid import uuid4
    from app.db import Base, SessionLocal
    from app.identity import User, create_session_record

    # Ensure tables exist (TestClient may not run init_db/Alembic)
    Base.metadata.create_all(bind=SessionLocal().get_bind())

    db = SessionLocal()
    try:
        user_id = str(uuid4())
        db.add(User(id=user_id, status="active", identity_source="email", broker_provider=None))
        db.flush()
        session_id = secrets.token_urlsafe(32)
        create_session_record(db, user_id, session_id)
        db.commit()
    finally:
        db.close()

    resp = client.get(
        "/chains/NIFTY/expiries",
        headers={"X-Session-Id": session_id},
    )
    assert resp.status_code == 403
    # Analytics-Token market-data authorization era: 403 means the valid
    # platform session has NO active market-data credential (not the old
    # "broker login" framing).
    assert "Market data is not connected" in resp.json()["detail"]


def test_expiries_returns_403_for_google_session(client):
    """Google session (identity token, no broker token) gets 403, not 401."""
    import secrets
    from uuid import uuid4
    from app.db import Base, SessionLocal
    from app.identity import User, create_session_record

    Base.metadata.create_all(bind=SessionLocal().get_bind())

    db = SessionLocal()
    try:
        user_id = str(uuid4())
        db.add(User(id=user_id, status="active", identity_source="google", broker_provider=None))
        db.flush()
        session_id = secrets.token_urlsafe(32)
        create_session_record(db, user_id, session_id)
        db.commit()
    finally:
        db.close()

    resp = client.get(
        "/chains/NIFTY/expiries",
        headers={"X-Session-Id": session_id},
    )
    assert resp.status_code == 403
    # Analytics-Token market-data authorization era: 403 means the valid
    # platform session has NO active market-data credential (not the old
    # "broker login" framing).
    assert "Market data is not connected" in resp.json()["detail"]


def test_expiries_returns_401_for_no_session(client):
    """No session at all returns 401 (not 403)."""
    resp = client.get("/chains/NIFTY/expiries")
    assert resp.status_code == 401
    assert "Not logged in" in resp.json()["detail"]


def test_chain_returns_403_for_email_session(client):
    """Chain endpoint returns 403 for email session."""
    import secrets
    from uuid import uuid4
    from app.db import Base, SessionLocal
    from app.identity import User, create_session_record

    Base.metadata.create_all(bind=SessionLocal().get_bind())

    db = SessionLocal()
    try:
        user_id = str(uuid4())
        db.add(User(id=user_id, status="active", identity_source="email", broker_provider=None))
        db.flush()
        session_id = secrets.token_urlsafe(32)
        create_session_record(db, user_id, session_id)
        db.commit()
    finally:
        db.close()

    resp = client.get(
        "/chains/NIFTY",
        params={"expiry_date": "2026-09-24"},
        headers={"X-Session-Id": session_id},
    )
    assert resp.status_code == 403
    assert "Market data is not connected" in resp.json()["detail"]


def test_broker_token_still_works_for_expiries(client, logged_in, monkeypatch):
    """Real broker token (no colon) still works normally."""
    mock = AsyncMock(return_value={"data": [{"expiry": "2026-09-24"}]})
    monkeypatch.setattr(upstox, "get_option_contracts", mock)

    resp = client.get("/chains/NIFTY/expiries")
    assert resp.status_code == 200


def test_token_store_skip_persist_works():
    """set_token with persist_to_db=False does not write to DB."""
    session_id = token_store.set_token(
        "email:test:val",
        persist_to_db=False,
    )
    assert session_id is not None
    assert token_store.get_token(session_id) == "email:test:val"

# ---------------------------------------------------------------------------
# Analytics-Token market-data authorization (canonical credential resolver)
#
# The Upstox Analytics Token is the authorized read-only market-data
# credential: platform session → resolve_market_data_token → Upstox.
# Tests A–F cover the required behaviors: analytics-only resolution,
# structured 403 when not connected, OAuth fallback, token isolation,
# the WebSocket path, and non-interference with GEX credential resolution.
# ---------------------------------------------------------------------------

import secrets as _secrets
from datetime import datetime, timedelta, timezone as _tz
from uuid import uuid4 as _uuid4

from app.crypto import encrypt as _encrypt
from app.db import Base, SessionLocal as _SessionLocal
from app.identity import (
    BrokerAuthorization as _BrokerAuthorization,
    BrokerConnection as _BrokerConnection,
    User as _User,
    create_session_record as _create_session_record,
)
from app.services import upstox as _upstox
from app.services.market_data_authorization import (
    ANALYTICS_SOURCE as _ANALYTICS_SOURCE,
    LEGACY_SESSION_SOURCE as _LEGACY_SOURCE,
    OAUTH_SOURCE as _OAUTH_SOURCE,
    resolve_market_data_token as _resolve_market_data_token,
)
from app.services.token_store import has_platform_session as _has_platform_session


def _make_db_world(*, analytics_user=True, analytics_token="ANALYTICS-TOKEN-XYZ", broker="UPSTOX"):
    """Create (user, session_id, cleanup) with a platform session and,
    when ``analytics_user``, a connected connection holding an encrypted
    Analytics Token. Everything lands in the shared test DB."""
    Base.metadata.create_all(bind=_SessionLocal().get_bind())
    db = _SessionLocal()
    try:
        user_id = str(_uuid4())
        db.add(_User(id=user_id, status="active", identity_source="email", broker_provider=None))
        db.flush()
        if analytics_user:
            conn = _BrokerConnection(
                id=str(_uuid4()),
                user_id=user_id,
                broker=broker,
                broker_account_id="data-only",
                status="connected",
                data_status="active",
                data_source="analytics_token",
                display_label=f"{broker} (Data Only)",
                broker_analytics_token_encrypted=_encrypt(analytics_token),
                connected_at=datetime.now(_tz.utc),
            )
            db.add(conn)
            db.flush()
        session_id = _secrets.token_urlsafe(32)
        _create_session_record(db, user_id, session_id)
        db.commit()
    finally:
        db.close()
    return user_id, session_id


# --- Test A: Analytics Token only (no OAuth session token) ------------------


def test_analytics_only_expiries_succeed_with_analytics_credential(client, monkeypatch):
    """Valid platform session + Analytics Token, NO session-scoped broker
    token: expiries resolve through the Analytics Token."""
    _user_id, session_id = _make_db_world()
    assert token_store.get_token(session_id) is None  # no legacy session token
    mock = AsyncMock(return_value={"data": [{"expiry": "2026-09-24"}]})
    monkeypatch.setattr(_upstox, "get_option_contracts", mock)

    resp = client.get(
        "/chains/NIFTY/expiries",
        headers={"X-Session-Id": session_id},
    )

    assert resp.status_code == 200
    assert resp.json() == {"symbol": "NIFTY", "expiries": ["2026-09-24"]}
    # The Analytics Token is the credential actually passed to the adapter.
    mock.assert_awaited_once_with("ANALYTICS-TOKEN-XYZ", INSTRUMENT_KEYS["NIFTY"])


def test_analytics_only_chain_succeeds_with_analytics_credential(client, monkeypatch):
    """Chain fetch resolves through the Analytics Token end to end."""
    _user_id, session_id = _make_db_world()
    mock = AsyncMock(return_value={"data": [make_chain_item(25000)]})
    monkeypatch.setattr(_upstox, "get_option_chain", mock)

    resp = client.get(
        "/chains/NIFTY",
        params={"expiry_date": "2026-08-28"},
        headers={"X-Session-Id": session_id},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "NIFTY"
    assert body["chain"][0]["strike"] == 25000
    mock.assert_awaited_once_with("ANALYTICS-TOKEN-XYZ", INSTRUMENT_KEYS["NIFTY"], "2026-08-28")


def test_resolver_prefers_analytics_over_oauth_authorization(client):
    """When both an Analytics Token and an active OAuth authorization
    exist, the Analytics Token wins (provenance = analytics_token)."""
    Base.metadata.create_all(bind=_SessionLocal().get_bind())
    db = _SessionLocal()
    try:
        user_id = str(_uuid4())
        db.add(_User(id=user_id, status="active", identity_source="email", broker_provider=None))
        db.flush()
        conn = _BrokerConnection(
            id=str(_uuid4()),
            user_id=user_id,
            broker="UPSTOX",
            broker_account_id="UCC-1",
            status="connected",
            data_status="active",
            data_source="analytics_token",
            broker_analytics_token_encrypted=_encrypt("ANALYTICS-TOKEN-XYZ"),
            connected_at=datetime.now(_tz.utc),
        )
        db.add(conn)
        db.flush()
        db.add(
            _BrokerAuthorization(
                id=str(_uuid4()),
                connection_id=conn.id,
                access_token_encrypted=_encrypt("OAUTH-TOKEN-ABC"),
                status="active",
                method="oauth_callback",
                issued_at=datetime.now(_tz.utc),
                created_at=datetime.now(_tz.utc),
                updated_at=datetime.now(_tz.utc),
            )
        )
        db.commit()
        credential = _resolve_market_data_token(db, user_id, "UPSTOX")
    finally:
        db.close()

    assert credential is not None
    assert credential.source == _ANALYTICS_SOURCE
    assert credential.token == "ANALYTICS-TOKEN-XYZ"


# --- Test B: no Analytics Token → structured 403 -----------------------------


def test_no_market_data_credential_returns_403_for_expiries(client):
    """Valid platform session, no Analytics Token, no OAuth credential:
    403 clearly identifying missing market-data authorization."""
    _user_id, session_id = _make_db_world(analytics_user=False)

    resp = client.get("/chains/NIFTY/expiries", headers={"X-Session-Id": session_id})

    assert resp.status_code == 403
    assert "Market data is not connected" in resp.json()["detail"]


def test_no_market_data_credential_returns_403_for_chain(client):
    _user_id, session_id = _make_db_world(analytics_user=False)

    resp = client.get(
        "/chains/NIFTY",
        params={"expiry_date": "2026-08-28"},
        headers={"X-Session-Id": session_id},
    )

    assert resp.status_code == 403
    assert "Market data is not connected" in resp.json()["detail"]


def test_not_connected_403_is_not_reported_as_session_expiry(client):
    """The 403 body must never claim the platform login expired."""
    _user_id, session_id = _make_db_world(analytics_user=False)

    resp = client.get("/chains/NIFTY/expiries", headers={"X-Session-Id": session_id})

    body = resp.json()["detail"].lower()
    assert "not logged in" not in body
    assert "session expired" not in body
    assert "log in" not in body.replace("analytics token", "")


# --- Test C: OAuth fallback (intentionally supported, preserved) -------------


def test_oauth_fallback_used_when_no_analytics_token(client, monkeypatch):
    """No Analytics Token but an active OAuth BrokerAuthorization on the
    user's default connection: the OAuth token is the credential."""
    Base.metadata.create_all(bind=_SessionLocal().get_bind())
    db = _SessionLocal()
    try:
        user_id = str(_uuid4())
        db.add(_User(id=user_id, status="active", identity_source="email", broker_provider=None))
        db.flush()
        conn = _BrokerConnection(
            id=str(_uuid4()),
            user_id=user_id,
            broker="UPSTOX",
            broker_account_id="UCC-2",
            status="connected",
            connected_at=datetime.now(_tz.utc),
        )
        db.add(conn)
        db.flush()
        db.add(
            _BrokerAuthorization(
                id=str(_uuid4()),
                connection_id=conn.id,
                access_token_encrypted=_encrypt("OAUTH-TOKEN-ABC"),
                status="active",
                method="oauth_callback",
                issued_at=datetime.now(_tz.utc),
                created_at=datetime.now(_tz.utc),
                updated_at=datetime.now(_tz.utc),
            )
        )
        session_id = _secrets.token_urlsafe(32)
        _create_session_record(db, user_id, session_id)
        db.commit()
    finally:
        db.close()

    mock = AsyncMock(return_value={"data": [{"expiry": "2026-09-24"}]})
    monkeypatch.setattr(_upstox, "get_option_contracts", mock)

    resp = client.get("/chains/NIFTY/expiries", headers={"X-Session-Id": session_id})

    assert resp.status_code == 200
    mock.assert_awaited_once_with("OAUTH-TOKEN-ABC", INSTRUMENT_KEYS["NIFTY"])


def test_expired_oauth_authorization_is_not_used_as_fallback(client):
    """An expired BrokerAuthorization never resolves as a credential."""
    Base.metadata.create_all(bind=_SessionLocal().get_bind())
    db = _SessionLocal()
    try:
        user_id = str(_uuid4())
        db.add(_User(id=user_id, status="active", identity_source="email", broker_provider=None))
        db.flush()
        conn = _BrokerConnection(
            id=str(_uuid4()),
            user_id=user_id,
            broker="UPSTOX",
            broker_account_id="UCC-3",
            status="connected",
            connected_at=datetime.now(_tz.utc),
        )
        db.add(conn)
        db.flush()
        db.add(
            _BrokerAuthorization(
                id=str(_uuid4()),
                connection_id=conn.id,
                access_token_encrypted=_encrypt("OAUTH-STALE"),
                status="active",
                method="oauth_callback",
                access_token_expires_at=datetime.now(_tz.utc) - timedelta(hours=1),
                issued_at=datetime.now(_tz.utc),
                created_at=datetime.now(_tz.utc),
                updated_at=datetime.now(_tz.utc),
            )
        )
        db.commit()
        credential = _resolve_market_data_token(db, user_id, "UPSTOX")
    finally:
        db.close()

    assert credential is None


# --- Test D: token isolation -------------------------------------------------


def test_user_a_cannot_resolve_user_b_analytics_token(client):
    """Ownership is enforced: B's session must never resolve A's token."""
    Base.metadata.create_all(bind=_SessionLocal().get_bind())
    db = _SessionLocal()
    try:
        user_a, user_b = str(_uuid4()), str(_uuid4())
        db.add(_User(id=user_a, status="active", identity_source="email", broker_provider=None))
        db.add(_User(id=user_b, status="active", identity_source="email", broker_provider=None))
        db.flush()
        db.add(
            _BrokerConnection(
                id=str(_uuid4()),
                user_id=user_a,  # A owns the connection
                broker="UPSTOX",
                broker_account_id="A-ONLY",
                status="connected",
                data_status="active",
                data_source="analytics_token",
                broker_analytics_token_encrypted=_encrypt("A-SECRET-TOKEN"),
                connected_at=datetime.now(_tz.utc),
            )
        )
        session_b = _secrets.token_urlsafe(32)
        _create_session_record(db, user_b, session_b)
        db.commit()

        # B resolves via the API with their own session.
        resp = client.get("/chains/NIFTY/expiries", headers={"X-Session-Id": session_b})
        # B's resolver call must never see A's token:
        credential_b = _resolve_market_data_token(db, user_b, "UPSTOX")
    finally:
        db.close()

    assert resp.status_code == 403  # no market-data authorization for B
    assert credential_b is None


def test_platform_session_identifier_never_sent_to_upstox(client, monkeypatch):
    """A platform session identifier (email:...) can never become the
    broker credential, even if the legacy store still holds one."""
    _user_id, session_id = _make_db_world(analytics_user=False)
    # Simulate a stale identity token in the legacy session store.
    legacy_sid = token_store.set_token(f"email:{_user_id}:notabroker", persist_to_db=False)
    # Bind BOTH ids to the same cookie-less header sequence: the request
    # uses the platform session; the legacy entry must never leak through.
    assert token_store.get_token(legacy_sid) == f"email:{_user_id}:notabroker"

    seen = {}

    async def spy(token, key):
        seen["token"] = token
        return {"data": []}

    monkeypatch.setattr(_upstox, "get_option_contracts", spy)

    resp = client.get("/chains/NIFTY/expiries", headers={"X-Session-Id": session_id})

    # No credential at all → structured 403; nothing broker-shaped was sent.
    assert resp.status_code == 403
    assert "token" not in seen


def test_analytics_token_never_returned_by_api_endpoints(client, monkeypatch):
    """No chains API response or session endpoint ever contains the token."""
    _user_id, session_id = _make_db_world()
    monkeypatch.setattr(
        _upstox, "get_option_contracts", AsyncMock(return_value={"data": [{"expiry": "2026-09-24"}]})
    )

    expiry_resp = client.get("/chains/NIFTY/expiries", headers={"X-Session-Id": session_id})
    status_resp = client.get("/auth/analytics-token/status", headers={"X-Session-Id": session_id})

    assert expiry_resp.status_code == 200
    assert "ANALYTICS-TOKEN-XYZ" not in expiry_resp.text
    if status_resp.status_code == 200:
        assert "ANALYTICS-TOKEN-XYZ" not in status_resp.text


def test_analytics_token_never_logged(client, monkeypatch, caplog):
    """The resolution path must not log the token value."""
    import logging as _logging

    _user_id, session_id = _make_db_world()
    monkeypatch.setattr(
        _upstox, "get_option_contracts", AsyncMock(return_value={"data": [{"expiry": "2026-09-24"}]})
    )

    with caplog.at_level(_logging.DEBUG):
        resp = client.get("/chains/NIFTY/expiries", headers={"X-Session-Id": session_id})

    assert resp.status_code == 200
    assert "ANALYTICS-TOKEN-XYZ" not in caplog.text


# --- Test E: WebSocket path ---------------------------------------------------


def test_ws_streams_chain_via_analytics_token(client, monkeypatch):
    """The live path resolves the Analytics Token from the platform
    session cookie — no subprotocol/session-token transport involved."""
    _user_id, session_id = _make_db_world()
    mock = AsyncMock(return_value={"data": [make_chain_item(25000, call_market={"ltp": 160.0})]})
    monkeypatch.setattr(_upstox, "get_option_chain", mock)

    with client.websocket_connect(
        "/chains/ws/NIFTY?expiry_date=2026-08-28",
        headers={"cookie": f"{SESSION_COOKIE_NAME}={session_id}"},
    ) as ws:
        body = ws.receive_json()

    assert body["symbol"] == "NIFTY"
    assert body["chain"][0]["call"]["ltp"] == 160.0
    mock.assert_awaited_with("ANALYTICS-TOKEN-XYZ", INSTRUMENT_KEYS["NIFTY"], "2026-08-28")


def test_ws_without_market_data_authorization_closes_4401(client):
    """Valid platform session, no Analytics Token: WS closes 4401 so the
    client's HTTP fallback renders the structured 403 state."""
    _user_id, session_id = _make_db_world(analytics_user=False)
    assert ws_close_code(
        client, "/chains/ws/NIFTY?expiry_date=2026-08-28", session_id
    ) == 4401


def test_ws_no_platform_session_closes_4401(client):
    assert ws_close_code(client, "/chains/ws/NIFTY?expiry_date=2026-08-28") == 4401


# --- Test F: GEX credential resolution stays green ----------------------------


def test_gex_resolution_uses_same_authority_as_market_data_resolver(client):
    """The canonical resolver and GEX's underlying authority
    (identity.get_analytics_token) agree for the same user/connection —
    one resolution path, no drift."""
    from app.identity import get_analytics_token as _get_analytics_token

    _user_id, session_id = _make_db_world()
    db = _SessionLocal()
    try:
        credential = _resolve_market_data_token(db, _user_id, "UPSTOX")
        gex_token = _get_analytics_token(db, _user_id, "UPSTOX")
    finally:
        db.close()

    assert credential is not None and credential.source == _ANALYTICS_SOURCE
    assert credential.token == gex_token == "ANALYTICS-TOKEN-XYZ"


def test_gex_pinned_connection_semantics_preserved(client):
    """A pinned connection_id resolves ONLY that connection (GEX's
    never-silently-select contract) — including when it belongs to
    another user."""
    Base.metadata.create_all(bind=_SessionLocal().get_bind())
    db = _SessionLocal()
    try:
        user_a, user_b = str(_uuid4()), str(_uuid4())
        db.add(_User(id=user_a, status="active", identity_source="email", broker_provider=None))
        db.add(_User(id=user_b, status="active", identity_source="email", broker_provider=None))
        db.flush()
        conn_a = _BrokerConnection(
            id=str(_uuid4()),
            user_id=user_a,
            broker="UPSTOX",
            broker_account_id="A-CONN",
            status="connected",
            data_status="active",
            data_source="analytics_token",
            broker_analytics_token_encrypted=_encrypt("A-SECRET-TOKEN"),
            connected_at=datetime.now(_tz.utc),
        )
        db.add(conn_a)
        db.commit()

        # B pinning A's connection id gets NOTHING (fail closed).
        assert _resolve_market_data_token(db, user_b, "UPSTOX", connection_id=conn_a.id) is None
        # A pinning their own connection gets exactly the analytics token.
        own = _resolve_market_data_token(db, user_a, "UPSTOX", connection_id=conn_a.id)
    finally:
        db.close()

    assert own is not None
    assert own.token == "A-SECRET-TOKEN"
    assert own.source == _ANALYTICS_SOURCE
