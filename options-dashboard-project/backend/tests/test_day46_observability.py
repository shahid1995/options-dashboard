"""Day 46 — Notifications, Observability and SaaS Gate (Issue #92).

Verifies the Day 46 blueprint contract on the SCOPED boundary:

* **Correlation IDs** — generated at the request boundary when absent,
  echoed via ``X-Correlation-Id``, stable per request, propagated into
  logs/operational events, never authorization material.
* **Structured secret-safe logging** — the JSON request-log formatter
  carries timestamp/level/logger/correlation ID/route/method/status/
  duration/user/tenant and REDACTS credential-shaped values; session
  IDs never enter log records even under malicious request headers.
* **Notification events** — backend-authoritative contract (event ID,
  type, tenant/user scope, severity, occurred-at, source, summary,
  details, correlation ID, dedup key); tenant-isolated reads; no admin
  self-grant; sanitized payloads; one deduplication identity per
  (scope, dedup key, unresolved window).
* **Operational alerts** — deterministic conditions for market-data
  staleness, broker adapter/auth failure, execution failure, ingestion
  job failure, and readiness degradation; each with source, timestamp,
  severity, reason, and dedup identity; no opaque aggregate score.
* **Health/readiness** — ``/health`` liveness stays cheap; ``/readiness``
  preserves its existing contract shape (``status``/``checks`` with
  ``database``/``token_store`` keys and 503-on-degraded) while remaining
  secret-free.
"""

from __future__ import annotations

import json
import logging
import uuid
from uuid import uuid4

import pytest

from tests.test_day45_admin import client, db_session, engine  # noqa: F401 — app-level fixtures

# ---------------------------------------------------------------------------
# 1. Correlation ID generation / propagation / non-authorization
# ---------------------------------------------------------------------------


