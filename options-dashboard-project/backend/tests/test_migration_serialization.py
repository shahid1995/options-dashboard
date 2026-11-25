"""ADR-017 migration lease — DB-free contract tests.

The behavioural proof against PostgreSQL 16.16 lives in
``test_issue145_application_integration.py``. These tests pin the properties
that must hold without a database: token handling, SQL shape, SQLSTATE retry
classification, configuration validation, fail-closed identity rules and the
retirement of the renewal-thread design.
"""

from __future__ import annotations

import inspect
import logging
import pathlib
import subprocess
import sys
from typing import ClassVar
from unittest.mock import patch

import pytest
from pydantic import ValidationError

import app.db as db_module
from app import _migration_lease as lease
from app.config import Settings
from tests import issue145_pg_support as support


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
    # ADR-018 §8.1 names both identities, so the URL logins must be exactly
    # these roles and not merely different from each other.
    ROLES = {"STRIKENOVA_MIGRATOR_ROLE": "migrator", "STRIKENOVA_RUNTIME_ROLE": "runtime"}

    def test_postgres_runtime_requires_a_migration_url(self, monkeypatch):
        monkeypatch.setattr(
            db_module,
            "settings",
            Settings(DATABASE_URL=self.RUNTIME, **self.ROLES),
        )
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
                **self.ROLES,
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
                **self.ROLES,
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


