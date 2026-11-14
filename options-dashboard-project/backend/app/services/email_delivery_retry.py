"""RC: retry a delivery after a retryable provider outcome.

The retry is one transaction. A delivery eligible for retry is *not* ``pending``:
it is still ``sending``/``dispatched`` and still carries the claim its last
attempt ran under. So the transition has to release that claim, advance the
generation pointer, create the next attempt and take a new claim without ever
committing an intermediate ``pending`` row -- if anything fails, the delivery is
still ``sending`` with its original claim.

Budget is four attempts. The fifth request does not retry: it finalizes the
delivery as ``failed`` with ``retry_budget_exhausted``.

Concurrency
-----------
* PostgreSQL / CockroachDB: the delivery row is locked with ``FOR UPDATE``.
* SQLite: ``BEGIN IMMEDIATE`` takes the write reservation for the transaction
  (see ``_write_transaction``); there is no row-level lock to take.

Time is compared in Python against the locked row rather than in SQL, because
SQLite stores datetimes as text and a lexicographic comparison of mixed formats
is silently wrong. The UPDATEs therefore guard on the fields that actually
change (status, claim token, reservation, active generation), not on timestamps.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

MAX_ATTEMPTS = 4

UNRESOLVED = ("reserved", "payload_released", "dispatch_authorized", "execution_unknown")


def _bind_dt(value: datetime | None) -> str | None:
    """Bind timestamps as ISO-8601 text.

    SQLite stores datetimes as text and PostgreSQL casts the same literal to
    ``timestamptz``, so one representation works on both engines and round-trips
    through ``_as_dt`` without depending on a driver adapter.
    """
    return None if value is None else value.isoformat()


def _as_dt(value):
    """Coerce a stored timestamp to an aware datetime.

    Raw ``text()`` selects carry no type information, so PostgreSQL returns a
    ``datetime`` while SQLite returns the text it stored. Naive values are read
    as UTC.
    """
    if value is None or isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class RetryRefused(Exception):
    """The delivery is not eligible for this retry, with a named disposition."""

    def __init__(self, disposition: str, **detail: object) -> None:
        super().__init__(disposition)
        self.disposition = disposition
        self.detail = detail


@dataclass(frozen=True)
class RetryOutcome:
    kind: str  # "claimed" | "failed"
    attempt_index: int
    generation: int
    attempt_id: str
    generation_id: str


@contextmanager
def _write_transaction(engine: Engine) -> Iterator[Connection]:
    """Yield a connection inside a write transaction.

    On SQLite the transaction is opened with ``BEGIN IMMEDIATE`` so the write
    reservation is taken up front instead of upgrading mid-transaction (which is
    how a deferred transaction fails with ``SQLITE_BUSY`` halfway through).
    """
    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _lock_clause(conn: Connection) -> str:
    # SQLite has no row-level locking; BEGIN IMMEDIATE already serializes writers.
    return " FOR UPDATE" if conn.dialect.name in ("postgresql", "cockroachdb") else ""


_SELECT_DELIVERY = """
    SELECT id, status, claim_token, claim_source, reservation_id,
           call_reserved_at, lease_expires_at, payload_encrypted,
           payload_expires_at, active_generation_id, finalized_at
    FROM email_deliveries
    WHERE id = :delivery_id AND user_id = :user_id AND principal_id = :principal_id
"""

_SELECT_GENERATION_NUMBER = """
    SELECT generation
    FROM email_delivery_generations
    WHERE id = :generation_id AND delivery_id = :delivery_id
"""

_SELECT_ATTEMPTS = """
    SELECT id, generation_id, attempt_index, execution_disposition,
           provider_outcome, payload_released_at
    FROM email_delivery_attempts
    WHERE delivery_id = :delivery_id
    ORDER BY attempt_index
"""

_RELEASE_CLAIM = """
    UPDATE email_deliveries
    SET status = 'pending',
        claim_token = NULL, claim_source = NULL,
        reservation_id = NULL, call_reserved_at = NULL,
        lease_expires_at = NULL,
        updated_at = :now
    WHERE id = :delivery_id
      AND user_id = :user_id
      AND principal_id = :principal_id
      AND status = :expected_status
      AND claim_token = :expected_claim_token
      AND reservation_id = :expected_reservation_id
      AND finalized_at IS NULL
      AND active_generation_id = :previous_generation_id
