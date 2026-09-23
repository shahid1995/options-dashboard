"""PR #91 remediation regressions (Issue #90 review findings 1–11).

F1 — dry-run recognition: `{"operation": "dry_run"}` must be treated as a
     dry run even when `dry_run` is omitted; the platform-credential gate
     must accept BOTH forms and still 503 real acquisition without a
     platform credential.
F2 — atomicity: a control mutation and its audit record commit in ONE
     transaction; an audit-write failure rolls back the control write.
F3 — append-only history: >50 control updates must never silently drop the
     earliest recorded version.
F4 — domain backstop: the production acquisition path enforces admin
     authorization at the domain boundary, derived from the authenticated
     principal — not from HTTP routing or a hard-coded True.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

# Shared Day 45 fixtures (client/db_session/admin_session/user_session/_hdr)
# are defined in the Day 45 test module; reuse them here.
from tests.test_day45_admin import (  # noqa: F401
    _hdr,
    admin_session,
    client,
    db_session,
    engine,
    user_session,
)


# ---------------------------------------------------------------------------
# F1 — dry-run forms
# ---------------------------------------------------------------------------


class _NoTokenBridge:
    """TokenBridge double whose platform cache is empty."""

    def __init__(self, session_id=None):
        pass

    def get_token(self):
        return None


class _ReadyBridge:
    """TokenBridge double with a platform credential present."""

    def __init__(self, session_id=None):
        pass

    def get_token(self):
        return "tok-platform-credential"


def _patch_bridge(monkeypatch, bridge_cls):
    from app.services import backfill_orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "TokenBridge", bridge_cls)


class TestF1DryRunForms:
    def test_operation_dry_run_form_allows_missing_platform_credential(
        self, client, admin_session
    ):
        """`{"operation": "dry_run"}` with no platform credential → allowed
        (the gate must recognize the operation form, not just the flag)."""
        sid, _uid = admin_session
        resp = client.post(
            "/api/v1/admin/acquisition/run",
            json={"operation": "dry_run"},
            cookies=_cookie(sid),
        )
        assert resp.status_code in (200, 202)
        assert resp.json()["status"] == "DRY_RUN"

    def test_dry_run_boolean_form_still_allowed_without_credential(
        self, client, admin_session
    ):
        """The original `dry_run: true` form keeps working (no regression)."""
        sid, _uid = admin_session
        resp = client.post(
            "/api/v1/admin/acquisition/run",
            json={"operation": "contracts", "dry_run": True},
            cookies=_cookie(sid),
        )
        assert resp.status_code in (200, 202)
        assert resp.json()["status"] == "DRY_RUN"

    def test_real_acquisition_without_platform_credential_is_503(
        self, client, admin_session, monkeypatch
    ):
        """Real acquisition with NO platform credential → 503 (never a
        silent start, never a dry-run shortcut)."""
        _patch_bridge(monkeypatch, _NoTokenBridge)
        sid, _uid = admin_session
        resp = client.post(
            "/api/v1/admin/acquisition/run",
            json={"operation": "contracts"},
            cookies=_cookie(sid),
        )
        assert resp.status_code == 503

    def test_dry_run_with_operation_form_reports_dry_run_status(
        self, client, admin_session
    ):
        sid, _uid = admin_session
        resp = client.post(
            "/api/v1/admin/acquisition/run",
            json={"operation": "dry_run", "dry_run": False},
            cookies=_cookie(sid),
        )
        assert resp.status_code in (200, 202)
        assert resp.json()["status"] == "DRY_RUN"


# ---------------------------------------------------------------------------
# F2 — control mutation + audit atomicity
# ---------------------------------------------------------------------------


class TestF2AtomicControlAudit:
    def test_audit_failure_rolls_back_control_mutation(self, db_session, monkeypatch):
        """An audit-write failure must roll back the associated control
        mutation: neither becomes durable alone."""
        from app.identity import AdminAuditEvent
        from app.services import admin_controls

        original_add = db_session.add
        attempts = {"audit_adds": 0}

        def failing_add(obj):
            if isinstance(obj, AdminAuditEvent):
                attempts["audit_adds"] += 1
                raise RuntimeError("simulated audit-persistence failure")
            return original_add(obj)

        monkeypatch.setattr(db_session, "add", failing_add, raising=True)

        with pytest.raises(RuntimeError):
            admin_controls.set_control_and_audit(
                db_session,
                domain="retention",
                key="atomicity_probe",
                value=42,
                updated_by="admin-atomic",
                audit_action="controls.set",
            )
        monkeypatch.undo()
        assert attempts["audit_adds"] == 1
        row = (
            db_session.query(admin_controls.AdminControl)
            .filter(
                admin_controls.AdminControl.domain == "retention",
                admin_controls.AdminControl.key == "atomicity_probe",
            )
            .one_or_none()
        )
        assert row is None  # rolled back — never durable without its audit

    def test_control_set_api_persists_control_and_audit_together(
        self, client, admin_session, db_session
    ):
        from app.services import admin_controls
        from app.services.admin_audit import list_admin_audit

        sid, user = admin_session
        resp = client.post(
            "/api/v1/admin/controls",
            json={"domain": "retention", "key": "atomic_api_probe", "value": 7},
            cookies=_cookie(sid),
        )
        assert resp.status_code in (200, 201)
        assert resp.json()["control"]["version"] == 1
        row = (
            db_session.query(admin_controls.AdminControl)
            .filter(
                admin_controls.AdminControl.domain == "retention",
                admin_controls.AdminControl.key == "atomic_api_probe",
            )
            .one()
        )
        assert row.version == 1
        events = [
            e
            for e in list_admin_audit(db_session)
            if e["action"] == "controls.set"
            and e["target"].get("key") == "atomic_api_probe"
            and e["result"] == "success"
        ]
        assert len(events) == 1
        assert events[0]["actor_user_id"] == user.id


# ---------------------------------------------------------------------------
# F3 — append-only control history
# ---------------------------------------------------------------------------


class TestF3AppendOnlyHistory:
    def test_history_beyond_50_versions_keeps_earliest_entry(self, db_session):
        from app.services import admin_controls
        from app.services.admin_controls import ControlValueRejected, set_control_and_audit

        actor = "admin-history"
        # Secretish KEY shape (contains "session") carrying non-secret values:
        # under the pre-F9 implementation the audit sanitizer stripped every
        # value on this key into {}.  Authoritative storage must keep each
        # submitted value verbatim across the whole ledger (PR #91 F9/F10).
        for i in range(1, 61):  # 60 updates
            set_control_and_audit(
                db_session,
                domain="configuration",
                key="session_cache_limit",
                value=i,
                updated_by=actor,
                audit_action="controls.set",
            )
        row = (
            db_session.query(admin_controls.AdminControl)
            .filter(
                admin_controls.AdminControl.domain == "configuration",
                admin_controls.AdminControl.key == "session_cache_limit",
            )
            .one()
        )
        assert row.version == 60
        versions = [h["version"] for h in row.history]
        assert len(versions) == 60
        assert min(versions) == 1  # earliest recorded version still present
        assert versions == sorted(versions)
        assert versions[-1] == 60
        # Append-only ledger stays complete and authoritative (F9): every
        # historical entry equals the exact submitted integer — never an
        # audit-redacted placeholder.
        assert [h["value"] for h in row.history] == list(range(1, 61))
        assert row.value == {"v": 60}
        # A genuinely credential-shaped VALUE is rejected outright and appends
        # nothing to the ledger — secret material can never reach storage (F10:
        # the security assertion rides an actual secret-shaped input).
        secret = "tok-super-secret-analytics-value-7710"
        with pytest.raises(ControlValueRejected):
            set_control_and_audit(
                db_session,
                domain="configuration",
                key="session_cache_limit",
                value={"analytics_token": secret},
                updated_by=actor,
            )
        db_session.expire_all()
        row2 = (
            db_session.query(admin_controls.AdminControl)
            .filter(
                admin_controls.AdminControl.domain == "configuration",
                admin_controls.AdminControl.key == "session_cache_limit",
            )
            .one()
        )
        assert row2.version == 60
        assert len(row2.history) == 60  # the rejected write appended nothing
        # The refused secret appears NOWHERE in the authoritative store: not
        # in the current value, not in any of the 60 retained history entries.
        assert secret not in repr(row2.value)
        assert secret not in repr(row2.history)


# ---------------------------------------------------------------------------
# F4 — domain-level acquisition backstop on the production path
# ---------------------------------------------------------------------------


def _fake_request() -> SimpleNamespace:
    return SimpleNamespace(headers={}, cookies={})


def _cookie(session_id: str) -> dict:
    """Admin requests authenticate via the canonical HttpOnly cookie only
    (PR #91 F6: X-Session-Id is not an admin authorization transport)."""
    from app.routers.deps import SESSION_COOKIE_NAME

    return {SESSION_COOKIE_NAME: session_id}


class TestF4DomainBackstop:
    def test_production_acquisition_rejects_non_admin_despite_http_bypass(
        self, db_session, monkeypatch
    ):
        """Invoking the production handler directly (HTTP routing bypassed)
        with a NON-admin principal → 403 from the domain boundary."""
        from app.api.v1 import admin as admin_api

        _patch_bridge(monkeypatch, _ReadyBridge)
        non_admin = SimpleNamespace(user_id="non-admin-id", access_token=None)
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(
                admin_api.run_acquisition(
                    body=admin_api.AcquisitionRunIn(operation="dry_run"),
                    request=_fake_request(),
                    user=non_admin,
                    db=db_session,
                )
            )
        assert excinfo.value.status_code == 403

    def test_production_acquisition_allows_admin_despite_http_bypass(
        self, db_session, monkeypatch
    ):
        """The same direct invocation with a DURABLE ADMIN principal succeeds
        — the gate derives its decision from persisted admin state, not a
        hard-coded True."""
        from app.api.v1 import admin as admin_api
        from app.identity import User

        _patch_bridge(monkeypatch, _ReadyBridge)
        db_session.add(
            User(
                id="durable-admin-bypass",
                status="active",
                identity_source="upstox",
                broker_provider="UPSTOX",
                broker_user_id="d45-bypass-admin",
                is_admin=True,
            )
        )
        db_session.commit()
        admin = SimpleNamespace(user_id="durable-admin-bypass", access_token=None)
        result = asyncio.run(
            admin_api.run_acquisition(
                body=admin_api.AcquisitionRunIn(operation="dry_run"),
                request=_fake_request(),
                user=admin,
                db=db_session,
            )
        )
        # Dry-run responses carry status "DRY_RUN" (payload key wins over the
        # "accepted" envelope key — same shape the API tests assert).
        assert result["status"] in ("accepted", "DRY_RUN")

    def test_backstop_resolves_admin_state_from_durable_user_row(
        self, db_session, monkeypatch
    ):
        """The domain gate reads the durable users.is_admin flag for the
        principal's user_id — flipping the flag flips the decision."""
        from app.api.v1 import admin as admin_api
        from app.identity import User

        _patch_bridge(monkeypatch, _ReadyBridge)
        uid = "durable-admin-row"
        db_session.add(
            User(
                id=uid,
                status="active",
                identity_source="upstox",
                broker_provider="UPSTOX",
                broker_user_id="d45-backstop",
                is_admin=True,
            )
        )
        db_session.commit()
        principal = SimpleNamespace(user_id=uid, access_token=None)
        ok = asyncio.run(
            admin_api.run_acquisition(
                body=admin_api.AcquisitionRunIn(operation="dry_run"),
                request=_fake_request(),
                user=principal,
                db=db_session,
            )
        )
        assert ok["status"] in ("accepted", "DRY_RUN")
        row = db_session.query(User).filter(User.id == uid).one()
        row.is_admin = False
        db_session.commit()
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(
                admin_api.run_acquisition(
                    body=admin_api.AcquisitionRunIn(operation="dry_run"),
                    request=_fake_request(),
                    user=principal,
                    db=db_session,
                )
            )
        assert excinfo.value.status_code == 403


# ---------------------------------------------------------------------------
# F6 — privileged admin authorization accepts ONLY the canonical cookie
# transport (HttpOnly strikenova_session), never X-Session-Id.
# ---------------------------------------------------------------------------


class TestF6CookieOnlyAdminAuth:
    def test_admin_denies_header_only_session_id(self, client, admin_session):
        """X-Session-Id alone must NEVER authorize the admin control plane
        (browser-readable session credentials are prohibited for the
        privileged boundary)."""
        sid, _uid = admin_session
        resp = client.get("/api/v1/admin/audit", headers={"X-Session-Id": sid})
        assert resp.status_code == 401

    def test_admin_accepts_canonical_cookie(self, client, admin_session):
        """The HttpOnly strikenova_session cookie remains the working admin
        transport."""
        from app.routers.deps import SESSION_COOKIE_NAME

        sid, _uid = admin_session
        resp = client.get(
            "/api/v1/admin/audit", cookies={SESSION_COOKIE_NAME: sid}
        )
        assert resp.status_code == 200
        assert "events" in resp.json()

    def test_admin_cookie_authoritative_over_bogus_header(
        self, client, admin_session
    ):
        """With both transports present, only the cookie may authorize: a
        bogus header cannot smuggle a different (or any) session in."""
        from app.routers.deps import SESSION_COOKIE_NAME

        sid, _uid = admin_session
        resp = client.get(
            "/api/v1/admin/audit",
            headers={"X-Session-Id": "forged-or-stale-header-sid"},
            cookies={SESSION_COOKIE_NAME: sid},
        )
        assert resp.status_code == 200

    def test_sensitive_admin_action_rejects_header_only_without_mutation(
        self, client, admin_session, db_session
    ):
        """Header-only attempts at a sensitive mutation are refused AND
        leave no control mutation behind."""
        from app.routers.deps import SESSION_COOKIE_NAME
        from app.services import admin_controls

        sid, _uid = admin_session
        resp = client.post(
            "/api/v1/admin/controls",
            json={"domain": "retention", "key": "f6_probe", "value": 1},
            headers={"X-Session-Id": sid},
        )
        assert resp.status_code == 401
        row = (
            db_session.query(admin_controls.AdminControl)
            .filter(
                admin_controls.AdminControl.domain == "retention",
                admin_controls.AdminControl.key == "f6_probe",
            )
            .one_or_none()
        )
        assert row is None
        # The canonical cookie transport succeeds for the same mutation.
        ok = client.post(
            "/api/v1/admin/controls",
            json={"domain": "retention", "key": "f6_probe", "value": 1},
            cookies={SESSION_COOKIE_NAME: sid},
        )
        assert ok.status_code in (200, 201)

    def test_header_only_acquisition_attempt_is_audited_denied(
        self, client, admin_session, db_session
    ):
        """The sensitive-action rejection audit still fires for header-only
        attempts (recorded as an anonymous denial — no actor identity is
        derived from the prohibited transport)."""
        from app.services.admin_audit import list_admin_audit

        sid, _uid = admin_session
        resp = client.post(
            "/api/v1/admin/acquisition/run",
            json={"operation": "dry_run"},
            headers={"X-Session-Id": sid},
        )
        assert resp.status_code == 401
        events = list_admin_audit(db_session)
        assert any(
            e["action"] == "acquisition.run" and e["result"] == "denied"
            for e in events
        )

    def test_non_admin_paths_keep_header_compat(self, client, admin_session):
        """Outside the admin boundary the intentional legacy/test header
        compatibility remains unchanged (non-admin auth behavior preserved)."""
        sid, _uid = admin_session
        resp = client.get("/auth/status", headers={"X-Session-Id": sid})
        assert resp.status_code == 200
        # Prove real authentication, not a 200-for-anonymous response
        # (PR #91 F11): the legacy header path must actually resolve the
        # session for non-admin routes.
        assert resp.json()["logged_in"] is True


# ---------------------------------------------------------------------------
# F9 — control values are validated, not audit-sanitized, before persistence
# ---------------------------------------------------------------------------


class TestF9ControlValueIntegrity:
    """PR #91 F9: authoritative control values must survive persistence
    exactly.  Audit redaction must never mutate stored configuration, and
    credential-bearing values must be REJECTED (422) — never transformed
    into different stored values.
    """

    def test_legitimate_session_config_persists_exactly(self, db_session):
        from app.services.admin_controls import get_control_value, set_control_and_audit

        value = {"session_timeout": 30}
        set_control_and_audit(
            db_session,
            domain="configuration",
            key="api_session",
            value=value,
            updated_by="admin-f9",
        )
        stored = get_control_value(db_session, "configuration", "api_session")
        assert stored == value

    def test_legitimate_session_config_persists_exactly_via_api(self, client, admin_session):
        sid, _user = admin_session
        resp = client.post(
            "/api/v1/admin/controls",
            json={
                "domain": "configuration",
                "key": "ui_session",
                "value": {"session_timeout": 30},
            },
            cookies=_cookie(sid),
        )
        assert resp.status_code == 200
        assert resp.json()["control"]["value"] == {"session_timeout": 30}
        read = client.get("/api/v1/admin/controls/configuration", cookies=_cookie(sid))
        stored = {c["key"]: c["value"] for c in read.json()["controls"]}["ui_session"]
        assert stored == {"session_timeout": 30}

    def test_history_preserves_authoritative_non_secret_value(self, db_session):
        from app.services import admin_controls
        from app.services.admin_controls import set_control_and_audit

        set_control_and_audit(
            db_session,
            domain="configuration",
            key="auth_session",
            value={"session_timeout": 45},
            updated_by="admin-f9",
        )
        row = (
            db_session.query(admin_controls.AdminControl)
            .filter(
                admin_controls.AdminControl.domain == "configuration",
                admin_controls.AdminControl.key == "auth_session",
            )
            .one()
        )
        assert row.history[-1]["value"] == {"session_timeout": 45}

    def test_credential_bearing_control_is_rejected_with_422(self, client, admin_session, db_session):
        from app.services import admin_controls

        sid, _user = admin_session
        resp = client.post(
            "/api/v1/admin/controls",
            json={
                "domain": "configuration",
                "key": "probe",
                "value": {"analytics_token": "tok-super-secret-abc123456"},
            },
            cookies=_cookie(sid),
        )
        assert resp.status_code == 422
        body = resp.json()
        code = body.get("code") or (body.get("error") or {}).get("code")
        assert code == "CONTROL_VALUE_REJECTED"
        rows = (
            db_session.query(admin_controls.AdminControl)
            .filter(
                admin_controls.AdminControl.domain == "configuration",
                admin_controls.AdminControl.key == "probe",
            )
            .all()
        )
        assert rows == []  # no durable control value, no durable history

    def test_rejected_secret_never_reaches_authoritative_storage(self, client, admin_session, db_session):
        """Rejected credential input proves all three boundaries: the request
        is REJECTED (422), the secret is NOT RETURNED, and NO durable control
        row/history exists. Persistence is proven by direct row queries — the
        ORM's default ``repr()`` carries no stored key/value, so it can never
        serve as a persistence assertion."""
        from app.services import admin_controls

        sid, _user = admin_session
        secret = "tok-super-secret-analytics-token-9999"
        resp = client.post(
            "/api/v1/admin/controls",
            json={
                "domain": "retention",
                "key": "probe2",
                "value": {"access_token": secret},
            },
            cookies=_cookie(sid),
        )
        assert resp.status_code == 422
        body = resp.json()
        code = body.get("code") or (body.get("error") or {}).get("code")
        assert code == "CONTROL_VALUE_REJECTED"
        assert secret not in resp.text  # secret never leaks in the response
        rows = (
            db_session.query(admin_controls.AdminControl)
            .filter(
                admin_controls.AdminControl.domain == "retention",
                admin_controls.AdminControl.key == "probe2",
            )
            .all()
        )
        assert rows == []  # no durable control value, no durable history

    def test_nested_secret_key_rejected_service_level(self, db_session):
        from app.services import admin_controls
        from app.services.admin_controls import ControlValueRejected, set_control_and_audit

        with pytest.raises(ControlValueRejected):
            set_control_and_audit(
                db_session,
                domain="retention",
                key="nested",
                value={"retry": {"password": "hunter2-secret-value"}},
                updated_by="admin-f9",
            )
        assert (
            db_session.query(admin_controls.AdminControl)
            .filter(
                admin_controls.AdminControl.domain == "retention",
                admin_controls.AdminControl.key == "nested",
            )
            .one_or_none()
            is None
        )

    def test_flat_secret_string_under_innocent_key_is_rejected(self, db_session):
        """A credential-shaped VALUE under an innocent KEY is refused (F9 D).

        Key-name detection alone is not enough: a raw bearer/token string
        stashed under an innocuous key must also be rejected outright, never
        persisted under a different value and never accepted silently.
        """
        from app.services import admin_controls
        from app.services.admin_controls import ControlValueRejected, set_control_and_audit

        secret = "tok-super-secret-analytics-value-7710"
        with pytest.raises(ControlValueRejected):
            set_control_and_audit(
                db_session,
                domain="configuration",
                key="plain_note",
                value=secret,
                updated_by="admin-f9",
            )
        # No durable control value, no durable history entry.
        assert (
            db_session.query(admin_controls.AdminControl)
            .filter(
                admin_controls.AdminControl.domain == "configuration",
                admin_controls.AdminControl.key == "plain_note",
            )
            .one_or_none()
            is None
        )

    def test_plain_values_remain_accepted(self, client, admin_session):
        sid, _user = admin_session
        for v in [90, 3000, True, "strict", None, {"max_rows": 5000, "batch": [1, 2, 3]}]:
            resp = client.post(
                "/api/v1/admin/controls",
                json={"domain": "retention", "key": "plain", "value": v},
                cookies=_cookie(sid),
            )
            assert resp.status_code == 200, f"legitimate value {v!r} was rejected"


# ---------------------------------------------------------------------------
# F12 — compound credential KEYS cannot bypass validation via _/- segmentation
# ---------------------------------------------------------------------------


class TestF12CredentialKeyBypass:
    """PR #91 F12: under the old segmentation ``_``/``-`` counted as word
    characters, so compound credential keys such as ``analytics_token`` were
    a SINGLE segment and the credential-tail check missed them entirely.
    Every case below uses a deliberately SHORT value ("abc", "xyz", "pwd",
    "key") so rejection proves KEY-based detection — not the value-shape
    heuristic the previous tests accidentally rode on."""

    def _row(self, db, domain, key):
        from app.identity import AdminControl

        return (
            db.query(AdminControl)
            .filter(AdminControl.domain == domain, AdminControl.key == key)
            .one_or_none()
        )

    @pytest.mark.parametrize(
        ("payload", "label"),
        [
            ({"analytics_token": "abc"}, "analytics_token"),
            ({"access_token": "xyz"}, "access_token"),
            ({"admin_password": "pwd"}, "admin_password"),
            ({"access-token": "key"}, "access-token"),
        ],
    )
    def test_short_credential_keys_are_rejected_service_level(
        self, db_session, payload, label
    ):
        from app.services.admin_controls import ControlValueRejected, set_control_and_audit

        control_key = f"f12-{label}"
        # Pre-existing valid control state on a DIFFERENT key must remain
        # completely untouched by the rejection.
        set_control_and_audit(
            db_session,
            domain="configuration",
            key="f12_keep",
            value={"session_timeout": 30},
            updated_by="admin-f12",
            audit_action="controls.set",
        )
        keep_before = self._row(db_session, "configuration", "f12_keep")
        version_before = keep_before.version
        history_before = list(keep_before.history)

        with pytest.raises(ControlValueRejected):
            set_control_and_audit(
                db_session,
                domain="configuration",
                key=control_key,
                value=payload,
                updated_by="admin-f12",
                audit_action="controls.set",
            )

        # The rejected credential-bearing value reaches NO durable storage:
        # no new AdminControl row/version, no history entry, and the valid
        # control state + its history are unchanged.
        assert self._row(db_session, "configuration", control_key) is None
        db_session.expire_all()
        assert self._row(db_session, "configuration", control_key) is None
        keep_after = self._row(db_session, "configuration", "f12_keep")
        assert keep_after.version == version_before
        assert list(keep_after.history) == history_before
        assert label not in repr(keep_after.history)

    def test_short_credential_key_rejected_via_api(self, client, admin_session, db_session):
        from app.services import admin_controls

        sid, _user = admin_session
        resp = client.post(
            "/api/v1/admin/controls",
            json={
                "domain": "configuration",
                "key": "f12-api-probe",
                "value": {"analytics_token": "abc"},
            },
            cookies=_cookie(sid),
        )
        assert resp.status_code == 422
        body = resp.json()
        code = body.get("code") or (body.get("error") or {}).get("code")
        assert code == "CONTROL_VALUE_REJECTED"  # Day 43 envelope code
        rows = (
            db_session.query(admin_controls.AdminControl)
            .filter(
                admin_controls.AdminControl.domain == "configuration",
                admin_controls.AdminControl.key == "f12-api-probe",
            )
            .all()
        )
        assert rows == []  # no durable control record, no durable history

    def test_legitimate_compound_payload_keys_remain_accepted(self, db_session):
        """F12 acceptance boundary: compound NON-credential keys remain valid
        INSIDE the submitted payload — the boundary `_validate_control_value`
        actually inspects (the top-level control key is neutral by design).
        """
        from app.services.admin_controls import get_control_value, set_control_and_audit

        for key, value in [
            ("session_timeout", 30),
            ("session_cache_limit", 500),
            ("cache_key_size", 256),
        ]:
            control_key = f"f12-valid-{key}"
            set_control_and_audit(
                db_session,
                domain="configuration",
                key=control_key,
                value={key: value},
                updated_by="admin-f12",
                audit_action="controls.set",
            )
            assert get_control_value(
                db_session,
                "configuration",
                control_key,
            ) == {key: value}
