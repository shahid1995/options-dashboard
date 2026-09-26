"""ADR-017 migration rehearsal against a REAL PostgreSQL server.

Runs only when ``TEST_DATABASE_URL`` points at a disposable PostgreSQL
database (in CI: the ``postgres:16`` service container; locally: any
scratch PostgreSQL). Skipped otherwise, so local/hermetic runs are
unaffected. This file exercises the PRODUCTION implementations —
``app.db.resolve_migration_database_url``, ``app._migration_lock`` (the
same module ``_execute_serialized`` uses) and ``app.db._migration_lock_url``
— with no reimplementation of resolver, normalization, or lock logic.

Bandit/Codacy note: checks use explicit ``raise AssertionError`` so the
verification survives ``python -O`` (no reliance on plain ``assert``).
"""
from __future__ import annotations

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app import _migration_lock as mlock
from app.db import normalize_database_url, resolve_migration_database_url

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _postgres_test_url() -> str:
    raw = os.getenv("TEST_DATABASE_URL")
    if not raw:
        pytest.skip("TEST_DATABASE_URL is not configured (rehearsal needs a real disposable PostgreSQL)")
    url = normalize_database_url(raw)
    if not url.startswith("postgresql+psycopg://"):
        pytest.fail("TEST_DATABASE_URL must resolve to the psycopg 3 SQLAlchemy dialect")
    return url


