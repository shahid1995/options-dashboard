"""RC certification: concurrency, rollback, argument validation, provenance.

Tests only -- no production code is imported beyond the public writer and its
documented refusal. Predecessor attempts in these tests are written by
``mark()`` (a direct fixture write of a worker-completion row), not by a real
worker execution path.

Run against SQLite by default, or against PostgreSQL with
``RC_TEST_DATABASE_URL`` set (see ``test_email_delivery_retry``).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, text

from app.services.email_delivery_retry import MAX_ATTEMPTS, RetryOutcome, RetryRefused, retry_delivery

from tests.test_email_delivery_retry import (  # same test package
    BASE,
    _uid,
    attempts_of,
    delivery_of,
    engine,  # re-exported fixture: migrated SQLite or PostgreSQL, per test
    expire_lease,
    generations_of,
    mark,
    retry,
    seed_delivery,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def expired_retryable(eng):
    """A delivery whose last attempt ended retryable and whose lease has lapsed.

    The predecessor row is written by ``mark()`` -- a fixture write -- not by a
    worker run.
    """
    seeded = seed_delivery(eng)
    mark(eng, seeded["attempt"], "rejected_retryable")
    expire_lease(eng, seeded["delivery"], BASE - timedelta(seconds=1))
    return seeded


def attempt_retry(eng, seeded, **overrides):
    kwargs = dict(
        delivery_id=seeded["delivery"],
        user_id=seeded["user"],
        principal_id=seeded["principal"],
        now=BASE + timedelta(seconds=10),
        new_generation_id=_uid(),
        new_attempt_id=_uid(),
        claim_token="tok-1",
        claim_source="worker-1",
        reservation_id="res-1",
        lease_until=BASE + timedelta(seconds=70),
    )
    kwargs.update(overrides)
    return retry_delivery(eng, **kwargs)


def snapshot(eng, delivery_id):
    """Every persisted row the retry can touch, plus schema-level invariants."""
    with eng.connect() as conn:
        d = conn.execute(text(
            "select status, claim_token, claim_source, reservation_id,"
            " call_reserved_at, lease_expires_at, active_generation_id,"
            " finalized_at, failure_code, payload_encrypted, updated_at"
            " from email_deliveries where id=:d"
        ), dict(d=delivery_id)).mappings().one()
        gens = conn.execute(text(
            "select id, generation, created_at from email_delivery_generations"
            " where delivery_id=:d order by generation"
        ), dict(d=delivery_id)).all()
        atts = conn.execute(text(
            "select id, generation_id, attempt_index, execution_disposition,"
            " provider_outcome, reservation_id, claim_token, claim_source,"
            " payload_released_at, dispatch_marker_at, finalized_at"
            " from email_delivery_attempts where delivery_id=:d order by attempt_index"
        ), dict(d=delivery_id)).all()
        orphans = conn.execute(text(
            "select count(*) from email_delivery_attempts a"
            " left join email_delivery_generations g on g.id = a.generation_id"
            " where a.delivery_id=:d and g.id is null"
        ), dict(d=delivery_id)).scalar_one()
        dangling_pointer = conn.execute(text(
            "select count(*) from email_deliveries d"
            " left join email_delivery_generations g on g.id = d.active_generation_id"
            " where d.id=:d and d.active_generation_id is not null and g.id is null"
        ), dict(d=delivery_id)).scalar_one()
        fk = []
        if conn.dialect.name == "sqlite":
            fk = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()

    return {
        "delivery": {k: (bytes(v) if isinstance(v, (bytearray, memoryview)) else v)
                     for k, v in d.items()},
        "generations": [tuple(g) for g in gens],
        "attempts": [tuple(a) for a in atts],
        "orphan_attempts": orphans,
        "dangling_pointer": dangling_pointer,
        "fk_violations": len(fk),
    }


def assert_unchanged(before, after):
    assert after == before


def _norm(value):
    """Compare ids across engines.

    The writer echoes the caller's string ids, while PostgreSQL stores and
    returns native ``uuid.UUID`` values; both forms describe the same id.
    """
    return uuid.UUID(str(value))


# ---------------------------------------------------------------------------
# 1a. SQLite concurrency: BEGIN IMMEDIATE before the first read
# ---------------------------------------------------------------------------
def test_sqlite_begin_immediate_precedes_the_first_delivery_read(engine):
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite-specific statement ordering")

    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        seeded = expired_retryable(engine)
        retry(engine, seeded, step=1)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    # The pool may hand back a connection used by the seed writes, so the
    # transaction boundary is the last BEGIN IMMEDIATE, not statements[0].
    begins = [i for i, s in enumerate(statements) if s.upper() == "BEGIN IMMEDIATE"]
    assert begins, f"no BEGIN IMMEDIATE issued: {statements[:3]}"
    window = statements[begins[-1] + 1:]
    assert window, "nothing ran inside the write transaction"
    assert window[0].lower().lstrip().startswith("select"), window[0]
    assert "from email_deliveries" in window[0].lower(), window[0]
    print("\nSQLite write-order:", json.dumps([statements[begins[-1]]] + window[:3], indent=2))


def test_sqlite_second_writer_blocks_on_the_first(engine):
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite-specific write lock")

    seeded = expired_retryable(engine)
    held = engine.connect()
    held.exec_driver_sql("BEGIN IMMEDIATE")
    held.execute(text("update email_deliveries set updated_at=updated_at where id=:d"),
                 {"d": seeded["delivery"]})

    outcome: dict = {}

    def _run():
        try:
            outcome["value"] = retry(engine, seeded, step=1)
        except Exception as exc:  # recorded, asserted below
            outcome["error"] = exc

    worker = threading.Thread(target=_run, name="rc-concurrent-writer")
    worker.start()
    try:
        time.sleep(0.5)
        assert worker.is_alive(), f"second writer did not block: {outcome}"
        # the blocked writer has written nothing yet
        assert len(attempts_of(engine, seeded["delivery"])) == 1
    finally:
        held.rollback()
        held.close()

    worker.join(timeout=30)
    assert not worker.is_alive(), "writer did not finish after the lock was released"
    assert "error" not in outcome, outcome
    assert isinstance(outcome["value"], RetryOutcome)


def test_two_concurrent_retries_create_exactly_one_successor(engine):
    """Two connections race the same delivery: one successor, one refusal."""
    seeded = expired_retryable(engine)
    before = snapshot(engine, seeded["delivery"])
    barrier = threading.Barrier(2)
    results: list = []

    def _run(step):
        barrier.wait()
        try:
            results.append(("claimed", retry(engine, seeded, step=step)))
        except RetryRefused as exc:
            results.append(("refused", exc.disposition))
        except Exception as exc:  # pragma: no cover - surfaces as a failure
            results.append(("error", repr(exc)))

    threads = [
        threading.Thread(target=_run, args=(1,), name="rc-writer-a"),
        threading.Thread(target=_run, args=(2,), name="rc-writer-b"),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads)

    kinds = [r[0] for r in results]
    assert kinds.count("claimed") == 1, results
    assert kinds.count("refused") == 1, results
    assert [r[1] for r in results if r[0] == "refused"] == ["abort_live_lease"]

    after = snapshot(engine, seeded["delivery"])
    assert len(after["generations"]) == len(before["generations"]) + 1
    assert len(after["attempts"]) == len(before["attempts"]) + 1
    assert after["orphan_attempts"] == 0
    assert after["dangling_pointer"] == 0
    assert after["fk_violations"] == 0

    winner = results[[r[0] for r in results].index("claimed")][1]
    delivery = delivery_of(engine, seeded["delivery"])
    active = [a for a in after["attempts"] if _norm(a[0]) == _norm(winner.attempt_id)][0]
    assert _norm(delivery["active_generation_id"]) == _norm(winner.generation_id)
    assert _norm(active[1]) == _norm(winner.generation_id)
    assert active[6] == delivery["claim_token"]


# ---------------------------------------------------------------------------
# 1b. PostgreSQL concurrency: FOR UPDATE serializes the writers
# ---------------------------------------------------------------------------
def test_postgres_for_update_serializes_writers(engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row-lock path")

    seeded = expired_retryable(engine)
    blocker = engine.connect()
    blocker.execute(text("select id from email_deliveries where id=:d for update"),
                    {"d": seeded["delivery"]})

    outcome: dict = {}

    def _run():
        try:
            outcome["value"] = retry(engine, seeded, step=1)
        except Exception as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=_run, name="rc-pg-writer")
    worker.start()
    try:
        time.sleep(0.5)
        assert worker.is_alive(), f"FOR UPDATE did not block the second writer: {outcome}"
        with engine.connect() as probe:
            waiting = probe.execute(text("""
                select query, wait_event_type, wait_event
                from pg_stat_activity
                where state = 'active'
                  and pid <> pg_backend_pid()
                  and query ilike '%FOR UPDATE%'
            """)).mappings().all()
        assert waiting, "no waiting statement found in pg_stat_activity"
        assert any(r["wait_event_type"] == "Lock" for r in waiting), waiting
        print("\npg_stat_activity while blocked:", json.dumps(
            [dict(r) for r in waiting], indent=2, default=str))
    finally:
        blocker.rollback()
        blocker.close()

    worker.join(timeout=30)
    assert not worker.is_alive()
    assert "error" not in outcome, outcome
    assert isinstance(outcome["value"], RetryOutcome)


# ---------------------------------------------------------------------------
# 2. rollback injection after each branch A stage
# ---------------------------------------------------------------------------
class InjectedFailure(RuntimeError):
    pass


# Matched against the *compiled* statement, so there must be no bind names
# here: sqlite uses ``?`` while psycopg uses ``%(name)s``.
STAGE_MARKERS = {
    "A0_claim_release": ["update email_deliveries set status = 'pending'"],
    "A2_generation_insert": ["insert into email_delivery_generations"],
    "A3_pointer_update": ["update email_deliveries set active_generation_id = "],
    "A4_attempt_insert": ["insert into email_delivery_attempts"],
    "A5_reclaim": ["update email_deliveries set status = 'sending'"],
}


@pytest.mark.parametrize("stage", list(STAGE_MARKERS))
def test_rollback_after_stage_leaves_state_untouched(engine, stage):
    seeded = expired_retryable(engine)
    before = snapshot(engine, seeded["delivery"])
    executed: list[str] = []
    needles = STAGE_MARKERS[stage]

    def _boom(conn, cursor, statement, parameters, context, executemany):
        flat = " ".join(statement.split()).lower()
        if all(n in flat for n in needles):
            executed.append(flat[:80])
            raise InjectedFailure(stage)

    event.listen(engine, "after_cursor_execute", _boom)
    try:
        with pytest.raises(InjectedFailure):
            retry(engine, seeded, step=1)
    finally:
        event.remove(engine, "after_cursor_execute", _boom)

    assert executed, f"stage {stage} never executed; injection proved nothing"
    after = snapshot(engine, seeded["delivery"])
    assert_unchanged(before, after)
    assert after["delivery"]["status"] == "sending"
    assert after["delivery"]["claim_token"] == before["delivery"]["claim_token"]
    assert after["delivery"]["reservation_id"] == before["delivery"]["reservation_id"]
    assert after["delivery"]["active_generation_id"] == before["delivery"]["active_generation_id"]
    assert len(after["generations"]) == len(before["generations"])
    assert len(after["attempts"]) == len(before["attempts"])
    assert after["fk_violations"] == 0
    assert after["orphan_attempts"] == 0
    print(f"\n[{stage}] executed={executed[0]!r}",
          f"status={after['delivery']['status']}",
          f"claim={after['delivery']['claim_token']!r}",
          f"reservation={after['delivery']['reservation_id']!r}",
          f"pointer={after['delivery']['active_generation_id']}",
          f"generations={len(after['generations'])}",
          f"attempts={len(after['attempts'])}",
          f"fk={after['fk_violations']} orphans={after['orphan_attempts']}",
          f"unchanged={after == before}")


# ---------------------------------------------------------------------------
# 3. required argument validation
# ---------------------------------------------------------------------------
STRING_FIELDS = [
    "new_generation_id",
    "new_attempt_id",
    "claim_token",
    "claim_source",
    "reservation_id",
]


@pytest.mark.parametrize("field", STRING_FIELDS)
@pytest.mark.parametrize("bad", [None, ""])
def test_missing_or_empty_required_argument_is_refused(engine, field, bad):
    seeded = expired_retryable(engine)
    before = snapshot(engine, seeded["delivery"])

    with pytest.raises(RetryRefused) as exc:
        attempt_retry(engine, seeded, **{field: bad})

    assert exc.value.disposition == "abort_invalid_arguments"
    assert field in exc.value.detail["missing"]
    assert_unchanged(before, snapshot(engine, seeded["delivery"]))


@pytest.mark.parametrize("field", STRING_FIELDS)
def test_whitespace_only_required_argument_is_refused(engine, field):
    seeded = expired_retryable(engine)
    before = snapshot(engine, seeded["delivery"])

    with pytest.raises(RetryRefused) as exc:
        attempt_retry(engine, seeded, **{field: "   "})

    assert exc.value.disposition == "abort_invalid_arguments"
    assert field in exc.value.detail["missing"]
    assert_unchanged(before, snapshot(engine, seeded["delivery"]))


def test_null_lease_deadline_is_refused(engine):
    seeded = expired_retryable(engine)
    before = snapshot(engine, seeded["delivery"])

    with pytest.raises(RetryRefused) as exc:
        attempt_retry(engine, seeded, lease_until=None)

    assert exc.value.disposition == "abort_invalid_arguments"
    assert "lease_until" in exc.value.detail["missing"]
    assert_unchanged(before, snapshot(engine, seeded["delivery"]))


@pytest.mark.parametrize("offset", [0, -1, -3600])
def test_lease_deadline_must_be_in_the_future(engine, offset):
    seeded = expired_retryable(engine)
    before = snapshot(engine, seeded["delivery"])
    now = BASE + timedelta(seconds=10)

    with pytest.raises(RetryRefused) as exc:
        attempt_retry(engine, seeded, now=now,
                      lease_until=now + timedelta(seconds=offset))

    assert exc.value.disposition == "abort_invalid_arguments"
    assert exc.value.detail["reason"] == "lease_not_in_future"
    assert_unchanged(before, snapshot(engine, seeded["delivery"]))


# ---------------------------------------------------------------------------
# 4. predecessor provenance across the four-attempt progression
# ---------------------------------------------------------------------------
def test_four_attempt_progression_records_predecessor_provenance(engine):
    seeded = expired_retryable(engine)
    records: list[dict] = []

    for step in range(1, MAX_ATTEMPTS):
        with engine.connect() as conn:
            d = conn.execute(text(
                "select status, claim_token, reservation_id, lease_expires_at,"
                " active_generation_id from email_deliveries where id=:d"
            ), dict(d=seeded["delivery"])).mappings().one()
        pred = attempts_of(engine, seeded["delivery"])[-1]
        assert pred["generation_id"] == d["active_generation_id"]

        records.append({
            "step": step,
            "delivery_status": d["status"],
            "claim_token": d["claim_token"],
            "reservation_id": d["reservation_id"],
            "lease_expires_at": str(d["lease_expires_at"]),
            "active_generation": d["active_generation_id"],
            "predecessor_attempt_id": pred["id"],
            "predecessor_attempt_index": pred["attempt_index"],
            "execution_disposition": pred["execution_disposition"],
            "provider_outcome": pred["provider_outcome"],
            "predecessor_source": "fixture-seeded worker-completion row",
        })

        outcome = retry(engine, seeded, step=step)
        assert outcome.kind == "claimed"
        delivery = delivery_of(engine, seeded["delivery"])
        assert _norm(delivery["active_generation_id"]) == _norm(outcome.generation_id)
        assert _norm(delivery["active_generation_id"]) != _norm(d["active_generation_id"])

        mark(engine, outcome.attempt_id, "rejected_retryable")
        expire_lease(engine, seeded["delivery"],
                     BASE + timedelta(seconds=10 * step + 1))

    # the fourth attempt exhausts the budget
    with engine.connect() as conn:
        d = conn.execute(text(
            "select status, claim_token, reservation_id, lease_expires_at,"
            " active_generation_id from email_deliveries where id=:d"
        ), dict(d=seeded["delivery"])).mappings().one()
    pred = attempts_of(engine, seeded["delivery"])[-1]
    records.append({
        "step": MAX_ATTEMPTS,
        "delivery_status": d["status"],
        "claim_token": d["claim_token"],
        "reservation_id": d["reservation_id"],
        "lease_expires_at": str(d["lease_expires_at"]),
        "active_generation": d["active_generation_id"],
        "predecessor_attempt_id": pred["id"],
        "predecessor_attempt_index": pred["attempt_index"],
        "execution_disposition": pred["execution_disposition"],
        "provider_outcome": pred["provider_outcome"],
        "predecessor_source": "fixture-seeded worker-completion row",
    })
    exhausted = retry(engine, seeded, step=MAX_ATTEMPTS)
    assert exhausted.kind == "failed"
    assert exhausted.generation == MAX_ATTEMPTS
    assert exhausted.generation == max(g["generation"] for g in generations_of(
        engine, seeded["delivery"]))
    assert len(attempts_of(engine, seeded["delivery"])) == MAX_ATTEMPTS

    print("\npredecessor provenance:", json.dumps(records, indent=2, default=str))
