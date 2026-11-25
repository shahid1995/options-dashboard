"""Real PostgreSQL restart proof for the ADR-017/018 target identity.

The identity a guarded migration proves must survive a PostgreSQL process
restart (and a Neon compute suspend/wake) while still rejecting another
cluster or another database. This module proves that against a cluster it
creates and restarts itself.

Requirements and safety:

* Set ``ISSUE145_PG_BINDIR`` to a PostgreSQL **16.16** binary directory
  (``initdb``, ``pg_ctl``, ``postgres``). The module refuses any other
  ``server_version_num`` instead of quietly testing a different build.
* The module runs ``initdb`` in a pytest ``tmp_path`` and starts/restarts that
  scratch server only. It never restarts, writes to, or even connects to the
  cluster ``ISSUE145_PG_ADMIN_URL`` names, and never touches Neon, staging,
  production or Render.

Why opt-in rather than part of the CI suites: the CI job's PostgreSQL is a
service container, which the job cannot restart (and must not, because the
integration and regression suites share it). The restart behaviour is proven
against a self-owned 16.16 cluster; the runbook records both the proof and
this limitation.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import socket
import subprocess
import tempfile

import psycopg
import pytest

from app import db as db_module
from app._migration_lease import LeaseError
from app.config import settings

BINDIR_ENV = "ISSUE145_PG_BINDIR"
REQUIRED_SERVER_VERSION_NUM = 160016

MIGRATOR_ROLE = "issue145_restart_migrator"
APP_ROLE = "issue145_restart_app"
TARGET_DATABASE = "issue145_restart_target"
OTHER_DATABASE = "issue145_restart_other"


def _configured_bindir() -> pathlib.Path:
    value = os.environ.get(BINDIR_ENV, "").strip()
    if not value:
        pytest.skip(
            f"{BINDIR_ENV} is not set; no restartable PostgreSQL 16.16 build",
            allow_module_level=True,
        )
    bindir = pathlib.Path(value)
    missing = [
        name for name in ("initdb", "pg_ctl", "postgres") if not (bindir / name).exists()
    ]
    if missing:
        pytest.skip(
            f"{BINDIR_ENV} does not contain {missing}",
            allow_module_level=True,
        )
    return bindir


BINDIR = _configured_bindir()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=180
    )


class _ScratchCluster:
    """A disposable PostgreSQL cluster this module may start and restart."""

    def __init__(self, root: pathlib.Path):
        self.root = root
        self.data = root / "data"
        # The Unix-domain socket path has a hard length limit (107 bytes) and a
        # pytest ``tmp_path`` is already long, so the socket lives in its own
        # short temporary directory. Connections still use TCP on 127.0.0.1.
        self.socket_dir = pathlib.Path(tempfile.mkdtemp(prefix="i145pg"))
        self.log = root / "server.log"
        self.port = _free_port()
        self.started = False

    # -- process control ---------------------------------------------------
    def _options(self) -> str:
        return (
            f"-p {self.port} -k {self.socket_dir} "
            "-c listen_addresses=127.0.0.1 -c fsync=off"
        )

    def initdb(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _run(
            [
                str(BINDIR / "initdb"),
                "-D",
                str(self.data),
                "-A",
                "trust",
                "-U",
                "postgres",
                "--no-sync",
                "--no-locale",
                "-E",
                "UTF8",
            ]
        )

    def start(self) -> None:
        _run(
            [
                str(BINDIR / "pg_ctl"),
                "-D",
                str(self.data),
                "-l",
                str(self.log),
                "-o",
                self._options(),
                "-w",
                "-t",
                "120",
                "start",
            ]
        )
        self.started = True

    def restart(self) -> None:
        """Restart the server process, keeping the same data directory."""
        _run(
            [
                str(BINDIR / "pg_ctl"),
                "-D",
                str(self.data),
                "-l",
                str(self.log),
                "-o",
                self._options(),
                "-w",
                "-t",
                "120",
                "restart",
            ]
        )

    def stop(self) -> None:
        if not self.started:
            return
        try:
            _run(
                [
                    str(BINDIR / "pg_ctl"),
                    "-D",
                    str(self.data),
                    "-m",
                    "fast",
                    "-w",
                    "-t",
                    "60",
                    "stop",
                ]
            )
        except subprocess.SubprocessError:
            pass
        finally:
            self.started = False
            shutil.rmtree(self.socket_dir, ignore_errors=True)

    # -- SQL helpers -------------------------------------------------------
    @property
    def admin_url(self) -> str:
        return f"postgresql://postgres@127.0.0.1:{self.port}/postgres"

    def admin_url_for(self, database: str = "postgres") -> str:
        return f"postgresql://postgres@127.0.0.1:{self.port}/{database}"

    def admin_exec(self, statement: str, *, database: str = "postgres") -> None:
        """Run an admin statement in ``database``.

        Function ACLs live in each database's own ``pg_proc``, so a grant or
        revoke must name the database the guarded connections actually use.
        """
        with psycopg.connect(self.admin_url_for(database), autocommit=True) as conn:
            conn.execute(statement)

    def scalar(self, statement: str):
        with psycopg.connect(self.admin_url) as conn:
            return conn.execute(statement).fetchone()[0]

    def provision_target(self) -> None:
        """Create the two logins and the target database this module proves."""
        version = int(self.scalar("SHOW server_version_num"))
        if version != REQUIRED_SERVER_VERSION_NUM:
            raise AssertionError(
                "the restart proof requires PostgreSQL 16.16 "
                f"(server_version_num {REQUIRED_SERVER_VERSION_NUM}), got {version}"
            )
        self.admin_exec(f"CREATE ROLE {MIGRATOR_ROLE} LOGIN")
        self.admin_exec(f"CREATE ROLE {APP_ROLE} LOGIN")
        self.admin_exec(f"CREATE DATABASE {TARGET_DATABASE} OWNER postgres")

    def role_url(self, role: str, database: str = TARGET_DATABASE) -> str:
        return (
            f"postgresql+psycopg://{role}@127.0.0.1:{self.port}/{database}"
        )

    @property
    def migrator_url(self) -> str:
        return self.role_url(MIGRATOR_ROLE)

    @property
    def app_url(self) -> str:
        return self.role_url(APP_ROLE)

    def postmaster_start_epoch_us(self) -> int:
        return int(
            self.scalar(
                "SELECT (extract(epoch from pg_postmaster_start_time()) "
                "* 1000000)::bigint"
            )
        )


def _configure(monkeypatch, cluster: _ScratchCluster) -> None:
    """Point the guarded configuration at this scratch cluster only."""
    monkeypatch.setattr(settings, "DATABASE_URL", cluster.app_url)
    monkeypatch.setattr(
        settings, "STRIKENOVA_MIGRATION_DATABASE_URL", cluster.migrator_url
    )
    monkeypatch.setattr(settings, "STRIKENOVA_OWNER_ROLE", "")
    monkeypatch.setattr(settings, "STRIKENOVA_MIGRATOR_ROLE", MIGRATOR_ROLE)
    monkeypatch.setattr(settings, "STRIKENOVA_RUNTIME_ROLE", APP_ROLE)
    monkeypatch.setattr(settings, "STRIKENOVA_LEASE_NONCE", "")
    monkeypatch.setattr(settings, "MIGRATION_LEASE_TTL_SECONDS", 300.0)
    monkeypatch.setattr(settings, "MIGRATION_LEASE_WAIT_SECONDS", 180.0)
    monkeypatch.setattr(settings, "MIGRATION_LEASE_LOCK_TIMEOUT_MS", 5000)


@pytest.fixture(autouse=True)
def _reset_runtime_reference():
    """Never let one test's proven target authorize the next test."""
    saved = db_module._RUNTIME_TARGET_SIGNATURE[0]
    db_module._RUNTIME_TARGET_SIGNATURE[0] = None
    yield
    db_module._RUNTIME_TARGET_SIGNATURE[0] = saved


