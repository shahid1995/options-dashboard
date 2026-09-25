"""Day 4 — PostgreSQL production foundation tests.

Verifies:
1. Database URL normalization for all dialect variants
2. Engine configuration: PostgreSQL pool settings vs SQLite
3. Production safety: warning when production lacks PostgreSQL
4. Health check: dialect-aware, file info only for SQLite
5. Readiness endpoint: liveness vs readiness, no secret leakage
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


# ---------------------------------------------------------------------------
# 1. normalize_database_url coverage
# ---------------------------------------------------------------------------

class TestNormalizeDatabaseUrl:
    """Verify URL normalization for all supported input variants."""

    def test_postgres_bare(self):
        from app.db import normalize_database_url
        result = normalize_database_url("postgres://user:pass@host:5432/db")
        assert result == "postgresql+psycopg://user:pass@host:5432/db"

    def test_postgresql_bare(self):
        from app.db import normalize_database_url
        result = normalize_database_url("postgresql://user:pass@host:5432/db")
        assert result == "postgresql+psycopg://user:pass@host:5432/db"

    def test_explicit_psycopg_preserved(self):
        from app.db import normalize_database_url
        url = "postgresql+psycopg://user:pass@host:5432/db"
        assert normalize_database_url(url) == url

    def test_sqlite_unchanged(self):
        from app.db import normalize_database_url
        url = "sqlite:////tmp/test.db"
        assert normalize_database_url(url) == url

    def test_empty_string(self):
        from app.db import normalize_database_url
        assert normalize_database_url("") == ""

    def test_postgres_with_railway_format(self):
        """Railway provides postgres:// URLs without explicit driver."""
        from app.db import normalize_database_url
        result = normalize_database_url(
            "postgres://postgres:secret@container-postgres.railway.internal:5432/railway"
        )
        assert result.startswith("postgresql+psycopg://")
        assert "container-postgres.railway.internal" in result
        # Credentials must not be stripped
        assert "secret" in result


# ---------------------------------------------------------------------------
# 2. Engine configuration
# ---------------------------------------------------------------------------

class TestEngineConfiguration:
    """Verify engine creation for PostgreSQL vs SQLite."""

    def test_postgresql_engine_has_pool_settings(self):
        """PostgreSQL engine must have connection pool configuration."""
        url = "postgresql+psycopg://user:pass@localhost:5432/test"
        eng = create_engine(
            url,
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,
            pool_pre_ping=True,
        )
        pool = eng.pool
        assert pool.size() == 5
        assert pool._max_overflow == 10
        assert pool._timeout == 30
        assert pool._recycle == 1800
        assert pool._pre_ping is True
        eng.dispose()

    def test_sqlite_engine_uses_static_pool_in_tests(self):
        """SQLite test engines use StaticPool for in-memory isolation."""
        eng = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        assert isinstance(eng.pool, StaticPool)
        eng.dispose()

    def test_db_module_engine_is_configured(self):
        """The module-level engine in app.db must be configured at import time."""
        from app.db import engine
        assert engine is not None
        # Engine must be usable
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            assert result.scalar_one() == 1


# ---------------------------------------------------------------------------
# 3. Production safety validation
# ---------------------------------------------------------------------------

