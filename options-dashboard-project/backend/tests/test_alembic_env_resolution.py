"""PR #107 review: alembic/env.py must resolve its URL through the shared
migration resolver (``app.db.resolve_migration_database_url``), preserving

    explicit sqlalchemy.url
        > STRIKENOVA_MIGRATION_DATABASE_URL
        > DATABASE_URL
        > SQLite fallback

end-to-end through ``alembic/env.py::_resolve_database_url()``.

Hermetic: the real env.py file is executed under an OFFLINE alembic
EnvironmentContext (as_sql=True, empty migration fn), so no connection is
ever opened and no real migration runs. The URL env.py resolves is captured
at the ``MigrationContext.configure`` seam — the exact call env.py's
``context.configure(url=...)`` reaches. Delegation is proven with a spy
resolver; precedence is proven end-to-end through the real resolver. The
tests re-implement neither precedence nor normalization.
"""
from __future__ import annotations

import io
import os
import runpy
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic.config import Config
from alembic.runtime import migration as alembic_migration
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory

BACKEND_DIR = Path(__file__).resolve().parents[1]
ENV_PY = BACKEND_DIR / "alembic" / "env.py"


def _env_cfg(sqlalchemy_url: str | None) -> Config:
    """A minimal Config pointing at the real alembic/ directory."""
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    if sqlalchemy_url is not None:
        cfg.set_main_option("sqlalchemy.url", sqlalchemy_url)
    return cfg


def _exec_env_py_offline(cfg, settings_obj, spy=None) -> str:
    """Execute the real alembic/env.py under an offline EnvironmentContext.

    Returns the URL env.py passed to ``context.configure()`` (captured at
    MigrationContext.configure). The proxy is installed before execution so
    env.py's module-level tail (``run_migrations_offline()``) runs inside
    the fake environment; the spy, when given, replaces app.db's shared
    resolver so delegation is observable.
    """
    script = ScriptDirectory.from_config(cfg)
    # as_sql=True == offline mode (alembic's is_offline_mode() reads
    # context_opts["as_sql"]). The empty fn mirrors what the alembic CLI
    # supplies: run_migrations() iterates zero steps, so no migration SQL
    # is generated or executed.
    env_ctx = EnvironmentContext(cfg, script, fn=lambda rev, ctx: [], as_sql=True)

    real_configure = alembic_migration.MigrationContext.configure  # cls-bound
    captured: dict = {}

    def capturing_configure(*args, **kwargs):
        captured["url"] = kwargs.get("url")
        return real_configure(*args, **kwargs)

    with env_ctx:  # installs the alembic.context proxy
        patches = [patch("app.db.settings", settings_obj)]
        if spy is not None:
            patches.append(
                patch(
                    "app.db.resolve_migration_database_url",
                    side_effect=spy,
                )
            )
        else:
            patches.append(patch.dict(os.environ, {}))
        try:
            with patches[0], patches[1]:
                buf = io.StringIO()
                with redirect_stdout(buf), patch.object(
                    alembic_migration.MigrationContext,
                    "configure",
                    capturing_configure,
                ):
                    runpy.run_path(str(ENV_PY), run_name="alembic_env_under_test")
        finally:
            pass
    if "url" not in captured:
        raise AssertionError("env.py never called context.configure()")
    return captured["url"]


class TestEnvPyDelegatesToSharedResolver:
    @pytest.fixture(autouse=True)
    def _no_alembic_env_vars(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("STRIKENOVA_MIGRATION_DATABASE_URL", raising=False)

    def test_env_py_offline_mode_runs_and_delegates(self):
        from app.config import Settings

        calls = []

        def spy(explicit_url=None):
            calls.append(explicit_url)
            return "sqlite:///C:/tmp/resolved_ver107.db"

        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        cfg = _env_cfg("driver://user:pw@h/db")
        url = _exec_env_py_offline(cfg, s, spy=spy)
        if not calls:
            raise AssertionError(
                "env.py did not call the shared migration resolver"
            )
        if calls[0] != "driver://user:pw@h/db":
            raise AssertionError(
                "env.py must pass the ini sqlalchemy.url to the shared "
                f"resolver, got explicit={calls[0]!r}"
            )
        if url != "sqlite:///C:/tmp/resolved_ver107.db":
            raise AssertionError(
                f"env.py must use the shared resolver's return value, got {url}"
            )

    def test_env_py_placeholder_ini_falls_through_to_migration_url(self):
        from app.config import Settings

        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        cfg = _env_cfg("  driver://placeholder  ")  # placeholder -> unset
        url = _exec_env_py_offline(cfg, s)
        if url != "postgresql+psycopg://migrator:pw@h:26257/app":
            raise AssertionError(
                f"expected migration identity through env.py, got {url}"
            )

    def test_env_py_explicit_ini_url_beats_everything(self):
        from app.config import Settings

        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        cfg = _env_cfg("postgresql://explicit:pw@h:26257/app")
        url = _exec_env_py_offline(cfg, s)
        if url != "postgresql+psycopg://explicit:pw@h:26257/app":
            raise AssertionError(
                f"expected explicit ini URL to win through env.py, got {url}"
            )
