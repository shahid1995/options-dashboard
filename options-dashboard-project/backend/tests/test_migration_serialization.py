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
        crash, with a 1s floor so tiny TTLs still renew at a sane cadence."""
        import inspect

        src = inspect.getsource(mlock.LeaseRenewer._run)
        assert "max(1.0" in src, "renewal interval must be max(1.0, ttl/3)"

        for ttl, expected in ((30, 10.0), (120, 40.0), (2, 1.0)):
            renewer = mlock.LeaseRenewer("postgresql://x", "me", ttl)
            state = _fresh_state(held_by="me", owner="me")
            renewer._stop = MagicMock()
            renewer._stop.wait.side_effect = [False, True]  # single loop
            with patch.object(mlock, "_connect", return_value=_FakeConn(state)):
                renewer._run()
            actual = renewer._stop.wait.call_args_list[0].args[0]
            assert actual == expected, f"ttl={ttl}: expected wait({expected}), got {actual}"


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
            assert db_module._migration_lock_url() == str(db_module.engine.url)

    def test_runtime_identity_not_used_for_ddl_when_separated(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            assert db_module._migration_engine_url() != str(db_module.engine.url)


# ------------------------------------------------- config contract
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
