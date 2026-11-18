"""Issue #145 behavioral rehearsal on a REAL PostgreSQL 16 server.

Proves that two independent ``app.db.init_db()`` startup processes serialize
their migrations AND the startup exposure backfill through the ADR-017
database-backed lease, that the lease is genuinely renewed while a backfill
runs longer than its TTL, and that the unique source-order constraint
(``uq_exposure_source_order``) is never violated (no SQLSTATE 23505).

Gating
------
These tests are inert unless ALL of the following hold:

* ``ISSUE145_REHEARSAL=1``
* ``GITHUB_ACTIONS=true`` and ``RUNNER_ENVIRONMENT=github-hosted`` (a
  precondition only -- runner strings never prove disposability)
* ``ISSUE145_DISPOSABLE_CONFIRM=issue145-disposable`` (explicit human intent)
* ``ISSUE145_BOOTSTRAP_DATABASE_URL`` is a PASSWORD-authenticated PostgreSQL
  URL on loopback using the ``postgres``/``template1`` maintenance database

The bootstrap URL is normalized exactly like the application does
(``app.db.normalize_database_url``): bare ``postgres://``/``postgresql://``
become ``postgresql+psycopg://``, so every engine this harness builds uses
the installed psycopg 3 driver instead of SQLAlchemy's psycopg2 default.

Once the gate passes, the test asks the server itself before creating
anything: PostgreSQL 16.x, loopback address and expected port, the
``/.dockerenv`` marker from the official image, the image's data directory, a
fresh postmaster (bounded uptime), an empty cluster (apart from this harness's
own leftovers) and no other client sessions. Only then does it create a
uniquely named database
(``issue145_rehearsal_<random>``) plus dedicated ``issue145_migrator_*`` and
``issue145_app_*`` roles, and drops them afterwards. It refuses to touch any
database it did not create, refuses non-loopback hosts, and refuses when
``STRIKENOVA_ENV=production``. No credentials are printed and none reach the
log files: role passwords are generated per run, worker processes redact
credentials before writing stdout/stderr and error markers, and the parent
redacts again before displaying anything.

Failure policy
--------------
Any assertion or worker failure fails the test. Skipping only happens when the
rehearsal environment is not configured at all; a half-configured environment
(e.g. missing confirm token) FAILS instead of silently skipping.
"""
from __future__ import annotations

import ipaddress
import os
import re
import secrets
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REHEARSAL_FLAG = "ISSUE145_REHEARSAL"
CONFIRM_ENV = "ISSUE145_DISPOSABLE_CONFIRM"
CONFIRM_TOKEN = "issue145-disposable"
BOOTSTRAP_ENV = "ISSUE145_BOOTSTRAP_DATABASE_URL"

# This rehearsal creates and drops databases, roles and a password credential.
# It is therefore restricted to the ephemeral, network-isolated PostgreSQL
# service of a GitHub-hosted Actions runner: loopback plus a database name does
# NOT prove disposability (a local install or an SSH tunnel looks identical).
GITHUB_ACTIONS_ENV = "GITHUB_ACTIONS"
RUNNER_ENVIRONMENT_ENV = "RUNNER_ENVIRONMENT"
GITHUB_HOSTED_RUNNER = "github-hosted"
MAINTENANCE_DATABASES = ("postgres", "template1")

# Single-pass credential redaction: scheme://<anything but @/whitespace>@
REDACT_PATTERN = r"(://)[^@\s]*@"
_URL_CREDENTIAL_RE = re.compile(REDACT_PATTERN)

# A job-local service container is identified by the server itself, not by
# runner environment strings: the official postgres image carries
# /.dockerenv, keeps its cluster under /var/lib/postgresql/data, and starts
# with an empty cluster and no other sessions.
SERVER_IMAGE_DATA_DIR = "/var/lib/postgresql/data"
SYSTEM_DATABASES = ("postgres", "template0", "template1")
# The workflow job is capped at 30 minutes, so a job-local service container
# can never be older than that; the default ceiling stays above it to avoid
# failing on a slow dependency install while still rejecting a long-lived
# server.
try:
    MAX_SERVER_UPTIME_SECONDS = int(os.getenv("ISSUE145_MAX_SERVER_UPTIME_SECONDS", "3600"))
except ValueError:  # pragma: no cover - defensive configuration
    MAX_SERVER_UPTIME_SECONDS = 3600

DB_PREFIX = "issue145_rehearsal_"
MIGRATOR_ROLE_PREFIX = "issue145_migrator_"
APP_ROLE_PREFIX = "issue145_app_"

# Lease timing for the rehearsal (production defaults are TTL 120 / wait 900).
# TTL 6s renews every max(1.0, 6/3) = 2s, so a ~10s hold spans several
# renewals while remaining far below the waiter budget.
REHEARSAL_TTL = 6
REHEARSAL_WAIT = 180
SCENARIO_TIMEOUT = 180.0

EXPECTED_EXPOSURES = 4  # 2 users x 2 filled entry orders, all backfillable

REQUIRED_CONSTRAINT = "uq_exposure_source_order"


_WORKER_REDACT_SUPPORT = r'''
REDACT_RE = re.compile(os.environ["ISSUE145_REDACT_PATTERN"])


def redact(text):
    """Apply the harness credential pattern before anything reaches disk."""
    return REDACT_RE.sub(r"\1***@", str(text))


class _RedactingStream:
    """Line-buffered stream that redacts URL credentials before writing."""

    def __init__(self, stream):
        self._stream = stream
        self._buffer = ""

    def write(self, text):
        self._buffer += text
        if "\n" not in self._buffer:
            return len(text)
        head, _, tail = self._buffer.rpartition("\n")
        self._buffer = tail
        self._stream.write(REDACT_RE.sub(r"\1***@", head + "\n"))
        return len(text)

    def flush(self):
        if self._buffer:
            self._stream.write(REDACT_RE.sub(r"\1***@", self._buffer))
            self._buffer = ""
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)
'''

