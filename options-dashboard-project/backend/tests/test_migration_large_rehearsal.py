from __future__ import annotations

import sys
import time
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
    migrate_database,
    verify_databases,
)
from app.identity import BrokerConnection, User  # noqa: E402
from app.models import GexSnapshot  # noqa: E402
from tests import issue145_pg_support as support  # noqa: E402

DATASET_USERS = 20
GEX_ROWS_PER_USER = 500
EXPECTED_GEX_ROWS = DATASET_USERS * GEX_ROWS_PER_USER
MAX_REHEARSAL_SECONDS = 60.0


def _prepare_schema(engine) -> None:
    """Build the SQLite source schema.

    The PostgreSQL target is migrated exclusively by
    ``python -m app.migrate upgrade head`` (see the ``postgres_engine``
    fixture); ``alembic/env.py`` refuses an unleased PostgreSQL run.
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
    tables = [
        table_name
        for table_name in metadata.tables
        if table_name not in SKIP_TABLES
    ]
    with engine.begin() as conn:
        if tables:
            quoted = ", ".join(f'"{table_name}"' for table_name in tables)
            conn.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))


def _populate_large_dataset(engine) -> None:
    session = sessionmaker(bind=engine)()
    try:
        users = []
        connections = []
        for user_index in range(DATASET_USERS):
            user_id = f"large-rehearsal-user-{user_index:03d}"
            connection_id = f"large-rehearsal-connection-{user_index:03d}"
            users.append(
                User(
                    id=user_id,
                    email=f"large-rehearsal-{user_index}@example.com",
                    identity_source="email",
                )
            )
            connections.append(
                BrokerConnection(
                    id=connection_id,
                    user_id=user_id,
                    broker="UPSTOX",
                    broker_account_id=f"large-rehearsal-account-{user_index:03d}",
                    is_default=True,
                    status="connected",
                    capability_mode="data",
                    data_status="active",
                    trading_status="inactive",
                )
            )
        session.add_all(users)
        session.add_all(connections)
        session.flush()

        captured_at = datetime(2026, 8, 31, 9, 15, tzinfo=timezone.utc)
        snapshots = []
        for user_index in range(DATASET_USERS):
            user_id = f"large-rehearsal-user-{user_index:03d}"
            connection_id = f"large-rehearsal-connection-{user_index:03d}"
            for row_index in range(GEX_ROWS_PER_USER):
                snapshots.append(
                    GexSnapshot(
                        owner_id=user_id,
                        connection_id=connection_id,
                        data_source="analytics_token",
                        symbol="NIFTY",
                        expiry="2026-12-31",
                        spot=25000.0 + (row_index % 100),
                        methodology="GEX_STANDARD_V1",
                        sign_convention="NAIVE_DEALER_CONVENTION",
                        availability_status="available",
                        valid_strike_count=1,
                        total_strike_count=1,
                        captured_at=captured_at,
                        strike_data=f"[{{\"strike\":{24000 + (row_index % 100)},\"gex\":{row_index}}}]",
                        expiry_data="[]",
                        methodology_metadata="{}",
                    )
                )
            if len(snapshots) >= 1000:
                session.add_all(snapshots)
                session.flush()
                snapshots.clear()
        if snapshots:
            session.add_all(snapshots)
        session.commit()
    finally:
        session.close()


def test_large_realistic_rehearsal_migrates_and_verifies_without_data_loss(
    tmp_path, postgres_engine
):
    sqlite_path = tmp_path / "large-rehearsal.db"
    sqlite_engine = create_engine(
        f"sqlite:///{sqlite_path}",
        connect_args={"check_same_thread": False},
    )
    try:
        _prepare_schema(sqlite_engine)
        _populate_large_dataset(sqlite_engine)
        _clear_postgres_target(postgres_engine)

        with sqlite_engine.connect() as conn:
            source_count = int(conn.execute(text("SELECT COUNT(*) FROM gex_snapshots")).scalar())
        source_size_bytes = sqlite_path.stat().st_size
        assert source_count == EXPECTED_GEX_ROWS
        assert source_size_bytes > 0

        started = time.monotonic()
        migrate_database(sqlite_engine, postgres_engine, batch_size=500)
        elapsed_seconds = time.monotonic() - started

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
        assert report["tables"]["gex_snapshots"]["source_count"] == EXPECTED_GEX_ROWS
        assert report["tables"]["gex_snapshots"]["row_count"] == EXPECTED_GEX_ROWS
        assert report["tables"]["gex_snapshots"]["fingerprint_match"] is True
        assert elapsed_seconds < MAX_REHEARSAL_SECONDS

        with postgres_engine.connect() as conn:
            target_size_bytes = int(
                conn.execute(
                    text("SELECT pg_total_relation_size('public.gex_snapshots')")
                ).scalar()
            )
        assert target_size_bytes > 0
        print(
            f"large migration rehearsal: rows={EXPECTED_GEX_ROWS} "
            f"source_sqlite_bytes={source_size_bytes} "
            f"target_gex_relation_bytes={target_size_bytes} "
            f"elapsed_seconds={elapsed_seconds:.3f}"
        )
    finally:
        sqlite_engine.dispose()
        postgres_engine.dispose()
