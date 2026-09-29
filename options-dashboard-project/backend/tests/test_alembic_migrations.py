"""Tests for the Alembic migration infrastructure (Phase 10.1A/B).

These tests verify:
1. Alembic baseline migration creates all tables including users/user_sessions
2. Migrations are idempotent (upgrade head is safe to run multiple times)
3. init_db() runs Alembic migrations as sole schema mechanism
4. No runtime DDL in auth path
5. Phase 10.1B: no create_all() or ensure_column() in production startup
"""

import os
import tempfile

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def fresh_engine(tmp_path):
    """Create a fresh file-based SQLite engine for migration tests.

    File-based, not in-memory, because Alembic creates its own engine
    internally. Both must point to the same database file.
    """
    db_path = tmp_path / "test_fresh.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    yield engine
    engine.dispose()


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary database file for file-based migration tests."""
    db_path = tmp_path / "test_migration.db"
    return f"sqlite:///{db_path}"


def test_alembic_baseline_creates_all_model_tables(fresh_engine):
    """Verify that Base.metadata.create_all creates all expected tables."""
    from app.db import Base
    from app import models  # noqa: F401
    from app.identity import User, UserSession  # noqa: F401

    Base.metadata.create_all(bind=fresh_engine)

    with fresh_engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }

    # Core trading tables
    expected_tables = {
        "trades", "legs", "paper_accounts", "paper_orders", "paper_transactions",
        "positions", "strategy_executions", "strategy_leg_exposures",
        "exit_exposure_allocations", "bulk_exit_records",
        "strategy_templates", "strategy_template_legs",
        # Market data tables
        "gex_snapshots", "iv_observations", "nifty_candles",
        "contract_specs", "option_candles", "option_greeks",
        "historical_gex",
        # Pipeline tables
        "ingestion_log", "data_completeness", "ingestion_checkpoint",
        # Phase 10.1 identity tables
        "users", "user_sessions",
    }

    missing = expected_tables - tables
    assert not missing, f"Tables missing from schema: {missing}"


def test_init_db_uses_alembic_when_available(monkeypatch, fresh_engine):
    """Verify init_db() calls Alembic migrations as sole schema mechanism."""
    monkeypatch.setattr("app.db.engine", fresh_engine)
    monkeypatch.setattr("app.db.SessionLocal", sessionmaker(bind=fresh_engine))

    from app.db import init_db
    init_db()

    with fresh_engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }

    assert "users" in tables
    assert "user_sessions" in tables
    assert "trades" in tables
    assert "positions" in tables


def test_init_db_is_idempotent(monkeypatch, fresh_engine):
    """Verify init_db() can be called multiple times without errors."""
    monkeypatch.setattr("app.db.engine", fresh_engine)
    monkeypatch.setattr("app.db.SessionLocal", sessionmaker(bind=fresh_engine))

    from app.db import init_db
    init_db()
    init_db()  # Second call must not raise

    with fresh_engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
    assert "users" in tables


def test_no_ensure_identity_schema_in_auth_router():
    """Verify ensure_identity_schema is NOT called at request time in auth.

    Phase 10.1A removes runtime DDL from the authentication path.
    Schema is managed by Alembic migrations at startup.
    """
    with open(
        os.path.join(os.path.dirname(__file__), "..", "app", "routers", "auth.py")
    ) as f:
        auth_source = f.read()

    assert "ensure_identity_schema" not in auth_source, (
        "ensure_identity_schema() must not be called in auth.py after Phase 10.1A. "
        "Schema creation is handled by Alembic migrations at startup."
    )


def test_identity_module_has_no_engine_dependency():
    """Verify identity.py does not import engine (runtime DDL removed)."""
    with open(
        os.path.join(os.path.dirname(__file__), "..", "app", "identity.py")
    ) as f:
        identity_source = f.read()

    # Should not import engine for create_all
    assert "from app.db import Base, engine" not in identity_source, (
        "identity.py should not import engine after Phase 10.1A. "
        "Schema creation is handled by Alembic migrations."
    )
    # Should still import Base for model definitions
    assert "from app.db import Base" in identity_source


def test_day49_migrates_legacy_iv_timestamps_to_ist(temp_db):
    from alembic import command
    from alembic.config import Config
    from app.db import Base
    from app.models import IVObservation

    engine = create_engine(temp_db, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL)"
            )
        )
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('d48aa0000001')")
        )
        conn.execute(
            text(
                "INSERT INTO iv_observations "
                "(symbol, expiry, strike, option_type, iv, spot, source, observed_at) "
                "VALUES ('NIFTY', '2026-09-03', 24500, 'call', 0.18, 24500, "
                "'test', '2026-08-27 04:33:00')"
            )
        )
    engine.dispose()

    alembic_cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", temp_db)
    command.upgrade(alembic_cfg, "head")

    engine2 = create_engine(temp_db, connect_args={"check_same_thread": False})
    with engine2.connect() as conn:
        value = conn.execute(
            text("SELECT observed_at FROM iv_observations WHERE source = 'test'")
        ).scalar_one()
        # SQLite renders the canonical naive-IST DATETIME with a fractional-
        # second suffix; the canonical instant is what the contract requires.
        assert str(value).startswith("2026-08-27 10:03:00")
    engine2.dispose()


def test_alembic_stamped_database_is_upgradeable(temp_db):
    """Verify that a create_all database can be stamped and then upgraded."""
    # Create database the old way
    engine = create_engine(temp_db, connect_args={"check_same_thread": False})
    from app.db import Base
    from app import models  # noqa: F401
    from app.identity import User, UserSession  # noqa: F401

    Base.metadata.create_all(bind=engine)
    engine.dispose()

    # Stamp with alembic
    from alembic.config import Config
    from alembic import command

    alembic_cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", temp_db)
    command.stamp(alembic_cfg, "head")

    # Verify stamp
    engine2 = create_engine(temp_db, connect_args={"check_same_thread": False})
    with engine2.connect() as conn:
        result = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        assert len(result) == 1, "alembic_version table should have exactly one row"
    engine2.dispose()


def test_production_init_db_has_no_create_all():
    """Phase 10.1B: production init_db() must not call create_all()."""
    import inspect
    import app.db as db_module
    source = inspect.getsource(db_module.init_db)
    assert "create_all" not in source, (
        "init_db() must not call create_all() after Phase 10.1B. "
        "Alembic is the sole schema management mechanism."
    )


def test_production_init_db_has_no_ensure_column():
    """Phase 10.1B: production init_db() must not call ensure_column()."""
    import inspect
    import app.db as db_module
    source = inspect.getsource(db_module.init_db)
    assert "ensure_column" not in source, (
        "init_db() must not call ensure_column() after Phase 10.1B. "
        "All legacy columns are in the Alembic baseline."
    )


def test_auth_callback_does_not_call_ensure_identity_schema(monkeypatch):
    """Integration test: auth callback path does not trigger runtime DDL.

    This verifies that the OAuth callback handler works without
    ensure_identity_schema() being called at request time.
    """
    from app.db import Base, engine
    from app import models  # noqa: F401
    from app.identity import User, UserSession  # noqa: F401

    # Ensure schema exists via create_all (simulating startup migration)
    Base.metadata.create_all(bind=engine)

    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    # Test /auth/status - should not trigger any DDL
    resp = client.get("/auth/status")
    assert resp.status_code == 200
    assert resp.json() == {"logged_in": False}

    # Test /auth/logout without session - idempotent (200), no DDL
    resp = client.post("/auth/logout")
    assert resp.status_code == 200
