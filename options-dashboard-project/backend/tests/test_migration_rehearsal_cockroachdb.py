"""ADR-017 migration rehearsal against a REAL CockroachDB server.

Companion to ``test_migration_rehearsal_postgres.py``: the same rehearsal
matrix (lock lifecycle, live-holder contention, cross-process fail-closed,
SIGKILL crash recovery, takeover, and the real Alembic chain) executed against
a disposable, non-production CockroachDB target.

Runs only when ``TEST_COCKROACHDB_URL`` points at a disposable CockroachDB
database (single-node insecure ``cockroach start-single-node`` is explicitly
supported; no production credentials are involved). Skipped otherwise so
hermetic/local/CI runs are unaffected.

As with the PostgreSQL rehearsal, this file exercises the PRODUCTION
implementations — ``app.db.resolve_migration_database_url``,
``app._migration_lock`` (the same module ``_execute_serialized`` uses) and
``app.db._migration_lock_url`` — with no reimplementation of resolver,
normalization, or lock semantics.

Bandit/Codacy note: checks use explicit ``raise AssertionError`` so the
verification survives ``python -O`` (no reliance on plain ``assert``).
"""
from __future__ import annotations

import concurrent.futures
import os
import threading
import time
import uuid
from datetime import timedelta

import pytest
from alembic import command
from sqlalchemy import create_engine, text

# Inherit the full rehearsal matrix (actors, handshake discipline, assertions)
# from the PostgreSQL rehearsal module — one framework, two engines. The base
# class is imported under a non-collectible alias so pytest does not collect
# the PostgreSQL-targeted class inside this CockroachDB module as well.
from tests.test_migration_rehearsal_postgres import (  # noqa: F401
    _check,
    _make_alembic_cfg,
)
from tests.test_migration_rehearsal_postgres import (
    TestPostgresMigrationRehearsal as _PostgresRehearsalBase,
)

from app import _migration_lock as mlock
from app.db import normalize_database_url, resolve_migration_database_url

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cockroach_test_url() -> str:
    raw = os.getenv("TEST_COCKROACHDB_URL")
    if not raw:
        pytest.skip(
            "TEST_COCKROACHDB_URL is not configured "
            "(rehearsal needs a real disposable CockroachDB)"
        )
    url = normalize_database_url(raw)
    if not url.startswith("cockroachdb+psycopg://"):
        pytest.fail(
            "TEST_COCKROACHDB_URL must resolve to the CockroachDB SQLAlchemy "
            "dialect (cockroachdb+psycopg://), got: "
            + url.split("://")[0]
            + "://"
        )
    return url


class TestCockroachDBMigrationRehearsal(_PostgresRehearsalBase):
    """ADR-017 matrix on real CockroachDB.

    Every inherited test (resolver identity, full lock lifecycle, waiter
    fail-closed, two-process contention, SIGKILL takeover, Alembic chain)
    runs unchanged against the CockroachDB target — only the URL fixture is
    retargeted. The base class asserts against the production lock module,
    so no CockroachDB-specific lock behavior is reimplemented here.
    """

    # Same timing discipline as the PostgreSQL rehearsal: TTL 15s, renewal
    # every max(1.0, ttl/3) ≈ 5s, waiter budget 7s (< TTL, >= 1 renewal
    # interval) so fail-closed is proven against a genuinely live holder.
    REHEARSAL_TTL = 15
    WAITER_BUDGET = 7

    @pytest.fixture(scope="class")
    def migration_url(self):
        return _cockroach_test_url()


