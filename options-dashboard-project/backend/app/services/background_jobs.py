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
* **Heartbeat (lease renewal)** — while a claimed job executes, the worker
  renews its lease every ~lease/3 (floored) through :func:`renew_lease`, an
  atomic conditional UPDATE requiring the row to still be ``RUNNING``, still
  leased to the same worker, and unexpired at renewal time. Each renewal runs
  on its own session/transaction (execution work never shares it). The
  heartbeat stops the moment execution ends — normally or via exception — and
  a lost or failed renewal is logged, never fabricated into success. A crashed
  process stops renewing by construction, so its lease expires and the job is
  reclaimed: crash recovery is unchanged.
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
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import and_, func, or_, select, update  # noqa: F401 (func: coalesce)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import BackgroundJob, JobStatus, JobType
from app.utils.retry import retry_on_serialization  # noqa: E402 (patch point for tests)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Defaults (overridable per job through the payload policy keys)
# --------------------------------------------------------------------------
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 30.0
DEFAULT_BACKOFF_CAP_SECONDS = 3600.0
DEFAULT_LEASE_SECONDS = 900

# Heartbeat period floor: a lease shorter than ~1.5s (test-scale only) still
# gets at least this long between renewal attempts instead of busy-looping.
_MIN_HEARTBEAT_INTERVAL_SECONDS = 0.5

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

