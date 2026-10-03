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
PROBE_TOKEN = "tok-day50-seam-live-secret"

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
    session_id = token_store.set_token(f"tok-d50-{user.id[:8]}")
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
            "current_ist_date": "2026-10-03",
            "probe_date_ist": CANDLE_DATE,
            "intraday_returned_current_session_candle": current_session,
            "verified_unexpired": verified_unexpired,
            "verified_reason": verified_reason,
            "method": "instrument-key expiry parse vs current IST date",
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
                "token": "attacker-supplied-token",
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
        probe.assert_awaited_once_with(PROBE_TOKEN, LIVE_KEY, CANDLE_DATE)

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
        probe.assert_awaited_once_with(PROBE_TOKEN, LIVE_KEY, CANDLE_DATE)

    def test_omitted_or_blank_candle_date_passes_none(
        self, client, admin_session, monkeypatch
    ):
        sid, _admin = admin_session
        _resolver, probe = _install(monkeypatch, _credential())
        assert _post(client, sid, {"instrument_key": LIVE_KEY}).status_code == 200
        probe.assert_awaited_once_with(PROBE_TOKEN, LIVE_KEY, None)
        probe.reset_mock()
        assert (
            _post(client, sid, {"instrument_key": LIVE_KEY, "candle_date": "  "}).status_code
            == 200
        )
        probe.assert_awaited_once_with(PROBE_TOKEN, LIVE_KEY, None)

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
        secret = "tok-internal-secret-must-not-leak"
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
