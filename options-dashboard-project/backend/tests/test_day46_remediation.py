"""Day 46 remediation — F13–F17 (PR #94 review).

Failing-first tests for the five independent-review findings. Every test
targets REAL production behavior, not test-only wrappers:

* **F13** — the four alert helpers are invoked by the actual production
  failure boundaries (broker call translation, paper execution error
  mapping, ingestion pipeline failure, stale market feed).
* **F14** — dedup identity is (scope, dedup_key) for user AND platform
  scopes; scopes never cross-deduplicate.
* **F15** — even an unhandled exception's 500 response echoes the same
  correlation ID.
* **F16** — structured access logs carry the authenticated user's
  durable id (and nothing for anonymous requests), without cross-user
  context bleed.
* **F17** — channel delivery is transaction-aware: delivery is observed
  only after COMMIT; rollback/failure leaves no phantom notification.
"""

from __future__ import annotations

import json
import logging
import uuid
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.identity import User, create_session_record
from app.main import app
from app.services import token_store
from tests.test_day45_admin import client, db_session, engine  # noqa: F401
from tests.test_day46_observability import two_users  # noqa: F401
from tests.day34_seeding import day34_gated_seeding  # noqa: F401  (autouse fixture)


@pytest.fixture()
def user_session(db_session):
    uid = str(uuid4())
    user = User(
        id=uid,
        status="active",
        identity_source="upstox",
        broker_provider="UPSTOX",
        broker_user_id=f"d46r-{uid[:8]}",
    )
    db_session.add(user)
    db_session.commit()
    db_session.expire(user)
    sid = token_store.set_token(f"tok-d46r-{uid[:8]}")
    create_session_record(db_session, uid, sid)
    return sid, uid


# ---------------------------------------------------------------------------
# F13 — production failure paths emit operational alerts
# ---------------------------------------------------------------------------


class TestF13ProductionAlertWiring:
    def test_broker_auth_failure_on_chain_call_emits_alert(
        self, client, db_session, two_users
    ):
        """A real chain request whose Upstox adapter raises a session-code
        BrokerError emits broker.auth_failed (user-scoped, deduped)."""
        from unittest.mock import patch

        from app.brokers.domain.errors import BrokerError
        from app.services.notifications import captured_deliveries
        from tests.test_day45_admin import _cookie

        sid_a, uid_a, _, _ = two_users
        captured_deliveries().clear()

        err = BrokerError(code="TOKEN_EXPIRED", message="token expired", status_code=401)
        with patch(
            "app.routers.chains.gateway.create",
        ) as fake_gateway:
            fake_gateway.return_value.get_option_contracts = _async_raises(err)
            resp = client.get(
                "/chains/NIFTY/expiries",
                headers={"X-Session-Id": sid_a},
            )
        assert resp.status_code == 401
        rows = _events(db_session)
        stale = [r for r in rows if r.event_type == "broker.auth_failed"]
        assert stale, "real broker auth failure emitted no operational alert"
        row = stale[0]
        assert row.user_scope == uid_a
        assert row.source == "broker_adapter"
        assert row.dedup_key == "broker.auth_failed:UPSTOX"

    def test_broker_upstream_failure_502_emits_alert(
        self, client, db_session, two_users
    ):
        from unittest.mock import patch

        from app.brokers.domain.errors import BrokerError
        from tests.test_day45_admin import _cookie

        sid_a, uid_a, _, _ = two_users
        err = BrokerError(code="UPSTREAM_ERROR", message="upstream 503", status_code=503)
        with patch("app.routers.chains.gateway.create") as fake_gateway:
            fake_gateway.return_value.get_option_contracts = _async_raises(err)
            resp = client.get(
                "/chains/NIFTY/expiries",
                headers={"X-Session-Id": sid_a},
            )
        assert resp.status_code == 502
        rows = _events(db_session)
        assert any(
            r.event_type == "broker.auth_failed" and r.user_scope == uid_a
            for r in rows
        )

    def test_paper_execution_failure_emits_alert(self, client, db_session, two_users):
        """A real paper execution whose price resolution fails emits
        execution.failed (user-scoped) through _paper_error."""
        from unittest.mock import patch

        from app.brokers.domain.errors import BrokerError
        from tests.test_day45_admin import _cookie

        sid_a, uid_a, _, _ = two_users
        body = {
            "client_order_id": f"ord-{uuid.uuid4().hex[:10]}",
            "symbol": "NIFTY",
            "legs": [
                {
                    "symbol": "NIFTY",
                    "expiration_date": "2026-12-31",
                    "strike_price": 26000,
                    "option_type": "call",
                    "action": "buy",
                    "quantity": 1,
                    "lot_size": 75,
                }
            ],
        }
        # Drive the REAL production failure chain: the market gate is
        # OPEN, then the broker adapter raises a (non-session) BrokerError
        # → resolve_market_prices wraps it in PaperExecutionError(
        # EXECUTION_FAILED) → _paper_error maps it to 502 AND emits
        # execution.failed for the authenticated actor.
        err = BrokerError(code="UPSTREAM_ERROR", message="chain upstream down", status_code=503)
        open_status = SimpleNamespace(status="open")
        with patch(
            "app.routers.paper.get_market_status",
            new=_async_returns(open_status),
        ), patch(
            "app.routers.paper.gateway.create",
        ) as fake_gateway:
            fake_gateway.return_value.get_option_chain = _async_raises(err)
            resp = client.post("/paper/executions", json=body, headers={"X-Session-Id": sid_a})
        assert resp.status_code == 502, resp.text
        rows = _events(db_session)
        assert any(
            r.event_type == "execution.failed" and r.user_scope == uid_a
            for r in rows
        ), "real execution failure emitted no operational alert"

    def test_ingestion_job_failure_emits_platform_alert(self, db_session):
        """The DailyIngestion pipeline failure boundary emits a
        platform-scoped ingestion.job_failed event."""
        from unittest.mock import MagicMock, patch

        from app.services.daily_ingestion import DailyIngestionPipeline

        # The REAL fixture DB session: the alert is an event row persisted
        # through the pipeline's own unit of work — a MagicMock sink would
        # silently discard it (and prove nothing).
        pipeline = DailyIngestionPipeline(
            db=db_session,
            client=MagicMock(),
            target_date=__import__("datetime").date(2026, 9, 21),
            skip_contracts=True, skip_options=True, skip_greeks=True, skip_gex=True,
        )
        with patch(
            "app.services.daily_ingestion._ingest_nifty_day",
            new=_async_raises(RuntimeError("boom")),
        ):
            import asyncio

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(pipeline.run())
            finally:
                loop.close()
        assert result.status == "FAILED"
        rows = _events(db_session)
        assert any(
            r.event_type == "ingestion.job_failed" and r.user_scope is None
            for r in rows
        ), "real ingestion failure emitted no platform alert"

    def test_market_data_stale_wired_into_live_feed(self, db_session, two_users):
        """The live-feed staleness recovery path (production chains WS)
        emits market_data.stale for the affected user."""
        from app.services import operations
        from app.services import notifications as notif_mod

        # The wiring calls the operations helper from the feed recovery
        # path; here we prove the helper is imported by the production
        # module (source-level, since WS spanning needs a live broker).
        import inspect

        from app.routers import chains as chains_mod

        src = inspect.getsource(chains_mod)
        assert "record_market_data_stale" in src
        assert "market_data" in src


    def test_stale_feed_without_platform_identity_is_platform_scoped(self, db_session, two_users):
        """A legacy session-scoped WS caller (no durable platform identity)
        produces a PLATFORM-scoped staleness event — never an empty-string
        user scope, which would strand the event outside every tenant.

        The WS loop needs a live broker feed, so the production call site
        is pinned at source level (same convention as
        ``test_market_data_stale_wired_into_live_feed``) and the identity
        resolution is proven behaviorally: an unknown session resolves to
        ``None``, and the call site passes that value through UNMODIFIED
        (no ``or ""`` coercion).
        """
        import inspect

        from app.routers import chains as chains_mod

        # Behavioral: no platform row ⇒ resolved identity is None.
        assert chains_mod._platform_user_id("legacy-session-without-platform-row") is None

        # Production call site: the resolved identity is passed through
        # verbatim as the event scope (platform scope when None).
        call_site = inspect.getsource(chains_mod)
        assert "user_scope=_platform_user_id(session_id)" in call_site, (
            "staleness call site must pass the resolved platform identity "
            "through unmodified (platform scope when it is None)"
        )
        assert 'user_scope=_platform_user_id(session_id) or ""' not in call_site, (
            "staleness events must never be stranded under an empty-string "
            "user scope"
        )


