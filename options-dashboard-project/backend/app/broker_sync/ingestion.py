"""Day 39 Task 2 — Durable broker-event ingestion pipeline.

Durable pipeline:
    BrokerSyncEvent
        ↓
    validation (identity, tenant, quantity invariants)
        ↓
    tenant / order identity
        ↓
    durable idempotency (durable identity beats broker ordering:
        same canonical_id + same fingerprint -> DUPLICATE_NOOP,
        same canonical_id + different fingerprint -> CONFLICT,
        regardless of canonical_sequence vs the broker anchor)
        ↓
    broker ordering validation (sequence gap / stale / out-of-order,
        for genuinely new canonical_ids only)
        ↓
    terminal-state enforcement (against durable projection)
        ↓
    durable normalized projection
        ↓
    explicit Day38 lifecycle mapping
        (broker events WITH a Day38 state transition; projection-only
        events such as ORDER_ACCEPTED are persisted without a lifecycle
        event — approved Day38 design §13/§14)
        ↓
    single transaction (all commit or all roll back)

All operations share the caller's transaction.  On any failure the caller
rolls back the entire transaction — no partial durable state.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError as SAIntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.utils.db_dialect import dialect_insert
from app.utils.retry import is_serialization_failure, retry_on_serialization

from app.broker_sync import (
    BrokerEventSourceMode,
    BrokerEventType,
    BrokerSyncEvent,
    CanonicalOrderState,
    FillFacts,
    OrderFacts,
    compute_ceid,
)
from app.broker_sync.models import (
    BrokerOrderProjection,
    BrokerSyncIdempotency,
    BrokerSyncSequenceAnchor,
    OrderFamilySyncLock,
)
from app.trade_lifecycle.persistence import append_lifecycle_event, next_event_sequence

logger = logging.getLogger(__name__)


class IngestionError(Exception):
    """Raised when event ingestion fails."""

    def __init__(self, reason: str, action: str = "REJECTED"):
        self.reason = reason
        self.action = action
        super().__init__(reason)


# ---------------------------------------------------------------------------
# Content fingerprint for conflict detection
# ---------------------------------------------------------------------------

def _content_fingerprint(event: BrokerSyncEvent) -> str:
    """Deterministic canonical-content fingerprint.

    Covers every semantically relevant field so that two events with the
    same canonical_id but different content produce different fingerprints.
    """
    parts: dict[str, Any] = {
        "tenant_id": event.tenant_id,
        "broker": event.broker,
        "event_type": event.event_type,
        "event_version": event.event_version,
        "provider_event_id": event.provider_event_id,
        "source_mode": event.source_mode.value,
        "broker_order_id": event.broker_order_id,
        "canonical_sequence": event.canonical_sequence,
    }
    if event.event_timestamp is not None:
        parts["event_timestamp"] = event.event_timestamp.isoformat()
    if event.order_facts is not None:
        parts["order_facts"] = {
            "order_id": event.order_facts.order_id,
            "broker_order_id": event.order_facts.broker_order_id,
            "status": event.order_facts.status.value,
            "total_quantity": event.order_facts.total_quantity,
            "cumulative_filled": event.order_facts.cumulative_filled,
            "average_price": event.order_facts.average_price,
            "last_fill_price": event.order_facts.last_fill_price,
            "last_fill_quantity": event.order_facts.last_fill_quantity,
            "rejection_reason": event.order_facts.rejection_reason,
            "is_terminal": event.order_facts.is_terminal,
        }
    if event.fill_facts is not None:
        parts["fill_facts"] = {
            "fill_id": event.fill_facts.fill_id,
            "fill_quantity": event.fill_facts.fill_quantity,
            "fill_price": event.fill_facts.fill_price,
            "fill_timestamp": (
                event.fill_facts.fill_timestamp.isoformat()
                if event.fill_facts.fill_timestamp is not None
                else None
            ),
            "cumulative_filled_after": event.fill_facts.cumulative_filled_after,
            "remaining_after": event.fill_facts.remaining_after,
        }
    if event.metadata is not None:
        parts["metadata"] = dict(event.metadata)
    canonical = json.dumps(parts, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _verify_ceid_metadata(event: BrokerSyncEvent) -> str | None:
    """Day40.4 §3.4 — verify a supplied CEID against its strikenova metadata block.

    Returns None when the event carries no ``metadata["strikenova"]`` block
    (legacy events continue through the existing path unchanged) or when the
    supplied canonical_event_id equals the derived CEID.  Returns a failure
    reason string on mismatch.
    """
    if event.metadata is None:
        return None
    strikenova = event.metadata.get("strikenova")
    if not isinstance(strikenova, Mapping):
        return None
    d1 = strikenova.get("d1")
    fp = strikenova.get("content_fingerprint")
    if d1 is None and fp is None:
        return None
    if not isinstance(d1, str) or not isinstance(fp, str) or not d1 or not fp:
        return "canonical identity mismatch: strikenova metadata block is incomplete"
    if event.canonical_event_id is None:
        return (
            "canonical identity mismatch: strikenova metadata present but the "
            "event supplies no canonical_event_id"
        )
    derived = compute_ceid(d1, fp)
    if derived != event.canonical_event_id:
        return (
            f"canonical identity mismatch: derived CEID {derived[:16]}... does not "
            f"match supplied canonical_event_id {event.canonical_event_id[:16]}..."
        )
    return None


# ---------------------------------------------------------------------------
# Terminal-state enforcement
# ---------------------------------------------------------------------------

_TERMINAL_STATES = {
    CanonicalOrderState.FILLED,
    CanonicalOrderState.CANCELLED,
    CanonicalOrderState.REJECTED,
    CanonicalOrderState.EXPIRED,
}


def _is_terminal(state: CanonicalOrderState) -> bool:
    return state in _TERMINAL_STATES


# ---------------------------------------------------------------------------
# Broker-to-Day38 lifecycle event mapping
# ---------------------------------------------------------------------------

# Maps broker event type → Day38 lifecycle event type.  A value of None
# marks a PROJECTION-ONLY broker event: fully ingestable (normalized
# projection + durable idempotency + broker ordering) but carrying NO
# Day38 state transition, so it must not be persisted as a lifecycle event.
_BROKER_TO_LIFECYCLE: dict[str, str | None] = {
    BrokerEventType.ORDER_SUBMITTED.value: "OrderSubmitted",
    # ORDER_PROCESSING → None (projection-only, Day40 §1.3):
    # Processing chatter (validation pending / open pending / trigger pending /
    # modify pending / modify validation pending / modified / not modified /
    # cancel pending / not cancelled / modify after market order req received)
    # is broker-observed state, NOT a lifecycle transition.  It must never mint
    # a Day38 event: the order is already SUBMITTED and the replay engine
    # would reject a duplicate SUBMITTED transition.
    BrokerEventType.ORDER_PROCESSING.value: None,
    # ORDER_ACCEPTED → None (projection-only):
    # The approved Day38 design defines ``OrderSubmitted`` as an audit
    # record of the submission attempt that explicitly "does not mean
    # broker accepted" (design §13.7), mandates "No lifecycle event implies
    # broker state" (§4 rule 4), and deliberately removed standalone
    # broker-acceptance events from the Day38 vocabulary (§14); a dedicated
    # ``BrokerOrderAccepted`` event is a planned ADDITIVE extension for
    # Days 39–42 (§13.5) and must not be invented inside Task2.  Broker
    # acceptance therefore changes no Day38 state — the order is already
    # SUBMITTED (working) and acceptance is broker-observed state, durably
    # recorded in the Task2 normalized projection (status=OPEN).  Mapping
    # it to a second ``OrderSubmitted`` produced the lifecycle stream
    # PENDING→SUBMITTED→SUBMITTED, which the approved replay engine
    # correctly rejects (ReplayInvalidTransition): the system must never
    # persist a durable stream its own replay engine cannot rebuild.
    BrokerEventType.ORDER_ACCEPTED.value: None,
    BrokerEventType.PARTIAL_FILL.value: "OrderFilled",
    BrokerEventType.FILL_RECORDED.value: "FillRecorded",
    BrokerEventType.FULL_FILL.value: "OrderFilled",
    BrokerEventType.ORDER_CANCELLED.value: "OrderCancelled",
    BrokerEventType.ORDER_REJECTED.value: "OrderRejected",
    BrokerEventType.ORDER_EXPIRED.value: "OrderCancelled",
}


def _map_to_lifecycle_event_type(event_type: str) -> str | None:
    """Map a canonical broker event type to the Day38 lifecycle event type.

    Uses the approved Day38 vocabulary (OrderSubmitted, OrderFilled,
    FillRecorded, OrderCancelled, OrderRejected).
    Does NOT invent new Day38 event names.

    Returns None for PROJECTION-ONLY broker events: they carry no Day38
    state transition (currently ORDER_ACCEPTED — see _BROKER_TO_LIFECYCLE
    and the approved Day38 design §13/§14) yet remain fully ingestable
    (normalized projection + durable idempotency + broker ordering).

    Raises IngestionError for events that cannot be mapped at all.
    """
    if event_type in _BROKER_TO_LIFECYCLE:
        return _BROKER_TO_LIFECYCLE[event_type]
    raise IngestionError(
        f"cannot map broker event type '{event_type}' to a Day38 lifecycle event type",
        action="REJECTED",
    )


def _event_canonical_state(event: BrokerSyncEvent) -> CanonicalOrderState:
    """Map event type to canonical order state."""
    mapping = {
        BrokerEventType.ORDER_SUBMITTED: CanonicalOrderState.SUBMITTED,
        # Day40 §1.3/§8: processing chatter projects SUBMITTED (the order is
        # submitted-but-not-yet-open); projection-only — no Day38 transition.
        BrokerEventType.ORDER_PROCESSING: CanonicalOrderState.SUBMITTED,
        BrokerEventType.ORDER_ACCEPTED: CanonicalOrderState.OPEN,
        BrokerEventType.ORDER_REJECTED: CanonicalOrderState.REJECTED,
        BrokerEventType.ORDER_CANCELLED: CanonicalOrderState.CANCELLED,
        BrokerEventType.ORDER_EXPIRED: CanonicalOrderState.EXPIRED,
        BrokerEventType.PARTIAL_FILL: CanonicalOrderState.PARTIALLY_FILLED,
        BrokerEventType.FILL_RECORDED: CanonicalOrderState.PARTIALLY_FILLED,
        BrokerEventType.FULL_FILL: CanonicalOrderState.FILLED,
        # ORDER_RECOVERED is NOT mapped to UNKNOWN — it is a future recovery
        # event type.  For Task 2 it is rejected at the mapping layer.
    }
    return mapping.get(event.event_type, CanonicalOrderState.UNKNOWN)


# ---------------------------------------------------------------------------
# Broker ordering validation — PostgreSQL-safe atomic pattern
# ---------------------------------------------------------------------------

def _validate_broker_sequence_position(
    db: Session,
    event: BrokerSyncEvent,
) -> dict | None:
    """Validate canonical_sequence ordering position without advancing.

    Returns a dict with position info, or None if sequence is unknown.

    The returned dict contains:
    - ``incoming``: the event's canonical_sequence
    - ``last_sequence``: the current anchor's last_sequence (0 if anchor doesn't exist yet)
    - ``is_duplicate``: True if incoming == last_sequence (duplicate seq)
    - ``broker_order_id``: the normalized broker order ID
    - ``anchor_exists``: True if the anchor row already exists

    Raises IngestionError for gap/stale sequences.

    Does NOT fabricate missing sequences.  Does NOT advance the anchor.
    Does NOT create the anchor — that happens inside the SAVEPOINT.
    """
    if event.canonical_sequence is None:
        return None

    incoming = event.canonical_sequence
    broker_order_id = event.broker_order_id or ""

    # --- Read current anchor state (does NOT create it) ---
    anchor = db.execute(
        select(BrokerSyncSequenceAnchor).where(
            BrokerSyncSequenceAnchor.tenant_id == event.tenant_id,
            BrokerSyncSequenceAnchor.broker == event.broker,
            BrokerSyncSequenceAnchor.broker_order_id == broker_order_id,
        )
    ).scalar_one_or_none()

    last_sequence = anchor.last_sequence if anchor is not None else 0
    anchor_exists = anchor is not None

    # --- Validate position ---

    if incoming < last_sequence:
        raise IngestionError(
            f"stale canonical_sequence={incoming} "
            f"(last applied={last_sequence}) — "
            f"stale/out-of-order event rejected",
            action="REJECTED",
        )

    if incoming > last_sequence + 1:
        raise IngestionError(
            f"sequence gap: received canonical_sequence={incoming}, "
            f"expected {last_sequence + 1} (last applied={last_sequence}) — "
            f"quarantined as synchronization gap",
            action="REJECTED",
        )

    return {
        "incoming": incoming,
        "last_sequence": last_sequence,
        "is_duplicate": incoming == last_sequence,
        "broker_order_id": broker_order_id,
        "anchor_exists": anchor_exists,
    }


def _ensure_broker_sequence_anchor(
    db: Session,
    event: BrokerSyncEvent,
    position_info: dict | None,
) -> None:
    """Create the broker sequence anchor row if it doesn't exist.

    Must be called INSIDE the SAVEPOINT so that anchor creation
    rolls back with projection, idempotency, and lifecycle.

    Uses INSERT ... ON CONFLICT DO NOTHING for concurrency safety.
    """
    if position_info is None:
        return

    if position_info["anchor_exists"]:
        return

    broker_order_id = position_info["broker_order_id"]

    db.execute(
        text(
            """
            INSERT INTO broker_sync_sequence_anchor
                (tenant_id, broker, broker_order_id, last_sequence, created_at, updated_at)
            VALUES
                (:tenant_id, :broker, :broker_order_id, 0, :now, :now)
            ON CONFLICT (tenant_id, broker, broker_order_id) DO NOTHING
            """
        ),
        {
            "tenant_id": event.tenant_id,
            "broker": event.broker,
            "broker_order_id": broker_order_id,
            "now": datetime.now(timezone.utc),
        },
    )
    db.flush()


def _advance_broker_sequence(
    db: Session,
    event: BrokerSyncEvent,
    position_info: dict | None,
) -> None:
    """Atomically advance the broker sequence anchor AFTER successful application.

    This is the serialization point for concurrent consumers.  It must be
    called AFTER the projection, idempotency record, and Day38 lifecycle event
    have all been persisted (inside the same SAVEPOINT) so that a failed
    advancement rolls back all durable effects together.

    Raises IngestionError if a concurrent worker already advanced the anchor.
    """
    if position_info is None:
        return

    if position_info["is_duplicate"]:
        return  # Duplicate sequence — idempotency layer handles it

    incoming = position_info["incoming"]
    expected = position_info["last_sequence"]
    broker_order_id = position_info["broker_order_id"]

    result = db.execute(
        text(
            """
            UPDATE broker_sync_sequence_anchor
            SET last_sequence = :advance_to,
                updated_at = :now
            WHERE tenant_id = :tenant_id
              AND broker = :broker
              AND broker_order_id = :broker_order_id
              AND last_sequence = :expected
            """
        ),
        {
            "advance_to": incoming,
            "expected": expected,
            "tenant_id": event.tenant_id,
            "broker": event.broker,
            "broker_order_id": broker_order_id,
            "now": datetime.now(timezone.utc),
        },
    )
    db.flush()

    if result.rowcount == 1:
        return

    # Concurrent worker advanced — signal failure for re-classification
    raise IngestionError(
        f"concurrent worker advanced broker sequence past {incoming}",
        action="CONFLICT",
    )


# ---------------------------------------------------------------------------
# Quantity invariant validation
# ---------------------------------------------------------------------------

def _validate_quantity_invariants(
    event: BrokerSyncEvent,
    previous: BrokerOrderProjection | None,
) -> None:
    """Validate fill and cumulative quantity semantics before persisting.

    Rejects:
    - cumulative_filled_after < previous cumulative_filled (regression)
    - cumulative_filled_after > total_quantity (overfill)
    - fill_quantity < 0 (negative fill)
    - remaining inconsistent with total/cumulative
    - cumulative_filled_after < previous cumulative + fill_quantity (fill arithmetic)

    Does not silently repair — fails closed.
    """
    ff = event.fill_facts
    if ff is None:
        return

    # Negative fill quantity
    if ff.fill_quantity is not None and ff.fill_quantity < 0:
        raise IngestionError(
            f"negative fill_quantity={ff.fill_quantity} rejected",
            action="REJECTED",
        )

    total_quantity = None
    if event.order_facts is not None and event.order_facts.total_quantity is not None:
        total_quantity = event.order_facts.total_quantity
    elif previous is not None:
        total_quantity = previous.total_quantity

    # Cumulative must not regress
    prev_cumulative = previous.cumulative_filled if previous is not None else 0
    if ff.cumulative_filled_after is not None and ff.cumulative_filled_after < prev_cumulative:
        raise IngestionError(
            f"cumulative_filled_after={ff.cumulative_filled_after} "
            f"regresses previous cumulative={prev_cumulative}",
            action="REJECTED",
        )

    # FIX 3: Fill arithmetic consistency — cumulative_filled_after must account
    # for the full incremental fill.  Catches cases where cumulative_after
    # doesn't include the current fill's contribution.
    if (
        previous is not None
        and ff.fill_quantity is not None
        and ff.cumulative_filled_after is not None
    ):
        expected_minimum = prev_cumulative + ff.fill_quantity
        if ff.cumulative_filled_after < expected_minimum:
            raise IngestionError(
                f"cumulative_filled_after={ff.cumulative_filled_after} is less than "
                f"previous_cumulative={prev_cumulative} + fill_quantity={ff.fill_quantity} "
                f"(expected >= {expected_minimum})",
                action="REJECTED",
            )

    # Cumulative must not exceed total
    if ff.cumulative_filled_after is not None and total_quantity is not None:
        if ff.cumulative_filled_after > total_quantity:
            raise IngestionError(
                f"cumulative_filled_after={ff.cumulative_filled_after} "
                f"exceeds total_quantity={total_quantity} (overfill)",
                action="REJECTED",
            )

    # Remaining consistency check
    if ff.remaining_after is not None and total_quantity is not None and ff.cumulative_filled_after is not None:
        expected_remaining = total_quantity - ff.cumulative_filled_after
        if ff.remaining_after != expected_remaining:
            raise IngestionError(
                f"remaining_after={ff.remaining_after} inconsistent with "
                f"total={total_quantity} - cumulative={ff.cumulative_filled_after} "
                f"(expected {expected_remaining})",
                action="REJECTED",
            )

    # Fill quantity must not exceed total
    if ff.fill_quantity is not None and total_quantity is not None:
        if ff.fill_quantity > total_quantity:
            raise IngestionError(
                f"fill_quantity={ff.fill_quantity} exceeds total_quantity={total_quantity}",
                action="REJECTED",
            )


# ---------------------------------------------------------------------------
# Projection builder
# ---------------------------------------------------------------------------

def _build_projection(
    event: BrokerSyncEvent,
    previous: BrokerOrderProjection | None,
    canonical_sequence: int | None,
) -> BrokerOrderProjection:
    """Build a new BrokerOrderProjection from the event and previous state."""
    state = _event_canonical_state(event)
    is_terminal = _is_terminal(state)

    total_quantity: int | None = None
    cumulative_filled: int = 0
    remaining_quantity: int | None = None
    average_price: float | None = None
    last_fill_price: float | None = None
    last_fill_quantity: int | None = None
    fill_count: int = 0
    last_fill_id: str | None = None
    rejection_reason: str | None = None

    if previous is not None and canonical_sequence is not None:
        # Carry forward previous state unless overridden by this event
        total_quantity = previous.total_quantity
        cumulative_filled = previous.cumulative_filled
        remaining_quantity = previous.remaining_quantity
        average_price = previous.average_price
        fill_count = previous.fill_count
        last_fill_id = previous.last_fill_id

    if event.order_facts is not None:
        of = event.order_facts
        if of.total_quantity is not None:
            total_quantity = of.total_quantity
        if of.cumulative_filled is not None and of.cumulative_filled != 0:
            cumulative_filled = of.cumulative_filled
        if of.average_price is not None:
            average_price = of.average_price
        if of.last_fill_price is not None:
            last_fill_price = of.last_fill_price
        if of.last_fill_quantity is not None:
            last_fill_quantity = of.last_fill_quantity
        if of.rejection_reason is not None:
            rejection_reason = of.rejection_reason

    if event.fill_facts is not None:
        ff = event.fill_facts
        last_fill_id = ff.fill_id
        last_fill_price = ff.fill_price
        last_fill_quantity = ff.fill_quantity
        if ff.cumulative_filled_after is not None:
            cumulative_filled = ff.cumulative_filled_after
        remaining_quantity = ff.remaining_after
        fill_count += 1
        if ff.fill_price is not None and ff.fill_quantity is not None:
            if previous is not None and previous.cumulative_filled > 0 and average_price is not None:
                prev_total_price = (average_price or 0.0) * previous.cumulative_filled
                new_total_price = ff.fill_price * ff.fill_quantity
                if cumulative_filled > 0:
                    average_price = (prev_total_price + new_total_price) / cumulative_filled
            else:
                average_price = ff.fill_price

    # Compute remaining if we have total and cumulative
    if remaining_quantity is None and total_quantity is not None:
        remaining_quantity = total_quantity - cumulative_filled

    # Rejection reason from event
    if event.event_type == BrokerEventType.ORDER_REJECTED:
        if event.order_facts is not None and event.order_facts.rejection_reason:
            rejection_reason = event.order_facts.rejection_reason

    occurred_at = event.event_timestamp or event.received_at

    return BrokerOrderProjection(
        tenant_id=event.tenant_id,
        broker=event.broker,
        broker_order_id=event.broker_order_id or "",
        canonical_id=event.canonical_id,
        event_type=event.event_type,
        status=state.value,
        total_quantity=total_quantity,
        cumulative_filled=cumulative_filled,
        remaining_quantity=remaining_quantity,
        average_price=average_price,
        last_fill_price=last_fill_price,
        last_fill_quantity=last_fill_quantity,
        rejection_reason=rejection_reason,
        is_terminal=is_terminal,
        fill_count=fill_count,
        last_fill_id=last_fill_id,
        canonical_sequence=canonical_sequence,
        occurred_at=occurred_at,
        # Day41.2 — durable S2 evidence (frozen r2 design §12): STRICTLY the
        # provider/exchange event timestamp.  Never the ``occurred_at``
        # fallback, never ``received_at`` — missing evidence stays NULL so it
        # remains structurally distinct from any timestamp value.
        event_timestamp=event.event_timestamp,
        received_at=event.received_at,
    )


# ---------------------------------------------------------------------------
# Day38 lifecycle event mapping
# ---------------------------------------------------------------------------

def _append_lifecycle_from_event(
    db: Session,
    event: BrokerSyncEvent,
    day38_sequence: int,
    execution_id: str,
) -> None:
    """Append a Day38 lifecycle event derived from the canonical broker event.

    Maps the broker event type to the approved Day38 vocabulary and uses
    ``next_event_sequence`` to allocate the Day38 aggregate sequence
    independently of ``canonical_sequence``.  The lifecycle aggregate is
    the ACTUAL StrikeNova execution (``execution_id``) resolved from the
    canonical application order reference — never a synthetic broker-order
    aggregate.

    Projection-only broker events (mapper returns None — ORDER_ACCEPTED,
    per approved Day38 design §13/§14) persist NO lifecycle event: they
    carry no Day38 state transition, and persisting one would create a
    durable stream the replay engine cannot rebuild.
    """
    lifecycle_event_type = _map_to_lifecycle_event_type(event.event_type)
    if lifecycle_event_type is None:
        # Projection-only broker event (design §13/§14): no lifecycle event.
        return

    aggregate_id = execution_id

    payload: dict[str, Any] = {
        "canonical_id": event.canonical_id,
        "broker": event.broker,
        "broker_event_type": event.event_type,
        "broker_event_version": event.event_version,
        "provider_event_id": event.provider_event_id,
        "source_mode": event.source_mode.value,
        "broker_order_id": event.broker_order_id,
    }
    if event.event_timestamp is not None:
        payload["event_timestamp"] = event.event_timestamp.isoformat()
    if event.order_facts is not None:
        payload["order_facts"] = {
            "total_quantity": event.order_facts.total_quantity,
            "cumulative_filled": event.order_facts.cumulative_filled,
            "average_price": event.order_facts.average_price,
            "is_terminal": event.order_facts.is_terminal,
        }
    if event.fill_facts is not None:
        payload["fill_facts"] = {
            "fill_id": event.fill_facts.fill_id,
            "fill_quantity": event.fill_facts.fill_quantity,
            "fill_price": event.fill_facts.fill_price,
            "cumulative_filled_after": event.fill_facts.cumulative_filled_after,
            "remaining_after": event.fill_facts.remaining_after,
        }

    # FIX 2: Day38 lifecycle replay-compatible payload fields.
    # The replay state machine (app/trade_lifecycle/replay.py) requires specific
    # top-level payload keys for each event type:
    #   - order_id (str): required by OrderSubmitted, OrderFilled, OrderCancelled,
    #     OrderRejected, FillRecorded
    #   - cumulative_filled (int >= 1): required by OrderFilled
    #   - fill_quantity (int >= 1): required by FillRecorded
    # Only include when they have positive integer values because the replay
    # handler uses _require_payload_positive_int which rejects values < 1.

    # order_id: STRICTLY from the canonical application-order reference.
    # broker_order_id is a provider identity and is never used as the
    # Day38 order identity (Control Center Issue #1, §11).
    order_id_value = None
    if event.order_facts is not None and event.order_facts.order_id:
        order_id_value = event.order_facts.order_id
    if order_id_value:
        payload["order_id"] = order_id_value

    # cumulative_filled (int >= 1): from order_facts.cumulative_filled, else
    # fill_facts.cumulative_filled_after — only if positive
    cumulative_value = 0
    if (
        event.order_facts is not None
        and event.order_facts.cumulative_filled is not None
        and event.order_facts.cumulative_filled > 0
    ):
        cumulative_value = event.order_facts.cumulative_filled
    elif (
        event.fill_facts is not None
        and event.fill_facts.cumulative_filled_after is not None
        and event.fill_facts.cumulative_filled_after > 0
    ):
        cumulative_value = event.fill_facts.cumulative_filled_after
    if cumulative_value > 0:
        payload["cumulative_filled"] = cumulative_value

    # fill_quantity (int >= 1): from event.fill_facts.fill_quantity — only if positive
    if (
        event.fill_facts is not None
        and event.fill_facts.fill_quantity is not None
        and event.fill_facts.fill_quantity > 0
    ):
        payload["fill_quantity"] = event.fill_facts.fill_quantity

    append_lifecycle_event(
        db=db,
        aggregate_type="TradeLifecycle",
        aggregate_id=aggregate_id,
        event_type=lifecycle_event_type,
        event_version=event.event_version,
        tenant_id=event.tenant_id,
        sequence=day38_sequence,
        position_sequence=None,
        quantity_delta=None,
        position_identity=None,
        occurred_at=event.event_timestamp or event.received_at,
        payload=payload,
        metadata=None,
    )


def _resolve_execution_identity(
    db: Session,
    event: BrokerSyncEvent,
) -> str | None:
    """Resolve the actual StrikeNova execution identity for a broker event.

    The Day38 lifecycle aggregate must represent the ACTUAL StrikeNova
    execution (design §4/§5/§8), NOT the broker order.  A broker order ID
    is not automatically a StrikeNova execution ID.

    Resolution path (STRICT — no fallback):
        canonical application order reference (order_facts.order_id)
            → PaperOrder.client_order_id
            → StrategyExecution.execution_id

    The canonical application order reference is ``OrderFacts.order_id``
    (the application order ID carried by the broker-neutral event, per
    design §5 "canonical execution/order/fill references when available").
    We map it to ``PaperOrder.client_order_id`` — the application's
    per-order idempotency key (unique per user) — then to the execution
    that owns that order via ``PaperOrder.execution_id``.

    ``broker_order_id`` is a PROVIDER identity and is NEVER reinterpreted
    as an application order identity — there is NO fallback.

    Returns the resolved execution_id, or None if the broker event cannot
    be deterministically resolved to an existing execution (FAIL CLOSED).
    """
    # Canonical application order reference is REQUIRED.
    # broker_order_id is never silently reinterpreted as an application
    # order identity (Control Center Issue #1, §11).
    order_ref: str | None = None
    if event.order_facts is not None and event.order_facts.order_id:
        order_ref = event.order_facts.order_id
    if not order_ref:
        # FAIL CLOSED: without the canonical application-order reference the
        # broker_order_id must NOT be reinterpreted as an application order id.
        return None

    from app.models import PaperOrder

    order = db.execute(
        select(PaperOrder).where(
            PaperOrder.user_id == event.tenant_id,
            PaperOrder.client_order_id == order_ref,
        )
    ).scalar_one_or_none()
    if order is None or not order.execution_id:
        return None
    return order.execution_id


def _lock_execution_for_sequencing(
    db: Session, tenant_id: str, execution_id: str,
) -> None:
    """Serialize concurrent writers to the same execution aggregate.

    Takes a PostgreSQL row-level lock (SELECT ... FOR UPDATE) on the
    actual StrategyExecution row so that MAX+1 sequence allocation
    (next_event_sequence) is serialized per (tenant, execution).

    SQLite treats FOR UPDATE as a no-op (single-writer engine).
    Raises IngestionError (fail closed) if the row no longer exists.
    """
    from app.models import StrategyExecution

    locked = db.execute(
        select(StrategyExecution)
        .where(
            StrategyExecution.user_id == tenant_id,
            StrategyExecution.execution_id == execution_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise IngestionError(
            f"execution '{execution_id}' not found for tenant '{tenant_id}' "
            f"during sequence lock acquisition",
            action="REJECTED",
        )


def _allocate_day38_sequence(db: Session, event: BrokerSyncEvent, execution_id: str) -> int:
    """Allocate the next Day38 aggregate sequence, execution-serialized.

    Uses the existing Day38 ``next_event_sequence`` mechanism against the
    ACTUAL resolved execution aggregate.  The Day38 sequence is allocated
    independently of ``canonical_sequence``.

    Before allocation the actual ``StrategyExecution`` row is locked with
    ``SELECT ... FOR UPDATE`` (tenant-scoped) so that concurrent broker
    events for the same execution can never allocate the same Day38
    lifecycle sequence.  Two concurrent transactions block at the lock;
    the second sees the first's committed MAX(sequence) and allocates
    MAX+1.
    """
    _lock_execution_for_sequencing(db, event.tenant_id, execution_id)
    return next_event_sequence(db, event.tenant_id, "TradeLifecycle", execution_id)


# ---------------------------------------------------------------------------
# Main ingestion entry point
# ---------------------------------------------------------------------------

def ingest_canonical_event(
    event: BrokerSyncEvent,
    db: Session,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Consume a canonical broker event exactly once semantically.

    Durable pipeline: all operations share the caller's transaction.
    On any failure the caller must roll back the entire transaction.

    Args:
        event: A canonical ``BrokerSyncEvent`` (already constructed).
        db: A SQLAlchemy ``Session`` for durable persistence.
        tenant_id: Optional tenant context for projection.  If provided,
            the event's tenant_id must match.

    Returns:
        A result dictionary with:
        - ``canonical_id`` (str)
        - ``action``: ``APPLIED``, ``DUPLICATE_NOOP``, ``REJECTED``, ``CONFLICT``,
          ``STALE`` (preserved, not applied), or ``UNRESOLVED`` (quarantined)
        - ``normalized_state``: projected state dict when applied, else ``None``
        - ``reason``: explanation (None when action is APPLIED)
    """
    try:
        return _do_ingest(event, db, tenant_id)
    except IngestionError as e:
        return {
            "canonical_id": event.canonical_id,
            "action": e.action,
            "normalized_state": None,
            "reason": e.reason,
        }


