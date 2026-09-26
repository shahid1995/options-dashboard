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
import signal
import subprocess
import sys
import textwrap
import time
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app import _migration_lock as mlock
from app.db import normalize_database_url, resolve_migration_database_url

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Cross-process actor for the concurrent rehearsal (Part 2): each invocation
# is a SEPARATE OS process with its own real PostgreSQL session. Synchronized
# via marker files in a temp dir (no fixed sleeps); every wait has a deadline
# so a deadlock can never hang CI. Roles:
#   holder: acquire -> signal -> keep renewing until waiter reports fail-closed
#           -> release -> signal
#   waiter: observe live holder (try_acquire rejected) -> wait_for_release
#           must raise LeaseLockUnavailable (fail closed) -> signal -> after
#           release, acquire -> signal (parent verifies persisted ownership)
#           -> release after parent's ack
_CONCURRENT_ACTOR = textwrap.dedent(
    """
    import os
    import sys
    import time
    import uuid

    sys.path.insert(0, os.environ["REHEARSAL_BACKEND"])
    from app import _migration_lock as mlock  # noqa: E402

    url = os.environ["REHEARSAL_DB_URL"]
    markers = os.environ["REHEARSAL_MARKERS"]
    ttl = int(os.environ.get("REHEARSAL_TTL", "15"))
    role = sys.argv[1]


    def path(name):
        return os.path.join(markers, name)


    def put(name, value="1"):
        # ATOMIC publication: write a same-directory temp file, flush+fsync,
        # then os.replace so readers only ever observe a COMPLETE marker
        # (a bare create-then-write could expose an empty/partial file).
        tmp = path(name) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(value)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path(name))
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


    def wait_for(name, deadline_seconds):
        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            if os.path.exists(path(name)):
                with open(path(name), encoding="utf-8") as f:
                    return f.read().strip()
            time.sleep(0.05)
        raise SystemExit(f"TIMEOUT waiting for marker {name}")


    try:
        if role == "holder":
            owner = "holder:" + uuid.uuid4().hex[:8]
            # Acquire a free lock OR take over a stale expired lease left by
            # an earlier test (ADR-017 takeover); retry bounded so a
            # still-live leftover lease (<= its TTL) can never fail the run.
            acquired = False
            for _attempt in range(60):
                acquired, _took_over = mlock.try_acquire(url, owner, ttl)
                if acquired:
                    break
                time.sleep(0.5)
            if not acquired:
                raise SystemExit(
                    "holder could not acquire the lock (free or via expired-lease takeover)"
                )
            put("holder_acquired", owner)
            renewer = mlock.LeaseRenewer(url, owner, ttl)
            renewer.start()
            try:
                wait_for("waiter_failed_closed", 90)
            finally:
                renewer.stop()
            if not mlock.release(url, owner):
                raise SystemExit("holder release failed")
            put("holder_released", owner)

        elif role == "waiter":
            wait_for("holder_acquired", 90)
            owner = "waiter:" + uuid.uuid4().hex[:8]
            acquired, took_over = mlock.try_acquire(url, owner, ttl)
            if acquired or took_over:
                raise SystemExit(
                    "waiter acquired/takeover while a live holder was renewing"
                )
            # Bounded wait budget: comfortably shorter than the TTL (which
            # the live holder keeps renewing), so exhaustion here is genuine
            # fail-closed behavior, never an expiry artifact.
            waiter_budget = max(2, ttl // 2)
            failed_closed = False
            for _attempt in range(3):
                try:
                    mlock.wait_for_release(
                        url, ttl_seconds=ttl, max_wait_seconds=waiter_budget
                    )
                    # Returned True = expiry observed. With a LIVE holder
                    # that is a renewal race, not a crashed holder: verify
                    # ownership is unchanged and retry the bounded wait.
                    if mlock.current_holder(url) is None:
                        raise SystemExit(
                            "lock became free while holder was renewing"
                        )
                except mlock.LeaseLockUnavailable:
                    failed_closed = True
                    break
            if not failed_closed:
                raise SystemExit(
                    "waiter did NOT fail closed against a live holder"
                )
            put("waiter_failed_closed", owner)
            wait_for("holder_released", 90)
            acquired_2, took_over_2 = mlock.try_acquire(url, owner, ttl)
            if not acquired_2 or took_over_2:
                raise SystemExit(
                    "waiter could not acquire after holder released"
                )
            put("waiter_acquired", owner)
            wait_for("parent_ack", 90)
            if not mlock.release(url, owner):
                raise SystemExit("waiter cleanup release failed")
            put("waiter_released", owner)
        else:
            raise SystemExit(f"unknown actor role: {role!r}")
    except BaseException as exc:  # noqa: BLE001 - surface the failure to the
        # parent via an ATOMIC error marker (stderr is DEVNULL by design so a
        # chatty child can never block on an undrained pipe).
        try:
            put(f"error_{role}", f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001 - best effort
            pass
        raise
    """
)


