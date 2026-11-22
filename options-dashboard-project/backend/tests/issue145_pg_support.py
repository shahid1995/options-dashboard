"""Disposable PostgreSQL 16.16 provisioning for the ADR-017/018 tests.

The tests only run when ``ISSUE145_PG_ADMIN_URL`` points at a disposable
PostgreSQL 16.16 server whose admin role can CREATE ROLE/DATABASE (the GitHub
Actions service container, or a local scratch cluster). No production,
staging, Neon or provider credential is ever read or used.

Every database gets its own uniquely named owner / migrator / application
roles so a run can never collide with another run on the same server.
"""

from __future__ import annotations

import os
import pathlib
import secrets
import subprocess
import sys
import textwrap

import psycopg

ADMIN_ENV = "ISSUE145_PG_ADMIN_URL"
REQUIRED_SERVER_VERSION_NUM = 160016  # PostgreSQL 16.16 exactly
BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]

#: Topology B (ADR §8.3): a separate NOLOGIN owner role owns application
#: objects; the migrator reaches it only through an explicit SET ROLE window.
TOPOLOGY_B = "B"
#: Topology A (ADR §8.3): no separate owner role exists; the migration
#: identity itself creates and owns application objects.
TOPOLOGY_A = "A"
TOPOLOGIES = (TOPOLOGY_A, TOPOLOGY_B)


class Provisioned:
    def __init__(self, admin_url: str, prefix: str, topology: str = TOPOLOGY_B):
        if topology not in TOPOLOGIES:
            raise ValueError(f"unknown topology {topology!r}; expected one of {TOPOLOGIES}")
        self.admin_url = admin_url
        self.prefix = prefix
        self.topology = topology
        self.database = prefix
        # Topology A has no separate owner role at all: the attribute is empty
        # so every consumer (settings, SET ROLE window) is genuinely bypassed.
        self.owner_role = f"{prefix}_owner" if topology == TOPOLOGY_B else ""
        self.migrator_role = f"{prefix}_migrator"
        self.app_role = f"{prefix}_app"
        self.migrator_password = secrets.token_hex(16)
        self.app_password = secrets.token_hex(16)

    @property
    def admin_database_url(self) -> str:
        return _replace_database(self.admin_url, self.database)

    @property
    def migrator_url(self) -> str:
        return _role_url(self.migrator_role, self.migrator_password, self.admin_url, self.database)

    @property
    def app_url(self) -> str:
        return _role_url(self.app_role, self.app_password, self.admin_url, self.database)

    def env(self, **overrides: str) -> dict[str, str]:
        """Environment for a subprocess that must use this database."""
        env = dict(os.environ)
        # No unleased-Alembic escape hatch exists: these subprocesses must
        # prove the guarded path is the only PostgreSQL migration path.
        env.pop("STRIKENOVA_ALEMBIC_SCHEMA_REHEARSAL", None)
        env.update(
            {
                "DATABASE_URL": self.app_url,
                "STRIKENOVA_MIGRATION_DATABASE_URL": self.migrator_url,
                "STRIKENOVA_OWNER_ROLE": self.owner_role,
                "STRIKENOVA_MIGRATOR_ROLE": self.migrator_role,
                "STRIKENOVA_RUNTIME_ROLE": self.app_role,
                "STRIKENOVA_ENV": "",
                "PYTHONPATH": os.getcwd(),
            }
        )
        env.update(overrides)
        return env


