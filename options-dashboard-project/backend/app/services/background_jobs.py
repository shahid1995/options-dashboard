"""Day 47 — durable background-job service.

Implements the queue/worker boundary on top of the ``background_jobs``
table (see ``app/models.py``). The database is the durable queue: every
transition is a plain transaction on the application database, so queued
jobs survive process restart by construction.

Boundaries
----------
* **Producer** — :func:`enqueue` is the only way work enters the queue.
  It is idempotent per ``idempotency_key``: a duplicate enqueue of a live
  job returns the existing row unchanged (racing INSERTs collapse through
  the unique constraint), and re-enqueueing a terminal job re-arms the
  same row instead of duplicating it.
* **Worker acquisition** — :func:`claim_next` hands a job to exactly one
  worker through a single conditional ``UPDATE ... WHERE`` (the same
  proven pattern as the ADR-017 migration-lock acquire): the winning
  worker sets a lease owner + deadline; losers observe ``rowcount == 0``
  and move on. A crashed worker's lease simply expires, making the job
  claimable again — no cleanup process is needed.
* **Completion / failure** — :func:`complete_job` and :func:`fail_job`
  are the only transitions out of ``RUNNING``. Retryable failures are
  rescheduled with bounded exponential backoff; non-retryable failures
  and exhausted attempt budgets become ``DEAD_LETTERED`` rows that stay
  inspectable (never silently deleted).
* **Execution** — :func:`execute_job` dispatches by job type. Day 47
  scope: ``HISTORICAL_INGESTION`` executes the real
  ``BackfillOrchestrator`` through the application service boundary (the
  same object the CLI drives); the CLI's checkpoint/resume semantics
  provide execution idempotency, so a retry after a crash never
  duplicates durable ingestion rows.

Attempt accounting: ``attempt_count`` is incremented atomically inside
the claim UPDATE (an "attempt" = one execution start). A job whose
attempt budget is exhausted is dead-lettered at claim time instead of
being started again, so a crash-looping worker cannot retry forever.

Retryability: a failure is retryable when it is transient — SQLSTATE
40001/40P01/55P03, serialization/deadlock/lock/connection markers, or
``JobExecutionError(retryable=True)``. Authentication failures are
explicitly NON-retryable (they require human re-authentication).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import and_, func, or_, select, update  # noqa: F401 (func: coalesce)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import BackgroundJob, JobStatus, JobType

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Defaults (overridable per job through the payload policy keys)
# --------------------------------------------------------------------------
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 30.0
DEFAULT_BACKOFF_CAP_SECONDS = 3600.0
DEFAULT_LEASE_SECONDS = 900

_TERMINAL_STATUSES = frozenset(
    {
        JobStatus.SUCCEEDED.value,
        JobStatus.DEAD_LETTERED.value,
        JobStatus.CANCELLED.value,
    }
)

_LIVE_STATUSES = frozenset(
    {
        JobStatus.PENDING.value,
        JobStatus.RUNNING.value,
        JobStatus.FAILED_RETRYABLE.value,
    }
)

# SQLSTATE codes treated as transient: serialization failure (CRDB/PG),
# deadlock detection, and lock-not-available.
_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01", "55P03"})

_RETRYABLE_MESSAGE_MARKERS = (
    "serialization",
    "could not serialize",
    "restart transaction",
    "deadlock",
    "lock not available",
    "tuple concurrently updated",
    "database table is locked",
    "database is locked",
    "connection reset",
    "connection refused",
    "server closed the connection",
    "terminating connection",
    "timed out",
    "timeout",
)


class JobExecutionError(RuntimeError):
    """Raised by job executors to signal failure with a retryability verdict."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class JobPolicy:
    """Retry/lease policy resolved from a job's payload (with defaults)."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS
    lease_seconds: int = DEFAULT_LEASE_SECONDS


def _utcnow() -> datetime:
    """Naive UTC timestamp.

    The job table stores naive UTC (matching what PostgreSQL/CRDB TIMESTAMPS
    hold after the driver strips tzinfo, and what SQLite string-comparison
    semantics require). All scheduling comparisons bind THIS application
    clock as a parameter, so claim/retry behavior is identical across
    SQLite, PostgreSQL and CockroachDB and free of server-clock skew.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def policy_from_payload(payload_json: str | None) -> JobPolicy:
    """Resolve the retry/lease policy stored in the job payload JSON."""
    try:
        data = json.loads(payload_json or "{}")
    except (TypeError, ValueError):
        return JobPolicy()
    policy = data.get("policy") if isinstance(data, dict) else None
    if not isinstance(policy, dict):
        return JobPolicy()
    try:
        return JobPolicy(
            max_attempts=max(1, int(policy.get("max_attempts", DEFAULT_MAX_ATTEMPTS))),
            backoff_base_seconds=max(
                0.0, float(policy.get("backoff_base_seconds", DEFAULT_BACKOFF_BASE_SECONDS))
            ),
            backoff_cap_seconds=max(
                0.0, float(policy.get("backoff_cap_seconds", DEFAULT_BACKOFF_CAP_SECONDS))
            ),
            lease_seconds=max(1, int(policy.get("lease_seconds", DEFAULT_LEASE_SECONDS))),
        )
    except (TypeError, ValueError):
        return JobPolicy()


