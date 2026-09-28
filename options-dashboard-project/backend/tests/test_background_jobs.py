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
from app.models import BackgroundJob, ContractSpec, DataCompleteness, HistoricalDatasetGovernance, HistoricalIngestionRun, JobStatus
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
    seed = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    seed.add_all(
        [
            HistoricalDatasetGovernance(
                dataset_key="UPSTOX_OPTION_CANDLES_3MIN",
                domain="MARKET_DATA",
                dataset_tier="RAW",
                table_name="option_candles",
                pipeline="backfill_options",
                completeness_data_type="option_candles",
                source="UPSTOX",
                source_reference="test",
                source_version="test",
                entitlement_requirement="TEST",
                entitlement_status="REVIEW_REQUIRED",
                license_status="REVIEW_REQUIRED",
                usage_policy="INTERNAL_ONLY",
                redistribution_status="REVIEW_REQUIRED",
                retention_policy="KEEP",
                raw_immutable=True,
                recomputable=True,
                dependencies_json="[]",
            ),
            HistoricalDatasetGovernance(
                dataset_key="UPSTOX_NIFTY_CANDLES_3MIN",
                domain="MARKET_DATA",
                dataset_tier="RAW",
                table_name="nifty_candles",
                pipeline="backfill_nifty",
                completeness_data_type="nifty_candles",
                source="UPSTOX",
                source_reference="test",
                source_version="test",
                entitlement_requirement="TEST",
                entitlement_status="REVIEW_REQUIRED",
                license_status="REVIEW_REQUIRED",
                usage_policy="INTERNAL_ONLY",
                redistribution_status="REVIEW_REQUIRED",
                retention_policy="KEEP",
                raw_immutable=True,
                recomputable=True,
                dependencies_json="[]",
            ),
            HistoricalDatasetGovernance(
                dataset_key="UPSTOX_CONTRACT_SPECS",
                domain="MARKET_DATA",
                dataset_tier="RAW",
                table_name="contract_specs",
                pipeline="backfill_contracts",
                completeness_data_type="contract_metadata",
                source="UPSTOX",
                source_reference="test",
                source_version="test",
                entitlement_requirement="TEST",
                entitlement_status="REVIEW_REQUIRED",
                license_status="REVIEW_REQUIRED",
                usage_policy="INTERNAL_ONLY",
                redistribution_status="REVIEW_REQUIRED",
                retention_policy="KEEP",
                raw_immutable=True,
                recomputable=True,
                dependencies_json="[]",
            ),
        ]
    )
    seed.commit()
    seed.close()
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
        assert bj.complete_job(db, claimed, worker_id="w1") is True
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
        assert bj.fail_job(db, job, bj.JobExecutionError("bad request shape", retryable=False), worker_id="w1") == bj.FAIL_DEAD_LETTERED
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
        assert bj.fail_job(db, job, bj.JobExecutionError("transient", retryable=True), worker_id="w1") == bj.FAIL_RETRIED
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
        assert bj.complete_job(db, job, worker_id="w1") is True
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
        assert bj.complete_job(db, job, worker_id="w1") is True
        assert job.status == JobStatus.SUCCEEDED.value
        assert job.completed_at is not None
        assert job.lease_owner is None
        assert job.lease_expires_at is None

    def test_retryable_failure_schedules_backoff(self, session_factory):
        db = session_factory()
        _enqueue(db, "job:1")
        job = bj.claim_next(db, worker_id="w1")
        out = bj.fail_job(db, job, bj.JobExecutionError("connection reset", retryable=True), worker_id="w1")
        assert out == bj.FAIL_RETRIED
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        assert row.status == JobStatus.FAILED_RETRYABLE.value
        assert row.lease_owner is None
        assert row.attempt_count == 1
        assert "connection reset" in row.last_error
        delta = (row.available_at - _utcnow()).total_seconds()
        assert 25 <= delta <= 35  # base backoff 30s for attempt 1

    def test_non_retryable_failure_dead_letters(self, session_factory):
        db = session_factory()
        _enqueue(db, "job:1")
        job = bj.claim_next(db, worker_id="w1")
        out = bj.fail_job(db, job, bj.JobExecutionError("malformed payload", retryable=False), worker_id="w1")
        assert out == bj.FAIL_DEAD_LETTERED
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        assert row.status == JobStatus.DEAD_LETTERED.value
        assert "non-retryable" in row.dead_letter_reason
        assert row.completed_at is not None
        assert row.lease_owner is None

    def test_plain_string_failure_is_non_retryable(self, session_factory):
        db = session_factory()
        _enqueue(db, "job:1")
        job = bj.claim_next(db, worker_id="w1")
        out = bj.fail_job(db, job, "deterministic bug", worker_id="w1")
        assert out == bj.FAIL_DEAD_LETTERED

    def test_unknown_exception_is_non_retryable_by_default(self, session_factory):
        db = session_factory()
        _enqueue(db, "job:1")
        job = bj.claim_next(db, worker_id="w1")
        out = bj.fail_job(db, job, ValueError("unexpected"), worker_id="w1")
        assert out == bj.FAIL_DEAD_LETTERED

    def test_max_attempts_dead_letters_at_claim_time(self, session_factory):
        db = session_factory()
        _enqueue(db, "job:1", payload={"policy": {"max_attempts": 2}})
        # attempt 1: fail retryably
        job = bj.claim_next(db, worker_id="w1")
        assert job.attempt_count == 1
        assert bj.fail_job(db, job, bj.JobExecutionError("transient", retryable=True), worker_id="w1") == bj.FAIL_RETRIED
        # force the backoff window to pass
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        row.available_at = _utcnow() - timedelta(seconds=1)
        db.commit()
        # attempt 2: fail retryably again -> budget now exhausted
        job = bj.claim_next(db, worker_id="w2")
        assert job.attempt_count == 2
        assert bj.fail_job(db, job, bj.JobExecutionError("transient again", retryable=True), worker_id="w2") == bj.FAIL_RETRIED
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

        def fake_execute(db, job, **kwargs):
            executed.append(job.idempotency_key)
            return {"ok": True}

        monkeypatch.setattr(bj, "execute_job", fake_execute)
        summary = bj.run_worker(session_factory=session_factory, once=True)
        assert executed == ["job:1", "job:2", "job:3"]
        assert summary == {"claimed": 3, "succeeded": 3, "failed": 0, "dead_lettered": 0, "stale": 0}
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

        def fake_execute(db, job, **kwargs):
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

        def fake_execute(db, job, **kwargs):
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
        self.metadata = {}


class _FakeOrchestrator:
    last_instance = None

    def __init__(self, db, client, *, force=False, rate_limiter=None):
        self.db = db
        self.client = client
        self.force = force
        self.rate_limiter = rate_limiter
        _FakeOrchestrator.last_instance = self

    async def run_all(self, *, stages=None, nifty_start_date=None, options_concurrency=None):
        self.stages = stages
        self.nifty_start_date = nifty_start_date
        self.options_concurrency = options_concurrency
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
        job, _ = _enqueue(db, "job:1", payload={"stages": ["contracts"]})
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
        job, _ = _enqueue(db, "job:1", payload={"stages": ["contracts"]})
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
        from sqlalchemy.exc import OperationalError

        # Assert the SPECIFIC missing-table failure mode: a normal database
        # error, NOT automatic schema creation and NOT an unrelated exception
        # whose message merely contains the table name.
        with pytest.raises(OperationalError) as excinfo:
            db.execute(select(BackgroundJob)).scalars().first()
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
        assert bj.complete_job(db, job, worker_id="w1") is True
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
        out = bj.fail_job(db, job, bj.JobExecutionError("transient", retryable=True), worker_id="w1")
        assert out == bj.FAIL_RETRIED
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        assert row.available_at.tzinfo is None
        assert row.available_at > bj._utcnow()  # pure naive-vs-naive comparison


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
            lambda db, job, **kwargs: (_ for _ in ()).throw(RuntimeError("executor exploded")),
        )

        def fake_fail_job(db, job, exc, *, worker_id, attempt_count=None):
            # invoked AFTER the (failed) rollback; records the transition
            return bj.FAIL_RETRIED

        monkeypatch.setattr(bj, "fail_job", fake_fail_job)

        with caplog.at_level(logging.WARNING):
            summary = bj.run_worker(session_factory=session_factory, once=True)

        assert summary["failed"] == 1  # the loop survived the rollback error
        rollback_warnings = [
            r for r in caplog.records if "rollback" in r.getMessage().lower()
        ]
        assert rollback_warnings, "rollback failure must be logged"
        assert rollback_warnings[0].exc_info is not None  # exception info attached


# ---------------------------------------------------------------------------
# 10. Qodo round-2 remediation (F1-F9)


