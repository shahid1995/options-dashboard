"""Day 47 — durable background job service tests (hermetic, SQLite).

Covers the Day 47 acceptance areas that do not require a real network
database: idempotent enqueue, lease-claim exclusivity, success/retry/
dead-letter transitions, bounded backoff math, restart recovery, the
worker loop, and the historical-ingestion execution verdicts.

Real PostgreSQL/CockroachDB concurrency and migration rehearsal live in
``tests/test_background_jobs_integration.py`` (skipped without
``TEST_DATABASE_URL`` / ``TEST_COCKROACHDB_URL``).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import BackgroundJob, JobStatus
from app.services import background_jobs as bj


def _utcnow():
    """Match the service's storage convention: naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)



@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    yield factory
    engine.dispose()


def _enqueue(db, key: str, **kwargs):
    return bj.enqueue(
        db,
        job_type=kwargs.pop("job_type", "HISTORICAL_INGESTION"),
        idempotency_key=key,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Job creation / enqueue idempotency
# ---------------------------------------------------------------------------


class TestEnqueueIdempotency:
    def test_first_enqueue_creates_pending_row(self, session_factory):
        db = session_factory()
        job, created = _enqueue(db, "backfill:all:2024-01-01", payload={"stages": ["contracts"]})
        assert created is True
        assert job.status == JobStatus.PENDING.value
        assert job.attempt_count == 0
        assert json.loads(job.payload) == {"stages": ["contracts"]}
        assert job.user_scope is None
        assert len(job.id) == 36

    def test_duplicate_enqueue_collapses_onto_same_row(self, session_factory):
        db = session_factory()
        job1, created1 = _enqueue(db, "backfill:all", payload={"stages": ["nifty"]})
        job2, created2 = _enqueue(db, "backfill:all", payload={"stages": ["nifty"]})
        assert created1 is True and created2 is False
        assert job1.id == job2.id
        total = db.scalar(select(func.count(BackgroundJob.id)))
        assert total == 1

    def test_different_keys_create_different_jobs(self, session_factory):
        db = session_factory()
        job1, _ = _enqueue(db, "backfill:all")
        job2, _ = _enqueue(db, "backfill:nifty-only")
        assert job1.id != job2.id

    def test_reenqueue_terminal_success_rearms_same_row(self, session_factory):
        db = session_factory()
        job, _ = _enqueue(db, "backfill:all")
        claimed = bj.claim_next(db, worker_id="w1")
        bj.complete_job(db, claimed)
        requeued, created = _enqueue(db, "backfill:all", payload={"stages": ["options"]})
        assert created is False
        assert requeued.id == job.id
        assert requeued.status == JobStatus.PENDING.value
        assert requeued.attempt_count == 0
        assert requeued.completed_at is None
        assert json.loads(requeued.payload) == {"stages": ["options"]}

    def test_reenqueue_dead_lettered_rearms_and_clears_failure(self, session_factory):
        db = session_factory()
        _enqueue(db, "backfill:all")
        job = bj.claim_next(db, worker_id="w1")
        bj.fail_job(db, job, bj.JobExecutionError("bad request shape", retryable=False))
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        assert row.status == JobStatus.DEAD_LETTERED.value

        requeued, created = _enqueue(db, "backfill:all")
        assert created is False
        assert requeued.status == JobStatus.PENDING.value
        assert requeued.dead_letter_reason is None
        assert requeued.last_error is None

    def test_reenqueue_live_running_job_is_noop(self, session_factory):
        db = session_factory()
        _enqueue(db, "backfill:all")
        job = bj.claim_next(db, worker_id="w1")
        requeued, created = _enqueue(db, "backfill:all", payload={"stages": ["other"]})
        assert created is False
        assert requeued.id == job.id
        assert requeued.status == JobStatus.RUNNING.value
        assert requeued.lease_owner == "w1"
        # live jobs are never silently re-armed or re-parameterized
        assert json.loads(requeued.payload) != {"stages": ["other"]}


# ---------------------------------------------------------------------------
# 2. Claiming
# ---------------------------------------------------------------------------


class TestClaiming:
    def test_claim_returns_none_when_queue_empty(self, session_factory):
        db = session_factory()
        assert bj.claim_next(db, worker_id="w1") is None

    def test_claim_sets_lease_and_counts_attempt(self, session_factory):
        db = session_factory()
        _enqueue(db, "job:1")
        before = _utcnow()
        job = bj.claim_next(db, worker_id="worker-a")
        assert job is not None
        assert job.status == JobStatus.RUNNING.value
        assert job.lease_owner == "worker-a"
        assert job.attempt_count == 1
        assert job.started_at is not None
        assert job.lease_expires_at is not None
        assert job.lease_expires_at > before

    def test_claim_orders_by_available_at(self, session_factory):
        db = session_factory()
        _enqueue(db, "job:older", available_at=datetime.now(timezone.utc) - timedelta(minutes=5))
        _enqueue(db, "job:newer")
        first = bj.claim_next(db, worker_id="w1")
        assert first.idempotency_key == "job:older"

    def test_claim_picks_up_ready_retries(self, session_factory):
        db = session_factory()
        _enqueue(db, "job:1")
        job = bj.claim_next(db, worker_id="w1")
        bj.fail_job(db, job, bj.JobExecutionError("transient", retryable=True))
        # backoff is in the future by default -> not claimable yet
        assert bj.claim_next(db, worker_id="w2") is None
        # after the backoff window passes, the retry is claimable
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        row.available_at = _utcnow() - timedelta(seconds=1)
        db.commit()
        again = bj.claim_next(db, worker_id="w2")
        assert again is not None
        assert again.attempt_count == 2
        assert again.lease_owner == "w2"

    def test_claim_does_not_pick_terminal_jobs(self, session_factory):
        db = session_factory()
        _enqueue(db, "job:1")
        job = bj.claim_next(db, worker_id="w1")
        bj.complete_job(db, job)
        assert bj.claim_next(db, worker_id="w2") is None

    def test_job_type_filter(self, session_factory):
        db = session_factory()
        _enqueue(db, "job:ingest", job_type="HISTORICAL_INGESTION")
        _enqueue(db, "job:other", job_type="SOMETHING_ELSE")
        job = bj.claim_next(db, worker_id="w1", job_type="HISTORICAL_INGESTION")
        assert job.idempotency_key == "job:ingest"


# ---------------------------------------------------------------------------
# 3. Concurrent claim protection (sequential interleaving; real concurrency
#    is proven on PostgreSQL/CockroachDB in the integration module)
# ---------------------------------------------------------------------------


class TestConcurrentClaimProtection:
    def test_second_worker_cannot_claim_running_job(self, session_factory):
        db1 = session_factory()
        db2 = session_factory()
        _enqueue(db1, "job:1")
        winner = bj.claim_next(db1, worker_id="w1")
        assert winner is not None
        loser = bj.claim_next(db2, worker_id="w2")
        assert loser is None
        db2.expire_all()
        row = db2.scalar(select(BackgroundJob))
        assert row.lease_owner == "w1"
        assert row.attempt_count == 1

    def test_expired_lease_allows_takeover_by_new_worker(self, session_factory):
        db1 = session_factory()
        _enqueue(db1, "job:1")
        job = bj.claim_next(db1, worker_id="w1", lease_seconds=1)
        # simulate a crashed worker: its lease expires with the job stuck RUNNING
        db1.expire_all()
        row = db1.scalar(select(BackgroundJob))
        row.lease_expires_at = _utcnow() - timedelta(seconds=1)
        db1.commit()

        db2 = session_factory()
        takeover = bj.claim_next(db2, worker_id="w2")
        assert takeover is not None
        assert takeover.lease_owner == "w2"
        assert takeover.attempt_count == 2
        db2.expire_all()
        row = db2.scalar(select(BackgroundJob))
        assert row.status == JobStatus.RUNNING.value
        assert row.lease_owner == "w2"


# ---------------------------------------------------------------------------
# 4. Success / failure transitions
# ---------------------------------------------------------------------------


class TestTransitions:
    def test_complete_job(self, session_factory):
        db = session_factory()
        _enqueue(db, "job:1")
        job = bj.claim_next(db, worker_id="w1")
        job.last_error = None
        bj.complete_job(db, job)
        assert job.status == JobStatus.SUCCEEDED.value
        assert job.completed_at is not None
        assert job.lease_owner is None
        assert job.lease_expires_at is None

    def test_retryable_failure_schedules_backoff(self, session_factory):
        db = session_factory()
        _enqueue(db, "job:1")
        job = bj.claim_next(db, worker_id="w1")
        out = bj.fail_job(db, job, bj.JobExecutionError("connection reset", retryable=True))
        assert out.status == JobStatus.FAILED_RETRYABLE.value
        assert out.lease_owner is None
        assert out.attempt_count == 1
        assert "connection reset" in out.last_error
        delta = (out.available_at - _utcnow()).total_seconds()
        assert 25 <= delta <= 35  # base backoff 30s for attempt 1

    def test_non_retryable_failure_dead_letters(self, session_factory):
        db = session_factory()
        _enqueue(db, "job:1")
        job = bj.claim_next(db, worker_id="w1")
        out = bj.fail_job(db, job, bj.JobExecutionError("malformed payload", retryable=False))
        assert out.status == JobStatus.DEAD_LETTERED.value
        assert "non-retryable" in out.dead_letter_reason
        assert out.completed_at is not None
        assert out.lease_owner is None

    def test_plain_string_failure_is_non_retryable(self, session_factory):
        db = session_factory()
        _enqueue(db, "job:1")
        job = bj.claim_next(db, worker_id="w1")
        out = bj.fail_job(db, job, "deterministic bug")
        assert out.status == JobStatus.DEAD_LETTERED.value

    def test_unknown_exception_is_non_retryable_by_default(self, session_factory):
        db = session_factory()
        _enqueue(db, "job:1")
        job = bj.claim_next(db, worker_id="w1")
        out = bj.fail_job(db, job, ValueError("unexpected"))
        assert out.status == JobStatus.DEAD_LETTERED.value

    def test_max_attempts_dead_letters_at_claim_time(self, session_factory):
        db = session_factory()
        _enqueue(db, "job:1", payload={"policy": {"max_attempts": 2}})
        # attempt 1: fail retryably
        job = bj.claim_next(db, worker_id="w1")
        assert job.attempt_count == 1
        bj.fail_job(db, job, bj.JobExecutionError("transient", retryable=True))
        # force the backoff window to pass
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        row.available_at = _utcnow() - timedelta(seconds=1)
        db.commit()
        # attempt 2: fail retryably again -> budget now exhausted
        job = bj.claim_next(db, worker_id="w2")
        assert job.attempt_count == 2
        bj.fail_job(db, job, bj.JobExecutionError("transient again", retryable=True))
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        row.available_at = _utcnow() - timedelta(seconds=1)
        db.commit()
        # attempt 3 is refused: claim dead-letters instead of executing
        result = bj.claim_next(db, worker_id="w3")
        assert result is None
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        assert row.status == JobStatus.DEAD_LETTERED.value
        assert "max attempts (2) exhausted" in row.dead_letter_reason
        assert row.lease_owner is None


# ---------------------------------------------------------------------------
# 5. Retry policy math
# ---------------------------------------------------------------------------


class TestBackoffAndPolicy:
    def test_backoff_doubles_and_caps(self):
        policy = bj.JobPolicy(backoff_base_seconds=30.0, backoff_cap_seconds=120.0)
        assert bj.backoff_delay_seconds(policy, 1) == 30.0
        assert bj.backoff_delay_seconds(policy, 2) == 60.0
        assert bj.backoff_delay_seconds(policy, 3) == 120.0
        assert bj.backoff_delay_seconds(policy, 10) == 120.0

    def test_policy_from_payload(self):
        assert bj.policy_from_payload(None) == bj.JobPolicy()
        assert bj.policy_from_payload("not json") == bj.JobPolicy()
        policy = bj.policy_from_payload(
            json.dumps({"policy": {"max_attempts": 7, "lease_seconds": 60}})
        )
        assert policy.max_attempts == 7
        assert policy.lease_seconds == 60

    def test_policy_max_attempts_floor(self):
        policy = bj.policy_from_payload(json.dumps({"policy": {"max_attempts": 0}}))
        assert policy.max_attempts == 1


class TestRetryabilityClassification:
    def test_explicit_verdict_wins(self):
        assert bj.is_retryable_failure(bj.JobExecutionError("x", retryable=True)) is True
        assert bj.is_retryable_failure(bj.JobExecutionError("x", retryable=False)) is False

    def test_serialization_sqlstate_is_retryable(self):
        err = Exception("conflict")
        err.sqlstate = "40001"
        assert bj.is_retryable_failure(err) is True

    def test_transient_markers_are_retryable(self):
        for message in ("deadlock detected", "connection reset by peer", "could not serialize"):
            assert bj.is_retryable_failure(Exception(message)) is True

    def test_unknown_error_is_not_retryable(self):
        assert bj.is_retryable_failure(Exception("something happened")) is False

    def test_authentication_error_is_not_retryable(self):
        class UpstoxAuthenticationError(Exception):
            pass

        assert bj.is_retryable_failure(UpstoxAuthenticationError("token expired")) is False


# ---------------------------------------------------------------------------
# 6. Worker loop
# ---------------------------------------------------------------------------


class TestWorkerLoop:
    def test_once_drains_queue_and_records_summary(self, session_factory, monkeypatch):
        db = session_factory()
        for key in ("job:1", "job:2", "job:3"):
            _enqueue(db, key)
        db.close()

        executed = []

        def fake_execute(db, job):
            executed.append(job.idempotency_key)
            return {"ok": True}

        monkeypatch.setattr(bj, "execute_job", fake_execute)
        summary = bj.run_worker(session_factory=session_factory, once=True)
        assert executed == ["job:1", "job:2", "job:3"]
        assert summary == {"claimed": 3, "succeeded": 3, "failed": 0, "dead_lettered": 0}
        db = session_factory()
        statuses = {
            j.idempotency_key: j.status
            for j in db.execute(select(BackgroundJob)).scalars()
        }
        assert set(statuses.values()) == {JobStatus.SUCCEEDED.value}

    def test_retryable_failure_is_rescheduled_then_dead_lettered_at_budget(
        self, session_factory, monkeypatch
    ):
        _enqueue(
            session_factory(),
            "job:flaky",
            payload={"policy": {"max_attempts": 2, "backoff_base_seconds": 0.0}},
        )

        calls = {"n": 0}

        def fake_execute(db, job):
            calls["n"] += 1
            raise bj.JobExecutionError("transient", retryable=True)

        monkeypatch.setattr(bj, "execute_job", fake_execute)
        summary = bj.run_worker(session_factory=session_factory, once=True)
        # attempt 1 executes and is rescheduled (backoff 0 => immediately
        # available); attempt 2 executes; the third claim refuses to start
        # the job and dead-letters it at claim time (no third execution).
        assert calls["n"] == 2
        assert summary["claimed"] == 2
        assert summary["failed"] == 2
        # budget-exhaustion dead-letters happen during claiming; the row is
        # the authoritative inspectable evidence (plus the worker warning log)
        db = session_factory()
        row = db.scalar(select(BackgroundJob))
        assert row.status == JobStatus.DEAD_LETTERED.value
        assert "max attempts (2) exhausted" in row.dead_letter_reason
        assert row.lease_owner is None

    def test_non_retryable_failure_dead_letters_immediately(
        self, session_factory, monkeypatch
    ):
        _enqueue(session_factory(), "job:bad")

        def fake_execute(db, job):
            raise bj.JobExecutionError("malformed", retryable=False)

        monkeypatch.setattr(bj, "execute_job", fake_execute)
        summary = bj.run_worker(session_factory=session_factory, once=True)
        assert summary["dead_lettered"] == 1
        assert summary["succeeded"] == 0


# ---------------------------------------------------------------------------
# 7. Restart / durability semantics
# ---------------------------------------------------------------------------


class TestRestartRecovery:
    def test_jobs_survive_session_and_engine_restart(self, tmp_path):
        db_path = tmp_path / "jobs.db"
        url = f"sqlite:///{db_path}"

        def make_factory():
            engine = create_engine(url, connect_args={"check_same_thread": False})
            Base.metadata.create_all(bind=engine)
            return sessionmaker(bind=engine, autocommit=False, autoflush=False), engine

        factory1, engine1 = make_factory()
        db = factory1()
        _enqueue(db, "survivor:1")
        _enqueue(db, "survivor:2")
        job = bj.claim_next(db, worker_id="worker-crashed", lease_seconds=1)
        db.close()
        engine1.dispose()  # simulate the worker process dying
        # Lease expiry must already be in the past so the new worker's claim
        # is deterministic (no wall-clock waiting in the test).
        factory1b, _ = make_factory()
        db1b = factory1b()
        row = db1b.scalar(select(BackgroundJob).where(BackgroundJob.idempotency_key == "survivor:1"))
        row.lease_expires_at = _utcnow() - timedelta(seconds=1)
        db1b.commit()
        db1b.close()

        factory2, engine2 = make_factory()  # brand-new "process"
        db2 = factory2()
        rows = {
            r.idempotency_key: r.status
            for r in db2.execute(select(BackgroundJob)).scalars()
        }
        assert rows == {
            "survivor:1": JobStatus.RUNNING.value,  # claimed before the crash
            "survivor:2": JobStatus.PENDING.value,  # never claimed
        }
        # the new worker recovers the abandoned job and runs the pending one
        recovered = bj.claim_next(db2, worker_id="worker-new")
        assert recovered.idempotency_key == "survivor:1"
        assert recovered.attempt_count == 2
        engine2.dispose()


# ---------------------------------------------------------------------------
# 8. Historical-ingestion execution boundary
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, status="SUCCESS", errors=None):
        self.operation = "backfill_all"
        self.status = status
        self.api_calls = 1
        self.rows_fetched = 2
        self.rows_inserted = 3
        self.rows_skipped = 0
        self.errors = errors or []


