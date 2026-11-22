from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tools import migrate_sqlite_to_postgres
from tools.migrate_sqlite_to_postgres import (
    backup_sqlite,
    canonical_value,
    get_table_order,
    normalize_url,
    redact_url,
    sequence_reset_sql,
    sha256_rows,
    storage_safety_ok,
)
from app.identity import BrokerConnection, User
from app.models import GexSnapshot
from tests import issue145_pg_support as support

ROOT = Path(__file__).resolve().parents[1]


def prepare_schema(engine) -> None:
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


def test_normalize_postgres_url_to_psycopg():
    assert normalize_url("postgres://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert normalize_url("postgresql://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    explicit = "postgresql+psycopg://u:p@h:5432/db"
    assert normalize_url(explicit) == explicit
    sqlite = "sqlite:////tmp/test.db"
    assert normalize_url(sqlite) == sqlite


def test_redact_url_does_not_expose_password():
    redacted = redact_url("postgresql+psycopg://user:secret@host:5432/db")
    assert "secret" not in redacted
    assert redacted.startswith("postgresql+psycopg://user:")


def test_canonical_value_is_stable_for_common_database_types():
    assert canonical_value(None) == ["null"]
    assert canonical_value(True) == ["bool", "1"]
    assert canonical_value(12) == ["int", "12"]
    assert canonical_value(1.5) == ["float", "1.5"]
    assert canonical_value(Decimal("1.50")) == ["decimal", "1.50"]
    assert canonical_value(date(2026, 8, 30)) == ["date", "2026-08-30"]
    assert canonical_value(datetime(2026, 8, 30, tzinfo=timezone.utc)) == ["datetime", "2026-08-30T00:00:00+00:00"]


def test_storage_safety_rejects_over_budget():
    assert storage_safety_ok(399 * 1024 * 1024, 500 * 1024 * 1024)
    assert not storage_safety_ok(401 * 1024 * 1024, 500 * 1024 * 1024)


def test_table_order_places_parent_before_child():
    metadata = MetaData()
    Table("parent", metadata, Column("id", Integer, primary_key=True))
    Table("child", metadata, Column("id", Integer, primary_key=True), Column("parent_id", Integer, ForeignKey("parent.id")))
    assert get_table_order(metadata) == ["parent", "child"]


def test_table_order_rejects_dependency_cycles():
    metadata = MetaData()
    parent = Table("parent", metadata, Column("id", Integer, primary_key=True))
    child = Table("child", metadata, Column("id", Integer, primary_key=True))
    parent.append_column(Column("child_id", Integer, ForeignKey("child.id")))
    child.append_column(Column("parent_id", Integer, ForeignKey("parent.id")))
    with pytest.raises(RuntimeError, match="dependency cycle"):
        get_table_order(metadata)


def test_schema_parity_rejects_missing_target_table():
    source = MetaData(); target = MetaData()
    Table("source_only", source, Column("id", Integer, primary_key=True))
    with pytest.raises(ValueError, match="missing from target"):
        migrate_sqlite_to_postgres.assert_schema_compatible(source, target)


def test_sqlite_backup_produces_a_valid_copy(tmp_path):
    source = tmp_path / "source.db"; backup = tmp_path / "backup.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO demo(value) VALUES ('ok')")
        conn.commit()
    backup_sqlite(str(source), str(backup))
    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT value FROM demo").fetchone()[0] == "ok"


def test_sequence_reset_sql_uses_pg_sequence_functions():
    sql = sequence_reset_sql("users", "id")
    assert "pg_get_serial_sequence" in sql
    assert "setval" in sql
    assert "users" in sql
    assert "id" in sql


@pytest.fixture(scope="module")
def postgres_engine():
    """Disposable PostgreSQL target, migrated by the guarded CLI (Topology A)."""
    if support.admin_url() is None:
        pytest.skip(f"{support.ADMIN_ENV} is not configured (disposable PostgreSQL 16.16)")
    prov = support.provision_schema_ready()
    engine = support.migrator_engine(prov)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
        yield engine
    finally:
        engine.dispose()
        support.drop(prov)


def _column_fingerprint_differences(sqlite_engine, postgres_engine, tables):
    differences = {}
    with sqlite_engine.connect() as src, postgres_engine.connect() as dst:
        for table in tables:
            source_columns = [c[1] for c in src.exec_driver_sql(f'PRAGMA table_info("{table}")').fetchall()]
            target_columns = [row[0] for row in dst.execute(text("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=:table ORDER BY ordinal_position"), {"table": table}).fetchall()]
            common = [c for c in source_columns if c in target_columns]
            table_diffs = {}
            for column in common:
                source_rows = src.execute(text(f'SELECT "{column}" FROM "{table}"')).fetchall()
                target_rows = dst.execute(text(f'SELECT "{column}" FROM "{table}"')).fetchall()
                source_hash = sha256_rows(source_rows)
                target_hash = sha256_rows(target_rows)
                if source_hash != target_hash:
                    table_diffs[column] = (source_hash, target_hash)
            if table_diffs:
                differences[table] = table_diffs
    return differences


def test_rehearsal_migrates_sqlite_rows_into_postgres(postgres_engine, tmp_path):
    sqlite_engine = create_engine(f"sqlite:///{tmp_path / 'source.db'}", connect_args={"check_same_thread": False})
    prepare_schema(sqlite_engine)
    source_session = __import__("sqlalchemy.orm", fromlist=["sessionmaker"]).sessionmaker(bind=sqlite_engine)()
    try:
        user = User(id="rehearsal-user", email="rehearsal@example.com", identity_source="email")
        connection = BrokerConnection(
            id="rehearsal-connection", user_id=user.id, broker="UPSTOX", broker_account_id="account-rehearsal",
            is_default=True, status="connected", capability_mode="data", data_status="active", trading_status="inactive",
        )
        source_session.add_all([user, connection])
        source_session.add(GexSnapshot(
            owner_id=user.id, connection_id=connection.id, data_source="analytics_token", symbol="NIFTY",
            expiry="2026-12-31", spot=25000.0, methodology="GEX_STANDARD_V1", sign_convention="NAIVE_DEALER_CONVENTION",
            availability_status="available", valid_strike_count=1, total_strike_count=1, captured_at=datetime.now(timezone.utc),
            strike_data="[]", expiry_data="[]", methodology_metadata="{}",
        ))
        source_session.commit()
    finally:
        source_session.close()

    target_metadata = MetaData(); target_metadata.reflect(bind=postgres_engine)
    with postgres_engine.begin() as conn:
        for table_name in reversed(get_table_order(target_metadata)):
            conn.execute(text(f'DELETE FROM "{table_name}"'))

    migrate_sqlite_to_postgres.migrate_database(sqlite_engine, postgres_engine, batch_size=1000)
    report = migrate_sqlite_to_postgres.verify_databases(sqlite_engine, postgres_engine)

    if not report["ok"]:
        bad_tables = {name: details["errors"] for name, details in report["tables"].items() if not details["passed"]}
        bad_sequences = {name: details for name, details in report["sequences"].items() if not details.get("ok")}
        security = report["security"]
        isolation = report["isolation"]
        pytest.fail(f"bad_tables={bad_tables}; bad_sequences={bad_sequences}; security={security}; isolation={isolation}")

    assert report["tables"]["users"]["row_count"] == 1
    assert report["tables"]["gex_snapshots"]["row_count"] == 1

    with postgres_engine.connect() as conn:
        connection_count = conn.execute(text('SELECT count(*) FROM "broker_connections"')).scalar_one()
        assert connection_count == 1

    sqlite_engine.dispose()