"""

_INSERT_GENERATION = """
    INSERT INTO email_delivery_generations (id, delivery_id, generation, created_at)
    SELECT :new_generation_id, :delivery_id, :next_generation, :now
    WHERE EXISTS (
        SELECT 1 FROM email_deliveries
        WHERE id = :delivery_id AND user_id = :user_id
          AND principal_id = :principal_id
          AND status = 'pending'
          AND claim_token IS NULL
          AND active_generation_id = :previous_generation_id
    )
      AND NOT EXISTS (
        SELECT 1 FROM email_delivery_generations
        WHERE delivery_id = :delivery_id AND generation = :next_generation
    )
"""

_ADVANCE_POINTER = """
    UPDATE email_deliveries
    SET active_generation_id = :new_generation_id, updated_at = :now
    WHERE id = :delivery_id
      AND user_id = :user_id
      AND principal_id = :principal_id
      AND status = 'pending'
      AND claim_token IS NULL
      AND active_generation_id = :previous_generation_id
"""

_INSERT_ATTEMPT = """
    INSERT INTO email_delivery_attempts
        (id, delivery_id, user_id, principal_id, generation_id, attempt_index,
         execution_disposition, reservation_id, claim_token, claim_source,
         original_call_reserved_at, original_lease_expires_at, created_at)
    SELECT :new_attempt_id, :delivery_id, :user_id, :principal_id,
           :new_generation_id, :next_attempt_index, 'reserved',
           :reservation_id, :claim_token, :claim_source,
           :now, :lease_until, :now
    WHERE EXISTS (
        SELECT 1 FROM email_deliveries
        WHERE id = :delivery_id AND user_id = :user_id
          AND principal_id = :principal_id
          AND status = 'pending'
          AND claim_token IS NULL
          AND active_generation_id = :new_generation_id
    )
      AND NOT EXISTS (
        SELECT 1 FROM email_delivery_attempts WHERE id = :new_attempt_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM email_delivery_attempts
        WHERE delivery_id = :delivery_id AND generation_id = :new_generation_id
      )
"""

_CLAIM_DELIVERY = """
    UPDATE email_deliveries
    SET status = 'sending',
        claim_token = :claim_token, claim_source = :claim_source,
        reservation_id = :reservation_id,
        call_reserved_at = :now, lease_expires_at = :lease_until,
        updated_at = :now
    WHERE id = :delivery_id
      AND user_id = :user_id
      AND principal_id = :principal_id
      AND status = 'pending'
      AND claim_token IS NULL
      AND claim_source IS NULL
      AND reservation_id IS NULL
      AND call_reserved_at IS NULL
      AND lease_expires_at IS NULL
      AND finalized_at IS NULL
      AND active_generation_id = :new_generation_id
      AND EXISTS (
        SELECT 1 FROM email_delivery_attempts
        WHERE id = :new_attempt_id
          AND delivery_id = email_deliveries.id
          AND generation_id = email_deliveries.active_generation_id
          AND reservation_id = :reservation_id
          AND claim_token = :claim_token
          AND claim_source = :claim_source
          AND execution_disposition = 'reserved'
      )
"""

_EXHAUST_BUDGET = """
    UPDATE email_deliveries
    SET status = 'failed', finalized_at = :now,
        failure_code = 'retry_budget_exhausted',
        claim_token = NULL, claim_source = NULL,
        reservation_id = NULL, call_reserved_at = NULL,
        lease_expires_at = NULL,
        payload_encrypted = NULL, payload_expires_at = NULL,
        updated_at = :now
    WHERE id = :delivery_id
      AND user_id = :user_id
      AND principal_id = :principal_id
      AND status = :expected_status
      AND claim_token = :expected_claim_token
      AND finalized_at IS NULL
