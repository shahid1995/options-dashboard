"""DB-free tests for the ADR-017/018 migration configuration contract.

These tests exercise ``app.config.Settings`` only. They never read the
developer's real ``.env``, never inherit the ambient environment, and never
open a database connection.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch):
    """Detach the settings contract from the ambient environment and .env."""
    for key in list(os.environ):
        if key.startswith(
            (
                "STRIKENOVA_",
                "MIGRATION_",
                "RAILWAY_",
                "DATABASE_URL",
                "PRODUCTION",
            )
        ):
            monkeypatch.delenv(key, raising=False)


def make_settings(**overrides) -> Settings:
    """Construct Settings without reading any .env file."""
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------------------
# Defaults and accepted custom configuration
# ---------------------------------------------------------------------------


def test_default_configuration_constructs():
    settings = make_settings()

    assert settings.STRIKENOVA_ENV == ""
    assert settings.STRIKENOVA_MIGRATION_DATABASE_URL is None
    assert settings.STRIKENOVA_MIGRATION_TARGET_ALIASES == ""
    assert settings.MIGRATION_LEASE_TTL_SECONDS == 300.0
    assert settings.MIGRATION_LEASE_WAIT_SECONDS == 180.0
    assert settings.MIGRATION_LEASE_LOCK_TIMEOUT_MS == 10000
    assert settings.MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS == 150000
    assert settings.STRIKENOVA_MIGRATOR_ROLE == "strikenova_prod_migrator"
    assert settings.STRIKENOVA_RUNTIME_ROLE == "strikenova_production_app"
    assert settings.STRIKENOVA_OWNER_ROLE == ""
    assert settings.STRIKENOVA_LEASE_NONCE == ""
    assert settings.IS_PRODUCTION is False


def test_existing_settings_are_preserved():
    settings = make_settings()

    assert settings.DATABASE_URL is None
    assert settings.FRONTEND_URL == "http://localhost:3000"
    assert settings.FRONTEND_ORIGIN == "http://localhost:3000"
    assert settings.DEBUG is False
    assert settings.CANDLE_INTERVAL == "3min"


def test_valid_custom_configuration_is_accepted():
    settings = make_settings(
        STRIKENOVA_ENV="staging",
        STRIKENOVA_MIGRATION_DATABASE_URL=(
            "postgresql+psycopg://migrator@example.invalid/strikenova_staging"
        ),
        STRIKENOVA_MIGRATION_TARGET_ALIASES="pooled.example.invalid=direct.example.invalid",
        MIGRATION_LEASE_TTL_SECONDS=120,
        MIGRATION_LEASE_WAIT_SECONDS=60,
        MIGRATION_LEASE_LOCK_TIMEOUT_MS=2500,
        MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS=60000,
        STRIKENOVA_MIGRATOR_ROLE="strikenova_staging_migrator",
        STRIKENOVA_RUNTIME_ROLE="strikenova_staging_app",
        STRIKENOVA_OWNER_ROLE="strikenova_staging_owner",
        STRIKENOVA_LEASE_NONCE="rehearsal-nonce",
    )

    assert settings.STRIKENOVA_ENV == "staging"
    assert settings.MIGRATION_LEASE_TTL_SECONDS == 120
    assert settings.MIGRATION_LEASE_LOCK_TIMEOUT_MS == 2500
    assert settings.STRIKENOVA_OWNER_ROLE == "strikenova_staging_owner"


# ---------------------------------------------------------------------------
# Lease TTL bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ttl", [0, -1, -300.5, 86400.5, 90000])
def test_lease_ttl_out_of_bounds_is_rejected(ttl):
    with pytest.raises(ValidationError):
        make_settings(MIGRATION_LEASE_TTL_SECONDS=ttl)


@pytest.mark.parametrize("ttl", [0.01, 1, 86400])
def test_lease_ttl_boundary_values_are_accepted(ttl):
    settings = make_settings(
        MIGRATION_LEASE_TTL_SECONDS=ttl,
        MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS=1,
    )
    assert settings.MIGRATION_LEASE_TTL_SECONDS == ttl


def test_zero_lease_wait_is_rejected():
    with pytest.raises(ValidationError):
        make_settings(MIGRATION_LEASE_WAIT_SECONDS=0)

    with pytest.raises(ValidationError):
        make_settings(MIGRATION_LEASE_WAIT_SECONDS=-5)


# ---------------------------------------------------------------------------
# Lock timeout bounds (0 means "wait forever" in PostgreSQL)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lock_timeout", [0, -1, 600001, 1_000_000])
def test_lock_timeout_out_of_bounds_is_rejected(lock_timeout):
    with pytest.raises(ValidationError):
        make_settings(MIGRATION_LEASE_LOCK_TIMEOUT_MS=lock_timeout)


@pytest.mark.parametrize("lock_timeout", [1, 10000, 600000])
def test_lock_timeout_valid_range_is_accepted(lock_timeout):
    settings = make_settings(MIGRATION_LEASE_LOCK_TIMEOUT_MS=lock_timeout)
    assert settings.MIGRATION_LEASE_LOCK_TIMEOUT_MS == lock_timeout


# ---------------------------------------------------------------------------
# Backfill statement timeout: positive and strictly below the TTL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeout", [0, -1, -150000])
def test_non_positive_statement_timeout_is_rejected(timeout):
    with pytest.raises(ValidationError):
        make_settings(MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS=timeout)


def test_statement_timeout_equal_to_ttl_is_rejected():
    with pytest.raises(ValidationError):
        make_settings(
            MIGRATION_LEASE_TTL_SECONDS=300,
            MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS=300_000,
        )


def test_statement_timeout_greater_than_ttl_is_rejected():
    with pytest.raises(ValidationError):
        make_settings(
            MIGRATION_LEASE_TTL_SECONDS=60,
            MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS=60_001,
        )


def test_statement_timeout_below_ttl_is_accepted():
    settings = make_settings(
        MIGRATION_LEASE_TTL_SECONDS=300,
        MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS=299_999,
    )
    assert settings.MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS == 299_999


# ---------------------------------------------------------------------------
# Production detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["production", "Production", " PRODUCTION "])
def test_strikenova_env_production_is_detected(value, monkeypatch):
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_SERVICE_NAME", raising=False)
    monkeypatch.delenv("PRODUCTION", raising=False)

    settings = make_settings(STRIKENOVA_ENV=value)
    assert settings.IS_PRODUCTION is True


@pytest.mark.parametrize("value", ["", "staging", "preview", "development"])
def test_non_production_environment_is_not_production(value, monkeypatch):
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_SERVICE_NAME", raising=False)
    monkeypatch.delenv("PRODUCTION", raising=False)

    settings = make_settings(STRIKENOVA_ENV=value)
    assert settings.IS_PRODUCTION is False


def test_legacy_railway_marker_is_still_honoured(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    settings = make_settings(STRIKENOVA_ENV="")
    assert settings.IS_PRODUCTION is True


# ---------------------------------------------------------------------------
# No database access at import time
# ---------------------------------------------------------------------------


def test_importing_config_does_not_load_a_database_driver():
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("STRIKENOVA_", "MIGRATION_", "RAILWAY_", "PRODUCTION"))
        and key != "DATABASE_URL"
    }
    env["PYTHONPATH"] = str(BACKEND_DIR)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, app.config; "
            "assert 'sqlalchemy' not in sys.modules, 'sqlalchemy imported'; "
            "assert 'psycopg' not in sys.modules, 'psycopg imported'; "
            "print('no-database-import-ok')",
        ],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "no-database-import-ok" in result.stdout