class TestProductionSafety:
    """Verify production environment detection and warnings."""

    def test_config_has_is_production(self):
        """Settings must expose IS_PRODUCTION for production detection."""
        from app.config import Settings
        # IS_PRODUCTION should be a property or field on Settings
        s = Settings()
        assert hasattr(s, "IS_PRODUCTION")
        assert isinstance(s.IS_PRODUCTION, bool)

    def test_is_production_false_in_test_env(self):
        """IS_PRODUCTION must be False when no production indicators are set."""
        from app.config import Settings
        with patch.dict(os.environ, {}, clear=False):
            # Remove production indicators if present
            env = os.environ.copy()
            env.pop("RAILWAY_ENVIRONMENT", None)
            env.pop("RAILWAY_SERVICE_NAME", None)
            env.pop("PRODUCTION", None)
            with patch.dict(os.environ, env, clear=True):
                s = Settings()
                assert s.IS_PRODUCTION is False

    def test_is_production_true_with_railway(self):
        """IS_PRODUCTION must be True when RAILWAY_ENVIRONMENT is set."""
        from app.config import Settings
        with patch.dict(os.environ, {"RAILWAY_ENVIRONMENT": "production"}):
            s = Settings()
            assert s.IS_PRODUCTION is True

    def test_validate_production_config_fails_closed_missing_url(self, caplog):
        """Production without DATABASE_URL must fail closed (fail-closed hardening).

        Originally a warning-only contract (Day 4); production may NEVER
        silently fall back to SQLite, so this now raises.
        """
        from app.db import validate_production_config
        import pytest

        with patch("app.db.settings") as mock_settings:
            mock_settings.IS_PRODUCTION = True
            mock_settings.DATABASE_URL = None
            import logging
            with caplog.at_level(logging.WARNING, logger="app.db"):
                with pytest.raises(RuntimeError, match="production database configuration is required"):
                    validate_production_config()
            assert any("DATABASE_URL" in r.message for r in caplog.records)

    def test_validate_production_config_fails_closed_sqlite_url(self, caplog):
        """Production with sqlite:// DATABASE_URL must fail closed (fail-closed hardening).

        Originally a warning-only contract (Day 4); production may NEVER
        run on SQLite, so this now raises.
        """
        from app.db import validate_production_config
        import pytest

        with patch("app.db.settings") as mock_settings:
            mock_settings.IS_PRODUCTION = True
            mock_settings.DATABASE_URL = "sqlite:///production.db"
            import logging
            with caplog.at_level(logging.WARNING, logger="app.db"):
                with pytest.raises(RuntimeError, match="production database configuration is required"):
                    validate_production_config()
            assert any("SQLite" in r.message for r in caplog.records)

    def test_validate_production_config_ok_with_postgresql(self, caplog):
        """Production with PostgreSQL DATABASE_URL must not warn."""
        from app.db import validate_production_config
        with patch("app.db.settings") as mock_settings:
            mock_settings.IS_PRODUCTION = True
            mock_settings.DATABASE_URL = "postgresql://user:pass@host:5432/db"
            import logging
            with caplog.at_level(logging.WARNING, logger="app.db"):
                validate_production_config()
            assert not any("DATABASE_URL" in r.message for r in caplog.records)
            assert not any("SQLite" in r.message for r in caplog.records)

    def test_validate_production_config_silent_in_non_production(self, caplog):
        """Non-production must not log warnings."""
        from app.db import validate_production_config
        with patch("app.db.settings") as mock_settings:
            mock_settings.IS_PRODUCTION = False
            mock_settings.DATABASE_URL = None
            import logging
            with caplog.at_level(logging.WARNING, logger="app.db"):
                validate_production_config()
            assert not any("DATABASE_URL" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 4. Health check — dialect-aware
# ---------------------------------------------------------------------------

class TestHealthCheck:
    """Verify check_database_health() works for both SQLite and PostgreSQL."""

    def test_health_check_includes_dialect(self):
        """Health report must include the active dialect name."""
        from app.db import check_database_health
        report = check_database_health()
        assert "dialect" in report
        assert report["dialect"] in ("sqlite", "postgresql")

    def test_health_check_sqlite_includes_file_info(self):
        """SQLite health report includes file_exists and file_size_bytes."""
        from app.db import check_database_health
        report = check_database_health()
        if report["dialect"] == "sqlite":
            assert "file_exists" in report
            assert "file_size_bytes" in report

    def test_health_check_postgresql_excludes_file_info(self):
        """PostgreSQL health report does NOT include file-specific fields."""
        # This test only makes sense when running against PostgreSQL.
        # In test env with in-memory SQLite, we skip.
        from app.db import engine
        if engine.dialect.name != "postgresql":
            pytest.skip("Not running against PostgreSQL")
        from app.db import check_database_health
        report = check_database_health()
        assert "file_exists" not in report
        assert "file_size_bytes" not in report

    def test_health_check_accessible_or_schema_limited(self):
        """Health check returns a valid report.

        In the test environment (in-memory SQLite without full schema),
        accessible may be False because model tables don't exist. In
        production with a real database, accessible must be True.
        This test verifies the report structure is correct.
        """
        from app.db import check_database_health, engine
        report = check_database_health()
        # Report must have all expected keys
        assert "accessible" in report
        assert "dialect" in report
        assert "tables_present" in report
        assert "tables_missing" in report
        assert "row_counts" in report
        # In test env (in-memory SQLite), tables may not exist
        if engine.dialect.name == "sqlite":
            # Acceptable: test DB has limited schema
            assert isinstance(report["accessible"], bool)
        else:
            # Production PostgreSQL must be accessible
            assert report["accessible"] is True

    def test_health_check_no_credentials_in_report(self):
        """Health report must not contain database URL or credentials."""
        from app.db import check_database_health
        report = check_database_health()
        report_str = str(report)
        # Should not contain password patterns
        assert "password" not in report_str.lower()
        assert "secret" not in report_str.lower()
        # database_path for PostgreSQL should not contain credentials
        if report["dialect"] == "postgresql":
            assert "@" not in report.get("database_path", "")


# ---------------------------------------------------------------------------
# 5. Readiness endpoint behavior
# ---------------------------------------------------------------------------

class TestReadinessEndpoint:
    """Verify the /readiness endpoint distinguishes liveness from readiness."""

    def test_health_endpoint_returns_ok(self):
        """GET /health must return 200 with status ok."""
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_readiness_endpoint_returns_ready(self):
        """GET /readiness must return 200 when DB is reachable."""
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        resp = client.get("/readiness")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert "checks" in data
        assert data["checks"]["database"] == "ok"

    def test_readiness_no_connection_string(self):
        """Readiness response must never expose connection strings."""
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        resp = client.get("/readiness")
        body = resp.text
        assert "postgresql" not in body.lower() or "database" in body.lower()
        assert "psycopg" not in body
        assert "5432" not in body

    def test_readiness_checks_token_store(self):
        """Readiness must also check token store health."""
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        resp = client.get("/readiness")
        data = resp.json()
        assert "token_store" in data["checks"]

    def test_readiness_503_on_db_failure(self):
        """Readiness returns 503 when database is unreachable."""
        from fastapi.testclient import TestClient
        from app.main import app
        from sqlalchemy import create_engine

        # Create a broken engine
        broken_engine = create_engine("sqlite:///:memory:")

        import app.db as db_mod
        original_engine = db_mod.engine
        try:
            # Patch engine to simulate DB failure
            class BrokenEngine:
                dialect = type("D", (), {"name": "sqlite"})()

                class pool:
                    @staticmethod
                    def size():
                        return 0

                def connect(self):
                    raise ConnectionError("DB unreachable")

                def dispose(self):
                    pass

            db_mod.engine = BrokenEngine()
            client = TestClient(app)
            resp = client.get("/readiness")
            assert resp.status_code == 503
            data = resp.json()
            assert data["status"] == "degraded"
            assert "error" in data["checks"]["database"]
        finally:
            db_mod.engine = original_engine
