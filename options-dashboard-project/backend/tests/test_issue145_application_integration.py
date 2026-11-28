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
def pg_topology_a(monkeypatch):
    """Topology A: the migrator owns what Alembic creates (ADR §8.3).

    Independently provisioned: no owner role exists at all, no membership row
    is created, and the runtime default privileges are attached to the role
    that actually creates the objects (the migrator). Blanking the setting in
    a Topology B fixture would not demonstrate this contract.
    """
    prov = support.provision(topology=support.TOPOLOGY_A)
    _apply_settings(monkeypatch, prov)
    if settings.STRIKENOVA_OWNER_ROLE:
        raise AssertionError("Topology A must not configure an owner role")
    try:
        yield prov
    finally:
        support.drop(prov)


@pytest.fixture
def pg_ready(pg, monkeypatch):
    """A bootstrapped database whose Alembic chain has run under the lease."""
    _bootstrap(pg)
    db_module._run_alembic_migrations()
    support.grant_backfill_privileges(pg)
    return pg


@pytest.fixture
def pg_ready_a(pg_topology_a):
    """Topology A after the guarded chain, without the Topology B grants.

    The §8.3 Topology A restrictions are applied by the guarded migration
    itself, so nothing here may re-grant anything: the fixture is the exact
    privilege state the production startup leaves behind for the backfill.
    """
    _bootstrap(pg_topology_a)
    db_module._run_alembic_migrations()
    return pg_topology_a


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
    with psycopg.connect(pg.admin_database_url) as conn:
        owner = conn.execute(
            "SELECT c.relowner::regrole::text FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relname='positions'"
        ).fetchone()[0]
        memberships = conn.execute(
            "SELECT count(*) FROM pg_auth_members WHERE member = "
            "(SELECT oid FROM pg_roles WHERE rolname = %s)",
            (pg.migrator_role,),
        ).fetchone()[0]
        owner_roles = conn.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname = %s",
            (pg.owner_role or "__topology_a_has_no_owner__",),
        ).fetchone()[0]
    if owner != pg.migrator_role:
        raise AssertionError(f"Topology A expected migrator ownership, got {owner}")
    if memberships != 0:
        raise AssertionError(
            f"Topology A must have no role membership, found {memberships}"
        )
    if owner_roles != 0:
        raise AssertionError("Topology A must not create a separate owner role")
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


def _guard_lock_granted(observer, backend_pid: int) -> bool:
    """True only when *backend_pid* holds the exact migration-guard lock.

    PostgreSQL stores the one-bigint advisory-lock form in ``pg_locks`` as
    ``classid`` = the high 32 bits, ``objid`` = the low 32 bits (both unsigned)
    and ``objsubid = 1``; the two-integer form uses ``objsubid = 2``. Counting
    every granted advisory lock of a backend (the earlier probe) accepts any
    unrelated advisory lock as evidence, so the components are derived from the
    very expression the lease uses (``lease_mod.GUARD_SQL``) and compared
    exactly.
    """
    key = observer.execute(f"SELECT {lease_mod.GUARD_SQL}").fetchone()[0]
    classid = (key >> 32) & 0xFFFFFFFF
    objid = key & 0xFFFFFFFF
    row = observer.execute(
        "SELECT count(*) FROM pg_locks "
        " WHERE pid = %s AND locktype = 'advisory' AND objsubid = 1 "
        "   AND granted "
        "   AND classid::bigint = %s AND objid::bigint = %s",
        (backend_pid, classid, objid),
    ).fetchone()
    return bool(row and row[0])


def _observe_lease_lock(
    prov: support.Provisioned, backend_pid: int | None = None
) -> tuple[bool, bool, bool]:
    """Prove from a second session that the lease row lock is really held.

    A relation-OID entry in ``pg_locks`` is only the ACCESS SHARE lock taken by
    the observing SELECT itself, so it cannot show row-level locking. Asking an
    independent session for the same row with ``FOR UPDATE NOWAIT`` can: the
    lock is held by the protected transaction exactly when that attempt is
    refused with ``lock_not_available`` (SQLSTATE 55P03). The advisory migration
    guard is probed the same way, with ``pg_try_advisory_xact_lock``, and when
    ``backend_pid`` is given the guard lock must also be listed as granted for
    *that* backend under the exact guard key, so the lock is proven to belong to
    the session under test rather than to some other activity in the database.
    """
    with psycopg.connect(prov.migrator_url) as observer:
        owned_row = observer.execute(
            "SELECT owner_token_hash IS NOT NULL "
            "  FROM migration_internal.migration_lease "
            " WHERE lease_name = 'migration-lease'"
        ).fetchone()
        guard_row = observer.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended("
            "'strikenova:migration_internal:migration_guard', 0))"
        ).fetchone()
        guard_owner = None
        if backend_pid is not None:
            guard_owner = _guard_lock_granted(observer, backend_pid)
        observer.rollback()
        row_locked = False
        try:
            observer.execute(
                "SELECT 1 FROM migration_internal.migration_lease "
                " WHERE lease_name = 'migration-lease' FOR UPDATE NOWAIT"
            ).fetchone()
        except psycopg.errors.LockNotAvailable:
            row_locked = True
        observer.rollback()
    owned = bool(owned_row and owned_row[0])
    guard_locked = not bool(guard_row and guard_row[0])
    if backend_pid is not None and not guard_owner:
        # The guard is held, but not by the connection running this step under
        # the exact migration-guard key.
        guard_locked = False
    return (owned, row_locked, guard_locked)


class _StatementTrace:
    """SQL statement buckets keyed by the physical connection that ran them.

    SQLAlchemy fires ``begin``/``commit``/``rollback`` for the connection whose
    DBAPI transaction opens or ends, and ``before_cursor_execute`` for the
    connection executing the statement, so keying buckets by the DBAPI
    connection keeps two interleaved transactions apart: a statement from
    another physical connection has no bucket to land in. A pooled connection
    that begins another transaction starts a fresh bucket and therefore cannot
    inherit a previous transaction's statements. The callbacks issue no SQL, so
    they cannot reorder what they observe.
    """

    def __init__(self) -> None:
        self.transactions: list[dict] = []
        self._active: dict[int, dict] = {}

    @staticmethod
    def _dbapi(conn):  # noqa: ANN001 - SQLAlchemy event argument
        return conn.connection.dbapi_connection

    def _opened(self, conn):  # noqa: ANN001 - SQLAlchemy event argument
        dbapi = self._dbapi(conn)
        bucket = {
            "connection": id(dbapi),
            # Holding the connection keeps its id from being reused by a later
            # object, so an association can never be silently reassigned.
            "connection_ref": dbapi,
            "statements": [],
        }
        self.transactions.append(bucket)
        self._active[bucket["connection"]] = bucket

    def _ended(self, conn):  # noqa: ANN001 - SQLAlchemy event argument
        self._active.pop(id(self._dbapi(conn)), None)

    def _executed(
        self, conn, cursor, statement, parameters, context, executemany
    ):  # noqa: ANN001 - SQLAlchemy event arguments
        dbapi = self._dbapi(conn)
        key = id(dbapi)
        bucket = self._active.get(key)
        if bucket is None:
            bucket = {"connection": key, "connection_ref": dbapi, "statements": []}
            self.transactions.append(bucket)
            self._active[key] = bucket
        bucket["statements"].append(
            {"sql": statement, "connection": key, "parameters": parameters}
        )

    def instrument(self, engine_) -> None:  # noqa: ANN001 - SQLAlchemy engine
        from sqlalchemy import event

        event.listen(engine_, "begin", self._opened)
        event.listen(engine_, "before_cursor_execute", self._executed)
        event.listen(engine_, "commit", self._ended)
        event.listen(engine_, "rollback", self._ended)


def _assert_lease_enter_is_first_statement(
    transactions: list[dict], *, users: list[str]
) -> None:
    """Prove MigrationLease.enter() is the first SQL of every protected transaction.

    ``transactions`` holds one bucket per DBAPI-level transaction on the
    connections the trace observed, in execution order, each with the
    statements (and the connection that ran them) inside it. A transaction that
    enters the lease must open with that entry: any statement executed before
    it - including an unrelated SELECT that a wrapper might add - would sit at
    index 0 and fail here. Every bucket is also required to be single-
    connection, so a statement from another physical connection can never be
    credited to this transaction.

    The second ``lease_enter_v1`` must be the last captured SQL statement in
    the transaction before commit - no diagnostic or wrapper SQL may follow it.
    """
    lease_transactions = [
        tx
        for tx in transactions
        if any("lease_enter_v1" in statement["sql"] for statement in tx["statements"])
    ]
    # One DDL transaction (its enter pair) plus one transaction per user.
    if len(lease_transactions) != 1 + len(users):
        raise AssertionError(
            "expected the DDL transaction and one transaction per user to enter "
            f"the lease, got {len(lease_transactions)}: "
            f"{[tx['statements'][:2] for tx in lease_transactions]}"
        )
    traced_users: list[str] = []
    for index, tx in enumerate(lease_transactions):
        statements = tx["statements"]
        connections = {statement["connection"] for statement in statements}
        if connections != {tx["connection"]}:
            raise AssertionError(
                "statements from another physical connection were attributed to "
                f"this transaction: {connections} vs {tx['connection']}"
            )
        enters = [
            position
            for position, statement in enumerate(statements)
            if "lease_enter_v1" in statement["sql"]
        ]
        if enters[0] != 0:
            raise AssertionError(
                "a protected transaction executed SQL before "
                f"MigrationLease.enter(): {statements[0]['sql']!r}"
            )
        if len(enters) != 2:
            raise AssertionError(
                "every protected transaction must enter the lease at its start "
                f"and again before commit, got {len(enters)} entries: "
                f"{[s['sql'][:60] for s in statements]}"
            )
        # The second lease_enter_v1 must be the last SQL statement in the transaction
        second_enter_pos = enters[1]
        if second_enter_pos != len(statements) - 1:
            raise AssertionError(
                f"the second MigrationLease.enter() must be the last SQL "
                f"statement in the transaction before commit, got position {second_enter_pos} "
                f"of {len(statements) - 1} possible, last statement: {statements[second_enter_pos + 1 if second_enter_pos + 1 < len(statements) else second_enter_pos]['sql']!r}"
            )
        between = statements[enters[0] + 1 : enters[1]]
        if not between:
            raise AssertionError(
                "nothing ran between the two lease entries, so the pre-commit "
                f"entry is not protecting completed work: {[s['sql'][:60] for s in statements]}"
            )
        if index == 0:
            # The first protected transaction is the guarded migration/DDL one.
            continue
        rendered = " ".join(
            f"{statement['sql']} {statement['parameters']!r}" for statement in between
        )
        owners = [user for user in users if user in rendered]
        if len(owners) != 1:
            raise AssertionError(
                "the backfill between the two lease entries is not attributable "
                f"to exactly one user: {owners} in {rendered[:400]}"
            )
        if not any(
            "strategy_leg_exposures" in statement["sql"] for statement in between
        ):
            raise AssertionError(
                f"{owners[0]}: the completed backfill does not sit between the two "
                "lease entries"
            )
        traced_users.append(owners[0])
    if sorted(traced_users) != sorted(users) or len(set(traced_users)) != len(users):
        raise AssertionError(
            f"expected one protected transaction per user {users}, got {traced_users}"
        )


