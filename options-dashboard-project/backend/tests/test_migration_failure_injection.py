from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text

TESTS_DIR = Path(__file__).resolve().parent
TOOLS_DIR = TESTS_DIR.parent / "tools"
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TOOLS_DIR))

from test_rehearsal_ci_database import (  # noqa: E402
    _clear_postgres_target,
    _populate_current_schema_synthetic_dataset,
    _prepare_schema,
)
from migrate_sqlite_to_postgres import migrate_database  # noqa: E402
from tests import issue145_pg_support as support  # noqa: E402


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


def _make_source_engine(tmp_path):
    return create_engine(
        f"sqlite:///{tmp_path / 'failure-injection.db'}",
        connect_args={"check_same_thread": False},
    )


def _assert_target_empty(engine):
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM "users"')).scalar_one() == 0
        assert conn.execute(text('SELECT COUNT(*) FROM "broker_connections"')).scalar_one() == 0
        assert conn.execute(text('SELECT COUNT(*) FROM "gex_snapshots"')).scalar_one() == 0


def test_migration_failure_rolls_back_all_imported_rows(tmp_path, postgres_engine):
    sqlite_engine = _make_source_engine(tmp_path)
    try:
        _prepare_schema(sqlite_engine)
        _populate_current_schema_synthetic_dataset(sqlite_engine)
        _clear_postgres_target(postgres_engine)

        @event.listens_for(postgres_engine, "before_cursor_execute")
        def fail_on_users_insert(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().lower().startswith("insert into users"):
                raise RuntimeError("injected migration failure")

        try:
            with pytest.raises(RuntimeError, match="injected migration failure"):
                migrate_database(sqlite_engine, postgres_engine, batch_size=1000)
        finally:
            event.remove(postgres_engine, "before_cursor_execute", fail_on_users_insert)

        _assert_target_empty(postgres_engine)
    finally:
        sqlite_engine.dispose()


def test_migration_failure_during_later_table_rolls_back_earlier_tables(
    tmp_path, postgres_engine
):
    sqlite_engine = _make_source_engine(tmp_path)
    try:
        _prepare_schema(sqlite_engine)
        _populate_current_schema_synthetic_dataset(sqlite_engine)
        _clear_postgres_target(postgres_engine)

        @event.listens_for(postgres_engine, "before_cursor_execute")
        def fail_on_gex_insert(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().lower().startswith("insert into gex_snapshots"):
                raise RuntimeError("injected later-table failure")

        try:
            with pytest.raises(RuntimeError, match="injected later-table failure"):
                migrate_database(sqlite_engine, postgres_engine, batch_size=1000)
        finally:
            event.remove(postgres_engine, "before_cursor_execute", fail_on_gex_insert)

        _assert_target_empty(postgres_engine)
    finally:
        sqlite_engine.dispose()


def test_migration_failure_during_sequence_repair_rolls_back_data(
    tmp_path, postgres_engine
):
    sqlite_engine = _make_source_engine(tmp_path)
    try:
        _prepare_schema(sqlite_engine)
        _populate_current_schema_synthetic_dataset(sqlite_engine)
        _clear_postgres_target(postgres_engine)

        @event.listens_for(postgres_engine, "before_cursor_execute")
        def fail_on_sequence_repair(conn, cursor, statement, parameters, context, executemany):
            if "pg_get_serial_sequence" in statement.lower():
                raise RuntimeError("injected sequence-repair failure")

        try:
            with pytest.raises(RuntimeError, match="injected sequence-repair failure"):
                migrate_database(sqlite_engine, postgres_engine, batch_size=1000)
        finally:
            event.remove(postgres_engine, "before_cursor_execute", fail_on_sequence_repair)

        _assert_target_empty(postgres_engine)
    finally:
        sqlite_engine.dispose()