# Historical-ingestion stages that a job payload may request (F9).
_KNOWN_STAGES = frozenset({"contracts", "nifty", "options"})

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
    lease_seconds: int | None = None,
    max_attempts: int | None = None,
) -> BackgroundJob | None:
    """Atomically claim the next runnable job, or return ``None``.

    A single conditional ``UPDATE`` transfers ownership, so concurrent
    workers can never own the same attempt: losers see ``rowcount == 0``
    and loop to the next candidate. The claim also recovers jobs whose
    lease expired (crashed worker) and dead-letters jobs whose attempt
    budget is exhausted instead of starting them again.

    Lease-duration precedence (deterministic):
    1. explicit ``lease_seconds`` argument (worker/CLI override)
    2. the job payload's ``policy.lease_seconds`` (per-job override)
    3. ``DEFAULT_LEASE_SECONDS``
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

        stored = policy_from_payload(candidate.payload)
        effective_lease = (
            lease_seconds if lease_seconds is not None else stored.lease_seconds
        )
        if max_attempts is not None:
            policy = JobPolicy(lease_seconds=effective_lease, max_attempts=max_attempts)
        else:
            policy = JobPolicy(
                lease_seconds=effective_lease, max_attempts=stored.max_attempts
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


def complete_job(db: Session, job: BackgroundJob, *, worker_id: str) -> bool:
    """Mark the OWNED RUNNING attempt SUCCEEDED. Returns True on success.

    Ownership-protected: a single atomic conditional UPDATE requires the
    row to still be RUNNING, still leased to ``worker_id``, with an
    UNEXPIRED lease. A stale worker whose lease expired and whose job was
    reclaimed by another worker CANNOT overwrite the replacement attempt
    (returns False; the row is left untouched).
    """
    now = _utcnow()
    result = db.execute(
        update(BackgroundJob)
        .where(BackgroundJob.id == job.id)
        .where(BackgroundJob.status == JobStatus.RUNNING.value)
        .where(BackgroundJob.lease_owner == worker_id)
        .where(BackgroundJob.lease_expires_at.isnot(None))
        .where(BackgroundJob.lease_expires_at > now)
        .values(
            status=JobStatus.SUCCEEDED.value,
            completed_at=now,
            lease_owner=None,
            lease_expires_at=None,
            last_error=None,
        )
    )
    db.commit()
    return (result.rowcount or 0) == 1


def renew_lease(
    db: Session,
    job_id: str,
    *,
    worker_id: str,
    lease_seconds: int,
) -> bool:
    """Extend the OWNED, still-valid lease by ``lease_seconds``. Returns True
    on renewal, False when the caller may no longer renew.

    Ownership-protected exactly like :func:`complete_job`: a single atomic
    conditional UPDATE requires the row to still be ``RUNNING``, still
    leased to ``worker_id``, and its CURRENT lease to be unexpired at
    renewal time. False therefore means one of:

    * the job was reclaimed by another worker after our lease expired, or
    * our lease already expired (another worker may claim it at any
      instant), or
    * the row reached a terminal state.

    A ``False`` return is the stale-worker signal: the caller must stop
    renewing and rely on the ownership-protected transitions; it must
    never overwrite a replacement attempt. The heartbeat machinery lives
    in :func:`_heartbeat_loop`.
    """
    now = _utcnow()
    result = db.execute(
        update(BackgroundJob)
        .where(BackgroundJob.id == job_id)
        .where(BackgroundJob.status == JobStatus.RUNNING.value)
        .where(BackgroundJob.lease_owner == worker_id)
        .where(BackgroundJob.lease_expires_at.isnot(None))
        .where(BackgroundJob.lease_expires_at > now)
        .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
    )
    db.commit()
    return (result.rowcount or 0) == 1


def heartbeat_interval(lease_seconds: int) -> float:
    """Heartbeat period derived from the lease: ~lease/3 with a floor.

    Short leases (typically test-scale) clamp to
    ``_MIN_HEARTBEAT_INTERVAL_SECONDS`` so tests with sub-second leases run
    quickly without busy-looping the renewal thread.
    """
    return max(_MIN_HEARTBEAT_INTERVAL_SECONDS, lease_seconds / 3.0)


def _heartbeat_loop(
    session_factory: Callable[[], Session] | sessionmaker,
    job_id: str,
    *,
    worker_id: str,
    lease_seconds: int,
    stop: threading.Event,
) -> None:
    """Renew the claimed job's lease until ``stop`` is set or ownership is
    lost.

    Runs on its own thread with its own session per renewal (execution work
    NEVER shares the heartbeat's transaction). Each cycle waits
    :func:`heartbeat_interval` and then attempts one renewal. The loop
    exits when ``stop`` is set (normal completion, exception, or worker
    shutdown) or when a renewal reports ownership loss / expiry.

    A database failure during a renewal is logged and NOT treated as a
    successful renewal; ownership safeguards remain the only authority for
    the final outcome. A heartbeat thread is always a daemon, so a worker
    process death cannot leak it or keep it alive.
    """
    interval = heartbeat_interval(lease_seconds)
    while not stop.wait(interval):
        session = session_factory()
        try:
            renewed = renew_lease(
                session, job_id, worker_id=worker_id, lease_seconds=lease_seconds
            )
        except Exception:
            # Renewal failure (transient DB error, connection loss, ...) is
            # logged and retried next cycle. It is NEVER reported as a
            # successful renewal, and it never kills the thread: the
            # ownership-protected transitions remain the final authority.
            logger.warning(
                "heartbeat renewal for job %s failed transiently; the lease "
                "safeguard remains authoritative",
                job_id,
                exc_info=True,
            )
            continue
        finally:
            try:
                session.close()
            except Exception:  # pragma: no cover - defensive
                logger.warning(
                    "heartbeat session failed to close cleanly", exc_info=True
                )
        if not renewed:
            logger.warning(
                "heartbeat for job %s stopped: worker %s no longer owns a "
                "valid lease (reclaimed, expired, or terminal)",
                job_id,
                worker_id,
            )
            return


# Outcomes of a failure transition.
FAIL_RETRIED = "retry"          # rescheduled with backoff by the owning worker
FAIL_DEAD_LETTERED = "dead"     # permanently failed by the owning worker
FAIL_STALE = "stale"            # ownership lost; the row was left untouched


def fail_job(
    db: Session,
    job: BackgroundJob,
    exc: BaseException | str,
    *,
    worker_id: str,
    attempt_count: int | None = None,
) -> str:
    """Handle a failed attempt for the OWNED claim; returns the outcome.

    Ownership-protected exactly like :func:`complete_job`: the transition
    is a single atomic conditional UPDATE (RUNNING + owner + unexpired
    lease). A stale worker cannot mark a replacement attempt failed, clear
    its lease, or overwrite its error state — it gets ``FAIL_STALE`` and
    the row is untouched.

    The attempt was already counted at claim time; ``attempt_count`` may be
    supplied when the caller read it before its session became unusable
    (it only feeds the backoff calculation — ownership comes from the row
    itself). Retryable failures are rescheduled (``FAILED_RETRYABLE`` with
    a future ``available_at``); non-retryable failures become
    ``DEAD_LETTERED`` with the reason and last error preserved.
    """
    policy = policy_from_payload(job.payload)
    message = str(exc)[:2000]
    counted = attempt_count if attempt_count is not None else job.attempt_count
    now = _utcnow()

    _owned = (
        BackgroundJob.id == job.id,
        BackgroundJob.status == JobStatus.RUNNING.value,
        BackgroundJob.lease_owner == worker_id,
        BackgroundJob.lease_expires_at.isnot(None),
        BackgroundJob.lease_expires_at > now,
    )

    if not is_retryable_failure(
        exc if isinstance(exc, BaseException) else JobExecutionError(exc, retryable=False)
    ):
        result = db.execute(
            update(BackgroundJob)
            .where(*_owned)
            .values(
                status=JobStatus.DEAD_LETTERED.value,
                completed_at=now,
                lease_owner=None,
                lease_expires_at=None,
                last_error=message,
                dead_letter_reason=f"non-retryable failure: {type(exc).__name__}",
            )
        )
        db.commit()
        if (result.rowcount or 0) == 1:
            logger.warning("job %s dead-lettered: non-retryable failure", job.id)
            return FAIL_DEAD_LETTERED
        return FAIL_STALE

    delay = backoff_delay_seconds(policy, counted)
    result = db.execute(
        update(BackgroundJob)
        .where(*_owned)
        .values(
            status=JobStatus.FAILED_RETRYABLE.value,
            available_at=now + timedelta(seconds=delay),
            lease_owner=None,
            lease_expires_at=None,
            last_error=message,
        )
    )
    db.commit()
    if (result.rowcount or 0) == 1:
        logger.info(
            "job %s failed (attempt %s); retrying in %.1fs",
            job.id,
            counted,
            delay,
        )
        return FAIL_RETRIED
    return FAIL_STALE


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
    except (TypeError, ValueError) as exc:
        raise JobExecutionError(
            f"job payload is not valid JSON: {exc}", retryable=False
        ) from exc
    if not isinstance(params, dict):
        raise JobExecutionError(
            "job payload must be a JSON object", retryable=False
        )

    # F9: the payload MUST name a valid, non-empty list of known stages.
    # Malformed input dead-letters the job (non-retryable) instead of
    # silently defaulting to full ingestion or reporting false success.
    stages = params.get("stages")
    if not isinstance(stages, list) or not stages:
        raise JobExecutionError(
            "payload 'stages' must be a non-empty list of known stages "
            f"{sorted(_KNOWN_STAGES)}; got {stages!r}",
            retryable=False,
        )
    validated: list[str] = []
    for stage in stages:
        if not isinstance(stage, str) or stage not in _KNOWN_STAGES:
            raise JobExecutionError(
                f"unknown ingestion stage {stage!r}; known stages: "
                f"{sorted(_KNOWN_STAGES)}",
                retryable=False,
            )
        if stage in validated:
            raise JobExecutionError(
                f"duplicate ingestion stage {stage!r} in payload",
                retryable=False,
            )
        validated.append(stage)
    stages = validated

    try:
        concurrency = max(1, min(int(params.get("concurrency", 1)), 100))
    except (TypeError, ValueError) as exc:
        raise JobExecutionError(
            f"payload 'concurrency' must be an integer: {exc}", retryable=False
        ) from exc
    start_raw = params.get("nifty_start_date")
    nifty_start_date = None
    if start_raw:
        from datetime import date as _date

        nifty_start_date = _date.fromisoformat(str(start_raw))
    force = bool(params.get("force", False))

    token_bridge = TokenBridge()
    client = UpstoxClient(token_provider=token_bridge)
    rate_limiter = GlobalRateLimiter(
        config=RateLimiterConfig(initial_concurrency=concurrency, max_concurrency=6)
    )
    orchestrator = BackfillOrchestrator(
        db, client, force=force, rate_limiter=rate_limiter
    )
    # F5: forward the requested concurrency so the option stage's limiter
    # ceiling is the job's request, not the orchestrator default.
    result = asyncio.run(
        orchestrator.run_all(
            stages=list(stages),
            nifty_start_date=nifty_start_date,
            options_concurrency=concurrency,
        )
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


def _execute_one(
    *,
    session_factory: Callable[[], Session] | sessionmaker,
    job_id: str,
    worker_id: str,
    lease_seconds: int | None = None,
) -> str:
    """Execute one claimed job with ownership-protected transitions.

    Failure-isolated: no database problem during lookup, execution, or
    transition may escape to the worker loop. Returns one of
    ``"succeeded"``, ``"retried"``, ``"dead_lettered"``, ``"stale"``
    (ownership lost before completion) or ``"lost"`` (claimed row could
    not be safely re-loaded; its lease expires and recovery reclaims it).

    ``lease_seconds`` is the effective lease claim_next applied for this
    claim (explicit override > payload policy > default); the heartbeat
    renews with exactly this value (F3). While the job runs, a daemon
    heartbeat thread extends the lease every ~lease/3 on its own session,
    so a long execution never expires its own lease. The heartbeat stops
    when execution ends — success or exception — and a crashed process
    stops it by construction (lease expiry stays the recovery path). If
    ownership is lost, renewal reports False and the ownership-protected
    transitions still refuse to touch the row.
    """
    db = session_factory()
    try:
        try:
            job = db.get(BackgroundJob, job_id)
        except Exception as lookup_exc:
            # F6: a lookup failure must never raise UnboundLocalError or
            # kill the worker. Roll back defensively, log, and abandon the
            # attempt — the row is NOT mutated blindly; lease expiry makes
            # it recoverable by another worker.
            try:
                db.rollback()
            except Exception as rollback_exc:  # pragma: no cover - defensive
                logger.warning(
                    "rollback after job %s lookup failure also failed; continuing",
                    job_id,
                    exc_info=rollback_exc,
                )
            logger.warning(
                "could not load claimed job %s; leaving it to lease expiry",
                job_id,
                exc_info=lookup_exc,
            )
            return "lost"

        if job is None:  # pragma: no cover - row vanished mid-flight
            logger.warning("claimed job %s no longer exists", job_id)
            return "lost"

        effective_lease = (
            lease_seconds
            if lease_seconds is not None
            else policy_from_payload(job.payload).lease_seconds
        )
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=_heartbeat_loop,
            args=(session_factory, job_id),
            kwargs={
                "worker_id": worker_id,
                "lease_seconds": effective_lease,
                "stop": stop_heartbeat,
            },
            name=f"heartbeat-{job_id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            execute_job(db, job)
        except Exception as exc:
            # F7: persist the failure transition on a FRESH session via the
            # repository's serialization retry — the execution session may
            # be in a broken transaction state, and a transient error during
            # the transition must not escape and terminate the loop.
            try:
                db.rollback()
            except Exception as rollback_exc:  # pragma: no cover - defensive
                logger.warning(
                    "rollback after execution failure of job %s failed; "
                    "continuing with failure transition",
                    job_id,
                    exc_info=rollback_exc,
                )
            finally:
                db.close()
            try:
                attempt = job.attempt_count  # read before the instance detaches
            except Exception:  # pragma: no cover - session too broken to read
                attempt = None

            def _transition(session: Session) -> str:
                fresh = session.get(BackgroundJob, job_id)
                if fresh is None:  # pragma: no cover - row vanished
                    return FAIL_STALE
                return fail_job(
                    session,
                    fresh,
                    exc,
                    worker_id=worker_id,
                    attempt_count=attempt,
                )

            verdict = retry_on_serialization(_transition, session_factory)
            if verdict == FAIL_DEAD_LETTERED:
                return "dead_lettered"
            if verdict == FAIL_STALE:
                return "stale"
            return "retried"
        finally:
            # The heartbeat stops before this function returns on every
            # path: on success it is stopped before the completion
            # transition below (no renewal races the final update); on
            # failure it keeps the owner's lease alive while the failure
            # transition persists, then stops. No thread or session leaks.
            stop_heartbeat.set()
            heartbeat.join(timeout=10.0)
            if heartbeat.is_alive():  # pragma: no cover - defensive
                logger.warning(
                    "heartbeat thread for job %s did not stop within 10s; "
                    "it is a daemon and cannot block worker shutdown",
                    job_id,
                )

        # Completion retry: the SUCCESS transition gets the same durable
        # treatment as the failure transition. The execution session is
        # closed and the ownership-protected completion runs on a FRESH
        # session through the repository's serialization retry — only the
        # idempotent state transition is retried, never the ingestion work.
        db.close()

        def _complete_transition(session: Session) -> bool:
            fresh = session.get(BackgroundJob, job_id)
            if fresh is None:  # pragma: no cover - row vanished mid-flight
                return False
            return complete_job(session, fresh, worker_id=worker_id)

        owned = retry_on_serialization(_complete_transition, session_factory)
        if owned:
            return "succeeded"
        # F1/F10: our lease expired mid-execution and another worker
        # reclaimed the job. The row belongs to the replacement attempt —
        # the stale outcome is discarded and the row stays untouched.
        logger.warning(
            "worker %s lost ownership of job %s before completion; "
            "discarding stale outcome",
            worker_id,
            job_id,
        )
        return "stale"
    finally:
        try:
            db.close()
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "worker execution session failed to close cleanly", exc_info=True
            )


def run_worker(
    *,
    session_factory: Callable[[], Session] | sessionmaker,
    poll_interval_seconds: float = 5.0,
    once: bool = False,
    worker_id: str | None = None,
    job_type: str | None = None,
    lease_seconds: int | None = None,
    stop: Any = None,
) -> dict[str, int]:
    """Poll-and-drain worker loop.

    Each claim runs on a FRESH session/transaction; a worker process may
    be terminated at any instant — claimed-but-unfinished jobs simply
    keep their lease until it expires and another worker picks them up.
    Nothing is held in memory across restarts.

    ``lease_seconds`` is the explicit worker/CLI override (precedence:
    explicit override > job payload policy > system default).

    ``once=True`` drains until no runnable job remains (used by tests and
    administrative runs). ``stop`` is an optional ``threading.Event``.
    Returns a summary of transitions performed (``stale`` counts outcomes
    discarded because the attempt's lease was lost).
    """
    wid = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    summary = {
        "claimed": 0,
        "succeeded": 0,
        "failed": 0,
        "dead_lettered": 0,
        "stale": 0,
    }

    effective_lease_box: dict[str, int | None] = {}

    def _claim_id(db: Session) -> str | None:
        # Read the id while the claim session is still open: ORM instances
        # expire on commit and cannot be refreshed after the session closes.
        claimed = claim_next(
            db,
            worker_id=wid,
            job_type=job_type,
            lease_seconds=lease_seconds,
        )
        if claimed is None:
            return None
        # F3: remember the effective lease claim_next applied (explicit
        # override > payload policy > default) so execution, heartbeat, and
        # completion all use the SAME value.
        effective_lease_box["lease"] = (
            lease_seconds
            if lease_seconds is not None
            else policy_from_payload(claimed.payload).lease_seconds
        )
        return claimed.id

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
            except Exception:  # pragma: no cover - defensive
                logger.warning(
                    "worker claim session failed to close cleanly", exc_info=True
                )

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
        # Execute on a FRESH session with fully isolated failure semantics;
        # nothing raised here may terminate the loop.
        try:
            outcome = _execute_one(
                session_factory=session_factory,
                job_id=job_id,
                worker_id=wid,
                lease_seconds=effective_lease_box.get("lease"),
            )
        except Exception:  # pragma: no cover - defensive belt-and-braces
            logger.exception("job %s execution failed unexpectedly", job_id)
            continue

        if outcome == "succeeded":
            summary["succeeded"] += 1
        elif outcome == "retried":
            summary["failed"] += 1
        elif outcome == "dead_lettered":
            summary["dead_lettered"] += 1
        elif outcome in ("stale", "lost"):
            summary["stale"] += 1

        if once and stop is None:
            continue  # drain until claim_next returns None
    return summary
