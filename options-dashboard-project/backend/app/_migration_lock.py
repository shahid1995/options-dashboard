"""Transactional migration lease lock (ADR-017).

Why this exists
---------------
Every application instance runs ``init_db() -> alembic upgrade head`` during
startup. Two instances starting simultaneously can therefore execute the same
schema DDL concurrently, which has historically produced real
``DuplicateTable`` startup failures. The runtime/migration identity separation
(ADR-016) changed WHO migrates, not WHETHER concurrent migration attempts can
overlap. This module provides the missing serialization: only one process may
execute the migration chain at a time.

Mechanism (Option A — database-backed transactional lease)
----------------------------------------------------------
A single-row lock table is managed with strictly transactional statements:

* bootstrap: ``CREATE TABLE IF NOT EXISTS`` (CockroachDB serializes DDL, so
  two simultaneous creations converge on one table), then
  ``INSERT ... ON CONFLICT DO NOTHING`` for the single ``_id = true`` row.
* acquire: a single UPDATE that sets the lock row only when it is free or
  its lease has expired. Under CockroachDB's serializable isolation such an
  UPDATE cannot succeed twice for overlapping leases.
* takeover: an expired lease (holder crashed mid-migration) is overwritten by
  the next acquirer, so a wedged holder can never block recovery forever.
* release: the holder clears its own row.

While the holder is alive it RENEWS the lease on a background thread (every
``max(1.0, ttl / 3)`` — TTL/3 with a 1-second floor; configuration enforces
``ttl >= 2`` so the interval is always strictly below the TTL), so a
legitimately long migration is never stolen by a waiter. Because renewal is
a thread inside the holder process, a crashed
holder simply stops renewing: waiters observe the lease expiry, take over,
and re-run the (idempotent, Alembic-managed) chain. Alembic's version table
makes re-running a completed migration a no-op, so takeover after a
*successful-but-unreleased* migration is also safe.

Bootstrap on an EMPTY database
------------------------------
The lock table is created by the migrator identity itself through plain
transactional DDL/DML before any Alembic revision runs; it does not depend on
any application or Alembic table existing first. Because ``CREATE TABLE IF
NOT EXISTS`` and ``INSERT ... ON CONFLICT DO NOTHING`` are both serialized by
CockroachDB's transaction protocol, concurrent bootstrap converges without a
race window in which two processes could both believe they hold the lock.

Identity requirements
---------------------
The lock is acquired with the migration database URL (the migrator identity
when ``STRIKENOVA_MIGRATION_DATABASE_URL`` is set, otherwise the historical
runtime URL). The runtime identity never needs owner/admin privileges for
locking: schema CREATE for the bootstrap belongs to the migrator identity,
exactly like the migrations themselves (ADR-016).

SQLite / in-memory deployments
------------------------------
Single-process by construction; locking is skipped and logged, preserving all
hermetic test behavior and local development ergonomics.

Logging never includes connection strings or credentials — only the lock
owner token, wait durations, and lease state.
"""

from __future__ import annotations

import datetime as _dt
import logging
import secrets
import time
import uuid

logger = logging.getLogger(__name__)

LOCK_TABLE = "_migration_lock"

#: How a waiter proves the holder wedged: the holder's lease deadline must be
#: strictly in the past before takeover is allowed.
_DEFAULT_TTL_SECONDS = 120


class LeaseLockUnavailable(RuntimeError):
    """Raised when the migration lock stays held beyond its TTL."""


def _utcnow() -> _dt.datetime:
    # Timezone-AWARE on purpose: CockroachDB returns tz-aware datetimes for
    # TIMESTAMPTZ columns, and mixing aware/naive in Python comparisons
    # raises TypeError. Naive values are treated as UTC by the database on
    # insert, so an aware UTC clock keeps every comparison consistent.
    return _dt.datetime.now(_dt.timezone.utc)


def _acquire_sql(
    owner: str,
    ttl_seconds: int,
) -> str:
    """Parameterized acquire/takeover statement (dialect-neutral)."""
    return (
        "UPDATE " + LOCK_TABLE + " SET locked_by = %s, acquired_at = %s, expires_at = %s "
        "WHERE _id = true AND (locked_by IS NULL OR expires_at IS NULL OR expires_at < %s)"
    )


