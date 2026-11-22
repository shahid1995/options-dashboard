from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.orm import sessionmaker

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from migrate_sqlite_to_postgres import (  # noqa: E402
    SKIP_TABLES,
    get_table_order,
    migrate_database,
    verify_databases,
)
from app.identity import BrokerConnection, User  # noqa: E402
from app.models import GexSnapshot  # noqa: E402
from tests import issue145_pg_support as support  # noqa: E402


def _prepare_schema(engine) -> None:
    """Build the schema of a SQLite source database.

    PostgreSQL targets are prepared exclusively through
    ``python -m app.migrate upgrade head`` by the ``postgres_engine`` fixture:
    ``alembic/env.py`` refuses an unleased PostgreSQL run and that guard must
    stay effective.
    """
    if engine.dialect.name != "sqlite":
        raise RuntimeError(
            "PostgreSQL schemas are built by the guarded migration CLI, not by "
            "a direct Alembic run (ADR-017)"
        )
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    cfg.attributes["connectable"] = engine
    command.upgrade(cfg, "head")


@pytest.fixture(scope="module")
def postgres_engine():
    """Disposable PostgreSQL target, migrated by the guarded CLI (Topology A)."""
    if support.admin_url() is None:
        pytest.skip(f"{support.ADMIN_ENV} is not configured (disposable PostgreSQL 16.16)")
    prov = support.provision_schema_ready()
    engine = support.migrator_engine(prov)
    try:
        yield engine
    finally:
        engine.dispose()
        support.drop(prov)


def _clear_postgres_target(engine) -> None:
    metadata = MetaData()
    metadata.reflect(bind=engine)
    tables = [name for name in get_table_order(metadata) if name not in SKIP_TABLES]
    with engine.begin() as conn:
        for table_name in reversed(tables):
            conn.execute(text(f'DELETE FROM "{table_name}"'))


def _populate_current_schema_synthetic_dataset(engine) -> None:
    session = sessionmaker(bind=engine)()
    try:
        user = User(
            id="ci-rehearsal-user",
            email="ci-rehearsal@example.com",
            identity_source="email",
        )
        connection = BrokerConnection(
            id="ci-rehearsal-connection",
            user_id=user.id,
            broker="UPSTOX",
            broker_account_id="ci-rehearsal-account",
            is_default=True,
            status="connected",
            capability_mode="data",
            data_status="active",
            trading_status="inactive",
        )
        session.add_all([user, connection])
        session.add(
            GexSnapshot(
                owner_id=user.id,
                connection_id=connection.id,
                data_source="analytics_token",
                symbol="NIFTY",
                expiry="2026-12-31",
                spot=25000.0,
                methodology="GEX_STANDARD_V1",
                sign_convention="NAIVE_DEALER_CONVENTION",
                availability_status="available",
                valid_strike_count=1,
                total_strike_count=1,
                captured_at=datetime.now(timezone.utc),
                strike_data="[]",
                expiry_data="[]",
                methodology_metadata="{}",
            )
        )
        session.commit()
    finally:
        session.close()


def test_synthetic_rehearsal_uses_current_alembic_schema_and_verifies_full_report(
    tmp_path, postgres_engine
):
    sqlite_engine = create_engine(
        f"sqlite:///{tmp_path / 'synthetic-ci.db'}",
        connect_args={"check_same_thread": False},
    )
    try:
        _prepare_schema(sqlite_engine)
        _populate_current_schema_synthetic_dataset(sqlite_engine)

        _clear_postgres_target(postgres_engine)

        migrate_database(sqlite_engine, postgres_engine, batch_size=1000)
        report = verify_databases(sqlite_engine, postgres_engine)

        assert report["ok"] is True, {
            "failed_tables": {
                name: details["errors"]
                for name, details in report["tables"].items()
                if not details["passed"]
            },
            "failed_sequences": {
                name: details
                for name, details in report["sequences"].items()
                if not details.get("ok")
            },
            "security": report["security"],
            "isolation": report["isolation"],
        }
    finally:
        sqlite_engine.dispose()
        postgres_engine.dispose()