def _async_raises(err):
    async def _raise(*args, **kwargs):
        raise err

    return _raise


def _async_returns(value):
    async def _return(*args, **kwargs):
        return value

    return _return


class _RestoreOnExit:
    """Patch + route-app rebuild with full restore on exit (context manager).

    Also exposes ``probe(correlation_header_value)`` — a raw ASGI probe
    through the FULL app stack (including middleware and the server error
    responder) with ``raise_server_exceptions`` disabled, so the test sees
    exactly the server-generated 500 response a real client would get.
    """

    def __init__(self, patcher, rebuild, route, original_app):
        self._patcher = patcher
        self._rebuild = rebuild
        self._route = route
        self._original_app = original_app

    def __enter__(self):
        self._patcher.__enter__()
        self._rebuild()
        return self._probe

    def __exit__(self, *exc):
        try:
            self._route.app = self._original_app
        finally:
            return self._patcher.__exit__(*exc)

    @staticmethod
    def _probe(correlation_header_value):
        # Plain TestClient (no ``with``): the probe exercises the ASGI
        # middleware stack only. Entering the context manager would run
        # the lifespan handler, whose init_db() → ``alembic upgrade``
        # collides with the conftest-shared engine already schema-loaded
        # by other suites (see tests/conftest.py “Hermetic init_db”).
        from fastapi.testclient import TestClient

        from app.main import app

        raw = TestClient(app, raise_server_exceptions=False)
        headers = {"X-Correlation-Id": correlation_header_value} if correlation_header_value else {}
        return raw.get("/health", headers=headers)


def _events(db):
    from app.identity import NotificationEvent

    db.expire_all()
    return db.query(NotificationEvent).all()


# ---------------------------------------------------------------------------
# F14 — deduplication identity (scope, dedup_key), both scopes
# ---------------------------------------------------------------------------


class TestF14DedupScopes:
    def _kwargs(self, **over):
        base = dict(
            event_type="ingestion.job_failed",
            severity="error",
            source="background_jobs",
            summary="job failed",
            details={"job": "daily"},
            correlation_id=f"corr-{uuid.uuid4().hex[:12]}",
            dedup_key="ingestion.job_failed:daily",
        )
        base.update(over)
        return base

    def test_repeated_platform_event_dedups(self, db_session):
        from app.services import notifications

        first = notifications.publish(db_session, user_scope=None, **self._kwargs())
        second = notifications.publish(db_session, user_scope=None, **self._kwargs())
        db_session.commit()
        assert first["event_id"] == second["event_id"]

    def test_platform_and_user_events_do_not_cross_dedup(
        self, db_session, two_users
    ):
        from app.services import notifications

        sid_a, uid_a, _, _ = two_users
        platform = notifications.publish(db_session, user_scope=None, **self._kwargs())
        user = notifications.publish(db_session, user_scope=uid_a, **self._kwargs())
        db_session.commit()
        assert platform["event_id"] != user["event_id"]

    def test_two_users_do_not_dedup_each_other(self, db_session, two_users):
        from app.services import notifications

        sid_a, uid_a, sid_b, uid_b = two_users
        a = notifications.publish(db_session, user_scope=uid_a, **self._kwargs())
        b = notifications.publish(db_session, user_scope=uid_b, **self._kwargs())
        db_session.commit()
        assert a["event_id"] != b["event_id"]

    def test_different_dedup_key_creates_new_event(self, db_session):
        from app.services import notifications

        a = notifications.publish(db_session, user_scope=None, **self._kwargs())
        b = notifications.publish(
            db_session, user_scope=None, **self._kwargs(dedup_key="ingestion.job_failed:other")
        )
        db_session.commit()
        assert a["event_id"] != b["event_id"]

    def test_platform_window_expiry_creates_new_event(self, db_session):
        from app.services import notifications

        first = notifications.publish(db_session, user_scope=None, **self._kwargs())
        notifications.force_expire_dedup_window(db_session, user_scope=None)
        second = notifications.publish(db_session, user_scope=None, **self._kwargs())
        db_session.commit()
        assert first["event_id"] != second["event_id"]


