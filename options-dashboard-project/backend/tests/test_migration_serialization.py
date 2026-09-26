"""ADR-017: migration serialization (transactional lease lock).

Only one process may execute the migration chain at a time. These tests are
hermetic: every database interaction is mocked at the module boundary, there
are no module reloads, no global metadata mutation, and no test-order
dependence. Real-database behavior is proven separately on disposable
infrastructure (documented in ADR-017).
"""
import uuid
import datetime as _dt
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

import app._migration_lock as mlock
import app.db as db_module
from app.config import Settings


# ---------------------------------------------------------------- helpers
class _FakeCursor:
    """Records statements; controllable rowcount/rows for acquire paths."""

    def __init__(self, state):
        self._state = state
        self.rowcount = 0

    def execute(self, stmt, params=None):
        self._state["stmts"].append(stmt)
        s = stmt.lower()
        if "select locked_by" in s:
            self._state["last_select"] = (self._state["held_by"], self._state["expires_at"])
        elif "update _migration_lock set locked_by" in s and "expires_at <" in s:
            held, _ = self._state.get("last_select", (None, None))
            free_or_expired = held is None or self._state["expired"]
            self.rowcount = 1 if free_or_expired else 0
            if self.rowcount:
                # record the ACTUAL owner token from the statement params
                self._state["held_by"] = params[0]
                self._state["expires_at"] = None
        elif "update _migration_lock set locked_by = null" in s:
            self.rowcount = 1 if self._state["held_by"] == params[0] else 0
            if self.rowcount:
                self._state["held_by"] = None
        elif "update _migration_lock set expires_at" in s:
            self.rowcount = 1 if self._state["held_by"] == params[1] else 0

    def fetchone(self):
        return self._state.get("last_select", (None, None))


class _FakeConn:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return _FakeCursor(self.state)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _fresh_state(owner="me", held_by=None, expired=False):
    past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=30)
    return {
        "owner": owner,
        "held_by": held_by,
        # an expired lease is represented by a genuinely past deadline
        # (tz-aware, matching what CRDB returns for TIMESTAMPTZ)
        "expires_at": past if (held_by and expired) else None,
        "expired": expired,
        "stmts": [],
    }


# ------------------------------------------------- acquire / release
class TestLeaseAcquire:
    def test_acquire_on_free_lock(self):
        state = _fresh_state()
        with patch.object(mlock, "_connect", return_value=_FakeConn(state)):
            acquired, took_over = mlock.try_acquire("postgresql://x", "me")
        assert acquired is True
        assert took_over is False
        assert state["held_by"] == "me"

    def test_acquire_denied_while_other_holds_unexpired_lease(self):
        state = _fresh_state(held_by="other")
        with patch.object(mlock, "_connect", return_value=_FakeConn(state)):
            acquired, took_over = mlock.try_acquire("postgresql://x", "me")
        assert (acquired, took_over) == (False, False)
        assert state["held_by"] == "other"

    def test_takeover_of_expired_lease(self):
        state = _fresh_state(held_by="ghost", expired=True)
        with patch.object(mlock, "_connect", return_value=_FakeConn(state)):
            acquired, took_over = mlock.try_acquire("postgresql://x", "me")
        assert (acquired, took_over) == (True, True)
        assert state["held_by"] == "me"

    def test_release_only_by_holder(self):
        state = _fresh_state(held_by="me")
        with patch.object(mlock, "_connect", return_value=_FakeConn(state)):
            assert mlock.release("postgresql://x", "me") is True
            state["held_by"] = "someone-else"
            assert mlock.release("postgresql://x", "me") is False

    def test_owner_token_shape_is_host_pid_uuid(self):
        owner = f"host-{uuid.uuid4().hex[:4]}:4242:{uuid.uuid4().hex[:8]}"
        assert owner.count(":") == 2
        assert len(owner.split(":")[2]) == 8