"""


def _rank(row, attempts, now: datetime) -> tuple[int, str]:
    """Return ``(rank, disposition)``. Only ranks 6 and 7 proceed."""
    if row["finalized_at"] is not None:
        return 5, "abort_invalid_budget"
    if row["status"] not in ("sending", "dispatched"):
        return 5, "abort_invalid_budget"
    if row["active_generation_id"] is None:
        return 5, "abort_invalid_budget"
    if not row["claim_token"] or not row["reservation_id"] or row["call_reserved_at"] is None:
        return 5, "abort_invalid_budget"
    lease = _as_dt(row["lease_expires_at"])
    if lease is None:
        return 5, "abort_invalid_budget"
    if lease > now:
        # A live claim means a worker may still be inside its lease.
        return 5, "abort_live_lease"
    payload_expiry = _as_dt(row["payload_expires_at"])
    if row["payload_encrypted"] is None or payload_expiry is None:
        return 5, "abort_invalid_budget"
    if payload_expiry <= now:
        return 5, "abort_expired_payload"

    completed = [a for a in attempts if a["execution_disposition"] == "execution_completed"]
    accepted = [a for a in completed if a["provider_outcome"] == "accepted"]
    permanent = [a for a in completed if a["provider_outcome"] == "rejected_permanent"]
    retryable = [a for a in completed if a["provider_outcome"] == "rejected_retryable"]

    if any(a["payload_released_at"] is None for a in completed):
        return 1, "abort_contradictory_history"
    if accepted and permanent:
        return 1, "abort_contradictory_history"
    if accepted:
        return 2, "abort_accepted_present"
    if permanent:
        return 3, "abort_permanent_attempt"
    if any(a["execution_disposition"] in UNRESOLVED for a in attempts):
        return 4, "abort_unresolved_sibling"
    if not 1 <= len(attempts) <= MAX_ATTEMPTS:
        return 5, "abort_invalid_budget"
    if not any(a["generation_id"] == row["active_generation_id"] for a in retryable):
        # The retryable outcome must sit on the generation that is actually active,
        # otherwise the successor would chain off an arbitrary older attempt.
        return 5, "abort_invalid_budget"
    return (6, "branch_a_claim") if len(attempts) < MAX_ATTEMPTS else (7, "branch_b_exhaust")


def _is_blank(value) -> bool:
    """A required value that carries no identity.

    ``"   "`` is not a claim token: it would satisfy the delivery/attempt
    identity joins while naming nothing, so it is refused exactly like ``None``.
    Values are compared after stripping but are not rewritten -- a caller's
    token is persisted verbatim.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return not value


def _one(conn: Connection, statement: str, params: dict, what: str) -> None:
    result = conn.execute(text(statement), params)
    if result.rowcount != 1:
        raise RetryRefused("abort_state_changed", step=what, rowcount=result.rowcount)