class TestCockroachDBSpecificEvidence:
    """Evidence only a real CockroachDB can provide.

    These tests prove the environment is genuinely CockroachDB (not
    PostgreSQL wearing a URL), that the lock travels with the migration
    identity on separate databases, and that CockroachDB's 40001
    serialization-retry handling holds on the real lock table.
    """

    @pytest.fixture(scope="class")
    def crdb_url(self):
        return _cockroach_test_url()

    @pytest.fixture(scope="class")
    def verify_engine(self, crdb_url):
        engine = create_engine(crdb_url, pool_pre_ping=True)
        try:
            yield engine
        finally:
            engine.dispose()

    def test_target_is_real_cockroachdb_serializable(self, verify_engine):
        """Prove the rehearsal target is CockroachDB under serializable
        isolation — not a PostgreSQL instance behind a cockroachdb:// URL."""
        with verify_engine.connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar_one()
            isolation = conn.execute(
                text("SHOW TRANSACTION ISOLATION LEVEL")
            ).scalar_one()
        _check(
            "cockroach" in str(version).lower(),
            f"target must be CockroachDB, version() = {version!r}",
        )
        _check(
            str(isolation).lower() == "serializable",
            f"CockroachDB must run serializable isolation, got {isolation!r}",
        )

    def test_lock_travels_with_migration_identity_across_databases(
        self, monkeypatch
    ):
        """ADR-016/ADR-017 identity coupling on CockroachDB: with a distinct
        runtime database and migration database, acquiring through the
        resolver-selected migration URL must persist the lock row in the
        MIGRATION database only — the runtime database must never gain a
        _migration_lock table."""
        runtime_db = os.getenv("TEST_COCKROACHDB_RUNTIME_URL")
        migration_db = os.getenv("TEST_COCKROACHDB_URL")
        if not runtime_db:
            pytest.skip(
                "TEST_COCKROACHDB_RUNTIME_URL not configured; "
                "identity-coupling needs two CockroachDB databases"
            )
        runtime_url = normalize_database_url(runtime_db)
        migration_url = normalize_database_url(migration_db)
        _check(
            runtime_url != migration_url,
            "runtime and migration identity URLs must be distinct databases",
        )

        monkeypatch.setenv("DATABASE_URL", runtime_url)
        monkeypatch.setenv("STRIKENOVA_MIGRATION_DATABASE_URL", migration_url)
        from unittest.mock import patch

        from app.config import Settings
        from app import db as db_module

        s = Settings()
        with patch("app.db.settings", s):
            resolved = resolve_migration_database_url()
            lock_target = db_module._migration_lock_url()
        _check(
            resolved == migration_url,
            "resolver must select the migration identity on CockroachDB",
        )
        _check(
            lock_target == migration_url,
            "the lock URL must equal the migration identity (ADR-017 coupling)",
        )

        owner = f"crdb-identity:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        acquired, took_over = mlock.try_acquire(lock_target, owner, 15)
        try:
            _check(acquired, "lock acquisition on the migration identity must succeed")
            _check(not took_over, "a fresh database must not report a takeover")
            mig_engine = create_engine(migration_url, pool_pre_ping=True)
            try:
                with mig_engine.connect() as conn:
                    held = conn.execute(
                        text("SELECT locked_by FROM _migration_lock")
                    ).scalar_one()
            finally:
                mig_engine.dispose()
            _check(
                held == owner,
                f"lock must be persisted under the migration identity, got {held!r}",
            )
            # The runtime database must NOT have been touched by the lock.
            rt_engine = create_engine(runtime_url, pool_pre_ping=True)
            try:
                with rt_engine.connect() as conn:
                    rows = conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'public' "
                            "AND table_name = '_migration_lock'"
                        )
                    ).fetchall()
            finally:
                rt_engine.dispose()
            _check(
                not rows,
                "runtime database must never receive the migration lock table",
            )
        finally:
            held_now = mlock.current_holder(lock_target)
            if held_now:
                mlock.release(lock_target, held_now)

    def test_serialization_conflicts_occur_and_are_40001(self, crdb_url):
        """Force real concurrent writers onto the single lock row and prove
        CockroachDB raises SQLSTATE 40001 (the exact transient error class
        the production acquire/bootstrap/renew paths retry) — then prove the
        production lock functions survive the same contention without ever
        double-holding the lease."""
        engine = create_engine(crdb_url, pool_pre_ping=True)
        try:
            # Bootstrap through the production path so the table exists.
            boot_owner = f"crdb-40001-boot:{uuid.uuid4().hex[:8]}"
            acquired, _ = mlock.try_acquire(crdb_url, boot_owner, 15)
            _check(acquired, "bootstrap acquisition must succeed")
            _check(mlock.release(crdb_url, boot_owner), "bootstrap release must succeed")

            # (a) Raw concurrent renewals against one row: losers get 40001.
            serialization_hits = []
            other_errors = []

            def raw_renew(owner_tag: str) -> None:
                conn = mlock._connect(crdb_url)
                try:
                    cur = conn.cursor()
                    # Read-modify-write on the single lock row: the initial
                    # read takes a timestamp cache entry, so an overlapping
                    # writer's UPDATE must be RESTARTED by CockroachDB
                    # (SQLSTATE 40001) rather than silently queued — this is
                    # exactly the transient error class the production
                    # bootstrap/acquire/renew paths retry. (A blind
                    # single-statement UPDATE only queues on the lock
                    # manager and would exercise nothing.)
                    cur.execute("SELECT expires_at FROM _migration_lock WHERE _id = true")
                    cur.fetchone()
                    time.sleep(0.012)  # widen the read-write conflict window
                    cur.execute(
                        "UPDATE _migration_lock "
                        "SET expires_at = now() + interval '15 seconds' "
                        "WHERE _id = true"
                    )
                    conn.commit()
                except Exception as exc:  # noqa: BLE001 - classification IS the test
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001 - conn may already be broken
                        pass
                    if mlock._is_serialization_failure(exc):
                        serialization_hits.append(
                            f"{type(exc).__name__}:sqlstate={getattr(exc, 'sqlstate', None)}"
                        )
                    else:
                        other_errors.append(f"{type(exc).__name__}: {exc}")
                finally:
                    conn.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                list(pool.map(raw_renew, range(72)))

            _check(
                not other_errors,
                f"unexpected non-40001 errors under contention: {other_errors[:3]}",
            )
            _check(
                len(serialization_hits) > 0,
                "expected real CockroachDB 40001 serialization failures under "
                "concurrent single-row writes; none observed (contention too weak?)",
            )
            # Evidence: the observed failures are genuine 40001 restarts
            # (sqlstate attribute present on psycopg exceptions).
            _check(
                all("sqlstate=40001" in h for h in serialization_hits),
                f"all observed conflicts must carry SQLSTATE 40001, got {serialization_hits[:4]}",
            )

            # (b) The production acquire path under identical contention:
            # concurrent try_acquire/release rounds. At most one holder may
            # be live at any instant; every transient failure must be a 40001
            # class error (surviving internal retries), never anything else.
            live = set()
            live_lock = threading.Lock()
            max_live = [0]
            failures = []

            def hammer(worker_id: int) -> None:
                for _ in range(6):
                    owner = f"crdb-hammer:{worker_id}:{uuid.uuid4().hex[:8]}"
                    try:
                        acquired, _took = mlock.try_acquire(crdb_url, owner, 15)
                        if acquired:
                            with live_lock:
                                live.add(owner)
                                max_live[0] = max(max_live[0], len(live))
                            _check(
                                mlock.release(crdb_url, owner),
                                "hammer holder release must succeed",
                            )
                            with live_lock:
                                live.discard(owner)
                    except AssertionError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        if not mlock._is_serialization_failure(exc):
                            failures.append(f"{type(exc).__name__}: {exc}")
                        # 40001-class errors escaping try_acquire would indicate
                        # retry exhaustion under extreme contention; the lock is
                        # bounded by design, so record but only fail below if a
                        # non-40001 error appears or double-hold is observed.

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                list(pool.map(hammer, range(10)))

            _check(
                not failures,
                f"non-serialization errors from production lock under contention: {failures[:3]}",
            )
            _check(
                max_live[0] <= 1,
                f"lease must never be double-held under contention, saw {max_live[0]}",
            )
        finally:
            try:
                held = mlock.current_holder(crdb_url)
                if held:
                    mlock.release(crdb_url, held)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
            engine.dispose()

    def test_alembic_chain_idempotent_on_cockroachdb(
        self, crdb_url, monkeypatch, caplog
    ):
        """Phase 4 idempotence, end-to-end on the REAL Alembic path: the
        full chain applies from an empty CockroachDB database through the
        PRODUCTION serialized runner (``_execute_serialized`` — lease
        acquire -> upgrade -> release, the exact code the FastAPI lifespan
        executes at startup), then a SECOND full serialized run must be a
        no-op — same alembic_version, lock released, and the
        already-current observation logged."""
        import logging

        caplog.set_level(logging.INFO)
        monkeypatch.setenv("DATABASE_URL", crdb_url)
        monkeypatch.setenv("STRIKENOVA_MIGRATION_DATABASE_URL", crdb_url)
        from unittest.mock import patch

        from app.config import Settings

        s = Settings()
        with patch("app.db.settings", s):
            resolved = resolve_migration_database_url()
        _check(resolved == crdb_url, "resolver must resolve the CRDB rehearsal target")

        engine = create_engine(crdb_url, pool_pre_ping=True)
        try:
            # Phase 4 step 1-2: authoritative chain from empty state through
            # the production serialized runner (not a bare command.upgrade).
            cfg = _make_alembic_cfg(resolved)
            with patch("app.db.settings", s):
                from app import db as db_module

                db_module._execute_serialized(cfg, command, logging.getLogger("crdb-rehearsal"))

            with engine.connect() as conn:
                before = conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
            _check(bool(before), f"chain must be applied after first run, got {before!r}")

            # Phase 4 step 5-6: re-run and prove idempotence/no-op.
            with patch("app.db.settings", s):
                db_module._execute_serialized(cfg, command, logging.getLogger("crdb-rehearsal"))

            with engine.connect() as conn:
                after = conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
            _check(
                before == after,
                f"second migration run must be a no-op, {before!r} -> {after!r}",
            )
            no_op_logged = any(
                "migration already current" in r.message for r in caplog.records
            )
            _check(
                no_op_logged,
                "serialized run must log the already-current observation (no-op evidence)",
            )
            # Lock must be released by the serialized runner itself.
            with engine.connect() as conn:
                holder = conn.execute(
                    text("SELECT locked_by FROM _migration_lock")
                ).scalar_one()
            _check(
                holder is None,
                f"serialized runner must release the lease, found {holder!r}",
            )
        finally:
            engine.dispose()