def _assert_protected_backfill_trace(
    events: list[dict], *, migrator_role: str, users: list[str]
) -> None:
    """Prove the ADR §7.2–§7.3 order from one ordered event trace.

    The trace must read exactly: the DDL transaction's two lease entries, then
    one ``enter`` → ``backfill`` → ``enter`` triple per user. Grouping is by
    *position*, never by transaction id, so moving an entry across a backfill
    (or starting a user transaction with backfill work) changes the sequence
    and fails here even when every recorded id would still agree.
    """
    labels = [event["event"] for event in events]
    ddl, per_user = events[:2], events[2:]
    if [event["event"] for event in ddl] != ["enter", "enter"]:
        raise AssertionError(
            f"the DDL transaction must open with its two lease entries: {labels}"
        )
    if len(per_user) % 3:
        raise AssertionError(
            f"every user must contribute one enter/backfill/enter triple: {labels}"
        )
    triples = [per_user[index : index + 3] for index in range(0, len(per_user), 3)]
    expected = ["enter", "enter"] + ["enter", "backfill", "enter"] * len(triples)
    if labels != expected:
        raise AssertionError(
            f"the protected order is not enter -> backfill -> pre-commit enter: {labels}"
        )
    if len(triples) != len(users):
        raise AssertionError(
            f"expected {len(users)} protected user transactions, got {len(triples)}"
        )

    ddl_txids = {event["txid"] for event in ddl}
    if len(ddl_txids) != 1:
        raise AssertionError(f"the DDL enter pair spans transactions: {ddl_txids}")
    traced_users: list[str] = []
    per_user_txids: set[int] = set()
    for triple in triples:
        user = triple[1]["user"]
        traced_users.append(user)
        txids = {event["txid"] for event in triple}
        if len(txids) != 1:
            raise AssertionError(
                f"{user}: both entries and the backfill must share one "
                f"transaction, got {txids}"
            )
        if txids & ddl_txids:
            raise AssertionError(f"{user} ran inside the DDL transaction")
        per_user_txids |= txids
    if sorted(traced_users) != sorted(users) or len(set(traced_users)) != len(users):
        raise AssertionError(
            f"expected one protected transaction per user {users}, got {traced_users}"
        )
    if len(per_user_txids) != len(triples):
        raise AssertionError(f"different users shared one transaction: {per_user_txids}")

    for event in events:
        if event["current_user"] != migrator_role:
            raise AssertionError(
                f"{event['event']} for {event['user']} ran as "
                f"{event['current_user']}, not {migrator_role}"
            )
        if not event["lease_owned"]:
            raise AssertionError(
                f"{event['event']} for {event['user']}: the lease row is unowned"
            )
        if not event["lease_row_locked"]:
            raise AssertionError(
                f"{event['event']} for {event['user']}: an independent session "
                "could take the lease row lock, so it was not held"
            )
        if not event["lease_guard_locked"]:
            raise AssertionError(
                f"{event['event']} for {event['user']}: the advisory migration "
                "guard was not held"
            )


def test_backfill_uses_migrator_identity_and_per_user_transactions(pg_ready, monkeypatch):
    users = [f"issue145-user-{index}" for index in (1, 2, 3)]
    support.seed_legacy_users(pg_ready.admin_database_url, users=3)

    import app.services.leg_exposure as leg

    # ---- B1: capture every SQL statement per physical connection -----------
    #
    # Each bucket belongs to one DBAPI transaction on one connection, so an
    # unrelated statement placed ahead of MigrationLease.enter() becomes index 0
    # of its bucket and fails below, while a statement from another connection
    # can never be attributed to this transaction.
    trace = _StatementTrace()

    # The lease, the DDL and the backfill all run on the dedicated migration
    # engine that _migration_engine() builds per call, so every engine it
    # returns is instrumented before use.
    real_migration_engine = db_module._migration_engine

    def traced_migration_engine():
        engine_ = real_migration_engine()
        trace.instrument(engine_)
        return engine_

    monkeypatch.setattr(db_module, "_migration_engine", traced_migration_engine)

    # ---- one ordered trace of every lease-protected step -------------------
    events: list[dict] = []

    # Snapshot per physical DBAPI connection, captured once after the first
    # lease entry and replayed by the pre-commit entry.
    snapshots: dict[int, tuple[str, int, int]] = {}

    def record(
        executor,
        *,
        event: str,
        user: str | None,
        snapshot: tuple[str, int, int] | None = None,
    ) -> tuple[str, int, int]:
        """Append one event; issue SQL only when no snapshot is supplied.

        The snapshot is captured right after the first ``MigrationLease.enter``
        and cached per connection. The second entry replays it instead of
        querying again, so the pre-commit ``lease_enter_v1`` remains the last
        SQL statement the protected connection executes before COMMIT.
        """
        if snapshot is None:
            row = executor.execute(
                db_module.text("SELECT current_user, txid_current(), pg_backend_pid()")
            ).one()
            snapshot = (row[0], int(row[1]), int(row[2]))
        current_user, txid, backend_pid = snapshot
        owned, row_locked, guard_locked = _observe_lease_lock(
            pg_ready, backend_pid=backend_pid
        )
        events.append(
            {
                "event": event,
                "user": user,
                "current_user": current_user,
                "txid": txid,
                "lease_owned": owned,
                "lease_row_locked": row_locked,
                "lease_guard_locked": guard_locked,
                # Retained so the trace helper keeps one simple invariant: the
                # lease is owned and locked, proven from a second session.
                "lease_locks": int(owned and row_locked and guard_locked),
            }
        )
        return snapshot

    real_backfill = leg.backfill_exposures

    def traced_backfill(db, user_id):
        created = real_backfill(db, user_id)
        record(db, event="backfill", user=user_id)
        return created

    monkeypatch.setattr(leg, "backfill_exposures", traced_backfill)
    real_enter = MigrationLease.enter

    def traced_enter(self, conn):
        # The real entry runs first: the wrapper must not slip a statement in
        # ahead of the first-statement-of-the-transaction contract proven here.
        state = real_enter(self, conn)
        key = id(conn.connection.dbapi_connection)
        cached = snapshots.pop(key, None)
        if cached is None:
            # First entry: capture the transaction snapshot with SQL, then keep
            # it for the pre-commit entry.
            snapshots[key] = record(conn, event="enter", user=None)
        else:
            # Second (pre-commit) entry: append from the cached snapshot only.
            # No SQL may follow on the protected connection before COMMIT.
            record(conn, event="enter", user=None, snapshot=cached)
        return state

    monkeypatch.setattr(MigrationLease, "enter", traced_enter)

    db_module.init_db()

    labels = [event["event"] for event in events]
    # 2 entries for the DDL transaction + 2 for each of 3 user transactions,
    # and exactly one completed backfill per user: unchanged requirements.
    if labels.count("enter") != 8:
        raise AssertionError(
            f"expected 8 enter calls (DDL pair + 2 per user), got {labels}"
        )
    if labels.count("backfill") != 3:
        raise AssertionError(f"expected 3 completed user backfills, got {labels}")

    _assert_protected_backfill_trace(
        events, migrator_role=pg_ready.migrator_role, users=users
    )
    _assert_lease_enter_is_first_statement(trace.transactions, users=users)

    # Control: with no protected transaction open, the same probe reports the
    # row and guard as free, so the True values above are discriminating.
    _, row_locked_after, guard_locked_after = _observe_lease_lock(pg_ready)
    if row_locked_after or guard_locked_after:
        raise AssertionError(
            "the lease-lock probe reports a lock outside a protected "
            "transaction, so it cannot prove anything inside one"
        )




def test_statement_trace_keeps_interleaved_connections_separate(pg):
    """The trace associates statements with their own connection transaction."""
    from sqlalchemy import create_engine, text

    trace = _StatementTrace()
    url = db_module.normalize_database_url(pg.migrator_url)
    first = create_engine(url)
    second = create_engine(url)
    trace.instrument(first)
    trace.instrument(second)
    try:
        with first.connect() as a:
            a.execute(text("SELECT 'first-a'"))
            with second.connect() as b:
                b.execute(text("SELECT 'second-a'"))
                a.execute(text("SELECT 'first-b'"))
                a.commit()
                b.execute(text("SELECT 'second-b'"))
                b.commit()
            # The pooled connection is reused: a new transaction must get a new
            # bucket instead of appending to the committed one.
            a.execute(text("SELECT 'first-after-commit'"))
            a.commit()
    finally:
        first.dispose()
        second.dispose()

    def composed(bucket):
        return " ".join(statement["sql"] for statement in bucket["statements"])

    buckets = trace.transactions
    if len(buckets) != 3:
        raise AssertionError(
            "expected three transactions (two interleaved, one after commit), "
            f"got {[composed(bucket) for bucket in buckets]}"
        )
    for bucket in buckets:
        connections = {statement["connection"] for statement in bucket["statements"]}
        if connections != {bucket["connection"]}:
            raise AssertionError(
                "a bucket mixed statements from several physical connections: "
                f"{connections} vs {bucket['connection']}"
            )
    first_bucket, second_bucket, reused_bucket = buckets
    if "first-a" not in composed(first_bucket) or "first-b" not in composed(
        first_bucket
    ):
        raise AssertionError(f"the first connection lost its statements: {composed(first_bucket)}")
    if "second-" in composed(first_bucket):
        raise AssertionError(
            f"another connection's statement leaked into the first transaction: {composed(first_bucket)}"
        )
    if "second-a" not in composed(second_bucket) or "second-b" not in composed(
        second_bucket
    ):
        raise AssertionError(
            f"the second connection lost its statements: {composed(second_bucket)}"
        )
    if "first-" in composed(second_bucket):
        raise AssertionError(
            f"another connection's statement leaked into the second transaction: {composed(second_bucket)}"
        )
    if composed(reused_bucket).strip() != "SELECT 'first-after-commit'":
        raise AssertionError(
            "a reused connection inherited its previous transaction's bucket: "
            f"{composed(reused_bucket)}"
        )
    if reused_bucket["connection"] != first_bucket["connection"]:
        raise AssertionError(
            "the post-commit transaction did not run on the same pooled connection, "
            "so the reuse case was not exercised"
        )


def test_guard_lock_check_requires_the_exact_migration_guard_key(pg):
    """An unrelated advisory lock must not satisfy the guard-ownership probe."""
    unrelated = psycopg.connect(pg.migrator_url)
    holder = psycopg.connect(pg.migrator_url)
    observer = psycopg.connect(pg.migrator_url)
    try:
        unrelated_pid = unrelated.execute("SELECT pg_backend_pid()").fetchone()[0]
        unrelated.execute(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended('issue145:unrelated-advisory-lock', 0))"
        )
        # Control: the weaker "any granted advisory lock" count the earlier
        # probe used is satisfied by this unrelated lock ...
        naive = observer.execute(
            "SELECT count(*) FROM pg_locks "
            " WHERE pid = %s AND locktype = 'advisory' AND granted",
            (unrelated_pid,),
        ).fetchone()[0]
        if not naive:
            raise AssertionError(
                "control failed: the unrelated advisory lock is not visible in pg_locks"
            )
        # ... so only exact-key attribution can tell the difference.
        if _guard_lock_granted(observer, unrelated_pid):
            raise AssertionError(
                "an unrelated advisory lock satisfied the migration-guard check"
            )
        # Positive control: a session holding the exact guard key is detected.
        holder_pid = holder.execute("SELECT pg_backend_pid()").fetchone()[0]
        holder.execute(f"SELECT pg_advisory_xact_lock({lease_mod.GUARD_SQL})")
        if not _guard_lock_granted(observer, holder_pid):
            raise AssertionError(
                "the exact migration-guard advisory lock was not detected"
            )
    finally:
        for connection in (unrelated, holder, observer):
            connection.close()


