"""Day 47 — real-database integration tests for the durable job queue.

Runs only when real disposable databases are configured:

* ``TEST_DATABASE_URL`` — PostgreSQL 18 (psycopg 3 dialect)
* ``TEST_COCKROACHDB_URL`` — CockroachDB (psycopg dialect)

Proves on real engines what in-memory SQLite cannot:

* **Concurrent claim exclusivity** — many threads racing ``claim_next``
  produce exactly one claim per job attempt (no double ownership, no
  lost jobs), including under CockroachDB serializable contention.
* **Enqueue race** — concurrent duplicate enqueues collapse onto ONE row
  through the real unique constraint.
* **Restart recovery** — a worker "process" (engine) is disposed with a
  claimed job outstanding; a new engine recovers it after lease expiry.
* **Migration chain** — the full Alembic chain from an empty database
  creates ``background_jobs`` with every contract column and a live
  unique constraint, and the service works against the MIGRATED schema.

The job table is created through ``Base.metadata.create_all`` for the
queue-semantics tests (fast); the migration-chain tests verify the
authoritative Alembic path separately on scratch databases.
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.db import Base, normalize_database_url
from app.models import BackgroundJob
from app.services import background_jobs as bj


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _required_url(env_var: str, prefix: str, description: str) -> str:
    raw = os.getenv(env_var)
    if not raw:
        pytest.skip(f"{env_var} is not configured ({description})")
    url = normalize_database_url(raw)
    if not url.startswith(prefix):
        pytest.fail(f"{env_var} must resolve to the {prefix} dialect, got {url.split('://')[0]}://")
    return url


def _pg_url() -> str:
    return _required_url(
        "TEST_DATABASE_URL", "postgresql+psycopg://", "real disposable PostgreSQL"
    )


def _crdb_url() -> str:
    return _required_url(
        "TEST_COCKROACHDB_URL", "cockroachdb+psycopg://", "real disposable CockroachDB"
    )


def _cleanup(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM background_jobs"))


class _RealQueueBase:
    """Shared helpers for real-database queue tests."""

    def make_stack(self, url: str):
        engine = create_engine(url, pool_pre_ping=True)
        Base.metadata.create_all(bind=engine)
        return engine, sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def run_concurrent_claim_test(self, url: str, *, workers: int, rounds: int, jobs: int) -> None:
        engine, factory = self.make_stack(url)
        try:
            db = factory()
            keys = [f"{engine.dialect.name}:job:{i}" for i in range(jobs)]
            for key in keys:
                bj.enqueue(
                    db, job_type="HISTORICAL_INGESTION", idempotency_key=key
                )
            db.close()

            from app.utils.retry import retry_on_serialization

            claims: list[str] = []
            claims_lock = threading.Lock()

            def worker(wid: int) -> None:
                for _ in range(rounds):
                    session = factory()

                    def _claim(s):
                        claimed = bj.claim_next(s, worker_id=f"{engine.dialect.name}-w-{wid}")
                        return claimed.idempotency_key if claimed is not None else None

                    try:
                        key = retry_on_serialization(_claim, factory)
                        if key is not None:
                            with claims_lock:
                                claims.append(key)
                    finally:
                        session.close()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            # Every job claimed EXACTLY once: no double ownership, no losses.
            assert len(claims) == len(set(claims)), (
                f"a job attempt was claimed more than once: {sorted(claims)}"
            )
            assert set(claims) == set(keys)
            db = factory()
            rows = db.execute(select(BackgroundJob)).scalars().all()
            assert all(
                row.status == "RUNNING" and row.attempt_count == 1 for row in rows
            )
            owners = {row.lease_owner for row in rows}
            assert None not in owners
            db.close()
        finally:
            _cleanup(engine)
            engine.dispose()

    def run_enqueue_race_test(self, url: str, *, threads: int) -> None:
        from app.utils.retry import retry_on_serialization

        engine, factory = self.make_stack(url)
        try:
            barrier = threading.Barrier(threads)
            ids: list[str] = []
            ids_lock = threading.Lock()

            def enqueue_same_key() -> None:
                barrier.wait()  # maximize the collision window
                session = factory()

                def _enqueue(s):
                    job, _created = bj.enqueue(
                        s,
                        job_type="HISTORICAL_INGESTION",
                        idempotency_key=f"{engine.dialect.name}:race:1",
                    )
                    return job.id

                try:
                    # Producer-side transactions follow the repository's
                    # documented convention: the caller wraps the operation
                    # with the CRDB serialization retry (same as claims).
                    job_id = retry_on_serialization(_enqueue, factory)
                    with ids_lock:
                        ids.append(job_id)
                finally:
                    session.close()

            workers = [threading.Thread(target=enqueue_same_key) for _ in range(threads)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            assert len(set(ids)) == 1, f"racing enqueues produced different rows: {ids}"
            db = factory()
            total = db.scalar(select(func.count(BackgroundJob.id)))
            assert total == 1
            db.close()
        finally:
            _cleanup(engine)
            engine.dispose()

    def run_restart_recovery_test(self, url: str) -> None:
        key = f"{engine_key(url)}:crash:1"

        # "Process 1": enqueue, claim, then die with the lease outstanding.
        engine1, factory1 = self.make_stack(url)
        try:
            db = factory1()
            bj.enqueue(db, job_type="HISTORICAL_INGESTION", idempotency_key=key)
            # The crash scenario REQUIRES a successful claim: assert it.
            assert bj.claim_next(db, worker_id="w-crashed", lease_seconds=30) is not None
            db.close()
            # Backdate the lease deterministically (the crashed worker will
            # never renew it), then hard-kill the "process".
            db2 = factory1()
            row = db2.scalar(
                select(BackgroundJob).where(BackgroundJob.idempotency_key == key)
            )
            row.lease_expires_at = _utcnow() - timedelta(seconds=1)
            db2.commit()
            db2.close()
        finally:
            engine1.dispose()  # process 1 terminated

        # "Process 2": a brand-new engine/session recovers the orphan.
        engine2, factory2 = self.make_stack(url)
        try:
            session = factory2()
            recovered = bj.claim_next(session, worker_id="w-new")
            # Read attributes while the session is open (instances detach on close).
            recovered_key = recovered.idempotency_key
            recovered_attempts = recovered.attempt_count
            recovered_owner = recovered.lease_owner
            session.close()
            assert recovered_key == key
            assert recovered_attempts == 2  # crash attempt + recovery attempt
            assert recovered_owner == "w-new"
            session = factory2()
            row = session.scalar(
                select(BackgroundJob).where(BackgroundJob.idempotency_key == key)
            )
            assert row.status == "RUNNING"
            assert row.lease_owner == "w-new"
            session.close()
        finally:
            _cleanup(engine2)
            engine2.dispose()


def engine_key(url: str) -> str:
    return "pg" if url.startswith("postgresql") else "crdb"


# ---------------------------------------------------------------------------
# Dedicated queue databases.
#
# The queue-semantics tests create ALL ORM tables via create_all, which
# would pollute the shared rehearsal targets (``rehearsal`` on CockroachDB,
# ``strikenova_test`` on PostgreSQL) and break the migration-rehearsal
# suites' chain-from-empty assumption. Day 47 therefore runs against its
# OWN disposable databases, created from and dropped after the session.
# ---------------------------------------------------------------------------

PG_QUEUE_DB = "strikenova_jobs_queue"
CRDB_QUEUE_DB = "jobs_queue_test"


@pytest.fixture(scope="session")
def pg_queue_url():
    base = _pg_url()
    _create_scratch(base, "postgresql", PG_QUEUE_DB)
    yield _scratch_url(base, PG_QUEUE_DB)
    _drop_scratch(base, "postgresql", PG_QUEUE_DB)


@pytest.fixture(scope="session")
def crdb_queue_url():
    base = _crdb_url()
    _create_scratch(base, "cockroachdb", CRDB_QUEUE_DB)
    yield _scratch_url(base, CRDB_QUEUE_DB)
    _drop_scratch(base, "cockroachdb", CRDB_QUEUE_DB)


class TestPostgresDurableQueue(_RealQueueBase):
    def test_concurrent_claims_are_exactly_once(self, pg_queue_url):
        self.run_concurrent_claim_test(pg_queue_url, workers=8, rounds=4, jobs=12)

    def test_concurrent_duplicate_enqueue_collapses_to_one_row(self, pg_queue_url):
        self.run_enqueue_race_test(pg_queue_url, threads=6)

    def test_worker_crash_is_recovered_after_restart(self, pg_queue_url):
        self.run_restart_recovery_test(pg_queue_url)

    def test_real_orchestrator_boundary_fails_closed_without_credentials(
        self, pg_queue_url
    ):
        """Vertical slice: the WORKER drives the REAL BackfillOrchestrator
        through the application service boundary (never a subprocess).

        Without stored broker credentials the orchestrator fails closed at
        the authentication boundary (zero API calls) and the job lands in
        the inspectable dead-letter state as NON-retryable — the exact
        semantics an unattended worker must have.
        """
        from app.services.backfill_orchestrator import TokenBridge

        if TokenBridge().get_token() is not None:
            pytest.skip(
                "a real Upstox token is stored on this machine; refusing to "
                "exercise the real ingestion boundary in a test"
            )
        url = pg_queue_url
        engine, factory = self.make_stack(url)
        try:
            db = factory()
            bj.enqueue(
                db,
                job_type="HISTORICAL_INGESTION",
                idempotency_key="pg:real-boundary:1",
                payload={"stages": ["contracts"]},
            )
            db.close()
            summary = bj.run_worker(session_factory=factory, once=True)
            assert summary["claimed"] == 1
            assert summary["dead_lettered"] == 1
            db = factory()
            row = db.scalar(
                select(BackgroundJob).where(
                    BackgroundJob.idempotency_key == "pg:real-boundary:1"
                )
            )
            assert row.status == "DEAD_LETTERED"
            assert "non-retryable" in (row.dead_letter_reason or "")
            assert row.last_error  # inspectable failure evidence
            db.close()
        finally:
            _cleanup(engine)
            engine.dispose()


class TestCockroachDBDurableQueue(_RealQueueBase):
    def test_concurrent_claims_are_exactly_once_under_serializable_contention(
        self, crdb_queue_url
    ):
        self.run_concurrent_claim_test(crdb_queue_url, workers=10, rounds=5, jobs=15)

    def test_concurrent_duplicate_enqueue_collapses_to_one_row(self, crdb_queue_url):
        self.run_enqueue_race_test(crdb_queue_url, threads=6)

    def test_worker_crash_is_recovered_after_restart(self, crdb_queue_url):
        self.run_restart_recovery_test(crdb_queue_url)


# ---------------------------------------------------------------------------
# Alembic migration chain (authoritative schema path) on scratch databases
# ---------------------------------------------------------------------------


def _scratch_url(url: str, scratch_db: str) -> str:
    """Point a database URL at a different database (keep host/auth/query)."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    # urlunsplit adds the '?' itself; do not prepend one manually.
    return urlunsplit((parts.scheme, parts.netloc, f"/{scratch_db}", parts.query, ""))