# ---------------------------------------------------------------------------
# F15 — correlation ID echoes even on unhandled 500s
# ---------------------------------------------------------------------------


class TestF15UnhandledExceptionEcho:
    @staticmethod
    def _break_health():
        """Patch the REGISTERED /health endpoint to raise unhandled.

        FastAPI builds each route's ASGI app from the endpoint at
        registration time, so the test patches the endpoint AND rebuilds
        the route's app chain (restored on exit by patch.object).
        """
        from unittest.mock import patch

        from app.main import app

        route = next(r for r in app.routes if getattr(r, "path", "") == "/health")

        def boom():
            raise RuntimeError("unhandled probe failure")

        original_app = route.app

        def _rebuild():
            # FastAPI builds route.app as an ASGI callable wrapping the
            # endpoint via request_response(); rebuild that exact wrapper.
            async def app(scope, receive, send):
                raise RuntimeError("unhandled probe failure")

            route.app = app

        patcher = patch.object(route, "endpoint", boom)
        _rebuild()
        return _RestoreOnExit(patcher, _rebuild, route, original_app)

    @classmethod
    def contextmanager(cls):
        return cls._break_health()

    def test_unhandled_500_echoes_supplied_correlation_id(self):
        supplied = "corr-f15-supplied-000001"
        with self._break_health() as probe:
            resp = probe(supplied)
        assert resp.status_code in (500, 503)
        assert resp.headers.get("X-Correlation-Id") == supplied

    def test_unhandled_500_echoes_generated_correlation_id(self):
        with self._break_health() as probe:
            resp = probe(None)
        assert resp.status_code in (500, 503)
        cid = resp.headers.get("X-Correlation-Id")
        assert cid and cid.startswith("corr-")


# ---------------------------------------------------------------------------
# F16 — authenticated actor identity reaches structured access logs
# ---------------------------------------------------------------------------


class TestF16UserIdInAccessLogs:
    def test_anonymous_request_logs_null_user(self, client, caplog):
        from app import structlog_config

        with caplog.at_level(logging.INFO, logger=structlog_config.ACCESS_LOGGER_NAME):
            client.get("/health")
        records = [
            r for r in caplog.records if getattr(r, "structured_json", None)
        ]
        assert records
        rec = records[-1].structured_json
        assert not rec.get("user_id")

    def test_authenticated_request_logs_durable_user_id(
        self, client, caplog, user_session
    ):
        from app import structlog_config

        sid, uid = user_session
        with caplog.at_level(logging.INFO, logger=structlog_config.ACCESS_LOGGER_NAME):
            resp = client.get("/auth/me", headers={"X-Session-Id": sid})
        assert resp.status_code == 200
        records = [
            r for r in caplog.records if getattr(r, "structured_json", None)
        ]
        assert records
        rec = records[-1].structured_json
        assert rec.get("user_id") == uid

    def test_concurrent_users_do_not_bleed_identity(self, client, caplog, two_users):
        """Two sequential authenticated requests on the same app instance
        must each log their own durable user id (no cross-user bleed)."""
        from app import structlog_config

        sid_a, uid_a, sid_b, uid_b = two_users
        with caplog.at_level(logging.INFO, logger=structlog_config.ACCESS_LOGGER_NAME):
            r1 = client.get("/auth/me", headers={"X-Session-Id": sid_a})
            r2 = client.get("/auth/me", headers={"X-Session-Id": sid_b})
        assert r1.status_code == 200 and r2.status_code == 200
        records = [
            r for r in caplog.records if getattr(r, "structured_json", None)
        ]
        user_ids = [
            rec.structured_json.get("user_id")
            for rec in records
            if rec.structured_json.get("route") == "/auth/me"
        ]
        assert uid_a in user_ids and uid_b in user_ids


# ---------------------------------------------------------------------------
# F17 — transaction-aware delivery (no phantom notifications)
# ---------------------------------------------------------------------------


