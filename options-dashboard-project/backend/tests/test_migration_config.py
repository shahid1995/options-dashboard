"""DB-free tests for the ADR-017/018 migration configuration contract.

These tests exercise ``app.config.Settings`` only. Construction never reads the
developer's real ``.env``, never inherits ambient configuration, and never
opens a database connection.
"""

import math
import os
import subprocess
import sys
import unittest.mock as mock
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

BACKEND_DIR = Path(__file__).resolve().parents[1]

#: Every environment variable that can populate a ``Settings`` field. Derived
#: from the live model so a new field is isolated automatically.
_SETTINGS_ENV_KEYS = frozenset(name.upper() for name in Settings.model_fields)

#: Environment variables that ``Settings`` does not declare but whose behaviour
#: reads them directly: the legacy production markers used by
#: ``IS_PRODUCTION`` and the host variable used for redirect-URI derivation.
_EXTERNAL_ENV_KEYS = frozenset(
    {
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_SERVICE_NAME",
        "RAILWAY_PUBLIC_DOMAIN",
        "PRODUCTION",
    }
)

_ISOLATED_ENV_KEYS = _SETTINGS_ENV_KEYS | _EXTERNAL_ENV_KEYS


def _is_settings_key(key: str) -> bool:
    """True when ``key`` would populate a Settings field (case-insensitive)."""
    return key.upper() in _SETTINGS_ENV_KEYS


def _is_isolated_key(key: str) -> bool:
    return key.upper() in _ISOLATED_ENV_KEYS


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch):
    """Remove every ambient variable this contract can observe."""
    for key in list(os.environ):
        if _is_isolated_key(key):
            monkeypatch.delenv(key, raising=False)


def make_settings(**overrides) -> Settings:
    """Construct Settings with no .env file and no inherited field values.

    ``_env_file=None`` disables the dotenv source. The environment is also
    stripped of every Settings field name for the duration of the construction,
    so a value exported by the developer's shell (``FRONTEND_URL``, ``DEBUG``,
    ``CANDLE_INTERVAL``, ...) cannot influence the result. Variables that are
    not Settings fields are left untouched: a test that deliberately sets a
    legacy marker must still be able to observe it through ``IS_PRODUCTION``.
    """
    env = {key: value for key, value in os.environ.items() if not _is_settings_key(key)}
    with mock.patch.dict(os.environ, env, clear=True):
        return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------------------
# Isolation coverage
# ---------------------------------------------------------------------------


def test_isolation_set_is_derived_from_the_settings_model():
    assert _SETTINGS_ENV_KEYS == frozenset(Settings.model_fields)
    assert {"FRONTEND_URL", "DEBUG", "CANDLE_INTERVAL", "DATABASE_URL"} <= _SETTINGS_ENV_KEYS
    # IS_PRODUCTION is a property computed from the environment, not a field.
    assert "IS_PRODUCTION" not in _SETTINGS_ENV_KEYS
    assert {"RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_NAME", "PRODUCTION"} <= _ISOLATED_ENV_KEYS


def test_every_inherited_settings_field_is_ignored(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.setenv(name, "contaminated")
    monkeypatch.setenv("STRIKENOVA_ENV", "production")

    settings = make_settings()

    assert settings.FRONTEND_URL == "http://localhost:3000"
    assert settings.DEBUG is False
    assert settings.CANDLE_INTERVAL == "3min"
    assert settings.TOKEN_ENCRYPTION_KEY == ""
    assert settings.STRIKENOVA_ENV == ""
    assert settings.MIGRATION_LEASE_TTL_SECONDS == 300.0
    assert settings.IS_PRODUCTION is False


def test_unrelated_inherited_settings_cannot_contaminate_defaults(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://inherited.example")
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("CANDLE_INTERVAL", "1min")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://inherited@example.invalid/db")

    settings = make_settings()

    assert settings.FRONTEND_URL == "http://localhost:3000"
    assert settings.FRONTEND_ORIGIN == "http://localhost:3000"
    assert settings.DEBUG is False
    assert settings.CANDLE_INTERVAL == "3min"
    assert settings.DATABASE_URL is None


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


# ---------------------------------------------------------------------------
# Lease wait budget: finite and strictly positive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wait",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative-int"),
        pytest.param(-0.5, id="negative-float"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="pos-inf"),
        pytest.param(float("-inf"), id="neg-inf"),
        pytest.param("nan", id="nan-string"),
        pytest.param("inf", id="pos-inf-string"),
        pytest.param("-inf", id="neg-inf-string"),
    ],
)
def test_lease_wait_must_be_finite_and_positive(wait):
    with pytest.raises(ValidationError):
        make_settings(MIGRATION_LEASE_WAIT_SECONDS=wait)


