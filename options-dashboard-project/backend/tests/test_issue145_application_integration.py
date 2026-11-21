"""Issue #145 — ADR-017/018 application integration tests (PostgreSQL 16.16).

These tests run the REAL application path: ``app.db.init_db``, the guarded
``app.migrate`` CLI, ``alembic/env.py`` on a caller-supplied protected
connection, the startup backfill under the migration identity, and the
bootstrap/barrier/protocol lifecycle. They require a disposable PostgreSQL
16.16 server:

    ISSUE145_PG_ADMIN_URL=postgresql://postgres@127.0.0.1:5432/postgres

Every test provisions its own database and its own uniquely named
owner/migrator/application roles, so runs never collide and no real database
is touched. Without the environment variable the module is skipped.
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import time

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from app import _migration_lease as lease_mod
from app import db as db_module
from app._migration_lease import (
    EXPECTED_PROTOCOL_VERSION,
    LeaseError,
    MigrationLease,
    bootstrap_lease,
    read_protocol_info,
)
from app.config import settings
from tests import issue145_pg_support as support

pytestmark = pytest.mark.skipif(
    support.admin_url() is None,
    reason=f"{support.ADMIN_ENV} is not configured (disposable PostgreSQL 16.16)",
)

logger = logging.getLogger("issue145.integration")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _apply_settings(monkeypatch, prov: support.Provisioned) -> None:
    from sqlalchemy import create_engine

    # init_db() consults the runtime engine only for its dialect; give it the
    # real PostgreSQL runtime identity instead of the hermetic SQLite engine.
    monkeypatch.setattr(
        db_module,
        "engine",
        create_engine(db_module.normalize_database_url(prov.app_url)),
    )
    monkeypatch.setattr(settings, "DATABASE_URL", prov.app_url)
    monkeypatch.setattr(settings, "STRIKENOVA_MIGRATION_DATABASE_URL", prov.migrator_url)
    monkeypatch.setattr(settings, "STRIKENOVA_OWNER_ROLE", prov.owner_role)
    monkeypatch.setattr(settings, "STRIKENOVA_MIGRATOR_ROLE", prov.migrator_role)
    monkeypatch.setattr(settings, "STRIKENOVA_RUNTIME_ROLE", prov.app_role)
    monkeypatch.setattr(settings, "STRIKENOVA_LEASE_NONCE", "")
    monkeypatch.setattr(settings, "MIGRATION_LEASE_TTL_SECONDS", 300.0)
    monkeypatch.setattr(settings, "MIGRATION_LEASE_WAIT_SECONDS", 180.0)
    monkeypatch.setattr(settings, "MIGRATION_LEASE_LOCK_TIMEOUT_MS", 5000)


@pytest.fixture
def pg(monkeypatch):
    prov = support.provision()
    _apply_settings(monkeypatch, prov)
    try:
        yield prov
    finally:
        support.drop(prov)


@pytest.fixture
def pg_topology_a(pg, monkeypatch):
    """Topology A: the migrator owns what Alembic creates (ADR §8.3)."""
    monkeypatch.setattr(settings, "STRIKENOVA_OWNER_ROLE", "")
    return pg


@pytest.fixture
def pg_ready(pg, monkeypatch):
    """A bootstrapped database whose Alembic chain has run under the lease."""
    _bootstrap(pg)
    db_module._run_alembic_migrations()
    support.grant_backfill_privileges(pg)
    return pg


def _engine(prov: support.Provisioned):
    return db_module._migration_engine()


def _bootstrap(prov: support.Provisioned, **kwargs):
    engine = _engine(prov)
    try:
        return bootstrap_lease(engine, **kwargs)
    finally:
        engine.dispose()


def _info(prov: support.Provisioned):
    engine = _engine(prov)
    try:
        return read_protocol_info(engine)
    finally:
        engine.dispose()


def run_python(prov: support.Provisioned, code: str, *, timeout: int = 240, **env):
    overrides = {"STRIKENOVA_OWNER_ROLE": settings.STRIKENOVA_OWNER_ROLE}
    overrides.update(env)
    full_env = prov.env(**overrides)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=os.getcwd(),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_module(prov: support.Provisioned, args: list[str], *, timeout: int = 240, **env):
    overrides = {"STRIKENOVA_OWNER_ROLE": settings.STRIKENOVA_OWNER_ROLE}
    overrides.update(env)
    full_env = prov.env(**overrides)
    full_env["PYTHONPATH"] = os.getcwd()
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=os.getcwd(),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _admin(prov: support.Provisioned, autocommit: bool = True):
    return psycopg.connect(prov.admin_database_url, autocommit=autocommit)


def _exposure_counts(prov: support.Provisioned) -> dict[str, int]:
    with psycopg.connect(prov.migrator_url) as conn:
        rows = conn.execute(
            "SELECT user_id, count(*) FROM public.strategy_leg_exposures GROUP BY 1"
        ).fetchall()
    return {user: int(count) for user, count in rows}


def _lease_row(prov: support.Provisioned):
    with psycopg.connect(prov.migrator_url) as conn:
        return conn.execute(
            "SELECT owner_token_hash IS NULL AS unowned, generation, protocol_version, "
            "expires_at FROM migration_internal.migration_lease"
        ).fetchone()


def _assert_sqlstate(exc: pytest.ExceptionInfo, state: str) -> None:
    if not isinstance(exc.value, LeaseError):
        raise AssertionError(f"expected LeaseError, got {type(exc.value)!r}")
    if exc.value.sqlstate != state:
        raise AssertionError(f"expected SQLSTATE {state}, got {exc.value.sqlstate}: {exc.value}")


def _psycopg_sqlstate(exc) -> str | None:
    return getattr(exc, "sqlstate", None)


# ---------------------------------------------------------------------------
# Migration CLI and guarded DDL
# ---------------------------------------------------------------------------


def test_cli_upgrade_head_runs_guarded(pg):
    _bootstrap(pg)
    result = run_module(pg, ["app.migrate", "upgrade", "head"])
    if result.returncode != 0:
        raise AssertionError(f"guarded upgrade failed: {result.stderr[-3000:]}")
    if "lease acquired" not in result.stdout:
        raise AssertionError(f"unexpected CLI output: {result.stdout}")
    with psycopg.connect(pg.admin_database_url) as conn:
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    if revision is None:
        raise AssertionError("alembic_version was not created")
    unowned, generation, protocol, _ = _lease_row(pg)
    if not unowned or generation != 1 or protocol != 1:
        raise AssertionError(
            f"lease row after upgrade was unowned={unowned} generation={generation} "
            f"protocol={protocol}"
        )


def test_bare_alembic_upgrade_fails_closed(pg_ready):
    result = run_module(pg_ready, ["alembic", "upgrade", "head"])
    if result.returncode == 0:
        raise AssertionError("bare 'alembic upgrade head' must fail closed on PostgreSQL")
    combined = result.stdout + result.stderr
    if "refusing to run Alembic against PostgreSQL" not in combined:
        raise AssertionError(f"wrong refusal: {combined[-2000:]}")


def test_missing_and_equal_migration_identity_fail_closed(pg, monkeypatch):
    _bootstrap(pg)
    monkeypatch.setattr(settings, "STRIKENOVA_MIGRATION_DATABASE_URL", None)
    with pytest.raises(LeaseError) as exc:
        db_module._run_alembic_migrations()
    _assert_sqlstate(exc, "45004")

    monkeypatch.setattr(settings, "STRIKENOVA_MIGRATION_DATABASE_URL", pg.app_url)
    with pytest.raises(LeaseError) as exc:
        db_module._run_alembic_migrations()
    _assert_sqlstate(exc, "45004")


def test_lease_and_ddl_share_one_physical_connection(pg, monkeypatch):
    _bootstrap(pg)
    pids: set[int] = set()
    original = MigrationLease.enter

    def recording_enter(self, conn):
        pids.add(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar())
        return original(self, conn)

    monkeypatch.setattr(MigrationLease, "enter", recording_enter)

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg.migrator_url)
    db_module._run_guarded_migrations(cfg, command, logger)
    ddl_pid = cfg.attributes.get("migration_connection_pid")
    if ddl_pid is None:
        raise AssertionError("env.py did not record the migration connection pid")
    if pids != {ddl_pid}:
        raise AssertionError(
            f"lease enters ran on {pids}, DDL ran on {ddl_pid}: not one connection"
        )


# ---------------------------------------------------------------------------
# Concurrent startup and the protected per-user backfill
# ---------------------------------------------------------------------------


def _concurrent_startups(pg) -> None:
    worker = "from app.db import init_db; init_db()"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", worker],
            cwd=os.getcwd(),
            env=pg.env(STRIKENOVA_OWNER_ROLE=settings.STRIKENOVA_OWNER_ROLE),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = [proc.communicate(timeout=300) for proc in procs]
    for proc, (stdout, stderr) in zip(procs, results):
        if proc.returncode != 0:
            raise AssertionError(
                f"startup worker failed rc={proc.returncode}: {(stdout + stderr)[-3000:]}"
            )
        if "23505" in stdout + stderr:
            raise AssertionError("startup workers reported SQLSTATE 23505 (duplicate key)")


def test_two_concurrent_startups_serialize_and_backfill_exactly_once(pg_topology_a):
    pg = pg_topology_a
    _bootstrap(pg)

    # 1. Two simultaneous cold starts race the whole Alembic chain.
    _concurrent_startups(pg)
    with psycopg.connect(pg.admin_database_url) as conn:
        heads = conn.execute("SELECT count(*) FROM alembic_version").fetchone()[0]
        tables = conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public'").fetchone()[0]
    if heads != 1:
        raise AssertionError(f"expected exactly one alembic_version row, got {heads}")
    if tables < 50:
        raise AssertionError(f"the chain did not create the schema ({tables} tables)")
    unowned, generation, _, _ = _lease_row(pg)
    if not unowned or generation != 2:
        raise AssertionError(
            f"lease after two cold starts: unowned={unowned} generation={generation}"
        )

    # 2. Legacy data exists; two more concurrent startups (Alembic already at
    #    head) must serialize the protected backfill and never duplicate rows.
    support.apply_topology_a_revokes(pg)
    with psycopg.connect(pg.admin_database_url) as conn:
        owner = conn.execute(
            "SELECT c.relowner::regrole::text FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relname='positions'"
        ).fetchone()[0]
    if owner != pg.migrator_role:
        raise AssertionError(f"Topology A expected migrator ownership, got {owner}")
    support.seed_legacy_users(pg.admin_database_url, users=3)
    _concurrent_startups(pg)
    counts = _exposure_counts(pg)
    if counts != {f"issue145-user-{u}": 1 for u in (1, 2, 3)}:
        raise AssertionError(f"unexpected exposure ledger after concurrent start: {counts}")
    unowned, generation, _, _ = _lease_row(pg)
    if not unowned or generation != 4:
        raise AssertionError(
            f"lease after four acquisitions: unowned={unowned} generation={generation}"
        )

    # 3. A later startup with Alembic at head still performs the protected
    #    backfill for newly legacy data and creates no duplicates.
    support.seed_legacy_users(pg.admin_database_url, users=1, start_user=4)
    result = run_python(pg, "from app.db import init_db; init_db()")
    if result.returncode != 0:
        raise AssertionError(f"second startup failed: {result.stderr[-2000:]}")
    counts = _exposure_counts(pg)
    if counts != {f"issue145-user-{u}": 1 for u in (1, 2, 3, 4)}:
        raise AssertionError(f"backfill at head was not idempotent/exact: {counts}")


def test_backfill_uses_migrator_identity_and_per_user_transactions(pg_ready, monkeypatch):
    support.seed_legacy_users(pg_ready.admin_database_url, users=3)

    import app.services.leg_exposure as leg

    observations: list[dict] = []
    real_backfill = leg.backfill_exposures

    def observed(db, user_id):
        row = db.execute(
            db_module.text(
                "SELECT current_user, "
                " (SELECT count(*) FROM pg_locks l "
                "   WHERE l.pid = pg_backend_pid() "
                "     AND l.relation = 'migration_internal.migration_lease'::regclass "
                "     AND l.granted) AS lease_locks"
            )
        ).one()
        observations.append(
            {"user": user_id, "current_user": row[0], "lease_locks": int(row[1])}
        )
        return real_backfill(db, user_id)

    monkeypatch.setattr(leg, "backfill_exposures", observed)
    enter_calls: list[str] = []
    real_enter = MigrationLease.enter

    def counting_enter(self, conn):
        enter_calls.append(conn.exec_driver_sql("SELECT current_user").scalar())
        return real_enter(self, conn)

    monkeypatch.setattr(MigrationLease, "enter", counting_enter)

    db_module.init_db()

    if len(observations) != 3:
        raise AssertionError(f"expected 3 user backfills, got {observations}")
    for obs in observations:
        if obs["current_user"] != pg_ready.migrator_role:
            raise AssertionError(f"backfill ran as {obs['current_user']}, not the migrator")
        if obs["lease_locks"] < 1:
            raise AssertionError(
                f"backfill for {obs['user']} ran without holding the lease row lock"
            )
    # 2 enters for the DDL transaction + 2 for each of 3 user transactions.
    if len(enter_calls) != 8:
        raise AssertionError(
            f"expected 8 enter calls (DDL pair + 2 per user), got {len(enter_calls)}"
        )
    if enter_calls.count(pg_ready.migrator_role) != 8:
        raise AssertionError("every enter must run as the migration identity")


def test_backfill_failure_rolls_back_user_and_releases_lease(pg_ready, monkeypatch):
    support.seed_legacy_users(pg_ready.admin_database_url, users=3)

    import app.services.leg_exposure as leg

    real_backfill = leg.backfill_exposures

    def failing(db, user_id):
        if user_id.endswith("-2"):
            raise RuntimeError("injected backfill failure")
        return real_backfill(db, user_id)

    monkeypatch.setattr(leg, "backfill_exposures", failing)
    with pytest.raises(RuntimeError, match="injected backfill failure"):
        db_module.init_db()

    counts = _exposure_counts(pg_ready)
    if counts != {"issue145-user-1": 1}:
        raise AssertionError(f"expected only user 1 committed, got {counts}")
    unowned, _, _, _ = _lease_row(pg_ready)
    if not unowned:
        raise AssertionError("the lease was not released after the injected failure")

    monkeypatch.setattr(leg, "backfill_exposures", real_backfill)
    db_module.init_db()
    counts = _exposure_counts(pg_ready)
    if counts != {f"issue145-user-{u}": 1 for u in (1, 2, 3)}:
        raise AssertionError(f"recovery was not idempotent/exact: {counts}")


def test_killed_backfill_worker_recovers_after_expiry(pg, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MIGRATION_LEASE_TTL_SECONDS", 3.0)
    _bootstrap(pg)
    db_module._run_alembic_migrations()
    support.grant_backfill_privileges(pg)
    support.seed_legacy_users(pg.admin_database_url, users=3)

    marker = tmp_path / "paused"
    worker = tmp_path / "worker.py"
    worker.write_text(
        textwrap.dedent(
            f"""
            import pathlib, time
            import app.services.leg_exposure as leg
            real = leg.backfill_exposures
            marker = pathlib.Path({str(marker)!r})
            def slow(db, user_id):
                if user_id == "issue145-user-2":
                    marker.write_text("paused")
                    time.sleep(120)
                return real(db, user_id)
            leg.backfill_exposures = slow
            from app.db import init_db
            init_db()
            """
        )
    )

    proc = subprocess.Popen(
        [sys.executable, str(worker)],
        cwd=os.getcwd(),
        env=pg.env(MIGRATION_LEASE_TTL_SECONDS="3"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 90
        while not marker.exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise AssertionError(f"worker exited before pausing: {out + err}")
            time.sleep(0.1)
        if not marker.exists():
            raise AssertionError("worker never reached the second user")
        proc.kill()  # SIGKILL: no cleanup runs, the server releases the lock
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()

    counts = _exposure_counts(pg)
    if counts != {"issue145-user-1": 1}:
        raise AssertionError(f"expected only user 1 committed before the kill, got {counts}")
    unowned, _, _, expires_at = _lease_row(pg)
    if unowned:
        raise AssertionError("the lease should still be owned by the killed worker")

    time.sleep(3.5)  # let the database-clock expiry pass
    result = run_python(pg, "from app.db import init_db; init_db()")
    if result.returncode != 0:
        raise AssertionError(f"recovery startup failed: {result.stderr[-3000:]}")
    counts = _exposure_counts(pg)
    if counts != {f"issue145-user-{u}": 1 for u in (1, 2, 3)}:
        raise AssertionError(f"recovery produced duplicates or missing rows: {counts}")


# ---------------------------------------------------------------------------
# Barrier and protocol lifecycle
# ---------------------------------------------------------------------------


def test_barrier_close_reports_live_owner_and_blocks_admission(pg):
    _bootstrap(pg)
    db_module._run_alembic_migrations()
    engine = _engine(pg)
    try:
        owner = MigrationLease(engine)
        owner.acquire()
        holder = engine.connect()
        tx = holder.begin()
        owner.enter(holder)  # holds the lease row lock for this transaction

        report = bootstrap_lease(engine, mode="close_barrier")
        if report.get("lease_row_lock") != "held":
            raise AssertionError(f"expected a held lease row lock, got {report}")

        owner.enter(holder)  # an admitted owner keeps working (barrier ignored)
        tx.commit()
        holder.close()

        # The row lock is free now, so admission reaches the barrier check and
        # fails fast with 45004 (no retry budget consumed).
        newcomer = MigrationLease(engine, wait_seconds=2)
        with pytest.raises(LeaseError) as exc:
            newcomer.acquire()
        _assert_sqlstate(exc, "45004")

        owner.release()

        reopened = bootstrap_lease(engine, mode="verify")
        if reopened.get("installed") != "verified":
            raise AssertionError(f"verify mode did not reopen: {reopened}")
        second = MigrationLease(engine)
        second.acquire()
        second.release()
    finally:
        engine.dispose()


def test_verify_refuses_version_mismatch_without_repair(pg):
    _bootstrap(pg)
    before = _info(pg)
    bootstrap_lease(_engine(pg), mode="verify")  # returns nothing to prove after dispose
    after = _info(pg)
    if (after.installed_version, after.barrier_state) != (1, "open"):
        raise AssertionError(f"verify changed state: {before} -> {after}")

    engine = _engine(pg)
    try:
        with pytest.raises(LeaseError) as exc:
            bootstrap_lease(
                engine,
                mode="verify",
                expected_version=2,
                supported_min=1,
                supported_max=2,
            )
        _assert_sqlstate(exc, "45006")
    finally:
        engine.dispose()
    unchanged = _info(pg)
    if (
        unchanged.installed_version,
        unchanged.min_supported_version,
        unchanged.max_supported_version,
        unchanged.barrier_state,
    ) != (1, 1, 1, "open"):
        raise AssertionError(f"a refused verify changed the control row: {unchanged}")


def test_protocol_upgrade_downgrade_and_refusals(pg):
    _bootstrap(pg)
    engine = _engine(pg)
    try:
        # Simulated protocol-2 binary: same SQL objects, version 2 constants.
        report = bootstrap_lease(
            engine,
            mode="upgrade",
            upgrade_to=2,
            expected_version=2,
            supported_min=1,
            supported_max=2,
            authorized_sources=(1,),
        )
        if report.get("installed_version") != 2:
            raise AssertionError(f"upgrade did not move the version: {report}")

        stale = MigrationLease(engine, protocol=1)
        with pytest.raises(LeaseError) as exc:
            stale.acquire()
        _assert_sqlstate(exc, "45006")

        with pytest.raises(LeaseError) as exc:
            bootstrap_lease(
                engine,
                mode="upgrade",
                upgrade_to=1,
                expected_version=1,
                supported_min=2,
                supported_max=2,
                authorized_sources=(2,),
            )
        _assert_sqlstate(exc, "45006")  # downgrade refused

        with pytest.raises(LeaseError) as exc:
            bootstrap_lease(
                engine,
                mode="verify",
                expected_version=1,
                supported_min=1,
                supported_max=2,
            )
        _assert_sqlstate(exc, "45006")  # an older binary refuses to run
    finally:
        engine.dispose()

    with psycopg.connect(pg.admin_database_url) as conn:
        row = conn.execute(
            "SELECT installed_version, min_supported_version, max_supported_version, "
            "barrier_state, bootstrap_state FROM migration_internal.lease_protocol"
        ).fetchone()
        mirror = conn.execute(
            "SELECT protocol_version FROM migration_internal.migration_lease"
        ).fetchone()[0]
    if row != (2, 1, 2, "open", "ready") or mirror != 2:
        raise AssertionError(f"unexpected control state after refusals: {row}, mirror={mirror}")


def test_upgrade_cancellation_changes_nothing(pg):
    _bootstrap(pg)
    engine = _engine(pg)
    try:
        conn = engine.connect()
        tx = conn.begin()
        lease_mod._verify_or_upgrade(
            conn,
            mode="upgrade",
            upgrade_to=2,
            expected_version=2,
            supported_min=1,
            supported_max=2,
            authorized_sources=(1,),
            migrator_role=pg.migrator_role,
            runtime_role=pg.app_role,
        )
        tx.rollback()  # cancellation before COMMIT
        conn.close()
    finally:
        engine.dispose()

    info = _info(pg)
    if (info.installed_version, info.min_supported_version, info.max_supported_version) != (1, 1, 1):
        raise AssertionError(f"a cancelled upgrade half-applied: {info}")
    if info.barrier_state != "open" or info.bootstrap_state != "ready":
        raise AssertionError(f"a cancelled upgrade left the wrong barrier state: {info}")


def test_enter_refuses_an_unready_protocol(pg):
    _bootstrap(pg)
    with psycopg.connect(pg.admin_database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE migration_internal.lease_protocol SET bootstrap_state='installing'"
        )
    engine = _engine(pg)
    try:
        lease = MigrationLease(engine)
        with pytest.raises(LeaseError) as exc:
            lease.acquire()
        _assert_sqlstate(exc, "45005")
    finally:
        engine.dispose()
    with psycopg.connect(pg.admin_database_url) as conn:
        row = conn.execute(
            "SELECT owner_token_hash, expires_at, generation "
            "FROM migration_internal.migration_lease"
        ).fetchone()
        conn.execute(
            "UPDATE migration_internal.lease_protocol SET bootstrap_state='ready'"
        )
        conn.commit()
    if row != (None, None, 0):
        raise AssertionError(f"the refused admission wrote the lease row: {row}")


# ---------------------------------------------------------------------------
# Lease protocol mechanics
# ---------------------------------------------------------------------------


def test_invalid_ttl_and_lock_timeout_are_refused(pg_ready):
    with psycopg.connect(pg_ready.migrator_url) as conn:
        for ttl, lock_timeout in ((0, 1000), (-1, 1000), ("'NaN'", 1000), (1, 0), (1, -5)):
            with pytest.raises(psycopg.Error) as exc:
                conn.execute(
                    "SELECT * FROM migration_internal.lease_acquire_v1(1, "
                    "repeat('a', 40), %s, %s)" % (ttl, lock_timeout)
                )
            if _psycopg_sqlstate(exc.value) != "45004":
                raise AssertionError(
                    f"ttl={ttl} lock_timeout={lock_timeout} gave "
                    f"{_psycopg_sqlstate(exc.value)}"
                )
            conn.rollback()


def test_expired_lease_takeover_and_superseded_entry(pg_ready):
    engine = _engine(pg_ready)
    try:
        first = MigrationLease(engine, ttl_seconds=1)
        first.acquire()
        time.sleep(1.3)

        second = MigrationLease(engine, wait_seconds=10)
        second.acquire()  # takes over the expired lease

        conn = engine.connect()
        try:
            with pytest.raises(LeaseError) as exc:
                first.enter(conn)
            _assert_sqlstate(exc, "45001")  # superseded owner
        finally:
            conn.rollback()
            conn.close()
        second.release()
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Privilege boundary (ADR-018)
# ---------------------------------------------------------------------------


def test_runtime_identity_has_zero_access_to_migration_infrastructure(pg_ready):
    with psycopg.connect(pg_ready.app_url) as conn:
        attempts = [
            "SELECT count(*) FROM migration_internal.migration_lease",
            "SELECT migration_internal.lease_protocol_info_v1()",
            "SELECT migration_internal.lease_release_v1(1, repeat('a', 40))",
            f"SET ROLE {pg_ready.owner_role}",
        ]
        for statement in attempts:
            with pytest.raises(psycopg.Error) as exc:
                conn.execute(statement)
            if _psycopg_sqlstate(exc.value) != "42501":
                raise AssertionError(
                    f"{statement!r} gave {_psycopg_sqlstate(exc.value)}, expected 42501"
                )
            conn.rollback()

    with psycopg.connect(pg_ready.admin_database_url) as conn:
        usage = conn.execute(
            "SELECT has_schema_privilege(%s, 'migration_internal', 'USAGE')",
            (pg_ready.app_role,),
        ).fetchone()[0]
        table_privs = conn.execute(
            "SELECT c.relname, p.priv, has_table_privilege(%s, c.oid, p.priv) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE')) AS p(priv) "
            "WHERE n.nspname='migration_internal' AND c.relkind='r'",
            (pg_ready.app_role,),
        ).fetchall()
        functions = conn.execute(
            "SELECT p.proname, has_function_privilege(%s, p.oid, 'EXECUTE') "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname='migration_internal'",
            (pg_ready.app_role,),
        ).fetchall()
        reach = conn.execute(
            "WITH RECURSIVE reach(roleid, member_id, depth) AS ("
            "  SELECT m.roleid, m.member, 1 FROM pg_auth_members m "
            "   WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = %s)"
            "  UNION ALL SELECT m.roleid, m.member, r.depth + 1 "
            "   FROM pg_auth_members m JOIN reach r ON m.member = r.roleid WHERE r.depth < 10)"
            " SELECT count(*) FROM reach",
            (pg_ready.app_role,),
        ).fetchone()[0]
        public_entries = conn.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN LATERAL aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) a "
            "WHERE n.nspname='migration_internal' AND a.grantee = 0"
        ).fetchone()[0] + conn.execute(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "CROSS JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
            "WHERE n.nspname='migration_internal' AND a.grantee = 0"
        ).fetchone()[0]

    if usage:
        raise AssertionError("the runtime role has USAGE on migration_internal")
    if any(row[2] for row in table_privs):
        raise AssertionError(f"the runtime role has table privileges: {table_privs}")
    if any(row[1] for row in functions):
        raise AssertionError(f"the runtime role can execute lease functions: {functions}")
    if reach != 0:
        raise AssertionError(f"the runtime role reaches {reach} role(s) transitively")
    if public_entries != 0:
        raise AssertionError(f"PUBLIC holds {public_entries} grant(s) on migration_internal")


def test_migrator_minimum_privileges_and_owner_window(pg_ready):
    with psycopg.connect(pg_ready.migrator_url) as conn:
        matrix = conn.execute(
            "SELECT c.relname, p.priv, has_table_privilege(current_user, c.oid, p.priv) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE')) AS p(priv) "
            "WHERE n.nspname='public' "
            "AND c.relname IN ('positions','paper_orders','strategy_leg_exposures')"
        ).fetchall()
        expected = {
            ("positions", "SELECT"): True,
            ("positions", "INSERT"): False,
            ("positions", "UPDATE"): False,
            ("positions", "DELETE"): False,
            ("paper_orders", "SELECT"): True,
            ("paper_orders", "INSERT"): False,
            ("paper_orders", "UPDATE"): False,
            ("paper_orders", "DELETE"): False,
            ("strategy_leg_exposures", "SELECT"): True,
            ("strategy_leg_exposures", "INSERT"): True,
            ("strategy_leg_exposures", "UPDATE"): False,
            ("strategy_leg_exposures", "DELETE"): False,
        }
        for table, privilege, effective in matrix:
            if expected[(table, privilege)] != effective:
                raise AssertionError(
                    f"migrator {privilege} on {table} = {effective}, "
                    f"expected {expected[(table, privilege)]}"
                )
        seq_usage, seq_select = conn.execute(
            "SELECT has_sequence_privilege(current_user, "
            "  pg_get_serial_sequence('public.strategy_leg_exposures','id'), 'USAGE'), "
            "has_sequence_privilege(current_user, "
            "  pg_get_serial_sequence('public.strategy_leg_exposures','id'), 'SELECT')"
        ).fetchone()
        if not seq_usage or seq_select:
            raise AssertionError(f"sequence privileges usage={seq_usage} select={seq_select}")

        with pytest.raises(psycopg.Error) as exc:
            conn.execute("UPDATE public.positions SET status='closed'")
        if _psycopg_sqlstate(exc.value) != "42501":
            raise AssertionError("the migrator must not UPDATE positions")
        conn.rollback()

        conn.execute(f"SET ROLE {pg_ready.owner_role}")
        effective = conn.execute("SELECT current_user").fetchone()[0]
        if effective != pg_ready.owner_role:
            raise AssertionError(f"SET ROLE took effect as {effective}")
        conn.execute("CREATE TABLE public._issue145_ddl_probe (id integer)")
        conn.commit()
        with pytest.raises(psycopg.Error) as exc:
            conn.execute("SELECT migration_internal.lease_protocol_info_v1()")
        if _psycopg_sqlstate(exc.value) != "42501":
            raise AssertionError("the owner window must not execute lease functions")
        conn.rollback()
        if conn.execute("SELECT current_user").fetchone()[0] != pg_ready.owner_role:
            raise AssertionError("the elevated window ended before the DDL cleanup")
        conn.execute("DROP TABLE public._issue145_ddl_probe")
        conn.execute("RESET ROLE")
        if conn.execute("SELECT current_user").fetchone()[0] != pg_ready.migrator_role:
            raise AssertionError("RESET ROLE did not restore the migrator identity")
        conn.commit()


# ---------------------------------------------------------------------------
# Bootstrap concurrency
# ---------------------------------------------------------------------------


def test_concurrent_first_installation_is_idempotent(pg):
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "app.migrate", "bootstrap-lease"],
            cwd=os.getcwd(),
            env=pg.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = [(proc, proc.communicate(timeout=300)) for proc in procs]
    for proc, (out, err) in results:
        if proc.returncode != 0 and "55P03" not in err:
            raise AssertionError(f"bootstrap failed: {(out + err)[-3000:]}")
    with psycopg.connect(pg.admin_database_url) as conn:
        schemas = conn.execute(
            "SELECT count(*) FROM pg_namespace WHERE nspname='migration_internal'"
        ).fetchone()[0]
        protocols = conn.execute(
            "SELECT count(*) FROM migration_internal.lease_protocol"
        ).fetchone()[0]
        leases = conn.execute(
            "SELECT count(*) FROM migration_internal.migration_lease"
        ).fetchone()[0]
        functions = conn.execute(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='migration_internal'"
        ).fetchone()[0]
    if (schemas, protocols, leases, functions) != (1, 1, 1, 4):
        raise AssertionError(
            f"concurrent install produced schemas={schemas} protocols={protocols} "
            f"leases={leases} functions={functions}"
        )


# ---------------------------------------------------------------------------
# Measured per-user backfill timing (drives the TTL and statement timeout)
# ---------------------------------------------------------------------------


def test_measure_per_user_backfill_timing(pg_ready, monkeypatch, capsys):
    users, positions_per_user = 50, 2
    support.seed_legacy_users(
        pg_ready.admin_database_url, users=users, positions_per_user=positions_per_user
    )

    import app.services.leg_exposure as leg

    durations: list[float] = []
    real = leg.backfill_exposures

    def timed(db, user_id):
        started = time.perf_counter()
        try:
            return real(db, user_id)
        finally:
            durations.append(time.perf_counter() - started)

    monkeypatch.setattr(leg, "backfill_exposures", timed)
    engine = _engine(pg_ready)
    try:
        lease = MigrationLease(engine)
        lease.acquire()
        db_module.run_startup_backfill(db_module.MigrationWork(engine=engine, lease=lease))
        lease.release()
    finally:
        engine.dispose()

    durations.sort()
    p50 = durations[len(durations) // 2]
    p99 = durations[min(len(durations) - 1, int(len(durations) * 0.99))]
    ttl = settings.MIGRATION_LEASE_TTL_SECONDS
    summary = (
        f"per-user backfill over {len(durations)} users: "
        f"p50={p50 * 1000:.1f}ms p99={p99 * 1000:.1f}ms max={durations[-1] * 1000:.1f}ms "
        f"ttl={ttl:.0f}s statement_timeout="
        f"{settings.MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS}ms"
    )
    print(summary)
    with capsys.disabled():
        print(summary)
    if p99 * 4 >= ttl:
        raise AssertionError(f"TTL {ttl}s is not safely above the measured per-user p99: {summary}")
    counts = _exposure_counts(pg_ready)
    if set(counts.values()) != {positions_per_user}:
        raise AssertionError(f"timing run wrote the wrong ledger: {counts}")