class TestStaleWorkerOwnershipProtection:
    """F1/F10: a stale worker cannot mutate a replacement worker's attempt."""

    def _stale_scenario(self, session_factory):
        """worker A claims -> lease expires -> worker B reclaims."""
        dbA = session_factory()
        bj.enqueue(dbA, job_type="HISTORICAL_INGESTION", idempotency_key="stale:1")
        jobA = bj.claim_next(dbA, worker_id="worker-A", lease_seconds=1)
        assert jobA is not None
        # Simulate the lease expiring while A is still "executing".
        dbA.expire_all()
        row = dbA.scalar(select(BackgroundJob))
        row.lease_expires_at = _utcnow() - timedelta(seconds=1)
        dbA.commit()
        # Worker B reclaims the same row (attempt 2).
        dbB = session_factory()
        jobB = bj.claim_next(dbB, worker_id="worker-B")
        assert jobB is not None
        assert jobB.attempt_count == 2
        return jobA.id, jobB.id

    def test_stale_worker_cannot_complete_replacement_attempt(self, session_factory):
        jobA_id, _jobB_id = self._stale_scenario(session_factory)
        dbA = session_factory()
        fresh = dbA.get(BackgroundJob, jobA_id)
        assert bj.complete_job(dbA, fresh, worker_id="worker-A") is False
        dbA.expire_all()
        row = dbA.scalar(select(BackgroundJob))
        assert row.status == JobStatus.RUNNING.value
        assert row.lease_owner == "worker-B"
        assert row.attempt_count == 2
        assert row.completed_at is None

    def test_stale_worker_cannot_fail_replacement_attempt(self, session_factory):
        jobA_id, _jobB_id = self._stale_scenario(session_factory)
        dbA = session_factory()
        fresh = dbA.get(BackgroundJob, jobA_id)
        outcome = bj.fail_job(
            dbA,
            fresh,
            bj.JobExecutionError("transient", retryable=True),
            worker_id="worker-A",
        )
        assert outcome == bj.FAIL_STALE
        dbA.expire_all()
        row = dbA.scalar(select(BackgroundJob))
        assert row.status == JobStatus.RUNNING.value
        assert row.lease_owner == "worker-B"
        assert row.last_error is None
        assert row.dead_letter_reason is None

    def test_current_owner_still_completes_normally(self, session_factory):
        db = session_factory()
        bj.enqueue(db, job_type="HISTORICAL_INGESTION", idempotency_key="own:1")
        job = bj.claim_next(db, worker_id="worker-A")
        assert bj.complete_job(db, job, worker_id="worker-A") is True
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        assert row.status == JobStatus.SUCCEEDED.value

    def test_expired_lease_blocks_owner_transition(self, session_factory):
        db = session_factory()
        bj.enqueue(db, job_type="HISTORICAL_INGESTION", idempotency_key="own:2")
        job = bj.claim_next(db, worker_id="worker-A", lease_seconds=1)
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        row.lease_expires_at = _utcnow() - timedelta(seconds=1)
        db.commit()
        assert bj.complete_job(db, job, worker_id="worker-A") is False
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        assert row.status == JobStatus.RUNNING.value

    def test_wrong_worker_id_cannot_complete(self, session_factory):
        db = session_factory()
        bj.enqueue(db, job_type="HISTORICAL_INGESTION", idempotency_key="own:3")
        job = bj.claim_next(db, worker_id="worker-A")
        assert bj.complete_job(db, job, worker_id="worker-IMPOSTOR") is False
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        assert row.status == JobStatus.RUNNING.value
        assert row.lease_owner == "worker-A"

    def test_worker_loop_records_stale_outcome(self, session_factory, monkeypatch):
        session_factory()
        bj.enqueue(
            session_factory(),
            job_type="HISTORICAL_INGESTION",
            idempotency_key="stale:loop",
        )

        def slow_execute(db, job, **kwargs):
            s = session_factory()
            row = s.scalar(select(BackgroundJob))
            row.lease_expires_at = _utcnow() - timedelta(seconds=1)
            s.commit()
            s.close()
            reclaimer = session_factory()
            assert bj.claim_next(reclaimer, worker_id="worker-B") is not None
            reclaimer.close()

        monkeypatch.setattr(bj, "execute_job", slow_execute)
        summary = bj.run_worker(
            session_factory=session_factory, once=True, worker_id="worker-A"
        )
        assert summary["claimed"] == 1
        assert summary["succeeded"] == 0
        assert summary["stale"] == 1
        db = session_factory()
        row = db.scalar(select(BackgroundJob))
        assert row.status == JobStatus.RUNNING.value
        assert row.lease_owner == "worker-B"