class TestPytestGate:
    """The CI gate over both pytest logs must fail closed (ADR-017 completion).

    These are deterministic negative controls over synthetic logs: no real test
    is skipped, altered or re-run to produce them.
    """

    SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "pytest_gate.py"

    def _run(self, tmp_path, log_text, *, rc="0", minimum=22, log_name="suite.log"):
        log = tmp_path / log_name
        if log_text is not None:
            log.write_text(log_text, encoding="utf-8")
        rc_file = tmp_path / "suite.rc"
        if rc is not None:
            rc_file.write_text(rc, encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                str(self.SCRIPT),
                "--suite",
                f"unit|{minimum}|{log}|{rc_file}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_real_summary_passes(self, tmp_path):
        result = self._run(tmp_path, "22 passed, 1 warning in 29.34s\n")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "unit: 22 passed" in result.stdout

    def test_long_run_summary_with_clock_suffix_passes(self, tmp_path):
        """pytest adds ``(0:01:10)`` after a minute; the gate must accept it."""
        result = self._run(tmp_path, "22 passed, 1 warning in 70.36s (0:01:10)\n")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "unit: 22 passed" in result.stdout

    def test_all_skipped_log_fails(self, tmp_path):
        result = self._run(tmp_path, "22 skipped, 3 warnings in 4.10s\n")
        assert result.returncode == 1
        assert "skipped" in result.stdout

    def test_missing_summary_fails(self, tmp_path):
        result = self._run(tmp_path, "collected 0 items\n\nno tests ran in 0.01s\n")
        assert result.returncode == 1
        assert "no authoritative pytest summary line" in result.stdout

    def test_untrusted_output_cannot_inject_failure_counts(self, tmp_path):
        """A test printing '999999 failed' must not fabricate a gate failure."""
        log = (
            "FAILED tests/x.py::test_y - assertion\n"
            "999999 failed\n"
            "1 error\n"
            "22 passed, 1 warning in 9.00s\n"
        )
        result = self._run(tmp_path, log)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "unit: 22 passed" in result.stdout

    def test_last_authoritative_summary_wins(self, tmp_path):
        """Only pytest's final summary line decides, not an earlier paste."""
        log = "999 failed in 0.01s\n22 passed in 9.00s\n"
        result = self._run(tmp_path, log)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "unit: 22 passed" in result.stdout

    def test_summary_without_duration_is_not_trusted(self, tmp_path):
        result = self._run(tmp_path, "22 passed\n")
        assert result.returncode == 1
        assert "no authoritative pytest summary line" in result.stdout

    def test_error_status_in_summary_fails(self, tmp_path):
        result = self._run(tmp_path, "1 error, 21 passed in 12.0s\n")
        assert result.returncode == 1
        assert "failed/errored" in result.stdout

    def test_failed_test_fails_even_with_passes(self, tmp_path):
        result = self._run(tmp_path, "21 passed, 1 failed in 12.0s\n")
        assert result.returncode == 1
        assert "failed" in result.stdout

    def test_non_zero_exit_code_fails(self, tmp_path):
        result = self._run(tmp_path, "22 passed in 12.0s\n", rc="1")
        assert result.returncode == 1
        assert "pytest exited 1" in result.stdout

    def test_missing_exit_code_fails(self, tmp_path):
        result = self._run(tmp_path, "22 passed in 12.0s\n", rc=None)
        assert result.returncode == 1
        assert "no recorded pytest exit code" in result.stdout

    def test_low_pass_count_fails(self, tmp_path):
        result = self._run(tmp_path, "21 passed in 12.0s\n", minimum=22)
        assert result.returncode == 1
        assert "only 21 test(s) passed" in result.stdout

    def test_missing_log_fails(self, tmp_path):
        result = self._run(tmp_path, None)
        assert result.returncode == 1
        assert "no pytest log" in result.stdout

    def test_regression_suite_is_gated_independently(self, tmp_path):
        """A green integration log must not mask a broken regression log."""
        good = tmp_path / "integration.log"
        good.write_text("22 passed in 20.0s\n", encoding="utf-8")
        good_rc = tmp_path / "integration.rc"
        good_rc.write_text("0", encoding="utf-8")
        bad = tmp_path / "regression.log"
        bad.write_text("135 skipped in 20.0s\n", encoding="utf-8")
        bad_rc = tmp_path / "regression.rc"
        bad_rc.write_text("0", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(self.SCRIPT),
                "--suite",
                f"integration|22|{good}|{good_rc}",
                "--suite",
                f"regression|141|{bad}|{bad_rc}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 1
        assert "integration: 22 passed" in result.stdout
        assert "regression: 135 skipped" in result.stdout


class TestLeaseWaitValidation:
    """The admission budget must be a finite positive duration."""

    @pytest.mark.parametrize("wait", [float("nan"), float("inf"), 0, -1, 86400.5])
    def test_invalid_wait_is_rejected(self, wait):
        with pytest.raises(ValidationError, match="MIGRATION_LEASE_WAIT_SECONDS"):
            Settings(
                DATABASE_URL="postgresql://r@h/db",
                MIGRATION_LEASE_WAIT_SECONDS=wait,
            )

    def test_explicit_lease_waits_are_also_validated(self):
        for wait in (float("nan"), float("inf"), 0.0, -1.0):
            with pytest.raises(lease.LeaseError) as exc:
                lease.MigrationLease(object(), wait_seconds=wait)  # type: ignore[arg-type]
            assert exc.value.sqlstate == "45004"
        with pytest.raises(lease.LeaseError) as exc:
            lease.MigrationLease(object(), ttl_seconds=float("nan"))  # type: ignore[arg-type]
        assert exc.value.sqlstate == "45004"


class TestAcquisitionBudget:
    def test_an_exhausted_budget_starts_no_attempt(self, monkeypatch):
        class _NeverConnects:
            calls = 0

            def connect(self):  # pragma: no cover - must never run
                self.calls += 1
                raise AssertionError("acquire() connected after the deadline")

        engine = _NeverConnects()
        guard = lease.MigrationLease(engine, wait_seconds=0.05)  # type: ignore[arg-type]
        clock = iter([0.0, 10.0])  # deadline is 0.05s; the next read is 10s
        monkeypatch.setattr(lease.time, "monotonic", lambda: next(clock))
        with pytest.raises(lease.LeaseError) as exc:
            guard.acquire()
        assert exc.value.sqlstate == "45002"
        assert "wait budget was already exhausted" in str(exc.value)
        assert engine.calls == 0


class TestGuardedMigrationCleanup:
    @staticmethod
    def _engine(disposed):
        class _Engine:
            def dispose(self):
                disposed.append(True)

        return _Engine()

    def test_preflight_failure_disposes_without_release(self, monkeypatch):
        disposed: list[bool] = []
        released: list[bool] = []
        monkeypatch.setattr(
            db_module, "_migration_engine", lambda: self._engine(disposed)
        )
        monkeypatch.setattr(
            lease,
            "preflight",
            lambda engine: (_ for _ in ()).throw(
                lease.LeaseError("45005", "protocol row missing")
            ),
        )
        monkeypatch.setattr(
            lease.MigrationLease,
            "release",
            lambda self: released.append(True),
        )
        with pytest.raises(lease.LeaseError):
            db_module._run_guarded_migrations(object(), object(), logging.getLogger("t"))
        assert disposed == [True]
        assert released == []

    def test_acquisition_failure_disposes_without_release(self, monkeypatch):
        disposed: list[bool] = []
        released: list[bool] = []
        monkeypatch.setattr(
            db_module, "_migration_engine", lambda: self._engine(disposed)
        )
        monkeypatch.setattr(lease, "preflight", lambda engine: None)
        monkeypatch.setattr(
            lease.MigrationLease,
            "acquire",
            lambda self: (_ for _ in ()).throw(
                lease.LeaseError("45002", "lease held by another owner")
            ),
        )
        monkeypatch.setattr(
            lease.MigrationLease,
            "release",
            lambda self: released.append(True),
        )
        with pytest.raises(lease.LeaseError):
            db_module._run_guarded_migrations(object(), object(), logging.getLogger("t"))
        assert disposed == [True]
        assert released == []


class TestSqliteMigrationTarget:
    def test_a_separate_sqlite_migration_url_is_refused(self, monkeypatch, tmp_path):
        runtime = tmp_path / "runtime.db"
        other = tmp_path / "other.db"
        settings_obj = Settings(
            DATABASE_URL=f"sqlite:///{runtime}",
            STRIKENOVA_MIGRATION_DATABASE_URL=f"sqlite:///{other}",
        )
        with patch("app.db.settings", settings_obj), pytest.raises(
            lease.LeaseError
        ) as exc:
            db_module._run_alembic_migrations()
        assert exc.value.sqlstate == "45004"
        assert "separate SQLite migration URL is not supported" in str(exc.value)
        assert not other.exists(), "Alembic must not migrate a different SQLite file"

    def test_the_same_sqlite_target_is_accepted(self, monkeypatch, tmp_path):
        runtime = tmp_path / "runtime.db"
        settings_obj = Settings(
            DATABASE_URL=f"sqlite:///{runtime}",
            STRIKENOVA_MIGRATION_DATABASE_URL=f"sqlite:///{runtime}",
        )
        with patch("app.db.settings", settings_obj):
            resolved = db_module.assert_guarded_migration_configuration()
        assert resolved.startswith("sqlite:///")


class TestConnectionTargetOverrides:
    ROLES: ClassVar[dict[str, str]] = {
        "STRIKENOVA_MIGRATOR_ROLE": "migrator",
        "STRIKENOVA_RUNTIME_ROLE": "runtime",
    }

    def _assert_refused(self, runtime, migration, fragment):
        settings_obj = Settings(
            DATABASE_URL=runtime,
            STRIKENOVA_MIGRATION_DATABASE_URL=migration,
            **self.ROLES,
        )
        with patch("app.db.settings", settings_obj), pytest.raises(
            lease.LeaseError
        ) as exc:
            db_module.assert_guarded_migration_configuration()
        assert exc.value.sqlstate == "45004"
        assert fragment in str(exc.value)
        for secret in ("runtime-secret", "migrator-secret", "postgresql://"):
            assert secret not in str(exc.value)

    def test_query_user_override_is_refused(self):
        self._assert_refused(
            "postgresql://runtime:runtime-secret@db.example:5432/app?user=migrator",
            "postgresql://migrator:migrator-secret@db.example:5432/app",
            "connection query parameter",
        )

    def test_query_dbname_override_is_refused(self):
        self._assert_refused(
            "postgresql://runtime:runtime-secret@db.example:5432/app",
            "postgresql://migrator:migrator-secret@db.example:5432/app?dbname=elsewhere",
            "connection query parameter",
        )

    def test_multi_host_migration_url_is_refused(self):
        self._assert_refused(
            "postgresql://runtime:runtime-secret@db.example:5432/app",
            "postgresql://migrator:migrator-secret@db.example,other.example:5432/app",
            "names multiple hosts",
        )

    def test_unrelated_connection_options_are_preserved(self):
        settings_obj = Settings(
            DATABASE_URL=(
                "postgresql://runtime:runtime-secret@db.example:5432/app?sslmode=require"
            ),
            STRIKENOVA_MIGRATION_DATABASE_URL=(
                "postgresql://migrator:migrator-secret@db.example:5432/app"
                "?application_name=issue145"
            ),
            **self.ROLES,
        )
        with patch("app.db.settings", settings_obj):
            resolved = db_module.assert_guarded_migration_configuration()
        assert "sslmode=require" not in resolved  # migration URL keeps its own options
        assert "application_name=issue145" in resolved


class TestDisposableHarnessUrls:
    def test_admin_query_parameters_cannot_override_derived_targets(self):
        from sqlalchemy.engine import make_url

        admin = (
            "postgresql://admin:pw@db.example:5432/postgres"
            "?dbname=postgres&user=admin&sslmode=require"
        )
        prov = support.Provisioned(admin, "sn145_probe", support.TOPOLOGY_A)
        migrator = make_url(prov.migrator_url)
        assert migrator.database == "sn145_probe"
        assert migrator.username == prov.migrator_role
        assert migrator.password == prov.migrator_password
        assert migrator.query.get("sslmode") == "require"
        assert "dbname" not in migrator.query
        assert "user" not in migrator.query
        admin_db = make_url(prov.admin_database_url)
        assert admin_db.database == "sn145_probe"
        assert "dbname" not in admin_db.query