_WORKER_SCRIPT = textwrap.dedent(
    r'''
    import logging
    import os
    import re
    import sys
    import time

    BACKEND = os.environ["ISSUE145_WORKER_BACKEND"]
    MARKERS = os.environ["ISSUE145_WORKER_MARKERS"]
    WORKER_ID = sys.argv[1]
    MODE = os.environ.get("ISSUE145_WORKER_MODE", "none")
    PEER = os.environ.get("ISSUE145_WORKER_PEER", "")

    __REDACT_SUPPORT__

    # Redact before anything reaches the log files: logging, print() and
    # traceback output all flow through these wrappers.
    sys.stdout = _RedactingStream(sys.stdout)
    sys.stderr = _RedactingStream(sys.stderr)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


    def path(name):
        return os.path.join(MARKERS, name)


    def put(name, value="1"):
        tmp = path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(value))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path(name))


    def wait_for(name, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if os.path.exists(path(name)):
                return True
            time.sleep(0.05)
        return False


    def main():
        sys.path.insert(0, BACKEND)

        from sqlalchemy.engine import make_url

        from app import db as db_module
        from app.services import leg_exposure

        app_role = os.environ["ISSUE145_WORKER_APP_ROLE"]
        migrator_role = os.environ["ISSUE145_WORKER_MIGRATOR_ROLE"]

        runtime_identity = make_url(db_module.engine.url)
        migration_identity = make_url(db_module._migration_engine_url())
        if runtime_identity.username != app_role:
            raise SystemExit(
                "runtime engine identity is not the application role: "
                + repr(runtime_identity.username)
            )
        if migration_identity.username != migrator_role:
            raise SystemExit(
                "migration identity is not the migrator role: "
                + repr(migration_identity.username)
            )
        if runtime_identity.database != migration_identity.database:
            raise SystemExit("runtime and migration identities target different databases")

        real_backfill = leg_exposure.backfill_all_exposures

        def instrumented_backfill(session):
            put("backfill_entered_" + WORKER_ID, time.time())
            if MODE == "hold_for_peer_begin":
                if not wait_for("begin_" + PEER, 120):
                    raise SystemExit("peer startup never began")
            elif MODE == "hold_until_released":
                if not wait_for("release_holder", 240):
                    raise SystemExit("holder release marker never appeared")
            created = real_backfill(session)
            put("backfill_exited_" + WORKER_ID, time.time())
            put("backfill_created_" + WORKER_ID, created)
            return created

        leg_exposure.backfill_all_exposures = instrumented_backfill

        from app import _migration_lock as mlock

        real_try_acquire = mlock.try_acquire

        def instrumented_try_acquire(*args, **kwargs):
            result = real_try_acquire(*args, **kwargs)
            with open(
                path("acquire_attempts_" + WORKER_ID), "a", encoding="utf-8"
            ) as handle:
                handle.write(f"{time.time()} {bool(result[0])} {bool(result[1])}\n")
                handle.flush()
                os.fsync(handle.fileno())
            return result

        mlock.try_acquire = instrumented_try_acquire

        put("begin_" + WORKER_ID, time.time())
        db_module.init_db()
        put("done_" + WORKER_ID, time.time())


    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - surface to the parent
        try:
            put("error_" + WORKER_ID, redact(f"{type(exc).__name__}: {exc}"))
        except Exception:  # noqa: BLE001 - best effort
            pass
        raise
    '''
).replace("__REDACT_SUPPORT__", _WORKER_REDACT_SUPPORT.strip("\n"))


def _require_rehearsal_env() -> str:
    if os.getenv(REHEARSAL_FLAG) != "1":
        pytest.skip(
            "Issue #145 PostgreSQL rehearsal not configured "
            "(set ISSUE145_REHEARSAL=1 and ISSUE145_BOOTSTRAP_DATABASE_URL)"
        )
    # Fail closed from here on: nothing below opens a connection, and the
    # fixture provisions nothing until this function returns a URL.
    if os.getenv(GITHUB_ACTIONS_ENV) != "true":
        pytest.fail(
            f"{GITHUB_ACTIONS_ENV} is not 'true': this rehearsal is restricted to "
            "a GitHub-hosted Actions runner and refuses to touch any other server"
        )
    if os.getenv(RUNNER_ENVIRONMENT_ENV) != GITHUB_HOSTED_RUNNER:
        pytest.fail(
            f"{RUNNER_ENVIRONMENT_ENV} is not {GITHUB_HOSTED_RUNNER!r}: the "
            "PostgreSQL service is only guaranteed disposable on an "
            "ephemeral GitHub-hosted runner"
        )
    if os.getenv(CONFIRM_ENV) != CONFIRM_TOKEN:
        pytest.fail(
            f"{REHEARSAL_FLAG}=1 without {CONFIRM_ENV}={CONFIRM_TOKEN!r}: "
            "refusing to create databases on an unconfirmed server"
        )
    raw = os.getenv(BOOTSTRAP_ENV, "")
    if not raw:
        pytest.fail(f"{REHEARSAL_FLAG}=1 but {BOOTSTRAP_ENV} is not set")
    if "production" in (os.getenv("STRIKENOVA_ENV", "") or "").lower():
        pytest.fail("refusing to rehearse against STRIKENOVA_ENV=production")
    # Reuse the application's own mapping so the harness connects exactly like
    # production does: SQLAlchemy's bare ``postgresql://`` dialect targets
    # psycopg2, which this project does not install.
    from app.db import normalize_database_url

    normalized = normalize_database_url(raw)
    try:
        url = make_url(normalized)
    except Exception as exc:  # noqa: BLE001 - malformed configuration
        pytest.fail(f"{BOOTSTRAP_ENV} is not a valid SQLAlchemy URL: {exc}")
    if url.get_backend_name() != "postgresql":
        pytest.fail(f"{BOOTSTRAP_ENV} must be a PostgreSQL URL")
    if not url.password:
        pytest.fail(
            f"{BOOTSTRAP_ENV} must carry the per-run service password; "
            "password authentication is required (trust auth is not accepted)"
        )
    if (url.host or "") not in ("127.0.0.1", "localhost", "::1"):
        pytest.fail(
            f"{BOOTSTRAP_ENV} must point at loopback (got host {url.host!r}); "
            "this rehearsal only runs against the job-local service container"
        )
    maintenance_db = url.database or ""
    if maintenance_db not in MAINTENANCE_DATABASES:
        pytest.fail(
            f"{BOOTSTRAP_ENV} must use one of the maintenance databases "
            f"{MAINTENANCE_DATABASES} (got {maintenance_db!r})"
        )
    return normalized