def _make_alembic_cfg(url: str) -> Config:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class TestPostgresMigrationRehearsal:
    """The same target identity ``_run_alembic_migrations()`` serializes."""

    @pytest.fixture()
    def migration_url(self):
        return _postgres_test_url()

    def test_migration_url_beats_runtime_url_on_postgres(self, migration_url, monkeypatch):
        """Identity separation on the REAL server: the resolver must return
        the migration identity when separated, never the runtime URL."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://runtime_user:pw@127.0.0.1:1/other")
        monkeypatch.setenv("STRIKENOVA_MIGRATION_DATABASE_URL", migration_url)
        from app.config import Settings

        s = Settings()
        from unittest.mock import patch

        with patch("app.db.settings", s):
            resolved = resolve_migration_database_url()
        _check(
            resolved == migration_url,
            "resolver must select the migration identity, not the runtime URL",
        )

    def test_full_real_lock_lifecycle(self, migration_url):
        """acquire -> persisted ownership -> renew -> concurrent block ->
        release -> re-acquire, against real PostgreSQL via the production
        lock module (no mocks)."""
        owner = f"rehearsal:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        ttl = 120
        engine = create_engine(migration_url, pool_pre_ping=True)
        try:
            acquired, took_over = mlock.try_acquire(migration_url, owner, ttl)
            _check(acquired, "lock acquisition against real PostgreSQL must succeed")
            _check(not took_over, "a fresh database must not report a lease takeover")
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT locked_by, acquired_at, expires_at FROM _migration_lock")
                ).fetchone()
            _check(row is not None, "lock row must exist while held")
            _check(row[0] == owner, f"ownership must be persisted for the holder, got {row[0]!r}")
            _check(row[1] is not None and row[2] is not None, "lease timestamps must be persisted")

            holder = mlock.LeaseRenewer(migration_url, owner, ttl)
            holder.start()
            try:
                other = f"second:{os.getpid()}:{uuid.uuid4().hex[:8]}"
                acquired_2, took_over_2 = mlock.try_acquire(migration_url, other, ttl)
                _check(
                    not acquired_2 and not took_over_2,
                    "a second live-lease acquisition must be rejected while held",
                )
                with engine.connect() as conn:
                    still = conn.execute(text("SELECT locked_by FROM _migration_lock")).scalar_one()
                _check(
                    still == owner,
                    "the holder must retain ownership after a rejected second acquirer",
                )
            finally:
                holder.stop()

            released = mlock.release(migration_url, owner)
            _check(released, "the holder must be able to release its own lease")
            with engine.connect() as conn:
                cleared = conn.execute(text("SELECT locked_by FROM _migration_lock")).scalar_one()
            _check(cleared is None, "lock row must be cleared after release")

            again_owner = f"reacquire:{os.getpid()}:{uuid.uuid4().hex[:8]}"
            re_acquired, re_took_over = mlock.try_acquire(migration_url, again_owner, ttl)
            _check(re_acquired, "the lock must be acquirable again after release")
            _check(not re_took_over, "re-acquisition of a free lock is not a takeover")
            _check(mlock.release(migration_url, again_owner), "cleanup release must succeed")
        finally:
            engine.dispose()

    def test_waiter_fails_closed_while_holder_renews_then_proceeds_after_release(
        self, migration_url
    ):
        """ADR-017 wait semantics against real PostgreSQL: a waiter whose
        budget is exhausted by a LIVE (renewing) holder fails closed with
        LeaseLockUnavailable; after the holder releases, the lock is free
        (wait_for_release -> False) and acquirable again."""
        holder_owner = f"rehearsal-holder:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        ttl = 2  # minimum: renewal interval max(1.0, ttl/3) -> ~1s
        first = mlock.try_acquire(migration_url, holder_owner, ttl)
        _check(first[0], "holder must acquire the lease")
        holder = mlock.LeaseRenewer(migration_url, holder_owner, ttl)
        holder.start()
        try:
            # While the holder renews, a short waiter budget must fail closed.
            with pytest.raises(mlock.LeaseLockUnavailable):
                mlock.wait_for_release(
                    migration_url, ttl_seconds=ttl, max_wait_seconds=1
                )
            with create_engine(migration_url, pool_pre_ping=True).connect() as conn:
                still = conn.execute(text("SELECT locked_by FROM _migration_lock")).scalar_one()
            _check(
                still == holder_owner,
                "the renewing holder must retain ownership after a failed-closed waiter",
            )
        finally:
            holder.stop()
        _check(
            mlock.release(migration_url, holder_owner),
            "holder release must succeed after renewal stops",
        )
        # Lock free within budget -> wait_for_release returns False (not a
        # takeover signal); the next acquisition then succeeds.
        waited_after = mlock.wait_for_release(
            migration_url, ttl_seconds=ttl, max_wait_seconds=30
        )
        _check(waited_after is False, "a released lock must be observed as free")
        _check(
            mlock.try_acquire(migration_url, f"after:{os.getpid()}:{uuid.uuid4().hex[:8]}", ttl)[0],
            "lock must be acquirable after the waiter observed release",
        )
        # leave the lock free for the other tests
        mlock.release(migration_url, f"after:{os.getpid()}:{uuid.uuid4().hex[:8]}")

    def test_alembic_chain_applies_on_postgres_through_resolver_target(
        self, migration_url, monkeypatch
    ):
        """The full Alembic chain applies on the REAL server using the same
        resolver-selected migration target the startup path serializes; the
        resolver's decision must be identical to the startup path's."""
        monkeypatch.setenv("DATABASE_URL", migration_url)
        monkeypatch.setenv("STRIKENOVA_MIGRATION_DATABASE_URL", migration_url)
        from unittest.mock import patch

        from app.config import Settings

        s = Settings()
        with patch("app.db.settings", s):
            resolved = resolve_migration_database_url()
        _check(resolved == migration_url, "resolver must resolve the CI rehearsal target")
        cfg = _make_alembic_cfg(resolved)
        command.upgrade(cfg, "head")
        engine = create_engine(resolved, pool_pre_ping=True)
        try:
            with engine.connect() as conn:
                version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            _check(bool(version), f"alembic_version must be stamped (got {version!r})")
            from app import db as db_module

            with patch("app.db.settings", s):
                startup_target = db_module._migration_engine_url()
            _check(
                startup_target == resolved,
                "startup migration target and rehearsal target must be identical",
            )
        finally:
            engine.dispose()