def test_backfill_failure_rolls_back_user_and_releases_lease(pg_ready, monkeypatch):
    support.seed_legacy_users(pg_ready.admin_database_url, users=3)

    import app.services.leg_exposure as leg

    real_backfill = leg.backfill_exposures
    work: dict[str, int] = {}

    def failing(db, user_id):
        created = real_backfill(db, user_id)
        if user_id.endswith("-2"):
            # Prove user 2 really wrote inside its own transaction before the
            # exception, so "its writes rolled back" is a meaningful assertion
            # rather than a transaction that never did anything.
            work["rows"] = int(
                db.execute(
                    db_module.text(
                        "SELECT count(*) FROM strategy_leg_exposures "
                        "WHERE user_id = :u"
                    ),
                    {"u": user_id},
                ).scalar()
            )
            raise RuntimeError("injected backfill failure")
        return created

    monkeypatch.setattr(leg, "backfill_exposures", failing)
    with pytest.raises(RuntimeError, match="injected backfill failure"):
        db_module.init_db()

    if work.get("rows") != 1:
        raise AssertionError(
            f"user 2 performed no real backfill work before the failure: {work}"
        )
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
    # A short TTL is only a valid configuration when the per-user statement
    # timeout is below it (the cross-field rule enforced by Settings).
    monkeypatch.setattr(settings, "MIGRATION_LEASE_TTL_SECONDS", 3.0)
    monkeypatch.setattr(settings, "MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS", 2500)
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
        env=pg.env(
            MIGRATION_LEASE_TTL_SECONDS="3",
            MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS="2500",
        ),
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
        schema_public_entries = conn.execute(
            "SELECT count(*) FROM pg_namespace n CROSS JOIN LATERAL "
            "aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a "
            "WHERE n.nspname='migration_internal' AND a.grantee = 0"
        ).fetchone()[0]
        schema_effective = conn.execute(
            "SELECT has_schema_privilege(%s, 'migration_internal', 'USAGE'), "
            "       has_schema_privilege(%s, 'migration_internal', 'CREATE')",
            (pg_ready.app_role, pg_ready.app_role),
        ).fetchone()

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
    if schema_public_entries != 0:
        raise AssertionError(
            f"PUBLIC holds {schema_public_entries} schema grant(s) on migration_internal"
        )
    if schema_effective != (False, False):
        raise AssertionError(
            f"runtime effective schema privileges are {schema_effective}, expected none"
        )


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


def test_topology_a_creator_objects_are_runtime_usable(pg_topology_a):
    """Topology A: the migrator creates, the runtime consumes, DDL is denied."""
    pg = pg_topology_a
    _bootstrap(pg)
    db_module._run_alembic_migrations()

    with psycopg.connect(pg.migrator_url) as conn:
        conn.execute(
            "CREATE TABLE public._topology_a_probe ("
            "id bigserial PRIMARY KEY, value text NOT NULL)"
        )
        conn.commit()
        owner = conn.execute(
            "SELECT tableowner FROM pg_tables "
            "WHERE schemaname='public' AND tablename='_topology_a_probe'"
        ).fetchone()[0]
        chain_owner = conn.execute(
            "SELECT tableowner FROM pg_tables "
            "WHERE schemaname='public' AND tablename='positions'"
        ).fetchone()[0]
    if owner != pg.migrator_role or chain_owner != pg.migrator_role:
        raise AssertionError(
            f"Topology A owners: probe={owner} positions={chain_owner}, "
            f"expected {pg.migrator_role}"
        )

    with psycopg.connect(pg.app_url) as conn:
        conn.execute("INSERT INTO public._topology_a_probe (value) VALUES ('a'), ('b')")
        count = conn.execute("SELECT count(*) FROM public._topology_a_probe").fetchone()[0]
        conn.execute("UPDATE public._topology_a_probe SET value = value || '!'")
        conn.execute("DELETE FROM public._topology_a_probe WHERE id = 1")
        conn.commit()
        if count != 2:
            raise AssertionError(f"runtime could not read the creator's table ({count})")
        for statement in (
            "ALTER TABLE public._topology_a_probe ADD COLUMN extra integer",
            "DROP TABLE public._topology_a_probe",
            "TRUNCATE public._topology_a_probe",
            "CREATE TABLE public._runtime_ddl_probe (id integer)",
            "SELECT count(*) FROM migration_internal.migration_lease",
        ):
            with pytest.raises(psycopg.Error) as exc:
                conn.execute(statement)
            if _psycopg_sqlstate(exc.value) != "42501":
                raise AssertionError(
                    f"{statement!r} gave {_psycopg_sqlstate(exc.value)}, expected 42501"
                )
            conn.rollback()

    with psycopg.connect(pg.admin_database_url) as conn:
        sequence_usage = conn.execute(
            "SELECT has_sequence_privilege(%s, "
            "  pg_get_serial_sequence('public._topology_a_probe','id'), 'USAGE')",
            (pg.app_role,),
        ).fetchone()[0]
        truncate = conn.execute(
            "SELECT has_table_privilege(%s, 'public._topology_a_probe', 'TRUNCATE')",
            (pg.app_role,),
        ).fetchone()[0]
        create_on_public = conn.execute(
            "SELECT has_schema_privilege(%s, 'public', 'CREATE')", (pg.app_role,)
        ).fetchone()[0]
        conn.execute("DROP TABLE public._topology_a_probe")
        conn.commit()
    if not sequence_usage or truncate or create_on_public:
        raise AssertionError(
            f"Topology A runtime privileges: sequence_usage={sequence_usage} "
            f"truncate={truncate} create_on_public={create_on_public}"
        )


def _migrator_privilege_matrix(prov: support.Provisioned) -> dict:
    """Effective table/sequence privileges of the migration identity."""
    with psycopg.connect(prov.migrator_url) as conn:
        rows = conn.execute(
            "SELECT c.relname, p.priv, has_table_privilege(current_user, c.oid, p.priv) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),"
            "('TRUNCATE')) AS p(priv) "
            "WHERE n.nspname = 'public' "
            "AND c.relname IN ('positions','paper_orders','strategy_leg_exposures')"
        ).fetchall()
        sequence = conn.execute(
            "SELECT pg_get_serial_sequence('public.strategy_leg_exposures','id')"
        ).fetchone()[0]
        sequence_usage, sequence_select = conn.execute(
            "SELECT has_sequence_privilege(current_user, %s, 'USAGE'), "
            "has_sequence_privilege(current_user, %s, 'SELECT')",
            (sequence, sequence),
        ).fetchone()
    matrix = {(table, privilege): bool(effective) for table, privilege, effective in rows}
    matrix[("sequence", "USAGE")] = bool(sequence_usage)
    matrix[("sequence", "SELECT")] = bool(sequence_select)
    return matrix


def test_topology_a_restricted_privilege_matrix(pg_ready_a):
    """ADR §8.3 Topology A: the owner's ordinary privileges are revoked.

    The migrator owns these objects, yet PostgreSQL 16 applies the revokes to
    the owner's own ACL, so the effective matrix is the §8.2 minimum and the
    denied statements fail ``42501`` for the owner itself.
    """
    expected = {
        ("positions", "SELECT"): True,
        ("positions", "INSERT"): False,
        ("positions", "UPDATE"): False,
        ("positions", "DELETE"): False,
        ("positions", "TRUNCATE"): False,
        ("paper_orders", "SELECT"): True,
        ("paper_orders", "INSERT"): False,
        ("paper_orders", "UPDATE"): False,
        ("paper_orders", "DELETE"): False,
        ("paper_orders", "TRUNCATE"): False,
        ("strategy_leg_exposures", "SELECT"): True,
        ("strategy_leg_exposures", "INSERT"): True,
        ("strategy_leg_exposures", "UPDATE"): False,
        ("strategy_leg_exposures", "DELETE"): False,
        ("strategy_leg_exposures", "TRUNCATE"): False,
        ("sequence", "USAGE"): True,
        ("sequence", "SELECT"): False,
    }
    matrix = _migrator_privilege_matrix(pg_ready_a)
    for key, wanted in expected.items():
        if matrix.get(key) is not wanted:
            raise AssertionError(
                f"Topology A migrator privilege {key} = {matrix.get(key)}, expected {wanted}"
            )

    with psycopg.connect(pg_ready_a.migrator_url) as conn:
        # The allowed reads really work for the owning migrator.
        conn.execute("SELECT count(*) FROM public.positions").fetchone()
        conn.execute("SELECT count(*) FROM public.paper_orders").fetchone()
        conn.execute("SELECT count(*) FROM public.strategy_leg_exposures").fetchone()
        conn.rollback()
        denied = (
            "INSERT INTO public.positions SELECT * FROM public.positions",
            "UPDATE public.positions SET status = 'closed'",
            "DELETE FROM public.positions",
            "TRUNCATE public.positions",
            "INSERT INTO public.paper_orders SELECT * FROM public.paper_orders",
            "UPDATE public.paper_orders SET status = 'FILLED'",
            "DELETE FROM public.paper_orders",
            "TRUNCATE public.paper_orders",
            "UPDATE public.strategy_leg_exposures SET order_id = order_id",
            "DELETE FROM public.strategy_leg_exposures",
            "TRUNCATE public.strategy_leg_exposures",
            "SELECT last_value FROM public.strategy_leg_exposures_id_seq",
        )
        for statement in denied:
            with pytest.raises(psycopg.Error) as exc:
                conn.execute(statement)
            state = _psycopg_sqlstate(exc.value)
            if state != "42501":
                raise AssertionError(f"{statement!r} gave {state}, expected 42501")
            conn.rollback()
        # USAGE alone authorizes nextval, which INSERT … RETURNING id needs.
        conn.execute("SELECT nextval('public.strategy_leg_exposures_id_seq')").fetchone()
        conn.rollback()


def test_topology_a_restricted_backfill_completes(pg_ready_a):
    """The real startup backfill succeeds under the restricted owner."""
    support.seed_legacy_users(pg_ready_a.admin_database_url, users=3)

    # init_db() re-runs the guarded chain (the restrictions are re-applied) and
    # then the protected per-user backfill as the same restricted identity.
    db_module.init_db()
    counts = _exposure_counts(pg_ready_a)
    if counts != {f"issue145-user-{u}": 1 for u in (1, 2, 3)}:
        raise AssertionError(f"unexpected exposure ledger after backfill: {counts}")

    support.seed_legacy_users(pg_ready_a.admin_database_url, users=1, start_user=4)
    db_module.init_db()
    counts = _exposure_counts(pg_ready_a)
    if counts != {f"issue145-user-{u}": 1 for u in (1, 2, 3, 4)}:
        raise AssertionError(f"later upgrade changed the backfill result: {counts}")

    # A subsequent upgrade must not silently restore the owner's privileges.
    matrix = _migrator_privilege_matrix(pg_ready_a)
    for key in (
        ("positions", "INSERT"),
        ("positions", "UPDATE"),
        ("positions", "DELETE"),
        ("positions", "TRUNCATE"),
        ("paper_orders", "INSERT"),
        ("strategy_leg_exposures", "UPDATE"),
        ("strategy_leg_exposures", "DELETE"),
        ("sequence", "SELECT"),
    ):
        if matrix.get(key) is not False:
            raise AssertionError(
                f"after a later upgrade the migrator regained {key} "
                f"({matrix.get(key)})"
            )
    if matrix.get(("strategy_leg_exposures", "INSERT")) is not True:
        raise AssertionError("the backfill lost INSERT on strategy_leg_exposures")