def retry_delivery(
    engine: Engine,
    *,
    delivery_id: str,
    user_id: str,
    principal_id: str,
    now: datetime,
    new_generation_id: str | None = None,
    new_attempt_id: str | None = None,
    claim_token: str | None = None,
    claim_source: str | None = None,
    reservation_id: str | None = None,
    lease_until: datetime | None = None,
) -> RetryOutcome:
    """Retry the delivery once, or finalize it when the budget is spent."""
    with _write_transaction(engine) as conn:
        row = conn.execute(
            text(_SELECT_DELIVERY + _lock_clause(conn)),
            {"delivery_id": delivery_id, "user_id": user_id, "principal_id": principal_id},
        ).mappings().first()
        if row is None:
            raise RetryRefused("abort_unknown_delivery")

        attempts = conn.execute(
            text(_SELECT_ATTEMPTS), {"delivery_id": delivery_id}
        ).mappings().all()

        rank, disposition = _rank(row, attempts, now)
        if rank < 6:
            raise RetryRefused(disposition, rank=rank)

        if rank == 7:
            # The real generation number, not the attempt count: they agree only
            # while nothing else has touched the delivery's generation chain.
            generation_number = conn.execute(
                text(_SELECT_GENERATION_NUMBER),
                {"generation_id": row["active_generation_id"],
                 "delivery_id": delivery_id},
            ).scalar_one_or_none()
            if generation_number is None:
                raise RetryRefused(
                    "abort_invalid_budget", reason="active_generation_missing"
                )

            _one(conn, _EXHAUST_BUDGET, {
                "delivery_id": delivery_id, "user_id": user_id,
                "principal_id": principal_id, "now": _bind_dt(now),
                "expected_status": row["status"],
                "expected_claim_token": row["claim_token"],
            }, "exhaust_budget")
            return RetryOutcome(
                kind="failed",
                attempt_index=attempts[-1]["attempt_index"],
                generation=generation_number,
                attempt_id=attempts[-1]["id"],
                generation_id=row["active_generation_id"],
            )

        # ---- branch A -----------------------------------------------------
        next_index = attempts[-1]["attempt_index"] + 1
        missing = [
            name
            for name, value in (
                ("new_generation_id", new_generation_id),
                ("new_attempt_id", new_attempt_id),
                ("claim_token", claim_token),
                ("claim_source", claim_source),
                ("reservation_id", reservation_id),
                ("lease_until", lease_until),
            )
            if _is_blank(value)
        ]
        if missing:
            # A claim needs all six: a retry that cannot name its generation,
            # attempt or claim identity must not touch the delivery.
            raise RetryRefused("abort_invalid_arguments", missing=missing)
        if _as_dt(lease_until) <= now:
            raise RetryRefused("abort_invalid_arguments", reason="lease_not_in_future")

        _one(conn, _RELEASE_CLAIM, {
            "delivery_id": delivery_id, "user_id": user_id, "principal_id": principal_id,
            "now": _bind_dt(now),
            "expected_status": row["status"],
            "expected_claim_token": row["claim_token"],
            "expected_reservation_id": row["reservation_id"],
            "previous_generation_id": row["active_generation_id"],
        }, "release_claim")

        _one(conn, _INSERT_GENERATION, {
            "new_generation_id": new_generation_id, "delivery_id": delivery_id,
            "user_id": user_id, "principal_id": principal_id,
            "next_generation": next_index, "now": _bind_dt(now),
            "previous_generation_id": row["active_generation_id"],
        }, "insert_generation")

        _one(conn, _ADVANCE_POINTER, {
            "new_generation_id": new_generation_id, "delivery_id": delivery_id,
            "user_id": user_id, "principal_id": principal_id, "now": _bind_dt(now),
            "previous_generation_id": row["active_generation_id"],
        }, "advance_pointer")

        _one(conn, _INSERT_ATTEMPT, {
            "new_attempt_id": new_attempt_id, "delivery_id": delivery_id,
            "user_id": user_id, "principal_id": principal_id,
            "new_generation_id": new_generation_id,
            "next_attempt_index": next_index,
            "reservation_id": reservation_id, "claim_token": claim_token,
            "claim_source": claim_source, "now": _bind_dt(now),
            "lease_until": _bind_dt(lease_until),
        }, "insert_attempt")

        _one(conn, _CLAIM_DELIVERY, {
            "claim_token": claim_token, "claim_source": claim_source,
            "reservation_id": reservation_id, "delivery_id": delivery_id,
            "user_id": user_id, "principal_id": principal_id,
            "now": _bind_dt(now), "lease_until": _bind_dt(lease_until),
            "new_generation_id": new_generation_id,
            "new_attempt_id": new_attempt_id,
        }, "claim_delivery")

        _assert_claimed(conn, delivery_id, new_attempt_id, new_generation_id,
                        claim_token, claim_source, reservation_id)
        return RetryOutcome(
            kind="claimed",
            attempt_index=next_index,
            generation=next_index,
            attempt_id=new_attempt_id,
            generation_id=new_generation_id,
        )


# ---------------------------------------------------------------------------
# post-write assertion: the claim-identity invariant spans two rows, so it is
# re-read rather than inferred from the UPDATE's rowcount.
# ---------------------------------------------------------------------------
def _assert_claimed(conn, delivery_id, attempt_id, generation_id,
                    claim_token, claim_source, reservation_id):
    row = conn.execute(text("""
        SELECT d.id, d.status, d.claim_token, d.claim_source, d.reservation_id,
               d.active_generation_id, a.id AS attempt_id, a.generation_id,
               a.reservation_id AS a_reservation, a.claim_token AS a_claim,
               a.claim_source AS a_source, a.execution_disposition
        FROM email_deliveries d
        JOIN email_delivery_attempts a
          ON a.delivery_id = d.id AND a.id = :attempt_id
        WHERE d.id = :delivery_id
          AND d.status = 'sending'
          AND d.active_generation_id = :generation_id
          AND a.generation_id = d.active_generation_id
          AND a.reservation_id = d.reservation_id
          AND a.claim_token = d.claim_token
          AND a.claim_source = d.claim_source
          AND d.claim_token = :claim_token
          AND d.claim_source = :claim_source
          AND d.reservation_id = :reservation_id
          AND a.execution_disposition = 'reserved'
    """), {
        "delivery_id": delivery_id, "attempt_id": attempt_id,
        "generation_id": generation_id, "claim_token": claim_token,
        "claim_source": claim_source, "reservation_id": reservation_id,
    }).mappings().first()
    if row is None:
        raise RetryRefused("abort_state_changed", step="assert_claimed")