# Forced-termination actor for ADR-017 takeover rehearsal: a POSIX holder
# is SIGKILLed while actively renewing; the independent waiter is blocked
# before the kill, then must take over only after the lease actually expires.
_SIGKILL_ACTOR = textwrap.dedent(
    """
    import os
    import sys
    import time
    import uuid

    sys.path.insert(0, os.environ["REHEARSAL_BACKEND"])
    from app import _migration_lock as mlock  # noqa: E402

    url = os.environ["REHEARSAL_DB_URL"]
    markers = os.environ["REHEARSAL_MARKERS"]
    ttl = int(os.environ.get("REHEARSAL_TTL", "15"))
    role = sys.argv[1]


    def path(name):
        return os.path.join(markers, name)


    def put(name, value="1"):
        tmp = path(name) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(value)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path(name))
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


    def wait_for(name, deadline_seconds):
        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            if os.path.exists(path(name)):
                with open(path(name), encoding="utf-8") as f:
                    return f.read().strip()
            time.sleep(0.05)
        raise SystemExit(f"TIMEOUT waiting for marker {name}")


    try:
        if role == "holder":
            owner = "sigkill-holder:" + uuid.uuid4().hex[:8]
            acquired = False
            for _attempt in range(60):
                acquired, _took_over = mlock.try_acquire(url, owner, ttl)
                if acquired:
                    break
                time.sleep(0.5)
            if not acquired:
                raise SystemExit(
                    "holder could not acquire the lock (free or via expired-lease takeover)"
                )
            renewer = mlock.LeaseRenewer(url, owner, ttl)
            renewer.start()
            put("sigkill_holder_acquired", owner)
            # The parent will SIGKILL this process after observing a renewal.
            while True:
                time.sleep(1.0)

        elif role == "waiter":
            wait_for("sigkill_holder_acquired", 90)
            owner = "sigkill-waiter:" + uuid.uuid4().hex[:8]

            acquired, took_over = mlock.try_acquire(url, owner, ttl)
            if acquired or took_over:
                raise SystemExit(
                    "waiter acquired/took over before SIGKILL while the holder was live"
                )
            put("sigkill_waiter_blocked", owner)

            # The parent kills the live holder only after proving the lease
            # has renewed. Do not attempt takeover until that event is signalled.
            wait_for("sigkill_sent", 90)

            deadline = time.monotonic() + max(60, ttl * 4)
            while time.monotonic() < deadline:
                acquired, took_over = mlock.try_acquire(url, owner, ttl)
                if acquired:
                    if not took_over:
                        raise SystemExit(
                            "waiter acquired after SIGKILL without takeover evidence"
                        )
                    put("sigkill_waiter_acquired", f"{owner}|{took_over}")
                    wait_for("sigkill_parent_ack", 90)
                    if not mlock.release(url, owner):
                        raise SystemExit("waiter cleanup release failed")
                    put("sigkill_waiter_released", owner)
                    return
                time.sleep(0.25)

            raise SystemExit(
                "waiter could not take over the dead holder's expired lease before deadline"
            )

        else:
            raise SystemExit(f"unknown actor role: {role!r}")
    except BaseException as exc:  # noqa: BLE001 - surface the failure to parent
        try:
            put(f"error_{role}", f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001 - best effort
            pass
        raise
    """
)


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


def _monotonic_deadline(seconds: float) -> float:
    """Absolute monotonic deadline for marker-file handshakes."""
    return time.monotonic() + seconds