def _replace_database(url: str, database: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(url).set(database=database).render_as_string(hide_password=False)


def _role_url(role: str, password: str, admin_url: str, database: str) -> str:
    from sqlalchemy.engine import make_url

    parsed = make_url(admin_url).set(
        username=role, password=password, database=database
    )
    return parsed.render_as_string(hide_password=False)


def admin_url() -> str | None:
    value = (os.environ.get(ADMIN_ENV) or "").strip()
    return value or None


def require_pg16_16(url: str) -> None:
    with psycopg.connect(url) as conn:
        row = conn.execute("SHOW server_version_num").fetchone()
        banner = conn.execute("SHOW server_version").fetchone()[0]
    number = int(row[0])
    if number != REQUIRED_SERVER_VERSION_NUM:
        raise RuntimeError(
            f"the Issue #145 acceptance run requires PostgreSQL 16.16 "
            f"(server_version_num {REQUIRED_SERVER_VERSION_NUM}); got {banner} ({number})"
        )


def provision(url: str | None = None, *, topology: str = TOPOLOGY_B) -> Provisioned:
    """Create a fresh database and the identities for one ADR-018 topology.

    ``topology=B`` (the founder-directed arrangement) creates a separate
    ``NOLOGIN`` owner role and grants the migrator exactly
    ``INHERIT FALSE, SET TRUE`` membership of it, so the owner window is the
    only way the migrator can own a created object.

    ``topology=A`` creates no owner role at all: the migrator owns the
    database and therefore every object it creates. The two fixtures are
    independently provisioned rather than one being derived from the other
    by blanking a setting.

    In both topologies the default privileges that let the runtime identity
    use future application tables are scoped to ``IN SCHEMA public`` and
    attached to the role that actually creates those tables (ADR-018), so the
    ``migration_internal`` schema never inherits a runtime grant.
    """
    url = url or admin_url()
    if not url:
        raise RuntimeError(f"{ADMIN_ENV} is not configured")
    require_pg16_16(url)
    prefix = "sn145_" + secrets.token_hex(5)
    prov = Provisioned(url, prefix, topology)
    creator = prov.owner_role or prov.migrator_role

    # A half-provisioned database/role set must never survive a failed setup:
    # without this, a raise between the two blocks would leak resources that no
    # fixture could ever drop (it never received the Provisioned object).
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            if prov.owner_role:
                conn.execute(f"CREATE ROLE {prov.owner_role} NOLOGIN")
            conn.execute(
                f"CREATE ROLE {prov.migrator_role} LOGIN PASSWORD '{prov.migrator_password}'"
            )
            conn.execute(f"CREATE ROLE {prov.app_role} LOGIN PASSWORD '{prov.app_password}'")
            conn.execute(f"CREATE DATABASE {prov.database} OWNER {prov.migrator_role}")

        with psycopg.connect(prov.admin_database_url, autocommit=True) as conn:
            if prov.owner_role:
                conn.execute(
                    f"GRANT CREATE, USAGE ON SCHEMA public TO {prov.owner_role}"
                )
                conn.execute(
                    f"GRANT {prov.owner_role} TO {prov.migrator_role} "
                    "WITH INHERIT FALSE, SET TRUE, ADMIN FALSE"
                )
            conn.execute(f"GRANT USAGE ON SCHEMA public TO {prov.app_role}")
            # The runtime identity is a DML identity only: no schema DDL.
            conn.execute(f"REVOKE CREATE ON SCHEMA public FROM {prov.app_role}")
            # Application tables created by the creator role are usable by the
            # runtime identity with ordinary DML only (ADR-018). Schema-scoped
            # on purpose: a global default would also cover migration_internal.
            conn.execute(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {creator} IN SCHEMA public "
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {prov.app_role}"
            )
            conn.execute(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {creator} IN SCHEMA public "
                f"GRANT USAGE, SELECT ON SEQUENCES TO {prov.app_role}"
            )
    except BaseException:
        drop(prov)
        raise
    return prov


def migrator_engine(prov: Provisioned):
    """SQLAlchemy engine for the migration identity of a provisioned database."""
    from sqlalchemy import create_engine

    from app.db import normalize_database_url

    return create_engine(normalize_database_url(prov.migrator_url), pool_pre_ping=True)


def run_cli(prov: Provisioned, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run ``python -m app.migrate ...`` against this database.

    This is the supported guarded operator path. Nothing here bypasses the
    lease or connects as the provisioned superuser.
    """
    env = prov.env()
    env["PYTHONPATH"] = str(BACKEND_DIR)
    return subprocess.run(
        [sys.executable, "-m", "app.migrate", *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def install_lease(prov: Provisioned) -> subprocess.CompletedProcess:
    """Install the ADR-017 lease objects as the migration identity."""
    result = run_cli(prov, "bootstrap-lease")
    if result.returncode != 0:
        raise RuntimeError(
            "bootstrap-lease failed: " + (result.stderr or result.stdout)[-3000:]
        )
    return result


def run_guarded_upgrade(prov: Provisioned) -> subprocess.CompletedProcess:
    """Build the application schema through the guarded migration CLI."""
    result = run_cli(prov, "upgrade", "head")
    if result.returncode != 0:
        raise RuntimeError(
            "guarded upgrade head failed: " + (result.stderr or result.stdout)[-3000:]
        )
    return result


def provision_schema_ready(
    url: str | None = None, *, topology: str = TOPOLOGY_A
) -> Provisioned:
    """Provision a disposable database and build its schema the supported way.

    Used by the PostgreSQL compatibility and SQLite -> PostgreSQL import
    rehearsals, which need a real migrated schema but are not testing the
    lease itself. The schema is always created by ``python -m app.migrate
    upgrade head`` under the migration identity: a bare ``alembic upgrade
    head`` is refused by ``alembic/env.py`` and must never be re-enabled for
    tests.

    Topology A is the default because those rehearsals then run their data
    work as the migration identity, which owns the objects it created.
    """
    prov = provision(url, topology=topology)
    try:
        install_lease(prov)
        run_guarded_upgrade(prov)
        grant_importer_write_privileges(prov)
    except BaseException:
        drop(prov)
        raise
    return prov


def grant_importer_write_privileges(prov: Provisioned) -> None:
    """Re-grant the legacy importer's write privileges on a disposable database.

    ``python -m app.migrate upgrade head`` deliberately leaves Topology A in
    the ADR §8.3 shape: the migrator *owns* ``positions`` and ``paper_orders``
    but has had INSERT/UPDATE/DELETE/TRUNCATE revoked, because a production
    startup only reads them. The rehearsals that use this helper are
    **importer** tests — they copy legacy SQLite rows into those tables as the
    migration identity — so they explicitly re-grant exactly the privileges
    the importer needs.

    This is a documented, test-only concession on a throwaway database: the
    application never re-grants, the startup backfill runs restricted, and the
    §8.3 matrix itself is asserted by
    ``test_issue145_application_integration.py``.
    """
    if prov.owner_role:
        raise RuntimeError(
            "the importer grants assume Topology A (the migrator owns the "
            "application tables); Topology B uses grant_backfill_privileges"
        )
    with psycopg.connect(prov.migrator_url, autocommit=True) as conn:
        conn.execute(
            f'GRANT INSERT, UPDATE, DELETE, TRUNCATE ON TABLE '
            f'public."positions", public."paper_orders", '
            f'public."strategy_leg_exposures" TO "{prov.migrator_role}"'
        )
        conn.execute(
            f'GRANT ALL ON SEQUENCE public."strategy_leg_exposures_id_seq" '
            f'TO "{prov.migrator_role}"'
        )


def grant_backfill_privileges(prov: Provisioned) -> None:
    """ADR §8.3 Topology B: the migrator's entire application privilege set."""
    if not prov.owner_role:
        raise RuntimeError(
            "the §8.3 backfill grants are a Topology B operation: in Topology A "
            "the migration identity owns the application tables and the guarded "
            "upgrade already applied the §8.3 Topology A restrictions"
        )
    with psycopg.connect(prov.admin_database_url, autocommit=True) as conn:
        conn.execute(
            f"GRANT SELECT ON public.positions TO {prov.migrator_role}"
        )
        conn.execute(
            f"GRANT SELECT ON public.paper_orders TO {prov.migrator_role}"
        )
        conn.execute(
            f"GRANT SELECT, INSERT ON public.strategy_leg_exposures TO {prov.migrator_role}"
        )
        conn.execute(
            f"GRANT USAGE ON SEQUENCE public.strategy_leg_exposures_id_seq "
            f"TO {prov.migrator_role}"
        )


def drop(prov: Provisioned) -> None:
    with psycopg.connect(prov.admin_url, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (prov.database,),
        )
        conn.execute(f"DROP DATABASE IF EXISTS {prov.database}")
        roles = [prov.app_role, prov.migrator_role]
        if prov.owner_role:
            roles.append(prov.owner_role)
        for role in roles:
            conn.execute(f"DROP ROLE IF EXISTS {role}")


def seed_legacy_users(
    admin_database_url: str,
    users: int,
    positions_per_user: int = 1,
    start_user: int = 1,
) -> None:
    """Insert provably unambiguous legacy positions/orders for the backfill."""
    with psycopg.connect(admin_database_url, autocommit=True) as conn:
        for u in range(start_user, start_user + users):
            user_id = f"issue145-user-{u}"
            for p in range(1, positions_per_user + 1):
                execution = f"issue145-exec-{u}-{p}"
                strike = 24000 + p * 50
                position_id = conn.execute(
                    "INSERT INTO positions (user_id, symbol, expiry, strike, option_type, "
                    "net_quantity, average_entry_price, lot_size, realized_pnl, status, "
                    "strategy_execution_id, opened_at, updated_at) "
                    "VALUES (%s,'NIFTY','2026-08-27',%s,'call',1,100.0,65,0.0,'open',%s,now(),now()) "
                    "RETURNING id",
                    (user_id, strike, execution),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO paper_orders (user_id, client_order_id, execution_id, "
                    "position_id, kind, symbol, expiry, strike, option_type, action, "
                    "quantity, lot_size, status, filled_quantity, fill_price, price_source, "
                    "created_at, updated_at) "
                    "VALUES (%s,%s,%s,%s,'entry','NIFTY','2026-08-27',%s,'call','buy',"
                    "1,65,'FILLED',1,100.0,'market',now(),now())",
                    (user_id, f"issue145-legacy-{u}-{p}", execution, position_id, strike),
                )