# ---------------------------------------------------------------------------
# Day41.2 — Cross-D1 S1/S2 ordering (frozen r2 design §6/§7/§13)
# ---------------------------------------------------------------------------

# Quarantine status domain (design §13): APPLIED and STALE are terminal;
# UNRESOLVED is transient and resolvable through S3/S4.
_STATUS_QUARANTINE = ("STALE", "UNRESOLVED")


@dataclass(frozen=True)
class _CrossD1Classification:
    """Pure classification outcome (design §6 contract).

    ``outcome`` ∈ {FIRST, AUTHORIZED, STALE, UNRESOLVED}; ``reason`` carries
    the approved-evidence explanation (S1/S2/S3 authority reference, scope
    guard, or mixed-authority flag).  Consumes ONLY approved evidence: S1
    ``canonical_sequence``, S2 ``event_timestamp``, S3 ``source_mode``
    RECOVERY, and D1-scope identity (event_type).  Never compares state
    names, event-type ordering, receipt time, or row ids.
    """

    outcome: str
    reason: str


def _as_utc(ts: datetime | None) -> datetime | None:
    """Normalize an S2 timestamp to timezone-aware UTC (comparison only).

    Naive datetimes (SQLite test round-trips) are interpreted as UTC,
    matching PostgreSQL ``TIMESTAMPTZ`` storage semantics; aware values are
    converted to UTC.  Pure normalization — introduces no ordering authority
    and consumes no evidence beyond the approved S2 timestamp itself.
    """
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _classify_cross_d1(
    event: BrokerSyncEvent,
    previous: Any,
) -> _CrossD1Classification:
    """Classify an incoming observation against the previous family state.

    Pure function — no database access, no mutation, deterministic.

    Scope guards (steps 1–4) route everything that is NOT a sequence-less
    cross-D1 pair to the existing architecture unchanged; only step 5
    applies the human-approved cross-D1 S2 rule (decision memo
    ``2026-09-12-strikenova-cross-d1-regression-human-architecture-decision.md``):

        T_in > T_prev   → AUTHORIZED (supersession)
        T_in < T_prev   → STALE (preserved, never applied)
        T_in == T_prev  → UNRESOLVED (quarantine)
        missing (either) → UNRESOLVED (quarantine)

    S1 (provider sequence) strictly outranks S2.  S3 (history-sourced
    RECOVERY observation) is consulted exactly when S2 cannot decide —
    ladder rank 3 above the quarantine fallback.  No lifecycle-state
    ranking is created or consulted (Invariant: no invented ordering).
    """
    # 1. No previous observation exists ≠ "timestamp missing" (§16):
    #    the lone-observation application path (Day40.3 §5.3 single-observation
    #    note) applies even when the event's own S2 evidence is absent.
    if previous is None:
        return _CrossD1Classification(
            "FIRST",
            "no previous observation exists for the order family (first application)",
        )

    # 2. Incoming sequence-bearing → existing S1 machinery governs
    #    (anchor validation / CAS).  Behavior byte-identical to pre-Day41.2.
    if event.canonical_sequence is not None:
        return _CrossD1Classification(
            "AUTHORIZED",
            "S1 scope: provider sequence present — existing sequence machinery governs",
        )

    # 3. Previous sequence-bearing + incoming sequence-less: MIXED pair.
    #    OUT OF SCOPE — HUMAN DECISION REQUIRED (design §18): behavior is
    #    unchanged (applies through the existing path); the reason flags the
    #    mixed-authority boundary.  No new semantics are invented here.
    if previous.canonical_sequence is not None:
        return _CrossD1Classification(
            "AUTHORIZED",
            "mixed authority pair (previous sequence-bearing, incoming sequence-less) "
            "— behavior unchanged; OUT OF SCOPE — HUMAN DECISION REQUIRED",
        )

    # 4. Same D1 scope (same family + same event_type — D1 includes
    #    event_type per Day40 §2.2): PR-8 / Day40.3 §5.3 same-D1 behavior
    #    remains authoritative.  Never routed through the cross-D1 rule.
    if previous.event_type == event.event_type:
        return _CrossD1Classification(
            "AUTHORIZED",
            "same D1 scope (same event_type) — existing Day40.3 §5.3 same-D1 "
            "correction behavior governs; cross-D1 rule not applied",
        )

    # 5. Both sequence-less, different D1, same family: the approved S2 rule.
    #    Timezone-normalize for comparison only (engine-neutral strictness):
    #    SQLite round-trips return naive UTC while events carry aware UTC;
    #    PostgreSQL TIMESTAMPTZ always returns aware.  No new authority.
    t_in = _as_utc(event.event_timestamp)
    t_prev = _as_utc(previous.event_timestamp)
    if t_in is not None and t_prev is not None:
        # Second precision (decision memo: Upstox exchange timestamps are
        # second-precision; sub-second noise must not create a false strict
        # ordering).  "Equal at second precision" is UNRESOLVED, not a tie
        # to be broken by anything unapproved.
        t_in_s = t_in.replace(microsecond=0)
        t_prev_s = t_prev.replace(microsecond=0)
        if t_in_s > t_prev_s:
            return _CrossD1Classification(
                "AUTHORIZED",
                f"S2: incoming exchange_timestamp {t_in_s.isoformat()} is newer than "
                f"previous {t_prev_s.isoformat()} — authorized supersession",
            )
        if t_in_s < t_prev_s:
            return _CrossD1Classification(
                "STALE",
                f"S2: incoming exchange_timestamp {t_in_s.isoformat()} is older than "
                f"previous {t_prev_s.isoformat()} — STALE, preserved and not applied",
            )
        # Equal at second precision → S3 fallback (design §6 step 5b).
        if event.source_mode == BrokerEventSourceMode.RECOVERY:
            return _CrossD1Classification(
                "AUTHORIZED",
                "S3: history-sourced observation resolves the family "
                "(authoritative order-history ordering, ladder rank 3)",
            )
        return _CrossD1Classification(
            "UNRESOLVED",
            "S2 equal at second precision — unresolved, quarantined for S3/S4",
        )

    # Missing S2 evidence on either side → S3 fallback, else UNRESOLVED.
    if event.source_mode == BrokerEventSourceMode.RECOVERY:
        return _CrossD1Classification(
            "AUTHORIZED",
            "S3: history-sourced observation resolves the family "
            "(authoritative order-history ordering, ladder rank 3)",
        )
    if t_in is None:
        return _CrossD1Classification(
            "UNRESOLVED",
            "incoming S2 evidence missing — unresolved, quarantined for S3/S4",
        )
    return _CrossD1Classification(
        "UNRESOLVED",
        "previous S2 evidence missing — unresolved, quarantined for S3/S4",
    )