def backoff_delay_seconds(policy: JobPolicy, failed_attempt: int) -> float:
    """Bounded exponential backoff for the given failed attempt number.

    First failure waits ``base``, second waits ``2 * base``, then
    ``4 * base`` ... capped at ``backoff_cap_seconds``. Deterministic (no
    jitter) so scheduling is exactly testable.
    """
    return min(
        policy.backoff_cap_seconds,
        policy.backoff_base_seconds * (2 ** max(0, failed_attempt - 1)),
    )


def is_retryable_failure(exc: BaseException) -> bool:
    """Classify an execution failure as retryable or permanent.

    ``JobExecutionError`` carries an explicit verdict. Authentication
    failures are never retried. Everything else is retried only when it
    looks transient (SQLSTATE 40001/40P01/55P03 or known transient
    message markers); unknown errors are treated as non-retryable so a
    deterministic bug cannot burn the retry budget.
    """
    verdict = getattr(exc, "retryable", None)
    if verdict is not None:
        return bool(verdict)
    if type(exc).__name__ == "UpstoxAuthenticationError":
        return False
    state = getattr(exc, "sqlstate", None) or getattr(
        getattr(exc, "orig", None), "sqlstate", None
    )
    if state:
        return str(state) in _RETRYABLE_SQLSTATES
    message = str(exc).lower()
    return any(marker in message for marker in _RETRYABLE_MESSAGE_MARKERS)


# --------------------------------------------------------------------------
# Producer boundary
# --------------------------------------------------------------------------


def enqueue(
    db: Session,
    *,
    job_type: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    user_scope: str | None = None,
    available_at: datetime | None = None,
) -> tuple[BackgroundJob, bool]:
    """Enqueue a job idempotently. Returns ``(job, created)``.

    ``created`` is True only when a NEW row was inserted. Duplicate
    enqueues of a live job (PENDING/RUNNING/FAILED_RETRYABLE) return the
    existing row unchanged. Re-enqueueing a terminal job (SUCCEEDED /
    DEAD_LETTERED / CANCELLED) re-arms that same row as a fresh lifecycle
    (PENDING, attempt budget reset, failure info cleared) — one row per
    idempotency key, ever, so duplicate triggers can never multiply jobs
    and dead-letter history is never silently duplicated or lost.
    """
    job = (
        db.execute(
            select(BackgroundJob).where(
                BackgroundJob.idempotency_key == idempotency_key
            )
        )
        .scalars()
        .first()
    )
    if job is not None:
        if job.status in _TERMINAL_STATUSES:
            _rearm(job, payload=payload, available_at=available_at or _utcnow())
            db.commit()
            return job, False
        return job, False

    job = BackgroundJob(
        id=str(uuid.uuid4()),
        job_type=job_type,
        idempotency_key=idempotency_key,
        payload=json.dumps(payload or {}),
        user_scope=user_scope,
        status=JobStatus.PENDING.value,
        attempt_count=0,
        available_at=available_at or _utcnow(),
    )
    db.add(job)
    try:
        db.commit()
        return job, True
    except IntegrityError:
        # A concurrent producer inserted the same idempotency key first.
        # Collapse onto the winner's row; re-arm it if it was terminal.
        db.rollback()
        winner = (
            db.execute(
                select(BackgroundJob).where(
                    BackgroundJob.idempotency_key == idempotency_key
                )
            )
            .scalars()
            .first()
        )
        if winner is None:  # pragma: no cover - defensive
            raise
        if winner.status in _TERMINAL_STATUSES:
            _rearm(winner, payload=payload, available_at=available_at or _utcnow())
            db.commit()
        return winner, False