class _FakeOrchestrator:
    last_instance = None

    def __init__(self, db, client, *, force=False, rate_limiter=None):
        self.db = db
        self.client = client
        self.force = force
        self.rate_limiter = rate_limiter
        _FakeOrchestrator.last_instance = self

    async def run_all(self, *, stages=None, nifty_start_date=None):
        self.stages = stages
        self.nifty_start_date = nifty_start_date
        return self._result


class TestHistoricalIngestionExecution:
    def _patch_dependencies(self, monkeypatch, fake):
        import app.services.backfill_orchestrator as orch_mod
        import app.services.upstox_client as client_mod

        class _FakeTokenBridge:
            pass

        class _FakeClient:
            def __init__(self, token_provider=None):
                self.token_provider = token_provider

        monkeypatch.setattr(orch_mod, "BackfillOrchestrator", fake)
        monkeypatch.setattr(orch_mod, "TokenBridge", _FakeTokenBridge)
        monkeypatch.setattr(client_mod, "UpstoxClient", _FakeClient)

    def test_success_returns_summary(self, session_factory, monkeypatch):
        fake = type("FakeOK", (_FakeOrchestrator,), {"_result": _FakeResult()})
        self._patch_dependencies(monkeypatch, fake)
        db = session_factory()
        job, _ = _enqueue(db, "job:1", payload={"stages": ["contracts", "nifty"]})
        summary = bj.execute_historical_ingestion(db, job)
        assert summary["status"] == "SUCCESS"
        assert summary["rows_inserted"] == 3
        assert _FakeOrchestrator.last_instance.stages == ["contracts", "nifty"]

    def test_start_date_and_force_are_forwarded(self, session_factory, monkeypatch):
        fake = type("FakeOK", (_FakeOrchestrator,), {"_result": _FakeResult()})
        self._patch_dependencies(monkeypatch, fake)
        db = session_factory()
        job, _ = _enqueue(
            db,
            "job:1",
            payload={"stages": ["nifty"], "nifty_start_date": "2024-01-01", "force": True},
        )
        bj.execute_historical_ingestion(db, job)
        assert str(_FakeOrchestrator.last_instance.nifty_start_date) == "2024-01-01"
        assert _FakeOrchestrator.last_instance.force is True

    def test_auth_failure_is_non_retryable(self, session_factory, monkeypatch):
        fake = type(
            "FakeAuthFail",
            (_FakeOrchestrator,),
            {"_result": _FakeResult(status="FAILED", errors=["Authentication failed. Please re-authenticate."])},
        )
        self._patch_dependencies(monkeypatch, fake)
        db = session_factory()
        job, _ = _enqueue(db, "job:1")
        with pytest.raises(bj.JobExecutionError) as excinfo:
            bj.execute_historical_ingestion(db, job)
        assert excinfo.value.retryable is False

    def test_partial_failure_is_retryable(self, session_factory, monkeypatch):
        fake = type(
            "FakePartial",
            (_FakeOrchestrator,),
            {"_result": _FakeResult(status="PARTIAL", errors=["some chunk failed"])},
        )
        self._patch_dependencies(monkeypatch, fake)
        db = session_factory()
        job, _ = _enqueue(db, "job:1")
        with pytest.raises(bj.JobExecutionError) as excinfo:
            bj.execute_historical_ingestion(db, job)
        assert excinfo.value.retryable is True

    def test_unknown_job_type_is_non_retryable(self, session_factory):
        db = session_factory()
        job, _ = _enqueue(db, "job:1", job_type="NOT_A_REAL_TYPE")
        with pytest.raises(bj.JobExecutionError) as excinfo:
            bj.execute_job(db, job)
        assert excinfo.value.retryable is False