def _bootstrap_db(dialect: str) -> str:
    """Maintenance database used to create/drop scratch databases."""
    return "postgres" if dialect == "postgresql" else "defaultdb"


# Strict SQL identifier whitelist: letters/digits/underscores only, must not
# start with a digit, bounded length (PostgreSQL/CRDB limit is 63). Every
# name interpolated into CREATE/DROP DATABASE DDL MUST pass this first —
# database identifiers cannot be bind parameters, so validation + quoting
# is the safety boundary (test-only infrastructure, but explicitly safe).
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _validate_identifier(name: str) -> str:
    if not _SAFE_IDENTIFIER.match(name):
        raise ValueError(
            f"unsafe database identifier rejected: {name!r} "
            "(must match [A-Za-z_][A-Za-z0-9_]{0,62})"
        )
    return name


def _create_scratch(url: str, dialect: str, scratch_db: str) -> None:
    """Recreate the scratch database so the chain starts from truly empty."""
    _validate_identifier(scratch_db)
    bootstrap = _scratch_url(url, _bootstrap_db(dialect))
    engine = create_engine(bootstrap, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
            conn.execute(text(f'CREATE DATABASE "{scratch_db}"'))
    finally:
        engine.dispose()


def _drop_scratch(url: str, dialect: str, scratch_db: str) -> None:
    _validate_identifier(scratch_db)
    bootstrap = _scratch_url(url, _bootstrap_db(dialect))
    engine = create_engine(bootstrap, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
    finally:
        engine.dispose()


def _migration_matrix():
    cases = []
    if os.getenv("TEST_DATABASE_URL"):
        cases.append(
            pytest.param(
                "postgresql", _scratch_url(_pg_url(), "strikenova_jobs_mig"), id="postgresql"
            )
        )
    if os.getenv("TEST_COCKROACHDB_URL"):
        cases.append(
            pytest.param(
                "cockroachdb",
                _scratch_url(_crdb_url(), "jobs_migration_test"),
                id="cockroachdb",
            )
        )
    return cases


@pytest.mark.parametrize("dialect,url", _migration_matrix())
def test_alembic_chain_creates_working_background_jobs(dialect, url):
    import os as _os

    from alembic import command
    from alembic.config import Config

    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    cfg = Config(_os.path.join(root, "alembic.ini"))
    cfg.set_main_option("script_location", _os.path.join(root, "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)

    from urllib.parse import urlsplit

    scratch_db = urlsplit(url).path.lstrip("/")
    _create_scratch(url, dialect, scratch_db)

    # Authoritative path: full chain from an EMPTY scratch database.
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            columns = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'background_jobs'"
                    )
                )
            }
        expected = {
            "id",
            "job_type",
            "idempotency_key",
            "payload",
            "user_scope",
            "status",
            "attempt_count",
            "available_at",
            "lease_owner",
            "lease_expires_at",
            "started_at",
            "completed_at",
            "last_error",
            "dead_letter_reason",
            "created_at",
            "updated_at",
        }
        missing = expected - columns
        assert not missing, f"migration is missing columns: {sorted(missing)}"

        # The service works against the MIGRATED schema (no create_all here).
        factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        db = factory()
        job, created = bj.enqueue(
            db,
            job_type="HISTORICAL_INGESTION",
            idempotency_key=f"mig:{dialect}:1",
        )
        assert created is True
        claimed = bj.claim_next(db, worker_id="migration-check")
        assert claimed is not None
        bj.complete_job(db, claimed)
        db.close()

        # The idempotency unique constraint is REAL on this engine.
        db = factory()
        duplicate = BackgroundJob(
            id=str(uuid.uuid4()),
            job_type="HISTORICAL_INGESTION",
            idempotency_key=f"mig:{dialect}:1",
            payload="{}",
            status="PENDING",
            attempt_count=0,
            available_at=_utcnow(),
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        db.add(duplicate)
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            db.commit()
        db.close()
    finally:
        engine.dispose()
        # Tear the scratch database down completely for a clean next run.
        _drop_scratch(url, dialect, scratch_db)