class TestFailureTransitionPersistence:
    """F7: the failure transition survives transient database errors."""

    def test_transient_error_during_transition_is_retried(
        self, session_factory, monkeypatch
    ):
        from app.utils.retry import RetryExhausted

        bj.enqueue(
            session_factory(),
            job_type="HISTORICAL_INGESTION",
            idempotency_key="f7:1",
        )
        outcome_box = {}

        def fake_execute(db, job, **kwargs):
            outcome_box["attempt_count"] = job.attempt_count
            outcome_box["job_id"] = job.id
            raise bj.JobExecutionError("connection reset", retryable=True)

        monkeypatch.setattr(bj, "execute_job", fake_execute)

        # Make the FIRST failure-transition attempt raise a genuine CRDB
        # serialization failure; the real retry loop must re-run the
        # transition, which then lands. Proves F7 without faking the retry
        # helper itself.
        calls = {"n": 0}
        real_fail_job = bj.fail_job

        def flaky_transition(db, job, exc, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                orig = Exception("restart transaction")
                orig.sqlstate = "40001"
                from sqlalchemy.exc import OperationalError

                raise OperationalError("UPDATE background_jobs ...", {}, orig)
            return real_fail_job(db, job, exc, **kwargs)

        monkeypatch.setattr(bj, "fail_job", flaky_transition)
        summary = bj.run_worker(session_factory=session_factory, once=True)
        assert calls["n"] == 2, "transition must be retried, not fatal"
        assert summary["failed"] == 1
        db = session_factory()
        row = db.scalar(select(BackgroundJob))
        assert row.status == JobStatus.FAILED_RETRYABLE.value
        assert "connection reset" in row.last_error


class TestWorkerLookupFailure:
    """F6: a db.get failure must not crash the worker (no UnboundLocal)."""

    def test_lookup_failure_leaves_job_recoverable(self, session_factory, monkeypatch):
        from sqlalchemy.orm import Session

        bj.enqueue(session_factory(), job_type="HISTORICAL_INGESTION", idempotency_key="f6:1")

        def boom(self, entity, key):
            raise RuntimeError("connection lost during get")

        monkeypatch.setattr(Session, "get", boom)
        executed = []
        monkeypatch.setattr(bj, "execute_job", lambda db, job, **kwargs: executed.append(job.id))

        summary = bj.run_worker(session_factory=session_factory, once=True)
        assert summary["claimed"] == 1
        assert summary["succeeded"] == 0
        assert executed == []
        db = session_factory()
        row = db.scalar(select(BackgroundJob))
        assert row.status == JobStatus.RUNNING.value
        assert row.lease_owner is not None

    def test_worker_continues_after_lookup_failure(self, session_factory, monkeypatch):
        from sqlalchemy.orm import Session

        bj.enqueue(session_factory(), job_type="HISTORICAL_INGESTION", idempotency_key="f6:bad")
        bj.enqueue(session_factory(), job_type="HISTORICAL_INGESTION", idempotency_key="f6:good")

        state = {"gets": 0}
        original_get = Session.get

        def flaky_get(self, entity, key):
            state["gets"] += 1
            if state["gets"] == 1:
                raise RuntimeError("transient lookup failure")
            return original_get(self, entity, key)

        monkeypatch.setattr(Session, "get", flaky_get)
        monkeypatch.setattr(bj, "execute_job", lambda db, job, **kwargs: None)
        summary = bj.run_worker(session_factory=session_factory, once=True)
        assert summary["claimed"] == 2


class TestLeasePolicyPrecedence:
    """F8: explicit override > payload policy > default."""

    def test_payload_lease_used_when_no_override(self, session_factory):
        db = session_factory()
        bj.enqueue(
            db,
            job_type="HISTORICAL_INGESTION",
            idempotency_key="lease:1",
            payload={"policy": {"lease_seconds": 7777}},
        )
        job = bj.claim_next(db, worker_id="w1")
        delta = (job.lease_expires_at - job.started_at).total_seconds()
        assert 7770 <= delta <= 7790

    def test_explicit_override_beats_payload(self, session_factory):
        db = session_factory()
        bj.enqueue(
            db,
            job_type="HISTORICAL_INGESTION",
            idempotency_key="lease:2",
            payload={"policy": {"lease_seconds": 7777}},
        )
        job = bj.claim_next(db, worker_id="w1", lease_seconds=42)
        delta = (job.lease_expires_at - job.started_at).total_seconds()
        assert 41 <= delta <= 43

    def test_default_lease_without_payload_or_override(self, session_factory):
        db = session_factory()
        bj.enqueue(db, job_type="HISTORICAL_INGESTION", idempotency_key="lease:3")
        job = bj.claim_next(db, worker_id="w1")
        delta = (job.lease_expires_at - job.started_at).total_seconds()
        assert 899 <= delta <= 901

    def test_invalid_payload_lease_falls_back_to_default(self, session_factory):
        db = session_factory()
        bj.enqueue(
            db,
            job_type="HISTORICAL_INGESTION",
            idempotency_key="lease:4",
            payload={"policy": {"lease_seconds": "not-a-number"}},
        )
        job = bj.claim_next(db, worker_id="w1")
        delta = (job.lease_expires_at - job.started_at).total_seconds()
        assert 899 <= delta <= 901


class TestStageValidation:
    """F9: malformed stage payloads must dead-letter, never false-succeed."""

    def _enqueue_and_run(self, session_factory, payload):
        bj.enqueue(
            session_factory(),
            job_type="HISTORICAL_INGESTION",
            idempotency_key=f"stages:{json.dumps(payload, sort_keys=True)}",
            payload=payload,
        )
        return bj.run_worker(session_factory=session_factory, once=True)

    def test_empty_stages_list_rejected(self, session_factory):
        summary = self._enqueue_and_run(session_factory, {"stages": []})
        assert summary["dead_lettered"] == 1, summary
        row = db_scalar_helper(session_factory)
        assert "non-empty list" in row.last_error

    def test_missing_stages_rejected(self, session_factory):
        summary = self._enqueue_and_run(session_factory, {})
        assert summary["dead_lettered"] == 1
        row = db_scalar_helper(session_factory)
        assert "non-empty list" in row.last_error

    def test_string_stages_rejected(self, session_factory):
        summary = self._enqueue_and_run(session_factory, {"stages": "nifty"})
        assert summary["dead_lettered"] == 1
        row = db_scalar_helper(session_factory)
        assert "non-empty list" in row.last_error

    def test_none_stages_rejected(self, session_factory):
        summary = self._enqueue_and_run(session_factory, {"stages": None})
        assert summary["dead_lettered"] == 1
        row = db_scalar_helper(session_factory)
        assert "non-empty list" in row.last_error

    def test_unknown_stage_rejected(self, session_factory):
        summary = self._enqueue_and_run(session_factory, {"stages": ["nifty", "gex"]})
        assert summary["dead_lettered"] == 1
        row = db_scalar_helper(session_factory)
        assert "unknown ingestion stage" in row.last_error
        assert "'gex'" in row.last_error

    def test_non_string_stage_rejected(self, session_factory):
        summary = self._enqueue_and_run(session_factory, {"stages": ["nifty", 7]})
        assert summary["dead_lettered"] == 1
        row = db_scalar_helper(session_factory)
        assert "unknown ingestion stage" in row.last_error

    def test_duplicate_stages_rejected(self, session_factory):
        summary = self._enqueue_and_run(session_factory, {"stages": ["nifty", "nifty"]})
        assert summary["dead_lettered"] == 1
        row = db_scalar_helper(session_factory)
        assert "duplicate" in row.last_error

    def test_valid_stages_pass_and_reach_executor(self, session_factory, monkeypatch):
        bj.enqueue(
            session_factory(),
            job_type="HISTORICAL_INGESTION",
            idempotency_key="stages:ok",
            payload={"stages": ["contracts", "options"]},
        )
        seen = []
        monkeypatch.setattr(
            bj,
            "execute_job",
            lambda db, job, **kwargs: seen.append(json.loads(job.payload)["stages"]),
        )
        summary = bj.run_worker(session_factory=session_factory, once=True)
        assert summary["succeeded"] == 1
        assert seen == [["contracts", "options"]]


def db_scalar_helper(session_factory):
    db = session_factory()
    row = db.scalar(select(BackgroundJob).order_by(BackgroundJob.created_at.desc()))
    db.close()
    return row


class TestMalformedPayloadJson:
    def test_invalid_json_dead_letters_non_retryably(self, session_factory):
        db = session_factory()
        job, _ = bj.enqueue(
            db, job_type="HISTORICAL_INGESTION", idempotency_key="json:bad"
        )
        job.payload = "{not json"
        db.commit()
        with pytest.raises(bj.JobExecutionError) as excinfo:
            bj.execute_job(db, job)
        assert excinfo.value.retryable is False


class TestConcurrencyPropagation:
    """F5: payload concurrency reaches run_all -> run_options."""

    def test_requested_concurrency_forwarded_to_run_all(
        self, session_factory, monkeypatch
    ):
        captured = {}

        class _FakeResult:
            operation = "backfill_all"
            status = "SUCCESS"
            api_calls = 1
            rows_fetched = 0
            rows_inserted = 0
            rows_skipped = 0
            errors = []
            metadata = {}

        class _FakeOrchestrator:
            def __init__(self, db, client, *, force=False, rate_limiter=None):
                self.run_id = None

            async def run_all(
                self, *, stages=None, nifty_start_date=None, options_concurrency=None
            ):
                captured["options_concurrency"] = options_concurrency
                captured["orchestrator_run_id"] = self.run_id
                return _FakeResult()

        import app.services.backfill_orchestrator as orch_mod
        import app.services.upstox_client as upstox_mod

        monkeypatch.setattr(orch_mod, "BackfillOrchestrator", _FakeOrchestrator)
        monkeypatch.setattr(orch_mod, "TokenBridge", type("B", (), {}))
        monkeypatch.setattr(
            upstox_mod,
            "UpstoxClient",
            type("C", (), {"__init__": lambda self, token_provider=None: None}),
        )

        db = session_factory()
        job, _ = bj.enqueue(
            db,
            job_type="HISTORICAL_INGESTION",
            idempotency_key="conc:1",
            payload={"stages": ["options"], "concurrency": 4},
        )
        summary = bj.execute_historical_ingestion(db, job)
        assert captured["options_concurrency"] == 4
        assert summary["governance_run_id"]
        assert captured["orchestrator_run_id"] == summary["governance_run_id"]

        audit = db.scalar(
            select(HistoricalIngestionRun).where(
                HistoricalIngestionRun.run_id == summary["governance_run_id"]
            )
        )
        assert audit is not None
        assert json.loads(audit.dataset_keys_json) == ["UPSTOX_OPTION_CANDLES_3MIN"]
        assert audit.status == "SUCCEEDED"
        assert audit.background_job_id == job.id

    def test_orchestrator_run_all_forwards_to_run_options(self):
        """Direct regression: run_all(options_concurrency=N) reaches the
        option-stage limiter ceiling (smallest-compatible-change proof)."""
        import asyncio

        from app.services.backfill_orchestrator import BackfillOrchestrator

        from app.services.backfill_orchestrator import BackfillResult

        recorded = {}

        class _MiniOrchestrator(BackfillOrchestrator):
            async def run_options(self, **kwargs):
                recorded.update(kwargs)
                return BackfillResult(operation="options", status="SUCCESS")

            async def run_contracts(self):
                return BackfillResult(operation="contracts", status="SUCCESS")

            async def run_nifty(self, start_date=None):
                return BackfillResult(operation="nifty", status="SUCCESS")

        orch = _MiniOrchestrator.__new__(_MiniOrchestrator)
        asyncio.run(orch.run_all(stages=["options"], options_concurrency=4))
        assert recorded.get("concurrency") == 4

    def test_orchestrator_default_unchanged_without_request(self):
        import asyncio

        from app.services.backfill_orchestrator import BackfillOrchestrator

        from app.services.backfill_orchestrator import BackfillResult

        recorded = {}

        class _MiniOrchestrator(BackfillOrchestrator):
            async def run_options(self, **kwargs):
                recorded.update(kwargs)
                return BackfillResult(operation="options", status="SUCCESS")

            async def run_contracts(self):
                return BackfillResult(operation="contracts", status="SUCCESS")

            async def run_nifty(self, start_date=None):
                return BackfillResult(operation="nifty", status="SUCCESS")

        orch = _MiniOrchestrator.__new__(_MiniOrchestrator)
        asyncio.run(orch.run_all(stages=["options"]))
        assert "concurrency" not in recorded  # run_options default preserved


class TestHistoricalGovernanceFailureAudit:
    def test_orchestrator_failure_finalizes_governance_run(
        self, session_factory, monkeypatch
    ):
        class _FailingOrchestrator:
            def __init__(self, db, client, *, force=False, rate_limiter=None):
                pass

            async def run_all(
                self, *, stages=None, nifty_start_date=None, options_concurrency=None
            ):
                raise RuntimeError("synthetic ingestion failure")

        import app.services.backfill_orchestrator as orch_mod
        import app.services.upstox_client as upstox_mod

        monkeypatch.setattr(orch_mod, "BackfillOrchestrator", _FailingOrchestrator)
        monkeypatch.setattr(orch_mod, "TokenBridge", type("B", (), {}))
        monkeypatch.setattr(
            upstox_mod,
            "UpstoxClient",
            type("C", (), {"__init__": lambda self, token_provider=None: None}),
        )

        db = session_factory()
        job, _ = bj.enqueue(
            db,
            job_type="HISTORICAL_INGESTION",
            idempotency_key="gov-fail:1",
            payload={"stages": ["options"]},
        )
        with pytest.raises(RuntimeError, match="synthetic ingestion failure"):
            bj.execute_historical_ingestion(db, job)

        audit = db.scalar(
            select(HistoricalIngestionRun).where(
                HistoricalIngestionRun.background_job_id == job.id
            )
        )
        assert audit is not None
        assert audit.status == "FAILED"
        assert "synthetic ingestion failure" in (audit.error_message or "")


class TestGovernanceAuditWindow:
    """Day 48 audit-window: the governance manifest's coverage window must
    be the SAME effective window the NIFTY stage actually ingests —
    resolved once and shared — so completeness aggregation cannot be
    skewed by DataCompleteness rows from outside this run's window.
    (CodeRabbit finding on 8b0071b.)"""

    def _seed_nifty_expiry(self, db, expiry: str):
        db.add(
            ContractSpec(
                instrument_key="NSE_FO|58124|TESTCE",
                underlying="NIFTY",
                underlying_key="NSE_FO|58124",
                expiry=expiry,
                strike_price=25000.0,
                instrument_type="CE",
                trading_symbol="NIFTY",
                segment="NSE_FO",
                exchange="NSE",
                source="TEST",
                source_reference="test",
                fetched_at=datetime(2026, 9, 1),
            )
        )
        db.commit()

    def _enqueue_and_run(self, monkeypatch, session_factory, payload, captured, result_metadata=None, key=None):
        class _FakeResult:
            operation = "backfill_all"
            status = "SUCCESS"
            api_calls = 1
            rows_fetched = 0
            rows_inserted = 0
            rows_skipped = 0
            errors = []
            metadata = result_metadata or {}

        class _FakeOrch:
            def __init__(self, db, client, *, force=False, rate_limiter=None):
                self.run_id = None

            async def run_all(
                self,
                *,
                stages=None,
                nifty_start_date=None,
                nifty_end_date=None,
                options_concurrency=None,
            ):
                captured["nifty_start"] = nifty_start_date
                captured["nifty_end"] = nifty_end_date
                captured["orchestrator_run_id"] = self.run_id
                return _FakeResult()

        import app.services.backfill_orchestrator as orch_mod
        import app.services.upstox_client as upstox_mod

        monkeypatch.setattr(orch_mod, "BackfillOrchestrator", _FakeOrch)
        monkeypatch.setattr(orch_mod, "TokenBridge", type("B", (), {}))
        monkeypatch.setattr(
            upstox_mod,
            "UpstoxClient",
            type("C", (), {"__init__": lambda self, token_provider=None: None}),
        )
        db = session_factory()
        job, _ = bj.enqueue(
            db,
            job_type="HISTORICAL_INGESTION",
            idempotency_key=key
            or f"gov-window:{payload.get('nifty_start_date', 'default')}",
            payload=payload,
        )
        summary = bj.execute_historical_ingestion(db, job)
        audit = db.scalar(
            select(HistoricalIngestionRun).where(
                HistoricalIngestionRun.run_id == summary["governance_run_id"]
            )
        )
        db.close()
        return job, summary, audit

    def test_explicit_start_shared_between_governance_and_ingestion(
        self, session_factory, monkeypatch
    ):
        """Case A: an explicit nifty_start_date reaches run_nifty unchanged
        AND the manifest records exactly that start plus the effective end
        (today), not NULL bounds."""
        db = session_factory()
        self._seed_nifty_expiry(db, "2026-12-24")
        db.close()

        captured = {}
        job, summary, audit = self._enqueue_and_run(
            monkeypatch,
            session_factory,
            {"stages": ["nifty"], "nifty_start_date": "2024-01-01"},
            captured,
        )

        today = datetime.now(timezone.utc).date()
        assert captured["nifty_start"] == datetime(2024, 1, 1).date()
        assert audit.coverage_start == "2024-01-01"
        assert audit.coverage_end == today.isoformat()
        assert captured["orchestrator_run_id"] == summary["governance_run_id"]
    def test_omitted_start_records_resolved_default_window(
        self, session_factory, monkeypatch
    ):
        """Case B: with no explicit start, run_nifty's normal default
        (earliest NIFTY expiry - 3 days, or today - 365 without a registry)
        must be recorded on the manifest — not NULL — so the audit window
        equals the execution window."""
        captured = {}
        job, summary, audit = self._enqueue_and_run(
            monkeypatch,
            session_factory,
            {"stages": ["nifty"]},
            captured,
        )

        today = datetime.now(timezone.utc).date()
        expected_default_start = today - timedelta(days=365)
        # background_jobs forwards the RAW payload start (None when omitted);
        # default resolution happens inside run_all after contract discovery.
        assert captured["nifty_start"] is None
        assert audit.coverage_start == expected_default_start.isoformat()
        assert audit.coverage_end == today.isoformat()

    def test_run_nifty_resolution_matches_shared_window(
        self, session_factory
    ):
        """Case B (execution side): run_nifty via run_all with no explicit
        start must produce the SAME chunks the shared resolver computes —
        proving the execution window equals the audit window (run_nifty in
        dry-run mode returns the resolved chunk plan without API calls)."""
        import asyncio

        from app.services.backfill_orchestrator import (
            BackfillOrchestrator,
            resolve_nifty_window,
        )

        db = session_factory()
        orch = BackfillOrchestrator.__new__(BackfillOrchestrator)
        orch.db = db
        orch.dry_run = True

        result = asyncio.run(orch.run_nifty())

        today = datetime.now(timezone.utc).date()
        expected_start, expected_end = today - timedelta(days=365), today
        resolved_start, resolved_end = resolve_nifty_window(db)
        assert (resolved_start, resolved_end) == (expected_start, expected_end)
        chunks = result.metadata["chunks"]
        assert chunks[0]["from"] == expected_start.isoformat()
        assert chunks[-1]["to"] == expected_end.isoformat()

    def test_stale_completeness_rows_outside_window_do_not_flip_status(
        self, session_factory
    ):
        """Case C: with the effective window recorded, an older
        DataCompleteness row outside it must not make a successful run
        PARTIAL (finish_ingestion_run downgrade must not fire)."""
        from app.services import historical_data_governance as hdg

        today = datetime.now(timezone.utc).date()
        window_start = (today - timedelta(days=365)).isoformat()

        run = hdg.start_ingestion_run(
            session_factory(),
            dataset_keys=["UPSTOX_NIFTY_CANDLES_3MIN"],
            coverage_start=window_start,
            coverage_end=today.isoformat(),
            run_id="run-audit-window",
        )
        db = session_factory()
        db.add_all(
            [
                # Stale row: far outside the effective window.
                DataCompleteness(
                    instrument_key="NSE_INDEX|NIFTY 50",
                    session_date="2020-01-15",
                    data_type="nifty_candles",
                    expected_count=500,
                    actual_count=0,
                    missing_count=500,
                    status="PARTIAL",
                ),
                # Current row: inside the effective window, complete.
                DataCompleteness(
                    instrument_key="NSE_INDEX|NIFTY 50",
                    session_date=today.isoformat(),
                    data_type="nifty_candles",
                    expected_count=75,
                    actual_count=75,
                    missing_count=0,
                    status="COMPLETE",
                ),
            ]
        )
        db.commit()

        refreshed = hdg.refresh_ingestion_run_metrics(db, run.run_id)
        assert refreshed.completeness_status == "COMPLETE"
        assert refreshed.missing_records == 0
        assert refreshed.expected_records == 75

        finished = hdg.finish_ingestion_run(db, run.run_id, status=hdg.RUN_SUCCEEDED)
        assert finished.status == hdg.RUN_SUCCEEDED

    def _run_real_chain(self, session_factory, stages, nifty_start_date=None):
        """Drive the REAL BackfillOrchestrator.run_all with only the
        external boundaries faked: contract discovery is simulated by
        seeding ContractSpec rows (what run_contracts would persist) and
        the options stage is stubbed. run_nifty itself stays REAL in
        dry-run mode, so the resolved chunk plan is observable without
        any API or candle writes."""
        import asyncio
        from datetime import date as _date

        from app.services.backfill_orchestrator import (
            BackfillOrchestrator,
            BackfillResult,
        )

        class _FakeContractsResult:
            operation = "contracts"
            status = "SUCCESS"
            api_calls = 0
            rows_fetched = 0
            rows_inserted = 0
            errors = []
            metadata = {"expiries": ["2020-01-30"]}

        class _ChainOrchestrator(BackfillOrchestrator):
            async def run_contracts(self):
                # Simulates discovered expiries; the DB seed below is the
                # persisted effect real discovery would have produced.
                return _FakeContractsResult()

            async def run_options(self, concurrency=None):
                return BackfillResult(operation="options", status="SUCCESS")

        db = session_factory()
        orch = _ChainOrchestrator.__new__(_ChainOrchestrator)
        BackfillOrchestrator.__init__(
            orch, db, client=None, dry_run=True
        )
        orch.force = False

        result = asyncio.run(
            orch.run_all(stages=list(stages), nifty_start_date=nifty_start_date)
        )
        db.close()
        return result

    def test_contracts_plus_nifty_resolves_default_start_after_discovery(
        self, session_factory
    ):
        """Case A (CodeRabbit on 2d4cec2): with an initially EMPTY registry,
        contract discovery must run BEFORE the default start is resolved.
        A newly discovered expiry older than the 365-day fallback must
        extend the effective window back to (expiry - 3 days)."""
        db = session_factory()
        # "Discovered" by the contracts stage: older than today - 365d.
        self._seed_nifty_expiry(db, "2020-01-30")
        db.close()

        result = self._run_real_chain(session_factory, ["contracts", "nifty"])

        today = datetime.now(timezone.utc).date()
        assert result.metadata["nifty_coverage_start"] == "2020-01-27"
        assert result.metadata["nifty_coverage_end"] == today.isoformat()
        chunks = result.metadata["chunks"]
        assert chunks[0]["from"] == "2020-01-27"
        assert chunks[-1]["to"] == today.isoformat()

    def test_run_all_forwards_resolved_start_and_end_to_run_nifty(
        self, session_factory
    ):
        """Cases B+C (CodeRabbit on 2d4cec2): run_nifty must receive BOTH
        the resolved start AND the resolved end explicitly — it must never
        re-derive an omitted end (UTC-midnight divergence) — and the
        forwarded start must be the post-discovery default."""
        import asyncio

        from app.services.backfill_orchestrator import (
            BackfillOrchestrator,
            BackfillResult,
        )

        db = session_factory()
        self._seed_nifty_expiry(db, "2020-01-30")
        db.close()

        captured = {}

        class _CaptureNifty(BackfillOrchestrator):
            async def run_contracts(self):
                result = BackfillResult(operation="contracts", status="SUCCESS")
                result.metadata["expiries"] = ["2020-01-30"]
                return result

            async def run_nifty(self, start_date=None, end_date=None):
                captured["start"] = start_date
                captured["end"] = end_date
                result = BackfillResult(operation="nifty_candles", status="SUCCESS")
                result.metadata["chunks"] = [
                    {"from": start_date.isoformat(), "to": end_date.isoformat()}
                ]
                return result

            async def run_options(self, concurrency=None):
                return BackfillResult(operation="options", status="SUCCESS")

        orch = _CaptureNifty.__new__(_CaptureNifty)
        BackfillOrchestrator.__init__(orch, session_factory(), client=None)

        run_result = asyncio.run(orch.run_all(stages=["contracts", "nifty"]))
        assert run_result.errors == [], run_result.errors

        today = datetime.now(timezone.utc).date()
        assert captured["start"] == datetime(2020, 1, 27).date()
        assert captured["end"] == today

    def test_explicit_start_survives_contracts_and_nifty(self, session_factory):
        """Case E: an explicit nifty_start_date is respected as-is through
        the contracts + nifty coordination (a registry with an older expiry
        must NOT override it)."""
        db = session_factory()
        self._seed_nifty_expiry(db, "2020-01-30")
        db.close()

        result = self._run_real_chain(
            session_factory,
            ["contracts", "nifty"],
            nifty_start_date=datetime(2024, 1, 1).date(),
        )

        assert result.metadata["nifty_coverage_start"] == "2024-01-01"
        chunks = result.metadata["chunks"]
        assert chunks[0]["from"] == "2024-01-01"

    def test_manifest_records_actual_window_from_execution_result(
        self, session_factory, monkeypatch
    ):
        """Case D: the manifest must carry the window the execution ACTUALLY
        used (from the orchestrator result), not a value background_jobs
        guessed before run_all."""
        captured = {}
        job, summary, audit = self._enqueue_and_run(
            monkeypatch,
            session_factory,
            {"stages": ["nifty"]},
            captured,
            result_metadata={
                "nifty_coverage_start": "2021-06-01",
                "nifty_coverage_end": "2021-06-30",
            },
            key="gov-window:actual",
        )

        # background_jobs forwards the RAW payload start; the manifest's
        # authoritative window comes from the executed result metadata.
        assert captured["nifty_start"] is None
        assert audit.coverage_start == "2021-06-01"
        assert audit.coverage_end == "2021-06-30"
        assert captured["orchestrator_run_id"] == summary["governance_run_id"]

    def test_options_only_keeps_existing_manifest_semantics(
        self, session_factory, monkeypatch
    ):
        """Options-only jobs have no NIFTY coverage window; the manifest
        keeps the resolver-derived bounds and a result without NIFTY window
        metadata must not overwrite them."""
        captured = {}
        job, summary, audit = self._enqueue_and_run(
            monkeypatch,
            session_factory,
            {"stages": ["options"]},
            captured,
            result_metadata={},
            key="gov-window:options-only",
        )

        today = datetime.now(timezone.utc).date()
        expected_start = (today - timedelta(days=365)).isoformat()
        assert audit.coverage_start == expected_start
        assert audit.coverage_end == today.isoformat()

    def test_contracts_failure_stops_nifty_and_records_failure(
        self, session_factory
    ):
        """Failure path: run_all converts a contracts-stage exception into a
        FAILED result WITHOUT running NIFTY — no effective NIFTY window is
        resolved or fabricated after the failure (status/errors carry the
        failure; background_jobs finalizes the manifest FAILED)."""
        import asyncio

        from app.services.backfill_orchestrator import BackfillOrchestrator

        captured = {}

        class _FailingContracts(BackfillOrchestrator):
            async def run_contracts(self):
                raise RuntimeError("synthetic contract discovery failure")

            async def run_nifty(self, start_date=None, end_date=None):
                captured["called"] = True
                raise AssertionError("run_nifty must not run after contract failure")

            async def run_options(self, concurrency=None):
                raise AssertionError("run_options must not run after contract failure")

        orch = _FailingContracts.__new__(_FailingContracts)
        BackfillOrchestrator.__init__(orch, session_factory(), client=None)

        result = asyncio.run(orch.run_all(stages=["contracts", "nifty"]))
        assert result.status == "FAILED"
        assert any(
            "synthetic contract discovery failure" in e for e in result.errors
        )
        assert "called" not in captured
        assert "nifty_coverage_start" not in result.metadata


class TestCliDatabaseUrlNormalization:
    """F3: the CLI uses the canonical normalization path."""

    def test_sqlite_unchanged(self):
        from app.db import normalize_database_url

        assert normalize_database_url("sqlite:///foo.db") == "sqlite:///foo.db"

    def test_explicit_psycopg_dialect_unchanged(self):
        from app.db import normalize_database_url

        url = "postgresql+psycopg://u:p@h:5432/db"
        assert normalize_database_url(url) == url

    def test_bare_postgres_scheme_normalized(self):
        from app.db import normalize_database_url

        assert (
            normalize_database_url("postgres://u:p@h:5432/db")
            == "postgresql+psycopg://u:p@h:5432/db"
        )

    def test_bare_postgresql_scheme_normalized(self):
        from app.db import normalize_database_url

        assert (
            normalize_database_url("postgresql://u:p@h:5432/db")
            == "postgresql+psycopg://u:p@h:5432/db"
        )

    def test_cli_session_factory_uses_normalization(self, monkeypatch):
        """The factory itself must normalize (end-to-end, not just the helper)."""
        import run_jobs

        captured = {}

        class _FakeEngine:
            def __init__(self, url, connect_args=None):
                captured["url"] = url

        monkeypatch.setattr(run_jobs, "create_engine", _FakeEngine)
        monkeypatch.setattr(
            run_jobs.settings,
            "DATABASE_URL",
            "postgres://u:p@h:5432/db",
            raising=False,
        )
        run_jobs._get_session_factory()
        assert captured["url"] == "postgresql+psycopg://u:p@h:5432/db"


class TestCliStageCombinations:
    """F4: stages combine; --all is the deterministic superset rule."""

    def _stages(self, all_flag=False, contracts=False, index=False, options=False):
        from run_jobs import _build_stage_list

        return _build_stage_list(
            all_flag=all_flag,
            contracts=contracts,
            index=index,
            options=options,
        )

    def test_single_stages(self):
        assert self._stages(contracts=True) == ["contracts"]
        assert self._stages(index=True) == ["nifty"]
        assert self._stages(options=True) == ["options"]

    def test_combinations(self):
        assert self._stages(index=True, options=True) == ["nifty", "options"]
        assert self._stages(contracts=True, options=True) == ["contracts", "options"]
        assert self._stages(contracts=True, index=True) == ["contracts", "nifty"]

    def test_all_wins_over_individual_flags(self):
        assert self._stages(all_flag=True) == ["contracts", "nifty", "options"]
        assert self._stages(all_flag=True, index=True) == [
            "contracts",
            "nifty",
            "options",
        ]

    def test_no_stage_is_empty(self):
        assert self._stages() == []


# ---------------------------------------------------------------------------
# 11. Heartbeat / lease renewal (CodeRabbit Major) + completion retry (Minor)
# ---------------------------------------------------------------------------


def _shared_memory_sqlite_factory():
    """A session factory sharing ONE in-memory SQLite database across ALL
    threads.

    Plain ``sqlite://`` gives each thread its own empty database, so the
    heartbeat thread (a different thread) could never see claimed rows.
    Sharing a single connection is the standard cross-thread in-memory
    SQLite pattern; SQLite serializes the writes, which is exactly what a
    heartbeat needs.
    """
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    # Day 48: the real historical-ingestion execution path fails closed
    # without a governed catalog, so mirror the migrated schema's seeded
    # stage datasets here (same rows the session_factory fixture seeds).
    seed = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    seed.add_all(
        HistoricalDatasetGovernance(
            dataset_key=key,
            domain="MARKET_DATA",
            dataset_tier="RAW",
            table_name=table_name,
            pipeline=pipeline,
            completeness_data_type=data_type,
            source="UPSTOX",
            source_reference="test",
            source_version="test",
            entitlement_requirement="TEST",
            entitlement_status="REVIEW_REQUIRED",
            license_status="REVIEW_REQUIRED",
            usage_policy="INTERNAL_ONLY",
            redistribution_status="REVIEW_REQUIRED",
            retention_policy="KEEP",
            raw_immutable=True,
            recomputable=True,
            dependencies_json="[]",
        )
        for key, table_name, pipeline, data_type in (
            (
                "UPSTOX_CONTRACT_SPECS",
                "contract_specs",
                "backfill_contracts",
                "contract_metadata",
            ),
            (
                "UPSTOX_NIFTY_CANDLES_3MIN",
                "nifty_candles",
                "backfill_nifty",
                "nifty_candles",
            ),
            (
                "UPSTOX_OPTION_CANDLES_3MIN",
                "option_candles",
                "backfill_options",
                "option_candles",
            ),
        )
    )
    seed.commit()
    seed.close()
    return sessionmaker(bind=engine, autocommit=False, autoflush=False), engine


class TestRenewLease:
    """F1: renew_lease is ownership-protected exactly like complete_job."""

    def test_current_owner_renews(self, session_factory):
        db = session_factory()
        _enqueue(db, "hb:1")
        job = bj.claim_next(db, worker_id="worker-A", lease_seconds=60)
        assert job is not None
        before = job.lease_expires_at
        assert bj.renew_lease(
            db, job.id, worker_id="worker-A", lease_seconds=60
        ) is True
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        assert row.status == JobStatus.RUNNING.value
        assert row.lease_owner == "worker-A"
        assert row.lease_expires_at > before

    def test_wrong_worker_rejected(self, session_factory):
        db = session_factory()
        _enqueue(db, "hb:2")
        job = bj.claim_next(db, worker_id="worker-A", lease_seconds=60)
        assert job is not None
        assert bj.renew_lease(
            db, job.id, worker_id="worker-IMPOSTOR", lease_seconds=60
        ) is False
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        assert row.lease_owner == "worker-A"  # untouched

    def test_expired_lease_rejected(self, session_factory):
        db = session_factory()
        _enqueue(db, "hb:3")
        job = bj.claim_next(db, worker_id="worker-A", lease_seconds=60)
        assert job is not None
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        row.lease_expires_at = _utcnow() - timedelta(seconds=1)
        db.commit()
        assert bj.renew_lease(
            db, job.id, worker_id="worker-A", lease_seconds=60
        ) is False
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        assert row.lease_expires_at <= _utcnow() - timedelta(seconds=1)

    def test_repeated_renewal_extends_lease(self, session_factory):
        db = session_factory()
        _enqueue(db, "hb:4")
        job = bj.claim_next(db, worker_id="worker-A", lease_seconds=60)
        assert job is not None
        for _ in range(3):
            assert bj.renew_lease(
                db, job.id, worker_id="worker-A", lease_seconds=60
            ) is True
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        delta = (row.lease_expires_at - _utcnow()).total_seconds()
        # The lease keeps getting pushed ~60s out (commit latency tolerated).
        assert 55 <= delta <= 65

    def test_renewal_requires_running_state(self, session_factory):
        db = session_factory()
        _enqueue(db, "hb:5")
        job = bj.claim_next(db, worker_id="worker-A", lease_seconds=60)
        assert job is not None
        db.expire_all()
        row = db.scalar(select(BackgroundJob))
        row.status = JobStatus.SUCCEEDED.value
        row.lease_owner = None
        row.lease_expires_at = None
        db.commit()
        assert bj.renew_lease(
            db, job.id, worker_id="worker-A", lease_seconds=60
        ) is False


class TestHeartbeatInterval:
    def test_interval_is_lease_over_three(self):
        assert bj.heartbeat_interval(90) == 30.0

    def test_interval_has_safe_floor(self):
        assert bj.heartbeat_interval(1) == bj._MIN_HEARTBEAT_INTERVAL_SECONDS
        assert bj.heartbeat_interval(0) == bj._MIN_HEARTBEAT_INTERVAL_SECONDS


class TestHeartbeatDuringExecution:
    """The worker's heartbeat loop: renews while executing, stops after."""

    def test_execution_beyond_original_lease_completes_via_heartbeat(
        self, session_factory, monkeypatch
    ):
        factory, engine = _shared_memory_sqlite_factory()
        try:
            db = factory()
            _enqueue(db, "hb:long", payload={"policy": {"lease_seconds": 1}}
                     )
            db.close()
            renewals = {"n": 0}
            real_renew = bj.renew_lease

            def counting_renew(db, job_id, **kwargs):
                ok = real_renew(db, job_id, **kwargs)
                if ok:
                    renewals["n"] += 1
                return ok

            monkeypatch.setattr(bj, "renew_lease", counting_renew)

            def long_execute(db, job, **kwargs):
                # Exceeds the 1s payload lease; only renewal keeps it valid.
                import time as _time

                _time.sleep(1.8)

            monkeypatch.setattr(bj, "execute_job", long_execute)
            summary = bj.run_worker(
                session_factory=factory, once=True, worker_id="worker-A"
            )
            assert summary["succeeded"] == 1, summary
            assert renewals["n"] >= 1, "heartbeat must have renewed the lease"
            db = factory()
            row = db.scalar(select(BackgroundJob))
            assert row.status == JobStatus.SUCCEEDED.value
            db.close()
        finally:
            engine.dispose()

    def test_heartbeat_stops_after_execution(self, session_factory, monkeypatch):
        factory, engine = _shared_memory_sqlite_factory()
        try:
            db = factory()
            # 1s lease -> 0.5s heartbeat period, so a leaked thread would
            # attempt a renewal within the observation window below.
            _enqueue(db, "hb:stop", payload={"policy": {"lease_seconds": 1}})
            db.close()
            attempts = {"n": 0}
            real_renew = bj.renew_lease

            def counting_renew(db, job_id, **kwargs):
                attempts["n"] += 1
                return real_renew(db, job_id, **kwargs)

            monkeypatch.setattr(bj, "renew_lease", counting_renew)

            def brief_execute(db, job, **kwargs):
                import time as _time

                _time.sleep(1.2)  # >= 2 heartbeat cycles while RUNNING

            monkeypatch.setattr(bj, "execute_job", brief_execute)
            summary = bj.run_worker(
                session_factory=factory, once=True, worker_id="worker-A"
            )
            assert summary["succeeded"] == 1
            during = attempts["n"]
            assert during >= 2, "heartbeat must have run during execution"
            import time as _time

            _time.sleep(0.9)  # > one full heartbeat period
            assert attempts["n"] == during, (
                "no renewal attempt may happen after execution finished"
            )
            db = factory()
            row = db.scalar(select(BackgroundJob))
            assert row.status == "SUCCEEDED"
            db.close()
        finally:
            engine.dispose()

    def test_ownership_lost_mid_execution_stale_outcome(
        self, session_factory, monkeypatch
    ):
        """Heartbeat stops renewing when ownership is lost; the stale worker
        still cannot overwrite the replacement attempt."""
        factory, engine = _shared_memory_sqlite_factory()
        try:
            db = factory()
            _enqueue(db, "hb:stale")
            db.close()

            def steal_lease(db, job, **kwargs):
                s = factory()
                row = s.scalar(select(BackgroundJob))
                row.lease_expires_at = _utcnow() - timedelta(seconds=1)
                s.commit()
                reclaimer = factory()
                assert bj.claim_next(reclaimer, worker_id="worker-B") is not None
                reclaimer.close()
                s.close()

            monkeypatch.setattr(bj, "execute_job", steal_lease)
            summary = bj.run_worker(
                session_factory=factory, once=True, worker_id="worker-A"
            )
            assert summary["stale"] == 1
            assert summary["succeeded"] == 0
            db = factory()
            row = db.scalar(select(BackgroundJob))
            assert row.status == "RUNNING"
            assert row.lease_owner == "worker-B"  # replacement intact
            db.close()
        finally:
            engine.dispose()

    def test_renewal_db_failure_does_not_crash_worker(
        self, session_factory, monkeypatch, caplog
    ):
        """A transient renewal DB failure is logged; the lease safeguard
        (not the heartbeat) remains the final authority."""
        import logging as _logging

        factory, engine = _shared_memory_sqlite_factory()
        try:
            db = factory()
            # 1s lease -> heartbeat period floored to 0.5s so renewals
            # actually occur during the 1.2s execution below.
            _enqueue(db, "hb:dbfail", payload={"policy": {"lease_seconds": 1}})
            db.close()
            calls = {"n": 0}
            real_renew = bj.renew_lease

            def flaky_renew(db, job_id, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("connection reset during renewal")
                return real_renew(db, job_id, **kwargs)

            monkeypatch.setattr(bj, "renew_lease", flaky_renew)

            def slow_execute(db, job, **kwargs):
                import time as _time

                _time.sleep(1.2)

            monkeypatch.setattr(bj, "execute_job", slow_execute)
            with caplog.at_level(_logging.WARNING):
                summary = bj.run_worker(
                    session_factory=factory, once=True, worker_id="worker-A"
                )
            assert summary["succeeded"] == 1
            assert calls["n"] >= 2
            warnings = [r for r in caplog.records if "heartbeat" in r.getMessage().lower()]
            assert warnings, "renewal failure must be logged"
            db = factory()
            row = db.execute(select(BackgroundJob)).scalars().first()
            assert row.status == "SUCCEEDED"
            db.close()
        finally:
            engine.dispose()


class TestHeartbeatSessionAcquisitionFailure:
    """Heartbeat durability gap: a failure to ACQUIRE the heartbeat's DB
    session (pool exhaustion, connection loss) must be handled exactly
    like a renewal failure — logged, retried next cycle, never fatal.
    Before the fix, session_factory() sat OUTSIDE the try block, so an
    acquisition failure killed the heartbeat thread with an uncaught
    exception and a long-running job silently lost lease renewal."""

    def test_acquisition_failure_is_recoverable(
        self, session_factory, monkeypatch
    ):
        """Test A: first acquisition fails, later ones succeed; the thread
        survives, renews, and the worker still completes with ownership
        intact. No unhandled exception may escape the heartbeat thread
        (asserted via threading.excepthook)."""
        import threading as _threading
        import time as _time

        factory, engine = _shared_memory_sqlite_factory()
        try:
            db = factory()
            # 2s lease -> 0.67s heartbeat period (lease/3); the first
            # heartbeat acquisition fails, later ones succeed.
            _enqueue(db, "hb:acq:1", payload={"policy": {"lease_seconds": 2}})
            db.close()

            escaped = []
            prev_excepthook = _threading.excepthook

            def _hook(args):
                escaped.append(args)

            _threading.excepthook = _hook
            try:
                renewals = {"ok": 0}
                real_renew = bj.renew_lease

                def counting_renew(db, job_id, **kwargs):
                    ok = real_renew(db, job_id, **kwargs)
                    if ok:
                        renewals["ok"] += 1
                    return ok

                monkeypatch.setattr(bj, "renew_lease", counting_renew)

                def long_execute(db, job, **kwargs):
                    _time.sleep(2.5)  # outlives the original 2s lease

                monkeypatch.setattr(bj, "execute_job", long_execute)
                summary = bj.run_worker(
                    session_factory=_HeartbeatFailingFactory(factory, fail_first=1),
                    once=True,
                    worker_id="worker-A",
                )
                assert summary["succeeded"] == 1, summary
            finally:
                _threading.excepthook = prev_excepthook

            assert escaped == [], (
                f"no unhandled exception may escape the heartbeat thread: {escaped}"
            )
            assert renewals["ok"] >= 1, "a later renewal must succeed after recovery"
            db = factory()
            row = db.scalar(select(BackgroundJob))
            assert row.status == "SUCCEEDED"
            assert row.completed_at is not None
            db.close()
        finally:
            engine.dispose()

    def test_repeated_acquisition_failures_logged_not_fatal(
        self, session_factory, monkeypatch, caplog
    ):
        """Test B: repeated acquisition failures must keep the heartbeat
        attempting (logged, thread alive), report NO false renewal, and
        leave the ownership-protected completion as the final authority.

        Deterministic scenario: every heartbeat acquisition fails during
        attempt 1 (2.4s execution, 1s lease), so ownership is genuinely
        lost and attempt 1's completion is refused (stale). The drain loop
        then reclaims the expired job; with acquisitions working again,
        attempt 2 renews and completes — proving the outcome was decided
        by ownership protection, not by the heartbeat failing silently."""
        import logging as _logging
        import threading as _threading
        import time as _time

        factory, engine = _shared_memory_sqlite_factory()
        try:
            db = factory()
            _enqueue(db, "hb:acq:2", payload={"policy": {"lease_seconds": 1}})
            db.close()

            escaped = []
            prev_excepthook = _threading.excepthook

            def _hook(args):
                escaped.append(args)

            _threading.excepthook = _hook
            state = {"fail": True, "first_attempt_done": False}

            def failing_while_flagged():
                if state["fail"] and _threading.current_thread().name.startswith(
                    "heartbeat-"
                ):
                    raise RuntimeError("db unavailable (test injection)")
                return factory()

            def execute_by_attempt(db, job, **kwargs):
                if state["first_attempt_done"]:  # attempt 2 (reclaim): healthy
                    _time.sleep(0.2)
                    return
                _time.sleep(2.4)  # attempt 1: outlives the 1s lease
                state["first_attempt_done"] = True
                state["fail"] = False  # later heartbeats succeed

            monkeypatch.setattr(bj, "execute_job", execute_by_attempt)
            try:
                with caplog.at_level(_logging.WARNING):
                    summary = bj.run_worker(
                        session_factory=failing_while_flagged,
                        once=True,
                        worker_id="worker-A",
                    )
            finally:
                _threading.excepthook = prev_excepthook

            # Thread survived (no unhandled escape), no fabricated success
            # during attempt 1 (its completion was refused as stale), and
            # the ownership-protected transition decided the final result.
            assert escaped == [], (
                f"heartbeat thread must survive repeated failures: {escaped}"
            )
            assert summary == {
                "claimed": 2,
                "succeeded": 1,
                "failed": 0,
                "dead_lettered": 0,
                "stale": 1,
            }, summary
            failures = [
                r
                for r in caplog.records
                if "heartbeat cycle" in r.getMessage() and r.exc_info
            ]
            assert len(failures) >= 2, "acquisition failures must be logged with exc_info"
            db = factory()
            row = db.scalar(select(BackgroundJob))
            assert row.status == "SUCCEEDED"  # attempt 2, valid ownership
            db.close()
        finally:
            engine.dispose()


class _HeartbeatFailingFactory:
    """Session-factory wrapper that fails session ACQUISITION for the
    heartbeat thread's first N attempts — deterministically, by thread
    name (the worker names the thread ``heartbeat-<job-id-8>``). Every
    other consumer (claim, execution, completion — all on the worker
    thread) passes through untouched, so only the heartbeat's acquisition
    path is exercised."""

    def __init__(self, inner, fail_first: int):
        self._inner = inner
        self._remaining = fail_first

    def __call__(self):
        import threading

        if (
            self._remaining > 0
            and threading.current_thread().name.startswith("heartbeat-")
        ):
            self._remaining -= 1
            raise RuntimeError("connection pool exhausted (test injection)")
        return self._inner()


class TestWorkerRateLimiterLifecycle:
    """Rate-limiter lifecycle finding (verified against repository
    architecture, then fixed): ``GlobalRateLimiter`` is a WORKER-LIFETIME
    in-process limiter — the same scope ``run_backfill.py`` gives its whole
    CLI process. Adaptive/cooldown state must survive job boundaries; each
    job's requested concurrency is applied as THAT job's ceiling without
    inheriting the previous job's request."""

    def test_a_limiter_persists_across_sequential_jobs(
        self, session_factory, monkeypatch
    ):
        """Two jobs through one worker lifecycle observe the SAME limiter
        object and its preserved adaptive state (no fresh limiter per
        job, no process-wide singleton reset)."""
        factory, engine = _shared_memory_sqlite_factory()
        try:
            db = factory()
            _enqueue(db, "lim:1")
            _enqueue(db, "lim:2")
            db.close()

            observed = []

            def fake_execute(db, job, **kwargs):
                limiter = kwargs["rate_limiter"]
                observed.append(
                    {
                        "job": job.idempotency_key,
                        "id": id(limiter),
                        "interval": limiter.interval,
                    }
                )
                if job.idempotency_key == "lim:1":
                    # Simulate adaptive state earned during job 1.
                    limiter._interval = 2.5
                    limiter._consecutive_429s = 3

            monkeypatch.setattr(bj, "execute_job", fake_execute)
            summary = bj.run_worker(session_factory=factory, once=True)
            assert summary["succeeded"] == 2
            assert len(observed) == 2
            assert observed[0]["id"] == observed[1]["id"], (
                "one worker lifecycle must reuse ONE limiter across jobs"
            )
            assert observed[1]["interval"] == 2.5, (
                "job 2 must observe the adaptive state job 1 earned"
            )
        finally:
            engine.dispose()

    def test_b_real_dispatch_preserves_state_and_applies_per_job_concurrency(
        self, session_factory, monkeypatch
    ):
        """Through the REAL execute_historical_ingestion path (fake
        orchestrator boundary): the shared limiter's adaptive state
        survives prepare_run_rate_limiter, while each job's requested
        concurrency is applied as that job's ceiling — job 2 does NOT
        inherit job 1's request."""
        import app.services.backfill_orchestrator as orch_mod
        import app.services.upstox_client as upstox_mod

        factory, engine = _shared_memory_sqlite_factory()
        try:
            db = factory()
            _enqueue(db, "lim:conc:5", payload={"stages": ["contracts"], "concurrency": 5})
            _enqueue(db, "lim:conc:2", payload={"stages": ["options"], "concurrency": 2})
            db.close()

            seen = []

            class _LocalResult:
                operation = "backfill_all"
                status = "SUCCESS"
                api_calls = 1
                rows_fetched = 0
                rows_inserted = 0
                rows_skipped = 0
                errors = []
                metadata = {}

            class _FakeOrch:
                def __init__(self, db, client, *, force=False, rate_limiter=None):
                    seen.append(
                        {
                            "limiter": rate_limiter,
                            "concurrency": rate_limiter.concurrency,
                            "ceiling": rate_limiter.config.initial_concurrency,
                            "interval": rate_limiter.interval,
                        }
                    )

                async def run_all(
                    self, *, stages=None, nifty_start_date=None, options_concurrency=None
                ):
                    # Job 1 earns adaptive state AFTER prepare ran.
                    if seen[-1]["ceiling"] == 5:
                        seen[-1]["limiter"]._interval = 3.3
                    return _LocalResult()

            monkeypatch.setattr(orch_mod, "BackfillOrchestrator", _FakeOrch)
            monkeypatch.setattr(orch_mod, "TokenBridge", type("B", (), {}))
            monkeypatch.setattr(
                upstox_mod,
                "UpstoxClient",
                type("C", (), {"__init__": lambda self, token_provider=None: None}),
            )
            summary = bj.run_worker(session_factory=factory, once=True)
            assert summary["succeeded"] == 2
            assert len(seen) == 2
            assert seen[0]["limiter"] is seen[1]["limiter"], (
                "the worker shares one limiter across jobs"
            )
            assert seen[0]["ceiling"] == 5 and seen[0]["concurrency"] == 5
            assert seen[1]["ceiling"] == 2 and seen[1]["concurrency"] == 2, (
                "job 2's explicit concurrency must be applied, not inherited"
            )
            assert seen[1]["interval"] == 3.3, (
                "per-job ceiling application must not reset adaptive state"
            )
        finally:
            engine.dispose()

    def test_c_cooldown_survives_job_boundary(self, session_factory, monkeypatch):
        """Job 1 takes a genuine 429 cooldown; job 2 starts through the
        same worker and still sees the remaining cooldown and the widened
        pacing interval (a fresh limiter would show neither)."""
        import asyncio as _asyncio

        factory, engine = _shared_memory_sqlite_factory()
        try:
            db = factory()
            _enqueue(db, "lim:cool:1")
            _enqueue(db, "lim:cool:2")
            db.close()

            observed = []

            def fake_execute(db, job, **kwargs):
                limiter = kwargs["rate_limiter"]
                if job.idempotency_key == "lim:cool:1":
                    # Real limiter mechanics: 30s Retry-After cooldown.
                    _asyncio.run(limiter.on_429(retry_after=30.0))
                else:
                    observed.append(
                        {
                            "cooldown": limiter.cooldown_remaining,
                            "interval": limiter.interval,
                            "consecutive_429s": limiter._consecutive_429s,
                        }
                    )

            monkeypatch.setattr(bj, "execute_job", fake_execute)
            summary = bj.run_worker(session_factory=factory, once=True)
            assert summary["succeeded"] == 2
            assert len(observed) == 1
            assert observed[0]["cooldown"] > 25.0, (
                f"job 2 must inherit the remaining cooldown: {observed}"
            )
            # on_429 widens pacing to min(config.max_interval, cooldown) —
            # the default ceiling is 5.0s; a fresh limiter would show the
            # 0.25s initial interval instead.
            assert observed[0]["interval"] == 5.0, (
                "pacing interval widened by the 429 must persist"
            )
            assert observed[0]["consecutive_429s"] == 1
        finally:
            engine.dispose()

    def test_e_shared_limiter_rebinds_async_lock_between_job_loops(self):
        """A worker-lifetime limiter may cross asyncio.run boundaries.

        Adaptive/cooldown state must remain on the shared limiter, but the
        pacing mutex must follow the current event loop. Two concurrent
        acquisitions in each loop force the lock onto that loop; the second
        loop must not raise a cross-event-loop RuntimeError.
        """
        import asyncio
        import time

        from app.services.rate_limiter import GlobalRateLimiter, RateLimiterConfig

        limiter = GlobalRateLimiter(
            config=RateLimiterConfig(
                initial_concurrency=2,
                max_concurrency=2,
                initial_interval=0.01,
                min_interval=0.005,
                max_interval=0.1,
            )
        )

        async def exercise_current_job_loop():
            # Hold the pacing lock long enough for the second acquisition
            # to become a waiter, which binds the lock to this event loop.
            limiter._last_request = time.monotonic()
            await asyncio.gather(limiter.acquire(), limiter.acquire())
            limiter.release()
            limiter.release()

        asyncio.run(exercise_current_job_loop())
        asyncio.run(exercise_current_job_loop())

    def test_d_worker_lifecycles_have_independent_limiters(
        self, session_factory, monkeypatch
    ):
        """Two separate run_worker lifecycles get separate limiter objects
        (no process-wide singleton): state earned in worker 1 never leaks
        into worker 2."""
        factory1, engine1 = _shared_memory_sqlite_factory()
        factory2, engine2 = _shared_memory_sqlite_factory()
        try:
            db = factory1()
            _enqueue(db, "lim:w1")
            db.close()
            db = factory2()
            _enqueue(db, "lim:w2")
            db.close()

            observed = []

            def fake_execute(db, job, **kwargs):
                limiter = kwargs["rate_limiter"]
                if not observed:  # only worker 1's job "earns" state
                    limiter._interval = 4.2
                # Hold the OBJECT (not id()): the first worker's limiter
                # would otherwise be garbage-collected and CPython could
                # reuse its address for the second, faking equality.
                observed.append({"job": job.idempotency_key, "limiter": limiter})

            monkeypatch.setattr(bj, "execute_job", fake_execute)
            s1 = bj.run_worker(session_factory=factory1, once=True, worker_id="w-A")
            s2 = bj.run_worker(session_factory=factory2, once=True, worker_id="w-B")
            assert s1["succeeded"] == 1 and s2["succeeded"] == 1
            assert len(observed) == 2
            assert observed[0]["limiter"] is not observed[1]["limiter"], (
                "separate worker lifecycles must NOT share a limiter singleton"
            )
            assert observed[0]["limiter"].interval == 4.2
            assert observed[1]["limiter"].interval != 4.2, (
                "worker 2 must not inherit worker 1's adaptive state"
            )
        finally:
            engine1.dispose()
            engine2.dispose()


class TestCompletionRetry:
    """CodeRabbit Minor: the SUCCESS transition survives transient DB
    errors exactly like the failure transition (F7 parity)."""

    def test_serialization_failure_during_completion_is_retried(
        self, session_factory, monkeypatch
    ):
        bj.enqueue(
            session_factory(),
            job_type="HISTORICAL_INGESTION",
            idempotency_key="f2:complete:1",
        )
        calls = {"n": 0}
        real_complete = bj.complete_job

        def flaky_complete(db, job, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                orig = Exception("restart transaction")
                orig.sqlstate = "40001"
                from sqlalchemy.exc import OperationalError

                raise OperationalError("UPDATE background_jobs ...", {}, orig)
            return real_complete(db, job, **kwargs)

        monkeypatch.setattr(bj, "complete_job", flaky_complete)
        # Successful (fake) execution; only the transition is under test.
        monkeypatch.setattr(bj, "execute_job", lambda db, job, **kwargs: {"ok": True})
        summary = bj.run_worker(session_factory=session_factory, once=True)
        assert calls["n"] == 2, "completion must be retried, not fatal"
        assert summary["succeeded"] == 1
        db = session_factory()
        row = db.scalar(select(BackgroundJob))
        assert row.status == JobStatus.SUCCEEDED.value

    def test_completion_uses_fresh_session_after_execution_session_closed(
        self, session_factory, monkeypatch
    ):
        """The completion transition must NOT run on the execution session."""
        bj.enqueue(
            session_factory(),
            job_type="HISTORICAL_INGESTION",
            idempotency_key="f2:complete:2",
        )
        seen = {}

        def record_session(db, job, **kwargs):
            seen["execution_session"] = db
            return {"ok": True}

        monkeypatch.setattr(bj, "execute_job", record_session)
        sessions_used_for_completion = []
        real_complete = bj.complete_job

        def spy_complete(db, job, **kwargs):
            sessions_used_for_completion.append(db)
            return real_complete(db, job, **kwargs)

        monkeypatch.setattr(bj, "complete_job", spy_complete)
        summary = bj.run_worker(session_factory=session_factory, once=True)
        assert summary["succeeded"] == 1
        assert len(sessions_used_for_completion) == 1
        assert sessions_used_for_completion[0] is not seen["execution_session"], (
            "completion must run on a FRESH session, not the execution session"
        )

    def test_stale_worker_cannot_complete_replacement_attempt(
        self, session_factory, monkeypatch
    ):
        """After a mid-execution takeover the retried completion still
        refuses to touch the replacement attempt (ownership preserved)."""
        bj.enqueue(
            session_factory(),
            job_type="HISTORICAL_INGESTION",
            idempotency_key="f2:complete:3",
        )

        def steal_lease(db, job, **kwargs):
            s = session_factory()
            row = s.scalar(select(BackgroundJob))
            row.lease_expires_at = _utcnow() - timedelta(seconds=1)
            s.commit()
            reclaimer = session_factory()
            assert bj.claim_next(reclaimer, worker_id="worker-B") is not None
            reclaimer.close()
            s.close()

        monkeypatch.setattr(bj, "execute_job", steal_lease)
        summary = bj.run_worker(
            session_factory=session_factory, once=True, worker_id="worker-A"
        )
        assert summary["stale"] == 1
        assert summary["succeeded"] == 0
        db = session_factory()
        row = db.scalar(select(BackgroundJob))
        assert row.status == JobStatus.RUNNING.value
        assert row.lease_owner == "worker-B"
        assert row.completed_at is None


class TestHeartbeatLeasePropagation:
    """F3: execution/heartbeat/completion use the SAME effective lease.

    The default 900s lease is far longer than any test runs, so these tests
    use distinct leases per precedence source and observe the heartbeat's
    renewal arguments and the claim's stored expiry.
    """

    def _claim_and_inspect(self, session_factory, monkeypatch, *, lease_arg, payload):
        renew_args = []
        real_renew = bj.renew_lease

        def spying_renew(db, job_id, *, worker_id, lease_seconds):
            renew_args.append(lease_seconds)
            return real_renew(
                db, job_id, worker_id=worker_id, lease_seconds=lease_seconds
            )

        monkeypatch.setattr(bj, "renew_lease", spying_renew)
        bj.enqueue(
            session_factory(),
            job_type="HISTORICAL_INGESTION",
            idempotency_key=f"f3:lease:{lease_arg}:{json.dumps(payload, sort_keys=True)}",
            payload=payload,
        )
        started = {}

        def capturing_execute(db, job, **kwargs):
            started["started_at"] = job.started_at
            started["lease_expires_at"] = job.lease_expires_at

        monkeypatch.setattr(bj, "execute_job", capturing_execute)
        bj.run_worker(session_factory=session_factory, once=True)
        db = session_factory()
        row = db.scalar(select(BackgroundJob))
        claim_delta = (started["lease_expires_at"] - started["started_at"]).total_seconds()
        return row, claim_delta, renew_args

    def test_default_lease_used_end_to_end(self, session_factory, monkeypatch):
        row, claim_delta, renew_args = self._claim_and_inspect(
            session_factory, monkeypatch, lease_arg=None, payload={}
        )
        assert 899 <= claim_delta <= 901
        assert row.status == JobStatus.SUCCEEDED.value

    def test_payload_policy_lease_used_end_to_end(self, session_factory, monkeypatch):
        row, claim_delta, _ = self._claim_and_inspect(
            session_factory,
            monkeypatch,
            lease_arg=None,
            payload={"policy": {"lease_seconds": 7777}},
        )
        assert 7770 <= claim_delta <= 7790
        assert row.status == JobStatus.SUCCEEDED.value

    def test_explicit_override_beats_payload_end_to_end(
        self, session_factory, monkeypatch
    ):
        bj.enqueue(
            session_factory(),
            job_type="HISTORICAL_INGESTION",
            idempotency_key="f3:lease:override",
            payload={"policy": {"lease_seconds": 7777}},
        )
        claim = {}

        def capturing_execute(db, job, **kwargs):
            claim["delta"] = (
                job.lease_expires_at - job.started_at
            ).total_seconds()

        monkeypatch.setattr(bj, "execute_job", capturing_execute)
        bj.run_worker(
            session_factory=session_factory,
            once=True,
            worker_id="w1",
            lease_seconds=42,
        )
        assert 41 <= claim["delta"] <= 43  # explicit override won at claim
        db = session_factory()
        row = db.scalar(select(BackgroundJob))
        assert row.status == JobStatus.SUCCEEDED.value

    def test_heartbeat_renews_with_the_selected_value(
        self, session_factory, monkeypatch
    ):
        """The heartbeat's renewal duration equals the claim's effective
        lease (payload-policy case exercised through the real worker)."""
        renew_args = []
        real_renew = bj.renew_lease

        def spying_renew(db, job_id, *, worker_id, lease_seconds):
            renew_args.append(lease_seconds)
            return real_renew(
                db, job_id, worker_id=worker_id, lease_seconds=lease_seconds
            )

        monkeypatch.setattr(bj, "renew_lease", spying_renew)
        factory, engine = _shared_memory_sqlite_factory()
        try:
            db = factory()
            _enqueue(db, "f3:hb:value", payload={"policy": {"lease_seconds": 3}})
            db.close()

            def slow_execute(db, job, **kwargs):
                import time as _time

                _time.sleep(2.2)  # spans >= 2 heartbeat cycles (1.0s period)

            monkeypatch.setattr(bj, "execute_job", slow_execute)
            bj.run_worker(session_factory=factory, once=True)
            assert renew_args, "heartbeat must have attempted renewal"
            assert set(renew_args) == {3}, (
                f"heartbeat must renew with the effective lease (3), got {renew_args}"
            )
        finally:
            engine.dispose()


class TestCliLeaseOverrideDefault:
    """CodeRabbit outside-diff Minor: --lease-seconds defaults to None so
    the documented lease precedence (explicit > payload > default) holds
    for CLI workers too — a CLI process without the flag must NOT force
    the system default over a job's payload policy."""

    def test_cli_work_without_flag_passes_no_override(self, tmp_path, monkeypatch):
        import run_jobs

        db_path = tmp_path / "cli-lease.db"
        engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(bind=engine)

        captured = {}

        def fake_run_worker(**kwargs):
            captured.update(kwargs)
            return {"claimed": 0, "succeeded": 0, "failed": 0,
                    "dead_lettered": 0, "stale": 0}

        monkeypatch.setattr(run_jobs.settings, "DATABASE_URL", f"sqlite:///{db_path}",
                            raising=False)
        monkeypatch.setattr(bj, "run_worker", fake_run_worker)
        monkeypatch.setattr(
            "sys.argv", ["run_jobs.py", "work", "--once"]
        )
        assert run_jobs.main() == 0
        # The CLI must NOT inject the system default as an explicit override:
        # claim_next then applies payload policy > default per documentation.
        assert captured["lease_seconds"] is None

    def test_cli_work_with_flag_passes_override(self, tmp_path, monkeypatch):
        import run_jobs

        db_path = tmp_path / "cli-lease2.db"
        engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(bind=engine)

        captured = {}

        def fake_run_worker(**kwargs):
            captured.update(kwargs)
            return {"claimed": 0, "succeeded": 0, "failed": 0,
                    "dead_lettered": 0, "stale": 0}

        monkeypatch.setattr(run_jobs.settings, "DATABASE_URL", f"sqlite:///{db_path}",
                            raising=False)
        monkeypatch.setattr(bj, "run_worker", fake_run_worker)
        monkeypatch.setattr(
            "sys.argv", ["run_jobs.py", "work", "--once", "--lease-seconds", "42"]
        )
        assert run_jobs.main() == 0
        assert captured["lease_seconds"] == 42


class TestCrashRecoveryWithHeartbeat:
    """A crashed process stops renewing; the expired lease is reclaimable.
    (Recovery itself is already proven by TestRestartRecovery and the
    real-engine restart tests — here we prove the heartbeat does not
    interfere with it.)"""

    def test_no_heartbeat_after_process_death_lease_reclaimable(
        self, tmp_path
    ):
        import time as _time

        from sqlalchemy.pool import StaticPool

        db_path = tmp_path / "hb-crash.db"
        url = f"sqlite:///{db_path}"

        def make_factory():
            engine = create_engine(
                url,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            Base.metadata.create_all(bind=engine)
            return sessionmaker(bind=engine, autocommit=False, autoflush=False), engine

        factory1, engine1 = make_factory()
        db = factory1()
        _enqueue(db, "crash:hb:1", payload={"policy": {"lease_seconds": 1}})
        job = bj.claim_next(db, worker_id="worker-crashed", lease_seconds=1)
        assert job is not None
        db.close()
        engine1.dispose()  # process death: heartbeat thread dies with it

        # No heartbeat is running -> the 1s lease actually expires.
        _time.sleep(1.3)

        factory2, engine2 = make_factory()
        try:
            db2 = factory2()
            recovered = bj.claim_next(db2, worker_id="worker-new")
            assert recovered is not None
            assert recovered.idempotency_key == "crash:hb:1"
            assert recovered.attempt_count == 2
            assert recovered.lease_owner == "worker-new"
        finally:
            engine2.dispose()