# ---------------------------------------------------------------------------
# 9. Reviewer remediation evidence
# ---------------------------------------------------------------------------


class TestCliSchemaAuthority:
    """F1: the operational CLI must never create or mutate schema.

    Alembic is the sole schema authority (ADR-002). The CLI session factory
    connects and assumes the migrated schema; on an uninitialized database
    it must fail with a normal database error, not silently create tables.
    """

    def test_session_factory_never_calls_create_all(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            Base.metadata,
            "create_all",
            lambda **kwargs: calls.append(kwargs),
        )
        import run_jobs

        factory = run_jobs._get_session_factory()
        assert calls == [], "CLI must not issue DDL through create_all"
        # The factory is a working sessionmaker bound to a real engine.
        assert factory.kw["bind"] is not None

    def test_uninitialized_sqlite_database_fails_closed(self, tmp_path, monkeypatch):
        import run_jobs

        db_file = tmp_path / "unmigrated.db"
        monkeypatch.setattr(
            run_jobs.settings, "DATABASE_URL", f"sqlite:///{db_file}", raising=False
        )
        factory = run_jobs._get_session_factory()
        db = factory()
        from sqlalchemy import select

        with pytest.raises(Exception) as excinfo:
            db.execute(select(BackgroundJob)).scalars().first()
        # no-such-table style failure — NOT automatic schema creation
        assert "background_jobs" in str(excinfo.value)
        db.close()


