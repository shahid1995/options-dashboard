#!/usr/bin/env python
"""Durable Background Jobs CLI — Day 47.

Administrative boundary for the durable job queue. The queue itself lives
in the application database (``background_jobs`` table); this tool only
enqueues work, reports queue state, or runs the worker loop.

Historical ingestion becomes a durable job::

    # Enqueue a full historical backfill (idempotent per key)
    python run_jobs.py enqueue-backfill --all

    # Enqueue with specific stages / start date
    python run_jobs.py enqueue-backfill --index --options --start-date 2024-01-01

    # Run the worker until the queue is drained
    python run_jobs.py work --once

    # Run the worker as a long-lived process (poll every 5s)
    python run_jobs.py work

    # Inspect the queue, including dead-lettered jobs
    python run_jobs.py status
    python run_jobs.py status --show-failed

The worker executes the real ``BackfillOrchestrator`` through the
application service boundary; jobs survive process restart and are
retried with bounded backoff. Failed jobs land in a dead-letter state
and are never silently deleted.

This tool does NOT deploy a worker service, modify production
configuration, or enable scheduled execution — those require separate
authorization.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

_backend_dir = os.path.dirname(os.path.abspath(__file__))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import _DEFAULT_DB_PATH, normalize_database_url  # noqa: E402
from app.models import BackgroundJob, JobStatus  # noqa: E402
from app.services import background_jobs  # noqa: E402

LIVE_STATUSES = (
    JobStatus.PENDING.value,
    JobStatus.RUNNING.value,
    JobStatus.FAILED_RETRYABLE.value,
)


def _build_stage_list(
    *, all_flag: bool, contracts: bool, index: bool, options: bool
) -> list[str]:
    """Resolve enqueue-backfill stage flags (F4, deterministic).

    Individual stage flags COMBINE (``--index --options`` runs both).
    ``--all`` is the superset: when present it replaces any individual
    flags rather than intersecting or erroring. Empty selection means the
    caller must reject the invocation.
    """
    stages = []
    if contracts:
        stages.append("contracts")
    if index:
        stages.append("nifty")
    if options:
        stages.append("options")
    if all_flag:
        stages = ["contracts", "nifty", "options"]
    return stages


def _get_session_factory():
    """Build a session factory for the configured database.

    Alembic is the sole schema authority (ADR-002): this CLI NEVER creates
    or mutates schema — no ``create_all``, no implicit migration run. It
    assumes the target database has already been migrated (e.g. via the
    application's serialized startup path or ``alembic upgrade head``).
    Against an uninitialized database the CLI fails with a normal
    database/schema error, which is the intended fail-closed behavior.
    """
    url = settings.DATABASE_URL or f"sqlite:///{_DEFAULT_DB_PATH}"
    # F3: use the application's canonical URL normalization — no duplicated
    # parsing. ``postgres://`` and ``postgresql://`` map to the psycopg 3
    # dialect exactly like every other application entry point; SQLite and
    # explicit-driver URLs pass through unchanged.
    url = normalize_database_url(url)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _print_status(SessionLocal, show_failed: bool) -> None:
    db = SessionLocal()
    try:
        # F2: aggregate WITH an explicit GROUP BY — PostgreSQL and
        # CockroachDB reject a bare aggregate/select mix.
        counts = dict(
            db.execute(
                select(BackgroundJob.status, func.count(BackgroundJob.id))
                .group_by(BackgroundJob.status)
            ).all()
        )
        total = sum(counts.values())
        print(f"Background jobs: {total} total")
        for status in JobStatus:
            if counts.get(status.value):
                print(f"  {status.value:<16} {counts[status.value]}")
        next_up = (
            db.execute(
                select(BackgroundJob)
                .where(
                    BackgroundJob.status.in_(LIVE_STATUSES),
                    BackgroundJob.available_at <= func.now(),
                )
                .order_by(BackgroundJob.available_at.asc())
                .limit(5)
            )
            .scalars()
            .all()
        )
        if next_up:
            print("Next runnable:")
            for job in next_up:
                print(
                    f"  {job.id[:8]}  {job.job_type}  attempts={job.attempt_count}"
                    f"  key={job.idempotency_key}"
                )
        if show_failed:
            dead = (
                db.execute(
                    select(BackgroundJob)
                    .where(BackgroundJob.status == JobStatus.DEAD_LETTERED.value)
                    .order_by(BackgroundJob.completed_at.desc())
                    .limit(10)
                )
                .scalars()
                .all()
            )
            if dead:
                print("Dead-lettered (most recent):")
                for job in dead:
                    print(f"  {job.id[:8]}  {job.dead_letter_reason}")
                    if job.last_error:
                        print(f"    last error: {job.last_error[:160]}")
    finally:
        db.close()


def _cmd_enqueue_backfill(args) -> int:
    SessionLocal = _get_session_factory()
    db = SessionLocal()
    try:
        stages = _build_stage_list(
            all_flag=args.all,
            contracts=args.contracts,
            index=args.index,
            options=args.options,
        )
        if not stages:
            print("ERROR: specify --all and/or --contracts, --index, --options")
            return 1

        payload = {"stages": stages, "concurrency": args.concurrency}
        if args.start_date:
            payload["nifty_start_date"] = args.start_date
        if args.force:
            payload["force"] = True

        key_parts = ["backfill", "+".join(stages)]
        key_parts.append(args.start_date or "full-range")
        if args.force:
            key_parts.append("force")
        idempotency_key = ":".join(key_parts)

        job, created = background_jobs.enqueue(
            db,
            job_type="HISTORICAL_INGESTION",
            idempotency_key=idempotency_key,
            payload=payload,
        )
        verb = "enqueued" if created else "already enqueued (idempotent)"
        print(f"Job {verb}: {job.id}")
        print(f"  idempotency key: {job.idempotency_key}")
        print(f"  status: {job.status}  attempts: {job.attempt_count}")
        print(f"  payload: {job.payload}")
        return 0
    finally:
        db.close()


def _cmd_work(args) -> int:
    SessionLocal = _get_session_factory()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    summary = background_jobs.run_worker(
        session_factory=SessionLocal,
        poll_interval_seconds=args.poll_interval,
        once=args.once,
        job_type=args.job_type,
        lease_seconds=args.lease_seconds,
    )
    print(
        f"Worker done: claimed={summary['claimed']} "
        f"succeeded={summary['succeeded']} "
        f"retry-scheduled={summary['failed']} "
        f"dead-lettered={summary['dead_lettered']}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Durable background jobs — Day 47",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_enqueue = sub.add_parser(
        "enqueue-backfill", help="Enqueue a historical-ingestion job (idempotent)"
    )
    # F4: stages combine; at least one must be selected.
    p_enqueue.add_argument("--all", action="store_true", help="All backfill stages")
    p_enqueue.add_argument("--contracts", action="store_true", help="Contract metadata stage")
    p_enqueue.add_argument("--index", action="store_true", help="NIFTY index candles stage")
    p_enqueue.add_argument("--options", action="store_true", help="Option candles stage")
    p_enqueue.add_argument("--start-date", type=str, help="NIFTY backfill start (YYYY-MM-DD)")
    p_enqueue.add_argument("--force", action="store_true", help="Force re-download")
    p_enqueue.add_argument("--concurrency", type=int, default=1, help="API concurrency")
    p_enqueue.set_defaults(func=_cmd_enqueue_backfill)

    p_work = sub.add_parser("work", help="Run the job worker loop")
    p_work.add_argument(
        "--once", action="store_true", help="Drain until no runnable job remains"
    )
    p_work.add_argument(
        "--poll-interval", type=float, default=5.0, help="Seconds between polls"
    )
    p_work.add_argument("--job-type", type=str, default=None, help="Restrict to a job type")
    p_work.add_argument(
        "--lease-seconds",
        type=int,
        default=None,
        help="Lease duration override (default: job payload policy, else "
        "system default) — omitting it preserves the documented lease "
        "precedence",
    )
    p_work.set_defaults(func=_cmd_work)

    p_status = sub.add_parser("status", help="Show queue state")
    p_status.add_argument(
        "--show-failed", action="store_true", help="Include dead-letter details"
    )
    p_status.set_defaults(func=lambda a: (_print_status(_get_session_factory(), a.show_failed), 0)[1])

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