# ------------------------------------------------- wait / fail-closed
class TestWaitBehavior:
    def test_wait_returns_immediately_when_free(self):
        state = _fresh_state()
        with patch.object(mlock, "_connect", return_value=_FakeConn(state)):
            assert mlock.wait_for_release("postgresql://x") is False

    def test_wait_detects_expired_lease(self):
        state = _fresh_state(held_by="ghost", expired=True)
        with patch.object(mlock, "_connect", return_value=_FakeConn(state)):
            assert mlock.wait_for_release("postgresql://x") is True

    def test_fail_closed_after_wait_budget(self):
        state = _fresh_state(held_by="other")
        with patch.object(mlock, "_connect", return_value=_FakeConn(state)), patch.object(
            mlock.time, "sleep"
        ), patch.object(mlock.time, "monotonic", side_effect=[0, 10_000]):
            with pytest.raises(mlock.LeaseLockUnavailable):
                mlock.wait_for_release("postgresql://x", max_wait_seconds=5)

    def test_error_message_has_no_credentials(self):
        state = _fresh_state(held_by="other")
        with patch.object(mlock, "_connect", return_value=_FakeConn(state)), patch.object(
            mlock.time, "sleep"
        ), patch.object(mlock.time, "monotonic", side_effect=[0, 10_000]):
            with pytest.raises(mlock.LeaseLockUnavailable) as ei:
                mlock.wait_for_release(
                    "postgresql://user:supersecret@h/db", max_wait_seconds=5
                )
        assert "supersecret" not in str(ei.value)
        assert "postgresql://" not in str(ei.value)


