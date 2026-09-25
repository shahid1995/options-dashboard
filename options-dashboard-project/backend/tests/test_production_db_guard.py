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


class TestMalformedProductionSchemes:
    """Every invalid production scheme must fail through the application's
    production-configuration error contract — never with a raw SQLAlchemy
    dialect error from engine construction (PR #102 final hardening).
    """

    def test_unknown_scheme_raises_production_error(self, caplog):
        with patch("app.db.settings") as mock_settings:
            mock_settings.IS_PRODUCTION = True
            mock_settings.DATABASE_URL = "unknown://host/db"
            with caplog.at_level(logging.ERROR, logger="app.db"):
                with pytest.raises(RuntimeError) as excinfo:
                    validate_production_config()
        message = str(excinfo.value)
        assert "production database configuration is required" in message
        assert "unsupported scheme 'unknown'" in message

    def test_malformed_postgres_dialect_raises_production_error(self, caplog):
        with patch("app.db.settings") as mock_settings:
            mock_settings.IS_PRODUCTION = True
            mock_settings.DATABASE_URL = "postgres+nosuchdriver://u:p@h/db"
            with caplog.at_level(logging.ERROR, logger="app.db"):
                with pytest.raises(RuntimeError) as excinfo:
                    validate_production_config()
        message = str(excinfo.value)
        assert "production database configuration is required" in message
        assert "unsupported scheme 'postgres+nosuchdriver'" in message

    def test_malformed_scheme_failure_does_not_leak_credentials(self, caplog):
        secret = "supersecret"
        with patch("app.db.settings") as mock_settings:
            mock_settings.IS_PRODUCTION = True
            mock_settings.DATABASE_URL = f"unknown://dbuser:{secret}@db.example:26257/verdb"
            with caplog.at_level(logging.ERROR, logger="app.db"):
                with pytest.raises(RuntimeError) as excinfo:
                    validate_production_config()
        assert secret not in str(excinfo.value)
        assert secret not in caplog.text
        assert "db.example" not in str(excinfo.value)
        assert "db.example" not in caplog.text

    def test_supported_postgresql_forms_remain_allowed(self):
        """The allowlist must not reject any URL form production legitimately uses."""
        for url in ("postgres://u:p@h:26257/db?sslmode=require",
                    "postgresql://u:p@h:26257/db",
                    "postgresql+psycopg://u:p@h:26257/db"):
            with patch("app.db.settings") as mock_settings:
                mock_settings.IS_PRODUCTION = True
                mock_settings.DATABASE_URL = url
                validate_production_config()  # must not raise

    def test_supported_cockroachdb_forms_remain_allowed(self):
        """CockroachDB dialect schemes (sqlalchemy-cockroachdb) must pass.

        CockroachDB Cloud is the mandated production database; staging's live
        configuration uses ``cockroachdb+psycopg://``. The guard must accept
        the schemes the project's declared dependency provides, otherwise no
        URL can both pass the guard and boot against the production DB.
        """
        for url in ("cockroachdb+psycopg://u:p@h:26257/db?sslmode=require",
                    "cockroachdb://u:p@h:26257/db?sslmode=require"):
            with patch("app.db.settings") as mock_settings:
                mock_settings.IS_PRODUCTION = True
                mock_settings.DATABASE_URL = url
                validate_production_config()  # must not raise

    def test_unknown_scheme_fails_import_end_to_end(self):
        """End-to-end: importing the app in production with an unknown scheme
        fails with the application error, not a SQLAlchemy dialect error.
        Runs a fresh subprocess so real import-time behavior is observed.
        """
        import subprocess
        import sys as _sys

        backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = os.environ.copy()
        for key in ("STRIKENOVA_ENV", "RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_NAME",
                    "PRODUCTION", "DATABASE_URL"):
            env.pop(key, None)
        env["STRIKENOVA_ENV"] = "production"
        env["DATABASE_URL"] = "unknown://host/db"
        r = subprocess.run(
            [_sys.executable, "-c", "import app.db"], env=env,
            capture_output=True, text=True, cwd=backend_dir, timeout=120,
        )
        assert r.returncode != 0
        assert "production database configuration is required" in r.stderr
        assert "NoSuchModuleError" not in r.stderr


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