class TestPostgresMigrationRehearsal:
    """The same target identity ``_run_alembic_migrations()`` serializes."""

    # Rehearsal timing (reviewer finding: the old 2s TTL / ~0.7s renewal
    # interval left too little scheduling margin). TTL 15s with renewal every
    # max(1.0, ttl/3) ≈ 5s fits ~3 renewal intervals before expiry, and the
    # waiter budget (7s < TTL, spanning one full renewal interval) proves
    # fail-closed against a genuinely ALIVE holder, never an expiry artifact.
    REHEARSAL_TTL = 15
    WAITER_BUDGET = 7

    @pytest.fixture(scope="class")
    def migration_url(self):
        return _postgres_test_url()

    @pytest.fixture(scope="class")
    def verify_engine(self, migration_url):
        """One shared read-only verification engine for the whole class."""
        engine = create_engine(migration_url, pool_pre_ping=True)
        try:
            yield engine
        finally:
            engine.dispose()

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

    def test_full_real_lock_lifecycle(self, migration_url, verify_engine):
        """acquire -> persisted ownership -> renew -> concurrent block ->
        release -> re-acquire, against real PostgreSQL via the production
        lock module (no mocks)."""
        owner = f"rehearsal:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        ttl = self.REHEARSAL_TTL
        engine = verify_engine
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
            # Shared class engine is disposed by its own fixture; the lock
            # must end free so later tests never inherit a live lease.
            held = mlock.current_holder(migration_url)
            if held:
                mlock.release(migration_url, held)

    def test_waiter_fails_closed_while_holder_renews_then_proceeds_after_release(
        self, migration_url, verify_engine
    ):
        """ADR-017 wait semantics against real PostgreSQL: a waiter whose
        budget is exhausted by a LIVE (renewing) holder fails closed with
        LeaseLockUnavailable; after the holder releases, the lock is free
        (wait_for_release -> False) and acquirable again."""
        holder_owner = f"rehearsal-holder:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        ttl = self.REHEARSAL_TTL  # renewal max(1.0, ttl/3) ≈ 5s, expiry 15s
        first = mlock.try_acquire(migration_url, holder_owner, ttl)
        _check(first[0], "holder must acquire the lease")
        holder = mlock.LeaseRenewer(migration_url, holder_owner, ttl)
        holder.start()
        try:
            # While the holder is genuinely alive and renewing, a bounded
            # waiter budget (<< TTL, but spanning >= one renewal interval)
            # must fail closed.
            with pytest.raises(mlock.LeaseLockUnavailable):
                mlock.wait_for_release(
                    migration_url, ttl_seconds=ttl, max_wait_seconds=self.WAITER_BUDGET
                )
            with verify_engine.connect() as conn:
                still = conn.execute(
                    text("SELECT locked_by FROM _migration_lock")
                ).scalar_one()
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
        # takeover signal); the next acquisition then succeeds. One owner
        # identity for BOTH acquire and release (release is holder-scoped).
        waited_after = mlock.wait_for_release(
            migration_url, ttl_seconds=ttl, max_wait_seconds=30
        )
        _check(waited_after is False, "a released lock must be observed as free")
        after_owner = f"after:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        _check(
            mlock.try_acquire(migration_url, after_owner, ttl)[0],
            "lock must be acquirable after the waiter observed release",
        )
        # leave the lock free for the other tests
        _check(
            mlock.release(migration_url, after_owner),
            "final cleanup release must succeed for the owning holder",
        )

    def test_two_process_concurrent_fail_closed(self, migration_url, verify_engine, tmp_path):
        """Genuine cross-session concurrency: Process A (its own OS process
        and PostgreSQL connection) holds and renews the lease while
        independent Process B attempts acquisition and must fail closed;
        after A releases, B acquires. Ownership is verified from a third,
        independent connection in this (parent) process. No fixed sleeps:
        marker-file handshakes with deadlines; subprocess timeouts guarantee
        no orphans and no CI hang."""
        markers = tmp_path / "markers"
        markers.mkdir()
        env = dict(
            os.environ,
            REHEARSAL_BACKEND=ROOT,
            REHEARSAL_DB_URL=migration_url,
            REHEARSAL_MARKERS=str(markers),
            REHEARSAL_TTL=str(self.REHEARSAL_TTL),
        )
        actor = tmp_path / "_concurrent_lock_actor.py"
        actor.write_text(_CONCURRENT_ACTOR, encoding="utf-8")

        ROLES = frozenset({"holder", "waiter"})

        def spawn(role):
            # Reject unexpected roles deterministically before spawning.
            if role not in ROLES:
                raise ValueError(f"unknown actor role: {role!r}")
            # Children run detached from the test's stdio and stderr is
            # DEVNULL so nothing can ever block on an undrained pipe — actor
            # failures are reported through the atomic error_<role> marker.
            return subprocess.Popen(
                [sys.executable, str(actor), role],
                env=env,
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        def await_marker(name, proc, deadline_seconds=90.0):
            deadline = _monotonic_deadline(deadline_seconds)
            while not (markers / name).exists():
                if proc is not None and proc.poll() is not None:
                    err = ""
                    error_marker = markers / f"error_{proc.args[-1]}"
                    if error_marker.exists():
                        err = error_marker.read_text(encoding="utf-8")
                    raise AssertionError(
                        f"{proc.args[-1]} actor exited before signalling {name}: "
                        f"rc={proc.returncode} error={err[:500]!r}"
                    )
                if time.monotonic() > deadline:
                    raise AssertionError(f"deadline waiting for marker {name}")
                time.sleep(0.05)
            with open(markers / name, encoding="utf-8") as f:
                return f.read().strip()

        holder = spawn("holder")
        waiter = None
        try:
            # A holds the lock in its own OS process/session.
            holder_owner = await_marker("holder_acquired", holder)

            # Independent verification (third session): ownership persisted.
            with verify_engine.connect() as conn:
                locked_by = conn.execute(
                    text("SELECT locked_by FROM _migration_lock")
                ).scalar_one()
            _check(
                locked_by == holder_owner,
                f"cross-process holder ownership must be persisted, got {locked_by!r}",
            )

            # B: independent process/session — contends, fails closed, then
            # acquires after A releases (signalled via marker handshake).
            waiter = spawn("waiter")
            await_marker("waiter_failed_closed", waiter)
            waiter_owner = await_marker("waiter_acquired", waiter)

            # Independent verification of B's persisted ownership.
            with verify_engine.connect() as conn:
                locked_by_b = conn.execute(
                    text("SELECT locked_by FROM _migration_lock")
                ).scalar_one()
            _check(
                locked_by_b == waiter_owner,
                f"waiter ownership must be persisted, got {locked_by_b!r}",
            )
            # Parent acknowledgment: published atomically (same pattern as
            # the actor markers) so B can never observe a partial file.
            ack_tmp = markers / "parent_ack.tmp"
            with open(ack_tmp, "w", encoding="utf-8") as f:
                f.write("1")
                f.flush()
                os.fsync(f.fileno())
            os.replace(ack_tmp, markers / "parent_ack")

            # Both actors exit 0; every marker milestone was real behavior.
            holder.wait(timeout=120)
            if holder.returncode != 0:
                raise AssertionError(f"holder exited non-zero: {holder.returncode}")
            waiter.wait(timeout=120)
            if waiter.returncode != 0:
                raise AssertionError(f"waiter exited non-zero: {waiter.returncode}")
            await_marker("waiter_released", None)

            # Cleanup verification with the real database.
            with verify_engine.connect() as conn:
                final = conn.execute(
                    text("SELECT locked_by FROM _migration_lock")
                ).scalar_one()
            _check(
                final is None,
                f"lock must be free after both processes released, got {final!r}",
            )
        finally:
            # Orphan-proof cleanup: kill children if anything above raised,
            # then force the lock free via the production release path.
            for proc in (holder, waiter):
                try:
                    if proc is not None and proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=15)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
            try:
                held = mlock.current_holder(migration_url)
                if held:
                    mlock.release(migration_url, held)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


    def test_sigkill_holder_requires_expiry_before_cross_process_takeover(
        self, migration_url, verify_engine, tmp_path
    ):
        """A real holder killed mid-renewal blocks takeover until its lease
        expires, then an independent PostgreSQL session takes over."""
        if os.name != "posix":
            pytest.skip("SIGKILL rehearsal requires a POSIX process model")

        markers = tmp_path / "sigkill_markers"
        markers.mkdir()
        env = dict(
            os.environ,
            REHEARSAL_BACKEND=ROOT,
            REHEARSAL_DB_URL=migration_url,
            REHEARSAL_MARKERS=str(markers),
            REHEARSAL_TTL=str(self.REHEARSAL_TTL),
        )
        actor = tmp_path / "_sigkill_lock_actor.py"
        actor.write_text(_SIGKILL_ACTOR, encoding="utf-8")

        roles = frozenset({"holder", "waiter"})

        def spawn(role):
            if role not in roles:
                raise ValueError(f"unknown actor role: {role!r}")
            return subprocess.Popen(
                [sys.executable, str(actor), role],
                env=env,
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        def await_marker(name, proc, deadline_seconds=90.0):
            deadline = _monotonic_deadline(deadline_seconds)
            while not (markers / name).exists():
                if proc is not None and proc.poll() is not None:
                    err = ""
                    error_marker = markers / f"error_{proc.args[-1]}"
                    if error_marker.exists():
                        err = error_marker.read_text(encoding="utf-8")
                    raise AssertionError(
                        f"{proc.args[-1]} actor exited before signalling {name}: "
                        f"rc={proc.returncode} error={err[:500]!r}"
                    )
                if time.monotonic() > deadline:
                    raise AssertionError(f"deadline waiting for marker {name}")
                time.sleep(0.05)
            with open(markers / name, encoding="utf-8") as f:
                return f.read().strip()

        def put_parent_marker(name, value="1"):
            tmp = markers / f"{name}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(value)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, markers / name)
            except BaseException:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise

        holder = spawn("holder")
        waiter = None
        try:
            holder_owner = await_marker("sigkill_holder_acquired", holder)

            # Prove the holder currently owns a live lease, then wait until
            # the background renewer has actually extended it at least once.
            with verify_engine.connect() as conn:
                row = conn.execute(
                    text("SELECT locked_by, expires_at FROM _migration_lock")
                ).fetchone()
            _check(row is not None, "SIGKILL holder row must exist")
            _check(
                row[0] == holder_owner,
                f"SIGKILL holder ownership must be persisted, got {row[0]!r}",
            )
            initial_expiry = row[1]
            _check(initial_expiry is not None, "SIGKILL holder lease must have an expiry")

            renewed = False
            renewal_deadline = _monotonic_deadline(12)
            while time.monotonic() < renewal_deadline:
                time.sleep(0.25)
                with verify_engine.connect() as conn:
                    current = conn.execute(
                        text("SELECT locked_by, expires_at FROM _migration_lock")
                    ).fetchone()
                _check(
                    current is not None and current[0] == holder_owner,
                    "live SIGKILL holder must retain ownership while renewing",
                )
                if current[1] is not None and current[1] > initial_expiry:
                    renewed = True
                    break
            _check(
                renewed,
                "SIGKILL holder must demonstrate at least one successful renewal before termination",
            )

            # Start the independent waiter before killing the holder. It must
            # prove that takeover is blocked while the live lease is valid.
            waiter = spawn("waiter")
            waiter_owner = await_marker("sigkill_waiter_blocked", waiter)

            with verify_engine.connect() as conn:
                live = conn.execute(
                    text("SELECT locked_by, expires_at FROM _migration_lock")
                ).fetchone()
            _check(live is not None, "live-holder row must exist before SIGKILL")
            _check(live[0] == holder_owner, "waiter must remain blocked by the live holder")
            _check(
                live[1] is not None and live[1] > initial_expiry,
                "holder lease must still be live after the renewal proof",
            )

            # Force abrupt process death. This intentionally bypasses the
            # normal release path so the database must recover by expiry.
            os.kill(holder.pid, signal.SIGKILL)
            holder.wait(timeout=15)
            _check(
                holder.returncode == -signal.SIGKILL,
                f"holder must terminate via SIGKILL, got return code {holder.returncode}",
            )
            put_parent_marker("sigkill_sent")

            acquired_marker = await_marker("sigkill_waiter_acquired", waiter, 90)
            parts = acquired_marker.split("|", 1)
            _check(
                len(parts) == 2,
                f"waiter acquisition marker must include owner and takeover flag, got {acquired_marker!r}",
            )
            acquired_owner, took_over_text = parts
            _check(
                acquired_owner == waiter_owner,
                f"waiter owner mismatch after takeover: {acquired_owner!r} != {waiter_owner!r}",
            )
            _check(
                took_over_text == "True",
                "waiter must report a genuine expired-lease takeover after SIGKILL",
            )

            # Third independent PostgreSQL connection: takeover persisted in
            # the database before the waiter receives the release ack.
            with verify_engine.connect() as conn:
                taken = conn.execute(
                    text("SELECT locked_by FROM _migration_lock")
                ).scalar_one()
            _check(
                taken == waiter_owner,
                f"taken-over lock ownership must persist as waiter, got {taken!r}",
            )

            put_parent_marker("sigkill_parent_ack")
            waiter.wait(timeout=30)
            _check(
                waiter.returncode == 0,
                f"waiter must exit cleanly after takeover/release, got {waiter.returncode}",
            )
            await_marker("sigkill_waiter_released", None)

            with verify_engine.connect() as conn:
                final = conn.execute(
                    text("SELECT locked_by FROM _migration_lock")
                ).scalar_one()
            _check(
                final is None,
                f"lock must be free after SIGKILL takeover rehearsal, got {final!r}",
            )
        finally:
            for proc in (holder, waiter):
                try:
                    if proc is not None and proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=15)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
            try:
                held = mlock.current_holder(migration_url)
                if held:
                    mlock.release(migration_url, held)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

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
