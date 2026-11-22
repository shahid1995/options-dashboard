"""Alembic environment configuration for StrikeNova.

This env.py supports:
1. CLI-driven migrations (alembic upgrade head)
2. Programmatic migrations during application startup
3. In-memory SQLite test databases via Config.attributes['connectable']
4. SQLite and PostgreSQL dialects without applying SQLite-specific batch
   behavior to PostgreSQL migrations.
"""

from __future__ import annotations

import logging
import os
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool

# Add backend/ to sys.path so app.* imports resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import all models so Base.metadata knows about every table.
from app.db import Base, resolve_migration_database_url  # noqa: E402
from app import models  # noqa: E402, F401
from app.identity import User, UserSession, BrokerConnection, BrokerToken  # noqa: E402, F401

config = context.config

# Keep Alembic logging isolated so pytest/caplog behavior is unaffected.
logging.getLogger("alembic").setLevel(logging.INFO)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

target_metadata = Base.metadata


def _resolve_database_url() -> str:
    """Resolve the database URL used by Alembic.

    Delegates to :func:`app.db.resolve_migration_database_url` so a
    standalone CLI invocation (``alembic upgrade head``) resolves its
    identity exactly like application-startup migrations (ADR-016/PR #107
    review). Precedence:

    1. ``sqlalchemy.url`` supplied by the caller/CLI configuration
       (highest-priority, intentional operator override)
    2. ``STRIKENOVA_MIGRATION_DATABASE_URL`` (dedicated migration identity)
    3. ``DATABASE_URL`` (runtime identity; historical single-identity
       behavior)
    4. backend/paper_journal.db SQLite fallback

    Without this, a manual CLI run could silently migrate under the
    DML-only runtime identity and create future tables that lack the
    runtime default privileges (ADR-018).
    """
    config_url = config.get_main_option("sqlalchemy.url")
    return resolve_migration_database_url(config_url)


def _render_as_batch(url: str) -> bool:
    """Use Alembic batch mode only for SQLite schema operations."""
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Run migrations in offline mode using the resolved dialect."""
    url = _resolve_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_render_as_batch(url),
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_on_connection(connection, lease_guard) -> None:
    """Configure the migration context on an existing Connection and run.

    When the caller supplies a Connection that is already inside a transaction
    (the ADR-017 DDL transaction), Alembic must not open or commit its own:
    exactly one COMMIT exists on that connection and it belongs to the caller
    (ADR §13.1–§13.2).
    """
    is_postgres = connection.dialect.name != "sqlite"
    if is_postgres and lease_guard is None:
        # No test-only opt-in: every PostgreSQL schema is built through
        # ``python -m app.migrate upgrade head`` (ADR-017 §13), and no test may
        # re-enable an unleased DDL path.
        raise RuntimeError(
            "refusing to run Alembic against PostgreSQL without the ADR-017 "
            "migration lease: use 'python -m app.migrate upgrade head'"
        )

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
    )

    # Diagnostic only: lets the acceptance tests prove that the lease and the
    # DDL share one physical connection (ADR §13.1). It is never identity or
    # authorization evidence.
    if is_postgres:
        config.attributes["migration_connection_pid"] = connection.exec_driver_sql(
            "SELECT pg_backend_pid()"
        ).scalar()

    if connection.in_transaction():
        context.run_migrations()
    else:
        with context.begin_transaction():
            context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations online using the supplied connection or a new engine."""
    lease_guard = config.attributes.get("lease_guard")
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_on_connection(connection, lease_guard)
        return

    connectable = config.attributes.get("connectable")
    if connectable is None:
        url = _resolve_database_url()
        configuration = config.get_section(config.config_ini_section, {})
        configuration["sqlalchemy.url"] = url

        if url.startswith("sqlite"):
            connectable = engine_from_config(
                configuration,
                prefix="sqlalchemy.",
                connect_args={"check_same_thread": False},
                poolclass=pool.NullPool,
            )
        else:
            connectable = engine_from_config(
                configuration,
                prefix="sqlalchemy.",
                poolclass=pool.NullPool,
            )

    with connectable.connect() as opened:
        _run_on_connection(opened, lease_guard)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