@pytest.mark.parametrize("wait", [0.001, 0.5, 1, 60, 180, 3600, 86400])
def test_lease_wait_accepts_ordinary_positive_values(wait):
    settings = make_settings(MIGRATION_LEASE_WAIT_SECONDS=wait)
    assert settings.MIGRATION_LEASE_WAIT_SECONDS == wait
    assert math.isfinite(settings.MIGRATION_LEASE_WAIT_SECONDS)


def test_lease_wait_default_matches_approved_policy():
    assert make_settings().MIGRATION_LEASE_WAIT_SECONDS == 180.0


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
# Production detection: STRIKENOVA_ENV is authoritative when set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["production", "Production", " PRODUCTION ", "\tProduction\n"]
)
def test_strikenova_env_production_is_detected(value):
    settings = make_settings(STRIKENOVA_ENV=value)
    assert settings.IS_PRODUCTION is True


@pytest.mark.parametrize(
    "value", ["", "   ", "staging", "preview", "development", "production-eu"]
)
def test_non_production_selector_is_not_production(value):
    settings = make_settings(STRIKENOVA_ENV=value)
    assert settings.IS_PRODUCTION is False


@pytest.mark.parametrize("marker", ["RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_NAME", "PRODUCTION"])
def test_empty_selector_falls_back_to_legacy_markers(marker, monkeypatch):
    monkeypatch.setenv(marker, "production")

    settings = make_settings(STRIKENOVA_ENV="")
    assert settings.IS_PRODUCTION is True

    # Whitespace-only is empty for precedence purposes, so the fallback applies.
    blank = make_settings(STRIKENOVA_ENV="   ")
    assert blank.IS_PRODUCTION is True


@pytest.mark.parametrize(
    "selector", ["staging", "Staging", "preview", " PREVIEW ", "development"]
)
@pytest.mark.parametrize("marker", ["RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_NAME", "PRODUCTION"])
def test_explicit_non_production_selector_overrides_legacy_marker(
    selector, marker, monkeypatch
):
    monkeypatch.setenv(marker, "production")

    settings = make_settings(STRIKENOVA_ENV=selector)
    assert settings.IS_PRODUCTION is False


def test_empty_selector_without_legacy_markers_is_not_production():
    settings = make_settings(STRIKENOVA_ENV="")
    assert settings.IS_PRODUCTION is False


# ---------------------------------------------------------------------------
# No database access at import time
# ---------------------------------------------------------------------------


def test_importing_config_does_not_load_a_database_driver(tmp_path):
    env = {key: value for key, value in os.environ.items() if not _is_isolated_key(key)}
    env["PYTHONPATH"] = str(BACKEND_DIR)

    script = "\n".join(
        [
            "import sys",
            "import app.config",
            "cfg = app.config.settings",
            "assert cfg.FRONTEND_URL == 'http://localhost:3000', cfg.FRONTEND_URL",
            "assert cfg.DEBUG is False, cfg.DEBUG",
            "assert cfg.IS_PRODUCTION is False, cfg.IS_PRODUCTION",
            "assert 'sqlalchemy' not in sys.modules, 'sqlalchemy imported'",
            "assert 'psycopg' not in sys.modules, 'psycopg imported'",
            "print('no-database-import-ok')",
        ]
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        # An empty working directory, so no stray .env can be picked up.
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "no-database-import-ok" in result.stdout
