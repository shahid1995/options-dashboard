"""Day 50 — admin in-process live-verification seam (option-candle probe).

Contract under test — the smallest sanctioned invocation seam for the
existing read-only Phase 7.9 probe:

    admin request
        -> authenticated caller user_id
        -> app.services.market_data_authorization.resolve_market_data_token(...)
        -> MarketDataCredential.token (in memory only)
        -> app.tools.live_verification.verify_option_candle_api(...)
        -> sanitized verification facts

Guarantees proven here:

* Admin authorization is mandatory and cookie-only (F6): anonymous -> 401,
  ordinary users -> 403, and the durable ``users.is_admin`` backstop is
  applied on the same terms as ``/admin/acquisition/run``.
* The credential is resolved for the AUTHENTICATED caller only: the
  request body cannot select a user, a connection, or a token.
* No fallback source is consulted — ``token_store.get_token``, the
  platform ``TokenBridge``/``UpstoxTokenManager`` cache, and browser
  cookies are never the probe credential.
* The resolved token is handed to the probe in-process only and never
  appears in the response, the audit record, or logs.
* Probe results are projected to non-sensitive facts; the four claims and
  ``live_option_oi_established`` keep their exact probe semantics.

Live upstream capability is NOT asserted here: the probe is mocked in
every test. Only an authenticated request against a connected account can
establish ``live_option_oi_established = true``.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.identity import User, create_session_record
from app.main import app
from app.models import OptionCandle
from app.routers.deps import SESSION_COOKIE_NAME
from app.services import token_store
from app.services.admin_audit import list_admin_audit
from app.services.market_data_authorization import MarketDataCredential

ROUTE = "/api/v1/admin/live-verification/option-candle"
LIVE_KEY = "NSE_FO|56930|09-10-2026"
CANDLE_DATE = "2026-10-02"
#: The broker's OWN identity for LIVE_KEY. Real Upstox contract metadata keys
#: an instrument by its two-segment form and appends no expiry segment, so the
#: seam must match on this identity and never trust the requested key's own
#: embedded expiry text.
LIVE_KEY_IDENTITY = "NSE_FO|56930"
#: ISO form of LIVE_KEY's broker-reported expiry, as the seam normalizes it.
LIVE_KEY_EXPIRY = "2026-10-09"
#: A real live option key shape: two segments, no embedded expiry. Freshness
#: for such a key is only establishable from server-side contract metadata.
TWO_SEGMENT_KEY = "NSE_FO|53806"
TWO_SEGMENT_EXPIRY = "2026-10-06"

#: Every fact the broker's own contract rows are required to carry before the
#: seam will treat them as NIFTY expiry authority.  Observed on the live
#: ``/option/contract`` response: all 1918 rows declared exactly these values.
NIFTY_ROW_FACTS = {
    "segment": "NSE_FO",
    "underlying_key": "NSE_INDEX|Nifty 50",
    "underlying_symbol": "NIFTY",
    "exchange": "NSE",
}

#: Contract-metadata payload the seam resolves the authoritative expiry from,
#: mirroring the REAL provider shape observed against the staging broker:
#: two-segment ``instrument_key`` values, expiries in ISO form, and the
#: NIFTY-placing facts above.  One row is deliberately kept in the provider's
#: ``dd-mm-yyyy`` form so both broker calendar formats stay covered.
DEFAULT_CONTRACT_METADATA = {
    "status": "success",
    "data": [
        {
            **NIFTY_ROW_FACTS,
            "instrument_key": LIVE_KEY_IDENTITY,
            "expiry": LIVE_KEY_EXPIRY,
            "instrument_type": "CE",
            "strike_price": 24000.0,
            "lot_size": 65,
        },
        {
            **NIFTY_ROW_FACTS,
            "instrument_key": TWO_SEGMENT_KEY,
            "expiry": "06-10-2026",
            "instrument_type": "CE",
            "strike_price": 22400.0,
            "lot_size": 65,
        },
    ],
}


def _contract_metadata_mock(monkeypatch, payload=None, side_effect=None):
    """Patch the server-side contract-metadata lookup the seam relies on."""
    mock = AsyncMock(
        return_value=DEFAULT_CONTRACT_METADATA if payload is None else payload,
        side_effect=side_effect,
    )
    monkeypatch.setattr("app.services.upstox.get_option_contracts", mock)
    return mock
# Synthetic fixture value. It is NOT a credential, is never sent anywhere,
# and exists only so leak-detection assertions have a unique marker.
PROBE_TOKEN = "unit-test-fixture-value-not-a-credential"

EXPECTED_TOP_LEVEL_KEYS = {
    "status",
    "instrument_key",
    "probe_date_ist",
    "current_ist_date",
    "endpoint_consistency",
    "open_interest_consistency",
    "intraday",
    "historical",
    "instrument_freshness",
    "authoritative_expiry_source",
    "claims",
    "live_option_oi_established",
    "conclusion",
}


# ---------------------------------------------------------------------------
# Fixtures — shared in-memory engine; request DB is the overridden session.
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db_session(engine):
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = TestingSession()
    yield db
    db.close()


@pytest.fixture()
def client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _mk_user(db, *, admin: bool = False) -> User:
    user_id = str(uuid4())
    user = User(
        id=user_id,
        status="active",
        identity_source="upstox",
        broker_provider="UPSTOX",
        broker_user_id=f"d50-{user_id[:8]}",
        is_admin=admin,
    )
    db.add(user)
    db.commit()
    db.expire(user)
    return user


def _login(db, user: User) -> str:
    session_id = token_store.set_token(f"unit-test-session-{user.id[:8]}")
    create_session_record(db, user.id, session_id)
    return session_id


def _cookie(session_id: str) -> dict:
    """The canonical admin transport is the HttpOnly cookie (PR #91 F6)."""
    return {SESSION_COOKIE_NAME: session_id}


def _denied_detail(resp) -> str:
    """Read the rejection detail across both error contracts."""
    body = resp.json()
    if "detail" in body:
        return body["detail"]
    return body.get("error", {}).get("message", "")


@pytest.fixture()
def admin_session(db_session):
    user = _mk_user(db_session, admin=True)
    return _login(db_session, user), user


@pytest.fixture()
def user_session(db_session):
    user = _mk_user(db_session, admin=False)
    return _login(db_session, user), user


# ---------------------------------------------------------------------------
# Synthetic probe/credential builders — the probe is NEVER called for real.
# ---------------------------------------------------------------------------


def _credential(token: str = PROBE_TOKEN) -> MarketDataCredential:
    return MarketDataCredential(
        token=token,
        source="analytics_token",
        connection_id=None,
        broker="UPSTOX",
    )


def _endpoint_evidence(
    *,
    status: str = "ok",
    oi_non_null: int = 6,
    http_status: str = "success (200)",
    error: str | None = None,
) -> dict:
    ok = status == "ok"
    return {
        "label": "endpoint",
        "request": "get_candles(...)",
        "status": status,
        "http_status": http_status,
        "error": error,
        "extraction_error": None,
        "candle_count": 75 if ok else 0,
        "candle_array_length": 7 if ok else None,
        "candle_array_lengths_observed": [7] if ok else [],
        "malformed_row_count": 0,
        "open_interest_field_present": ok,
        "open_interest_non_null_count": oi_non_null if ok else 0,
        "open_interest_sample": [12345.5] if oi_non_null and ok else [],
        "timestamp_format_sample": "2026-10-02T09:15:00+05:30" if ok else None,
        "timezone_offsets_observed": ["+05:30"] if ok else [],
        "first_timestamp": "2026-10-02T09:15:00+05:30" if ok else None,
        "last_timestamp": "2026-10-02T15:29:00+05:30" if ok else None,
        "naive_ist_last_timestamp": "2026-10-02T15:29:00" if ok else None,
        # Internal-only marker: the route must project facts, not echo.
        "internal_debug_marker": "MUST-NOT-LEAK",
    }


def _probe_result(
    *,
    status: str = "success",
    oi_non_null: int = 6,
    endpoint_status: str = "ok",
    verified_unexpired: bool | None = True,
    verified_reason: str = (
        "candles were returned for a key whose embedded expiry is on/after "
        "the current IST date (2026-10-03)"
    ),
    current_session: bool | None = True,
    upstream_error: str | None = None,
    endpoint_http_status: str = "success (200)",
    authoritative_expiry_date: str | None = None,
    authoritative_expiry_source: str | None = None,
    freshness_overrides: dict | None = None,
) -> dict:
    """A realistic ``verify_option_candle_api`` result (semantics verbatim)."""
    intraday = _endpoint_evidence(
        status=endpoint_status,
        oi_non_null=oi_non_null,
        error=upstream_error,
        http_status=endpoint_http_status,
    )
    historical = _endpoint_evidence(
        status=endpoint_status,
        oi_non_null=oi_non_null,
        error=upstream_error,
        http_status=endpoint_http_status,
    )
    accepted = endpoint_status in ("ok", "empty")
    returned = endpoint_status == "ok"
    has_oi = returned and oi_non_null > 0
    claims = {
        "claim_1_endpoint_accepted_live_option_key": accepted,
        "claim_2_endpoint_returned_candles": returned,
        "claim_3_candles_contained_open_interest": has_oi,
        "claim_4_instrument_verified_unexpired": verified_unexpired,
    }
    return {
        "section": "Live Option Instrument Candle Verification",
        "status": status,
        "instrument_key": LIVE_KEY,
        "instrument_key_source": "user-supplied CLI argument (--option-key)",
        "candle_interval": "3-minute (unit=minutes, interval=3)",
        "probe_date_ist": CANDLE_DATE,
        "current_ist_date": "2026-10-03",
        "open_interest_field_index": 6,
        "authoritative_expiry_date": authoritative_expiry_date,
        "authoritative_expiry_source": authoritative_expiry_source,
        "intraday": intraday,
        "historical": historical,
        "endpoint_consistency": (
            "consistent" if intraday["status"] == historical["status"] else "inconsistent"
        ),
        "open_interest_consistency": (
            "consistent"
            if intraday["open_interest_non_null_count"]
            == historical["open_interest_non_null_count"]
            else "not_comparable"
        ),
        "instrument_freshness": {
            "expiry_from_instrument_key": "2026-10-09",
            "key_implies_unexpired": verified_unexpired,
            "authoritative_expiry_date": authoritative_expiry_date,
            "authoritative_expiry_supplied": authoritative_expiry_date is not None,
            "authoritative_expiry_valid": authoritative_expiry_date is not None,
            "authoritative_expiry_implies_unexpired": verified_unexpired,
            "current_ist_date": "2026-10-03",
            "probe_date_ist": CANDLE_DATE,
            "intraday_returned_current_session_candle": current_session,
            "verified_unexpired": verified_unexpired,
            "verified_reason": verified_reason,
            "method": "instrument-key expiry parse vs current IST date",
            **(freshness_overrides or {}),
        },
        "claims": claims,
        "live_option_oi_established": bool(has_oi and verified_unexpired is True),
        "conclusion": "synthetic conclusion (route must pass it through verbatim)",
        "internal_debug_marker": "MUST-NOT-LEAK",
    }


def _install(monkeypatch, credential, *, probe_result=None, probe_side_effect=None):
    """Patch the canonical resolver and the probe; return both mocks."""
    resolver = Mock(return_value=credential)
    monkeypatch.setattr(
        "app.services.market_data_authorization.resolve_market_data_token", resolver
    )
    if probe_side_effect is not None:
        probe = AsyncMock(side_effect=probe_side_effect)
    else:
        probe = AsyncMock(
            return_value=_probe_result() if probe_result is None else probe_result
        )
    monkeypatch.setattr(
        "app.tools.live_verification.verify_option_candle_api", probe
    )
    _contract_metadata_mock(monkeypatch)
    return resolver, probe


def _forbid_platform_credential_sources(monkeypatch) -> None:
    """The platform credential plumbing must never be consulted.

    ``token_store`` is deliberately NOT banned here: the admin SESSION
    validation path legitimately reads session tokens from it (auth, not a
    market-data credential). ``_spy_token_store`` proves that distinction.
    """

    def _boom(*_args, **_kwargs):
        raise AssertionError("forbidden credential source consulted")

    monkeypatch.setattr("app.services.backfill_orchestrator.TokenBridge.get_token", _boom)

    class _ForbiddenManager:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("platform token cache consulted")

    monkeypatch.setattr(
        "app.services.upstox_token_manager.UpstoxTokenManager", _ForbiddenManager
    )


def _spy_token_store(monkeypatch) -> list:
    """Record every ``token_store.get_token`` argument (session auth only)."""
    from app.services import token_store as token_store_module

    original = token_store_module.get_token
    calls: list = []

    def _spy(session_id=None):
        calls.append(session_id)
        return original(session_id)

    monkeypatch.setattr(token_store_module, "get_token", _spy)
    return calls


def _post(client, sid, body, **kwargs):
    return client.post(ROUTE, json=body, cookies=_cookie(sid), **kwargs)


# ---------------------------------------------------------------------------
# 1. Authorization
# ---------------------------------------------------------------------------


class TestSeamAuthorization:
    def test_anonymous_rejected_and_probe_never_invoked(self, client, monkeypatch):
        resolver, probe = _install(monkeypatch, _credential())
        resp = client.post(ROUTE, json={"instrument_key": LIVE_KEY})
        assert resp.status_code == 401
        resolver.assert_not_called()
        probe.assert_not_called()

    def test_ordinary_user_rejected_and_resolver_never_called(
        self, client, user_session, monkeypatch
    ):
        sid, _user = user_session
        resolver, probe = _install(monkeypatch, _credential())
        resp = _post(client, sid, {"instrument_key": LIVE_KEY, "candle_date": CANDLE_DATE})
        assert resp.status_code == 403
        assert _denied_detail(resp) == "Admin privileges required."
        resolver.assert_not_called()
        probe.assert_not_called()

    def test_denied_attempt_is_audited_with_actor(self, client, user_session, db_session):
        sid, user = user_session
        resp = _post(client, sid, {"instrument_key": LIVE_KEY})
        assert resp.status_code == 403
        events = [
            e
            for e in list_admin_audit(db_session)
            if e["action"] == "live_verification.option_candle"
        ]
        assert len(events) == 1
        assert events[0]["result"] == "denied"
        assert events[0]["actor_user_id"] == user.id
        assert events[0]["detail"] == {"reason": "admin_required"}

    def test_header_transport_never_authorizes_admin(
        self, client, admin_session, monkeypatch
    ):
        sid, _admin = admin_session
        resolver, probe = _install(monkeypatch, _credential())
        resp = client.post(
            ROUTE, json={"instrument_key": LIVE_KEY}, headers={"X-Session-Id": sid}
        )
        assert resp.status_code == 401
        resolver.assert_not_called()
        probe.assert_not_called()

    def test_disabled_admin_account_is_rejected(
        self, client, admin_session, db_session, monkeypatch
    ):
        sid, user = admin_session
        user.status = "suspended"
        db_session.commit()
        resolver, probe = _install(monkeypatch, _credential())
        resp = _post(client, sid, {"instrument_key": LIVE_KEY})
        assert resp.status_code == 403
        resolver.assert_not_called()
        probe.assert_not_called()

    def test_request_cannot_select_another_user_or_connection(
        self, client, admin_session, db_session, monkeypatch
    ):
        sid, caller = admin_session
        victim = _mk_user(db_session, admin=True)
        resolver, probe = _install(monkeypatch, _credential())
        resp = _post(
            client,
            sid,
            {
                "instrument_key": LIVE_KEY,
                "user_id": victim.id,
                "actor_user_id": victim.id,
                "connection_id": "conn-not-owned",
                "session_id": "someone-elses-session",
                "token": "request-body-value-that-must-be-ignored",
            },
        )
        assert resp.status_code == 200
        args, _kwargs = resolver.call_args
        assert args[1] == caller.id
        assert victim.id != args[1]
        assert probe.call_args.args[0] == PROBE_TOKEN  # resolved credential, not body

    def test_route_is_absent_outside_the_admin_surface(self, client, admin_session):
        sid, _admin = admin_session
        non_admin = client.post(
            "/api/v1/live-verification/option-candle",
            json={"instrument_key": LIVE_KEY},
            cookies=_cookie(sid),
        )
        unversioned = client.post(
            "/live-verification/option-candle", json={"instrument_key": LIVE_KEY}
        )
        wrong_method = client.get(ROUTE, cookies=_cookie(sid))
        assert non_admin.status_code == 404
        assert unversioned.status_code == 404
        assert wrong_method.status_code == 405


# ---------------------------------------------------------------------------
# 2. Credential resolution
# ---------------------------------------------------------------------------


class TestCredentialResolution:
    def test_canonical_resolver_is_used_for_the_callers_own_user(
        self, client, admin_session, db_session, monkeypatch
    ):
        sid, caller = admin_session
        resolver, probe = _install(monkeypatch, _credential())
        resp = _post(client, sid, {"instrument_key": LIVE_KEY, "candle_date": CANDLE_DATE})
        assert resp.status_code == 200
        resolver.assert_called_once()
        args, kwargs = resolver.call_args
        assert args[0] is db_session
        assert args[1] == caller.id
        assert args[2] == "UPSTOX"
        assert kwargs == {}
        probe.assert_awaited_once_with(
            PROBE_TOKEN,
            LIVE_KEY,
            CANDLE_DATE,
            authoritative_expiry_date=LIVE_KEY_EXPIRY,
        )

    def test_missing_credential_fails_closed(self, client, admin_session, monkeypatch):
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, None)
        resp = _post(client, sid, {"instrument_key": LIVE_KEY})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "MARKET_DATA_NOT_CONNECTED"
        probe.assert_not_called()

    def test_empty_token_credential_fails_closed(
        self, client, admin_session, db_session, monkeypatch
    ):
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential(token=""))
        resp = _post(client, sid, {"instrument_key": LIVE_KEY})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "MARKET_DATA_NOT_CONNECTED"
        probe.assert_not_called()
        events = [
            e
            for e in list_admin_audit(db_session)
            if e["action"] == "live_verification.option_candle"
        ]
        assert events and events[0]["result"] == "failed"
        assert events[0]["detail"] == {"reason": "MARKET_DATA_NOT_CONNECTED"}

    def test_no_fallback_credential_sources_are_consulted(
        self, client, admin_session, monkeypatch
    ):
        sid, _admin = admin_session
        resolver, probe = _install(monkeypatch, _credential())
        _forbid_platform_credential_sources(monkeypatch)
        token_store_calls = _spy_token_store(monkeypatch)
        resp = _post(client, sid, {"instrument_key": LIVE_KEY, "candle_date": CANDLE_DATE})
        assert resp.status_code == 200
        resolver.assert_called_once()
        probe.assert_awaited_once()
        assert probe.call_args.args[0] == PROBE_TOKEN
        # token_store was consulted only to validate the admin SESSION id —
        # never to fetch a market-data credential (no other id was passed,
        # and the session token itself is not the token handed to the probe).
        assert set(token_store_calls) <= {sid}
        assert PROBE_TOKEN != token_store.get_token(sid)