class TestJobDatetimeSemantics:
    """F2: one explicit datetime representation for Day 47 scheduling.

    Model defaults and service-generated timestamps must be compatible
    WITHOUT implicit timezone stripping at comparison sites.
    """

    def test_model_defaults_are_naive_utc(self, session_factory):
        db = session_factory()
        job = BackgroundJob(
            id="dt-1", job_type="HISTORICAL_INGESTION", idempotency_key="dt:1"
        )
        db.add(job)
        db.flush()  # Python-side defaults applied
        assert job.available_at.tzinfo is None
        assert job.created_at.tzinfo is None
        assert job.updated_at.tzinfo is None

    def test_service_transitions_are_naive_and_mutually_comparable(
        self, session_factory
    ):
        db = session_factory()
        bj.enqueue(db, job_type="HISTORICAL_INGESTION", idempotency_key="dt:2")
        job = bj.claim_next(db, worker_id="w1")
        # started_at / lease_expires_at come from the service's naive clock
        assert job.started_at.tzinfo is None
        assert job.lease_expires_at.tzinfo is None
        # direct comparison with the service clock: no TypeError possible
        assert job.lease_expires_at > bj._utcnow()
        bj.complete_job(db, job)
        assert job.completed_at.tzinfo is None
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        # model default (created_at) and service value (started_at) compare
        assert row.created_at <= row.started_at

    def test_retry_scheduling_stays_in_the_same_representation(
        self, session_factory
    ):
        db = session_factory()
        bj.enqueue(db, job_type="HISTORICAL_INGESTION", idempotency_key="dt:3")
        job = bj.claim_next(db, worker_id="w1")
        out = bj.fail_job(db, job, bj.JobExecutionError("transient", retryable=True))
        assert out.available_at.tzinfo is None
        assert out.available_at > bj._utcnow()  # pure naive-vs-naive comparison