def _redact(message: str) -> str:
    """Strip URL credential fragments from diagnostic text.

    Single bounded pass (no fixpoint loop): every ``scheme://user:password@``
    and every already-masked ``scheme://user:***@`` collapses to
    ``scheme://***@``. Idempotent, so re-redacting sanitized logs is safe.
    """
    return _URL_CREDENTIAL_RE.sub(r"\1***@", str(message))


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(_redact(message))


def _is_local_server_address(address: str) -> bool:
    """Accept loopback and private (container bridge) server addresses.

    ``inet_server_addr()`` renders an ``inet`` value including its netmask
    (``172.18.0.2/32``), and a GitHub Actions service container publishes its
    port on the runner's loopback interface while the postmaster itself
    reports the container's private bridge address. Loopback and private
    addresses are therefore accepted here; disposability is still decided by
    the official-image markers and cluster freshness checked alongside this,
    and a routable address is refused outright.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return parsed.is_loopback or parsed.is_private


def _validate_job_local_server(observation: dict, bootstrap_url: str) -> None:
    """Reject any PostgreSQL that is not this job's disposable container.

    Runner environment strings are only a precondition; this read-only
    observation from the server itself is what decides. Nothing here creates,
    drops, or mutates anything, so a rejection leaves teardown with no
    ownership flags (it never connects).
    """
    url = make_url(bootstrap_url)
    expected_port = url.port or 5432

    version_num = int(observation["version_num"])
    if not (160000 <= version_num < 170000):
        pytest.fail(
            f"rehearsal requires PostgreSQL 16.x, server reports "
            f"{observation['banner']!r} ({version_num})"
        )
    server_addr = str(observation["server_addr"] or "").split("/", 1)[0].strip()
    if not _is_local_server_address(server_addr):
        pytest.fail(
            "the server reports a routable address "
            f"({observation['server_addr']!r}): refusing a remote server"
        )
    if int(observation["server_port"]) != expected_port:
        pytest.fail(
            f"the server listens on port {observation['server_port']}, the "
            f"bootstrap URL uses {expected_port}"
        )
    if observation["container_marker"] is None:
        pytest.fail(
            "the server cannot read /.dockerenv: it is not the job-local "
            "postgres service container (a native or tunnelled install is not "
            "provably disposable)"
        )
    data_directory = str(observation["data_directory"] or "")
    if data_directory != SERVER_IMAGE_DATA_DIR and not data_directory.startswith(
        SERVER_IMAGE_DATA_DIR + "/"
    ):
        pytest.fail(
            f"the server data directory is {data_directory!r}, not "
            f"{SERVER_IMAGE_DATA_DIR!r}: this is not the ephemeral service "
            "container image"
        )
    uptime = float(observation["uptime_seconds"] or 0)
    if uptime < 0 or uptime > MAX_SERVER_UPTIME_SECONDS:
        pytest.fail(
            f"the server has been running for {uptime:.0f}s (limit "
            f"{MAX_SERVER_UPTIME_SECONDS}s): a long-lived server is not "
            "disposable"
        )
    databases = set(observation["databases"] or ())
    foreign_databases = {
        name
        for name in databases
        if name not in SYSTEM_DATABASES and not name.startswith(DB_PREFIX)
    }
    if foreign_databases:
        pytest.fail(
            f"the server already contains non-system databases "
            f"{sorted(foreign_databases)}: it is not a freshly started "
            "service container"
        )
    login_roles = set(observation["login_roles"] or ())
    foreign_roles = {
        name
        for name in login_roles
        if name != "postgres"
        and not name.startswith(MIGRATOR_ROLE_PREFIX)
        and not name.startswith(APP_ROLE_PREFIX)
    }
    if foreign_roles:
        pytest.fail(
            f"the server has unexpected login roles {sorted(foreign_roles)}: a "
            "fresh service container only has 'postgres'"
        )
    if int(observation["other_client_backends"] or 0) != 0:
        pytest.fail(
            f"{observation['other_client_backends']} other client session(s) "
            "older than five seconds are using the server: it is not an idle, "
            "job-local container"
        )


# --------------------------------------------------------------------------
# Disposable database + role provisioning (bootstrap identity only)
# --------------------------------------------------------------------------
class _Rehearsal:
    def __init__(self, bootstrap_url: str):
        self.suffix = uuid.uuid4().hex[:8]
        self.database = f"{DB_PREFIX}{self.suffix}"
        self.migrator_role = f"{MIGRATOR_ROLE_PREFIX}{self.suffix}"
        self.app_role = f"{APP_ROLE_PREFIX}{self.suffix}"
        self.migrator_password = secrets.token_urlsafe(24)
        self.app_password = secrets.token_urlsafe(24)
        self._bootstrap_url = bootstrap_url
        self.procs: list[subprocess.Popen] = []
        # Ownership state: only resources whose CREATE completed in THIS run
        # are ever eligible for cleanup. Nothing is inferred from a name.
        self.created_database = False
        self.created_roles: list[str] = []

    def _url_for(self, database: str, user: str | None = None, password: str | None = None) -> str:
        url = make_url(self._bootstrap_url)
        if user is not None:
            url = url.set(username=user, password=password)
        # ``str(URL)`` masks the password as ``***``; these URLs are used to
        # connect, so render the real credential explicitly.
        return url.set(database=database).render_as_string(hide_password=False)

    @property
    def admin_db_url(self) -> str:
        """Bootstrap (superuser) connection to the rehearsal database."""
        return self._url_for(self.database)

    @property
    def migrator_url(self) -> str:
        return self._url_for(self.database, self.migrator_role, self.migrator_password)

    @property
    def app_url(self) -> str:
        return self._url_for(self.database, self.app_role, self.app_password)

    # -- lifecycle --------------------------------------------------------
    def observe_server(self) -> dict:
        """Read-only identity observation taken from the server itself."""
        engine = create_engine(self._bootstrap_url)
        try:
            with engine.connect() as conn:
                observation = dict(
                    conn.execute(
                        text(
                            "SELECT current_setting('server_version_num')::int AS version_num,"
                            " current_setting('server_version') AS banner,"
                            " coalesce(inet_server_addr()::text, '') AS server_addr,"
                            " coalesce(inet_server_port(), 0) AS server_port,"
                            " current_setting('data_directory') AS data_directory,"
                            " EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time()))"
                            " AS uptime_seconds,"
                            " (SELECT array_agg(datname::text) FROM pg_database)"
                            " AS databases,"
                            " (SELECT array_agg(rolname::text) FROM pg_roles"
                            "  WHERE rolcanlogin) AS login_roles,"
                            " (SELECT count(*) FROM pg_stat_activity"
                            "  WHERE backend_type = 'client backend'"
                            "  AND pid <> pg_backend_pid()"
                            "  AND now() - backend_start > interval '5 seconds')"
                            " AS other_client_backends"
                        )
                    ).mappings().one()
                )
            try:
                with engine.connect() as probe:
                    # Superuser-only filesystem probe: the official postgres
                    # image ships /.dockerenv; a native install does not.
                    probe.execute(text("SELECT pg_read_file('/.dockerenv')")).scalar_one()
                observation["container_marker"] = ""
            except SQLAlchemyError:
                observation["container_marker"] = None
        finally:
            engine.dispose()
        return observation

    def assert_job_local_server(self) -> None:
        """Prove the target is this job's disposable container, then report it.

        Every probe here is read-only; nothing is created before this passes.
        """
        observation = self.observe_server()
        _validate_job_local_server(observation, self._bootstrap_url)
        print(
            "PostgreSQL server identity: version "
            f"{observation['banner']} ({observation['version_num']}), "
            f"data_directory={observation['data_directory']}, "
            f"uptime={float(observation['uptime_seconds']):.0f}s, "
            "container_marker=ok, other_sessions=0"
        )

    def provision(self) -> None:
        admin = create_engine(self._bootstrap_url, isolation_level="AUTOCOMMIT")
        admin_db = create_engine(self.admin_db_url, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                for role, password in (
                    (self.migrator_role, self.migrator_password),
                    (self.app_role, self.app_password),
                ):
                    literal = password.replace("'", "''")
                    conn.execute(
                        text(
                            f'CREATE ROLE "{role}" LOGIN PASSWORD \'{literal}\' '
                            "NOSUPERUSER NOCREATEDB NOCREATEROLE"
                        )
                    )
                    self.created_roles.append(role)
                conn.execute(
                    text(f'CREATE DATABASE "{self.database}" OWNER "{self.migrator_role}"')
                )
                self.created_database = True
            with admin_db.connect() as conn:
                conn.execute(text(f'REVOKE ALL ON DATABASE "{self.database}" FROM PUBLIC'))
                conn.execute(
                    text(
                        f'GRANT CONNECT ON DATABASE "{self.database}" '
                        f'TO "{self.migrator_role}", "{self.app_role}"'
                    )
                )
                # The migrator owns the schema (ADR-016/ADR-018 model): it runs
                # the DDL, the app identity only receives DML + sequence usage.
                conn.execute(text(f'ALTER SCHEMA public OWNER TO "{self.migrator_role}"'))
                conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{self.app_role}"'))
                conn.execute(
                    text(
                        f'ALTER DEFAULT PRIVILEGES FOR ROLE "{self.migrator_role}" '
                        f'IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE '
                        f'ON TABLES TO "{self.app_role}"'
                    )
                )
                conn.execute(
                    text(
                        f'ALTER DEFAULT PRIVILEGES FOR ROLE "{self.migrator_role}" '
                        f'IN SCHEMA public GRANT USAGE, SELECT '
                        f'ON SEQUENCES TO "{self.app_role}"'
                    )
                )
        finally:
            admin.dispose()
            admin_db.dispose()

    def _reap_workers(self) -> None:
        for proc in list(self.procs):
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                    pass
        self.procs.clear()

    def teardown(self) -> None:
        """Drop only resources this run recorded as successfully created.

        A failed gate or version check leaves both ownership flags empty, so
        teardown never even opens a connection. Each drop is attempted
        independently; failures are collected and re-raised (redacted) so a
        leaked resource is visible as a teardown error instead of a warning.
        """
        self._reap_workers()
        if not self.created_database and not self.created_roles:
            return
        errors: list[str] = []
        try:
            admin = create_engine(self._bootstrap_url, isolation_level="AUTOCOMMIT")
        except SQLAlchemyError as exc:  # noqa: BLE001 - reported below
            raise RuntimeError(
                "rehearsal teardown failed: "
                + _redact(f"could not open the bootstrap connection: {exc}")
            ) from None
        try:
            if self.created_database:
                try:
                    with admin.connect() as conn:
                        conn.execute(
                            text(f'DROP DATABASE IF EXISTS "{self.database}" WITH (FORCE)')
                        )
                    self.created_database = False
                except SQLAlchemyError as exc:  # noqa: BLE001 - collected
                    errors.append(f"DROP DATABASE {self.database!r} failed: {exc}")
            for role in list(self.created_roles):
                try:
                    with admin.connect() as conn:
                        conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
                    self.created_roles.remove(role)
                except SQLAlchemyError as exc:  # noqa: BLE001 - collected
                    errors.append(f"DROP ROLE {role!r} failed: {exc}")
        finally:
            admin.dispose()
        if errors:
            raise RuntimeError(
                "rehearsal teardown failed: " + _redact("; ".join(errors))
            )

    # -- data -------------------------------------------------------------
    def seed(self) -> None:
        """Insert realistic positions + FILLED entry orders using the APP role.

        This doubles as proof that the application identity has the DML it
        needs on migrator-created tables (ADR-018 default privileges).
        """
        from app.models import PaperOrder, Position

        now = datetime.utcnow()
        engine = create_engine(self.app_url)
        session = sessionmaker(bind=engine)()
        try:
            for user_index in range(2):
                user_id = f"issue145-user-{user_index}"
                execution_id = f"issue145-exec-{user_index}"
                for leg_index in range(2):
                    symbol = f"REHEARSAL{user_index}{leg_index}"
                    expiry = "2026-12-24"
                    strike = 20000.0 + 100.0 * leg_index
                    position = Position(
                        user_id=user_id,
                        symbol=symbol,
                        expiry=expiry,
                        strike=strike,
                        option_type="call",
                        net_quantity=75,
                        average_entry_price=100.0,
                        lot_size=75,
                        realized_pnl=0.0,
                        status="open",
                        strategy_execution_id=execution_id,
                        opened_at=now,
                        updated_at=now,
                    )
                    session.add(position)
                    session.flush()
                    order = PaperOrder(
                        user_id=user_id,
                        client_order_id=f"issue145-{user_index}-{leg_index}",
                        execution_id=execution_id,
                        position_id=position.id,
                        kind="entry",
                        symbol=symbol,
                        expiry=expiry,
                        strike=strike,
                        option_type="call",
                        action="buy",
                        quantity=75,
                        lot_size=75,
                        status="FILLED",
                        filled_quantity=75,
                        fill_price=100.0,
                        price_source="market",
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(order)
            session.commit()
        finally:
            session.close()
            engine.dispose()

    # -- verification -----------------------------------------------------
    def _scalar(self, sql: str, **params):
        engine = create_engine(self.admin_db_url)
        try:
            with engine.connect() as conn:
                return conn.execute(text(sql), params).scalar()
        finally:
            engine.dispose()

    def _rows(self, sql: str, **params):
        engine = create_engine(self.admin_db_url)
        try:
            with engine.connect() as conn:
                return conn.execute(text(sql), params).all()
        finally:
            engine.dispose()

    def exposure_count(self) -> int:
        return int(self._scalar("SELECT count(*) FROM strategy_leg_exposures") or 0)

    def assert_exposure_ledger(self, expected: int) -> None:
        total = self.exposure_count()
        _check(
            total == expected,
            f"expected {expected} strategy_leg_exposures, found {total}",
        )
        duplicates = self._rows(
            "SELECT user_id, order_id, count(*) FROM strategy_leg_exposures "
            "GROUP BY user_id, order_id HAVING count(*) > 1"
        )
        _check(not duplicates, f"duplicate exposure rows for one source order: {duplicates}")
        distinct_orders = int(
            self._scalar("SELECT count(DISTINCT order_id) FROM strategy_leg_exposures") or 0
        )
        _check(
            distinct_orders == expected,
            f"expected {expected} distinct source orders in the ledger, found {distinct_orders}",
        )

    def assert_unique_constraint(self) -> None:
        found = self._scalar(
            "SELECT conname FROM pg_constraint WHERE conname = :name",
            name=REQUIRED_CONSTRAINT,
        )
        _check(
            found == REQUIRED_CONSTRAINT,
            f"PG constraint {REQUIRED_CONSTRAINT} is missing",
        )
        counts = self._rows(
            "SELECT order_id, count(*) FROM strategy_leg_exposures "
            "GROUP BY order_id HAVING count(*) > 1"
        )
        _check(not counts, f"source-order uniqueness is violated: {counts}")

    def assert_lease_released(self) -> None:
        rows = self._rows("SELECT locked_by, expires_at FROM _migration_lock")
        _check(len(rows) == 1, f"expected exactly one lease row, found {len(rows)}")
        _check(
            rows[0][0] is None,
            f"lease is still held by {rows[0][0]!r} after startup finished",
        )

    def lease_state(self) -> tuple[str | None, datetime | None]:
        rows = self._rows("SELECT locked_by, expires_at FROM _migration_lock")
        _check(len(rows) == 1, f"expected exactly one lease row, found {len(rows)}")
        return rows[0][0], rows[0][1]

    def assert_privilege_boundaries(self) -> None:
        # Migrator identity owns DDL and can create schema objects.
        engine = create_engine(self.migrator_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("CREATE TABLE issue145_ddl_probe (id integer)"))
                conn.execute(text("DROP TABLE issue145_ddl_probe"))
                conn.commit()
        finally:
            engine.dispose()

        owner = self._scalar(
            "SELECT tableowner FROM pg_tables "
            "WHERE schemaname='public' AND tablename='alembic_version'"
        )
        _check(
            owner == self.migrator_role,
            f"alembic_version must be owned by the migration identity, owner is {owner!r}",
        )
        lock_owner = self._scalar(
            "SELECT tableowner FROM pg_tables "
            "WHERE schemaname='public' AND tablename='_migration_lock'"
        )
        _check(
            lock_owner == self.migrator_role,
            f"_migration_lock must be owned by the migration identity, owner is {lock_owner!r}",
        )

        # Application identity: reads work, schema DDL is denied.
        self._expect_denied(
            self.app_url, "CREATE TABLE issue145_app_must_not_create (id integer)"
        )
        self._expect_denied(
            self.app_url,
            "ALTER TABLE strategy_leg_exposures ADD COLUMN must_not_exist integer",
        )
        self._expect_denied(self.app_url, "DROP TABLE strategy_leg_exposures")
        app_reads = self._count_with_url(
            self.app_url, "SELECT count(*) FROM strategy_leg_exposures"
        )
        _check(
            app_reads == self.exposure_count(),
            "application identity must be able to read the exposure ledger",
        )

    def _expect_denied(self, url: str, statement: str) -> None:
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                try:
                    conn.execute(text(statement))
                    conn.commit()
                except SQLAlchemyError as exc:  # noqa: BLE001 - the denial is the point
                    state = str(getattr(getattr(exc, "orig", None), "sqlstate", "") or "")
                    _check(
                        state == "42501",
                        f"expected insufficient_privilege (42501) for {statement!r}, "
                        f"got {state!r}",
                    )
                    return
                raise AssertionError(
                    f"application identity was allowed to execute: {statement!r}"
                )
        finally:
            engine.dispose()

    def _count_with_url(self, url: str, sql: str) -> int:
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                return int(conn.execute(text(sql)).scalar_one())
        finally:
            engine.dispose()

    # -- worker processes -------------------------------------------------
    def spawn_worker(
        self,
        markers: str,
        worker_id: str,
        mode: str = "none",
        peer: str = "",
        ttl: int = REHEARSAL_TTL,
        wait_seconds: int = REHEARSAL_WAIT,
    ) -> subprocess.Popen:
        script_path = os.path.join(markers, "_issue145_worker.py")
        if not os.path.exists(script_path):
            with open(script_path, "w", encoding="utf-8") as handle:
                handle.write(_WORKER_SCRIPT)
        env = dict(os.environ)
        env.update(
            {
                "ISSUE145_WORKER_BACKEND": BACKEND_ROOT,
                "ISSUE145_WORKER_MARKERS": markers,
                "ISSUE145_WORKER_APP_ROLE": self.app_role,
                "ISSUE145_WORKER_MIGRATOR_ROLE": self.migrator_role,
                "ISSUE145_WORKER_MODE": mode,
                "ISSUE145_WORKER_PEER": peer,
                "ISSUE145_REDACT_PATTERN": REDACT_PATTERN,
                "DATABASE_URL": self.app_url,
                "STRIKENOVA_MIGRATION_DATABASE_URL": self.migrator_url,
                "MIGRATION_LOCK_TTL_SECONDS": str(ttl),
                "MIGRATION_LOCK_WAIT_SECONDS": str(wait_seconds),
                "STRIKENOVA_ENV": "",
            }
        )
        # The bootstrap URL is the superuser credential this harness uses to
        # create and drop the disposable database. Workers authenticate as the
        # application or migrator identity, so the inherited bootstrap URL is
        # removed instead of being carried into a child environment.
        env.pop(BOOTSTRAP_ENV, None)
        stdout_path = _log_path(markers, worker_id, "stdout")
        stderr_path = _log_path(markers, worker_id, "stderr")
        stdout_handle = open(stdout_path, "wb")
        stderr_handle = open(stderr_path, "wb")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", script_path, worker_id],
                env=env,
                cwd=BACKEND_ROOT,
                stdout=stdout_handle,
                stderr=stderr_handle,
            )
        finally:
            stdout_handle.close()
            stderr_handle.close()
        self.procs.append(proc)
        return proc


# --------------------------------------------------------------------------
# Marker helpers
# --------------------------------------------------------------------------
def _read_marker(markers: str, name: str) -> str | None:
    path = os.path.join(markers, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def _log_path(markers: str, worker_id: str, stream: str) -> str:
    return os.path.join(markers, f"{stream}_{worker_id}.log")


def _acquire_attempts(markers: str, worker_id: str) -> list[tuple[float, bool, bool]]:
    """Parse the real ``try_acquire`` results recorded by a worker.

    Each line is ``<wall-clock timestamp> <acquired> <took_over_expired>``.
    """
    raw = _read_marker(markers, "acquire_attempts_" + worker_id) or ""
    attempts: list[tuple[float, bool, bool]] = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        attempts.append((float(parts[0]), parts[1] == "True", parts[2] == "True"))
    return attempts


def _read_log(markers: str, worker_id: str, stream: str = "stderr") -> str:
    path = _log_path(markers, worker_id, stream)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _wait_marker(
    markers: str, name: str, timeout: float = 120.0, proc=None, worker_id: str = ""
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = _read_marker(markers, name)
        if value is not None:
            return value
        if proc is not None and proc.poll() is not None:
            detail = _read_marker(markers, f"error_{worker_id}") or ""
            tail = _read_log(markers, worker_id)[-1500:]
            raise AssertionError(
                f"worker {worker_id} exited rc={proc.returncode} before marker {name}: "
                f"{_redact(detail)}\n--- stderr ---\n{_redact(tail)}"
            )
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for marker {name!r}")


def _wait_exit(proc: subprocess.Popen, timeout: float = SCENARIO_TIMEOUT) -> int:
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            pass
        raise AssertionError("worker did not exit within the bounded timeout")


def _assert_clean_worker(rehearsal: _Rehearsal, markers: str, worker_id: str, rc: int) -> None:
    _check(rc == 0, f"worker {worker_id} exited rc={rc}")
    stderr = _read_log(markers, worker_id)
    stdout = _read_log(markers, worker_id, "stdout")
    combined = f"{stdout}\n{stderr}"
    for needle in ("23505", "duplicate key", "IntegrityError", "uq_exposure_source_order"):
        _check(
            needle not in combined,
            f"worker {worker_id} surfaced {needle!r}; stderr tail:\n{_redact(stderr[-2000:])}",
        )
    _check(
        _read_marker(markers, f"error_{worker_id}") is None,
        f"worker {worker_id} recorded an error marker",
    )
    _check(
        _read_marker(markers, f"done_{worker_id}") is not None,
        f"worker {worker_id} never reported completion",
    )


def _boot_schema(rehearsal: _Rehearsal, markers: str) -> None:
    """Create the current schema with one real init_db() startup."""
    proc = rehearsal.spawn_worker(markers, "boot", mode="none")
    rc = _wait_exit(proc)
    _assert_clean_worker(rehearsal, markers, "boot", rc)
    _check(
        rehearsal.exposure_count() == 0,
        "a fresh database must start with an empty exposure ledger",
    )


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def rehearsal():
    bootstrap_url = _require_rehearsal_env()
    instance = _Rehearsal(bootstrap_url)
    try:
        instance.assert_job_local_server()
        instance.provision()
        yield instance
    finally:
        instance.teardown()


@pytest.fixture
def markers(tmp_path):
    path = tmp_path / "markers"
    path.mkdir()
    return str(path)


# --------------------------------------------------------------------------
# Scenario A - concurrent startup and exposure backfill
# --------------------------------------------------------------------------
class TestScenarioAConcurrentStartup:
    def test_two_concurrent_startups_serialize_migrations_and_backfill(
        self, rehearsal, markers
    ):
        _boot_schema(rehearsal, markers)
        rehearsal.seed()
        _check(
            rehearsal.exposure_count() == 0,
            "seeded database must have no exposure rows before the concurrent startup",
        )

        worker_a = rehearsal.spawn_worker(
            markers, "a", mode="hold_for_peer_begin", peer="b"
        )
        worker_b = rehearsal.spawn_worker(
            markers, "b", mode="hold_for_peer_begin", peer="a"
        )

        begin_a = float(_wait_marker(markers, "begin_a", proc=worker_a, worker_id="a"))
        begin_b = float(_wait_marker(markers, "begin_b", proc=worker_b, worker_id="b"))

        rc_a = _wait_exit(worker_a)
        rc_b = _wait_exit(worker_b)
        _assert_clean_worker(rehearsal, markers, "a", rc_a)
        _assert_clean_worker(rehearsal, markers, "b", rc_b)

        done_a = float(_wait_marker(markers, "done_a"))
        done_b = float(_wait_marker(markers, "done_b"))
        _check(
            begin_a < done_b and begin_b < done_a,
            "the two startup attempts did not overlap in time "
            f"(a=[{begin_a:.3f},{done_a:.3f}] b=[{begin_b:.3f},{done_b:.3f}])",
        )

        entered_a = float(_wait_marker(markers, "backfill_entered_a"))
        entered_b = float(_wait_marker(markers, "backfill_entered_b"))
        exited_a = float(_wait_marker(markers, "backfill_exited_a"))
        exited_b = float(_wait_marker(markers, "backfill_exited_b"))
        # The backfill that started later must have started only after the
        # earlier one finished: the lease (not scheduler luck) ordered them.
        if entered_a < entered_b:
            _check(
                entered_b >= exited_a,
                "second worker entered its backfill before the first finished "
                f"(entered_b={entered_b:.3f} exited_a={exited_a:.3f})",
            )
        else:
            _check(
                entered_a >= exited_b,
                "second worker entered its backfill before the first finished "
                f"(entered_a={entered_a:.3f} exited_b={exited_b:.3f})",
            )

        rehearsal.assert_exposure_ledger(EXPECTED_EXPOSURES)
        rehearsal.assert_unique_constraint()
        rehearsal.assert_lease_released()

        for worker_id in ("a", "b"):
            stderr = _read_log(markers, worker_id)
            _check(
                "migration lock acquisition started" in stderr,
                f"worker {worker_id} never logged lease acquisition",
            )
            _check(
                "migration lock released" in stderr,
                f"worker {worker_id} never logged lease release",
            )


# --------------------------------------------------------------------------
# Scenario B - prolonged backfill and real lease renewal
# --------------------------------------------------------------------------
class TestScenarioBLeaseRenewalDuringBackfill:
    def test_lease_is_renewed_while_backfill_outlasts_ttl(self, rehearsal, markers):
        _boot_schema(rehearsal, markers)
        rehearsal.seed()

        holder = rehearsal.spawn_worker(
            markers, "holder", mode="hold_until_released", ttl=REHEARSAL_TTL
        )
        entered = float(
            _wait_marker(
                markers, "backfill_entered_holder", proc=holder, worker_id="holder"
            )
        )
        holder_owner, first_expiry = rehearsal.lease_state()
        _check(holder_owner is not None, "the holder must own the lease during backfill")
        _check(
            f":{holder.pid}:" in holder_owner,
            f"lease owner {holder_owner!r} does not correspond to the holder process "
            f"(pid {holder.pid})",
        )
        _check(first_expiry is not None, "the held lease must carry an expiry")

        waiter = rehearsal.spawn_worker(markers, "waiter", mode="none", ttl=REHEARSAL_TTL)

        # Hold the backfill beyond the original TTL and record every distinct
        # expiry the database actually stores for the same owner.
        expiries = [first_expiry]
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            current_owner, current_expiry = rehearsal.lease_state()
            _check(
                current_owner == holder_owner,
                f"lease ownership changed during a live, renewing backfill: {current_owner!r}",
            )
            if current_expiry != expiries[-1]:
                expiries.append(current_expiry)
            # Marker timestamps are wall-clock (time.time()); monotonic() is
            # a different clock domain and would make this difference negative.
            elapsed = time.time() - entered
            if elapsed > REHEARSAL_TTL * 1.5 and len(expiries) >= 3:
                break
            time.sleep(0.2)

        _check(
            len(expiries) >= 3,
            "the lease expiry did not advance across multiple renewal intervals "
            f"(observed {len(expiries)} distinct values)",
        )
        _check(
            all(later > earlier for earlier, later in zip(expiries, expiries[1:])),
            f"lease expiry did not move forward monotonically: {expiries}",
        )
        _check(
            expiries[-1] > first_expiry,
            "the lease expiry never advanced beyond the value issued at acquisition",
        )
        _check(
            time.time() - entered > REHEARSAL_TTL,
            "the backfill was not held longer than the lease TTL",
        )

        # The competing startup must still be waiting and must not have
        # entered its own backfill while the first lease is valid.
        _check(
            waiter.poll() is None,
            "the competing startup exited (or failed) while the first lease was held",
        )
        _check(
            _read_marker(markers, "backfill_entered_waiter") is None,
            "the competing startup entered its backfill while the lease was held",
        )
        waiter_owner_now, _ = rehearsal.lease_state()
        _check(
            waiter_owner_now == holder_owner,
            "the competing startup took over a live, renewed lease",
        )

        # The holder's own acquisition really succeeded, and the waiter really
        # attempted acquisition and was really refused while the holder owned
        # the lease. The wrapper records the real return value and returns it
        # unchanged -- no acquisition result is ever faked.
        _check(
            any(acquired for _, acquired, _ in _acquire_attempts(markers, "holder")),
            "the holder never recorded a successful real try_acquire result",
        )
        _check(
            any(not acquired for _, acquired, _ in _acquire_attempts(markers, "waiter")),
            "the competing startup never recorded a refused real try_acquire "
            "while the holder owned the lease",
        )

        # Release the holder: it must finish and release the lease.
        release_time = time.time()
        with open(os.path.join(markers, "release_holder"), "w", encoding="utf-8") as handle:
            handle.write(str(release_time))

        rc_holder = _wait_exit(holder)
        _assert_clean_worker(rehearsal, markers, "holder", rc_holder)
        _check(
            "migration lock released" in _read_log(markers, "holder"),
            "the holder never logged releasing the lease",
        )

        rc_waiter = _wait_exit(waiter)
        _assert_clean_worker(rehearsal, markers, "waiter", rc_waiter)
        _check(
            _read_marker(markers, "backfill_entered_waiter") is not None,
            "the competing startup never reached its own backfill after the release",
        )
        entered_waiter = float(_wait_marker(markers, "backfill_entered_waiter"))
        exited_holder = float(_wait_marker(markers, "backfill_exited_holder"))
        # The waiter's callback must start only after the holder's callback has
        # finished: the explicit ordering the lease is supposed to guarantee.
        _check(
            entered_waiter >= exited_holder,
            "the competing startup entered its backfill before the holder finished "
            f"(entered_waiter={entered_waiter:.3f} exited_holder={exited_holder:.3f})",
        )
        # And it must have stayed outside its callback beyond the original TTL,
        # i.e. the holder's renewals - not a short pause - kept it out.
        _check(
            entered_waiter - entered > REHEARSAL_TTL,
            "the competing startup entered its backfill before the original lease "
            f"TTL had elapsed (delay={entered_waiter - entered:.3f}s)",
        )
        waiter_exited = float(_wait_marker(markers, "backfill_exited_waiter"))
        _check(
            waiter_exited >= entered_waiter,
            "the competing startup exited its backfill before entering it",
        )
        _check(
            any(
                attempt_time >= release_time and acquired
                for attempt_time, acquired, _ in _acquire_attempts(markers, "waiter")
            ),
            "the competing startup never recorded a successful real try_acquire "
            "after the holder released the lease",
        )

        rehearsal.assert_exposure_ledger(EXPECTED_EXPOSURES)
        rehearsal.assert_unique_constraint()
        rehearsal.assert_lease_released()

        holder_stderr = _read_log(markers, "holder")
        _check(
            "migration lock acquisition started" in holder_stderr,
            "the holder never logged lease acquisition",
        )
        _check(
            "Alembic migrations applied successfully" in holder_stderr,
            "the holder never completed its migration step",
        )


# --------------------------------------------------------------------------
# Scenario C - cleanup and final consistency
# --------------------------------------------------------------------------
class TestScenarioCCleanupAndConsistency:
    def test_workers_leave_a_clean_leased_database(self, rehearsal, markers):
        _boot_schema(rehearsal, markers)
        rehearsal.seed()

        worker_a = rehearsal.spawn_worker(
            markers, "c1", mode="hold_for_peer_begin", peer="c2"
        )
        worker_b = rehearsal.spawn_worker(
            markers, "c2", mode="hold_for_peer_begin", peer="c1"
        )
        rc_a = _wait_exit(worker_a)
        rc_b = _wait_exit(worker_b)
        _assert_clean_worker(rehearsal, markers, "c1", rc_a)
        _assert_clean_worker(rehearsal, markers, "c2", rc_b)

        rehearsal.assert_lease_released()
        rehearsal.assert_exposure_ledger(EXPECTED_EXPOSURES)
        rehearsal.assert_unique_constraint()
        rehearsal.assert_privilege_boundaries()

        leftover = [proc.pid for proc in rehearsal.procs if proc.poll() is None]
        _check(not leftover, f"worker processes are still running: {leftover}")