# ---------------------------------------------------------------------------
# 3. Probe invocation and secret handling
# ---------------------------------------------------------------------------


class TestProbeInvocation:
    def test_probe_runs_in_process_with_the_resolved_token(
        self, client, admin_session, monkeypatch
    ):
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        resp = _post(
            client,
            sid,
            {"instrument_key": f"  {LIVE_KEY}  ", "candle_date": CANDLE_DATE},
        )
        assert resp.status_code == 200
        probe.assert_awaited_once_with(
            PROBE_TOKEN,
            LIVE_KEY,
            CANDLE_DATE,
            authoritative_expiry_date=LIVE_KEY_EXPIRY,
        )

    def test_omitted_or_blank_candle_date_passes_none(
        self, client, admin_session, monkeypatch
    ):
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        assert _post(client, sid, {"instrument_key": LIVE_KEY}).status_code == 200
        probe.assert_awaited_once_with(
            PROBE_TOKEN,
            LIVE_KEY,
            None,
            authoritative_expiry_date=LIVE_KEY_EXPIRY,
        )
        probe.reset_mock()
        assert (
            _post(client, sid, {"instrument_key": LIVE_KEY, "candle_date": "  "}).status_code
            == 200
        )
        probe.assert_awaited_once_with(
            PROBE_TOKEN,
            LIVE_KEY,
            None,
            authoritative_expiry_date=LIVE_KEY_EXPIRY,
        )

    def test_invalid_inputs_are_rejected_before_any_credential_work(
        self, client, admin_session, monkeypatch
    ):
        sid, _admin = admin_session
        resolver, probe = _install(monkeypatch, _credential())
        cases = [
            {"instrument_key": "   "},
            {"instrument_key": LIVE_KEY, "candle_date": "10-02-2026"},
            {"instrument_key": LIVE_KEY, "candle_date": "2026-13-40"},
            {},
        ]
        for body in cases:
            resp = _post(client, sid, body)
            assert resp.status_code == 422, body
            assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        resolver.assert_not_called()
        probe.assert_not_called()

    def test_token_never_appears_in_response_audit_or_logs(
        self, client, admin_session, db_session, monkeypatch, caplog
    ):
        caplog.set_level(logging.DEBUG)
        sid, _admin = admin_session
        _resolver, _probe = _install(monkeypatch, _credential())
        resp = _post(client, sid, {"instrument_key": LIVE_KEY, "candle_date": CANDLE_DATE})
        assert resp.status_code == 200
        assert PROBE_TOKEN not in resp.text
        assert PROBE_TOKEN not in repr(list_admin_audit(db_session))
        assert PROBE_TOKEN not in caplog.text

    def test_probe_result_is_projected_not_echoed(
        self, client, admin_session, monkeypatch
    ):
        sid, _admin = admin_session
        _resolver, _probe = _install(monkeypatch, _credential())
        resp = _post(client, sid, {"instrument_key": LIVE_KEY, "candle_date": CANDLE_DATE})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == EXPECTED_TOP_LEVEL_KEYS
        assert "internal_debug_marker" not in body
        assert "internal_debug_marker" not in body["intraday"]
        assert "MUST-NOT-LEAK" not in resp.text