def _is_serialization_failure(exc: BaseException) -> bool:
    """Detect a transient transaction-retry error (SQLSTATE 40001).

    Uses psycopg's structured ``sqlstate`` attribute when present and falls
    back to the message text for other drivers. CRDB's message text does not
    reliably contain the bare code, so attribute detection is primary.
    """
    state = getattr(exc, "sqlstate", None)
    if state:
        return str(state) == "40001"
    return "40001" in str(exc)


def _ensure_lock_table(conn, max_retries: int = 8) -> None:
    """Create the lock table and its single row if absent.

    Both statements are transactional and idempotent; CockroachDB serializes
    concurrent executions, so two booting instances converge safely. The
    bounded ``40001`` retry handles the transient serialization conflicts two
    SIMULTANEOUS cold starts can hit while racing the same bootstrap DDL.
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS " + LOCK_TABLE + " ("
                "_id BOOLEAN PRIMARY KEY DEFAULT true CHECK (_id), "
                # TEXT (not CRDB's STRING alias) so the bootstrap DDL is
                # valid on BOTH CockroachDB and vanilla PostgreSQL.
                "locked_by TEXT, "
                "acquired_at TIMESTAMPTZ, "
                "expires_at TIMESTAMPTZ)"
            )
            cur.execute(
                "INSERT INTO " + LOCK_TABLE + " (_id) VALUES (true) ON CONFLICT (_id) DO NOTHING"
            )
            conn.commit()
            return
        except Exception as exc:  # noqa: BLE001 - retry only transient conflicts
            conn.rollback()
            last_exc = exc
            if _is_serialization_failure(exc) and attempt < max_retries - 1:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise
    raise last_exc  # pragma: no cover - defensive


def _connect(url: str):
    """Open a DBAPI connection for the given SQLAlchemy URL.

    Prefers psycopg 3 (the project's PostgreSQL/CRDB driver); falls back to
    psycopg2 when only psycopg 3 dialect URLs are otherwise unavailable.
    """
    try:
        import psycopg  # noqa: WPS433 (runtime driver probe)

        pg_url = url
        for prefix in ("cockroachdb+psycopg://", "postgresql+psycopg://", "cockroachdb://"):
            if pg_url.startswith(prefix):
                pg_url = "postgresql://" + pg_url[len(prefix):]
                break
        return psycopg.connect(pg_url, autocommit=False, connect_timeout=20)
    except ImportError:  # pragma: no cover - project always ships psycopg 3
        import psycopg2  # type: ignore[no-redef]

        pg_url = url
        for prefix in ("cockroachdb+psycopg://", "postgresql+psycopg://", "cockroachdb://"):
            if pg_url.startswith(prefix):
                pg_url = "postgresql://" + pg_url[len(prefix):]
                break
        return psycopg2.connect(pg_url)


def try_acquire(url: str, owner: str, ttl_seconds: int = _DEFAULT_TTL_SECONDS, max_retries: int = 6) -> tuple[bool, bool]:
    """Attempt to acquire the migration lease.

    Returns ``(acquired, took_over_expired_lease)``. Retries transient
    CockroachDB ``40001`` serialization failures a bounded number of times.
    """
    conn = _connect(url)
    try:
        _ensure_lock_table(conn)
        for attempt in range(max_retries):
            try:
                cur = conn.cursor()
                cur.execute("SELECT locked_by, expires_at FROM " + LOCK_TABLE + " WHERE _id = true")
                row = cur.fetchone()
                held_by = row[0] if row else None
                expires_at = row[1] if row else None
                now = _utcnow()
                expired = expires_at is not None and expires_at < now
                if held_by and not expired:
                    conn.rollback()
                    return False, False
                cur.execute(
                    _acquire_sql(owner, ttl_seconds),
                    (owner, now, now + _dt.timedelta(seconds=ttl_seconds), now),
                )
                acquired = (cur.rowcount or 0) > 0
                conn.commit()
                return acquired, (acquired and bool(held_by))
            except Exception as exc:  # noqa: BLE001 - retry only transient conflicts
                conn.rollback()
                if _is_serialization_failure(exc) and attempt < max_retries - 1:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                raise
        return False, False
    finally:
        conn.close()


def wait_for_release(url: str, ttl_seconds: int = _DEFAULT_TTL_SECONDS, max_wait_seconds: int = 900) -> bool:
    """Wait until the current holder's lease expires.

    Returns ``True`` when the observed lease has expired (the caller should
    then attempt takeover). Returns ``False`` when the lock became free
    within the wait budget. Raises :class:`LeaseLockUnavailable` when the
    holder keeps renewing/wedged past ``max_wait_seconds`` — the process then
    fails closed instead of executing concurrent DDL.
    """
    deadline = time.monotonic() + max_wait_seconds
    poll = 0.5
    while time.monotonic() < deadline:
        conn = _connect(url)
        try:
            cur = conn.cursor()
            cur.execute("SELECT locked_by, expires_at FROM " + LOCK_TABLE + " WHERE _id = true")
            row = cur.fetchone()
            held_by, expires_at = (row[0], row[1]) if row else (None, None)
            if not held_by:
                return False
            if expires_at is not None and expires_at < _utcnow():
                logger.info(
                    "migration lock lease expired (owner=%s); takeover eligible", held_by
                )
                return True
        finally:
            conn.close()
        time.sleep(poll)
    raise LeaseLockUnavailable(
        "migration lock still held after lease expiry wait budget; "
        "refusing to run migrations concurrently (fail closed)"
    )


def release(url: str, owner: str, max_retries: int = 6) -> bool:
    """Release the lease if (and only if) we still hold it."""
    conn = _connect(url)
    try:
        for attempt in range(max_retries):
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE " + LOCK_TABLE + " SET locked_by = NULL, acquired_at = NULL, expires_at = NULL "
                    "WHERE _id = true AND locked_by = %s",
                    (owner,),
                )
                released = (cur.rowcount or 0) > 0
                conn.commit()
                return released
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                if _is_serialization_failure(exc) and attempt < max_retries - 1:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                raise
        return False
    finally:
        conn.close()


class LeaseRenewer:
    """Extend the holder's lease until stopped (daemon thread per holder)."""

    def __init__(self, url: str, owner: str, ttl_seconds: int):
        self._url = url
        self._owner = owner
        self._ttl = ttl_seconds
        self._stop = None
        self._thread = None

    def start(self) -> None:
        import threading

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="migration-lease-renewer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
            self._stop = None

    def _run(self) -> None:
        # Renew strictly more often than the TTL (interval = TTL/3, min 1s):
        # a renewal interval >= TTL would leave expiry gaps a waiter could
        # misread as a crashed holder.
        interval = max(1.0, self._ttl / 3.0)
        while not self._stop.wait(interval):
            conn = None
            try:
                conn = _connect(self._url)
                cur = conn.cursor()
                cur.execute(
                    "UPDATE " + LOCK_TABLE + " SET expires_at = %s "
                    "WHERE _id = true AND locked_by = %s",
                    (_utcnow() + _dt.timedelta(seconds=self._ttl), self._owner),
                )
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - renewal is best-effort
                logger.warning("migration lease renewal failed (%s); will retry", type(exc).__name__)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass


def read_alembic_version(url: str) -> str | None:
    """Return the current alembic_version, or None when absent (empty DB)."""
    conn = _connect(url)
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT version_num FROM alembic_version")
            row = cur.fetchone()
            return row[0] if row else None
        except Exception:  # noqa: BLE001 - table absent on an empty database
            return None
    finally:
        conn.close()


def current_holder(url: str) -> str | None:
    """Inspect the current lock holder (diagnostics/tests only)."""
    conn = _connect(url)
    try:
        cur = conn.cursor()
        cur.execute("SELECT locked_by FROM " + LOCK_TABLE + " WHERE _id = true")
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()
