"""Day 43 — Versioned API boundary (Issue #86).

Proves the Day 43 blueprint contract (master plan "Day 43 — Versioned
API boundary"; design spec §28) on the SCOPED boundary — the market-data
chain domain, the one route family whose contract still passed the raw
broker-adapter dict straight through:

* **API version routing** — ``/api/v1/chains/*`` serves the chain
  contract; ``/api/v1`` is the single version prefix (no competing
  schemes); unknown versions 404; unversioned chains routes are retained
  as a compatibility alias so existing consumers keep working.
* **Domain schema boundary** — responses are validated by explicit
  domain schemas (no raw adapter passthrough); field-level missing data
  stays ``None`` and never becomes a fabricated value.
* **Error envelope** — every error on the versioned surface carries the
  canonical envelope: ``error.code`` (stable, machine-readable),
  ``error.message`` (human diagnostics), ``error.status`` (HTTP status).
* **Authorization** — unauthenticated 401 ``UNAUTHENTICATED``; valid
  session without market-data authorization 403 ``MARKET_DATA_NOT_CONNECTED``;
  tenant/user isolation preserved (one user's session never mints
  another user's credential).

Public API behavior only — no implementation-detail assertions.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.chains import INSTRUMENT_KEYS
from app.services import token_store

# The single canonical version prefix (design spec §28). Imported so a
# future rename breaks THIS test loudly rather than drifting silently.
from app.api.v1 import API_VERSION_PREFIX


@pytest.fixture
def client():
    # raise_server_exceptions=False: boundary tests assert the RESPONSE
    # envelope (500 + error.code), which ServerErrorMiddleware sends
    # before re-raising to the test client.
    return TestClient(app, raise_server_exceptions=False)


def _all_route_paths():
    """Flatten the app route table, including FastAPI 0.141's lazy
    ``_IncludedRouter`` entries (public behavior: which paths exist)."""
    paths = set()
    for r in app.routes:
        if type(r).__name__ == "_IncludedRouter":
            prefix = getattr(r.include_context, "prefix", "") or ""
            for rr in r.original_router.routes:
                p = getattr(rr, "path", None)
                if p is not None:
                    paths.add(prefix + p)
        else:
            p = getattr(r, "path", None)
            if p is not None:
                paths.add(p)
    return paths


def _seed_platform_identity(db, *, identity_source="email"):
    """Create a platform-only user + session; return session_id."""
    import secrets
    from uuid import uuid4

    from app.db import Base, SessionLocal
    from app.identity import User, create_session_record

    Base.metadata.create_all(bind=SessionLocal().get_bind())
    d = SessionLocal()
    try:
        user_id = str(uuid4())
        d.add(User(
            id=user_id, status="active",
            identity_source=identity_source, broker_provider=None,
        ))
        d.flush()
        session_id = secrets.token_urlsafe(32)
        create_session_record(d, user_id, session_id)
        d.commit()
        return session_id
    finally:
        d.close()


@pytest.fixture
def platform_session(client):
    return _seed_platform_identity(client)


# ===========================================================================
# 1. API version routing
# ===========================================================================


class TestApiVersionRouting:
    def test_api_v1_chains_expiries_serves_the_contract(self, client, monkeypatch):
        from app.services import upstox

        monkeypatch.setattr(upstox, "get_option_contracts", AsyncMock(return_value={
            "data": [{"expiry": "2026-08-28"}, {"expiry": "2026-09-24"}],
        }))
        session_id = token_store.set_token("tok-xyz")
        client.cookies.set("strikenova_session", session_id)

        resp = client.get("/api/v1/chains/NIFTY/expiries")
        assert resp.status_code == 200
        body = resp.json()
        assert body["symbol"] == "NIFTY"
        assert body["expiries"] == ["2026-08-28", "2026-09-24"]

    def test_version_prefix_is_the_single_canonical_constant(self):
        """§28: one versioning scheme. The constant is '/api/v1' and the
        versioned router is mounted under it (public route table)."""
        assert API_VERSION_PREFIX == "/api/v1"
        paths = _all_route_paths()
        assert "/api/v1/chains/{symbol}/expiries" in paths
        assert "/api/v1/chains/{symbol}" in paths

    def test_unversioned_chains_routes_remain_compatible(self, client):
        """Compatibility: existing consumers on unversioned paths keep
        working (same handlers, same behavior)."""
        # 401 (not 404): the route EXISTS and demands authentication —
        # proof the unversioned family is still routed.
        resp = client.get("/chains/NIFTY/expiries")
        assert resp.status_code == 401

    def test_unknown_api_version_is_not_routed(self, client):
        resp = client.get("/api/v9/chains/NIFTY/expiries")
        assert resp.status_code == 404

    def test_versioned_surface_has_no_competing_scheme(self):
        """No /v2, /api/v2, or other version mounts exist alongside v1."""
        paths = _all_route_paths()
        competing = [p for p in paths if p.startswith(("/api/v2", "/v2", "/api/v9"))]
        assert competing == []


# ===========================================================================
# 2. Domain schema boundary
# ===========================================================================


class TestDomainSchemaBoundary:
    def test_chain_response_is_schema_validated(self, client, monkeypatch):
        from app.services import upstox

        session_id = token_store.set_token("tok-xyz")
        client.cookies.set("strikenova_session", session_id)
        # Mock the RAW Upstox payload (the adapter transforms it) — exactly
        # the shape the real broker returns.
        monkeypatch.setattr(upstox, "get_option_chain", AsyncMock(return_value={
            "data": [{
                "strike_price": 24500.0,
                "underlying_spot_price": 25010.5,
                "call_options": {
                    "market_data": {"ltp": 175.0, "oi": 100, "prev_oi": 95,
                                    "volume": 10},
                    "option_greeks": {"iv": 12.0, "delta": 0.5},
                },
                "put_options": {"market_data": {}, "option_greeks": {}},
            }],
        }))

        resp = client.get(
            "/api/v1/chains/NIFTY", params={"expiry_date": "2026-08-28"},
        )
        assert resp.status_code == 200
        body = resp.json()
        # Explicit domain contract — every canonical field present.
        assert body["symbol"] == "NIFTY"
        assert body["expiry_date"] == "2026-08-28"
        assert body["underlying_spot_price"] == 25010.5
        row = body["chain"][0]
        assert row["strike"] == 24500.0
        assert row["call"]["ltp"] == 175.0
        assert row["call"]["delta"] == 0.5
        assert row["call"]["gamma"] is None  # missing stays missing

    def test_chain_response_rejects_nonconforming_payloads(self, client, monkeypatch):
        """A broker payload that violates the domain contract must fail
        loudly (500), never silently reshape the public contract."""
        from app.services import upstox

        session_id = token_store.set_token("tok-xyz")
        client.cookies.set("strikenova_session", session_id)
        monkeypatch.setattr(upstox, "get_option_chain", AsyncMock(return_value={
            "data": [{
                "strike_price": "not-a-number",
                "underlying_spot_price": 25010.5,
                "call_options": {},
                "put_options": {},
            }],
        }))

        resp = client.get(
            "/api/v1/chains/NIFTY", params={"expiry_date": "2026-08-28"},
        )
        assert resp.status_code == 500
        # And the failure itself is envelope-shaped.
        assert resp.json()["error"]["code"] == "INTERNAL_ERROR"

    def test_expiries_response_shape_is_stable(self, client, monkeypatch):
        from app.services import upstox

        session_id = token_store.set_token("tok-xyz")
        client.cookies.set("strikenova_session", session_id)
        monkeypatch.setattr(upstox, "get_option_contracts", AsyncMock(return_value={"data": []}))

        resp = client.get("/api/v1/chains/BANKNIFTY/expiries")
        assert resp.status_code == 200
        assert set(resp.json().keys()) == {"symbol", "expiries"}


# ===========================================================================
# 3. Error envelope
# ===========================================================================


class TestErrorEnvelope:
    def test_envelope_shape_on_validation_error(self, client):
        resp = client.get(
            "/api/v1/chains/NIFTY", params={"expiry_date": "not-a-date"},
        )
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["status"] == 422
        assert err["code"] == "VALIDATION_ERROR"
        assert isinstance(err["message"], str) and err["message"]
        # Validation diagnostics are preserved but capped.
        assert len(err.get("details", [])) <= 10

    def test_envelope_shape_on_unknown_symbol(self, client, platform_session):
        client.cookies.set("strikenova_session", platform_session)
        resp = client.get("/api/v1/chains/UNKNOWN/expiries")
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["status"] == 404
        assert err["code"] == "NOT_FOUND"

    def test_envelope_on_unauthenticated(self, client):
        resp = client.get("/api/v1/chains/NIFTY/expiries")
        assert resp.status_code == 401
        err = resp.json()["error"]
        assert err["code"] == "UNAUTHENTICATED"
        assert "token" not in resp.text.lower()

    def test_envelope_on_missing_market_data_authorization(self, client, platform_session):
        client.cookies.set("strikenova_session", platform_session)
        resp = client.get("/api/v1/chains/NIFTY/expiries")
        assert resp.status_code == 403
        err = resp.json()["error"]
        assert err["code"] == "MARKET_DATA_NOT_CONNECTED"

    def test_envelope_on_upstream_broker_error(self, client, monkeypatch):
        from app.services import upstox
        from app.services.upstox import UpstoxError

        session_id = token_store.set_token("tok-xyz")
        client.cookies.set("strikenova_session", session_id)
        monkeypatch.setattr(
            upstox, "get_option_contracts",
            AsyncMock(side_effect=UpstoxError(500, "upstream blew up")),
        )

        resp = client.get("/api/v1/chains/NIFTY/expiries")
        assert resp.status_code == 502
        err = resp.json()["error"]
        assert err["code"] == "UPSTREAM_ERROR"
        assert "upstream blew up" in err["message"]

    def test_internal_errors_do_not_leak_internals(self, client, monkeypatch):
        from app.api.v1 import chains as v1_chains

        session_id = token_store.set_token("tok-xyz")
        client.cookies.set("strikenova_session", session_id)

        monkeypatch.setattr(v1_chains, "gateway", type("G", (), {
            "create": staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("SECRET-DB-DNS-xyz"))),
        })())
        resp = client.get("/api/v1/chains/NIFTY/expiries")
        assert resp.status_code == 500
        text = resp.text
        assert "SECRET-DB-DNS-xyz" not in text
        assert resp.json()["error"]["code"] == "INTERNAL_ERROR"


# ===========================================================================
# 4. Authorization boundary
# ===========================================================================


class TestAuthorizationBoundary:
    def test_unauthenticated_is_rejected_before_domain_work(self, client, monkeypatch):
        from app.services import upstox

        mock = AsyncMock(return_value={"data": []})
        monkeypatch.setattr(upstox, "get_option_contracts", mock)
        resp = client.get("/api/v1/chains/NIFTY/expiries")
        assert resp.status_code == 401
        mock.assert_not_awaited()  # no broker call without authorization

    def test_authenticated_without_market_data_authorization_is_403(self, client, platform_session, monkeypatch):
        from app.services import upstox

        mock = AsyncMock(return_value={"data": []})
        monkeypatch.setattr(upstox, "get_option_contracts", mock)
        client.cookies.set("strikenova_session", platform_session)
        resp = client.get("/api/v1/chains/NIFTY/expiries")
        assert resp.status_code == 403
        mock.assert_not_awaited()

    def test_tenant_isolation_session_cannot_mint_foreign_credential(
        self, client, monkeypatch,
    ):
        """Each session resolves only its OWN market-data credential: a
        session's calls run under that session's token, and a user with
        no credential cannot borrow another identity's."""
        from app.services import upstox

        session_a = token_store.set_token("tok-xyz")  # A's own credential
        _seed_platform_identity(client)  # user B exists with NO credential
        mock = AsyncMock(return_value={"data": []})
        monkeypatch.setattr(upstox, "get_option_contracts", mock)
        client.cookies.set("strikenova_session", session_a)

        resp = client.get("/api/v1/chains/NIFTY/expiries")
        assert resp.status_code == 200
        # The credential forwarded to the broker was THIS session's own.
        mock.assert_awaited_once_with("tok-xyz", INSTRUMENT_KEYS["NIFTY"])

    def test_platform_session_token_never_used_as_broker_credential(
        self, client, monkeypatch,
    ):
        """A platform session identifier must never be forwarded to a
        broker adapter as a market-data credential (isolation boundary)."""
        from app.services import upstox

        session_id = token_store.set_token("email:someone@example.com")
        mock = AsyncMock(return_value={"data": []})
        monkeypatch.setattr(upstox, "get_option_contracts", mock)
        client.cookies.set("strikenova_session", session_id)

        resp = client.get("/api/v1/chains/NIFTY/expiries")
        assert resp.status_code == 401
        mock.assert_not_awaited()


# ===========================================================================
# 5. Backward compatibility
# ===========================================================================


class TestBackwardCompatibility:
    @pytest.mark.parametrize("symbol", sorted(INSTRUMENT_KEYS)[:2])
    def test_unversioned_expiries_unchanged(self, client, monkeypatch, symbol):
        from app.services import upstox

        session_id = token_store.set_token("tok-xyz")
        client.cookies.set("strikenova_session", session_id)
        monkeypatch.setattr(upstox, "get_option_contracts", AsyncMock(return_value={"data": []}))
        resp = client.get(f"/chains/{symbol}/expiries")
        assert resp.status_code == 200
        assert resp.json() == {"symbol": symbol, "expiries": []}

    def test_unversioned_error_contract_unchanged(self, client):
        """Unversioned routes keep FastAPI's native error shape."""
        resp = client.get("/chains/NIFTY", params={"expiry_date": "nope"})
        assert resp.status_code == 422
        assert "detail" in resp.json()
        assert "error" not in resp.json()