# ---------------------------------------------------------------------------
# 4. Result handling — the four claims keep their exact probe semantics
# ---------------------------------------------------------------------------


class TestResultHandling:
    def test_accepted_with_open_interest_and_current_expiry(
        self, client, admin_session, monkeypatch
    ):
        sid, _admin = admin_session
        payload = _probe_result()
        _resolver, _probe = _install(monkeypatch, _credential(), probe_result=payload)
        resp = _post(client, sid, {"instrument_key": LIVE_KEY, "candle_date": CANDLE_DATE})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["claims"] == {
            "claim_1_endpoint_accepted_live_option_key": True,
            "claim_2_endpoint_returned_candles": True,
            "claim_3_candles_contained_open_interest": True,
            "claim_4_instrument_verified_unexpired": True,
        }
        assert body["instrument_freshness"]["verified_unexpired"] is True
        assert body["live_option_oi_established"] is True
        assert body["conclusion"] == payload["conclusion"]
        assert body["probe_date_ist"] == CANDLE_DATE
        assert body["current_ist_date"] == "2026-10-03"

    def test_candles_without_open_interest_never_establish_live_oi(
        self, client, admin_session, monkeypatch
    ):
        sid, _admin = admin_session
        _resolver, _probe = _install(
            monkeypatch, _credential(), probe_result=_probe_result(oi_non_null=0)
        )
        resp = _post(client, sid, {"instrument_key": LIVE_KEY})
        assert resp.status_code == 200
        body = resp.json()
        assert body["claims"]["claim_2_endpoint_returned_candles"] is True
        assert body["claims"]["claim_3_candles_contained_open_interest"] is False
        assert body["live_option_oi_established"] is False

    def test_expired_option_is_not_reported_as_unexpired(
        self, client, admin_session, monkeypatch
    ):
        sid, _admin = admin_session
        _resolver, _probe = _install(
            monkeypatch,
            _credential(),
            probe_result=_probe_result(
                verified_unexpired=False,
                verified_reason=(
                    "the key embeds an expiry before the current IST date (2026-10-03)"
                ),
                current_session=False,
            ),
        )
        resp = _post(client, sid, {"instrument_key": LIVE_KEY})
        assert resp.status_code == 200
        body = resp.json()
        assert body["instrument_freshness"]["verified_unexpired"] is False
        assert body["claims"]["claim_4_instrument_verified_unexpired"] is False
        assert body["live_option_oi_established"] is False

    def test_unconfirmed_freshness_never_establishes_live_oi(
        self, client, admin_session, monkeypatch
    ):
        sid, _admin = admin_session
        _resolver, _probe = _install(
            monkeypatch,
            _credential(),
            probe_result=_probe_result(
                verified_unexpired=None,
                verified_reason=(
                    "no current-session candle and no embedded future expiry; "
                    "not established"
                ),
                current_session=None,
            ),
        )
        resp = _post(client, sid, {"instrument_key": LIVE_KEY})
        assert resp.status_code == 200
        body = resp.json()
        assert body["instrument_freshness"]["verified_unexpired"] is None
        assert body["claims"]["claim_4_instrument_verified_unexpired"] is None
        assert body["live_option_oi_established"] is False

    def test_upstream_error_is_a_safe_verification_failure(
        self, client, admin_session, db_session, monkeypatch
    ):
        sid, _admin = admin_session
        _resolver, _probe = _install(
            monkeypatch,
            _credential(),
            probe_result=_probe_result(
                status="error",
                endpoint_status="error",
                oi_non_null=0,
                verified_unexpired=None,
                current_session=None,
                upstream_error="UpstoxError(401): unauthorized",
                endpoint_http_status="UpstoxError(401)",
            ),
        )
        resp = _post(client, sid, {"instrument_key": LIVE_KEY})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "error"
        assert body["live_option_oi_established"] is False
        assert body["intraday"]["http_status"] == "UpstoxError(401)"
        assert body["intraday"]["error"] == "UpstoxError(401): unauthorized"
        assert body["claims"]["claim_1_endpoint_accepted_live_option_key"] is False
        events = [
            e
            for e in list_admin_audit(db_session)
            if e["action"] == "live_verification.option_candle"
        ]
        assert events and events[0]["detail"]["status"] == "error"
        assert events[0]["detail"]["live_option_oi_established"] is False

    def test_probe_exception_becomes_502_without_internal_details(
        self, client, admin_session, db_session, monkeypatch, caplog
    ):
        caplog.set_level(logging.DEBUG)
        sid, _admin = admin_session
        secret = "unit-test-leak-marker-not-a-credential"
        _resolver, _probe = _install(
            monkeypatch,
            _credential(),
            probe_side_effect=RuntimeError(f"upstream exploded: {secret}"),
        )
        resp = _post(client, sid, {"instrument_key": LIVE_KEY})
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "UPSTREAM_ERROR"
        assert resp.json()["error"]["message"] == "Live option verification probe failed."
        assert secret not in resp.text
        assert secret not in caplog.text
        events = [
            e
            for e in list_admin_audit(db_session)
            if e["action"] == "live_verification.option_candle"
        ]
        assert events and events[0]["result"] == "failed"
        assert events[0]["detail"] == {"error_class": "RuntimeError"}
        assert secret not in repr(events)


