"""The RC retry writer: four attempts, then exhaustion, plus refusals.

Each test runs against a real migrated SQLite database with foreign keys
enforced, matching what ``app/db.py`` now configures at runtime.
"""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.services.email_delivery_retry import (
    MAX_ATTEMPTS,
    RetryRefused,
    retry_delivery,
)

BACKEND = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Run the same suite against PostgreSQL by pointing this at a superuser-capable
# database (each test gets its own throwaway database):
#   RC_TEST_DATABASE_URL=postgresql+psycopg://postgres:@/postgres?host=/tmp/pgdata
PG_URL = os.environ.get("RC_TEST_DATABASE_URL")


def _uid() -> str:
    return uuid.uuid4().hex


def _upgrade(eng, url):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    # alembic.ini values go through configparser, so a URL-encoded host
    # (percent escapes in a socket path) must be escaped.
    cfg.set_main_option("sqlalchemy.url", str(url).replace("%", "%%"))
    cfg.attributes["connectable"] = eng
    command.upgrade(cfg, "head")


def _sqlite_engine(tmp_path):
    url = f"sqlite:///{tmp_path / 'delivery.db'}"
    eng = create_engine(url, future=True)

    @event.listens_for(eng, "connect")
    def _enforce_fk(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    _upgrade(eng, url)
    return eng


@pytest.fixture()
def engine(tmp_path):
    if not PG_URL:
        eng = _sqlite_engine(tmp_path)
        try:
            yield eng
        finally:
            eng.dispose()
        return

    base = make_url(PG_URL)
    name = f"rc_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name)
    eng = create_engine(url, future=True)
    try:
        _upgrade(eng, url.render_as_string(hide_password=False))
        yield eng
    finally:
        eng.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def seed_delivery(eng, *, lease_offset_seconds=-60, payload_offset_seconds=86400):
    """A delivery claimed by one worker, mid-attempt."""
    delivery, user, principal = _uid(), _uid(), _uid()
    generation, attempt = _uid(), _uid()
    now = BASE
    lease = BASE + timedelta(seconds=lease_offset_seconds)
    payload_expiry = BASE + timedelta(seconds=payload_offset_seconds)

    with eng.begin() as conn:
        conn.execute(text(
            "insert into email_deliveries (id,user_id,principal_id,status,"
            "payload_encrypted,payload_expires_at,created_at,updated_at) "
            "values (:d,:u,:p,'pending',:pl,:px,:t,:t)"
        ), dict(d=delivery, u=user, p=principal, pl=b"payload",
                px=payload_expiry.isoformat(), t=now.isoformat()))
        conn.execute(text(
            "insert into email_delivery_generations (id,delivery_id,generation,created_at) "
            "values (:g,:d,1,:t)"
        ), dict(g=generation, d=delivery, t=now.isoformat()))
        conn.execute(text(
            "update email_deliveries set active_generation_id=:g where id=:d"
        ), dict(g=generation, d=delivery))
        conn.execute(text(
            "insert into email_delivery_attempts (id,delivery_id,user_id,principal_id,"
            "generation_id,attempt_index,execution_disposition,reservation_id,"
            "claim_token,claim_source,original_call_reserved_at,"
            "original_lease_expires_at,created_at) "
            "values (:a,:d,:u,:p,:g,1,'reserved','res-1','tok-1','worker-1',:t,:lease,:t)"
        ), dict(a=attempt, d=delivery, u=user, p=principal, g=generation,
                t=now.isoformat(), lease=lease.isoformat()))
        conn.execute(text(
            "update email_deliveries set status='sending', claim_token='tok-1',"
            " claim_source='worker-1', reservation_id='res-1', call_reserved_at=:t,"
            " lease_expires_at=:lease where id=:d"
        ), dict(d=delivery, t=now.isoformat(), lease=lease.isoformat()))

    return dict(delivery=delivery, user=user, principal=principal,
                generation=generation, attempt=attempt)


def mark(eng, attempt_id, outcome):
    """Record a worker completion for one attempt."""
    now = BASE.isoformat()
    with eng.begin() as conn:
        conn.execute(text(
            "update email_delivery_attempts set execution_disposition='execution_completed',"
            " provider_outcome=:o, payload_released_at=:t, dispatch_marker_at=:t,"
            " finalized_at=:t where id=:a"
        ), dict(o=outcome, a=attempt_id, t=now))


def expire_lease(eng, delivery_id, when):
    """The worker finished and its lease has since lapsed."""
    with eng.begin() as conn:
        conn.execute(text(
            "update email_deliveries set lease_expires_at=:l where id=:d"
        ), dict(l=when.isoformat(), d=delivery_id))


def retry(eng, seeded, *, step, claim_token=None, claim_source="worker", reservation=None):
    n = step
    return retry_delivery(
        eng,
        delivery_id=seeded["delivery"],
        user_id=seeded["user"],
        principal_id=seeded["principal"],
        now=BASE + timedelta(seconds=10 * n),
        new_generation_id=_uid(),
        new_attempt_id=_uid(),
        claim_token=claim_token or f"tok-{n}",
        claim_source=claim_source,
        reservation_id=reservation or f"res-{n}",
        lease_until=BASE + timedelta(seconds=10 * n + 60),
    )


def attempts_of(eng, delivery_id):
    with eng.connect() as conn:
        return conn.execute(text(
            "select id, generation_id, attempt_index, execution_disposition,"
            " provider_outcome, claim_token, reservation_id"
            " from email_delivery_attempts where delivery_id=:d order by attempt_index"
        ), dict(d=delivery_id)).mappings().all()


def generations_of(eng, delivery_id):
    with eng.connect() as conn:
        return conn.execute(text(
            "select id, generation from email_delivery_generations"
            " where delivery_id=:d order by generation"
        ), dict(d=delivery_id)).mappings().all()


def delivery_of(eng, delivery_id):
    with eng.connect() as conn:
        return conn.execute(text(
            "select status, claim_token, reservation_id, active_generation_id,"
            " finalized_at, failure_code, payload_encrypted"
            " from email_deliveries where id=:d"
        ), dict(d=delivery_id)).mappings().one()


# ---------------------------------------------------------------------------
# the main path
# ---------------------------------------------------------------------------
def test_retry_drives_all_four_attempts_then_fails(engine):
    seeded = seed_delivery(engine)

    for step in range(1, MAX_ATTEMPTS):
        current = attempts_of(engine, seeded["delivery"])[-1]
        mark(engine, current["id"], "rejected_retryable")
        expire_lease(engine, seeded["delivery"],
                     BASE + timedelta(seconds=10 * step - 5))

        outcome = retry(engine, seeded, step=step)

        assert outcome.kind == "claimed"
        assert outcome.attempt_index == step + 1
        assert outcome.generation == step + 1

        rows = attempts_of(engine, seeded["delivery"])
        assert len(rows) == step + 1
        assert [r["attempt_index"] for r in rows] == list(range(1, step + 2))

        delivery = delivery_of(engine, seeded["delivery"])
        assert delivery["status"] == "sending"
        assert delivery["active_generation_id"] == rows[-1]["generation_id"]
        # the new attempt carries the same claim as its delivery
        assert rows[-1]["claim_token"] == delivery["claim_token"]
        assert rows[-1]["reservation_id"] == delivery["reservation_id"]
        # attempt n owns generation n, and generations are numbered from 1
        gens = generations_of(engine, seeded["delivery"])
        assert [g["generation"] for g in gens] == list(range(1, step + 2))
        assert [r["generation_id"] for r in rows] == [g["id"] for g in gens]
        assert len({r["generation_id"] for r in rows}) == len(rows)

    # budget exhausted: the fourth attempt fails, it does not retry
    mark(engine, attempts_of(engine, seeded["delivery"])[-1]["id"], "rejected_retryable")
    expire_lease(engine, seeded["delivery"],
                 BASE + timedelta(seconds=10 * MAX_ATTEMPTS - 5))
    exhausted = retry(engine, seeded, step=MAX_ATTEMPTS)

    assert exhausted.kind == "failed"
    assert len(attempts_of(engine, seeded["delivery"])) == MAX_ATTEMPTS

    delivery = delivery_of(engine, seeded["delivery"])
    assert delivery["status"] == "failed"
    assert delivery["failure_code"] == "retry_budget_exhausted"
    assert delivery["finalized_at"] is not None
    assert delivery["claim_token"] is None
    assert delivery["payload_encrypted"] is None


def test_every_retry_advances_the_pointer_and_keeps_generations_distinct(engine):
    seeded = seed_delivery(engine)
    seen = [seeded["generation"]]

    for step in range(1, MAX_ATTEMPTS):
        mark(engine, attempts_of(engine, seeded["delivery"])[-1]["id"], "rejected_retryable")
        expire_lease(engine, seeded["delivery"],
                     BASE + timedelta(seconds=10 * step - 5))
        retry(engine, seeded, step=step)
        seen.append(delivery_of(engine, seeded["delivery"])["active_generation_id"])

    assert len(set(seen)) == len(seen) == MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------
def test_live_lease_refuses_retry(engine):
    seeded = seed_delivery(engine, lease_offset_seconds=600)
    mark(engine, seeded["attempt"], "rejected_retryable")

    with pytest.raises(RetryRefused) as exc:
        retry(engine, seeded, step=1)
    assert exc.value.disposition == "abort_live_lease"
    assert len(attempts_of(engine, seeded["delivery"])) == 1


def test_accepted_delivery_is_not_retried(engine):
    seeded = seed_delivery(engine)
    mark(engine, seeded["attempt"], "accepted")

    with pytest.raises(RetryRefused) as exc:
        retry(engine, seeded, step=1)
    assert exc.value.disposition == "abort_accepted_present"
    assert delivery_of(engine, seeded["delivery"])["status"] == "sending"


def test_permanent_rejection_is_not_retried(engine):
    seeded = seed_delivery(engine)
    mark(engine, seeded["attempt"], "rejected_permanent")

    with pytest.raises(RetryRefused) as exc:
        retry(engine, seeded, step=1)
    assert exc.value.disposition == "abort_permanent_attempt"


def test_unresolved_sibling_blocks_retry(engine):
    seeded = seed_delivery(engine)
    mark(engine, seeded["attempt"], "rejected_retryable")
    sibling_generation, sibling_attempt = _uid(), _uid()
    with engine.begin() as conn:
        conn.execute(text(
            "insert into email_delivery_generations (id,delivery_id,generation,created_at)"
            " values (:g,:d,2,:t)"
        ), dict(g=sibling_generation, d=seeded["delivery"], t=BASE.isoformat()))
        conn.execute(text(
            "insert into email_delivery_attempts (id,delivery_id,user_id,principal_id,"
            "generation_id,attempt_index,execution_disposition,created_at)"
            " values (:a,:d,:u,:p,:g,2,'execution_unknown',:t)"
        ), dict(a=sibling_attempt, d=seeded["delivery"], u=seeded["user"],
                p=seeded["principal"], g=sibling_generation, t=BASE.isoformat()))

    with pytest.raises(RetryRefused) as exc:
        retry(engine, seeded, step=1)
    assert exc.value.disposition == "abort_unresolved_sibling"


def test_claim_arguments_are_required(engine):
    seeded = seed_delivery(engine)
    mark(engine, seeded["attempt"], "rejected_retryable")
    expire_lease(engine, seeded["delivery"], BASE + timedelta(seconds=5))

    with pytest.raises(RetryRefused) as exc:
        retry_delivery(
            engine,
            delivery_id=seeded["delivery"],
            user_id=seeded["user"],
            principal_id=seeded["principal"],
            now=BASE + timedelta(seconds=10),
            new_generation_id=_uid(),
            new_attempt_id=_uid(),
            claim_token="",  # missing
            claim_source="worker",
            reservation_id=None,  # missing
            lease_until=BASE + timedelta(seconds=70),
        )
    assert exc.value.disposition == "abort_invalid_arguments"
    assert exc.value.detail["missing"] == ["claim_token", "reservation_id"]
    # nothing was written
    assert len(attempts_of(engine, seeded["delivery"])) == 1
    assert delivery_of(engine, seeded["delivery"])["status"] == "sending"


def test_expired_payload_blocks_retry(engine):
    seeded = seed_delivery(engine, payload_offset_seconds=-3600)
    mark(engine, seeded["attempt"], "rejected_retryable")

    with pytest.raises(RetryRefused) as exc:
        retry(engine, seeded, step=1)
    assert exc.value.disposition == "abort_expired_payload"


# ---------------------------------------------------------------------------
# the schema the writer depends on
# ---------------------------------------------------------------------------
def test_cross_delivery_generation_is_rejected_by_the_database(engine):
    seeded = seed_delivery(engine)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text(
                "insert into email_delivery_attempts (id,delivery_id,user_id,principal_id,"
                "generation_id,attempt_index,execution_disposition,created_at)"
                " values (:a,:d,:u,:p,:g,2,'reserved',:t)"
            ), dict(a=_uid(), d=seeded["delivery"], u=seeded["user"],
                    p=seeded["principal"], g=_uid(), t=BASE.isoformat()))
