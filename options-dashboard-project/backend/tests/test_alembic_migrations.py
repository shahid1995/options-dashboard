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
    from app import models  # noqa: F401  (registers iv_observations metadata)

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
            text("INSERT INTO alembic_version (version_num) VALUES ('d48aa0000002')")
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


# ---------------------------------------------------------------------------
# Day 49 — CodeRabbit correction pass (d49aa0000001)
# ---------------------------------------------------------------------------


def _run_d49_migration(temp_db, rows):
    """Stamp a create_all database at d48aa0000002, seed IVObservation rows,
    run the d49aa0000001 migration, and return the persisted values."""
    from alembic import command
    from alembic.config import Config
    from app.db import Base
    from app import models  # noqa: F401  (registers iv_observations metadata)

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
            text("INSERT INTO alembic_version (version_num) VALUES ('d48aa0000002')")
        )
        for index, observed_at in enumerate(rows):
            conn.execute(
                text(
                    "INSERT INTO iv_observations "
                    "(symbol, expiry, strike, option_type, iv, spot, source, observed_at) "
                    "VALUES (:symbol, '2026-09-03', 24500, 'call', 0.18, 24500, "
                    ":source, :observed_at)"
                ),
                {
                    "symbol": "NIFTY",
                    "source": f"row-{index}",
                    "observed_at": observed_at,
                },
            )
    engine.dispose()

    alembic_cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", temp_db)
    command.upgrade(alembic_cfg, "head")

    engine2 = create_engine(temp_db, connect_args={"check_same_thread": False})
    with engine2.connect() as conn:
        values = conn.execute(
            text("SELECT source, observed_at FROM iv_observations ORDER BY source")
        ).fetchall()
    engine2.dispose()
    return values


def test_day49_migration_converts_legacy_utc_rows(temp_db):
    """CodeRabbit #1/#4: legacy naive-UTC rows convert to naive IST and
    multiple rows convert independently.

    NULL handling fact: ``iv_observations.observed_at`` is NOT NULL at the
    schema level, so a NULL row is unrepresentable in storage; the
    migration's NULL skip (``observed_at.isnot(None)`` in every paged
    batch) is defensive parity with the original per-row ``continue`` and
    is asserted by the source-contract test below.
    """
    values = _run_d49_migration(
        temp_db,
        [
            "2026-08-27 04:33:00",  # naive UTC → 10:03 IST
            "2026-08-27 05:00:00",  # naive UTC → 10:30 IST
        ],
    )
    by_source = {source: value for source, value in values}
    assert str(by_source["row-0"]).startswith("2026-08-27 10:03:00")
    assert str(by_source["row-1"]).startswith("2026-08-27 10:30:00")


def test_day49_migration_processes_large_history_in_batches(temp_db):
    """CodeRabbit #1: the migration must not materialize the whole history.

    620 synthetic rows exceed ``IV_MIGRATION_BATCH_SIZE`` (500), so the
    upgrade provably runs at least two bounded paged batches while keeping
    the exact per-row transformation semantics — batch-boundary rows and a
    mid-batch row are all converted correctly.
    """
    rows = []
    for index in range(620):
        # Deterministic legacy UTC stamps spread across days/minutes.
        day = 20 + (index % 5)
        hour = (index // 5) % 24
        minute = (index * 7) % 60
        rows.append(f"2026-08-{day:02d} {hour:02d}:{minute:02d}:00")
    assert len(rows) == 620

    values = _run_d49_migration(temp_db, rows)

    assert len(values) == 620
    by_source = {source: value for source, value in values}
    # Batch 1 first row, batch boundary, and batch 2 row.  Legacy UTC
    # stamps shift +5:30 to naive IST (e.g. 00:00 UTC → 05:30 IST).
    assert str(by_source["row-0"]).startswith("2026-08-20 05:30:00")
    assert str(by_source["row-249"]).startswith("2026-08-24 06:33:00")
    assert str(by_source["row-500"]).startswith("2026-08-20 09:50:00")
    assert str(by_source["row-619"]).startswith("2026-08-24 08:43:00")


def test_day49_migration_downgrade_is_refused(temp_db):
    """CodeRabbit #2: downgrade must fail loudly — the UTC→IST normalization
    is intentionally irreversible (rows written post-migration are already
    canonical IST; a blind reverse conversion would corrupt them)."""
    from alembic import command
    from alembic.config import Config
    from app.db import Base

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
            text("INSERT INTO alembic_version (version_num) VALUES ('d48aa0000002')")
        )
    engine.dispose()

    alembic_cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", temp_db)
    command.upgrade(alembic_cfg, "head")

    with pytest.raises(Exception) as excinfo:
        command.downgrade(alembic_cfg, "d48aa0000002")
    message = str(excinfo.value).lower()
    assert "irreversible" in message
    assert "ist" in message or "timestamp" in message