def _settled_idempotency_outcome(
    existing_idem: BrokerSyncIdempotency | None,
    fingerprint: str,
    canonical_id: str,
) -> dict[str, Any] | None:
    """Durable outcome for a settled idempotency record, or None to proceed.

    Day41.2 replay semantics (frozen r2 design §15 replay matrix): an
    identical replay re-emits the DURABLE outcome already recorded —
    APPLIED → DUPLICATE_NOOP (existing invariant); STALE/UNRESOLVED → their
    own status, because a preserved-but-unapplied observation must never
    convert to APPLIED by replaying and no duplicate idempotency record may
    be created.  Same identity with different content → CONFLICT.

    Called twice on the application path: once before the D-1 family lock
    (lock-free fast path for already-settled replays) and once after the
    lock is held (authoritative re-check — a concurrent identical replay
    that committed while this worker waited for the lock is then visible,
    restoring the DUPLICATE_NOOP contract under true concurrency).
    """
    if existing_idem is None:
        return None
    if existing_idem.content_fingerprint == fingerprint:
        if existing_idem.status in _STATUS_QUARANTINE:
            return {
                "canonical_id": canonical_id,
                "action": existing_idem.status,
                "normalized_state": None,
                "reason": (
                    f"replay of {existing_idem.status} observation "
                    f"(preserved, not applied)"
                ),
            }
        # Identical duplicate — no-op
        return {
            "canonical_id": canonical_id,
            "action": "DUPLICATE_NOOP",
            "normalized_state": None,
            "reason": "already applied (durable)",
        }
    # Same identity, different content — conflict
    return {
        "canonical_id": canonical_id,
        "action": "CONFLICT",
        "normalized_state": None,
        "reason": (
            f"canonical_id {canonical_id} exists with different content "
            f"(stored={existing_idem.content_fingerprint[:16]}..., "
            f"incoming={fingerprint[:16]}...)"
        ),
    }


