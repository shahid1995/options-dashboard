"""ADR-016: runtime/migration database identity separation.

When STRIKENOVA_MIGRATION_DATABASE_URL is set, Alembic must run under the
dedicated migration identity; otherwise behavior is identical to the
historical single-identity model. These tests are hermetic: no module
reloads, no shared-state mutation, no destructive operations.
"""
import os
from unittest.mock import patch

from app.config import Settings
from app.db import _migration_engine_url, normalize_database_url


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
        assert "_run_alembic_migrations()" in src