def test_topology_b_owner_role_shape_and_default_privileges(pg_ready):
    """Topology B: NOLOGIN owner, SET-only membership, creator-scoped defaults."""
    with psycopg.connect(pg_ready.admin_database_url) as conn:
        role = conn.execute(
            "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb "
            "FROM pg_roles WHERE rolname = %s",
            (pg_ready.owner_role,),
        ).fetchone()
        membership = conn.execute(
            "SELECT inherit_option, set_option, admin_option FROM pg_auth_members "
            "WHERE roleid = (SELECT oid FROM pg_roles WHERE rolname = %s) "
            "  AND member = (SELECT oid FROM pg_roles WHERE rolname = %s)",
            (pg_ready.owner_role, pg_ready.migrator_role),
        ).fetchone()
        creator = conn.execute(
            "SELECT tableowner FROM pg_tables "
            "WHERE schemaname='public' AND tablename='positions'"
        ).fetchone()[0]
        defaults = conn.execute(
            "SELECT d.defaclnamespace::regnamespace::text, d.defaclobjtype, "
            "       a.grantee::regrole::text, a.privilege_type "
            "FROM pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
            "WHERE d.defaclrole = (SELECT oid FROM pg_roles WHERE rolname = %s)",
            (pg_ready.owner_role,),
        ).fetchall()
        migrator_defaults = conn.execute(
            "SELECT count(*) FROM pg_default_acl WHERE defaclrole = "
            "(SELECT oid FROM pg_roles WHERE rolname = %s)",
            (pg_ready.migrator_role,),
        ).fetchone()[0]

    if role != (False, False, False, False):
        raise AssertionError(f"owner role attributes are {role}, expected NOLOGIN-only")
    if membership != (False, True, False):
        raise AssertionError(
            f"owner membership is {membership}, expected INHERIT FALSE, SET TRUE, ADMIN FALSE"
        )
    if creator != pg_ready.owner_role:
        raise AssertionError(f"application objects are owned by {creator}")
    if migrator_defaults != 0:
        raise AssertionError("Topology B must not attach default privileges to the migrator")
    if not defaults or {row[0] for row in defaults} != {"public"}:
        raise AssertionError(f"default privileges are not public-scoped: {defaults}")
    if any(row[2] != pg_ready.app_role for row in defaults):
        raise AssertionError(f"default privileges grant to the wrong role: {defaults}")

    # The owner's tables are usable by the runtime identity with DML only.
    with psycopg.connect(pg_ready.migrator_url) as conn:
        conn.execute(f"SET ROLE {pg_ready.owner_role}")
        conn.execute(
            "CREATE TABLE public._topology_b_probe ("
            "id bigserial PRIMARY KEY, value text NOT NULL)"
        )
        conn.execute("RESET ROLE")
        conn.commit()
    try:
        with psycopg.connect(pg_ready.app_url) as conn:
            conn.execute("INSERT INTO public._topology_b_probe (value) VALUES ('x')")
            total = conn.execute("SELECT count(*) FROM public._topology_b_probe").fetchone()[0]
            conn.commit()
            if total != 1:
                raise AssertionError("runtime cannot use an owner-created table")
            for statement in (
                "ALTER TABLE public._topology_b_probe ADD COLUMN extra integer",
                "DROP TABLE public._topology_b_probe",
                f"SET ROLE {pg_ready.owner_role}",
            ):
                with pytest.raises(psycopg.Error) as exc:
                    conn.execute(statement)
                if _psycopg_sqlstate(exc.value) != "42501":
                    raise AssertionError(
                        f"{statement!r} gave {_psycopg_sqlstate(exc.value)}, expected 42501"
                    )
                conn.rollback()
    finally:
        with psycopg.connect(pg_ready.admin_database_url) as conn:
            conn.execute("DROP TABLE IF EXISTS public._topology_b_probe")
            conn.commit()


# ---------------------------------------------------------------------------
# Schema ACL positive and negative controls (ADR §9.3)
# ---------------------------------------------------------------------------


def _verify_refusal(pg: support.Provisioned) -> str:
    """Run the verify mode and return the refusal message (asserting 45004)."""
    engine = _engine(pg)
    try:
        with pytest.raises(LeaseError) as exc:
            bootstrap_lease(engine, mode="verify")
        _assert_sqlstate(exc, "45004")
        return str(exc.value)
    finally:
        engine.dispose()


def _assert_verified(pg: support.Provisioned) -> None:
    report = _bootstrap(pg, mode="verify")
    if report.get("installed") != "verified":
        raise AssertionError(f"verification did not pass on a valid install: {report}")


def test_schema_acl_verification_positive_and_negative_controls(pg):
    _bootstrap(pg)
    _assert_verified(pg)

    # 1. PUBLIC USAGE on migration_internal must fail verification.
    with psycopg.connect(pg.admin_database_url, autocommit=True) as conn:
        conn.execute("GRANT USAGE ON SCHEMA migration_internal TO PUBLIC")
    try:
        message = _verify_refusal(pg)
        if "USAGE" not in message or "PUBLIC" not in message:
            raise AssertionError(f"PUBLIC USAGE refusal was not reported: {message}")
    finally:
        with psycopg.connect(pg.admin_database_url, autocommit=True) as conn:
            conn.execute("REVOKE USAGE ON SCHEMA migration_internal FROM PUBLIC")
    _assert_verified(pg)

    # 2. PUBLIC CREATE on migration_internal must fail verification.
    with psycopg.connect(pg.admin_database_url, autocommit=True) as conn:
        conn.execute("GRANT CREATE ON SCHEMA migration_internal TO PUBLIC")
    try:
        message = _verify_refusal(pg)
        if "CREATE" not in message or "PUBLIC" not in message:
            raise AssertionError(f"PUBLIC CREATE refusal was not reported: {message}")
    finally:
        with psycopg.connect(pg.admin_database_url, autocommit=True) as conn:
            conn.execute("REVOKE CREATE ON SCHEMA migration_internal FROM PUBLIC")

    # 3. Runtime USAGE on migration_internal must fail verification.
    with psycopg.connect(pg.admin_database_url, autocommit=True) as conn:
        conn.execute(f"GRANT USAGE ON SCHEMA migration_internal TO {pg.app_role}")
    try:
        message = _verify_refusal(pg)
        if pg.app_role not in message:
            raise AssertionError(f"runtime USAGE refusal was not reported: {message}")
    finally:
        with psycopg.connect(pg.admin_database_url, autocommit=True) as conn:
            conn.execute(f"REVOKE USAGE ON SCHEMA migration_internal FROM {pg.app_role}")

    # 4. Restored state verifies again and the control row is unchanged.
    _assert_verified(pg)
    with psycopg.connect(pg.admin_database_url) as conn:
        schema_acl = conn.execute(
            "SELECT nspacl::text FROM pg_namespace WHERE nspname='migration_internal'"
        ).fetchone()[0]
        public_entries = conn.execute(
            "SELECT count(*) FROM pg_namespace n CROSS JOIN LATERAL "
            "aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a "
            "WHERE n.nspname='migration_internal' AND a.grantee = 0"
        ).fetchone()[0]
        runtime_schema_privs = conn.execute(
            "SELECT has_schema_privilege(%s, 'migration_internal', 'USAGE'), "
            "       has_schema_privilege(%s, 'migration_internal', 'CREATE')",
            (pg.app_role, pg.app_role),
        ).fetchone()
    if public_entries != 0:
        raise AssertionError(f"PUBLIC still holds schema grants: {schema_acl}")
    if runtime_schema_privs != (False, False):
        raise AssertionError(f"runtime schema privileges are {runtime_schema_privs}")


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


# ---------------------------------------------------------------------------
# ADR §8.3 application privilege verifier (positive and negative controls)
# ---------------------------------------------------------------------------


def _application_check_connection(prov: support.Provisioned):
    """SQLAlchemy connection for the read-only verifier, as production uses."""
    from sqlalchemy import create_engine

    return create_engine(db_module.normalize_database_url(prov.migrator_url))


def _verify_application_matrix(prov: support.Provisioned) -> None:
    """Run the read-only verifier the enforcement path runs."""
    engine = _application_check_connection(prov)
    try:
        with engine.connect() as conn:
            lease_mod.verify_application_privileges(
                conn,
                migrator_role=prov.migrator_role,
                runtime_role=prov.app_role,
                owner_role=prov.owner_role or None,
            )
    finally:
        engine.dispose()


def _expect_application_refusal(prov: support.Provisioned, fragment: str) -> str:
    engine = _application_check_connection(prov)
    try:
        with engine.connect() as conn, pytest.raises(LeaseError) as exc:
            lease_mod.verify_application_privileges(
                conn,
                migrator_role=prov.migrator_role,
                runtime_role=prov.app_role,
                owner_role=prov.owner_role or None,
            )
    finally:
        engine.dispose()
    _assert_sqlstate(exc, "45004")
    message = str(exc.value)
    if fragment not in message:
        raise AssertionError(f"expected {fragment!r} in the refusal, got: {message}")
    return message


def _admin_exec(prov: support.Provisioned, statement: str) -> None:
    with _admin(prov) as conn:
        conn.execute(statement)


def test_application_privilege_verifier_accepts_topology_a(pg_ready_a):
    _verify_application_matrix(pg_ready_a)


def test_application_privilege_verifier_accepts_topology_b(pg_ready):
    _verify_application_matrix(pg_ready)


def test_verifier_refuses_migrator_superuser(pg_ready_a):
    _admin_exec(pg_ready_a, f"ALTER ROLE {pg_ready_a.migrator_role} SUPERUSER")
    _expect_application_refusal(pg_ready_a, "is a superuser")


def test_verifier_refuses_excessive_migrator_update(pg_ready_a):
    _admin_exec(
        pg_ready_a,
        f"GRANT UPDATE ON TABLE public.positions TO {pg_ready_a.migrator_role}",
    )
    _expect_application_refusal(
        pg_ready_a, "migrator UPDATE on public.positions is True"
    )


def test_verifier_refuses_excessive_sequence_select(pg_ready_a):
    _admin_exec(
        pg_ready_a,
        "GRANT SELECT ON SEQUENCE public.strategy_leg_exposures_id_seq "
        f"TO {pg_ready_a.migrator_role}",
    )
    _expect_application_refusal(
        pg_ready_a, "has SELECT on the exposure id sequence"
    )


def test_verifier_refuses_extra_membership_elevation_path(pg_ready_a):
    _admin_exec(
        pg_ready_a,
        f"GRANT {pg_ready_a.migrator_role} TO {pg_ready_a.app_role}",
    )
    _expect_application_refusal(
        pg_ready_a, f"runtime role {pg_ready_a.app_role} is a member"
    )


def test_verifier_refuses_runtime_migration_internal_access(pg_ready_a):
    _admin_exec(
        pg_ready_a,
        f"GRANT USAGE ON SCHEMA migration_internal TO {pg_ready_a.app_role}",
    )
    _expect_application_refusal(
        pg_ready_a, f"runtime role {pg_ready_a.app_role} effectively has USAGE"
    )


def test_verifier_refuses_missing_runtime_default_privileges(pg_ready_a):
    _admin_exec(
        pg_ready_a,
        "ALTER DEFAULT PRIVILEGES FOR ROLE "
        f"{pg_ready_a.migrator_role} IN SCHEMA public REVOKE DELETE ON TABLES "
        f"FROM {pg_ready_a.app_role}",
    )
    _expect_application_refusal(
        pg_ready_a, "missing runtime table privileges ['DELETE']"
    )


def test_verifier_refuses_runtime_default_truncate(pg_ready_a):
    """A future application table must not inherit runtime TRUNCATE."""
    _admin_exec(
        pg_ready_a,
        "ALTER DEFAULT PRIVILEGES FOR ROLE "
        f"{pg_ready_a.migrator_role} IN SCHEMA public GRANT TRUNCATE ON TABLES "
        f"TO {pg_ready_a.app_role}",
    )
    _expect_application_refusal(
        pg_ready_a, "excess table privileges ['TRUNCATE']"
    )


def test_verifier_refuses_runtime_default_references_and_trigger(pg_ready_a):
    """Future tables must not inherit REFERENCES or TRIGGER either."""
    for privilege in ("REFERENCES", "TRIGGER"):
        _admin_exec(
            pg_ready_a,
            "ALTER DEFAULT PRIVILEGES FOR ROLE "
            f"{pg_ready_a.migrator_role} IN SCHEMA public GRANT {privilege} "
            f"ON TABLES TO {pg_ready_a.app_role}",
        )
    message = _expect_application_refusal(
        pg_ready_a, "excess table privileges ['REFERENCES', 'TRIGGER']"
    )
    if "REFERENCES" not in message or "TRIGGER" not in message:
        raise AssertionError(f"both overgrants must be named: {message}")