@pytest.fixture
def cluster(tmp_path):
    scratch = _ScratchCluster(tmp_path / "cluster")
    scratch.initdb()
    scratch.start()
    try:
        scratch.provision_target()
        yield scratch
    finally:
        scratch.stop()


def _assert_sqlstate(exc: pytest.ExceptionInfo, state: str) -> None:
    if not isinstance(exc.value, LeaseError):
        raise AssertionError(f"expected LeaseError, got {type(exc.value)!r}")
    if exc.value.sqlstate != state:
        raise AssertionError(
            f"expected SQLSTATE {state}, got {exc.value.sqlstate}: {exc.value}"
        )


def test_target_identity_survives_a_real_postmaster_restart(cluster, monkeypatch):
    """A restart changes the start time but not the proven target identity."""
    _configure(monkeypatch, cluster)

    engine = db_module._migration_engine()
    try:
        with engine.connect() as conn:
            before = db_module._logical_target_identity(conn)
            conn.rollback()
    finally:
        engine.dispose()

    started_before = cluster.postmaster_start_epoch_us()
    cluster.restart()
    started_after = cluster.postmaster_start_epoch_us()
    if started_after <= started_before:
        raise AssertionError(
            "the restarted server did not publish a later postmaster start time: "
            f"{started_after} <= {started_before}"
        )

    # A brand-new migration engine re-runs the full preflight after the
    # restart: the migration connection must be accepted for the same target.
    engine = db_module._migration_engine()
    try:
        published = db_module._RUNTIME_TARGET_SIGNATURE[0]
        with engine.connect() as conn:
            after = db_module._logical_target_identity(conn)
            # The per-connection guard on an already-proven engine accepts it.
            if conn.exec_driver_sql("SELECT 1").scalar() != 1:
                raise AssertionError("the migration connection did not work")
    finally:
        engine.dispose()

    if after != before:
        raise AssertionError(
            "the target identity changed across a restart: "
            f"{before!r} != {after!r}"
        )
    if published != before:
        raise AssertionError(
            "preflight published a different runtime identity after the restart: "
            f"{published!r} != {before!r}"
        )
    if started_after in published:
        raise AssertionError(
            "the postmaster start time leaked into the identity: "
            f"{published!r} contains {started_after}"
        )

    # A new runtime connection against the re-proven target is accepted.
    runtime = db_module._create_runtime_engine(cluster.app_url)
    try:
        with runtime.connect() as conn:
            if conn.exec_driver_sql("SELECT 1").scalar() != 1:
                raise AssertionError("the runtime connection did not work")
            conn.rollback()
    finally:
        runtime.dispose()