# ---------------------------------------------------------------------------
# 5. Audit and read-only guarantees
# ---------------------------------------------------------------------------


class TestAuditAndReadOnly:
    def test_success_audit_records_safe_metadata_only(
        self, client, admin_session, db_session, monkeypatch
    ):
        sid, caller = admin_session
        _resolver, _probe = _install(monkeypatch, _credential())
        resp = _post(client, sid, {"instrument_key": LIVE_KEY, "candle_date": CANDLE_DATE})
        assert resp.status_code == 200
        events = [
            e
            for e in list_admin_audit(db_session)
            if e["action"] == "live_verification.option_candle"
        ]
        assert len(events) == 1
        event = events[0]
        assert event["actor_user_id"] == caller.id
        assert event["result"] == "success"
        assert event["target"]["instrument_key"] == LIVE_KEY
        assert event["target"]["candle_date"] == CANDLE_DATE
        assert event["detail"] == {
            "status": "success",
            "live_option_oi_established": True,
        }
        assert PROBE_TOKEN not in repr(event)

    def test_no_candle_rows_are_persisted(
        self, client, admin_session, db_session, monkeypatch
    ):
        sid, _admin = admin_session
        _resolver, _probe = _install(monkeypatch, _credential())
        assert db_session.query(OptionCandle).count() == 0
        resp = _post(client, sid, {"instrument_key": LIVE_KEY, "candle_date": CANDLE_DATE})
        assert resp.status_code == 200
        assert db_session.query(OptionCandle).count() == 0


# ---------------------------------------------------------------------------
# 7. Instrument-key grammar (path-injection hardening)
# ---------------------------------------------------------------------------
#
# ``instrument_key`` is placed verbatim into the Upstox V3 request path while
# using the caller's own credential.  A value carrying ``/``, ``?``, ``#`` or a
# ``..`` segment could therefore redirect that authenticated GET at another
# endpoint.  These tests pin the accepted grammar to the key forms this
# repository actually uses and prove nothing path-like gets through.

