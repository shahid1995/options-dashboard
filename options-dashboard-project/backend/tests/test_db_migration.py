"""Tests for the init_db startup path after Phase 10.1B (Alembic cutover).

Phase 10.1B removes create_all() and ensure_column() from production startup.
Alembic is now the sole authoritative schema management mechanism.

These tests verify:
- Alembic upgrade creates all 42 application tables
- init_db() does NOT call create_all()
- init_db() does NOT call ensure_column()
- init_db() is idempotent
- init_db() preserves existing data
- The backfill session uses the correct engine
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def temp_engine(tmp_path):
    """File-based SQLite engine for migration tests.

    In-memory SQLite gives each engine its own database, so Alembic's
    internal engine would be separate from the test engine. File-based
    databases ensure both point to the same file.
    """
    db_path = tmp_path / "test_migration.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    yield engine
    engine.dispose()


def _existing_columns(engine, table: str) -> set[str]:
    """Helper to check columns on a table."""
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return {r[1] for r in rows}


def test_init_db_uses_alembic_not_create_all(monkeypatch, temp_engine):
    """init_db() must NOT call Base.metadata.create_all().

    Phase 10.1B: Alembic is the sole schema management mechanism.
    """
    import inspect
    import app.db as db_module
    source = inspect.getsource(db_module.init_db)
    assert "create_all" not in source, (
        "init_db() must not call create_all() after Phase 10.1B. "
        "Alembic is the sole schema management mechanism."
    )


def test_init_db_uses_alembic_not_ensure_column(monkeypatch, temp_engine):
    """init_db() must NOT call ensure_column().

    Phase 10.1B: all legacy ensure_column columns are in the Alembic baseline.
    """
    import inspect
    import app.db as db_module
    source = inspect.getsource(db_module.init_db)
    assert "ensure_column" not in source, (
        "init_db() must not call ensure_column() after Phase 10.1B. "
        "All legacy columns are in the Alembic baseline."
    )


def test_ensure_column_function_removed():
    """The ensure_column() and _existing_columns() functions should be removed."""
    import app.db as db_module
    assert not hasattr(db_module, "ensure_column"), (
        "ensure_column() must be removed from db.py after Phase 10.1B"
    )
    assert not hasattr(db_module, "_existing_columns"), (
        "_existing_columns() must be removed from db.py after Phase 10.1B"
    )


def test_init_db_uses_alembic(monkeypatch, temp_engine):
    """init_db() must run Alembic migrations to create all tables."""
    monkeypatch.setattr("app.db.engine", temp_engine)
    monkeypatch.setattr("app.db.SessionLocal", sessionmaker(bind=temp_engine))

    from app.db import init_db
    init_db()

    with temp_engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }

    # Core application tables + alembic_version (schema asserted below)
    assert "users" in tables
    assert "user_sessions" in tables
    assert "trades" in tables
    assert "positions" in tables
    assert "paper_orders" in tables
    assert "alembic_version" in tables

    # Account-security lifecycle tables (Alembic c1d2e3f4a5b6)
    assert "email_verification_tokens" in tables
    assert "password_reset_tokens" in tables
    assert "pending_email_changes" in tables
    assert "security_events" in tables

    # Broker raw-ingress internals (Day41) and Day41.2 lock table
    assert "broker_raw_observation" in tables
    assert "order_family_sync_lock" in tables
    assert "broker_authorizations" in tables

    # Day 45 admin control plane (Issue #90) adds its two tables.
    assert "admin_controls" in tables
    assert "admin_audit_events" in tables

    # 42 application tables + alembic_version.
    # History: 24 app tables at the Phase 10.1B Alembic cutover; 37 after the
    # broker_sync expansions; 39 after Day41.2 (order_family_sync_lock +
    # broker_authorizations via the merge head); 43 since Auth account-security
    # c1d2e3f4a5b6 added email_verification_tokens, password_reset_tokens,
    # pending_email_changes, security_events; 49 since Issue #17 research
    # schema e9f8a7b6c5d4 added the six gap_* research tables (research-only).
    assert len(tables) ==  51


def test_init_db_creates_legacy_columns_via_baseline(monkeypatch, temp_engine):
    """Columns previously managed by ensure_column() must exist after Alembic.

    These 15 columns were the ones ensure_column() used to add. They are
    now in the Alembic baseline and created by alembic upgrade head.
    """
    monkeypatch.setattr("app.db.engine", temp_engine)
    monkeypatch.setattr("app.db.SessionLocal", sessionmaker(bind=temp_engine))

    from app.db import init_db
    init_db()

    # Phase 5.0 columns on trades
    cols = _existing_columns(temp_engine, "trades")
    assert "strategy_execution_id" in cols
    assert "client_order_id" in cols

    # Phase 6.10/7.0 columns on strategy_executions
    cols = _existing_columns(temp_engine, "strategy_executions")
    assert "execution_metadata" in cols
    assert "tags" in cols
    assert "notes" in cols

    # Phase 6.8B columns on strategy_template_legs
    cols = _existing_columns(temp_engine, "strategy_template_legs")
    assert "strike_mode" in cols
    assert "strike_offset" in cols
    assert "expiry_mode" in cols
    assert "formula_version" in cols

    # Phase 7.6/8F columns on gex_snapshots
    cols = _existing_columns(temp_engine, "gex_snapshots")
    assert "sweep_data" in cols
    assert "owner_id" in cols


def test_init_db_is_idempotent(monkeypatch, temp_engine):
    """Running init_db() twice must not raise or corrupt schema."""
    monkeypatch.setattr("app.db.engine", temp_engine)
    monkeypatch.setattr("app.db.SessionLocal", sessionmaker(bind=temp_engine))

    from app.db import init_db
    init_db()
    init_db()  # Second call must not raise

    with temp_engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
    assert "users" in tables
    assert "alembic_version" in tables


def test_init_db_preserves_existing_data(monkeypatch, temp_engine):
    """init_db() must not delete existing rows."""
    monkeypatch.setattr("app.db.engine", temp_engine)
    monkeypatch.setattr("app.db.SessionLocal", sessionmaker(bind=temp_engine))

    from app.db import init_db

    # First init to create schema
    init_db()

    # Insert test data
    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, status, identity_source, created_at, updated_at) "
                "VALUES (:id, :status, :source, :created, :updated)"
            ),
            {
                "id": "test-preservation-user",
                "status": "active",
                "source": "upstox",
                "created": "2026-01-01 00:00:00",
                "updated": "2026-01-01 00:00:00",
            },
        )

    # Second init — must preserve data
    init_db()

    with temp_engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM users WHERE id = :id"),
            {"id": "test-preservation-user"},
        ).scalar()
    assert count == 1, "init_db() must not delete existing data"


def test_init_db_session_uses_same_engine(monkeypatch, temp_engine):
    """The backfill session in init_db() must use the current engine variable.

    This is critical for tests that monkeypatch the engine.
    """
    monkeypatch.setattr("app.db.engine", temp_engine)
    monkeypatch.setattr("app.db.SessionLocal", sessionmaker(bind=temp_engine))

    from app.db import init_db

    # Must not raise — the backfill session must see the tables
    init_db()

    with temp_engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
    assert "positions" in tables

    # Verify the backfill session could query without error
    from app.models import Position
    from sqlalchemy import select

    with temp_engine.connect() as conn:
        result = conn.execute(select(Position.user_id).distinct())
        assert result.fetchall() == []