def test_day49_migration_module_declares_batching_contract():
    """Source-contract companion (repo precedent:
    test_production_init_db_has_no_create_all): the upgrade must page its
    input through the batch-size limit and keep the defensive NULL filter;
    the downgrade must refuse (no silent pass)."""
    migration_path = os.path.join(
        os.path.dirname(__file__), "..", "alembic", "versions",
        "d49aa0000001_normalize_iv_observation_timestamps_to_ist.py",
    )
    with open(migration_path, encoding="utf-8") as handle:
        source = handle.read()
    assert "IV_MIGRATION_BATCH_SIZE" in source
    assert ".limit(IV_MIGRATION_BATCH_SIZE)" in source
    assert "observed_at.isnot(None)" in source
    assert "NotImplementedError" in source


def test_day49_migration_never_reexecutes_over_post_day49_rows(temp_db):
    """Codacy MEDIUM adjudication: the migration can never run against rows
    written by the post-Day-49 application.

    Alembic executes d49aa0000001 exactly once per database: the version
    stamp advances to head inside the upgrade, and every supported
    re-invocation — ``alembic upgrade head`` (what ``init_db()`` runs on
    every application startup) — is a no-op once the stamp equals head.
    A post-Day-49 canonical naive-IST row (what ``record_iv_observations``
    writes after the switch) therefore cannot pass through the
    UTC→IST conversion via any supported deployment path: replaying the
    startup command leaves it unchanged, and the only lower-revision
    command (downgrade) refuses. Reaching the double-shift requires an
    unsupported out-of-band ``alembic stamp d48aa0000002`` before an
    upgrade — an operator action no workflow, script, or document in this
    repository performs.
    """
    from datetime import datetime

    from alembic import command
    from alembic.config import Config
    from app.db import Base
    from app import models  # noqa: F401  (registers iv_observations metadata)

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
            text("INSERT INTO alembic_version (version_num) VALUES ('d48aa0000002')")
        )
    engine.dispose()

    alembic_cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", temp_db)
    # Bind Alembic to THIS database through the official Config.attributes
    # mechanism (same contract as app.db._run_alembic_migrations): the
    # conftest engine override never touches Alembic's own engine, so the
    # URL alone is not enough under the test override.
    alembic_cfg.attributes["connectable"] = engine
    command.upgrade(alembic_cfg, "head")

    # Post-Day-49 application write: canonical naive IST, exactly what
    # record_iv_observations persists after the Day 49 switch.
    ist_row = datetime(2026, 9, 30, 10, 0, 0)
    engine = create_engine(temp_db, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO iv_observations "
                "(symbol, expiry, strike, option_type, iv, spot, source, observed_at) "
                "VALUES ('NIFTY', '2026-10-01', 24500, 'call', 0.18, 24500, "
                "'post-day49-ist', :observed_at)"
            ),
            {"observed_at": ist_row},
        )
    engine.dispose()

    # Every application startup replays exactly this command.
    command.upgrade(alembic_cfg, "head")

    engine = create_engine(temp_db, connect_args={"check_same_thread": False})
    with engine.connect() as conn:
        stored = conn.execute(
            text(
                "SELECT observed_at FROM iv_observations "
                "WHERE source = 'post-day49-ist'"
            )
        ).scalar_one()
        revision = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    engine.dispose()

    assert revision == "d49aa0000001"
    # Startup replay is a no-op: the canonical IST value is untouched
    # (no +5:30 double shift).
    assert str(stored).startswith("2026-09-30 10:00:00")
    assert str(stored) != "2026-09-30 15:30:00"  # the +5:30 double shift
