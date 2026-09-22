"""PR #91 remediation regressions (Issue #90 review findings 1–4).

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
            headers=_hdr(sid),
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
            headers=_hdr(sid),
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
            headers=_hdr(sid),
        )
        assert resp.status_code == 503

    def test_dry_run_with_operation_form_reports_dry_run_status(
        self, client, admin_session
    ):
        sid, _uid = admin_session
        resp = client.post(
            "/api/v1/admin/acquisition/run",
            json={"operation": "dry_run", "dry_run": False},
            headers=_hdr(sid),
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
            headers=_hdr(sid),
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
        from app.services.admin_controls import set_control_and_audit

        actor = "admin-history"
        for i in range(1, 61):  # 60 updates
            set_control_and_audit(
                db_session,
                domain="configuration",
                key="history_probe",
                value=i,
                updated_by=actor,
                audit_action="controls.set",
            )
        row = (
            db_session.query(admin_controls.AdminControl)
            .filter(
                admin_controls.AdminControl.domain == "configuration",
                admin_controls.AdminControl.key == "history_probe",
            )
            .one()
        )
        assert row.version == 60
        versions = [h["version"] for h in row.history]
        assert len(versions) == 60
        assert min(versions) == 1  # earliest recorded version still present
        assert versions == sorted(versions)
        assert versions[-1] == 60
        # Historical values remain sanitized — no secret-shaped material.
        assert "tok-secret-analytics-value" not in repr(row.history)


# ---------------------------------------------------------------------------
# F4 — domain-level acquisition backstop on the production path
# ---------------------------------------------------------------------------


def _fake_request() -> SimpleNamespace:
    return SimpleNamespace(headers={}, cookies={})


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
