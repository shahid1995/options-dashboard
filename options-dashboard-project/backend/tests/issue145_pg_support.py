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
import secrets
import textwrap

import psycopg

ADMIN_ENV = "ISSUE145_PG_ADMIN_URL"
REQUIRED_SERVER_VERSION_NUM = 160016  # PostgreSQL 16.16 exactly


class Provisioned:
    def __init__(self, admin_url: str, prefix: str):
        self.admin_url = admin_url
        self.prefix = prefix
        self.database = prefix
        self.owner_role = f"{prefix}_owner"
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


def provision(url: str | None = None) -> Provisioned:
    """Create a fresh database and its three ADR-018 identities."""
    url = url or admin_url()
    if not url:
        raise RuntimeError(f"{ADMIN_ENV} is not configured")
    require_pg16_16(url)
    prefix = "sn145_" + secrets.token_hex(5)
    prov = Provisioned(url, prefix)

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {prov.owner_role} NOLOGIN")
        conn.execute(
            f"CREATE ROLE {prov.migrator_role} LOGIN PASSWORD '{prov.migrator_password}'"
        )
        conn.execute(f"CREATE ROLE {prov.app_role} LOGIN PASSWORD '{prov.app_password}'")
        conn.execute(f"CREATE DATABASE {prov.database} OWNER {prov.migrator_role}")

    with psycopg.connect(prov.admin_database_url, autocommit=True) as conn:
        conn.execute(
            f"GRANT CREATE, USAGE ON SCHEMA public TO {prov.owner_role}"
        )
        conn.execute(
            f"GRANT {prov.owner_role} TO {prov.migrator_role} "
            "WITH INHERIT FALSE, SET TRUE, ADMIN FALSE"
        )
        conn.execute(f"GRANT USAGE ON SCHEMA public TO {prov.app_role}")
        # Application tables created by the owner are usable by the runtime
        # identity with ordinary DML only (ADR-018).
        conn.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {prov.owner_role} IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {prov.app_role}"
        )
        conn.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {prov.owner_role} IN SCHEMA public "
            f"GRANT USAGE, SELECT ON SEQUENCES TO {prov.app_role}"
        )
    return prov


def grant_backfill_privileges(prov: Provisioned) -> None:
    """ADR §8.3 Topology B: the migrator's entire application privilege set."""
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


def apply_topology_a_revokes(prov: Provisioned) -> None:
    """ADR §8.3 Topology A: the owning migrator's effective minimum privileges."""
    with psycopg.connect(prov.admin_database_url, autocommit=True) as conn:
        conn.execute(
            f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.positions "
            f"FROM {prov.migrator_role}"
        )
        conn.execute(
            f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.paper_orders "
            f"FROM {prov.migrator_role}"
        )
        conn.execute(
            f"REVOKE UPDATE, DELETE, TRUNCATE ON public.strategy_leg_exposures "
            f"FROM {prov.migrator_role}"
        )
        conn.execute(
            f"REVOKE ALL ON SEQUENCE public.strategy_leg_exposures_id_seq "
            f"FROM {prov.migrator_role}"
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
        for role in (prov.app_role, prov.migrator_role, prov.owner_role):
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