from app.api.v1.admin import OptionCandleProbeIn  # noqa: E402
from pydantic import ValidationError  # noqa: E402

#: Key forms that occur in this repository and must keep working.
VALID_INSTRUMENT_KEYS = [
    "NSE_FO|53806",                    # current option, no expiry suffix
    "NSE_FO|47983|31-12-2099",         # expired option, dd-mm-yyyy
    "NSE_FO|TEST_A|2025-04-17",        # expired option, yyyy-mm-dd
    "NSE_INDEX|Nifty 50",              # index with a space
    "NSE_INDEX|NIFTY MID SELECT",      # index with several spaces
    "NSE_INDEX|Nifty Fin Service",
    "BSE_INDEX|SENSEX",
    "BSE_INDEX|SENSEX50",
]

#: Values that must never reach the upstream path builder.
PATH_LIKE_INSTRUMENT_KEYS = [
    "NSE_FO|53806/../../market-quote",
    "NSE_FO|53806/intraday",
    "..",
    "../..",
    "NSE_FO|../..",
    "NSE_FO|53806?instrument_key=NSE_FO|1",
    "NSE_FO|53806#fragment",
    "NSE_FO|53806%2F..%2Fadmin",
    "NSE_FO|53806?unit=minutes",
    "/absolute/path",
    "NSE_FO|53806\\..\\admin",
    "NSE_FO",
    "|53806",
    "NSE_FO|",
    "NSE_FO|53806|31-12-2099|extra",
    "NSE_FO|53806|31-12-209x",
    "lowercase_segment|53806",
    "NSE FO|53806",
    "NSE_FO|53806|not-a-date",
    "A" * 200,
    "NSE_FO|" + "9" * 200,
]


class TestInstrumentKeyGrammar:
    """Finding A — the request model must reject path/query manipulation."""

    def test_valid_repository_key_forms_are_accepted(self):
        for key in VALID_INSTRUMENT_KEYS:
            model = OptionCandleProbeIn(instrument_key=key)
            assert model.instrument_key == key

    @pytest.mark.parametrize("key", PATH_LIKE_INSTRUMENT_KEYS)
    def test_path_and_query_manipulation_is_rejected(self, key):
        with pytest.raises(ValidationError):
            OptionCandleProbeIn(instrument_key=key)

    def test_surrounding_whitespace_is_normalized_not_forwarded(self):
        model = OptionCandleProbeIn(instrument_key="  NSE_FO|53806  ")
        assert model.instrument_key == "NSE_FO|53806"

    @pytest.mark.parametrize(
        "raw", ["  NSE_FO|53806  ", " NSE_FO|53806 ", "NSE_FO|53806 "]
    )
    def test_ordinary_spaces_are_stripped_before_the_path_is_built(self, raw):
        """Only the NORMALIZED value can ever reach the path builder.

        Ordinary SPACES are normalized.  Control characters are rejected by
        the raw-input check instead, so they can never be normalized away.
        """
        model = OptionCandleProbeIn(instrument_key=raw)
        assert model.instrument_key == "NSE_FO|53806"
        assert not any(c.isspace() and c != " " for c in model.instrument_key)

    def test_whitespace_only_is_rejected(self):
        for value in ("   ", "\t\n", ""):
            with pytest.raises(ValidationError):
                OptionCandleProbeIn(instrument_key=value)

    def test_accepted_keys_contain_no_path_or_query_metacharacter(self):
        """Belt-and-braces: the allowlist itself cannot express a delimiter."""
        for key in VALID_INSTRUMENT_KEYS:
            for bad in ("/", "\\", "?", "#", "%", ".."):
                assert bad not in key

    def test_route_rejects_path_like_key_before_any_credential_work(
        self, client, admin_session, monkeypatch
    ):
        sid, _admin = admin_session
        resolver, probe = _install(monkeypatch, _credential())
        for key in ["NSE_FO|53806/../admin", "NSE_FO|1?x=1", "NSE_FO|1#f"]:
            resp = _post(client, sid, {"instrument_key": key})
            assert resp.status_code == 422, (key, resp.text)
            assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        # The upstream call must never have been reached.
        resolver.assert_not_called()
        probe.assert_not_called()

    def test_route_still_accepts_a_valid_key(self, client, admin_session, monkeypatch):
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        resp = _post(client, sid, {"instrument_key": LIVE_KEY})
        assert resp.status_code == 200
        probe.assert_awaited_once()