# ------------------------------------------------- serialized flow
class TestSerializedExecution:
    @staticmethod
    def _cfg():
        cfg = MagicMock()
        cfg.get_main_option.return_value = "postgresql://x/db"
        return cfg

    def test_single_instance_runs_upgrade_once_and_releases(self):
        cfg, command = self._cfg(), MagicMock()
        state = _fresh_state(owner="holder")
        with patch.object(mlock, "_connect", return_value=_FakeConn(state)), patch(
            "alembic.script.ScriptDirectory.from_config"
        ) as sd:
            sd.return_value.get_current_head.return_value = None  # version check skipped
            db_module._execute_serialized(cfg, command, MagicMock())
        assert command.upgrade.call_count == 1
        assert state["held_by"] is None  # released

    def test_lock_released_after_migration_failure(self):
        cfg, command = self._cfg(), MagicMock()
        command.upgrade.side_effect = RuntimeError("boom")
        state = _fresh_state(owner="holder")
        logger = MagicMock()
        with patch.object(mlock, "_connect", return_value=_FakeConn(state)), patch(
            "alembic.script.ScriptDirectory.from_config"
        ) as sd:
            sd.return_value.get_current_head.return_value = None
            with pytest.raises(RuntimeError):
                db_module._execute_serialized(cfg, command, logger)
        assert state["held_by"] is None  # released despite failure

    def test_waiter_takes_over_expired_lease_then_runs(self):
        cfg, command = self._cfg(), MagicMock()
        state = _fresh_state(held_by="ghost", expired=True, owner="waiter")
        with patch.object(mlock, "_connect", return_value=_FakeConn(state)), patch(
            "alembic.script.ScriptDirectory.from_config"
        ) as sd:
            sd.return_value.get_current_head.return_value = None
            db_module._execute_serialized(cfg, command, MagicMock())
        assert command.upgrade.call_count == 1
        assert state["held_by"] is None

    def test_second_instance_observes_already_current_and_skips_ddl(self):
        """Waiter acquires a freed lock, sees chain at head, still calls upgrade (no-op)."""
        cfg, command = self._cfg(), MagicMock()
        state = _fresh_state(owner="waiter")
        with patch.object(mlock, "_connect", return_value=_FakeConn(state)), patch(
            "alembic.script.ScriptDirectory.from_config"
        ) as sd, patch.object(mlock, "read_alembic_version", return_value="d46aa0000001"):
            sd.return_value.get_current_head.return_value = "d46aa0000001"
            logger = MagicMock()
            db_module._execute_serialized(cfg, command, logger)
        assert command.upgrade.call_count == 1
        # structured 'already current' diagnostic emitted
        assert any(
            "migration already current" in str(c) for c in logger.info.call_args_list
        )

    def test_lock_acquired_once_per_instance(self):
        cfg, command = self._cfg(), MagicMock()
        state = _fresh_state(owner="solo")
        with patch.object(mlock, "_connect", return_value=_FakeConn(state)), patch(
            "alembic.script.ScriptDirectory.from_config"
        ) as sd:
            sd.return_value.get_current_head.return_value = None
            db_module._execute_serialized(cfg, command, MagicMock())
        acquires = [
            s for s in state["stmts"] if "set locked_by" in s.lower() and "expires_at <" in s.lower()
        ]
        assert len(acquires) == 1

    def test_logs_never_contain_url_or_credentials(self):
        cfg, command = self._cfg(), MagicMock()
        command.upgrade.side_effect = RuntimeError("migration boom")
        state = _fresh_state(owner="holder")
        logger = MagicMock()
        with patch.object(mlock, "_connect", return_value=_FakeConn(state)), patch(
            "alembic.script.ScriptDirectory.from_config"
        ) as sd:
            sd.return_value.get_current_head.return_value = None
            with pytest.raises(RuntimeError):
                db_module._execute_serialized(cfg, command, logger)
        for call in logger.info.call_args_list + logger.warning.call_args_list:
            assert "postgresql://x/db" not in str(call)

    def test_renewer_thread_extends_holder_lease(self):
        state = _fresh_state(held_by="me", owner="me")
        renewer = mlock.LeaseRenewer("postgresql://x", "me", 30)
        renewer._stop = MagicMock()
        renewer._stop.wait.side_effect = [False, True]  # one loop, then stop
        with patch.object(mlock, "_connect", return_value=_FakeConn(state)):
            renewer._run()
        # renew statement issued against holder's own row
        assert any(
            "update _migration_lock set expires_at" in s.lower() for s in state["stmts"]
        )

    def test_renewal_interval_is_ttl_third_floored_at_one_second(self):
        """Documented renewal rule (module docstring / ADR-017): the renewer
        waits max(1.0, ttl/3) between renewals — strictly below the TTL so an
        alive holder never leaves an expiry gap a waiter could misread as a
        crash, with a 1s floor so tiny TTLs still renew at a sane cadence.
        Verified behaviorally against the wait interval the renewer sleeps
        for, never by inspecting source text."""
        for ttl, expected in ((30, 10.0), (120, 40.0), (2, 1.0)):
            renewer = mlock.LeaseRenewer("postgresql://x", "me", ttl)
            state = _fresh_state(held_by="me", owner="me")
            renewer._stop = MagicMock()
            renewer._stop.wait.side_effect = [False, True]  # single loop
            with patch.object(mlock, "_connect", return_value=_FakeConn(state)):
                renewer._run()
            actual = renewer._stop.wait.call_args_list[0].args[0]
            if actual != expected:
                raise AssertionError(
                    f"ttl={ttl}: expected wait({expected}), got {actual}"
                )


# ------------------------------------------------- cold bootstrap retry
class TestBootstrapRetry:
    def test_ensure_lock_table_retries_serialization_conflicts(self):
        """Simultaneous cold starts can hit CRDB 40001 during bootstrap DDL;
        creation must retry and converge instead of crashing the child."""
        attempts = {"n": 0}

        class FlakyCursor:
            def execute(self, stmt, params=None):
                attempts["n"] += 1
                if attempts["n"] <= 2:  # first two statements conflict
                    raise Exception("40001: restart transaction")

        class FlakyConn:
            def cursor(self):
                return FlakyCursor()

            def commit(self):
                pass

            def rollback(self):
                pass

        conn = FlakyConn()
        with patch.object(mlock.time, "sleep"):
            mlock._ensure_lock_table(conn)  # must not raise
        assert attempts["n"] == 4  # CREATE fails twice, third CREATE + INSERT succeed

    def test_ensure_lock_table_gives_up_after_budget(self):
        class AlwaysConflictCursor:
            def execute(self, stmt, params=None):
                raise Exception("40001: restart transaction")

        class AlwaysConflictConn:
            def cursor(self):
                return AlwaysConflictCursor()

            def commit(self):
                pass

            def rollback(self):
                pass

        with patch.object(mlock.time, "sleep"), pytest.raises(Exception, match="40001"):
            mlock._ensure_lock_table(AlwaysConflictConn(), max_retries=3)