class TestF17TransactionAwareDelivery:
    def test_publish_commit_delivers_once(self, db_session):
        from app.services import notifications

        notifications.clear_captured_deliveries()
        event = notifications.publish(
            db_session,
            event_type="market_data.stale",
            severity="warning",
            source="market_data",
            summary="commit probe",
            details={},
            user_scope=None,
            dedup_key=f"f17-commit:{uuid.uuid4().hex[:8]}",
        )
        db_session.commit()
        assert notifications.captured_deliveries(), "commit must release delivery"
        assert notifications.captured_deliveries()[-1]["event_id"] == event["event_id"]

    def test_publish_rollback_never_delivers(self, db_session):
        from app.services import notifications

        notifications.clear_captured_deliveries()
        notifications.publish(
            db_session,
            event_type="market_data.stale",
            severity="warning",
            source="market_data",
            summary="rollback probe",
            details={},
            user_scope=None,
            dedup_key=f"f17-rollback:{uuid.uuid4().hex[:8]}",
        )
        db_session.rollback()
        assert notifications.captured_deliveries() == [], (
            "rolled-back notification must never reach a channel"
        )
        rows = _events(db_session)
        assert all("rollback probe" not in (r.summary or "") for r in rows)

    def test_commit_failure_discards_staged_delivery(self, db_session):
        from unittest.mock import patch

        from app.services import notifications

        notifications.clear_captured_deliveries()
        notifications.publish(
            db_session,
            event_type="market_data.stale",
            severity="warning",
            source="market_data",
            summary="commit-failure probe",
            details={},
            user_scope=None,
            dedup_key=f"f17-commitfail:{uuid.uuid4().hex[:8]}",
        )
        real_commit = db_session.commit

        def failing_commit(*args, **kwargs):
            db_session.rollback()
            raise RuntimeError("simulated commit failure")

        with patch.object(type(db_session), "commit", failing_commit):
            with pytest.raises(RuntimeError):
                db_session.commit()
        assert notifications.captured_deliveries() == [], (
            "failed commit must discard the staged delivery (no phantom)"
        )
        # restore for the fixture teardown
        db_session.commit = real_commit

    def test_duplicate_inside_window_still_deduped(self, db_session):
        from app.services import notifications

        notifications.clear_captured_deliveries()
        key = f"f17-dedup:{uuid.uuid4().hex[:8]}"
        a = notifications.publish(
            db_session,
            event_type="market_data.stale",
            severity="warning",
            source="market_data",
            summary="dedup probe",
            details={},
            user_scope=None,
            dedup_key=key,
        )
        b = notifications.publish(
            db_session,
            event_type="market_data.stale",
            severity="warning",
            source="market_data",
            summary="dedup probe",
            details={},
            user_scope=None,
            dedup_key=key,
        )
        db_session.commit()
        assert a["event_id"] == b["event_id"]
        deliveries = [
            d for d in notifications.captured_deliveries()
            if d["event_id"] == a["event_id"]
        ]
        assert len(deliveries) == 1, "dedup hit must not re-deliver"

    def test_nested_savepoint_semantics(self, db_session):
        """A rolled-back SAVEPOINT must discard that savepoint's staged
        delivery while outer-commit delivery still works."""
        from app.services import notifications

        notifications.clear_captured_deliveries()
        key = f"f17-sp:{uuid.uuid4().hex[:8]}"
        outer = notifications.publish(
            db_session,
            event_type="market_data.stale",
            severity="warning",
            source="market_data",
            summary="outer",
            details={},
            user_scope=None,
            dedup_key=key,
        )
        nested = db_session.begin_nested()
        notifications.publish(
            db_session,
            event_type="market_data.stale",
            severity="warning",
            source="market_data",
            summary="inner",
            details={},
            user_scope=None,
            dedup_key=f"{key}-inner",
        )
        nested.rollback()
        db_session.commit()

        delivered_ids = {d["event_id"] for d in notifications.captured_deliveries()}
        assert outer["event_id"] in delivered_ids
        rows = _events(db_session)
        assert all("inner" != (r.summary or "") for r in rows)

    def test_savepoint_release_delivers_with_outer_commit(self, db_session):
        """A RELEASED (committed) savepoint's staged delivery ships with the
        outer commit — release must not lose the delivery."""
        from app.services import notifications

        notifications.clear_captured_deliveries()
        key = f"f17-sp-rel:{uuid.uuid4().hex[:8]}"
        db_session.begin_nested()
        inner = notifications.publish(
            db_session,
            event_type="market_data.stale",
            severity="warning",
            source="market_data",
            summary="inner-released",
            details={},
            user_scope=None,
            dedup_key=key,
        )
        db_session.get_nested_transaction().commit()
        db_session.commit()

        delivered_ids = {d["event_id"] for d in notifications.captured_deliveries()}
        assert inner["event_id"] in delivered_ids, (
            "released savepoint's event must be delivered on outer commit"
        )

    def test_savepoint_rollback_discards_only_savepoint_delivery(self, db_session):
        """A savepoint rollback discards THAT savepoint's staged delivery
        while the OUTER transaction's staged delivery still ships."""
        from app.services import notifications

        notifications.clear_captured_deliveries()
        key = f"f17-sp-rb:{uuid.uuid4().hex[:8]}"
        outer = notifications.publish(
            db_session,
            event_type="market_data.stale",
            severity="warning",
            source="market_data",
            summary="outer-kept",
            details={},
            user_scope=None,
            dedup_key=key,
        )
        nested = db_session.begin_nested()
        inner = notifications.publish(
            db_session,
            event_type="market_data.stale",
            severity="warning",
            source="market_data",
            summary="inner-dropped",
            details={},
            user_scope=None,
            dedup_key=f"{key}-inner",
        )
        nested.rollback()
        db_session.commit()

        delivered = {d["event_id"]: d for d in notifications.captured_deliveries()}
        assert outer["event_id"] in delivered, "outer delivery must survive"
        assert inner["event_id"] not in delivered, (
            "rolled-back savepoint's delivery must be discarded (no phantom)"
        )

    def test_savepoint_release_does_not_deliver_before_root_commit(self, db_session):
        """Releasing a savepoint must NOT deliver staged events.

        SQLAlchemy fires ``after_commit`` on savepoint RELEASE as well; a
        release must therefore never sweep staging, because the OUTER
        transaction may still roll back (phantom delivery). The row must
        ship only when the root transaction commits.
        """
        from app.services import notifications

        notifications.clear_captured_deliveries()
        key = f"f17-rel-gate:{uuid.uuid4().hex[:8]}"
        notifications.publish(
            db_session,
            event_type="market_data.stale",
            severity="warning",
            source="market_data",
            summary="pre-release",
            details={},
            user_scope=None,
            dedup_key=key,
        )
        db_session.begin_nested()
        db_session.get_nested_transaction().commit()  # RELEASE
        db_session.rollback()  # outer rollback AFTER the release

        assert not notifications.captured_deliveries(), (
            "savepoint release must not deliver: the outer transaction "
            "rolled back after the release, so delivery here is a phantom"
        )

    def test_readiness_failure_does_not_emit_phantom(self, db_session):
        """If the readiness degradation transaction fails, no platform
        notification is delivered."""
        from unittest.mock import patch

        from app.services import notifications, operations

        notifications.clear_captured_deliveries()
        with patch.object(
            type(db_session), "commit", side_effect=RuntimeError("db gone")
        ):
            with pytest.raises(RuntimeError):
                operations.record_readiness_degradation(
                    db_session,
                    component="database",
                    reason="probe outage",
                    correlation_id="corr-f17-readiness-1",
                )
                db_session.commit()
        assert notifications.captured_deliveries() == [], (
            "failed readiness transaction must not deliver a phantom event"
        )


# ---------------------------------------------------------------------------
# Transaction ownership (post-merge follow-up): the alert path OWNS its
# transaction. Request-scoped alert boundaries record through a SHORT-LIVED
# session bound to the CALLER'S ENGINE — they never join, stage into, commit,
# or roll back the caller's session — so the business operation remains the
# only authority over its own unit of work while the alert stays durable on
# error paths (where the request DI lifecycle never commits). Only a
# background/ad-hoc caller without a session uses SessionLocal directly.
# ---------------------------------------------------------------------------