def test_verifier_refuses_runtime_default_sequence_select(pg_ready_a):
    """A future sequence must not inherit runtime SELECT (last_value)."""
    _admin_exec(
        pg_ready_a,
        "ALTER DEFAULT PRIVILEGES FOR ROLE "
        f"{pg_ready_a.migrator_role} IN SCHEMA public GRANT SELECT ON SEQUENCES "
        f"TO {pg_ready_a.app_role}",
    )
    _expect_application_refusal(
        pg_ready_a, "excess sequence privileges ['SELECT']"
    )


def test_verifier_refuses_runtime_default_sequence_update(pg_ready_a):
    """A future sequence must not inherit runtime UPDATE (setval)."""
    _admin_exec(
        pg_ready_a,
        "ALTER DEFAULT PRIVILEGES FOR ROLE "
        f"{pg_ready_a.migrator_role} IN SCHEMA public GRANT UPDATE ON SEQUENCES "
        f"TO {pg_ready_a.app_role}",
    )
    _expect_application_refusal(
        pg_ready_a, "excess sequence privileges ['UPDATE']"
    )


def test_verifier_refuses_runtime_sequence_select_on_the_exposure_sequence(
    pg_ready_a,
):
    """The existing exposure sequence is USAGE-only for the runtime too."""
    _admin_exec(
        pg_ready_a,
        "GRANT SELECT ON SEQUENCE public.strategy_leg_exposures_id_seq "
        f"TO {pg_ready_a.app_role}",
    )
    _expect_application_refusal(
        pg_ready_a,
        f"{pg_ready_a.app_role} has SELECT on the exposure id sequence",
    )


def test_verifier_refuses_topology_b_inheriting_membership(pg_ready):
    _admin_exec(
        pg_ready,
        f"GRANT {pg_ready.owner_role} TO {pg_ready.migrator_role} "
        "WITH INHERIT TRUE, SET TRUE, ADMIN FALSE",
    )
    _expect_application_refusal(pg_ready, "INHERIT TRUE")


def test_verifier_refuses_topology_b_login_owner(pg_ready):
    _admin_exec(pg_ready, f"ALTER ROLE {pg_ready.owner_role} LOGIN")
    _expect_application_refusal(pg_ready, "Topology B requires NOLOGIN")


def test_verifier_refuses_runtime_membership_of_owner(pg_ready):
    _admin_exec(
        pg_ready,
        f"GRANT {pg_ready.owner_role} TO {pg_ready.app_role}",
    )
    _expect_application_refusal(
        pg_ready, f"runtime role {pg_ready.app_role} is a member of {pg_ready.owner_role}"
    )


def test_verifier_refuses_runtime_create_on_public(pg_ready_a):
    _admin_exec(
        pg_ready_a, f"GRANT CREATE ON SCHEMA public TO {pg_ready_a.app_role}"
    )
    _expect_application_refusal(
        pg_ready_a,
        f"runtime role {pg_ready_a.app_role} effectively has CREATE on schema public",
    )


def test_verifier_refuses_runtime_createdb(pg_ready_a):
    _admin_exec(pg_ready_a, f"ALTER ROLE {pg_ready_a.app_role} CREATEDB")
    _expect_application_refusal(
        pg_ready_a, f"runtime identity {pg_ready_a.app_role} has CREATEDB"
    )


def test_verifier_refuses_runtime_createrole(pg_ready_a):
    _admin_exec(pg_ready_a, f"ALTER ROLE {pg_ready_a.app_role} CREATEROLE")
    _expect_application_refusal(
        pg_ready_a, f"runtime identity {pg_ready_a.app_role} has CREATEROLE"
    )


def test_verifier_refuses_runtime_bypassrls(pg_ready_a):
    _admin_exec(pg_ready_a, f"ALTER ROLE {pg_ready_a.app_role} BYPASSRLS")
    _expect_application_refusal(
        pg_ready_a, f"runtime identity {pg_ready_a.app_role} has BYPASSRLS"
    )


def test_verifier_refuses_runtime_sequence_update(pg_ready_a):
    _admin_exec(
        pg_ready_a,
        "GRANT UPDATE ON SEQUENCE public.strategy_leg_exposures_id_seq "
        f"TO {pg_ready_a.app_role}",
    )
    _expect_application_refusal(
        pg_ready_a,
        f"{pg_ready_a.app_role} has UPDATE on the exposure id sequence",
    )


def test_verifier_refuses_runtime_membership_of_any_role(pg_ready_a):
    """A membership outside migrator/owner is still an unreviewed path."""
    _admin_exec(
        pg_ready_a, f"GRANT pg_read_all_data TO {pg_ready_a.app_role}"
    )
    _expect_application_refusal(
        pg_ready_a,
        f"runtime role {pg_ready_a.app_role} is a member of pg_read_all_data",
    )


def test_verifier_refuses_transitive_migration_schema_access(pg_ready_a):
    """Access reached only through an intermediate role is still caught."""
    bridge = f"{pg_ready_a.app_role}_bridge"
    _admin_exec(pg_ready_a, f"CREATE ROLE {bridge} NOLOGIN")
    _admin_exec(
        pg_ready_a, f"GRANT USAGE ON SCHEMA migration_internal TO {bridge}"
    )
    _admin_exec(pg_ready_a, f"GRANT {bridge} TO {pg_ready_a.app_role}")
    try:
        _expect_application_refusal(
            pg_ready_a,
            f"runtime role {pg_ready_a.app_role} effectively has USAGE on migration_internal",
        )
    finally:
        _admin_exec(
            pg_ready_a, f"REVOKE USAGE ON SCHEMA migration_internal FROM {bridge}"
        )
        _admin_exec(pg_ready_a, f"DROP ROLE {bridge}")


def test_verifier_refuses_owner_membership_of_migrator(pg_ready):
    """PostgreSQL refuses the direct cycle; the verifier still states the rule.

    ``GRANT migrator TO owner`` cannot exist while ``migrator`` is a member of
    ``owner``: PostgreSQL rejects circular membership. The verifier's
    ``pg_has_role(owner, migrator, 'MEMBER')`` check is therefore
    defence-in-depth against a produced/imported state, and the constructible
    equivalent — owner reachable into migration privileges through an
    intermediate role — is the test below it.
    """
    import psycopg

    with _admin(pg_ready) as conn, pytest.raises(psycopg.Error) as exc:
        conn.execute(
            f"GRANT {pg_ready.migrator_role} TO {pg_ready.owner_role}"
        )
    assert "is a member of" in str(exc.value)
    _verify_application_matrix(pg_ready)


def test_verifier_refuses_owner_membership_path(pg_ready):
    """An intermediate role is enough to give the owner unintended access."""
    bridge = f"{pg_ready.owner_role}_bridge"
    _admin_exec(pg_ready, f"CREATE ROLE {bridge} NOLOGIN")
    _admin_exec(
        pg_ready, f"GRANT USAGE ON SCHEMA migration_internal TO {bridge}"
    )
    _admin_exec(pg_ready, f"GRANT {bridge} TO {pg_ready.owner_role}")
    try:
        _expect_application_refusal(
            pg_ready,
            f"owner role {pg_ready.owner_role} is a member of {bridge}",
        )
    finally:
        _admin_exec(
            pg_ready, f"REVOKE USAGE ON SCHEMA migration_internal FROM {bridge}"
        )
        _admin_exec(pg_ready, f"DROP ROLE {bridge}")


def test_verifier_refuses_owner_membership_of_unintended_role(pg_ready):
    _admin_exec(pg_ready, f"GRANT pg_read_all_data TO {pg_ready.owner_role}")
    _expect_application_refusal(
        pg_ready,
        f"owner role {pg_ready.owner_role} is a member of pg_read_all_data",
    )


def test_verifier_refuses_owner_createrole(pg_ready):
    _admin_exec(pg_ready, f"ALTER ROLE {pg_ready.owner_role} CREATEROLE")
    _expect_application_refusal(
        pg_ready, f"owner role {pg_ready.owner_role} has CREATEROLE"
    )


def test_verifier_refuses_owner_createdb(pg_ready):
    _admin_exec(pg_ready, f"ALTER ROLE {pg_ready.owner_role} CREATEDB")
    _expect_application_refusal(
        pg_ready, f"owner role {pg_ready.owner_role} has CREATEDB"
    )


def test_verifier_refuses_owner_migration_internal_access(pg_ready):
    _admin_exec(
        pg_ready,
        f"GRANT USAGE ON SCHEMA migration_internal TO {pg_ready.owner_role}",
    )
    _expect_application_refusal(
        pg_ready,
        f"owner role {pg_ready.owner_role} effectively has USAGE on migration_internal",
    )


def test_bad_privilege_matrix_aborts_the_guarded_upgrade(pg_topology_a):
    """An invalid identity is refused by the real path, before any lease work."""
    _bootstrap(pg_topology_a)
    db_module._run_alembic_migrations()
    # An accidental table grant is corrected by the §8.3 revokes that every
    # upgrade re-applies, so it cannot demonstrate enforcement. A superuser
    # attribute is not something the DDL path repairs, it makes the effective
    # matrix unprovable, and the connection-identity guard refuses it before
    # the lease is acquired — the refusal moved earlier on purpose.
    _admin_exec(
        pg_topology_a, f"ALTER ROLE {pg_topology_a.migrator_role} SUPERUSER"
    )
    result = support.run_cli(pg_topology_a, "upgrade", "head")
    if result.returncode == 0:
        raise AssertionError(
            "a bad application privilege matrix must abort the guarded upgrade"
        )
    output = result.stdout + result.stderr
    if "45004" not in output or "has SUPERUSER" not in output:
        raise AssertionError(f"unexpected refusal output: {output[-2000:]}")


# ---------------------------------------------------------------------------
# Cluster identity: deterministic system_identifier evidence
# ---------------------------------------------------------------------------


def test_both_identities_read_one_cluster_system_identifier(pg_ready_a):
    """Deterministic identity needs no superuser: both roles read the same id."""
    from sqlalchemy import create_engine

    migrator = create_engine(db_module.normalize_database_url(pg_ready_a.migrator_url))
    runtime = create_engine(db_module.normalize_database_url(pg_ready_a.app_url))
    try:
        with migrator.connect() as conn:
            migration_identity = db_module._logical_target_identity(conn)
        with runtime.connect() as conn:
            runtime_identity = db_module._logical_target_identity(conn)
    finally:
        migrator.dispose()
        runtime.dispose()

    if not migration_identity[2] or not runtime_identity[2]:
        raise AssertionError(
            "both identities must read pg_control_system().system_identifier on "
            f"stock PostgreSQL 16; got {migration_identity[2]!r} and "
            f"{runtime_identity[2]!r}"
        )
    assert migration_identity == runtime_identity
    if len(migration_identity) != len(db_module._TARGET_IDENTITY_FIELDS):
        raise AssertionError(
            "the identity must contain exactly "
            f"{db_module._TARGET_IDENTITY_FIELDS}, got {migration_identity!r}"
        )


def test_guarded_upgrade_refuses_runtime_public_create(pg_topology_a):
    """The real startup path refuses to commit an invalid runtime schema grant."""
    _bootstrap(pg_topology_a)
    db_module._run_alembic_migrations()
    _admin_exec(
        pg_topology_a,
        f"GRANT CREATE ON SCHEMA public TO {pg_topology_a.app_role}",
    )
    result = support.run_cli(pg_topology_a, "upgrade", "head")
    if result.returncode == 0:
        raise AssertionError(
            "a runtime CREATE on public must abort the guarded upgrade"
        )
    output = result.stdout + result.stderr
    if "45004" not in output or "application privilege verification failed" not in output:
        raise AssertionError(f"unexpected refusal output: {output[-2000:]}")