def _lock_order_family(
    db: Session,
    *,
    tenant_id: str,
    broker: str,
    broker_order_id: str,
) -> None:
    """Acquire the D-1 order-family synchronization lock (frozen r2 design §7).

    First-row creation is atomic: ``INSERT … ON CONFLICT DO NOTHING`` — the
    unique-key insert is the first-observer arbitration (the Day41.1-proven
    Lane-B pattern).  Check-then-insert is deliberately NOT used: two
    concurrent first observers could both see "missing" and proceed
    unserialized.  The follow-up ``SELECT … FOR UPDATE`` grants exclusive
    transaction ownership: a concurrent worker blocks here until this
    transaction commits or rolls back.  The lock is held by the caller's
    transaction until commit/rollback — never released early — and confers
    no semantic meaning by acquisition order.

    The lock row stores NO semantic data (no sequence, lifecycle, identity,
    or timestamp fields) and acquiring it never touches the S1 sequence
    anchor.
    """
    db.execute(
        dialect_insert(db.get_bind(), OrderFamilySyncLock.__table__)
        .values(
            tenant_id=tenant_id,
            broker=broker,
            broker_order_id=broker_order_id,
        )
        .on_conflict_do_nothing()
    )
    db.execute(
        select(OrderFamilySyncLock)
        .where(
            OrderFamilySyncLock.tenant_id == tenant_id,
            OrderFamilySyncLock.broker == broker,
            OrderFamilySyncLock.broker_order_id == broker_order_id,
        )
        .with_for_update()
    ).scalar_one_or_none()