class TestAuthoritativeExpiryResolution:
    """The expiry must come from broker contract metadata, never the request."""

    def test_two_segment_key_resolves_expiry_from_server_side_metadata(
        self, client, admin_session, monkeypatch
    ):
        """A key with no embedded expiry still reaches the probe with a real one."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        metadata = _contract_metadata_mock(monkeypatch)

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY, "candle_date": CANDLE_DATE})

        assert resp.status_code == 200
        metadata.assert_awaited_once()
        probe.assert_awaited_once_with(
            PROBE_TOKEN,
            TWO_SEGMENT_KEY,
            CANDLE_DATE,
            authoritative_expiry_date=TWO_SEGMENT_EXPIRY,
        )

    def test_response_reports_the_resolved_expiry_without_claiming_provenance(
        self, client, admin_session, monkeypatch
    ):
        sid, _admin = admin_session
        _install(monkeypatch, _credential(), probe_result=_probe_result(
            authoritative_expiry_date=TWO_SEGMENT_EXPIRY,
            freshness_overrides={
                "expiry_from_instrument_key": None,
                "key_implies_unexpired": None,
                "authoritative_expiry_date": TWO_SEGMENT_EXPIRY,
                "authoritative_expiry_supplied": True,
                "authoritative_expiry_valid": True,
                "authoritative_expiry_implies_unexpired": True,
                "intraday_returned_current_session_candle": None,
                "verified_unexpired": True,
                "verified_reason": "authoritative expiry is on/after today",
                "method": "authoritative expiry vs current IST date",
            },
            authoritative_expiry_source=(
                "caller-supplied (provenance not verified by this tool)"
            ),
        ))
        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 200
        body = resp.json()
        assert body["instrument_freshness"]["authoritative_expiry_date"] == TWO_SEGMENT_EXPIRY
        assert body["instrument_freshness"]["authoritative_expiry_valid"] is True
        assert "provenance not verified" in body["authoritative_expiry_source"]

    def test_unmatched_key_fails_closed_before_any_probe(
        self, client, admin_session, db_session, monkeypatch
    ):
        """An unknown key can never be probed with a guessed expiry."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(monkeypatch, payload={"status": "success", "data": []})

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "EXPIRY_UNRESOLVED"
        probe.assert_not_called()
        events = [
            e for e in list_admin_audit(db_session)
            if e["action"] == "live_verification.option_candle"
        ]
        assert events[-1]["result"] == "failed"
        # The audit reason carries the same machine code and is NOT scrubbed
        # by the audit sanitizer's 28-char value-shape rule.
        assert events[-1]["detail"]["reason"] == "EXPIRY_UNRESOLVED"

    def test_three_segment_key_matches_the_brokers_two_segment_identity(
        self, client, admin_session, monkeypatch
    ):
        """The candle endpoint's 3-segment form must still resolve.

        Real Upstox contract metadata keys the instrument by its two-segment
        form; the requested key's third segment is untrusted expiry text that
        is cross-checked, never used as the authority.
        """
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        metadata = _contract_metadata_mock(monkeypatch)

        resp = _post(client, sid, {"instrument_key": LIVE_KEY})

        assert resp.status_code == 200, resp.text
        metadata.assert_awaited_once()
        probe.assert_awaited_once_with(
            PROBE_TOKEN,
            LIVE_KEY,
            None,
            authoritative_expiry_date=LIVE_KEY_EXPIRY,
        )

    def test_embedded_expiry_disagreeing_with_the_broker_fails_closed(
        self, client, admin_session, monkeypatch
    ):
        """Key text that contradicts authoritative metadata is never trusted."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(monkeypatch, payload={
            "status": "success",
            "data": [
                {
                    "instrument_key": LIVE_KEY_IDENTITY,
                    "expiry": "2026-10-06",   # broker says 06-10
                    "instrument_type": "CE",
                },
            ],
        })

        # LIVE_KEY claims 09-10-2026; the broker says 2026-10-06.
        resp = _post(client, sid, {"instrument_key": LIVE_KEY})

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "EXPIRY_UNRESOLVED"
        probe.assert_not_called()

    def test_unparseable_broker_expiry_fails_closed(self, client, admin_session, monkeypatch
    ):
        """Only a real calendar date may become authoritative metadata."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(monkeypatch, payload={
            "status": "success",
            "data": [{"instrument_key": TWO_SEGMENT_KEY, "expiry": "not-a-date"}],
        })

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 422
        probe.assert_not_called()

    def test_contract_metadata_failure_is_surfaced_and_audited(
        self, client, admin_session, db_session, monkeypatch
    ):
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(monkeypatch, side_effect=RuntimeError("upstream down"))

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "CONTRACT_METADATA_FAILED"
        probe.assert_not_called()
        events = [
            e for e in list_admin_audit(db_session)
            if e["action"] == "live_verification.option_candle"
        ]
        assert events[-1]["result"] == "failed"
        assert events[-1]["detail"]["reason"] == "CONTRACT_METADATA_FAILED"
        assert events[-1]["detail"]["error_class"] == "RuntimeError"

    def test_request_body_cannot_supply_or_override_an_expiry(
        self, client, admin_session, monkeypatch
    ):
        """Operator-supplied expiry text is ignored; the broker value wins."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())

        resp = _post(client, sid, {
            "instrument_key": TWO_SEGMENT_KEY,
            "authoritative_expiry_date": "2099-01-01",
            "expiry": "2099-01-01",
        })

        assert resp.status_code == 200
        probe.assert_awaited_once_with(
            PROBE_TOKEN,
            TWO_SEGMENT_KEY,
            None,
            authoritative_expiry_date=TWO_SEGMENT_EXPIRY,
        )


# ---------------------------------------------------------------------------
# 3. Supported universe: NIFTY options only
# ---------------------------------------------------------------------------

#: Keys this seam does NOT support.  ``get_option_contracts`` is queried for
#: the Nifty 50 underlying only, so none of these can ever resolve a
#: trustworthy expiry here and each must fail closed.
NON_NIFTY_OPTION_KEYS = [
    "BSE_FO|51892",        # another exchange's option universe
    "NSE_EQ|RELIANCE",     # an equity, not an option contract
    "NSE_INDEX|Nifty 50",  # the index itself, not an option contract
    "BSE_INDEX|SENSEX",    # another exchange's index
]


class TestNiftyOptionOnlyScope:
    """The seam declares one universe and refuses everything outside it."""

    def test_nifty_option_identity_is_inside_the_supported_scope(self):
        from app.api.v1.admin import _supports_nifty_option_identity

        assert _supports_nifty_option_identity(TWO_SEGMENT_KEY) is True
        assert _supports_nifty_option_identity(LIVE_KEY) is True
        # Normalization happens before the scope question is asked.
        assert _supports_nifty_option_identity("   " + TWO_SEGMENT_KEY + "   ") is True

    @pytest.mark.parametrize("key", NON_NIFTY_OPTION_KEYS)
    def test_keys_outside_the_nifty_option_universe_are_unsupported(self, key):
        from app.api.v1.admin import _supports_nifty_option_identity

        assert _supports_nifty_option_identity(key) is False

    def test_a_malformed_key_is_never_inside_the_supported_scope(self):
        from app.api.v1.admin import _supports_nifty_option_identity

        for bad in ("", "NSE_FO", "NSE_FO|", "|53806", None, 53806):
            assert _supports_nifty_option_identity(bad) is False

    @pytest.mark.parametrize("key", NON_NIFTY_OPTION_KEYS)
    def test_unsupported_key_fails_closed_before_any_broker_work(
        self, client, admin_session, db_session, monkeypatch, key
    ):
        """Out of scope: no credential use, no metadata call, no probe."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        metadata = _contract_metadata_mock(monkeypatch)

        resp = _post(client, sid, {"instrument_key": key})

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "EXPIRY_UNRESOLVED"
        metadata.assert_not_called()
        probe.assert_not_called()
        events = [
            e for e in list_admin_audit(db_session)
            if e["action"] == "live_verification.option_candle"
        ]
        assert events[-1]["result"] == "failed"
        assert events[-1]["detail"]["reason"] == "EXPIRY_UNRESOLVED"

    def test_unresolved_explanation_states_the_supported_scope(
        self, client, admin_session, monkeypatch
    ):
        """The failure message declares NIFTY-only, never arbitrary coverage."""
        sid, _admin = admin_session
        _install(monkeypatch, _credential())
        _contract_metadata_mock(monkeypatch, payload={"status": "success", "data": []})

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 422
        message = resp.json()["error"]["message"]
        assert "NSE_FO" in message
        assert "NIFTY option contracts only" in message
        assert "arbitrary" not in message.lower()


# ---------------------------------------------------------------------------
# 4. Broker identity matching across key renderings
# ---------------------------------------------------------------------------

def _row(instrument_key, expiry, **overrides):
    row = dict(NIFTY_ROW_FACTS)
    row["instrument_key"] = instrument_key
    row["expiry"] = expiry
    row.update(overrides)
    return row


def _payload(*rows):
    return {"status": "success", "data": list(rows)}