def test_logical_target_verification_uses_the_cluster_identifier(pg_ready_a):
    """The real guarded path publishes a restart-stable proven identity."""
    from sqlalchemy import create_engine

    engine = db_module._migration_engine()
    try:
        published = db_module._RUNTIME_TARGET_SIGNATURE[0]
        probe = create_engine(db_module.normalize_database_url(pg_ready_a.app_url))
        try:
            with probe.connect() as conn:
                live = db_module._logical_target_identity(conn)
                conn.rollback()
        finally:
            probe.dispose()
    finally:
        engine.dispose()
    if published is None:
        raise AssertionError("startup preflight did not publish a runtime identity")
    if not published[2]:
        raise AssertionError(
            f"the published identity carries no cluster identifier: {published!r}"
        )
    if published != live:
        raise AssertionError(
            "the published runtime identity does not match the live target: "
            f"{published!r} != {live!r}"
        )


def test_logical_target_refuses_a_different_cluster_identifier(
    pg_ready_a, monkeypatch
):
    from sqlalchemy import create_engine

    values = iter(["7710572404024957629", "7710894975794810555"])
    monkeypatch.setattr(
        db_module, "_cluster_system_identifier", lambda conn: next(values)
    )
    engine = create_engine(db_module.normalize_database_url(pg_ready_a.migrator_url))
    try:
        with pytest.raises(LeaseError) as exc:
            db_module._verify_logical_target(engine)
    finally:
        engine.dispose()
    _assert_sqlstate(exc, "45004")
    assert "cluster identifiers" in str(exc.value)


class _SqlstateError(Exception):
    """Minimal DBAPI error carrying the SQLSTATE the verifier inspects."""

    def __init__(self, sqlstate: str):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _FailingConnection:
    """Connection whose identity query always raises a chosen SQLSTATE."""

    def __init__(self, sqlstate: str):
        self._sqlstate = sqlstate

    def exec_driver_sql(self, statement):
        from sqlalchemy.exc import DBAPIError

        raise DBAPIError(
            statement, {}, _SqlstateError(self._sqlstate), connection_invalidated=False
        )


def test_unreadable_cluster_identifier_is_refused_with_the_remedy():
    """42501 (revoked) and 42883 (absent) refuse instead of degrading."""
    for state in ("42501", "42883"):
        with pytest.raises(LeaseError) as exc:
            db_module._cluster_system_identifier(_FailingConnection(state))
        _assert_sqlstate(exc, "45004")
        message = str(exc.value)
        if state not in message:
            raise AssertionError(f"the refusal must name SQLSTATE {state}: {message}")
        if "grant EXECUTE on pg_control_system()" not in message:
            raise AssertionError(
                f"the refusal must name the one grant that restores it: {message}"
            )


def test_cluster_identifier_unexpected_error_fails_closed():
    """A connection failure must never be read as "the function is unavailable"."""
    with pytest.raises(LeaseError) as exc:
        db_module._cluster_system_identifier(_FailingConnection("08006"))
    _assert_sqlstate(exc, "45004")
    message = str(exc.value)
    if "08006" not in message or "refusing to prove the target" not in message:
        raise AssertionError(f"unexpected refusal message: {message}")


def test_logical_target_refuses_when_the_identifier_is_revoked(pg_ready_a):
    """The real documented revocation refuses instead of proving weakly.

    Postmaster start time is not identity: after a restart the weaker
    fingerprint would either reject the real target or accept another one, so
    an unreadable ``system_identifier`` fails closed with the exact grant.
    """
    from sqlalchemy import create_engine

    saved = db_module._RUNTIME_TARGET_SIGNATURE[0]
    _admin_exec(
        pg_ready_a,
        "REVOKE EXECUTE ON FUNCTION pg_control_system() FROM PUBLIC",
    )
    try:
        probe = create_engine(
            db_module.normalize_database_url(pg_ready_a.migrator_url)
        )
        try:
            with probe.connect() as conn:
                with pytest.raises(LeaseError) as exc:
                    db_module._cluster_system_identifier(conn)
        finally:
            probe.dispose()
        _assert_sqlstate(exc, "45004")
        if "grant EXECUTE on pg_control_system()" not in str(exc.value):
            raise AssertionError(f"unexpected refusal: {exc.value}")

        engine = create_engine(
            db_module.normalize_database_url(pg_ready_a.migrator_url)
        )
        try:
            with pytest.raises(LeaseError) as exc:
                db_module._verify_logical_target(engine)
        finally:
            engine.dispose()
        _assert_sqlstate(exc, "45004")
        if "system_identifier" not in str(exc.value):
            raise AssertionError(f"unexpected refusal: {exc.value}")
        if db_module._RUNTIME_TARGET_SIGNATURE[0] is not None:
            raise AssertionError(
                "an unproven target must not be published as the runtime reference"
            )
    finally:
        _admin_exec(
            pg_ready_a,
            "GRANT EXECUTE ON FUNCTION pg_control_system() TO PUBLIC",
        )
        db_module._RUNTIME_TARGET_SIGNATURE[0] = saved


def test_logical_target_refuses_an_empty_cluster_identifier(
    pg_ready_a, monkeypatch
):
    """Even a reader that yields nothing must not become an accepted target."""
    from sqlalchemy import create_engine

    monkeypatch.setattr(db_module, "_cluster_system_identifier", lambda conn: None)
    engine = create_engine(db_module.normalize_database_url(pg_ready_a.migrator_url))
    try:
        with pytest.raises(LeaseError) as exc:
            db_module._verify_logical_target(engine)
    finally:
        engine.dispose()
    _assert_sqlstate(exc, "45004")
    if "cannot be identified across a restart" not in str(exc.value):
        raise AssertionError(f"unexpected refusal: {exc.value}")


# ---------------------------------------------------------------------------
# Deterministic two-process startup contention (PostgreSQL-observed)
# ---------------------------------------------------------------------------


def _with_application_name(url: str, name: str) -> str:
    from sqlalchemy.engine import make_url

    return (
        make_url(url)
        .update_query_dict({"application_name": name})
        .render_as_string(hide_password=False)
    )


def _startup_env(prov: support.Provisioned, name: str) -> dict[str, str]:
    return prov.env(
        STRIKENOVA_OWNER_ROLE=settings.STRIKENOVA_OWNER_ROLE,
        DATABASE_URL=_with_application_name(prov.app_url, name),
        STRIKENOVA_MIGRATION_DATABASE_URL=_with_application_name(
            prov.migrator_url, name
        ),
    )


def _activity(conn, name: str):
    return conn.execute(
        "SELECT pid, state, wait_event_type, wait_event, query "
        "FROM pg_stat_activity WHERE application_name = %s",
        (name,),
    ).fetchone()


def _wait_for(conn, predicate, *, deadline: float, description: str):
    while time.monotonic() < deadline:
        row = predicate()
        if row:
            return row
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {description}")


def _guard_key(conn) -> int:
    """The unsigned 64-bit key ``pg_advisory_xact_lock`` is given.

    ``pg_locks`` renders a 64-bit advisory key as an unsigned ``(classid << 32)
    | objid`` pair, while ``hashtextextended`` returns a signed bigint, so the
    expected value is folded into the same unsigned range.
    """
    key = conn.execute(
        "SELECT hashtextextended("
        "'strikenova:migration_internal:migration_guard', 0)"
    ).fetchone()[0]
    return key % (1 << 64)


def _advisory_granted(conn, pid: int, key: int, *, granted: bool) -> int:
    return conn.execute(
        "SELECT count(*) FROM pg_locks l WHERE l.pid = %s "
        "AND l.locktype = 'advisory' AND l.granted = %s "
        "AND (l.classid::bigint::numeric * 4294967296) "
        "+ l.objid::bigint::numeric = %s",
        (pid, granted, key),
    ).fetchone()[0]