def test_other_database_in_the_same_cluster_is_still_refused(cluster, monkeypatch):
    """Restart stability must not blur two databases of one cluster."""
    _configure(monkeypatch, cluster)
    engine = db_module._migration_engine()
    engine.dispose()
    published = db_module._RUNTIME_TARGET_SIGNATURE[0]
    if published is None:
        raise AssertionError("preflight did not publish a runtime identity")

    cluster.admin_exec(f"CREATE DATABASE {OTHER_DATABASE} OWNER postgres")
    runtime = db_module._create_runtime_engine(
        cluster.role_url(APP_ROLE, OTHER_DATABASE)
    )
    try:
        with pytest.raises(LeaseError) as exc:
            with runtime.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
    finally:
        runtime.dispose()
    _assert_sqlstate(exc, "45004")
    message = str(exc.value)
    if "unproven target" not in message:
        raise AssertionError(f"unexpected refusal: {message}")


def test_other_cluster_with_the_same_database_is_still_refused(
    cluster, monkeypatch, tmp_path
):
    """A second cluster with identical names is rejected by system_identifier."""
    _configure(monkeypatch, cluster)
    engine = db_module._migration_engine()
    engine.dispose()

    other = _ScratchCluster(tmp_path / "other-cluster")
    other.initdb()
    other.start()
    try:
        other.admin_exec(f"CREATE ROLE {MIGRATOR_ROLE} LOGIN")
        other.admin_exec(f"CREATE ROLE {APP_ROLE} LOGIN")
        other.admin_exec(f"CREATE DATABASE {TARGET_DATABASE} OWNER postgres")

        # Same database name and OID pattern, different cluster: the guard on a
        # proven runtime engine must refuse it.
        runtime = db_module._create_runtime_engine(other.app_url)
        try:
            with pytest.raises(LeaseError) as exc:
                with runtime.connect() as conn:
                    conn.exec_driver_sql("SELECT 1")
        finally:
            runtime.dispose()
        _assert_sqlstate(exc, "45004")
        if "unproven target" not in str(exc.value):
            raise AssertionError(f"unexpected refusal: {exc.value}")

        # Preflight itself compares the two clusters' identifiers.
        from sqlalchemy import create_engine

        monkeypatch.setattr(settings, "DATABASE_URL", cluster.app_url)
        cross = create_engine(other.migrator_url)
        try:
            with pytest.raises(LeaseError) as exc:
                db_module._verify_logical_target(cross)
        finally:
            cross.dispose()
        _assert_sqlstate(exc, "45004")
        if "cluster identifiers" not in str(exc.value):
            raise AssertionError(f"unexpected refusal: {exc.value}")
    finally:
        other.stop()


def test_unreadable_identifier_fails_closed_after_a_restart(cluster, monkeypatch):
    """A revoked pg_control_system() is refused, never accepted weakly."""
    _configure(monkeypatch, cluster)
    engine = db_module._migration_engine()
    engine.dispose()
    cluster.restart()
    cluster.admin_exec(
        "REVOKE EXECUTE ON FUNCTION pg_control_system() FROM PUBLIC",
        database=TARGET_DATABASE,
    )
    try:
        with pytest.raises(LeaseError) as exc:
            db_module._migration_engine()
        _assert_sqlstate(exc, "45004")
        message = str(exc.value)
        if "grant EXECUTE on pg_control_system()" not in message:
            raise AssertionError(f"the refusal must name the remedy: {message}")
        if db_module._RUNTIME_TARGET_SIGNATURE[0] is not None:
            raise AssertionError(
                "an unproven target must not be published as the runtime reference"
            )
    finally:
        cluster.admin_exec(
            "GRANT EXECUTE ON FUNCTION pg_control_system() TO PUBLIC",
            database=TARGET_DATABASE,
        )