class TestBrokerIdentityMatching:
    """Matching compares broker-owned identity in every key rendering."""

    def test_two_segment_metadata_row_matches_a_two_segment_request(
        self, client, admin_session, monkeypatch
    ):
        """What ``/option/contract`` really returns must keep working."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(
            monkeypatch,
            payload=_payload(_row(TWO_SEGMENT_KEY, TWO_SEGMENT_EXPIRY)),
        )

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 200
        probe.assert_awaited_once_with(
            PROBE_TOKEN,
            TWO_SEGMENT_KEY,
            None,
            authoritative_expiry_date=TWO_SEGMENT_EXPIRY,
        )

    def test_three_segment_metadata_row_matches_the_same_identity(
        self, client, admin_session, monkeypatch
    ):
        """A three-segment broker key names the same contract, not another one."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(
            monkeypatch,
            payload=_payload(
                _row(TWO_SEGMENT_KEY + "|06-10-2026", TWO_SEGMENT_EXPIRY)
            ),
        )

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 200
        probe.assert_awaited_once_with(
            PROBE_TOKEN,
            TWO_SEGMENT_KEY,
            None,
            authoritative_expiry_date=TWO_SEGMENT_EXPIRY,
        )

    def test_three_segment_metadata_row_matches_a_three_segment_request(
        self, client, admin_session, monkeypatch
    ):
        """Both sides three-segment still reduce to the same broker identity."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(
            monkeypatch,
            payload=_payload(_row(LIVE_KEY, LIVE_KEY_EXPIRY)),
        )

        resp = _post(client, sid, {"instrument_key": LIVE_KEY})

        assert resp.status_code == 200
        probe.assert_awaited_once_with(
            PROBE_TOKEN,
            LIVE_KEY,
            None,
            authoritative_expiry_date=LIVE_KEY_EXPIRY,
        )

    def test_whitespace_padded_metadata_key_still_matches(
        self, client, admin_session, monkeypatch
    ):
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(
            monkeypatch,
            payload=_payload(_row("  " + TWO_SEGMENT_KEY + "  ", TWO_SEGMENT_EXPIRY)),
        )

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 200
        probe.assert_awaited_once_with(
            PROBE_TOKEN,
            TWO_SEGMENT_KEY,
            None,
            authoritative_expiry_date=TWO_SEGMENT_EXPIRY,
        )

    def test_row_with_all_nifty_facts_is_accepted(
        self, client, admin_session, monkeypatch
    ):
        """The baseline the missing-field cases below are measured against."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(
            monkeypatch,
            payload=_payload(_row(TWO_SEGMENT_KEY, TWO_SEGMENT_EXPIRY)),
        )

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 200
        probe.assert_awaited_once_with(
            PROBE_TOKEN,
            TWO_SEGMENT_KEY,
            None,
            authoritative_expiry_date=TWO_SEGMENT_EXPIRY,
        )

    @pytest.mark.parametrize("missing", ["segment", "underlying_key", "underlying_symbol"])
    def test_row_missing_a_nifty_fact_fails_closed(
        self, client, admin_session, monkeypatch, missing
    ):
        """Absence is not consent: an unproven universe is never authority.

        The broker ships all three facts on every ``/option/contract`` row, so a
        row missing one is not evidence of a NIFTY contract.  Skipping the check
        would let a row from an unknown universe pass as NIFTY authority.
        """
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        row = _row(TWO_SEGMENT_KEY, TWO_SEGMENT_EXPIRY)
        del row[missing]
        _contract_metadata_mock(monkeypatch, payload=_payload(row))

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "EXPIRY_UNRESOLVED"
        probe.assert_not_called()

    @pytest.mark.parametrize("missing", ["segment", "underlying_key", "underlying_symbol"])
    def test_row_with_a_blank_nifty_fact_fails_closed(
        self, client, admin_session, monkeypatch, missing
    ):
        """An empty string proves nothing either."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(
            monkeypatch,
            payload=_payload(_row(TWO_SEGMENT_KEY, TWO_SEGMENT_EXPIRY, **{missing: "   "})),
        )

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "EXPIRY_UNRESOLVED"
        probe.assert_not_called()

    @pytest.mark.parametrize("missing", ["segment", "underlying_key", "underlying_symbol"])
    def test_row_with_a_non_string_nifty_fact_fails_closed(
        self, client, admin_session, monkeypatch, missing
    ):
        """A non-string declaration is unusable, not skippable."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(
            monkeypatch,
            payload=_payload(_row(TWO_SEGMENT_KEY, TWO_SEGMENT_EXPIRY, **{missing: None})),
        )

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "EXPIRY_UNRESOLVED"
        probe.assert_not_called()

    # -- fail-closed rows ------------------------------------------------

    def test_metadata_row_contradicting_its_own_expiry_field_fails_closed(
        self, client, admin_session, monkeypatch
    ):
        """A row whose key text contradicts its expiry is not authority."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(
            monkeypatch,
            payload=_payload(_row(TWO_SEGMENT_KEY + "|20-10-2026", TWO_SEGMENT_EXPIRY)),
        )

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "EXPIRY_UNRESOLVED"
        probe.assert_not_called()

    def test_metadata_row_with_an_unreadable_expiry_fails_closed(
        self, client, admin_session, monkeypatch
    ):
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(
            monkeypatch,
            payload=_payload(_row(TWO_SEGMENT_KEY, "not-a-date")),
        )

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "EXPIRY_UNRESOLVED"
        probe.assert_not_called()

    def test_row_declaring_a_non_nifty_underlying_is_not_authority(
        self, client, admin_session, monkeypatch
    ):
        """A non-NIFTY row is never used as NIFTY expiry authority."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(
            monkeypatch,
            payload=_payload(
                _row(
                    TWO_SEGMENT_KEY,
                    TWO_SEGMENT_EXPIRY,
                    underlying_key="NSE_INDEX|NIFTY MID SELECT",
                    underlying_symbol="NIFTY MID SELECT",
                )
            ),
        )

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "EXPIRY_UNRESOLVED"
        probe.assert_not_called()

    def test_two_rows_disagreeing_about_one_contract_fail_closed(
        self, client, admin_session, monkeypatch
    ):
        """Ambiguous broker metadata is a failure, not a coin flip."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(
            monkeypatch,
            payload=_payload(
                _row(TWO_SEGMENT_KEY, TWO_SEGMENT_EXPIRY),
                _row(TWO_SEGMENT_KEY + "|13-10-2026", "2026-10-13"),
            ),
        )

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "EXPIRY_UNRESOLVED"
        probe.assert_not_called()

    def test_duplicate_rows_that_agree_are_accepted(
        self, client, admin_session, monkeypatch
    ):
        """Repeated identical rows are one fact, not an ambiguity."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(
            monkeypatch,
            payload=_payload(
                _row(TWO_SEGMENT_KEY, TWO_SEGMENT_EXPIRY),
                _row(TWO_SEGMENT_KEY, TWO_SEGMENT_EXPIRY),
            ),
        )

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY})

        assert resp.status_code == 200
        probe.assert_awaited_once_with(
            PROBE_TOKEN,
            TWO_SEGMENT_KEY,
            None,
            authoritative_expiry_date=TWO_SEGMENT_EXPIRY,
        )

    def test_embedded_expiry_disagreeing_with_the_broker_fails_closed(
        self, client, admin_session, monkeypatch
    ):
        """Caller key text never overrides the broker's own expiry."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(
            monkeypatch,
            payload=_payload(_row(TWO_SEGMENT_KEY, TWO_SEGMENT_EXPIRY)),
        )

        resp = _post(client, sid, {"instrument_key": TWO_SEGMENT_KEY + "|20-10-2026"})

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "EXPIRY_UNRESOLVED"
        probe.assert_not_called()

    def test_body_supplied_expiry_stays_ignored_under_the_new_scope_check(
        self, client, admin_session, monkeypatch
    ):
        """The scope change must not have opened a body-supplied expiry."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(
            monkeypatch,
            payload=_payload(_row(TWO_SEGMENT_KEY, TWO_SEGMENT_EXPIRY)),
        )

        resp = _post(
            client,
            sid,
            {
                "instrument_key": TWO_SEGMENT_KEY,
                "authoritative_expiry_date": "2099-01-01",
            },
        )

        assert resp.status_code == 200
        probe.assert_awaited_once_with(
            PROBE_TOKEN,
            TWO_SEGMENT_KEY,
            None,
            authoritative_expiry_date=TWO_SEGMENT_EXPIRY,
        )