def _resolve_family_unresolved(
    db: Session,
    *,
    tenant_id: str,
    broker: str,
    broker_order_id: str,
    evidence: str,
) -> int:
    """Transition family UNRESOLVED records → STALE with resolution evidence.

    Used by the S3 same-transaction hook (history-sourced application) and
    the S4 REJECT adjudication.  One-way: only rows currently in the
    UNRESOLVED (transient) state transition; APPLIED/STALE rows are never
    touched, so resolution is idempotent.  Returns the number of rows
    resolved.
    """
    result = db.execute(
        update(BrokerSyncIdempotency)
        .where(
            BrokerSyncIdempotency.tenant_id == tenant_id,
            BrokerSyncIdempotency.broker == broker,
            BrokerSyncIdempotency.broker_order_id == broker_order_id,
            BrokerSyncIdempotency.status == "UNRESOLVED",
        )
        .values(status="STALE", resolution_evidence=evidence)
    )
    return int(result.rowcount or 0)


def _record_quarantine(
    db: Session,
    *,
    event: BrokerSyncEvent,
    canonical_id: str,
    fingerprint: str,
    validated_sequence: int | None,
    status: str,
) -> dict[str, Any] | None:
    """Persist a STALE/UNRESOLVED quarantine record (frozen r2 design §10).

    Only the idempotency row is written, inside a SAVEPOINT: the observation
    is preserved with its S2 evidence, but no projection row, no lifecycle
    effect, and no anchor mutation occur.  ``event_timestamp`` is persisted
    STRICTLY from the event (missing stays NULL).

    Returns a reclassification result dict when a concurrent duplicate is
    detected (caller must return it verbatim), else None.
    """
    try:
        with db.begin_nested():
            db.add(
                BrokerSyncIdempotency(
                    canonical_id=canonical_id,
                    tenant_id=event.tenant_id,
                    broker=event.broker,
                    broker_order_id=event.broker_order_id,
                    canonical_sequence=validated_sequence,
                    event_type=event.event_type,
                    event_version=event.event_version,
                    content_fingerprint=fingerprint,
                    source_mode=event.source_mode.value,
                    provider_event_id=event.provider_event_id,
                    received_at=event.received_at,
                    event_timestamp=event.event_timestamp,
                    status=status,
                    resolution_evidence=None,
                )
            )
            db.flush()
    except SAIntegrityError:
        # Concurrent worker recorded the same canonical_id first — re-classify
        # through the committed record (same durable-arbitration pattern as
        # the application SAVEPOINT below).
        concurrent_idem = db.execute(
            select(BrokerSyncIdempotency).where(
                BrokerSyncIdempotency.canonical_id == canonical_id
            )
        ).scalar_one_or_none()
        if concurrent_idem is not None and concurrent_idem.content_fingerprint == fingerprint:
            return {
                "canonical_id": canonical_id,
                "action": (
                    concurrent_idem.status
                    if concurrent_idem.status in _STATUS_QUARANTINE
                    else "DUPLICATE_NOOP"
                ),
                "normalized_state": None,
                "reason": "concurrent worker recorded identical observation (durable)",
            }
        return {
            "canonical_id": canonical_id,
            "action": "CONFLICT",
            "normalized_state": None,
            "reason": (
                f"concurrent write conflict on canonical_id {canonical_id} "
                f"(quarantine record)"
            ),
        }
    return None