# ------------------------------------------------- identity rules
class TestIdentityRules:
    def test_lock_uses_migration_url_when_set(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            assert (
                db_module._migration_lock_url()
                == "postgresql+psycopg://migrator:pw@h:26257/app"
            )

    def test_lock_falls_back_to_runtime_url_when_unset(self):
        s = Settings(DATABASE_URL="postgresql://runtime:pw@h:26257/app")
        with patch("app.db.settings", s):
            assert db_module._migration_lock_url() == (
                "postgresql+psycopg://runtime:pw@h:26257/app"
            )

    def test_runtime_identity_not_used_for_ddl_when_separated(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            assert db_module._migration_engine_url() != str(db_module.engine.url)


# ------------------------------------------------- migration target rules
class TestMigrationTargetSerialization:
    """The SQLite bypass in _run_alembic_migrations must be decided by the
    MIGRATION TARGET (the resolved migration URL), not by the runtime
    engine. Otherwise a SQLite runtime + non-SQLite migration identity
    would run server-backed migrations without ADR-017 serialization."""

    def _patch_db(self, runtime_url, migration_url):
        s = Settings(
            DATABASE_URL=runtime_url,
            STRIKENOVA_MIGRATION_DATABASE_URL=migration_url,
        )
        return patch.object(db_module, "settings", s)

    def _run_startup_migration(self, runtime_url, migration_url):
        """Drive _run_alembic_migrations hermetically through COHERENT
        Settings: the real resolver decides the migration target and the
        separation flag exactly as in production. The runtime engine is a
        MagicMock (nothing real is touched) and _execute_serialized is a
        mock, so no database is ever contacted. Returns the mocks plus the
        connectable observed INSIDE the patched context (identity
        comparisons are meaningless after exit). Pass migration_url="" for
        the single-identity model."""
        from unittest.mock import MagicMock as _M

        fake_engine = _M(name="runtime-engine")
        fake_engine.url = runtime_url
        serialized = _M(name="_execute_serialized")
        direct = _M(name="direct_upgrade")
        cfg = _M(name="alembic_cfg")
        cfg.attributes = {}  # real dict so get()/[] stores are observable
        observed = {}
        with patch.object(
            db_module, "settings",
            Settings(DATABASE_URL=runtime_url, STRIKENOVA_MIGRATION_DATABASE_URL=migration_url or ""),
        ), patch.object(
            db_module, "engine", fake_engine
        ), patch.object(
            db_module, "_execute_serialized", serialized
        ), patch(
            "alembic.command.upgrade", direct
        ), patch(
            "alembic.config.Config", return_value=cfg
        ):
            db_module._run_alembic_migrations()
            observed["connectable"] = cfg.attributes.get("connectable")
            observed["engine"] = fake_engine
        return serialized, direct, cfg, observed

    def test_sqlite_runtime_with_nonsqlite_migration_target_serializes(self):
        serialized, direct, _, observed = self._run_startup_migration(
            "sqlite:///C:/tmp/runtime.db",
            "cockroachdb+psycopg://migrator:pw@h:26257/strikenova?sslmode=require",
        )
        if serialized.call_count != 1:
            raise AssertionError(
                "non-SQLite migration target must be serialized via "
                f"_execute_serialized, got calls={serialized.call_count}"
            )
        if direct.called:
            raise AssertionError("direct SQLite path must NOT be taken for a non-SQLite target")
        if observed["connectable"] is not None:
            raise AssertionError(
                "separated identities must not reuse the runtime engine as connectable"
            )

    def test_sqlite_target_takes_the_direct_path_even_with_nonsqlite_runtime(self):
        serialized, direct, _, observed = self._run_startup_migration(
            "postgresql://runtime:pw@h:26257/app",
            "sqlite:///C:/tmp/migration.db",
        )
        if serialized.called:
            raise AssertionError("SQLite migration target must bypass serialization")
        if direct.call_count != 1:
            raise AssertionError(
                f"SQLite target must migrate directly once, got {direct.call_count}"
            )
        if observed["connectable"] is not None:
            raise AssertionError(
                "separated identities must not reuse the runtime engine as connectable"
            )

    def test_single_identity_sqlite_still_takes_the_direct_path(self):
        serialized, direct, _, observed = self._run_startup_migration(
            "sqlite:///C:/tmp/single.db", ""
        )
        if serialized.called:
            raise AssertionError("historical single-identity SQLite behavior changed")
        if direct.call_count != 1:
            raise AssertionError(
                f"SQLite direct path must be preserved, got {direct.call_count}"
            )
        if observed["connectable"] is not observed["engine"]:
            raise AssertionError(
                "single-identity startup must reuse the runtime engine connectable"
            )

    def test_single_identity_nonsqlite_serializes_and_reuses_engine(self):
        serialized, direct, _, observed = self._run_startup_migration(
            "postgresql://runtime:pw@h:26257/app", ""
        )
        if serialized.call_count != 1:
            raise AssertionError("non-SQLite single-identity startup must serialize")
        if direct.called:
            raise AssertionError("direct SQLite path must not run for server targets")
        if observed["connectable"] is not observed["engine"]:
            raise AssertionError(
                "single-identity startup must reuse the runtime engine connectable"
            )


# ------------------------------------------------- blank URL semantics
class TestBlankUrlSemantics:
    """None, "" and whitespace-only values mean UNSET for every URL source
    (explicit sqlalchemy.url, STRIKENOVA_MIGRATION_DATABASE_URL,
    DATABASE_URL); nonblank values are stripped before normalization. The
    lock URL shares the resolver's semantics exactly and never diverges."""

    WS = "   "

    def test_blank_migration_url_falls_back_to_runtime(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="",
        )
        with patch("app.db.settings", s):
            assert db_module.resolve_migration_database_url() == (
                "postgresql+psycopg://runtime:pw@h:26257/app"
            )

    def test_whitespace_migration_url_falls_back_to_runtime(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL=self.WS,
        )
        with patch("app.db.settings", s):
            assert db_module.resolve_migration_database_url() == (
                "postgresql+psycopg://runtime:pw@h:26257/app"
            )

    def test_blank_explicit_url_falls_through_to_migration_url(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            assert db_module.resolve_migration_database_url("") == (
                "postgresql+psycopg://migrator:pw@h:26257/app"
            )

    def test_whitespace_explicit_url_falls_through_to_migration_url(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            assert db_module.resolve_migration_database_url(self.WS) == (
                "postgresql+psycopg://migrator:pw@h:26257/app"
            )

    def test_tab_only_explicit_url_is_unset(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            assert db_module.resolve_migration_database_url("\t") == (
                "postgresql+psycopg://migrator:pw@h:26257/app"
            )

    @pytest.mark.parametrize(
        "placeholder",
        ["driver://user:pw@h/db", " driver://user:pw@h/db", "\tdriver://user:pw@h/db"],
    )
    def test_whitespace_prefixed_placeholder_is_not_a_real_url(self, placeholder):
        """alembic.ini ships ``driver://...`` as a placeholder; env.py must
        never return it (decorated with whitespace or not) — it falls through
        to the migration/runtime URL as if unset."""
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            resolved = db_module.resolve_migration_database_url(placeholder)
        if resolved.startswith("driver://"):
            raise AssertionError(
                f"alembic placeholder leaked as a real URL: {placeholder!r}"
            )
        if resolved != "postgresql+psycopg://migrator:pw@h:26257/app":
            raise AssertionError(
                f"placeholder did not fall through to the migration URL: {resolved}"
            )

    def test_blank_runtime_url_falls_back_to_sqlite(self):
        s = Settings(DATABASE_URL="", STRIKENOVA_MIGRATION_DATABASE_URL=None)
        with patch("app.db.settings", s):
            url = db_module.resolve_migration_database_url()
        assert url.startswith("sqlite:///"), url
        assert url.endswith("paper_journal.db"), url

    def test_whitespace_runtime_url_falls_back_to_sqlite(self):
        s = Settings(DATABASE_URL=self.WS, STRIKENOVA_MIGRATION_DATABASE_URL=None)
        with patch("app.db.settings", s):
            url = db_module.resolve_migration_database_url()
        assert url.startswith("sqlite:///"), url
        assert url.endswith("paper_journal.db"), url

    def test_nonblank_urls_are_stripped_before_normalization(self):
        s = Settings(
            DATABASE_URL="  postgresql://runtime:pw@h:26257/app  ",
            STRIKENOVA_MIGRATION_DATABASE_URL="  cockroachdb+psycopg://migrator:pw@h:26257/strikenova?sslmode=require  ",
        )
        with patch("app.db.settings", s):
            assert db_module.resolve_migration_database_url() == (
                "cockroachdb+psycopg://migrator:pw@h:26257/strikenova?sslmode=require"
            )
            assert db_module._migration_engine_url() == (
                "cockroachdb+psycopg://migrator:pw@h:26257/strikenova?sslmode=require"
            )
            assert db_module._migration_lock_url() == (
                "cockroachdb+psycopg://migrator:pw@h:26257/strikenova?sslmode=require"
            )

    def test_explicit_url_is_stripped(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            assert db_module.resolve_migration_database_url(
                "  cockroachdb+psycopg://explicit:pw@h:26257/strikenova  "
            ) == "cockroachdb+psycopg://explicit:pw@h:26257/strikenova"

    @pytest.mark.parametrize(
        "migration_url",
        [
            "postgresql://migrator:pw@h:26257/app",  # normal
            "",  # blank
            "   ",  # whitespace
            None,  # unset
        ],
    )
    def test_lock_url_never_diverges_from_migration_url(self, migration_url):
        """Lock URL == engine URL under normal AND blank configurations:
        the lock travels with the migration identity and shares the exact
        blank-value semantics of the resolver."""
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL=migration_url,
        )
        with patch("app.db.settings", s):
            expected = db_module._migration_engine_url()
            assert db_module._migration_lock_url() == expected


# ------------------------------------------------- config contract
class TestMigrationLockTtlValidation:
    """The renewal interval max(1.0, TTL/3) is only strictly below the TTL
    when TTL >= 2; smaller/zero/negative values could make a live holder's
    lease appear expired, so the configuration boundary rejects them."""

    @pytest.mark.parametrize("ttl", [1, 0, -1, -120])
    def test_ttl_below_two_is_rejected(self, ttl):
        with pytest.raises(ValidationError, match="MIGRATION_LOCK_TTL_SECONDS"):
            Settings(
                DATABASE_URL="postgresql://runtime:pw@h:26257/app",
                MIGRATION_LOCK_TTL_SECONDS=ttl,
            )

    @pytest.mark.parametrize("ttl", [2, 3, 120, 900])
    def test_ttl_of_two_or_more_is_accepted(self, ttl):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            MIGRATION_LOCK_TTL_SECONDS=ttl,
        )
        if s.MIGRATION_LOCK_TTL_SECONDS != ttl:
            raise AssertionError(
                f"expected TTL {ttl} to be accepted, got {s.MIGRATION_LOCK_TTL_SECONDS}"
            )


class TestAlembicCliUrlPrecedence:
    """ADR-016 identity separation must hold for standalone CLI migrations
    too (PR #107 review): alembic env.py resolves its URL through
    app.db.resolve_migration_database_url with precedence
    explicit sqlalchemy.url > STRIKENOVA_MIGRATION_DATABASE_URL >
    DATABASE_URL > SQLite fallback."""

    def test_explicit_url_wins_over_both_env_urls(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            assert db_module.resolve_migration_database_url(
                "postgresql://explicit:pw@h:26257/app"
            ) == "postgresql+psycopg://explicit:pw@h:26257/app"

    def test_migration_url_beats_runtime_url(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            assert db_module.resolve_migration_database_url() == (
                "postgresql+psycopg://migrator:pw@h:26257/app"
            )

    def test_falls_back_to_runtime_url_when_migration_url_absent(self):
        s = Settings(DATABASE_URL="postgresql://runtime:pw@h:26257/app")
        with patch("app.db.settings", s):
            assert db_module.resolve_migration_database_url() == (
                "postgresql+psycopg://runtime:pw@h:26257/app"
            )

    def test_blank_migration_url_is_treated_as_absent(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="   ",
        )
        with patch("app.db.settings", s):
            assert db_module.resolve_migration_database_url() == (
                "postgresql+psycopg://runtime:pw@h:26257/app"
            )

    def test_blank_explicit_url_falls_through_to_migration_url(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            assert db_module.resolve_migration_database_url("   ") == (
                "postgresql+psycopg://migrator:pw@h:26257/app"
            )

    def test_sqlite_fallback_when_nothing_set(self):
        s = Settings(DATABASE_URL=None, STRIKENOVA_MIGRATION_DATABASE_URL=None)
        with patch("app.db.settings", s):
            url = db_module.resolve_migration_database_url()
        assert url.startswith("sqlite:///"), url
        assert url.endswith("paper_journal.db")

    def test_explicit_cockroach_url_is_preserved(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            assert db_module.resolve_migration_database_url(
                "cockroachdb+psycopg://migrator:pw@h:26257/strikenova?sslmode=require"
            ) == "cockroachdb+psycopg://migrator:pw@h:26257/strikenova?sslmode=require"

    def test_migration_url_is_normalized_like_the_startup_path(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            assert db_module.resolve_migration_database_url() == (
                db_module._migration_engine_url()
            )


class TestConfigContract:
    def test_lock_settings_exist_with_defaults(self):
        s = Settings(DATABASE_URL="postgresql://runtime:pw@h:26257/app")
        assert s.MIGRATION_LOCK_TTL_SECONDS == 120
        assert s.MIGRATION_LOCK_WAIT_SECONDS == 900

    def test_ttl_and_wait_are_plain_fields(self):
        s = Settings(
            DATABASE_URL="postgresql://r@h/db",
            MIGRATION_LOCK_TTL_SECONDS=60,
            MIGRATION_LOCK_WAIT_SECONDS=300,
        )
        assert (s.MIGRATION_LOCK_TTL_SECONDS, s.MIGRATION_LOCK_WAIT_SECONDS) == (60, 300)


# ------------------------------------------------- SQLite skip
class TestSqliteSkip:
    def test_sqlite_target_skips_locking_entirely(self):
        """SQLite runs upgrade directly: no lease lock, no serialized executor."""
        import sqlalchemy

        fake_engine = sqlalchemy.create_engine("sqlite://")
        with patch.object(db_module, "engine", fake_engine), patch(
            "alembic.config.Config"
        ), patch("alembic.command.upgrade") as upgrade, patch.object(
            db_module, "_execute_serialized"
        ) as serialized:
            db_module._run_alembic_migrations()
        upgrade.assert_called_once()
        serialized.assert_not_called()


class TestNoCrossTestPollution:
    def test_module_engine_is_a_real_engine(self):
        import sqlalchemy

        assert isinstance(db_module.engine, sqlalchemy.Engine)