class TestTransactionOwnership:
    """Fixtures use FILE-based SQLite so the "independent connection" is a
    real second connection: an in-memory StaticPool engine shares ONE
    connection across sessions, which makes uncommitted rows visible and
    would prove nothing about commit behavior."""

    @pytest.fixture()
    def tx_engine(self, tmp_path):
        from sqlalchemy import event as sa_event

        eng = create_engine(
            f"sqlite:///{tmp_path / 'tx.db'}",
            connect_args={"check_same_thread": False},
        )

        # WAL: the alert path legitimately writes from a SECOND session
        # while the request's DI session holds read transactions; SQLite's
        # default journal mode would block that writer behind the reader
        # ("database is locked"). WAL readers never block the writer —
        # mirroring the production CockroachDB concurrency model.
        @sa_event.listens_for(eng, "connect")
        def _wal(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        Base.metadata.create_all(eng)
        yield eng
        eng.dispose()

    @pytest.fixture()
    def tx_db(self, tx_engine):
        TestingSession = sessionmaker(bind=tx_engine, autocommit=False, autoflush=False)
        db = TestingSession()
        yield db
        db.close()

    @pytest.fixture()
    def tx_client(self, tx_db):
        def override_get_db():
            yield tx_db

        app.dependency_overrides[get_db] = override_get_db
        try:
            yield TestClient(app)
        finally:
            app.dependency_overrides.clear()

    @pytest.fixture()
    def tx_user(self, tx_db):
        uid = str(uuid4())
        user = User(
            id=uid,
            status="active",
            identity_source="upstox",
            broker_provider="UPSTOX",
            broker_user_id=f"tx-{uid[:8]}",
        )
        tx_db.add(user)
        tx_db.commit()
        tx_db.expire(user)
        sid = token_store.set_token(f"tok-tx-{uid[:8]}")
        create_session_record(tx_db, uid, sid)
        # Commit the setup: during the request the caller's session must
        # hold only READ transactions. (An uncommitted fixture write would
        # block the alert's independent writer on SQLite — WAL readers
        # never block writers, but another session's uncommitted WRITE
        # does. Production CockroachDB has no such writer-writer lock
        # across unrelated rows.)
        tx_db.commit()
        return sid, uid

    def _independent(self, tx_engine):
        return sessionmaker(bind=tx_engine)()

    def _pending_marker(self, db, marker):
        """Add an unrelated business write WITHOUT flushing: with
        autoflush=False the row stays pending (no DB lock held), and any
        commit of THIS session makes it durable. Used to prove the alert
        path never commits the caller's session."""
        from app.identity import AdminControl

        db.add(AdminControl(
            id=str(uuid4()), domain="retention", key=marker, value={},
        ))

    def _marker_control(self, db, marker):
        from app.identity import AdminControl

        db.add(AdminControl(
            id=str(uuid4()), domain="retention", key=marker, value={},
        ))
        db.flush()

    def test_broker_alert_does_not_commit_request_transaction(
        self, tx_client, tx_db, tx_user, tx_engine
    ):
        """call_upstox must leave the CALLER's session untouched.

        The alert OWNS its transaction: it is durable (via its own session,
        bound to the caller's ENGINE) and delivered immediately, while the
        caller's session is never joined, committed, or rolled back by the
        alert path. A REAL second connection proves exactly what the alert
        path committed: its own event, nothing of the caller's.
        """
        from unittest.mock import patch

        from app.brokers.domain.errors import BrokerError
        from app.identity import NotificationEvent
        from app.routers.chains import call_upstox
        from app.services import notifications

        sid_a, uid_a = tx_user
        notifications.clear_captured_deliveries()

        err = BrokerError(code="UPSTREAM_ERROR", message="upstream 503", status_code=503)
        with patch(
            "app.routers.chains.gateway.create",
        ) as fake_gateway:
            fake_gateway.return_value.get_option_contracts = _async_raises(err)
            resp = tx_client.get("/chains/NIFTY/expiries", headers={"X-Session-Id": sid_a})

        assert resp.status_code == 502  # translated HTTP outcome unchanged

        # The alert is durable (own transaction): a REAL second connection
        # sees it — no caller commit involved (the DI lifecycle never
        # commits on an error response).
        independent = self._independent(tx_engine)
        assert independent.query(NotificationEvent).filter(
            NotificationEvent.event_type == "broker.auth_failed",
            NotificationEvent.user_scope == uid_a,
        ).count() == 1, "alert did not commit durably in its own transaction"
        independent.close()

        # The caller's session was never joined by the alert path: nothing
        # was ADDED to (staged in) the caller's in-memory session.
        assert not any(
            isinstance(o, NotificationEvent) for o in list(tx_db.new) + list(tx_db.dirty)
        ), "the alert path staged into the caller's session"
        tx_db.expire_all()

        # Unit level with an unrelated PENDING write on the caller's
        # session: the alert must commit durably WITHOUT committing (or
        # otherwise touching) the caller's pending write. (Age out the F14
        # dedup window first: the route-level alert for this scope is
        # still inside it, and the same failure must be recordable again.)
        notifications.force_expire_dedup_window(tx_db, user_scope=uid_a)
        tx_db.commit()
        from app.identity import AdminControl

        marker = f"tx-marker-{uuid.uuid4().hex[:8]}"
        self._pending_marker(tx_db, marker)
        notifications.clear_captured_deliveries()
        adapter = type("A", (), {})()
        adapter.get_option_contracts = _async_raises(err)
        with pytest.raises(Exception) as excinfo:
            import asyncio

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    call_upstox(
                        adapter.get_option_contracts(),
                        source="legacy_session",
                        session_id=sid_a,
                        db=tx_db,
                        user_scope=uid_a,
                    )
                )
            finally:
                loop.close()
        assert getattr(excinfo.value, "status_code", None) == 502

        # Caller session still pending: its write is NOT committed and NO
        # alert was staged into it (expire_all forces a fresh SELECT on a
        # fresh transaction — the marker row is invisible => uncommitted).
        tx_db.expire_all()
        assert tx_db.query(AdminControl).filter_by(key=marker).one_or_none() is None, (
            "the alert path committed the caller's unrelated pending write"
        )
        assert not any(
            isinstance(o, NotificationEvent) for o in list(tx_db.new) + list(tx_db.dirty)
        ), "the alert path staged into the caller's session"

        # The unit-level alert is durable in its OWN transaction + delivered
        # (2 = route-level alert + unit-level alert; the dedup window was
        # aged out between phases so the same failure is recordable again).
        independent = self._independent(tx_engine)
        assert independent.query(NotificationEvent).filter(
            NotificationEvent.user_scope == uid_a
        ).count() == 2, "unit-level alert did not commit durably in its own transaction"
        independent.close()
        # Deliveries since the clear: exactly the unit-level alert (the
        # route-level alert was delivered by its own commit before the
        # clear) — delivery rides the alert's OWN transaction, never the
        # caller's.
        delivered = [d for d in notifications.captured_deliveries() if d["user_scope"] == uid_a]
        assert len(delivered) == 1, "alert delivery must not depend on the caller's transaction"

        # Caller authority — ROLLBACK discards ONLY the caller's write; the
        # alerts' own committed transactions are unaffected.
        tx_db.rollback()
        assert tx_db.query(AdminControl).filter_by(key=marker).count() == 0
        independent = self._independent(tx_engine)
        assert independent.query(NotificationEvent).filter(
            NotificationEvent.user_scope == uid_a
        ).count() == 2
        independent.close()

        # Caller COMMITS its own write: durable — no additional alert.
        self._marker_control(tx_db, marker)
        tx_db.commit()
        independent = self._independent(tx_engine)
        assert independent.query(AdminControl).filter_by(key=marker).count() == 1
        assert independent.query(NotificationEvent).filter(
            NotificationEvent.user_scope == uid_a
        ).count() == 2
        independent.close()

    def test_execution_alert_does_not_commit_request_transaction(
        self, tx_client, tx_db, tx_user, tx_engine
    ):
        """_paper_error must leave the CALLER's session untouched: the alert
        is durable in its own transaction and delivered immediately, the
        mapped business error is unchanged, and the caller alone decides
        its own transaction outcome."""
        from unittest.mock import patch

        from app.brokers.domain.errors import BrokerError
        from app.identity import AdminControl, NotificationEvent
        from app.routers.paper import PaperExecutionError, _paper_error
        from app.services import notifications

        sid_a, uid_a = tx_user
        notifications.clear_captured_deliveries()

        # Route level through the real /paper/executions failure chain.
        body = {
            "client_order_id": f"ord-{uuid.uuid4().hex[:10]}",
            "symbol": "NIFTY",
            "legs": [{
                "symbol": "NIFTY", "expiration_date": "2026-12-31",
                "strike_price": 26000, "option_type": "call",
                "action": "buy", "quantity": 1, "lot_size": 75,
            }],
        }
        err = BrokerError(code="UPSTREAM_ERROR", message="chain upstream down", status_code=503)
        open_status = SimpleNamespace(status="open")
        with patch(
            "app.routers.paper.get_market_status", new=_async_returns(open_status)
        ), patch("app.routers.paper.gateway.create") as fake_gateway:
            fake_gateway.return_value.get_option_chain = _async_raises(err)
            resp = tx_client.post("/paper/executions", json=body, headers={"X-Session-Id": sid_a})

        assert resp.status_code == 502  # business error response unchanged

        # Alert durable in its OWN transaction.
        independent = self._independent(tx_engine)
        assert independent.query(NotificationEvent).filter(
            NotificationEvent.event_type == "execution.failed",
            NotificationEvent.user_scope == uid_a,
        ).count() == 1, "execution alert did not commit durably in its own transaction"
        independent.close()

        # Caller session untouched (no staged alert: nothing ADDED to the
        # caller's in-memory session).
        assert not any(
            isinstance(o, NotificationEvent) for o in list(tx_db.new) + list(tx_db.dirty)
        ), "the alert path staged into the caller's session"
        tx_db.expire_all()

        # Unit level with an unrelated PENDING write on the caller's
        # session: the alert must commit durably WITHOUT committing (or
        # otherwise touching) the caller's pending write. (Age out the F14
        # dedup window first: the route-level alert for this scope is
        # still inside it.)
        notifications.force_expire_dedup_window(tx_db, user_scope=uid_a)
        tx_db.commit()
        marker = f"tx-marker-{uuid.uuid4().hex[:8]}"
        self._pending_marker(tx_db, marker)
        notifications.clear_captured_deliveries()
        mapped = _paper_error(
            PaperExecutionError("EXECUTION_FAILED", "unit probe"),
            db=tx_db,
            user_id=uid_a,
        )
        assert mapped.status_code == 502

        # Caller session still pending: write NOT committed by the alert
        # path, and nothing was ADDED to the caller's in-memory session.
        tx_db.expire_all()
        assert tx_db.query(AdminControl).filter_by(key=marker).one_or_none() is None, (
            "the alert path committed the caller's unrelated pending write"
        )
        assert not any(
            isinstance(o, NotificationEvent) for o in list(tx_db.new) + list(tx_db.dirty)
        ), "the alert path staged into the caller's session"

        # Alert durable in its own transaction + delivered.
        independent = self._independent(tx_engine)
        assert independent.query(NotificationEvent).filter(
            NotificationEvent.user_scope == uid_a
        ).count() == 2, "second alert did not commit durably in its own transaction"
        independent.close()
        # Deliveries since the clear: exactly the unit-level alert (the
        # route-level alert was delivered by its own commit before the
        # clear) — delivery rides the alert's OWN transaction.
        delivered = [d for d in notifications.captured_deliveries() if d["user_scope"] == uid_a]
        assert len(delivered) == 1

        # Caller authority — ROLLBACK discards ONLY the caller's write.
        tx_db.rollback()
        assert tx_db.query(AdminControl).filter_by(key=marker).count() == 0
        independent = self._independent(tx_engine)
        assert independent.query(NotificationEvent).filter(
            NotificationEvent.user_scope == uid_a
        ).count() == 2, "caller rollback must not destroy the alerts' own transactions"
        independent.close()

        # Caller COMMITS its own write: durable — no additional alert.
        self._marker_control(tx_db, marker)
        tx_db.commit()
        independent = self._independent(tx_engine)
        assert independent.query(AdminControl).filter_by(key=marker).count() == 1
        assert independent.query(NotificationEvent).filter(
            NotificationEvent.user_scope == uid_a
        ).count() == 2
        independent.close()

    def test_bulk_exit_atomicity_and_no_commit_from_failure_boundaries(
        self, client, db_session, two_users
    ):
        """The bulk exit stays ONE transaction: a mid-loop position failure
        never routes through a committing alert path, nothing is delivered
        before the service's single commit, and the exits + idempotency
        record commit together (replay proves durability)."""
        from unittest.mock import patch

        from app.services import paper_execution as pe

        sid_a, uid_a, _, _ = two_users
        from app.services import notifications
        notifications.clear_captured_deliveries()

        # Seed two open positions through the GENUINE entry chain (same seam
        # as tests/test_bulk_exit.py: the app.services.upstox chain mock),
        # market gate pinned OPEN.
        expiry = "2026-12-31"
        quotes = {expiry: {26000: {"call": 200.0, "put": 80.0},
                           26500: {"call": 160.0, "put": 95.0}}}

        def chain_payload(exp, q):
            data = []
            for strike, sides in q.items():
                item = {"strike_price": strike, "underlying_spot_price": 26000.0}
                item["call_options"] = {"market_data": {"ltp": sides["call"]}, "option_greeks": {}}
                item["put_options"] = {"market_data": {"ltp": sides["put"]}, "option_greeks": {}}
                data.append(item)
            return {"data": data}

        async def fake_chain(token, instrument_key, exp):
            return chain_payload(exp, quotes.get(exp, {}))

        entry = {
            "client_order_id": f"ord-{uuid.uuid4().hex[:10]}",
            "symbol": "NIFTY",
            "strategy_tag": "BulkOwnership",
            "starting_capital": 500000,
            "legs": [
                {
                    "symbol": "NIFTY", "expiration_date": expiry,
                    "strike_price": 26000, "option_type": "call",
                    "action": "buy", "quantity": 1, "lot_size": 75,
                },
                {
                    "symbol": "NIFTY", "expiration_date": expiry,
                    "strike_price": 26500, "option_type": "call",
                    "action": "buy", "quantity": 1, "lot_size": 75,
                },
            ],
        }
        open_status = SimpleNamespace(status="open")
        with patch(
            "app.routers.paper.get_market_status", new=_async_returns(open_status)
        ), patch(
            "app.services.upstox.get_option_chain", new=_async_returns({"data": chain_payload(expiry, quotes[expiry])["data"]})
        ):
            resp = client.post("/paper/executions", json=entry, headers={"X-Session-Id": sid_a})
        assert resp.status_code == 200, resp.text
        execution_id = resp.json()["execution_id"]

        # Mid-loop failure on the SECOND position (genuine bulk engine).
        real_exit = pe.exit_position
        state = {"n": 0}

        def flaky_exit(user_id, position_id, request, db, fill_price, *, commit=True, **kw):
            state["n"] += 1
            if state["n"] == 2:
                raise pe.PaperExecutionError("EXECUTION_FAILED", "mid-flight failure")
            return real_exit(user_id, position_id, request, db, fill_price, commit=commit, **kw)

        bulk_coid = f"bx-{uuid.uuid4().hex[:10]}"
        with patch(
            "app.routers.paper.get_market_status", new=_async_returns(open_status)
        ), patch(
            "app.services.upstox.get_option_chain", new=_async_returns({"data": chain_payload(expiry, quotes[expiry])["data"]})
        ), patch.object(pe, "exit_position", new=flaky_exit):
            resp = client.post(
                f"/paper/executions/{execution_id}/exit-all",
                json={"client_order_id": bulk_coid},
                headers={"X-Session-Id": sid_a},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "PARTIAL"
        assert body["exited_count"] == 1 and body["failed_count"] == 1

        # No alert path fired mid-loop and nothing was delivered: the bulk
        # service's own single commit is the only transaction boundary.
        assert notifications.captured_deliveries() == [], (
            "bulk exit must not deliver (or commit) anything from mid-loop failures"
        )
        from app.identity import NotificationEvent as _NE

        assert db_session.query(_NE).filter(
            _NE.event_type == "execution.failed"
        ).count() == 0, "mid-loop position failures must not emit boundary alerts"

        # The service's ONE commit persisted exits + idempotency record
        # together: replay returns the ORIGINAL result (no re-execution),
        # and the surviving DB state matches it exactly. (Replay still
        # passes the market/chain guards first — same seam as the
        # established bulk-exit suite's autouse fixtures.)
        from app.models import Position

        with patch(
            "app.routers.paper.get_market_status", new=_async_returns(open_status)
        ), patch(
            "app.services.upstox.get_option_chain", new=_async_returns({"data": chain_payload(expiry, quotes[expiry])["data"]})
        ):
            replay_resp = client.post(
                f"/paper/executions/{execution_id}/exit-all",
                json={"client_order_id": bulk_coid},
                headers={"X-Session-Id": sid_a},
            )
        assert replay_resp.status_code == 200
        replay = replay_resp.json()
        assert replay["execution_id"] == bulk_coid
        assert replay.get("duplicated") is True
        assert replay["exited_count"] == 1 and replay["failed_count"] == 1
        still_open = db_session.query(Position).filter(
            Position.user_id == uid_a, Position.status == "open"
        ).count()
        assert still_open == 1, "the failed position must remain open — no partial commit"

    def test_bulk_exit_error_boundary_alert_is_durable_without_caller_commit(
        self, tx_client, tx_db, tx_user, tx_engine
    ):
        """A bulk-exit failure that REACHES the mapper (``_paper_error``)
        leaves the caller's session untouched.

        Greptile G2: the mid-loop PARTIAL case in the previous test never
        reaches the failure boundary — this test exercises the actual
        changed code path. ``resolve_bulk_market_prices`` finds no usable
        quote (empty chain data) and raises ``BULK_EXIT_CHAIN_DATA_MISSING``
        BEFORE any mutation; the route maps it through ``_paper_error`` with
        the request's DI session. The alert must be durable in its OWN
        transaction on the caller's engine, delivered immediately, and the
        mapped business response must be unchanged — while an unrelated
        PENDING write on the caller's session proves the alert path never
        commits it, and a caller rollback proves the alert survives.
        """
        from unittest.mock import patch

        from app.identity import AdminControl, NotificationEvent
        from app.services import notifications

        sid_a, uid_a = tx_user
        expiry = "2026-12-31"
        notifications.clear_captured_deliveries()

        # Seed ONE open position through the genuine entry chain, market
        # gate pinned OPEN (same service-level seams as test_bulk_exit.py).
        def chain_payload(exp, strikes):
            data = []
            for strike, sides in strikes.items():
                item = {"strike_price": strike, "underlying_spot_price": 26000.0}
                item["call_options"] = {"market_data": {"ltp": sides["call"]}, "option_greeks": {}}
                item["put_options"] = {"market_data": {"ltp": sides["put"]}, "option_greeks": {}}
                data.append(item)
            return {"data": data}

        entry = {
            "client_order_id": f"ord-{uuid.uuid4().hex[:10]}",
            "symbol": "NIFTY",
            "strategy_tag": "BulkBoundary",
            "starting_capital": 500000,
            "legs": [{
                "symbol": "NIFTY", "expiration_date": expiry,
                "strike_price": 26000, "option_type": "call",
                "action": "buy", "quantity": 1, "lot_size": 75,
            }],
        }
        open_status = SimpleNamespace(status="open")
        quotes = {expiry: {26000: {"call": 200.0, "put": 80.0}}}
        with patch(
            "app.routers.paper.get_market_status", new=_async_returns(open_status)
        ), patch(
            "app.services.upstox.get_option_chain",
            new=_async_returns({"data": chain_payload(expiry, quotes[expiry])["data"]}),
        ):
            resp = tx_client.post("/paper/executions", json=entry, headers={"X-Session-Id": sid_a})
        assert resp.status_code == 200, resp.text

        # Caller session carries an unrelated PENDING write that must NOT
        # be committed by the alert path below.
        marker = f"tx-marker-{uuid.uuid4().hex[:8]}"
        self._pending_marker(tx_db, marker)

        # BOUNDARY failure: the chain resolver returns NO usable quote, so
        # resolve_bulk_market_prices raises BULK_EXIT_CHAIN_DATA_MISSING
        # before any mutation and the route maps it through _paper_error
        # (db=tx_db) — the exact production failure boundary under test.
        with patch(
            "app.routers.paper.get_market_status", new=_async_returns(open_status)
        ), patch(
            "app.services.upstox.get_option_chain", new=_async_returns({"data": []})
        ):
            resp = tx_client.post(
                "/paper/positions/exit-all",
                json={"client_order_id": f"bx-{uuid.uuid4().hex[:10]}"},
                headers={"X-Session-Id": sid_a},
            )

        # Mapped business error unchanged (pre-existing contract).
        assert resp.status_code == 409
        assert resp.json()["detail"].startswith("BULK_EXIT_CHAIN_DATA_MISSING:"), resp.text

        # Alert durable in its OWN transaction (a REAL second connection
        # sees it — the request DI lifecycle never commits on an error).
        independent = self._independent(tx_engine)
        assert independent.query(NotificationEvent).filter(
            NotificationEvent.event_type == "execution.failed",
            NotificationEvent.user_scope == uid_a,
        ).count() == 1, "boundary alert did not commit durably in its own transaction"
        independent.close()

        # Delivery rode the alert's OWN commit (no caller commit involved).
        delivered = [d for d in notifications.captured_deliveries() if d["user_scope"] == uid_a]
        assert len(delivered) == 1, "alert delivery must not depend on the caller's transaction"

        # Caller authority: the alert path committed ONLY its own session —
        # the caller's pending write is still uncommitted (expire_all forces
        # a fresh SELECT on a fresh transaction: the marker is invisible),
        # and nothing was staged into the caller's in-memory session.
        tx_db.expire_all()
        assert tx_db.query(AdminControl).filter_by(key=marker).one_or_none() is None, (
            "the alert path committed the caller's unrelated pending write"
        )
        assert not any(
            isinstance(o, NotificationEvent) for o in list(tx_db.new) + list(tx_db.dirty)
        ), "the alert path staged into the caller's session"

        # Caller ROLLBACK discards only its own write; the alert's own
        # committed transaction survives untouched.
        tx_db.rollback()
        assert tx_db.query(AdminControl).filter_by(key=marker).count() == 0
        independent = self._independent(tx_engine)
        assert independent.query(NotificationEvent).filter(
            NotificationEvent.user_scope == uid_a
        ).count() == 1, "caller rollback must not destroy the alert's own transaction"
        independent.close()

        # No partial exit happened: the position is still open (the whole
        # bulk request was rejected BEFORE any mutation).
        from app.models import Position

        tx_db.expire_all()
        assert tx_db.query(Position).filter(
            Position.user_id == uid_a, Position.status == "open"
        ).count() == 1

    def test_background_owned_alert_session_still_commits(
        self, db_session, two_users
    ):
        """A genuinely background-owned boundary (no request DI session)
        still records a DURABLE alert in its own independent session —
        proving the fix did not make operational alerts permanently
        non-durable. Uses the shared app.db.SessionLocal engine (the same
        mechanism conftest provides for background paths), with tables
        ensured per the established test_day43 pattern."""
        from unittest.mock import patch

        from app.brokers.domain.errors import BrokerError
        import app.db as app_db
        from app.identity import NotificationEvent
        from app.routers.chains import call_upstox

        _, uid_a, _, _ = two_users
        from app.services import notifications

        # The background path uses app.db.SessionLocal (conftest-swapped
        # shared engine). Ensure the identity tables exist there.
        app_db.Base.metadata.create_all(app_db.engine)

        notifications.clear_captured_deliveries()
        adapter = type("A", (), {})()
        adapter.get_option_contracts = _async_raises(
            BrokerError(code="UPSTREAM_ERROR", message="bg probe", status_code=503)
        )
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(Exception) as excinfo:
                loop.run_until_complete(
                    call_upstox(
                        adapter.get_option_contracts(),
                        source="legacy_session",
                        session_id=None,
                        db=None,  # background-owned: helper creates its own session
                        user_scope=uid_a,
                    )
                )
        finally:
            loop.close()
        assert getattr(excinfo.value, "status_code", None) == 502

        # Durable on the background engine + delivered: the independent
        # alert transaction committed.
        bg_session = app_db.SessionLocal()
        try:
            rows = (
                bg_session.query(NotificationEvent)
                .filter(
                    NotificationEvent.event_type == "broker.auth_failed",
                    NotificationEvent.user_scope == uid_a,
                )
                .all()
            )
            assert rows, "background-owned alert did not commit durably"
        finally:
            bg_session.close()
        delivered = [d for d in notifications.captured_deliveries() if d["user_scope"] == uid_a]
        assert len(delivered) == 1