class TestCorrelationIds:
    def test_generated_when_absent_and_echoed(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        cid = resp.headers.get("X-Correlation-Id")
        assert cid and len(cid) >= 16

    def test_incoming_header_is_stable_and_echoed(self, client):
        supplied = "corr-test-1234567890abcdef"
        resp = client.get("/health", headers={"X-Correlation-Id": supplied})
        assert resp.headers.get("X-Correlation-Id") == supplied

    def test_two_requests_get_different_ids(self, client):
        r1 = client.get("/health")
        r2 = client.get("/health")
        assert r1.headers.get("X-Correlation-Id") != r2.headers.get("X-Correlation-Id")

    def test_correlation_id_is_not_authorization_material(self, client):
        """A correlation ID must never authenticate: /auth/me with only a
        correlation ID (no session cookie) must be rejected."""
        resp = client.get(
            "/auth/me",
            headers={"X-Correlation-Id": "corr-not-a-session-000000"},
        )
        assert resp.status_code in (401, 403)

    def test_malicious_correlation_header_is_not_adopted(self, client, db_session):
        """A credential-shaped correlation ID must never ride into logs or
        operational events: the boundary REFUSES it (echoes a fresh ID) and
        the malicious value is persisted nowhere."""
        from app.identity import NotificationEvent

        supplied = "tok-super-secret-analytics-token-9999"
        resp = client.get("/health", headers={"X-Correlation-Id": supplied})
        echoed = resp.headers.get("X-Correlation-Id")
        assert echoed != supplied, "credential-shaped header was adopted verbatim"
        assert echoed and len(echoed) >= 16  # a fresh, sane ID was minted
        # Nothing durable carries it verbatim (events are sanitized/validated).
        rows = db_session.query(NotificationEvent).all()
        assert all(
            supplied not in json.dumps(r.details or {}, default=str) for r in rows
        )
        assert all(r.correlation_id != supplied for r in rows)


# ---------------------------------------------------------------------------
# 2. Structured secret-safe logging
# ---------------------------------------------------------------------------


class TestStructuredSecretSafeLogging:
    def _capture(self, caplog):
        records = []
        for r in caplog.records:
            records.append(r)
        return records

    def test_request_log_emits_structured_json(self, client, caplog):
        from app import structlog_config  # app.observability

        with caplog.at_level(logging.INFO, logger=structlog_config.ACCESS_LOGGER_NAME):
            resp = client.get("/health")
        assert resp.status_code == 200
        json_records = [
            r for r in self._capture(caplog) if getattr(r, "structured_json", None)
        ]
        assert json_records, "no structured JSON log record was emitted"
        rec = json_records[-1].structured_json
        for field in (
            "ts",
            "level",
            "logger",
            "correlation_id",
            "route",
            "method",
            "status",
            "duration_ms",
        ):
            assert field in rec, f"structured log missing field {field}"
        assert rec["route"] == "/health"
        assert rec["method"] == "GET"
        assert rec["status"] == 200

    def test_session_cookie_value_never_in_logs(self, client, caplog):
        from app import structlog_config  # app.observability

        # A session-shaped cookie; its VALUE must never appear in any record.
        with caplog.at_level(
            logging.DEBUG, logger=structlog_config.ACCESS_LOGGER_NAME
        ):
            client.get(
                "/health",
                headers={"Cookie": "strikenova_session=sid-ABSOLUTE-SECRET-9999"},
            )
        blob = caplog.text
        assert "sid-ABSOLUTE-SECRET-9999" not in blob

    def test_authorization_and_token_headers_never_in_logs(self, client, caplog):
        from app import structlog_config  # app.observability

        secret = "tok-super-secret-analytics-token-9999"
        with caplog.at_level(
            logging.DEBUG, logger=structlog_config.ACCESS_LOGGER_NAME
        ):
            client.get(
                "/health",
                headers={
                    "Authorization": f"Bearer {secret}",
                    "X-Session-Id": "sid-header-secret-0000",
                },
            )
        blob = caplog.text
        assert secret not in blob
        assert "sid-header-secret-0000" not in blob

    def test_error_payloads_do_not_leak_secrets(self, client, caplog):
        from app import structlog_config  # app.observability

        secret = "tok-super-secret-analytics-token-9999"
        with caplog.at_level(
            logging.DEBUG, logger=structlog_config.ACCESS_LOGGER_NAME
        ):
            client.get(
                "/api/v1/chains/NIFTY/expiries",
                headers={"Authorization": f"Bearer {secret}"},
            )
        blob = caplog.text
        assert secret not in blob


# ---------------------------------------------------------------------------
# 3. Notification contract / isolation / dedup
# ---------------------------------------------------------------------------


@pytest.fixture()
def two_users(db_session):
    """Create two platform users with live sessions (Day 45 test idiom);
    return (sid_a, uid_a, sid_b, uid_b)."""
    from app.routers.auth import create_session_record
    from app.services.token_store import set_token

    def _user():
        uid = str(uuid4())
        from app.identity import User

        user = User(
            id=uid,
            status="active",
            identity_source="upstox",
            broker_provider="UPSTOX",
            broker_user_id=f"d46-{uid[:8]}",
        )
        db_session.add(user)
        db_session.commit()
        db_session.expire(user)
        sid = set_token(f"tok-d46-{uid[:8]}")
        create_session_record(db_session, uid, sid)
        return sid, uid

    sid_a, uid_a = _user()
    sid_b, uid_b = _user()
    return sid_a, uid_a, sid_b, uid_b


class TestNotificationContract:
    def test_event_schema_is_complete(self, db_session, two_users):
        from app.services import notifications

        sid_a, uid_a, _, _ = two_users
        event = notifications.publish(
            db_session,
            event_type="market_data.stale",
            severity="warning",
            source="market_data",
            summary="NIFTY chain data is stale",
            details={"symbol": "NIFTY", "age_seconds": 900},
            user_scope=uid_a,
            correlation_id="corr-schema-0000000001",
            dedup_key="market_data.stale:NIFTY",
        )
        db_session.commit()
        for field in (
            "event_id",
            "event_type",
            "severity",
            "occurred_at",
            "source",
            "summary",
            "details",
            "correlation_id",
            "dedup_key",
        ):
            assert field in event, f"notification missing {field}"
        assert event["event_type"] == "market_data.stale"
        assert event["severity"] == "warning"
        assert event["correlation_id"] == "corr-schema-0000000001"

    def test_tenant_isolation_user_cannot_read_foreign_events(
        self, client, db_session, two_users
    ):
        from fastapi import HTTPException

        from app.services import notifications

        sid_a, uid_a, sid_b, uid_b = two_users
        notifications.publish(
            db_session,
            event_type="execution.failed",
            severity="error",
            source="execution",
            summary="Paper order rejected",
            details={"order_id": "ord-1"},
            user_scope=uid_a,
            correlation_id="corr-iso-00000000001",
            dedup_key=None,
        )
        db_session.commit()

        # Service-level: B cannot resolve A's event.
        events_a = notifications.list_for_user(db_session, uid_a)
        ids_a = {e["event_id"] for e in events_a}
        assert ids_a, "user A should see their own event"
        with pytest.raises(HTTPException):
            notifications.get_for_user(db_session, uid_b, next(iter(ids_a)))

        # API-level: B's session cannot read A's event either.
        resp_b = client2_get_foreign(client, sid_b, next(iter(ids_a)))
        assert resp_b.status_code in (403, 404)

    def test_user_scoped_event_not_visible_to_other_users(
        self, db_session, two_users
    ):
        from app.services import notifications

        sid_a, uid_a, sid_b, uid_b = two_users
        notifications.publish(
            db_session,
            event_type="market_data.stale",
            severity="warning",
            source="market_data",
            summary="A-only event",
            details={},
            user_scope=uid_a,
            correlation_id="corr-iso-00000000002",
            dedup_key=None,
        )
        db_session.commit()
        ids_b = {e["event_id"] for e in notifications.list_for_user(db_session, uid_b)}
        ids_a = {e["event_id"] for e in notifications.list_for_user(db_session, uid_a)}
        assert ids_a
        assert ids_a.isdisjoint(ids_b)

    def test_no_admin_self_grant_via_notifications(self, client, db_session, two_users):
        """Notification surfaces must not grant admin privileges: fabricated
        admin-ish event types/payloads must not elevate the caller."""
        sid_a, uid_a, _, _ = two_users
        cookie_name = "strikenova_session"
        for payload in (
            {"event_type": "admin.granted", "severity": "info"},
            {"event_type": "platform.escalate", "severity": "critical"},
        ):
            resp = client.post(
                "/api/v1/notifications/publish",
                json={**payload, "details": {"is_admin": True}},
                cookies={cookie_name: sid_a},
            )
            assert resp.status_code in (401, 403, 404, 405, 422), (
                f"fabricated {payload} must not be accepted: {resp.status_code}"
            )
        from app.identity import User

        user = (
            db_session.query(User).filter(User.id == uid_a).one_or_none()
        )
        assert user is not None
        assert bool(getattr(user, "is_admin", False)) is False

    def test_payloads_are_sanitized_secret_free(self, db_session, two_users):
        from app.services import notifications

        sid_a, uid_a, _, _ = two_users
        secret = "tok-super-secret-analytics-token-9999"
        event = notifications.publish(
            db_session,
            event_type="broker.auth_failed",
            severity="error",
            source="broker_adapter",
            summary="Upstox authorization failed",
            details={
                "broker": "UPSTOX",
                "access_token": secret,  # must be dropped
                "note": secret,  # value-shaped secret must be redacted
            },
            user_scope=uid_a,
            correlation_id="corr-san-0000000001",
            dedup_key=None,
        )
        db_session.commit()
        blob = json.dumps(event, default=str)
        assert secret not in blob
        assert "access_token" not in blob

    def test_dedup_suppresses_duplicate_within_window(
        self, db_session, two_users
    ):
        from app.services import notifications

        sid_a, uid_a, _, _ = two_users
        kwargs = dict(
            event_type="market_data.stale",
            severity="warning",
            source="market_data",
            summary="NIFTY stale",
            details={"symbol": "NIFTY"},
            user_scope=uid_a,
            correlation_id="corr-dedup-000000001",
        )
        first = notifications.publish(
            db_session, dedup_key="market_data.stale:NIFTY", **kwargs
        )
        second = notifications.publish(
            db_session, dedup_key="market_data.stale:NIFTY", **kwargs
        )
        db_session.commit()
        assert first["event_id"] == second["event_id"], (
            "duplicate within the dedup window must return the SAME event"
        )

    def test_dedup_window_expiry_allows_new_event(self, db_session, two_users):
        from app.services import notifications

        sid_a, uid_a, _, _ = two_users
        kwargs = dict(
            event_type="market_data.stale",
            severity="warning",
            source="market_data",
            summary="NIFTY stale again",
            details={"symbol": "NIFTY"},
            user_scope=uid_a,
            correlation_id="corr-dedup-000000002",
        )
        first = notifications.publish(
            db_session, dedup_key="market_data.stale:NIFTY", **kwargs
        )
        notifications.force_expire_dedup_window(db_session, user_scope=uid_a)
        second = notifications.publish(
            db_session, dedup_key="market_data.stale:NIFTY", **kwargs
        )
        db_session.commit()
        assert first["event_id"] != second["event_id"]

    def test_dedup_key_scoped_per_user(self, db_session, two_users):
        from app.services import notifications

        sid_a, uid_a, sid_b, uid_b = two_users
        kwargs = dict(
            event_type="market_data.stale",
            severity="warning",
            source="market_data",
            summary="stale",
            details={},
            correlation_id="corr-dedup-000000003",
        )
        a = notifications.publish(
            db_session, user_scope=uid_a, dedup_key="market_data.stale:NIFTY", **kwargs
        )
        b = notifications.publish(
            db_session, user_scope=uid_b, dedup_key="market_data.stale:NIFTY", **kwargs
        )
        db_session.commit()
        assert a["event_id"] != b["event_id"], (
            "identical dedup keys for different users must not collapse"
        )


def client2_get_foreign(client, sid_b, foreign_event_id):
    """Hit the notifications API with B's session for A's event id."""
    return client.get(
        f"/api/v1/notifications/{foreign_event_id}",
        cookies={"strikenova_session": sid_b},
    )


# ---------------------------------------------------------------------------
# 4. Operational alert conditions
# ---------------------------------------------------------------------------


class TestOperationalAlerts:
    def test_market_data_staleness_condition(self, db_session, two_users):
        from app.services import operations

        sid_a, uid_a, _, _ = two_users
        op = operations.record_market_data_stale(
            db_session,
            user_scope=uid_a,
            symbol="NIFTY",
            age_seconds=900,
            correlation_id="corr-alert-md-0000001",
        )
        db_session.commit()
        assert op["event_type"] == "market_data.stale"
        assert op["severity"] in ("warning", "error")
        assert op["source"] == "market_data"
        assert op["dedup_key"].startswith("market_data.stale")
        assert "NIFTY" in json.dumps(op["details"])

    def test_broker_adapter_failure_condition(self, db_session, two_users):
        from app.services import operations

        sid_a, uid_a, _, _ = two_users
        op = operations.record_broker_failure(
            db_session,
            user_scope=uid_a,
            broker="UPSTOX",
            reason="authentication rejected",
            correlation_id="corr-alert-bk-0000001",
        )
        db_session.commit()
        assert op["event_type"] == "broker.auth_failed"
        assert op["severity"] in ("error", "critical")
        # The human reason survives; no credential material is derivable.
        assert "authentication rejected" in json.dumps(op["details"])

    def test_execution_failure_condition(self, db_session, two_users):
        from app.services import operations

        sid_a, uid_a, _, _ = two_users
        op = operations.record_execution_failure(
            db_session,
            user_scope=uid_a,
            order_family="paper-1",
            reason="broker timeout",
            correlation_id="corr-alert-ex-0000001",
        )
        db_session.commit()
        assert op["event_type"] == "execution.failed"
        assert op["severity"] in ("error", "critical")

    def test_ingestion_job_failure_condition(self, db_session):
        from app.services import operations

        op = operations.record_job_failure(
            db_session,
            job="daily_ingestion",
            reason="upstream 503",
            correlation_id="corr-alert-job-000001",
        )
        db_session.commit()
        assert op["event_type"] == "ingestion.job_failed"
        assert op["severity"] in ("error", "critical")
        # Platform-scoped job events carry no user scope.
        assert op.get("user_scope") in (None, "", "platform")

    def test_readiness_degradation_condition(self, db_session):
        from app.services import operations

        op = operations.record_readiness_degradation(
            db_session,
            component="database",
            reason="connection refused",
            correlation_id="corr-alert-rd-0000001",
        )
        db_session.commit()
        assert op["event_type"] == "readiness.degraded"
        assert op["severity"] in ("error", "critical")

    def test_alerts_carry_required_fields_and_dedup(self, db_session, two_users):
        from app.services import operations

        sid_a, uid_a, _, _ = two_users
        op = operations.record_market_data_stale(
            db_session,
            user_scope=uid_a,
            symbol="NIFTY",
            age_seconds=900,
            correlation_id="corr-alert-md-0000002",
        )
        for field in ("source", "occurred_at", "severity", "dedup_key"):
            assert field in op
        again = operations.record_market_data_stale(
            db_session,
            user_scope=uid_a,
            symbol="NIFTY",
            age_seconds=910,
            correlation_id="corr-alert-md-0000003",
        )
        assert again["event_id"] == op["event_id"], "alert dedup must hold"


# ---------------------------------------------------------------------------
# 5. Health / readiness
# ---------------------------------------------------------------------------


class TestHealthReadiness:
    def test_health_is_cheap_liveness(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_readiness_preserves_contract_shape(self, client):
        resp = client.get("/readiness")
        assert resp.status_code in (200, 503)
        body = resp.json()
        assert body["status"] in ("ready", "degraded")
        assert "database" in body["checks"]
        assert "token_store" in body["checks"]

    def test_readiness_response_is_secret_free(self, client):
        resp = client.get("/readiness")
        text = resp.text
        for marker in (
            "strikenova_session",
            "Authorization",
            "tok-",
            "api_key",
        ):
            assert marker not in text
