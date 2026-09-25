"""Fail-closed production database configuration (post-merge hardening).

Production (``STRIKENOVA_ENV=production`` — provider-neutral, no
Railway-era markers required) must NEVER start on SQLite:

- missing ``DATABASE_URL``        -> startup fails (no silent local fallback)
- SQLite ``DATABASE_URL``         -> startup fails
- PostgreSQL/CockroachDB URL      -> accepted (psycopg normalization intact)
- non-production environments keep intentional SQLite behavior

Failure messages name the missing/invalid configuration WITHOUT ever
embedding the full connection string (no credentials in logs).
"""

import inspect
import logging
import os
from unittest.mock import patch

import pytest

from app.config import Settings
from app.db import validate_production_config


class TestProviderNeutralProductionDetection:
    """STRIKENOVA_ENV is the provider-neutral production signal."""

    def test_production_marker_enables_is_production(self):
        with patch.dict(os.environ, {"STRIKENOVA_ENV": "production"}, clear=False):
            s = Settings()
            assert s.IS_PRODUCTION is True

    def test_production_marker_case_insensitive(self):
        with patch.dict(os.environ, {"STRIKENOVA_ENV": "Production"}, clear=False):
            s = Settings()
            assert s.IS_PRODUCTION is True

    def test_no_markers_means_not_production(self):
        env = os.environ.copy()
        for key in (
            "STRIKENOVA_ENV",
            "RAILWAY_ENVIRONMENT",
            "RAILWAY_SERVICE_NAME",
            "PRODUCTION",
        ):
            env.pop(key, None)
        with patch.dict(os.environ, env, clear=True):
            s = Settings()
            assert s.IS_PRODUCTION is False

    def test_non_production_marker_is_not_production(self):
        # Clear any legacy markers that may exist on the host so the
        # non-production assertion is hermetic.
        env = os.environ.copy()
        for key in ("RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_NAME", "PRODUCTION"):
            env.pop(key, None)
        env["STRIKENOVA_ENV"] = "staging"
        with patch.dict(os.environ, env, clear=True):
            s = Settings()
            assert s.IS_PRODUCTION is False

    def test_render_style_environment_needs_no_railway_variables(self):
        """A Render-style deployment sets only STRIKENOVA_ENV (provider-neutral)."""
        env = os.environ.copy()
        for key in ("RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_NAME", "PRODUCTION"):
            env.pop(key, None)
        env["STRIKENOVA_ENV"] = "production"
        with patch.dict(os.environ, env, clear=True):
            s = Settings()
            assert s.IS_PRODUCTION is True


class TestFailClosedProductionDatabase:
    """Production database configuration must fail closed."""

    def test_production_missing_database_url_raises(self, caplog):
        with patch("app.db.settings") as mock_settings:
            mock_settings.IS_PRODUCTION = True
            mock_settings.DATABASE_URL = None
            with caplog.at_level(logging.WARNING, logger="app.db"):
                with pytest.raises(RuntimeError, match="production database configuration is required"):
                    validate_production_config()

    def test_production_sqlite_url_raises(self, caplog):
        with patch("app.db.settings") as mock_settings:
            mock_settings.IS_PRODUCTION = True
            mock_settings.DATABASE_URL = "sqlite:///production.db"
            with caplog.at_level(logging.WARNING, logger="app.db"):
                with pytest.raises(RuntimeError, match="production database configuration is required"):
                    validate_production_config()

    def test_production_sqlite_scheme_case_insensitive(self):
        """SQLITE://, SQLite://, and sqlite:// are all refused in production."""
        for url in ("SQLITE:///prod.db", "SQLite:///prod.db", "sqlite:///prod.db"):
            with patch("app.db.settings") as mock_settings:
                mock_settings.IS_PRODUCTION = True
                mock_settings.DATABASE_URL = url
                with pytest.raises(RuntimeError, match="points to SQLite"):
                    validate_production_config()

    def test_production_sqlite_memory_url_raises(self):
        with patch("app.db.settings") as mock_settings:
            mock_settings.IS_PRODUCTION = True
            mock_settings.DATABASE_URL = "sqlite://"
            with pytest.raises(RuntimeError, match="production database configuration is required"):
                validate_production_config()

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://user:pass@host:5432/db",
            "postgres://user:pass@host:5432/db",
            "postgresql+psycopg://user:pass@host:5432/db",
        ],
    )
    def test_production_postgresql_urls_accepted(self, url, caplog):
        with patch("app.db.settings") as mock_settings:
            mock_settings.IS_PRODUCTION = True
            mock_settings.DATABASE_URL = url
            with caplog.at_level(logging.WARNING, logger="app.db"):
                validate_production_config()  # must not raise
            assert not any("production database configuration is required" in r.message for r in caplog.records)

    def test_non_production_sqlite_still_accepted(self, caplog):
        with patch("app.db.settings") as mock_settings:
            mock_settings.IS_PRODUCTION = False
            mock_settings.DATABASE_URL = None
            with caplog.at_level(logging.WARNING, logger="app.db"):
                validate_production_config()  # intentional dev/test SQLite
            assert not any("production database configuration is required" in r.message for r in caplog.records)


    def test_guard_is_called_before_engine_construction(self):
        """ADR-014: the guard executes at import BEFORE ``engine = _engine()``.

        A malformed URL must surface the production-configuration RuntimeError
        rather than a SQLAlchemy dialect error from create_engine() (CodeRabbit
        review finding). Verified structurally on the live module source: a
        module reload here would rebind ``Base``/metadata and poison later
        tests, so ordering is asserted on source instead.
        """
        import app.db as db_module

        src = inspect.getsource(db_module)
        # rindex: the module-level call site (the def line matches earlier).
        guard_call = src.rindex("validate_production_config()")
        engine_assignment = src.index("engine = _engine()")
        assert guard_call < engine_assignment, (
            "validate_production_config() must run before engine construction "
            "so production misconfiguration fails closed with the correct error"
        )


class TestNoSecretLeakage:
    """Failure output must never embed the full connection string."""

    @pytest.mark.parametrize(
        "url",
        [
            "sqlite:///production.db?password=supersecret",
            "sqlite://",
        ],
    )
    def test_failure_messages_do_not_contain_connection_string(self, url, caplog):
        with patch("app.db.settings") as mock_settings:
            mock_settings.IS_PRODUCTION = True
            mock_settings.DATABASE_URL = url
            with caplog.at_level(logging.WARNING, logger="app.db"):
                with pytest.raises(RuntimeError) as excinfo:
                    validate_production_config()
            assert "supersecret" not in str(excinfo.value)
            assert "supersecret" not in caplog.text
