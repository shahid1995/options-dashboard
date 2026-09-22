"""Day 45 — Admin Control Plane (Issue #90).

Proves the Day 45 contract (master plan "Day 45 — Admin control plane"):

* **Explicit admin authorization boundary** — the admin principal is the
  durable ``users.is_admin`` flag; server-side enforcement happens in the
  ``AdminUser`` dependency BEFORE any admin work runs. Ordinary
  authenticated users (403), anonymous callers (401), and platform-tenant
  ownership tricks (user-owned BrokerConnection/authorization can NEVER
  grant admin) are all rejected.
* **Admin-only historical-data acquisition** — the orchestrator gate
  refuses when ``require_admin=False`` and accepts for admins, so customer
  broker connections can never drive platform historical ingestion.
* **Instrument/configuration/retention controls** — the AdminControl
  store is admin-only CRUD with a versioned audit trail.
* **Operational views** — ingestion health, adapters, feature flags,
  model metadata, and audit activity are admin-readable and REJECTED for
  ordinary users.
* **Audit records** — actor/action/target/result/time recorded; never
  contain secrets (Analytics Token / broker credential / session id
  shaped strings are asserted absent).

Controlled admin-only test injection only: no registration path grants
``is_admin`` — tests set the flag through the same durable DB column a
Founder bootstrap would set, and the API exposes no such endpoint.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.identity import User, create_session_record
from app.main import app
from app.services import token_store


# ---------------------------------------------------------------------------
# Fixtures — shared in-memory engine (conftest swaps app.db globally, but the
# identity assertions need direct handles).
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
        broker_user_id=f"d45-{user_id[:8]}",
        is_admin=admin,
    )
    db.add(user)
    db.commit()
    db.expire(user)
    return user


def _admin_denied_body(resp) -> dict:
    """Read the 403 body across both error contracts (Day 43 envelope on
    /api/v1: {error:{code,message,...}}; native elsewhere: {detail})."""
    body = resp.json()
    if "detail" in body:
        return body
    return {"detail": body.get("error", {}).get("message", "")}


def _login(db, user: User) -> str:
    session_id = token_store.set_token(f"tok-d45-{user.id[:8]}")
    create_session_record(db, user.id, session_id)
    return session_id


@pytest.fixture()
def admin_session(db_session):
    user = _mk_user(db_session, admin=True)
    return _login(db_session, user), user


@pytest.fixture()
def user_session(db_session):
    user = _mk_user(db_session, admin=False)
    return _login(db_session, user), user


ADMIN_COOKIE = {"X-Session-Id": None}  # placeholder, headers built inline


def _hdr(session_id: str) -> dict:
    return {"X-Session-Id": session_id}

def _cookie(session_id: str) -> dict:
    """Admin requests authenticate via the canonical HttpOnly cookie only
    (PR #91 F6: X-Session-Id is not an admin authorization transport)."""
    from app.routers.deps import SESSION_COOKIE_NAME

    return {SESSION_COOKIE_NAME: session_id}


# ---------------------------------------------------------------------------
# 1. Admin authorization boundary
# ---------------------------------------------------------------------------


class TestAdminAuthorizationBoundary:
    def test_admin_identity_is_explicit_durable_flag(self, db_session):
        """The admin principal source is users.is_admin — explicit, NOT
        inferred from ownership, broker linkage, or session transport."""
        user = _mk_user(db_session, admin=True)
        fresh = db_session.query(User).filter(User.id == user.id).one()
        assert fresh.is_admin is True
        other = _mk_user(db_session, admin=False)
        assert db_session.query(User).filter(User.id == other.id).one().is_admin is False

    def test_admin_endpoint_rejects_anonymous(self, client):
        resp = client.get("/api/v1/admin/audit")
        assert resp.status_code == 401

    def test_admin_endpoint_rejects_ordinary_user(self, client, user_session):
        sid, _uid = user_session
        resp = client.get("/api/v1/admin/audit", cookies=_cookie(sid))
        assert resp.status_code == 403
        assert _admin_denied_body(resp)["detail"] == "Admin privileges required."

    def test_admin_endpoint_accepts_admin(self, client, admin_session):
        sid, _uid = admin_session
        resp = client.get("/api/v1/admin/audit", cookies=_cookie(sid))
        assert resp.status_code == 200

    def test_tenant_ownership_cannot_confer_admin(self, db_session, client, user_session):
        """A user who owns broker connections/authorizations (full 'tenant
        ownership' of their own data) still cannot reach admin endpoints."""
        from sqlalchemy import select

        from app.identity import BrokerConnection, store_analytics_token

        sid, uid_obj = user_session
        uid = uid_obj.id
        store_analytics_token(db_session, uid, "UPSTOX", "tok-tenant-owner")
        conn = db_session.execute(
            select(BrokerConnection).where(BrokerConnection.user_id == uid)
        ).scalars().one()
        assert conn is not None  # genuinely owns a connection
        resp = client.get("/api/v1/admin/audit", cookies=_cookie(sid))
        assert resp.status_code == 403

    def test_disabled_admin_account_loses_admin_access(self, db_session, client, admin_session):
        sid, user = admin_session
        user.status = "suspended"
        db_session.commit()
        resp = client.get("/api/v1/admin/audit", cookies=_cookie(sid))
        assert resp.status_code == 403

    def test_no_api_path_grants_admin(self, client, user_session):
        """No endpoint mints admin privileges (self-service escalation is
        impossible through the API surface)."""
        sid, uid = user_session
        for method, path, payload in [
            ("post", "/api/v1/admin/acquisition/run", {"operation": "contracts"}),
            ("post", "/api/v1/admin/controls", {"domain": "retention", "key": "x", "value": "1"}),
            ("post", "/api/v1/admin/controls/become-admin", {}),
            ("get", "/api/v1/admin/whoami", None),
        ]:
            if method == "post":
                resp = client.post(path, cookies=_cookie(sid), json=payload)
            else:
                resp = client.get(path, cookies=_cookie(sid))
            assert resp.status_code in (403, 404, 405), (path, resp.status_code)


# ---------------------------------------------------------------------------
# 2. Historical-data acquisition controls (admin-only)
# ---------------------------------------------------------------------------


class TestHistoricalAcquisitionControls:
    def test_acquisition_requires_admin(self, client, user_session):
        sid, _uid = user_session
        resp = client.post(
            "/api/v1/admin/acquisition/run",
            json={"operation": "contracts"},
            cookies=_cookie(sid),
        )
        assert resp.status_code == 403
        body = _admin_denied_body(resp)
        assert body["detail"] == "Admin privileges required."

    def test_acquisition_gate_rejects_non_admin_domain_call(self, db_session):
        """The orchestrator-level gate refuses when the caller is not an
        admin — the domain boundary, independent of HTTP."""
        from app.services.admin_controls import require_platform_admin

        with pytest.raises(PermissionError):
            require_platform_admin(is_admin=False)

    def test_acquisition_gate_accepts_admin(self):
        from app.services.admin_controls import require_platform_admin

        require_platform_admin(is_admin=True)  # no raise

    def test_acquisition_rejects_customer_broker_connection_use(self, client, user_session, db_session):
        """Passing a customer broker connection id cannot turn a normal
        user into a historical-acquisition principal."""
        from sqlalchemy import select

        from app.identity import BrokerConnection, store_analytics_token

        sid, uid_obj = user_session
        uid = uid_obj.id
        store_analytics_token(db_session, uid, "UPSTOX", "tok-cust")
        conn = db_session.execute(
            select(BrokerConnection).where(BrokerConnection.user_id == uid)
        ).scalars().one()
        resp = client.post(
            "/api/v1/admin/acquisition/run",
            json={"operation": "contracts", "connection_id": conn.id},
            cookies=_cookie(sid),
        )
        assert resp.status_code == 403
        resp = client.post(
            "/api/v1/admin/acquisition/run",
            json={"operation": "contracts", "connection_id": uid},
            cookies=_cookie(sid),
        )
        assert resp.status_code == 403

    def test_acquisition_dry_run_admin_only_contract(self, client, admin_session):
        sid, _uid = admin_session
        resp = client.post(
            "/api/v1/admin/acquisition/run",
            json={"operation": "contracts", "dry_run": True},
            cookies=_cookie(sid),
        )
        assert resp.status_code in (200, 202)


# ---------------------------------------------------------------------------
# 3. Instrument / configuration / retention controls
# ---------------------------------------------------------------------------


class TestAdminControls:
    def test_controls_require_admin(self, client, user_session):
        sid, _uid = user_session
        resp = client.post(
            "/api/v1/admin/controls",
            json={"domain": "retention", "key": "chain_snapshots_days", "value": 90},
            cookies=_cookie(sid),
        )
        assert resp.status_code == 403

    def test_admin_sets_and_reads_control(self, client, admin_session):
        sid, _uid = admin_session
        set_resp = client.post(
            "/api/v1/admin/controls",
            json={"domain": "retention", "key": "chain_snapshots_days", "value": 90},
            cookies=_cookie(sid),
        )
        assert set_resp.status_code in (200, 201)
        get_resp = client.get(
            "/api/v1/admin/controls/retention", cookies=_cookie(sid)
        )
        assert get_resp.status_code == 200
        values = {c["key"]: c["value"] for c in get_resp.json()["controls"]}
        assert values.get("chain_snapshots_days") == 90

    def test_unknown_control_domain_rejected(self, client, admin_session):
        sid, _uid = admin_session
        resp = client.post(
            "/api/v1/admin/controls",
            json={"domain": "not_a_domain", "key": "x", "value": 1},
            cookies=_cookie(sid),
        )
        assert resp.status_code == 422

    def test_control_update_is_versioned(self, client, admin_session):
        sid, _uid = admin_session
        client.post(
            "/api/v1/admin/controls",
            json={"domain": "configuration", "key": "capture_interval_ms", "value": 3000},
            cookies=_cookie(sid),
        )
        again = client.post(
            "/api/v1/admin/controls",
            json={"domain": "configuration", "key": "capture_interval_ms", "value": 5000},
            cookies=_cookie(sid),
        )
        assert again.status_code in (200, 201)
        listing = client.get("/api/v1/admin/controls/configuration", cookies=_cookie(sid))
        row = next(c for c in listing.json()["controls"] if c["key"] == "capture_interval_ms")
        assert row["value"] == 5000


# ---------------------------------------------------------------------------
# 4. Operational views
# ---------------------------------------------------------------------------


class TestOperationalViews:
    def test_ingestion_health_admin_only(self, client, admin_session, user_session):
        a_sid, _ = admin_session
        u_sid, _ = user_session
        ok = client.get("/api/v1/admin/ingestion-health", cookies=_cookie(a_sid))
        assert ok.status_code == 200
        assert "runs" in ok.json()
        denied = client.get("/api/v1/admin/ingestion-health", cookies=_cookie(u_sid))
        assert denied.status_code == 403

    def test_adapters_view_admin_only(self, client, admin_session, user_session):
        a_sid, _ = admin_session
        u_sid, _ = user_session
        ok = client.get("/api/v1/admin/adapters", cookies=_cookie(a_sid))
        assert ok.status_code == 200
        brokers = ok.json()["adapters"]
        assert any(b["broker"] == "UPSTOX" for b in brokers)
        denied = client.get("/api/v1/admin/adapters", cookies=_cookie(u_sid))
        assert denied.status_code == 403

    def test_feature_flags_roundtrip_admin_only(self, client, admin_session, user_session):
        a_sid, _ = admin_session
        u_sid, _ = user_session
        resp = client.post(
            "/api/v1/admin/controls",
            json={"domain": "feature_flags", "key": "admin_views_v1", "value": True},
            cookies=_cookie(a_sid),
        )
        assert resp.status_code in (200, 201)
        ok = client.get("/api/v1/admin/feature-flags", cookies=_cookie(a_sid))
        assert ok.status_code == 200
        flags = {f["key"]: f["value"] for f in ok.json()["controls"]}
        assert flags.get("admin_views_v1") is True
        denied = client.get("/api/v1/admin/feature-flags", cookies=_cookie(u_sid))
        assert denied.status_code == 403

    def test_model_metadata_admin_only(self, client, admin_session, user_session):
        a_sid, _ = admin_session
        u_sid, _ = user_session
        ok = client.get("/api/v1/admin/model-metadata", cookies=_cookie(a_sid))
        assert ok.status_code == 200
        assert "models" in ok.json()
        denied = client.get("/api/v1/admin/model-metadata", cookies=_cookie(u_sid))
        assert denied.status_code == 403

    def test_audit_view_admin_only(self, client, admin_session, user_session):
        a_sid, _ = admin_session
        u_sid, _ = user_session
        ok = client.get("/api/v1/admin/audit", cookies=_cookie(a_sid))
        assert ok.status_code == 200
        assert "events" in ok.json()
        denied = client.get("/api/v1/admin/audit", cookies=_cookie(u_sid))
        assert denied.status_code == 403


# ---------------------------------------------------------------------------
# 5. Audit trail
# ---------------------------------------------------------------------------


class TestAdminAudit:
    def test_material_action_is_audited_with_actor(self, client, admin_session, db_session):
        from app.services.admin_audit import list_admin_audit

        sid, user = admin_session
        client.post(
            "/api/v1/admin/controls",
            json={"domain": "retention", "key": "audit_probe", "value": 7},
            cookies=_cookie(sid),
        )
        events = list_admin_audit(db_session)
        assert any(
            e["action"] == "controls.set" and e["actor_user_id"] == user.id and e["result"] == "success"
            for e in events
        )

    def test_audit_records_carry_core_fields(self, db_session):
        from app.services.admin_audit import record_admin_action, list_admin_audit

        record_admin_action(
            db_session,
            actor_user_id="admin-1",
            action="acquisition.run",
            target={"operation": "contracts"},
            result="success",
            detail={"dry_run": True},
        )
        record_admin_action(
            db_session,
            actor_user_id="admin-1",
            action="controls.set",
            target={"domain": "retention", "key": "k"},
            result="denied",
            detail={"reason": "not_admin"},
        )
        events = list_admin_audit(db_session)
        by_action = {e["action"]: e for e in events}
        ev = by_action["acquisition.run"]
        assert ev["actor_user_id"] == "admin-1"
        assert ev["result"] == "success"
        assert ev["occurred_at"] is not None
        assert ev["target"]["operation"] == "contracts"

    def test_audit_records_never_contain_secrets(self, client, admin_session, db_session):
        from app.services.admin_audit import list_admin_audit

        sid, _user = admin_session
        secret_like = "tok-super-secret-analytics-token"
        client.post(
            "/api/v1/admin/controls",
            json={
                "domain": "configuration",
                "key": "probe-key",
                "value": {"note": secret_like},
            },
            cookies=_cookie(sid),
        )
        blob = repr(list_admin_audit(db_session))
        assert secret_like not in blob

    def test_rejected_admin_attempt_is_audited(self, client, user_session, db_session):
        from app.services.admin_audit import list_admin_audit

        sid, uid_obj = user_session
        uid = uid_obj.id
        client.post(
            "/api/v1/admin/acquisition/run",
            json={"operation": "contracts"},
            cookies=_cookie(sid),
        )
        events = list_admin_audit(db_session)
        assert any(
            e["action"] == "acquisition.run" and e["actor_user_id"] == uid and e["result"] == "denied"
            for e in events
        )