def test_two_process_startup_serializes_the_per_user_backfill(pg_topology_a):
    """Deterministic overlap INSIDE the first protected per-user backfill.

    Worker A is paused by a database trigger on ``strategy_leg_exposures``
    that waits on an advisory lock held by this test. By the time it blocks, A
    has entered its first per-user transaction (``lease_enter_v1`` has taken
    the transaction advisory guard and the lease row lock) and performed real
    work (the INSERT that fired the trigger). Worker B is then observed by
    PostgreSQL waiting on the *admission* guard with no lease row lock, which
    is what proves the backfill is serialized, not merely the DDL. Releasing
    the lock proves B proceeds only after A releases ownership.

    The previous revision blocked A in its DDL transaction, which let A finish
    the whole backfill before B arrived: a regression that released the lease
    before the backfill would still have passed.
    """
    pg = pg_topology_a
    _bootstrap(pg)
    db_module._run_alembic_migrations()
    support.seed_legacy_users(pg.admin_database_url, users=2)

    worker = "from app.db import init_db; init_db()"
    env = _startup_env(pg, "sn145_worker_a")
    env_b = _startup_env(pg, "sn145_worker_b")
    procs: list[subprocess.Popen] = []
    blocker = psycopg.connect(pg.admin_database_url, autocommit=True)
    try:
        with psycopg.connect(pg.admin_database_url, autocommit=True) as setup:
            key = setup.execute(
                "SELECT hashtextextended("
                "'strikenova:issue145:backfill_pause', 0)"
            ).fetchone()[0]
            setup.execute(
                "CREATE OR REPLACE FUNCTION public._issue145_pause_insert() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                "PERFORM pg_advisory_xact_lock(hashtextextended("
                "'strikenova:issue145:backfill_pause', 0)); "
                "RETURN NEW; END $$"
            )
            setup.execute(
                "CREATE TRIGGER _issue145_pause_trigger BEFORE INSERT ON "
                "public.strategy_leg_exposures FOR EACH ROW EXECUTE FUNCTION "
                "public._issue145_pause_insert()"
            )
        unsigned_pause = key % (1 << 64)
        blocker.execute("SELECT pg_advisory_lock(%s)", (key,))

        a = subprocess.Popen(
            [sys.executable, "-c", worker],
            cwd=os.getcwd(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        procs.append(a)
        deadline = time.monotonic() + 90
        with psycopg.connect(pg.admin_database_url, autocommit=True) as observer:
            guardian = _guard_key(observer)

            # A is paused in the INSERT inside its first per-user transaction.
            row_a = _wait_for(
                observer,
                lambda: (
                    row
                    if (row := _activity(observer, "sn145_worker_a"))
                    and row[2] == "Lock"
                    and _advisory_granted(
                        observer, row[0], unsigned_pause, granted=False
                    )
                    else None
                ),
                deadline=deadline,
                description="worker A to pause inside its first backfill insert",
            )
            pid_a = row_a[0]
            if not _advisory_granted(observer, pid_a, guardian, granted=True):
                raise AssertionError(
                    "worker A does not hold the admission guard while paused in "
                    "the backfill"
                )
            hold = observer.execute(
                "SELECT count(*) FROM pg_locks l WHERE l.pid = %s "
                "AND l.relation = 'migration_internal.migration_lease'::regclass "
                "AND l.granted",
                (pid_a,),
            ).fetchone()[0]
            if not hold:
                raise AssertionError(
                    "worker A does not own the lease row while paused in the "
                    "protected backfill"
                )

            # A granted row-level lock is not a pg_locks row, so prove the
            # lock by trying to take it: an observer transaction must get
            # SQLSTATE 55P03 on the exact lease row while A owns it.
            probe = psycopg.connect(pg.admin_database_url)
            try:
                with pytest.raises(psycopg.errors.LockNotAvailable) as probe_exc:
                    probe.execute(
                        "SELECT 1 FROM migration_internal.migration_lease "
                        "WHERE lease_name = 'migration-lease' FOR UPDATE NOWAIT"
                    ).fetchone()
                if probe_exc.value.sqlstate != "55P03":
                    raise AssertionError(
                        "the row-lock probe failed with SQLSTATE "
                        f"{probe_exc.value.sqlstate}, expected 55P03"
                    )
            finally:
                probe.rollback()
                probe.close()

            b = subprocess.Popen(
                [sys.executable, "-c", worker],
                cwd=os.getcwd(),
                env=env_b,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            procs.append(b)

            row_b = _wait_for(
                observer,
                lambda: (
                    row
                    if (row := _activity(observer, "sn145_worker_b"))
                    and _advisory_granted(observer, row[0], guardian, granted=False)
                    else None
                ),
                deadline=deadline,
                description="worker B to wait for lease admission",
            )
            pid_b = row_b[0]
            row_locks_b = observer.execute(
                "SELECT count(*) FROM pg_locks l WHERE l.pid = %s "
                "AND l.relation = 'migration_internal.migration_lease'::regclass",
                (pid_b,),
            ).fetchone()[0]
            if row_locks_b:
                raise AssertionError(
                    "worker B reached the lease row before worker A released it"
                )
            if a.poll() is not None:
                raise AssertionError(
                    "worker A finished while it was supposed to be paused in the "
                    "backfill"
                )
            still_owned = observer.execute(
                "SELECT count(*) FROM pg_locks l WHERE l.pid = %s "
                "AND l.relation = 'migration_internal.migration_lease'::regclass "
                "AND l.granted",
                (pid_a,),
            ).fetchone()[0]
            if not still_owned:
                raise AssertionError(
                    "worker A lost the lease while worker B was waiting"
                )

        # Resume A. Only now may B be admitted and finish.
        blocker.execute("SELECT pg_advisory_unlock(%s)", (key,))

        for proc in procs:
            stdout, stderr = proc.communicate(timeout=240)
            output = stdout + stderr
            if proc.returncode != 0:
                raise AssertionError(
                    f"startup worker failed rc={proc.returncode}: {output[-2000:]}"
                )
            if "23505" in output:
                raise AssertionError("startup workers reported SQLSTATE 23505")

        # Negative control: once A (and B) released the lease, the same exact
        # row probe must succeed rather than raise 55P03.
        after = psycopg.connect(pg.admin_database_url)
        try:
            after.execute(
                "SELECT 1 FROM migration_internal.migration_lease "
                "WHERE lease_name = 'migration-lease' FOR UPDATE NOWAIT"
            ).fetchone()
        finally:
            after.rollback()
            after.close()

        counts = _exposure_counts(pg)
        if counts != {f"issue145-user-{u}": 1 for u in (1, 2)}:
            raise AssertionError(f"unexpected exposure ledger after contention: {counts}")
        unowned, _, _, _ = _lease_row(pg)
        if not unowned:
            raise AssertionError("the lease was left owned after both workers exited")
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=30)
        try:
            blocker.execute("SELECT pg_advisory_unlock_all()")
        except Exception:  # noqa: BLE001 - cleanup must not mask the result
            pass
        blocker.close()
        try:
            with psycopg.connect(pg.admin_database_url, autocommit=True) as cleanup:
                cleanup.execute(
                    "DROP TRIGGER IF EXISTS _issue145_pause_trigger "
                    "ON public.strategy_leg_exposures"
                )
                cleanup.execute(
                    "DROP FUNCTION IF EXISTS public._issue145_pause_insert()"
                )
        except Exception:  # noqa: BLE001 - the disposable database is dropped too
            pass


# ---------------------------------------------------------------------------
# Actual connection identity (ADR-018 §8.1): the session, not just the URL
# ---------------------------------------------------------------------------


def _login_url(prov: support.Provisioned, role: str, password: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(prov.admin_url).set(
        username=role, password=password, database=prov.database
    ).render_as_string(hide_password=False)


def _create_login(prov: support.Provisioned, suffix: str) -> tuple[str, str]:
    """Create an ordinary LOGIN role in the disposable server; return its URL."""
    import secrets

    role = f"{prov.prefix}_{suffix}"
    password = secrets.token_hex(16)
    _admin_exec(prov, f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
    return role, _login_url(prov, role, password)


def _startup_state(prov: support.Provisioned) -> dict:
    """Lease ownership/generation, schema version and application data.

    Read over the disposable admin connection: the Topology B migrator is not
    granted ``alembic_version``, and the observation itself must not depend on
    the privileges under test.
    """
    with _admin(prov) as conn:
        version = conn.execute(
            "SELECT version_num FROM public.alembic_version"
        ).fetchone()
        positions = conn.execute("SELECT count(*) FROM public.positions").fetchone()[0]
    return {
        "lease": _lease_row(prov),
        "alembic_version": version[0] if version else None,
        "positions": int(positions),
        "exposures": _exposure_counts(prov),
    }


def _assert_startup_refused(
    prov: support.Provisioned, migration_url: str, *fragments: str
) -> str:
    """Run the real startup path with a wrong migration login and prove no work."""
    before = _startup_state(prov)
    result = run_module(
        prov,
        ["app.migrate", "upgrade", "head"],
        STRIKENOVA_MIGRATION_DATABASE_URL=migration_url,
    )
    output = result.stdout + result.stderr
    if result.returncode == 0:
        raise AssertionError(f"startup must refuse the wrong identity: {output[-2000:]}")
    if "45004" not in output:
        raise AssertionError(f"expected SQLSTATE 45004 in the refusal: {output[-2000:]}")
    for fragment in fragments:
        if fragment not in output:
            raise AssertionError(
                f"expected {fragment!r} in the refusal output: {output[-2000:]}"
            )
    after = _startup_state(prov)
    if after != before:
        raise AssertionError(f"a refused startup changed state: {before} -> {after}")
    return output


def test_startup_refuses_the_superuser_login_before_any_work(pg_ready):
    """The admin/superuser login is not the configured migrator."""
    _assert_startup_refused(
        pg_ready,
        pg_ready.admin_database_url,
        "not the configured migration identity",
    )


def test_startup_refuses_an_unrelated_login_before_any_work(pg_ready):
    role, url = _create_login(pg_ready, "outsider")
    try:
        _assert_startup_refused(
            pg_ready, url, "not the configured migration identity"
        )
    finally:
        _admin_exec(pg_ready, f"DROP ROLE IF EXISTS {role}")


def test_startup_refuses_a_login_holding_migrator_membership(pg_ready):
    """Membership must not let another login pass as the migrator."""
    from sqlalchemy import create_engine

    role, url = _create_login(pg_ready, "member")
    _admin_exec(
        pg_ready,
        f"GRANT {pg_ready.migrator_role} TO {role} WITH INHERIT TRUE, SET TRUE",
    )
    try:
        _assert_startup_refused(
            pg_ready, url, "not the configured migration identity"
        )
        # The membership does not help even when the login elevates itself:
        # session_user still names the unapproved login, so the session check
        # refuses before any statement of the migration path can run.
        engine = create_engine(db_module.normalize_database_url(url))
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql(f"SET ROLE {pg_ready.migrator_role}")
                with pytest.raises(LeaseError) as exc:
                    db_module._assert_migration_session_identity(
                        conn, stage="the membership test connection"
                    )
        finally:
            engine.dispose()
        _assert_sqlstate(exc, "45004")
        message = str(exc.value)
        if "session_user" not in message or role not in message:
            raise AssertionError(f"unexpected identity refusal: {message}")
    finally:
        _admin_exec(pg_ready, f"REVOKE {pg_ready.migrator_role} FROM {role}")
        _admin_exec(pg_ready, f"DROP ROLE IF EXISTS {role}")


def test_startup_refuses_a_superuser_migration_role(pg_ready):
    """A URL naming the migrator is still refused if that role is a superuser."""
    _admin_exec(pg_ready, f"ALTER ROLE {pg_ready.migrator_role} SUPERUSER")
    _assert_startup_refused(pg_ready, pg_ready.migrator_url, "has SUPERUSER")


def test_session_identity_accepts_the_migrator_connection(pg_ready_a):
    engine = _application_check_connection(pg_ready_a)
    try:
        with engine.connect() as conn:
            db_module._assert_migration_session_identity(
                conn, stage="the positive control connection"
            )
    finally:
        engine.dispose()


def test_verifier_refuses_a_non_login_migration_identity(pg_ready):
    role = f"{pg_ready.prefix}_nologin"
    _admin_exec(pg_ready, f"CREATE ROLE {role} NOLOGIN")
    try:
        engine = _application_check_connection(pg_ready)
        try:
            with engine.connect() as conn, pytest.raises(LeaseError) as exc:
                lease_mod.verify_application_privileges(
                    conn,
                    migrator_role=role,
                    runtime_role=pg_ready.app_role,
                    owner_role=None,
                )
        finally:
            engine.dispose()
        _assert_sqlstate(exc, "45004")
        if "is not a LOGIN role" not in str(exc.value):
            raise AssertionError(f"unexpected refusal: {exc.value}")
    finally:
        _admin_exec(pg_ready, f"DROP ROLE IF EXISTS {role}")


def test_verifier_refuses_unexpected_incoming_migrator_membership(pg_ready_a):
    """Topology A: no second role may inherit or assume the migrator."""
    role, _ = _create_login(pg_ready_a, "inheritor")
    _admin_exec(pg_ready_a, f"GRANT {pg_ready_a.migrator_role} TO {role}")
    try:
        _expect_application_refusal(
            pg_ready_a,
            f"roles ['{role}'] are members of the migration identity "
            f"{pg_ready_a.migrator_role}",
        )
    finally:
        _admin_exec(pg_ready_a, f"REVOKE {pg_ready_a.migrator_role} FROM {role}")
        _admin_exec(pg_ready_a, f"DROP ROLE IF EXISTS {role}")


def test_verifier_refuses_unexpected_incoming_owner_membership(pg_ready):
    """Topology B: the migrator is the owner role's only approved member."""
    role, _ = _create_login(pg_ready, "co_owner")
    _admin_exec(pg_ready, f"GRANT {pg_ready.owner_role} TO {role}")
    try:
        _expect_application_refusal(
            pg_ready,
            f"roles ['{role}'] are members of the owner role {pg_ready.owner_role}",
        )
    finally:
        _admin_exec(pg_ready, f"REVOKE {pg_ready.owner_role} FROM {role}")
        _admin_exec(pg_ready, f"DROP ROLE IF EXISTS {role}")


# ---------------------------------------------------------------------------
# Column-level privilege bypass (ADR §8.3 table matrix is not the whole ACL)
# ---------------------------------------------------------------------------


def test_verifier_refuses_column_level_migrator_update_topology_b(pg_ready):
    _admin_exec(
        pg_ready,
        f"GRANT UPDATE (status) ON TABLE public.positions TO {pg_ready.migrator_role}",
    )
    try:
        _expect_application_refusal(
            pg_ready,
            "column-level UPDATE on public.positions.status",
        )
    finally:
        _admin_exec(
            pg_ready,
            f"REVOKE UPDATE (status) ON TABLE public.positions "
            f"FROM {pg_ready.migrator_role}",
        )


def test_verifier_refuses_column_level_migrator_references_topology_b(pg_ready):
    _admin_exec(
        pg_ready,
        f"GRANT REFERENCES (status) ON TABLE public.positions "
        f"TO {pg_ready.migrator_role}",
    )
    try:
        _expect_application_refusal(
            pg_ready,
            "column-level REFERENCES on public.positions.status",
        )
    finally:
        _admin_exec(
            pg_ready,
            f"REVOKE REFERENCES (status) ON TABLE public.positions "
            f"FROM {pg_ready.migrator_role}",
        )


def test_verifier_refuses_column_level_migrator_update_topology_a(pg_ready_a):
    """Topology A's owner-only restriction is also checked per column."""
    _admin_exec(
        pg_ready_a,
        f"GRANT UPDATE (status) ON TABLE public.positions "
        f"TO {pg_ready_a.migrator_role}",
    )
    try:
        _expect_application_refusal(
            pg_ready_a,
            "column-level UPDATE on public.positions.status",
        )
    finally:
        _admin_exec(
            pg_ready_a,
            f"REVOKE UPDATE (status) ON TABLE public.positions "
            f"FROM {pg_ready_a.migrator_role}",
        )


def test_column_level_update_aborts_the_guarded_upgrade(pg):
    """A column-level bypass cannot reach a commit through the startup path.

    Topology B's guarded upgrade installs its four grants but never an UPDATE
    grant, so a column-level UPDATE survives the topology step and the
    in-transaction verifier must abort the whole migration.
    """
    _bootstrap(pg)
    db_module._run_alembic_migrations()
    _admin_exec(
        pg,
        f"GRANT UPDATE (status) ON TABLE public.positions TO {pg.migrator_role}",
    )
    try:
        with pytest.raises(LeaseError) as exc:
            db_module._run_alembic_migrations()
        _assert_sqlstate(exc, "45004")
        if "column-level UPDATE" not in str(exc.value):
            raise AssertionError(f"unexpected refusal: {exc.value}")
        unowned, generation, _, _ = _lease_row(pg)
        if not unowned or generation != 2:
            raise AssertionError(
                "the aborted migration left the lease owned or did not "
                f"release the acquisition (unowned={unowned} generation={generation})"
            )
    finally:
        _admin_exec(
            pg,
            f"REVOKE UPDATE (status) ON TABLE public.positions "
            f"FROM {pg.migrator_role}",
        )


# ---------------------------------------------------------------------------
# Protocol mirror consistency (lease row vs control row)
# ---------------------------------------------------------------------------


def test_verify_refuses_an_inconsistent_protocol_mirror_without_repair(pg_ready):
    """A version-2 lease row under a version-1 control row must fail closed.

    Both rows are individually valid, but ``lease_acquire_v1`` compares them,
    so the next admission would fail 45006. Verification must catch the pair
    and roll back without changing either row.
    """
    with psycopg.connect(pg_ready.admin_database_url) as conn:
        control_before = conn.execute(
            "SELECT installed_version, min_supported_version, "
            "max_supported_version, barrier_state, bootstrap_state "
            "FROM migration_internal.lease_protocol"
        ).fetchone()
        lease_before = conn.execute(
            "SELECT protocol_version, owner_token_hash, generation "
            "FROM migration_internal.migration_lease"
        ).fetchone()
        conn.execute(
            "UPDATE migration_internal.migration_lease SET protocol_version = 2"
        )
        conn.commit()
    try:
        message = _verify_refusal(pg_ready)
        if "does not match the installed protocol" not in message:
            raise AssertionError(f"unexpected refusal: {message}")
        with psycopg.connect(pg_ready.admin_database_url) as conn:
            control_after = conn.execute(
                "SELECT installed_version, min_supported_version, "
                "max_supported_version, barrier_state, bootstrap_state "
                "FROM migration_internal.lease_protocol"
            ).fetchone()
            lease_after = conn.execute(
                "SELECT protocol_version, owner_token_hash, generation "
                "FROM migration_internal.migration_lease"
            ).fetchone()
        if control_after != control_before:
            raise AssertionError(
                f"verification changed the control row: {control_before} -> "
                f"{control_after}"
            )
        if lease_after[0] != 2 or lease_after[1:] != lease_before[1:]:
            raise AssertionError(
                "verification silently repaired the mismatched lease row: "
                f"{lease_after}"
            )
    finally:
        _admin_exec(
            pg_ready,
            "UPDATE migration_internal.migration_lease SET protocol_version = 1",
        )


# ---------------------------------------------------------------------------
# Runtime identity is read from the connection, not from the URL
# ---------------------------------------------------------------------------


def test_runtime_engine_refuses_a_query_parameter_identity_override(pg_ready):
    """A URL that authenticates as the migrator cannot pass as the runtime.

    SQLAlchemy/psycopg merges ``?user=`` into the connect arguments, so the
    URL's authority username is not the principal that actually logs in.
    """
    from sqlalchemy.engine import make_url

    url = (
        make_url(pg_ready.app_url)
        .set(password=pg_ready.migrator_password)
        .update_query_dict({"user": pg_ready.migrator_role})
        .render_as_string(hide_password=False)
    )
    engine = db_module._create_runtime_engine(db_module.normalize_database_url(url))
    try:
        with pytest.raises(LeaseError) as exc:
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
    finally:
        engine.dispose()
    _assert_sqlstate(exc, "45004")
    message = str(exc.value)
    if pg_ready.migrator_role not in message:
        raise AssertionError(f"unexpected refusal: {message}")
    if "runtime identity" not in message:
        raise AssertionError(f"unexpected refusal: {message}")
    if pg_ready.migrator_password in message:
        raise AssertionError("the refusal leaked a password")


def test_verified_runtime_role_comes_from_the_live_session(pg_ready):
    """The verifier uses the authenticated principal, and it is the app role."""
    if db_module._verified_runtime_role() != pg_ready.app_role:
        raise AssertionError("the verified runtime role is not the app role")


# ---------------------------------------------------------------------------
# ConfigParser interpolation: URL-encoded credentials on the guarded path
# ---------------------------------------------------------------------------


def test_guarded_upgrade_accepts_url_encoded_credentials(pg_ready, monkeypatch):
    """A password containing ``%40`` must not be interpreted by ConfigParser.

    The guarded PostgreSQL path supplies a live connection to ``env.py``, so
    the credential-bearing URL is never passed to ``set_main_option``. Before
    the correction, ``%40`` raised an interpolation error before the lease.
    """
    from sqlalchemy.engine import make_url

    password = "a@b%40c"
    _admin_exec(
        pg_ready,
        f"ALTER ROLE {pg_ready.migrator_role} PASSWORD '{password}'",
    )
    url = (
        make_url(pg_ready.migrator_url)
        .set(password=password)
        .render_as_string(hide_password=False)
    )
    if "%" not in url:
        raise AssertionError("the test URL did not encode the special characters")
    if make_url(url).password != password:
        raise AssertionError("the URL round-trip changed the password")
    monkeypatch.setattr(settings, "STRIKENOVA_MIGRATION_DATABASE_URL", url)
    try:
        db_module._run_alembic_migrations()
    finally:
        # Restore the fixture's password: the helper connections (and the
        # fixture teardown) still use ``migrator_url``. A real deployment does
        # not rotate the login as part of a migration; this only isolates the
        # interpolation test.
        _admin_exec(
            pg_ready,
            f"ALTER ROLE {pg_ready.migrator_role} "
            f"PASSWORD '{pg_ready.migrator_password}'",
        )
    unowned, _, _, _ = _lease_row(pg_ready)
    if not unowned:
        raise AssertionError("the lease was not released after the guarded upgrade")


def test_new_migration_connection_must_reach_the_proven_database(pg_ready_a):
    """A later connection to another database is rejected before any statement.

    The guard records the preflight signature and re-reads it on every new
    migration connection. Pointing the same guard at a database other than the
    one it proved must fail closed, which is the failover/substitution case.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    other = support.provision(topology=support.TOPOLOGY_A)
    try:
        other_engine = create_engine(
            db_module.normalize_database_url(other.migrator_url),
            poolclass=NullPool,
        )
        try:
            with other_engine.connect() as conn:
                other_signature = db_module._logical_target_identity(conn)
                conn.rollback()
        finally:
            other_engine.dispose()
        if other_signature[0] == pg_ready_a.database:
            raise AssertionError("the substitution test did not use another database")

        holder = [other_signature]
        engine = create_engine(
            db_module.normalize_database_url(pg_ready_a.migrator_url),
            poolclass=NullPool,
        )
        db_module._install_migration_identity_guard(engine, holder)
        try:
            with pytest.raises(LeaseError) as exc:
                with engine.connect() as conn:
                    conn.exec_driver_sql("SELECT 1")
        finally:
            engine.dispose()
        _assert_sqlstate(exc, "45004")
        message = str(exc.value)
        if "does not reach the same PostgreSQL cluster/database" not in message:
            raise AssertionError(f"unexpected refusal: {message}")
    finally:
        support.drop(other)


def test_new_runtime_connection_must_reach_the_proven_target(pg_ready):
    """A correctly authenticated runtime connection to another target is refused.

    The runtime listener now compares every new connection with the signature
    startup preflight proved. Tampering only the expected signature exercises
    the deterministic rejection a provider failover to another cluster would
    produce, without needing a second cluster.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    migrator = create_engine(
        db_module.normalize_database_url(pg_ready.migrator_url), poolclass=NullPool
    )
    try:
        with migrator.connect() as conn:
            actual = db_module._logical_target_identity(conn)
            conn.rollback()
    finally:
        migrator.dispose()

    saved = db_module._RUNTIME_TARGET_SIGNATURE[0]
    try:
        db_module._RUNTIME_TARGET_SIGNATURE[0] = (
            actual[0],
            actual[1],
            "different-cluster-identifier",
        )
        engine = db_module._create_runtime_engine(
            db_module.normalize_database_url(pg_ready.app_url)
        )
        try:
            with pytest.raises(LeaseError) as exc:
                with engine.connect() as conn:
                    conn.exec_driver_sql("SELECT 1")
        finally:
            engine.dispose()
        _assert_sqlstate(exc, "45004")
        message = str(exc.value)
        if "unproven target" not in message:
            raise AssertionError(f"unexpected refusal: {message}")
        if pg_ready.app_password in message:
            raise AssertionError("the runtime target refusal leaked a password")

        # Positive control: the proven signature accepts the same connection.
        db_module._RUNTIME_TARGET_SIGNATURE[0] = actual
        engine = db_module._create_runtime_engine(
            db_module.normalize_database_url(pg_ready.app_url)
        )
        try:
            with engine.connect() as conn:
                if conn.exec_driver_sql("SELECT 1").scalar() != 1:
                    raise AssertionError("the proven runtime connection did not work")
        finally:
            engine.dispose()
    finally:
        db_module._RUNTIME_TARGET_SIGNATURE[0] = saved


def test_target_signature_is_timezone_independent(pg_ready):
    """Two sessions with different TimeZone settings agree on the target.

    The identity itself must be timezone-independent, and the diagnostic
    postmaster epoch must also agree: it is compared in reports, never as
    identity.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    engine = create_engine(
        db_module.normalize_database_url(pg_ready.migrator_url), poolclass=NullPool
    )
    try:
        with engine.connect() as utc, engine.connect() as kolkata:
            utc.exec_driver_sql("SET TIME ZONE 'UTC'")
            kolkata.exec_driver_sql("SET TIME ZONE 'Asia/Kolkata'")
            first = db_module._logical_target_identity(utc)
            second = db_module._logical_target_identity(kolkata)
            first_started = db_module._postmaster_start_epoch_us(utc)
            second_started = db_module._postmaster_start_epoch_us(kolkata)
            utc.rollback()
            kolkata.rollback()
    finally:
        engine.dispose()
    if first != second:
        raise AssertionError(
            "the target identity depends on the session time zone: "
            f"{first!r} != {second!r}"
        )
    if first_started != second_started:
        raise AssertionError(
            "the postmaster start diagnostic depends on the session time zone: "
            f"{first_started!r} != {second_started!r}"
        )
    if not str(first_started).lstrip("-").isdigit():
        raise AssertionError(
            "the postmaster start time is not canonical epoch microseconds: "
            f"{first_started!r}"
        )
    if first_started in first:
        raise AssertionError(
            "the postmaster start time must not be part of the target identity: "
            f"{first!r} contains {first_started!r}"
        )


def test_effective_public_create_is_refused(pg_ready):
    """Inherited PUBLIC CREATE on public must fail the runtime matrix check."""
    _admin_exec(pg_ready, "GRANT CREATE ON SCHEMA public TO PUBLIC")
    try:
        _expect_application_refusal(pg_ready, "CREATE on schema public")
    finally:
        _admin_exec(pg_ready, "REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    # The refusal is gone once the inherited PUBLIC grant is removed.
    _verify_application_matrix(pg_ready)
