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
from app.db import Base, _DEFAULT_DB_PATH  # noqa: E402
from app.models import BackgroundJob, JobStatus  # noqa: E402
from app.services import background_jobs  # noqa: E402

LIVE_STATUSES = (
    JobStatus.PENDING.value,
    JobStatus.RUNNING.value,
    JobStatus.FAILED_RETRYABLE.value,
)


def _get_session_factory():
    url = settings.DATABASE_URL or f"sqlite:///{_DEFAULT_DB_PATH}"
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _print_status(SessionLocal, show_failed: bool) -> None:
    db = SessionLocal()
    try:
        counts = dict(
            db.execute(
                select(BackgroundJob.status, func.count(BackgroundJob.id))
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
        if args.all:
            stages = ["contracts", "nifty", "options"]
        else:
            stages = []
            if args.contracts:
                stages.append("contracts")
            if args.index:
                stages.append("nifty")
            if args.options:
                stages.append("options")
        if not stages:
            print("ERROR: specify --all, --contracts, --index, or --options")
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
    mode = p_enqueue.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all", action="store_true", help="All backfill stages")
    mode.add_argument("--contracts", action="store_true", help="Contract metadata stage")
    mode.add_argument("--index", action="store_true", help="NIFTY index candles stage")
    mode.add_argument("--options", action="store_true", help="Option candles stage")
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
        "--lease-seconds", type=int, default=background_jobs.DEFAULT_LEASE_SECONDS,
        help="Lease duration per claim",
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