def _rearm(
    job: BackgroundJob,
    *,
    payload: dict[str, Any] | None,
    available_at: datetime,
) -> None:
    """Reset a terminal job row into a fresh PENDING lifecycle in place."""
    job.status = JobStatus.PENDING.value
    job.attempt_count = 0
    job.payload = json.dumps(payload if payload is not None else json.loads(job.payload or "{}"))
    job.available_at = available_at
    job.lease_owner = None
    job.lease_expires_at = None
    job.started_at = None
    job.completed_at = None
    job.last_error = None
    job.dead_letter_reason = None


# --------------------------------------------------------------------------
# Worker acquisition boundary
# --------------------------------------------------------------------------


def claim_next(
    db: Session,
    *,
    worker_id: str,
    job_type: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int | None = None,
) -> BackgroundJob | None:
    """Atomically claim the next runnable job, or return ``None``.

    A single conditional ``UPDATE`` transfers ownership, so concurrent
    workers can never own the same attempt: losers see ``rowcount == 0``
    and loop to the next candidate. The claim also recovers jobs whose
    lease expired (crashed worker) and dead-letters jobs whose attempt
    budget is exhausted instead of starting them again.
    """
    def _runnable(now):
        """Claimable-now conditions: due queued work, or an expired lease."""
        due = (
            BackgroundJob.status.in_(
                [JobStatus.PENDING.value, JobStatus.FAILED_RETRYABLE.value]
            ),
            BackgroundJob.available_at <= now,
        )
        expired = (
            BackgroundJob.status == JobStatus.RUNNING.value,
            BackgroundJob.lease_expires_at.isnot(None),
            BackgroundJob.lease_expires_at <= now,
        )
        return or_(and_(*due), and_(*expired))

    now = _utcnow()
    conditions = [_runnable(now)]
    if job_type is not None:
        conditions.append(BackgroundJob.job_type == job_type)

    while True:
        candidate = (
            db.execute(
                select(BackgroundJob)
                .where(*conditions)
                .order_by(BackgroundJob.available_at.asc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if candidate is None:
            db.commit()
            return None

        policy = JobPolicy(lease_seconds=lease_seconds)
        if max_attempts is not None:
            policy = JobPolicy(
                lease_seconds=lease_seconds, max_attempts=max_attempts
            )
        else:
            stored = policy_from_payload(candidate.payload)
            policy = JobPolicy(
                lease_seconds=lease_seconds, max_attempts=stored.max_attempts
            )

        budget_exhausted = candidate.attempt_count >= policy.max_attempts
        if budget_exhausted:
            updated = db.execute(
                update(BackgroundJob)
                .where(BackgroundJob.id == candidate.id)
                .where(_runnable(now))
                .values(
                    status=JobStatus.DEAD_LETTERED.value,
                    lease_owner=None,
                    lease_expires_at=None,
                    completed_at=now,
                    dead_letter_reason=(
                        f"max attempts ({policy.max_attempts}) exhausted "
                        f"after {candidate.attempt_count} attempts; "
                        f"last error: {candidate.last_error or 'n/a'}"
                    ),
                )
            )
            db.commit()
            if (updated.rowcount or 0) == 0:
                continue  # lost the row to a racing worker; try the next
            logger.warning(
                "job %s dead-lettered after exhausting %s attempts",
                candidate.id,
                policy.max_attempts,
            )
            continue

        result = db.execute(
            update(BackgroundJob)
            .where(BackgroundJob.id == candidate.id)
            .where(_runnable(now))
            .values(
                status=JobStatus.RUNNING.value,
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=policy.lease_seconds),
                attempt_count=BackgroundJob.attempt_count + 1,
                started_at=func.coalesce(BackgroundJob.started_at, now),
            )
        )
        db.commit()
        if (result.rowcount or 0) == 1:
            return candidate  # attribute access refreshes post-commit state
        # Lost the race: another worker claimed this row between our SELECT
        # and UPDATE. Loop; the PENDING/RUNNING population only shrinks, so
        # this terminates.


def complete_job(db: Session, job: BackgroundJob) -> None:
    """Mark a RUNNING job SUCCEEDED and release its lease."""
    job.status = JobStatus.SUCCEEDED.value
    job.completed_at = _utcnow()
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error = None
    db.commit()


def fail_job(
    db: Session,
    job: BackgroundJob,
    exc: BaseException | str,
) -> BackgroundJob:
    """Handle a failed attempt: retry with backoff or dead-letter.

    The attempt was already counted at claim time. Retryable failures are
    rescheduled (``FAILED_RETRYABLE`` with a future ``available_at``);
    non-retryable failures become ``DEAD_LETTERED`` with the reason and
    last error preserved for inspection.
    """
    policy = policy_from_payload(job.payload)
    message = str(exc)[:2000]
    job.last_error = message
    job.lease_owner = None
    job.lease_expires_at = None
    now = _utcnow()

    if not is_retryable_failure(exc if isinstance(exc, BaseException) else JobExecutionError(exc, retryable=False)):
        job.status = JobStatus.DEAD_LETTERED.value
        job.completed_at = now
        job.dead_letter_reason = f"non-retryable failure: {type(exc).__name__}"
        db.commit()
        logger.warning("job %s dead-lettered: %s", job.id, job.dead_letter_reason)
        return job

    delay = backoff_delay_seconds(policy, job.attempt_count)
    job.status = JobStatus.FAILED_RETRYABLE.value
    job.available_at = now + timedelta(seconds=delay)
    db.commit()
    logger.info(
        "job %s failed (attempt %s); retrying in %.1fs",
        job.id,
        job.attempt_count,
        delay,
    )
    return job


# --------------------------------------------------------------------------
# Execution boundary
# --------------------------------------------------------------------------


def execute_historical_ingestion(db: Session, job: BackgroundJob) -> dict[str, Any]:
    """Execute a HISTORICAL_INGESTION job through the real orchestrator.

    Drives the same ``BackfillOrchestrator`` application service the CLI
    uses — never a subprocess — against the worker's session. Execution
    idempotency comes from the orchestrator's durable checkpoint resume
    plus the ingestion tables' unique-constraint insert semantics.

    Raises :class:`JobExecutionError` with a retryability verdict:
    authentication problems are permanent (human re-authentication
    required); other stage failures are retryable because the orchestrator
    resumes from durable checkpoints.
    """
    from app.services.backfill_orchestrator import BackfillOrchestrator, TokenBridge
    from app.services.rate_limiter import GlobalRateLimiter, RateLimiterConfig
    from app.services.upstox_client import UpstoxClient

    try:
        params = json.loads(job.payload or "{}")
    except (TypeError, ValueError):
        params = {}
    if not isinstance(params, dict):
        params = {}

    stages = params.get("stages") or ["contracts", "nifty", "options"]
    start_raw = params.get("nifty_start_date")
    nifty_start_date = None
    if start_raw:
        from datetime import date as _date

        nifty_start_date = _date.fromisoformat(str(start_raw))
    concurrency = max(1, int(params.get("concurrency", 1)))
    force = bool(params.get("force", False))

    token_bridge = TokenBridge()
    client = UpstoxClient(token_provider=token_bridge)
    rate_limiter = GlobalRateLimiter(
        config=RateLimiterConfig(initial_concurrency=concurrency, max_concurrency=6)
    )
    orchestrator = BackfillOrchestrator(
        db, client, force=force, rate_limiter=rate_limiter
    )
    result = asyncio.run(
        orchestrator.run_all(stages=list(stages), nifty_start_date=nifty_start_date)
    )

    summary: dict[str, Any] = {
        "operation": result.operation,
        "status": result.status,
        "api_calls": result.api_calls,
        "rows_fetched": result.rows_fetched,
        "rows_inserted": result.rows_inserted,
        "rows_skipped": result.rows_skipped,
        "errors": result.errors[:10],
    }

    if result.status == "SUCCESS":
        return summary

    auth_failure = any(
        "AUTH_EXPIRED" in e or "Authentication" in e for e in result.errors
    )
    raise JobExecutionError(
        f"historical ingestion ended with status {result.status}: "
        + ("; ".join(result.errors[:3]) or "no error detail"),
        retryable=not auth_failure,
    )


def execute_job(db: Session, job: BackgroundJob) -> dict[str, Any]:
    """Dispatch a claimed job by type."""
    if job.job_type == JobType.HISTORICAL_INGESTION.value:
        return execute_historical_ingestion(db, job)
    raise JobExecutionError(
        f"unknown job type: {job.job_type!r}", retryable=False
    )


# --------------------------------------------------------------------------
# Worker loop
# --------------------------------------------------------------------------


def run_worker(
    *,
    session_factory: Callable[[], Session] | sessionmaker,
    poll_interval_seconds: float = 5.0,
    once: bool = False,
    worker_id: str | None = None,
    job_type: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    stop: Any = None,
) -> dict[str, int]:
    """Poll-and-drain worker loop.

    Each claim runs on a FRESH session/transaction; a worker process may
    be terminated at any instant — claimed-but-unfinished jobs simply
    keep their lease until it expires and another worker picks them up.
    Nothing is held in memory across restarts.

    ``once=True`` drains until no runnable job remains (used by tests and
    administrative runs). ``stop`` is an optional ``threading.Event``.
    Returns a summary of transitions performed.
    """
    from app.utils.retry import retry_on_serialization

    wid = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    summary = {"claimed": 0, "succeeded": 0, "failed": 0, "dead_lettered": 0}

    def _claim_id(db: Session) -> str | None:
        # Read the id while the claim session is still open: ORM instances
        # expire on commit and cannot be refreshed after the session closes.
        claimed = claim_next(
            db,
            worker_id=wid,
            job_type=job_type,
            lease_seconds=lease_seconds,
        )
        return claimed.id if claimed is not None else None

    while stop is None or not stop.is_set():
        db = None
        job_id = None
        try:
            db = session_factory()
            job_id = retry_on_serialization(_claim_id, session_factory)
        except Exception:  # pragma: no cover - defensive: never crash the loop
            logger.exception("worker claim failed")
        finally:
            try:
                if db is not None:
                    db.close()
            except Exception:  # pragma: no cover
                pass

        if job_id is None:
            if once:
                break
            if stop is not None:
                stop.wait(poll_interval_seconds)
            else:
                import time as _time

                _time.sleep(poll_interval_seconds)
            continue

        summary["claimed"] += 1
        # Execute on a FRESH session: the claim transaction is complete and
        # closed; the row is re-fetched by primary key in this session.
        db = session_factory()
        try:
            job = db.get(BackgroundJob, job_id)
            if job is None:  # pragma: no cover - row vanished mid-flight
                logger.warning("claimed job %s no longer exists", job_id)
                continue
            execute_job(db, job)
            complete_job(db, job)
            summary["succeeded"] += 1
        except Exception as exc:
            # A failed executor can leave the session mid-transaction or in
            # a rolled-back-required state; clear it so fail_job's own
            # transition commits cleanly on a fresh transaction.
            try:
                db.rollback()
            except Exception:  # pragma: no cover - defensive
                pass
            job = fail_job(db, job, exc)
            if job.status == JobStatus.DEAD_LETTERED.value:
                summary["dead_lettered"] += 1
            else:
                summary["failed"] += 1
            logger.warning("job %s handled failure: %s", job.id, exc)
        finally:
            db.close()

        if once and stop is None:
            continue  # drain until claim_next returns None
    return summary