class TestScratchIdentifierValidation:
    """F5: database identifiers are validated before DDL interpolation."""

    def test_rejects_unsafe_identifiers(self):
        from tests.test_background_jobs_integration import _validate_identifier

        for unsafe in (
            "bad; DROP TABLE users",  # statement injection
            "db-name",  # dash not allowed
            "db.name",  # dot not allowed
            "db name",  # whitespace
            "1leadingdigit",  # must not start with a digit
            "a" * 64,  # exceeds 63-byte PG/CRDB identifier limit
            'quote"x',  # double quote (identifier escape char)
        ):
            with pytest.raises(ValueError):
                _validate_identifier(unsafe)

    def test_accepts_safe_identifiers(self):
        from tests.test_background_jobs_integration import _validate_identifier

        for safe in ("jobs_queue_test", "strikenova_jobs_mig", "_leading", "A9_"):
            assert _validate_identifier(safe) == safe


class TestWorkerRollbackFailureLogging:
    """F3: rollback failure is logged, never swallowed silently."""

    def test_rollback_failure_logs_warning_and_loop_survives(
        self, session_factory, monkeypatch, caplog
    ):
        import logging

        from sqlalchemy.exc import SQLAlchemyError
        from sqlalchemy.orm import Session

        bj.enqueue(session_factory(), job_type="HISTORICAL_INGESTION", idempotency_key="rb:1")

        def boom(self):
            raise SQLAlchemyError("rollback itself failed")

        monkeypatch.setattr(Session, "rollback", boom)
        monkeypatch.setattr(
            bj,
            "execute_job",
            lambda db, job: (_ for _ in ()).throw(RuntimeError("executor exploded")),
        )

        def fake_fail_job(db, job, exc):
            # invoked AFTER the (failed) rollback; records the transition
            return type("Stub", (), {"id": job.id, "status": "FAILED_RETRYABLE"})()

        monkeypatch.setattr(bj, "fail_job", fake_fail_job)

        with caplog.at_level(logging.WARNING):
            summary = bj.run_worker(session_factory=session_factory, once=True)

        assert summary["failed"] == 1  # the loop survived the rollback error
        rollback_warnings = [
            r for r in caplog.records if "rollback" in r.getMessage().lower()
        ]
        assert rollback_warnings, "rollback failure must be logged"
        assert rollback_warnings[0].exc_info is not None  # exception info attached
