"""ADR-017 migration lease — DB-free contract tests.

The behavioural proof against PostgreSQL 16.16 lives in
``test_issue145_application_integration.py``. These tests pin the properties
that must hold without a database: token handling, SQL shape, SQLSTATE retry
classification, configuration validation, fail-closed identity rules and the
retirement of the renewal-thread design.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

import app.db as db_module
from app import _migration_lease as lease
from app.config import Settings


class _FakeOrig(Exception):
    def __init__(self, sqlstate):
        super().__init__("boom")
        self.sqlstate = sqlstate


class _FakeDBAPIError(Exception):
    def __init__(self, sqlstate):
        super().__init__("wrapped")
        self.orig = _FakeOrig(sqlstate)


class TestOwnerCredentials:
    def test_token_is_256_bits_of_hex(self):
        token = lease.generate_token()
        assert len(token) == 64
        int(token, 16)
        assert token != lease.generate_token()

    def test_fingerprint_is_the_first_12_hex_of_the_hash(self):
        token = lease.generate_token()
        assert lease.fingerprint(token) == lease.token_hash(token)[:12]
        assert len(lease.fingerprint(token)) == 12

    def test_token_hash_matches_documented_sha256(self):
        import hashlib

        token = "a" * 64
        assert lease.token_hash(token) == hashlib.sha256(token.encode()).hexdigest()

    def test_lease_object_never_exposes_the_token(self):
        guard = lease.MigrationLease(object(), token="b" * 64)  # type: ignore[arg-type]
        assert "b" * 64 not in repr(guard)
        assert guard.fingerprint == lease.fingerprint("b" * 64)


class TestProtocolSql:
    def test_functions_are_security_definer_with_fixed_search_path(self):
        for sql in lease.FUNCTION_SQL:
            assert "SECURITY DEFINER" in sql
            assert "SET search_path = pg_catalog" in sql

    def test_every_worker_function_takes_the_cross_version_guard(self):
        for sql in (lease.ACQUIRE_SQL, lease.ENTER_SQL, lease.RELEASE_SQL):
            assert "hashtextextended('strikenova:migration_internal:migration_guard', 0)" in sql
            assert "pg_advisory_xact_lock" in sql
            assert sql.index("migration_guard") < sql.index("FOR UPDATE")

    def test_no_function_reads_an_ownership_guc(self):
        for sql in lease.FUNCTION_SQL:
            lowered = sql.lower()
            assert "app.migration" not in lowered
            assert "strikenova.lease_token" not in lowered
            assert "session_authorization" not in lowered
            assert lowered.count("current_setting(") == lowered.count(
                "current_setting('lock_timeout')"
            )

    def test_enter_checks_readiness_and_refreshes_expiry(self):
        sql = lease.ENTER_SQL
        assert "v_boot <> 'ready'" in sql
        assert "45005" in sql
        assert "expires_at = v_exp" in sql

    def test_acquire_is_the_only_ownership_transfer(self):
        assert "owner_token_hash  = v_hash" in lease.ACQUIRE_SQL
        assert "owner_token_hash  = v_hash" not in lease.ENTER_SQL
        assert lease.RELEASE_SQL.index(
            "owner_token_hash <> v_hash"
        ) < lease.RELEASE_SQL.index("owner_token_hash  = NULL")

    def test_release_is_not_gated_on_barrier_or_readiness(self):
        assert "barrier_state" not in lease.RELEASE_SQL
        assert "bootstrap_state" not in lease.RELEASE_SQL

    def test_revoke_set_covers_public_and_runtime_role(self):
        assert len(lease.REVOKE_SQL) == 8
        assert any("FROM PUBLIC" in s for s in lease.REVOKE_SQL)
        assert any("{runtime_role}" in s for s in lease.REVOKE_SQL)

    def test_protocol_objects_are_not_version_suffixed(self):
        assert lease.PROTOCOL_TABLE == "migration_internal.lease_protocol"
        assert lease.LEASE_TABLE == "migration_internal.migration_lease"


class TestSqlstateHandling:
    @pytest.mark.parametrize("state", ["45002", "55P03", "40001", "40P01"])
    def test_transient_states_are_retryable(self, state):
        assert state in lease.RETRYABLE_SQLSTATES

    @pytest.mark.parametrize("state", ["45001", "45004", "45005", "45006"])
    def test_terminal_states_are_not_retryable(self, state):
        assert state not in lease.RETRYABLE_SQLSTATES

    def test_sqlstate_is_read_from_the_driver_exception(self):
        assert lease.sqlstate_of(_FakeDBAPIError("45002")) == "45002"
        assert lease.sqlstate_of(RuntimeError("no state")) is None


class TestConfigurationValidation:
    @pytest.mark.parametrize("ttl", [0, -1, 86400.5])
    def test_invalid_lease_ttl_is_rejected(self, ttl):
        with pytest.raises(ValidationError, match="MIGRATION_LEASE_TTL_SECONDS"):
            Settings(DATABASE_URL="postgresql://r@h/db", MIGRATION_LEASE_TTL_SECONDS=ttl)

    @pytest.mark.parametrize("lock_timeout", [0, -1, 600001])
    def test_invalid_lock_timeout_is_rejected(self, lock_timeout):
        with pytest.raises(ValidationError, match="MIGRATION_LEASE_LOCK_TIMEOUT_MS"):
            Settings(
                DATABASE_URL="postgresql://r@h/db",
                MIGRATION_LEASE_LOCK_TIMEOUT_MS=lock_timeout,
            )

    def test_defaults_are_bounded_and_consistent(self):
        s = Settings(DATABASE_URL="postgresql://r@h/db")
        assert s.MIGRATION_LEASE_TTL_SECONDS == 300
        assert s.MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS == 150000
        assert 1 <= s.MIGRATION_LEASE_LOCK_TIMEOUT_MS <= 600000
        assert s.MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS < (
            s.MIGRATION_LEASE_TTL_SECONDS * 1000
        )

    def test_statement_timeout_below_the_ttl_is_accepted(self):
        s = Settings(
            DATABASE_URL="postgresql://r@h/db",
            MIGRATION_LEASE_TTL_SECONDS=300,
            MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS=299999,
        )
        assert s.MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS == 299999

    def test_statement_timeout_equal_to_the_ttl_is_rejected(self):
        with pytest.raises(ValidationError, match="strictly below"):
            Settings(
                DATABASE_URL="postgresql://r@h/db",
                MIGRATION_LEASE_TTL_SECONDS=300,
                MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS=300000,
            )

    def test_statement_timeout_above_the_ttl_is_rejected(self):
        with pytest.raises(ValidationError, match="strictly below"):
            Settings(
                DATABASE_URL="postgresql://r@h/db",
                MIGRATION_LEASE_TTL_SECONDS=300,
                MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS=300001,
            )

    @pytest.mark.parametrize("timeout", [0, -1, -150000])
    def test_non_positive_statement_timeout_is_rejected(self, timeout):
        with pytest.raises(
            ValidationError, match="MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS must be positive"
        ):
            Settings(
                DATABASE_URL="postgresql://r@h/db",
                MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS=timeout,
            )

    @pytest.mark.parametrize("ttl", [0, -1, 0.0005])
    def test_non_positive_ttl_is_rejected_by_the_cross_field_rule_too(self, ttl):
        with pytest.raises(ValidationError):
            Settings(DATABASE_URL="postgresql://r@h/db", MIGRATION_LEASE_TTL_SECONDS=ttl)


class TestFailClosedIdentityRules:
    RUNTIME = "postgresql://runtime:pw@h:5432/app"
    MIGRATOR = "postgresql://migrator:pw@h:5432/app"

    def test_postgres_runtime_requires_a_migration_url(self, monkeypatch):
        monkeypatch.setattr(db_module, "settings", Settings(DATABASE_URL=self.RUNTIME))
        with pytest.raises(lease.LeaseError) as exc:
            db_module.assert_guarded_migration_configuration()
        assert exc.value.sqlstate == "45004"

    def test_equal_identities_are_refused(self, monkeypatch):
        monkeypatch.setattr(
            db_module,
            "settings",
            Settings(
                DATABASE_URL=self.RUNTIME,
                STRIKENOVA_MIGRATION_DATABASE_URL=self.RUNTIME,
            ),
        )
        with pytest.raises(lease.LeaseError) as exc:
            db_module.assert_guarded_migration_configuration()
        assert exc.value.sqlstate == "45004"

    def test_separated_identities_are_accepted(self, monkeypatch):
        monkeypatch.setattr(
            db_module,
            "settings",
            Settings(
                DATABASE_URL=self.RUNTIME,
                STRIKENOVA_MIGRATION_DATABASE_URL=self.MIGRATOR,
            ),
        )
        url = db_module.assert_guarded_migration_configuration()
        assert url.startswith("postgresql+psycopg://migrator")

    def test_sqlite_needs_no_separate_identity(self, monkeypatch):
        monkeypatch.setattr(
            db_module, "settings", Settings(DATABASE_URL="sqlite:///local.db")
        )
        assert db_module.assert_guarded_migration_configuration().startswith("sqlite")


class TestRetiredDesign:
    def test_renewal_thread_module_is_gone(self):
        with pytest.raises(ImportError):
            import app._migration_lock  # noqa: F401

    def test_no_renewer_thread_or_legacy_api_remains(self):
        source = inspect.getsource(db_module)
        for forbidden in (
            "LeaseRenewer",
            "try_acquire",
            "wait_for_release",
            "_execute_serialized",
            "MIGRATION_LOCK_TTL_SECONDS",
        ):
            assert forbidden not in source

    def test_backfill_does_not_commit_inside_the_per_user_helper(self):
        from app.services import leg_exposure

        source = inspect.getsource(leg_exposure.backfill_exposures)
        assert "db.commit()" not in source
        assert "db.flush()" in source

    def test_sqlite_startup_still_runs_migrations_and_backfill(self, hermetic_init_db):
        hermetic_init_db()
        from sqlalchemy import text

        with db_module.engine.connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            }
        assert {
            "positions",
            "paper_orders",
            "strategy_leg_exposures",
            "alembic_version",
        } <= tables

    def test_sqlite_never_touches_the_postgres_guarded_path(
        self, hermetic_init_db, monkeypatch
    ):
        calls = []

        def forbidden(*args, **kwargs):  # pragma: no cover - must not run
            calls.append(args)
            raise AssertionError("SQLite startup must not enter the lease path")

        monkeypatch.setattr(db_module, "_run_guarded_migrations", forbidden)
        hermetic_init_db()
        assert calls == []