def resolve_unresolved_for_family(
    db: Session,
    *,
    tenant_id: str,
    broker: str,
    broker_order_id: str,
    decision: str,
    evidence_reference: str,
    event: BrokerSyncEvent | None = None,
) -> dict[str, Any]:
    """S4 operator adjudication for a family's UNRESOLVED records (design §13).

    Contract (frozen r2 design §13 — no invented workflow):
    - requires a documented operator decision (``AUTHORIZE`` | ``REJECT``)
      and a non-empty evidence reference (durable, ``S4:<reference>``);
    - ``REJECT``  → family UNRESOLVED records transition to STALE (one-way,
      idempotent — only UNRESOLVED rows are touched);
    - ``AUTHORIZE`` → the ORIGINAL observation is applied through the normal
      Task2 path; S4 is ladder rank-4 authority, so the operator decision
      overrides the UNRESOLVED S2 classification for exactly this
      observation;
    - one-way and idempotent: already-APPLIED → DUPLICATE_NOOP,
      already-STALE → STALE (no re-adjudication), non-UNRESOLVED states are
      rejected fail-closed;
    - resolution and its effects share the caller's single transaction — the
      D-1 family lock serializes adjudication with concurrent family
      synchronization decisions, and a rollback restores the UNRESOLVED
      quarantine exactly (no partial semantic effect).
    """
    if decision not in ("AUTHORIZE", "REJECT"):
        raise IngestionError(
            f"invalid S4 decision '{decision}' (expected AUTHORIZE or REJECT)",
            action="REJECTED",
        )
    if not evidence_reference or not evidence_reference.strip():
        raise IngestionError(
            "S4 adjudication requires a non-empty operator evidence reference",
            action="REJECTED",
        )
    evidence = f"S4:{evidence_reference.strip()}"

    # D-1: same serialization domain as classification/application.
    _lock_order_family(
        db,
        tenant_id=tenant_id,
        broker=broker,
        broker_order_id=broker_order_id,
    )

    if decision == "REJECT":
        resolved = _resolve_family_unresolved(
            db,
            tenant_id=tenant_id,
            broker=broker,
            broker_order_id=broker_order_id,
            evidence=evidence,
        )
        db.flush()
        return {
            "action": "REJECTED",
            "resolved": resolved,
            "reason": (
                f"S4 adjudication REJECT (evidence: {evidence_reference.strip()}) — "
                f"family UNRESOLVED records transitioned to STALE"
            ),
        }

    # --- AUTHORIZE: apply the original observation through the normal path ---
    if event is None:
        raise IngestionError(
            "S4 AUTHORIZE requires the original observation to apply",
            action="REJECTED",
        )
    if (event.tenant_id, event.broker, event.broker_order_id or "") != (
        tenant_id,
        broker,
        broker_order_id,
    ):
        raise IngestionError(
            "S4 AUTHORIZE event does not belong to the adjudicated order family",
            action="REJECTED",
        )

    canonical_id = event.canonical_id
    fingerprint = _content_fingerprint(event)
    row = db.execute(
        select(BrokerSyncIdempotency).where(
            BrokerSyncIdempotency.canonical_id == canonical_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise IngestionError(
            "S4 AUTHORIZE precondition failed: no quarantined record exists "
            "for this observation",
            action="REJECTED",
        )
    if row.status == "APPLIED":
        return {
            "action": "DUPLICATE_NOOP",
            "resolved": 0,
            "reason": "S4 AUTHORIZE no-op: observation already applied",
        }
    if row.status == "STALE":
        return {
            "action": "STALE",
            "resolved": 0,
            "reason": (
                "S4 AUTHORIZE no-op: observation already resolved (STALE) — "
                "one-way transition"
            ),
        }
    if row.status != "UNRESOLVED":
        raise IngestionError(
            f"S4 AUTHORIZE precondition failed: record status is {row.status}, "
            f"expected UNRESOLVED",
            action="REJECTED",
        )
    if row.content_fingerprint != fingerprint:
        raise IngestionError(
            f"S4 AUTHORIZE content mismatch for canonical_id {canonical_id[:16]}... "
            f"— conflict, not adjudication",
            action="CONFLICT",
        )

    # Remove the quarantine record within THIS transaction; the fresh
    # application recreates it with the adjudicated outcome and evidence.
    # On any failure below, the whole transaction (including this delete)
    # rolls back — the UNRESOLVED quarantine survives intact.
    db.delete(row)
    db.flush()
    result = _do_ingest(event, db, tenant_id, s4_authorized=True)
    if result["action"] == "APPLIED":
        db.execute(
            update(BrokerSyncIdempotency)
            .where(BrokerSyncIdempotency.canonical_id == canonical_id)
            .values(resolution_evidence=evidence)
        )
        db.flush()
        return result

    # Re-apply did not produce an applied record — restore the UNRESOLVED
    # quarantine in the same transaction (the observation is never lost).
    db.add(
        BrokerSyncIdempotency(
            canonical_id=canonical_id,
            tenant_id=event.tenant_id,
            broker=event.broker,
            broker_order_id=event.broker_order_id,
            canonical_sequence=event.canonical_sequence,
            event_type=event.event_type,
            event_version=event.event_version,
            content_fingerprint=fingerprint,
            source_mode=event.source_mode.value,
            provider_event_id=event.provider_event_id,
            received_at=event.received_at,
            event_timestamp=event.event_timestamp,
            status="UNRESOLVED",
            resolution_evidence=None,
        )
    )
    db.flush()
    raise IngestionError(
        f"S4 AUTHORIZE could not apply the observation ({result.get('reason')})",
        action="REJECTED",
    )


def _do_ingest(
    event: BrokerSyncEvent,
    db: Session,
    tenant_id: str | None = None,
    s4_authorized: bool = False,
) -> dict[str, Any]:
    canonical_id = event.canonical_id

    # --- Malformed event check ---
    if not canonical_id:
        return {
            "canonical_id": canonical_id,
            "action": "REJECTED",
            "normalized_state": None,
            "reason": "empty canonical identity",
        }

    # --- Tenant isolation ---
    if tenant_id is not None and not event.belongs_to_tenant(tenant_id):
        return {
            "canonical_id": canonical_id,
            "action": "REJECTED",
            "normalized_state": None,
            "reason": (
                f"tenant mismatch: event tenant '{event.tenant_id}' "
                f"!= projection tenant '{tenant_id}'"
            ),
        }

    # --- Day40.4 §3.4 CEID verification hook (BEFORE idempotency) ---
    # When a Task3-derived event carries the strikenova identity metadata
    # block, the supplied canonical_event_id MUST equal the derived
    # CEID = SHA256("CEIDv1:" || d1 || content_fingerprint).  A mismatch means
    # the event's identity does not derive from its claimed correlation
    # identity + content: reject fail-closed before any idempotency
    # arbitration can record it.
    ceid_metadata_error = _verify_ceid_metadata(event)
    if ceid_metadata_error is not None:
        return {
            "canonical_id": canonical_id,
            "action": "REJECTED",
            "normalized_state": None,
            "reason": ceid_metadata_error,
        }

    # --- Compute content fingerprint ---
    fingerprint = _content_fingerprint(event)

    # --- Durable idempotency check (BEFORE broker sequence validation) ---
    # Durable canonical identity takes precedence over broker ordering:
    #   canonical_id exists + same fingerprint  -> DUPLICATE_NOOP
    #   canonical_id exists + different content -> CONFLICT
    # both REGARDLESS of the incoming canonical_sequence relative to the
    # broker sequence anchor.  A previously persisted event must not become
    # STALE merely because later events have already advanced the anchor.
    #
    # Genuinely NEW events (unknown canonical_id) still undergo full
    # sequence validation below (duplicate / gap / stale / out-of-order),
    # so stale detection for new events is NOT weakened.
    #
    # Concurrency: this pre-lock read is a FAST PATH for already-settled
    # replays (no D-1 lock needed to re-emit a committed decision).  The
    # authoritative arbitration happens AFTER the D-1 family lock below —
    # the settled outcome is re-checked under the lock, so a concurrent
    # identical replay that committed while this worker waited is classified
    # DUPLICATE_NOOP and never reaches sequence validation.  Concurrent
    # NEW-event ingestion is additionally arbitrated durably inside the
    # SAVEPOINT (idempotency PK insert conflict) and by the anchor CAS.
    existing_idem = db.execute(
        select(BrokerSyncIdempotency).where(
            BrokerSyncIdempotency.canonical_id == canonical_id
        )
    ).scalar_one_or_none()

    settled = _settled_idempotency_outcome(existing_idem, fingerprint, canonical_id)
    if settled is not None:
        return settled

    # --- Day41.2 D-1 order-family lock (frozen r2 design §7/§11) ---
    # Acquired BEFORE any locked-variant read (sequence anchor, authoritative
    # previous state) so the READ → CLASSIFY → WRITE sequence is serialized
    # per order family; holding it to commit/rollback is what makes the final
    # state S2-authoritative rather than arrival-order.  It must precede
    # sequence validation: a concurrent duplicate replay that lost the lock
    # must re-observe the winner's committed idempotency record above
    # (DUPLICATE_NOOP), never fail stale-detection against the winner's
    # advanced anchor.  Acquisition is semantically pure: it never mutates
    # the S1 sequence anchor and stores no state (D-1 decision memo).  The
    # lock row is a pure mutex keyed (tenant_id, broker, broker_order_id);
    # creating one for an event that is subsequently rejected fail-closed
    # stores no semantic state.
    _lock_order_family(
        db,
        tenant_id=event.tenant_id,
        broker=event.broker,
        broker_order_id=event.broker_order_id or "",
    )

    # Authoritative re-check under the family lock (see above): restores the
    # §15 replay matrix under true concurrency.
    existing_idem = db.execute(
        select(BrokerSyncIdempotency).where(
            BrokerSyncIdempotency.canonical_id == canonical_id
        )
    ).scalar_one_or_none()
    settled = _settled_idempotency_outcome(existing_idem, fingerprint, canonical_id)
    if settled is not None:
        return settled

    # --- Broker sequence position validation (new events only) ---
    # Validates ordering without advancing the anchor.  Advancement happens
    # inside the SAVEPOINT after all durable effects succeed, so a failed
    # application rolls back the sequence anchor too.
    sequence_position = _validate_broker_sequence_position(db, event)
    validated_sequence = sequence_position["incoming"] if sequence_position else None

    # --- FIX 2: Reclassify same-sequence race through idempotency ---
    # When the broker sequence position indicates a duplicate sequence
    # (incoming == last_sequence), check if a DIFFERENT canonical event has
    # already claimed this sequence for the same broker order.  If so, this
    # is a same-sequence conflict, not an independent observation.
    if sequence_position is not None and sequence_position["is_duplicate"]:
        existing_for_sequence = db.execute(
            select(BrokerSyncIdempotency).where(
                BrokerSyncIdempotency.tenant_id == event.tenant_id,
                BrokerSyncIdempotency.broker == event.broker,
                BrokerSyncIdempotency.broker_order_id == event.broker_order_id,
                BrokerSyncIdempotency.canonical_sequence == validated_sequence,
                BrokerSyncIdempotency.canonical_id != canonical_id,
            )
        ).scalar_one_or_none()
        if existing_for_sequence is not None:
            raise IngestionError(
                f"canonical_sequence={validated_sequence} already consumed by a "
                f"different event for order {event.broker_order_id} "
                f"(existing={existing_for_sequence.canonical_id[:16]}..., "
                f"incoming={canonical_id[:16]}...)",
                action="CONFLICT",
            )

    # --- Map to Day38 lifecycle event type (explicit mapping) ---
    # This will raise IngestionError for unmappable types (e.g. ORDER_RECOVERED).
    # A None result marks a projection-only broker event (ORDER_ACCEPTED —
    # approved Day38 design §13/§14): ingestable, but with no Day38 state
    # transition, so it consumes NO Day38 sequence and appends NO lifecycle
    # event.  A duplicated transition would be non-replayable.
    lifecycle_event_type = _map_to_lifecycle_event_type(event.event_type)
    projection_only = lifecycle_event_type is None

    # --- Resolve actual StrikeNova execution identity (v6) ---
    # The Day38 lifecycle aggregate must be the ACTUAL execution, not the
    # broker order.  broker_order_id is NEVER reinterpreted as an
    # application order id.  Missing or unknown references FAIL CLOSED
    # (REJECTED) with no synthetic aggregate, no projection, no idempotency,
    # no lifecycle event, no anchor advancement.
    execution_id = _resolve_execution_identity(db, event)
    if execution_id is None:
        app_ref = (event.order_facts.order_id
                    if event.order_facts else None)
        if app_ref:
            reason_text = (
                f"unresolved broker order: canonical application order "
                f"reference '{app_ref}' does not match any PaperOrder for "
                f"tenant '{event.tenant_id}'. broker_order_id "
                f"'{event.broker_order_id}' is not used as an application "
                f"order identity. Failing closed; unknown broker state "
                f"remains observable for recovery."
            )
        else:
            reason_text = (
                f"unresolved broker order: missing canonical application "
                f"order reference (order_facts.order_id); broker_order_id "
                f"'{event.broker_order_id}' is a provider identity and "
                f"will not be reinterpreted as an application order id. "
                f"Failing closed; unknown broker state remains observable "
                f"for recovery."
            )
        return {
            "canonical_id": canonical_id,
            "action": "REJECTED",
            "normalized_state": None,
            "reason": reason_text,
        }

    # --- Find previous projection (deterministic: ordered by canonical_sequence) ---
    # (Read INSIDE the D-1 family lock acquired above.)
    # FIX 4: canonical_sequence is the primary ordering key; id is a deterministic
    # tiebreaker only (not a causal ordering mechanism) for events where
    # canonical_sequence is NULL.  The id column is stable within a database
    # session and provides a consistent, repeatable order for projection lookup.
    previous: BrokerOrderProjection | None = None
    if event.broker_order_id:
        previous = db.execute(
            select(BrokerOrderProjection)
            .where(
                BrokerOrderProjection.tenant_id == event.tenant_id,
                BrokerOrderProjection.broker == event.broker,
                BrokerOrderProjection.broker_order_id == event.broker_order_id,
            )
            .order_by(
                BrokerOrderProjection.canonical_sequence.desc().nullslast(),
                BrokerOrderProjection.id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()

    # --- Day41.2 cross-D1 S1/S2 classification (frozen r2 design §6) ---
    # Pure classification against the authoritative previous state, inside
    # the family lock.  FIRST/AUTHORIZED continue through the EXISTING
    # guards and application path unchanged; STALE/UNRESOLVED quarantine
    # below — after the existing terminal/quantity guards, which are never
    # weakened (a terminal order still rejects; guards keep precedence).
    classification = _classify_cross_d1(event, previous)

    # --- Day41.2 quarantine outcomes (frozen r2 design §10/§12/§13) ---
    # Branch off BEFORE the terminal/quantity guards — exactly the frozen
    # §11 integration order (classification → guards → fold): a STALE
    # observation is preserved, never applied, so terminal enforcement has
    # nothing to protect against it (no projection row, no lifecycle effect,
    # no anchor mutation — only the durable quarantine record).  An event
    # that would APPLY against terminal state is still rejected below,
    # unchanged.  UNRESOLVED: quarantined for S3/S4, resolvable, never a
    # dead end.  S4 AUTHORIZE (rank-4 authority) bypasses the UNRESOLVED
    # quarantine for exactly this adjudicated re-application
    # (s4_authorized); it never bypasses STALE (an operator cannot make an
    # older observation newer).  The quarantine write is a single
    # idempotency row inside its own SAVEPOINT — atomic and
    # concurrent-duplicate safe.
    if classification.outcome == "STALE":
        reclass = _record_quarantine(
            db,
            event=event,
            canonical_id=canonical_id,
            fingerprint=fingerprint,
            validated_sequence=validated_sequence,
            status="STALE",
        )
        if reclass is not None:
            return reclass
        return {
            "canonical_id": canonical_id,
            "action": "STALE",
            "normalized_state": None,
            "reason": classification.reason,
        }
    if classification.outcome == "UNRESOLVED" and not s4_authorized:
        reclass = _record_quarantine(
            db,
            event=event,
            canonical_id=canonical_id,
            fingerprint=fingerprint,
            validated_sequence=validated_sequence,
            status="UNRESOLVED",
        )
        if reclass is not None:
            return reclass
        return {
            "canonical_id": canonical_id,
            "action": "UNRESOLVED",
            "normalized_state": None,
            "reason": classification.reason,
        }

    # --- Terminal-state enforcement (before quantity validation) ---
    new_state = _event_canonical_state(event)
    if previous is not None and previous.is_terminal:
        # Terminal orders remain terminal — no post-terminal mutation allowed
        # ORDER_RECOVERED is rejected by the mapping layer, so it never
        # reaches here
        raise IngestionError(
            f"terminal state mutation rejected: order is {previous.status}, "
            f"cannot apply {event.event_type}",
            action="REJECTED",
        )

    # --- Validate quantity invariants ---
    _validate_quantity_invariants(event, previous)

    # --- Build projection ---
    projection = _build_projection(event, previous, validated_sequence)

    # --- Allocate Day38 sequence (independent of canonical_sequence) ---
    # Allocated against the ACTUAL resolved execution aggregate.
    # Projection-only broker events consume NO Day38 sequence: allocating
    # one would leave a sequence gap in the aggregate stream.
    day38_sequence = (
        None
        if projection_only
        else _allocate_day38_sequence(db, event, execution_id)
    )

    # --- Durable mutation, wrapped in a nested SAVEPOINT ---
    # Concurrency safety (FIX 1): two concurrent workers may pass the same
    # canonical_sequence (both read the same anchor, both advance it in their
    # own transaction).  Only one may win.  The loser's insert of the
    # idempotency record (PK canonical_id) will hit the DB-level unique
    # constraint.  We catch SAIntegrityError at the SAVEPOINT so the caller's
    # outer transaction stays intact and we can re-classify as a graceful
    # DUPLICATE_NOOP / CONFLICT instead of leaking an exception.
    #
    # FIX 1 (sequence coupling): the broker sequence anchor is advanced INSIDE
    # the SAVEPOINT, AFTER projection + idempotency + lifecycle have all been
    # persisted.  If any step fails, the SAVEPOINT rolls back all four effects
    # together, so the sequence anchor never represents an unapplied event.
    #
    # FIX 2 (first-use anchor atomicity): the anchor row is created INSIDE
    # the SAVEPOINT (via _ensure_broker_sequence_anchor), so a failed first-use
    # application rolls back the anchor along with all other durable effects.
    try:
        with db.begin_nested():
            # Ensure broker sequence anchor exists (first-use case)
            _ensure_broker_sequence_anchor(db, event, sequence_position)

            # Persist projection
            db.add(projection)
            db.flush()

            # Persist idempotency record
            idem = BrokerSyncIdempotency(
                canonical_id=canonical_id,
                tenant_id=event.tenant_id,
                broker=event.broker,
                broker_order_id=event.broker_order_id,
                canonical_sequence=validated_sequence,
                event_type=event.event_type,
                event_version=event.event_version,
                content_fingerprint=fingerprint,
                source_mode=event.source_mode.value,
                provider_event_id=event.provider_event_id,
                received_at=event.received_at,
                event_timestamp=event.event_timestamp,
                status="APPLIED",
            )
            db.add(idem)
            db.flush()

            # Day41.2 §13 S3 hook: a history-sourced (RECOVERY) application
            # resolves the family's UNRESOLVED quarantine rows → STALE with
            # durable S3 evidence, in THIS same transaction.  One-way and
            # idempotent (only UNRESOLVED rows transition); this event's own
            # APPLIED row is never touched.  A rollback removes both the
            # application and the resolution together.
            if event.source_mode == BrokerEventSourceMode.RECOVERY:
                _resolve_family_unresolved(
                    db,
                    tenant_id=event.tenant_id,
                    broker=event.broker,
                    broker_order_id=event.broker_order_id,
                    evidence=f"S3:{canonical_id}",
                )

            # Day38 lifecycle integration (against the ACTUAL execution).
            # Skipped for projection-only broker events (design §13/§14):
            # they carry no Day38 state transition.
            if not projection_only:
                _append_lifecycle_from_event(
                    db, event, day38_sequence, execution_id
                )

            # Advance broker sequence anchor AFTER all durable effects succeed.
            # This is the serialization point: if a concurrent worker already
            # advanced the anchor, this raises IngestionError(CONFLICT) which
            # rolls back the SAVEPOINT and triggers re-classification below.
            _advance_broker_sequence(db, event, sequence_position)
    except SAIntegrityError:
        # The SAVEPOINT was rolled back; the caller's outer transaction is
        # intact.  A concurrent worker committed the same canonical_id first.
        # Re-read the idempotency record to classify.
        concurrent_idem = db.execute(
            select(BrokerSyncIdempotency).where(
                BrokerSyncIdempotency.canonical_id == canonical_id
            )
        ).scalar_one_or_none()
        if concurrent_idem is not None:
            if concurrent_idem.content_fingerprint == fingerprint:
                return {
                    "canonical_id": canonical_id,
                    "action": "DUPLICATE_NOOP",
                    "normalized_state": None,
                    "reason": "concurrent worker applied identical event (durable)",
                }
            return {
                "canonical_id": canonical_id,
                "action": "CONFLICT",
                "normalized_state": None,
                "reason": (
                    f"concurrent worker applied canonical_id {canonical_id} "
                    f"with different content"
                ),
            }
        # No visible duplicate — a concurrent worker is mid-transaction.
        # Fail closed.
        return {
            "canonical_id": canonical_id,
            "action": "CONFLICT",
            "normalized_state": None,
            "reason": (
                f"concurrent write conflict on canonical_id {canonical_id} "
                f"(no committed idempotency record visible)"
            ),
        }
    except IngestionError as e:
        # Sequence advancement lost a race — re-classify through idempotency.
        if e.action == "CONFLICT":
            concurrent_idem = db.execute(
                select(BrokerSyncIdempotency).where(
                    BrokerSyncIdempotency.canonical_id == canonical_id
                )
            ).scalar_one_or_none()
            if concurrent_idem is not None:
                if concurrent_idem.content_fingerprint == fingerprint:
                    return {
                        "canonical_id": canonical_id,
                        "action": "DUPLICATE_NOOP",
                        "normalized_state": None,
                        "reason": "concurrent worker applied identical event (durable)",
                    }
                return {
                    "canonical_id": canonical_id,
                    "action": "CONFLICT",
                    "normalized_state": None,
                    "reason": (
                        f"concurrent worker applied canonical_id {canonical_id} "
                        f"with different content"
                    ),
                }
            return {
                "canonical_id": canonical_id,
                "action": "CONFLICT",
                "normalized_state": None,
                "reason": (
                    f"concurrent write conflict on canonical_id {canonical_id} "
                    f"(no committed idempotency record visible)"
                ),
            }
        raise

    # --- Build result ---
    normalized_state = {
        "canonical_id": projection.canonical_id,
        "tenant_id": projection.tenant_id,
        "broker_order_id": projection.broker_order_id,
        "event_type": projection.event_type,
        "status": projection.status,
        "total_quantity": projection.total_quantity,
        "cumulative_filled": projection.cumulative_filled,
        "remaining_quantity": projection.remaining_quantity,
        "average_price": projection.average_price,
        "last_fill_price": projection.last_fill_price,
        "last_fill_quantity": projection.last_fill_quantity,
        "is_terminal": projection.is_terminal,
        "fill_count": projection.fill_count,
        "last_fill_id": projection.last_fill_id,
        "rejection_reason": projection.rejection_reason,
    }

    return {
        "canonical_id": canonical_id,
        "action": "APPLIED",
        "normalized_state": normalized_state,
        "reason": None,
    }
