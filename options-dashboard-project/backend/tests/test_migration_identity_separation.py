"""ADR-016: runtime/migration database identity separation.

When STRIKENOVA_MIGRATION_DATABASE_URL is set, Alembic must run under the
dedicated migration identity; otherwise behavior is identical to the
historical single-identity model. These tests are hermetic: no module
reloads, no shared-state mutation, no destructive operations.
"""
import os
from unittest.mock import patch

from app.config import Settings
from app.db import _migration_engine_url, assert_guarded_migration_configuration


class TestMigrationIdentityResolution:
    def test_default_uses_runtime_url_when_no_migration_url(self):
        s = Settings(DATABASE_URL="postgresql://runtime:pw@h:26257/app", STRIKENOVA_MIGRATION_DATABASE_URL=None)
        with patch("app.db.settings", s):
            # Fallback is the runtime identity URL (historical behavior) —
            # resolved from settings and normalized, exactly what the runtime
            # engine is built from in every real deployment.
            assert _migration_engine_url() == "postgresql+psycopg://runtime:pw@h:26257/app"

    def test_migration_url_takes_precedence_when_set(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            # Returned through normalize_database_url (ADR-014 dialect rules).
            assert _migration_engine_url() == "postgresql+psycopg://migrator:pw@h:26257/app"

    def test_migration_url_is_normalized_to_supported_dialect(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="cockroachdb+psycopg://migrator:pw@h:26257/app?sslmode=require",
        )
        with patch("app.db.settings", s):
            assert _migration_engine_url() == (
                "cockroachdb+psycopg://migrator:pw@h:26257/app?sslmode=require"
            )

    def test_postgres_scheme_migration_url_maps_to_psycopg(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            assert _migration_engine_url() == "postgresql+psycopg://migrator:pw@h:26257/app"

    def test_blank_migration_url_falls_back_to_runtime(self):
        s = Settings(DATABASE_URL="postgresql://runtime:pw@h:26257/app", STRIKENOVA_MIGRATION_DATABASE_URL="")
        with patch("app.db.settings", s):
            assert _migration_engine_url() == "postgresql+psycopg://runtime:pw@h:26257/app"


class TestGuardedTargetIdentity:
    """ADR-018 §8.1: one logical database, two distinct PostgreSQL logins.

    These tests are hermetic: they exercise the URL contract only and never
    open a connection, so no database of any kind is required or touched.
    """

    RUNTIME = "postgresql://strikenova_app:runtime-secret@db.example:5432/options"
    MIGRATOR = "postgresql://strikenova_migrator:migrator-secret@db.example:5432/options"

    def _settings(self, **overrides):
        values = {
            "DATABASE_URL": self.RUNTIME,
            "STRIKENOVA_MIGRATION_DATABASE_URL": self.MIGRATOR,
            # ADR-018 §8.1 names both identities: the URLs must connect as
            # exactly these roles, not merely as two different logins.
            "STRIKENOVA_MIGRATOR_ROLE": "strikenova_migrator",
            "STRIKENOVA_RUNTIME_ROLE": "strikenova_app",
        }
        values.update(overrides)
        return Settings(**values)

    def _assert_refused(self, settings_obj, *fragments):
        import pytest

        from app._migration_lease import LeaseError

        with patch("app.db.settings", settings_obj), pytest.raises(LeaseError) as exc:
            assert_guarded_migration_configuration()
        assert exc.value.sqlstate == "45004"
        message = str(exc.value)
        for secret in ("runtime-secret", "migrator-secret", "postgresql://"):
            assert secret not in message
        for fragment in fragments:
            assert fragment in message

    def test_same_database_distinct_roles_is_accepted(self):
        settings_obj = self._settings()
        with patch("app.db.settings", settings_obj):
            resolved = assert_guarded_migration_configuration()
        assert resolved.startswith("postgresql+psycopg://strikenova_migrator:")
        assert resolved.endswith("@db.example:5432/options")

    def test_migration_login_must_be_the_configured_migrator(self):
        """A login that merely differs from the runtime login is not enough."""
        self._assert_refused(
            self._settings(
                STRIKENOVA_MIGRATION_DATABASE_URL=(
                    "postgresql://postgres:migrator-secret@db.example:5432/options"
                )
            ),
            "not the configured migration identity 'strikenova_migrator'",
        )

    def test_runtime_login_must_be_the_configured_runtime_identity(self):
        self._assert_refused(
            self._settings(
                DATABASE_URL=(
                    "postgresql://someone_else:runtime-secret@db.example:5432/options"
                )
            ),
            "not the configured runtime identity 'strikenova_app'",
        )

    def test_unnamed_migration_role_is_refused(self):
        self._assert_refused(
            self._settings(STRIKENOVA_MIGRATOR_ROLE="   "),
            "STRIKENOVA_MIGRATOR_ROLE is not configured",
        )

    def test_unconfigured_runtime_role_does_not_constrain_the_runtime_login(self):
        settings_obj = self._settings(
            DATABASE_URL=(
                "postgresql://another_runtime:runtime-secret@db.example:5432/options"
            ),
            STRIKENOVA_RUNTIME_ROLE="",
        )
        with patch("app.db.settings", settings_obj):
            assert assert_guarded_migration_configuration()

    def test_same_login_with_a_different_password_is_refused(self):
        self._assert_refused(
            self._settings(
                STRIKENOVA_MIGRATION_DATABASE_URL=(
                    "postgresql://strikenova_app:another-secret@db.example:5432/options"
                )
            ),
            "same PostgreSQL login",
        )

    def test_different_database_name_is_refused(self):
        self._assert_refused(
            self._settings(
                STRIKENOVA_MIGRATION_DATABASE_URL=(
                    "postgresql://strikenova_migrator:migrator-secret@db.example:5432/other"
                )
            ),
            "different PostgreSQL databases",
        )

    def test_different_endpoint_is_refused_without_a_declared_alias(self):
        self._assert_refused(
            self._settings(
                STRIKENOVA_MIGRATION_DATABASE_URL=(
                    "postgresql://strikenova_migrator:migrator-secret@other.example:5432/options"
                )
            ),
            "different PostgreSQL endpoints",
        )

    def test_declared_alias_is_accepted_for_one_logical_endpoint(self):
        settings_obj = self._settings(
            STRIKENOVA_MIGRATION_DATABASE_URL=(
                "postgresql://strikenova_migrator:migrator-secret@direct.example:5432/options"
            ),
            STRIKENOVA_MIGRATION_TARGET_ALIASES="db.example=direct.example",
        )
        with patch("app.db.settings", settings_obj):
            assert assert_guarded_migration_configuration()

    def test_alias_does_not_override_a_different_port(self):
        settings_obj = self._settings(
            STRIKENOVA_MIGRATION_DATABASE_URL=(
                "postgresql://strikenova_migrator:migrator-secret@direct.example:6432/options"
            ),
            STRIKENOVA_MIGRATION_TARGET_ALIASES="db.example=direct.example",
        )
        self._assert_refused(settings_obj, "different PostgreSQL endpoints")

    def test_neon_pooler_route_needs_an_explicit_declaration(self):
        runtime = "postgresql://strikenova_app:pw@ep-cool-1-pooler.eu.aws.neon.tech/neondb"
        migrator = "postgresql://strikenova_migrator:pw@ep-cool-1.eu.aws.neon.tech/neondb"
        self._assert_refused(
            self._settings(
                DATABASE_URL=runtime, STRIKENOVA_MIGRATION_DATABASE_URL=migrator
            ),
            "different PostgreSQL endpoints",
        )
        declared = self._settings(
            DATABASE_URL=runtime,
            STRIKENOVA_MIGRATION_DATABASE_URL=migrator,
            STRIKENOVA_MIGRATION_TARGET_ALIASES=(
                "ep-cool-1-pooler.eu.aws.neon.tech=ep-cool-1.eu.aws.neon.tech"
            ),
        )
        with patch("app.db.settings", declared):
            assert assert_guarded_migration_configuration()

    def test_postgres_runtime_with_sqlite_migration_target_is_refused(self):
        self._assert_refused(
            self._settings(
                STRIKENOVA_MIGRATION_DATABASE_URL="sqlite:///./paper_journal.db"
            ),
            "sqlite",
        )

    def test_postgres_runtime_with_cockroachdb_migration_target_is_refused(self):
        self._assert_refused(
            self._settings(
                STRIKENOVA_MIGRATION_DATABASE_URL=(
                    "cockroachdb+psycopg://strikenova_migrator:pw@db.example:26257/options"
                )
            ),
            "cockroachdb",
        )

    def test_missing_blank_and_malformed_urls_fail_closed(self):
        self._assert_refused(
            self._settings(STRIKENOVA_MIGRATION_DATABASE_URL=None),
            "required",
        )
        self._assert_refused(
            self._settings(STRIKENOVA_MIGRATION_DATABASE_URL="   "),
            "required",
        )
        self._assert_refused(
            self._settings(
                STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://strikenova_migrator:pw@/options"
            ),
            "host",
        )
        self._assert_refused(
            self._settings(STRIKENOVA_MIGRATION_DATABASE_URL="this is not a url"),
        )

    def test_postgres_migration_url_without_a_postgres_runtime_is_refused(self):
        self._assert_refused(
            self._settings(DATABASE_URL="sqlite:///./paper_journal.db"),
            "no proven target",
        )

    def test_malformed_alias_configuration_fails_closed(self):
        self._assert_refused(
            self._settings(STRIKENOVA_MIGRATION_TARGET_ALIASES="db.example"),
            "hostA=hostB",
        )
        self._assert_refused(
            self._settings(STRIKENOVA_MIGRATION_TARGET_ALIASES="db.example=db.example"),
            "distinct",
        )


class TestSingleIdentityBackwardCompatibility:
    def test_alembic_source_still_present_and_ordered(self):
        import inspect

        import app.db as db_module

        src = inspect.getsource(db_module)
        assert "STRIKENOVA_MIGRATION_DATABASE_URL" in src
        # The migration runner must still call command.upgrade(cfg, "head").
        assert 'command.upgrade(alembic_cfg, "head")' in src
        # Guard still ordered before engine construction.
        guard = src.rindex("validate_production_config()")
        engine_line = src.index("engine = _engine()")
        assert guard < engine_line

    def test_alembic_authority_unchanged(self):
        """init_db still delegates schema management solely to Alembic."""
        import inspect

        import app.db as db_module

        src = inspect.getsource(db_module.init_db)
        assert "_run_alembic_migrations(" in src