class TestControlCharacterRouteBoundary:
    """A rejected key must die at the boundary: no credential, no broker call."""

    @pytest.mark.parametrize(
        "raw",
        [
            "NSE_FO|53806" + chr(13) + chr(10),
            "NSE_FO|53806" + chr(9),
            chr(13) + chr(10) + "NSE_FO|53806",
            "NSE_FO|53" + chr(13) + "806",
            "NSE_FO|53" + chr(10) + "806",
            "NSE_FO|53" + chr(9) + "806",
        ],
    )
    def test_control_character_keys_are_refused_with_zero_side_effects(
        self, client, admin_session, monkeypatch, raw
    ):
        sid, _admin = admin_session
        resolver, probe = _install(monkeypatch, _credential())
        metadata = _contract_metadata_mock(monkeypatch)

        resp = _post(client, sid, {"instrument_key": raw})

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        resolver.assert_not_called()
        metadata.assert_not_called()
        probe.assert_not_called()

    @pytest.mark.parametrize(
        "raw", ["  NSE_FO|53806  ", " NSE_FO|53806 ", "NSE_FO|53806 "]
    )
    def test_ordinary_spaces_still_normalize_and_reach_the_probe(
        self, client, admin_session, monkeypatch, raw
    ):
        """Ordinary SPACE padding reaches the probe as the stripped key."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        _contract_metadata_mock(monkeypatch)

        resp = _post(client, sid, {"instrument_key": raw})

        assert resp.status_code == 200
        assert probe.call_args.args[1] == TWO_SEGMENT_KEY



    @pytest.mark.parametrize(
        "raw",
        [
            "NSE_FO|53806" + chr(0x85),
            chr(0x2028) + "NSE_FO|53806",
            "NSE_FO|53806" + chr(0x2029),
            "NSE_FO|53806" + chr(0xA0),
            "NSE_FO|53806" + chr(0x3000),
        ],
    )
    def test_unicode_whitespace_keys_are_refused_with_zero_side_effects(
        self, client, admin_session, monkeypatch, raw
    ):
        """422 with no credential resolution and no broker or probe call."""
        sid, _admin = admin_session
        resolver, probe = _install(monkeypatch, _credential())
        metadata = _contract_metadata_mock(monkeypatch)

        resp = _post(client, sid, {"instrument_key": raw})

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        resolver.assert_not_called()
        metadata.assert_not_called()
        probe.assert_not_called()
class TestInstrumentKeyControlCharacters:
    """Control characters must never survive the request boundary."""

    @pytest.mark.parametrize(
        "key",
        [
            "NSE_FO|53806" + chr(0),
            "NSE_FO|53806" + chr(127),
            "NSE_FO|53" + chr(9) + "806",
            "NSE_FO|53" + chr(10) + "806",
            "NSE_FO|53" + chr(13) + "806",
        ],
    )
    def test_interior_control_character_keys_are_rejected(self, key):
        """A control character inside the key can never be normalized away."""
        with pytest.raises(ValidationError):
            OptionCandleProbeIn(instrument_key=key)

    @pytest.mark.parametrize(
        "raw",
        [
            "NSE_FO|53806" + chr(13) + chr(10),
            "NSE_FO|53806" + chr(9),
            chr(10) + "NSE_FO|53806",
            chr(9) + "NSE_FO|53806",
            chr(13) + "NSE_FO|53806" + chr(9),
            "NSE_FO|53806" + chr(0),
            "NSE_FO|53806" + chr(127),
            "NSE_FO|53806" + chr(11) + " ",
        ],
    )
    def test_edge_control_characters_are_rejected_not_stripped(self, raw):
        """A trailing CRLF is NOT ordinary padding and must never normalize away.

        ``str.strip()`` would delete a leading/trailing CR, LF or TAB, which
        made injected control characters indistinguishable from padding.  The
        check now runs against the raw input, before normalization.
        """
        with pytest.raises(ValidationError):
            OptionCandleProbeIn(instrument_key=raw)

    @pytest.mark.parametrize(
        "raw",
        [
            "NSE_FO|53" + chr(13) + "806",
            "NSE_FO|53" + chr(10) + "806",
            "NSE_FO|53" + chr(9) + "806",
            "NSE_FO|53" + chr(11) + "806",
            "NSE_FO|53" + chr(127) + "806",
        ],
    )
    def test_embedded_control_characters_are_rejected(self, raw):
        with pytest.raises(ValidationError):
            OptionCandleProbeIn(instrument_key=raw)

    @pytest.mark.parametrize(
        "key",
        [
            "NSE_FO|53806/../admin",
            "NSE_FO|1?x=1",
            "NSE_FO|1#frag",
            "NSE_FO|53806%2f..",
            "..%2fNSE_FO|53806",
        ],
    )
    def test_traversal_query_and_fragment_keys_are_rejected(self, key):
        with pytest.raises(ValidationError):
            OptionCandleProbeIn(instrument_key=key)


    @pytest.mark.parametrize(
        "raw",
        [
            "NSE_FO|53806" + chr(0x85),
            chr(0x85) + "NSE_FO|53806",
            "NSE_FO|53806" + chr(0x9F),
            chr(0x2028) + "NSE_FO|53806",
            "NSE_FO|53806" + chr(0x2028),
            chr(0x2029) + "NSE_FO|53806",
            "NSE_FO|53806" + chr(0x2029),
            "NSE_FO|53806" + chr(0xA0),
            chr(0xA0) + "NSE_FO|53806",
            "NSE_FO|53806" + chr(0x3000),
            "NSE_FO|53" + chr(0xA0) + "806",
            "NSE_FO|53" + chr(0x2028) + "806",
        ],
    )
    def test_non_ascii_whitespace_and_c1_are_rejected_not_normalized(self, raw):
        """Only ASCII SPACE is normalizable; C1 and Unicode separators fail.

        ``str.strip()`` would have deleted U+0085, U+00A0, U+2028, U+2029 and
        U+3000 and turned a malformed key into a valid one.  Normalization is
        pinned to the single ASCII SPACE, and the control check now spans C1
        (U+0080-U+009F) as well as C0.
        """
        with pytest.raises(ValidationError):
            OptionCandleProbeIn(instrument_key=raw)

    @pytest.mark.parametrize("raw", ["  NSE_FO|53806  ", " NSE_FO|53806 "])
    def test_ordinary_ascii_space_padding_is_still_accepted(self, raw):
        """The one normalizable character remains normalizable."""
        assert OptionCandleProbeIn(instrument_key=raw).instrument_key == (
            TWO_SEGMENT_KEY
        )
    @pytest.mark.parametrize("raw", ["  NSE_FO|53806  ", " NSE_FO|53806 ", "NSE_FO|53806 "])
    def test_padded_key_is_normalized_and_reaches_the_probe(
        self, raw, client, admin_session, monkeypatch
    ):
        """The DOWNSTREAM call must receive the stripped key, not the raw one."""
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())

        resp = _post(client, sid, {"instrument_key": raw})

        assert resp.status_code == 200
        # The downstream probe call must carry the STRIPPED key, positionally.
        assert probe.call_args.args[1] == TWO_SEGMENT_KEY
        assert "instrument_key" not in probe.call_args.kwargs
        assert probe.call_args.kwargs["authoritative_expiry_date"] == TWO_SEGMENT_EXPIRY
