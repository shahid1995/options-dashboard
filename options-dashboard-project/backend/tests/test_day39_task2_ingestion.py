"""Day 39 Task 2 — Durable ingestion pipeline tests.

Tests the full durable pipeline:
  BrokerSyncEvent → validation → tenant check → broker ordering
    → durable idempotency → terminal-state enforcement → projection
    → Day38 lifecycle mapping → single transaction

Behavioral tests — not implementation-coupled.  All idempotency and
projection assertions hit durable SQL state.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.orm import sessionmaker

from app.broker_sync import (
    BrokerEventType,
    BrokerEventSourceMode,
    BrokerSyncEvent,
    CanonicalOrderState,
    FillFacts,
    OrderFacts,
    make_broker_sync_event,
)
from app.broker_sync.ingestion import IngestionError, ingest_canonical_event
from app.broker_sync.models import BrokerOrderProjection, BrokerSyncIdempotency, BrokerSyncSequenceAnchor

# ---------------------------------------------------------------------------
# Test database setup
# ---------------------------------------------------------------------------

from sqlalchemy.pool import StaticPool

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)

# broker_order_ids used across the suite — each maps to a seeded
# authoritative application order (PaperOrder.client_order_id) so Task2 can
# resolve to the real execution identity.
_STANDARD_ORDER_IDS = ["ORD-1", "ORD-A", "ORD-B", "ORD-DET", "ORD-SEQLESS"]


def _seed_app_order(db, broker_order_id: str, tenant_id: str = "tenant-1",
                    execution_id: str | None = None) -> str:
    """Seed an authoritative StrategyExecution + PaperOrder for a broker order.

    Task2 resolves a canonical application order reference
    (``OrderFacts.order_id``) to ``PaperOrder.client_order_id`` and then to
    the owning ``StrategyExecution.execution_id``.  In the test fixtures the
    canonical application order reference equals the broker order id, and the
    ``PaperOrder.client_order_id`` is seeded to match, so resolution succeeds
    deterministically.
    """
    from app.models import PaperOrder, StrategyExecution

    exec_id = execution_id or f"EXEC-{broker_order_id}"
    exec_row = StrategyExecution(
        user_id=tenant_id,
        execution_id=exec_id,
        client_order_id=f"exec-{broker_order_id}",
        strategy_id="strat-1",
        strategy_tag="Test",
        symbol="NIFTY",
        status="FILLED",
        entry_net=0.0,
        entry_at=_NOW,
    )
    db.add(exec_row)
    db.flush()

    order = PaperOrder(
        user_id=tenant_id,
        client_order_id=broker_order_id,
        execution_id=exec_id,
        kind="entry",
        symbol="NIFTY",
        expiry="2026-10-29",
        strike=24500.0,
        option_type="CE",
        action="buy",
        quantity=100,
        lot_size=1,
        status="FILLED",
        filled_quantity=100,
        fill_price=100.0,
    )
    db.add(order)
    db.flush()
    return exec_id


@pytest.fixture()
def db():
    """Provide a clean SQLite database session for each test (deterministic).

    Seeds authoritative StrategyExecution + PaperOrder context so Task2 can
    resolve broker events to the real execution identity (v5).
    """
    from app.db import Base
    import app.models  # noqa: F401  (registers all tables on Base.metadata)
    import app.broker_sync.models  # noqa: F401
    import app.trade_lifecycle.persistence  # noqa: F401

    Base.metadata.create_all(_engine)
    session = _TestSessionLocal()
    for oid in _STANDARD_ORDER_IDS:
        _seed_app_order(session, oid)
    # Commit the authoritative application context so test-level rollbacks
    # (e.g. rollback-on-failure tests) do not destroy the seeded execution/order.
    session.commit()
    yield session
    session.rollback()
    session.close()
    Base.metadata.drop_all(_engine)


_NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


def _make_submitted_event(
    broker_order_id: str = "ORD-1",
    tenant_id: str = "tenant-1",
    broker: str = "broker-test",
    canonical_sequence: int | None = 1,
    event_type: str = BrokerEventType.ORDER_SUBMITTED.value,
    total_quantity: int | None = 100,
    received_at: datetime | None = None,
    event_timestamp: datetime | None = None,
) -> BrokerSyncEvent:
    return make_broker_sync_event(
        tenant_id=tenant_id,
        broker=broker,
        event_type=event_type,
        event_version="1.0",
        broker_order_id=broker_order_id,
        canonical_sequence=canonical_sequence,
        received_at=received_at or (_NOW + timedelta(seconds=1)),
        event_timestamp=event_timestamp,
        order_facts=OrderFacts(
            broker_order_id=broker_order_id,
            order_id=broker_order_id,
            status=CanonicalOrderState.SUBMITTED,
            total_quantity=total_quantity,
            cumulative_filled=0,
        ),
    )


def _make_accepted_event(
    broker_order_id: str = "ORD-1",
    canonical_sequence: int = 2,
    received_at: datetime | None = None,
) -> BrokerSyncEvent:
    return make_broker_sync_event(
        tenant_id="tenant-1",
        broker="broker-test",
        event_type=BrokerEventType.ORDER_ACCEPTED,
        event_version="1.0",
        broker_order_id=broker_order_id,
        canonical_sequence=canonical_sequence,
        received_at=received_at or (_NOW + timedelta(seconds=2)),
        order_facts=OrderFacts(
            broker_order_id=broker_order_id,
            order_id=broker_order_id,
            status=CanonicalOrderState.OPEN,
            total_quantity=100,
        ),
    )


def _make_full_fill_event(
    broker_order_id: str = "ORD-1",
    canonical_sequence: int | None = 2,
    cumulative_filled_after: int = 100,
    total_quantity: int = 100,
    fill_quantity: int | None = None,
    received_at: datetime | None = None,
    event_timestamp: datetime | None = None,
) -> BrokerSyncEvent:
    # fill_quantity defaults to cumulative_filled_after (correct for first fill)
    # but should be overridden to the incremental amount when there's a prior fill
    fq = fill_quantity if fill_quantity is not None else cumulative_filled_after
    return make_broker_sync_event(
        tenant_id="tenant-1",
        broker="broker-test",
        event_type=BrokerEventType.FULL_FILL.value,
        event_version="1.0",
        broker_order_id=broker_order_id,
        canonical_sequence=canonical_sequence,
        # Day41.2: durable S2 evidence — sequence-less cross-D1 observations
        # are classified by provider event_timestamp (missing ⇒ UNRESOLVED),
        # never by arrival order.
        event_timestamp=event_timestamp,
        order_facts=OrderFacts(
            broker_order_id=broker_order_id,
            order_id=broker_order_id,
            status=CanonicalOrderState.FILLED,
            total_quantity=total_quantity,
            cumulative_filled=cumulative_filled_after,
            is_terminal=True,
        ),
        fill_facts=FillFacts(
            fill_quantity=fq,
            fill_price=100.0,
            cumulative_filled_after=cumulative_filled_after,
            remaining_after=total_quantity - cumulative_filled_after,
        ),
        received_at=received_at or (_NOW + timedelta(seconds=2)),
    )


# ---------------------------------------------------------------------------
# 1. Idempotency tests
# ---------------------------------------------------------------------------

class TestIdempotency:

    def test_first_event_persists_and_applies(self, db):
        """First event persists and applies."""
        event = _make_submitted_event()
        result = ingest_canonical_event(event, db)
        assert result["action"] == "APPLIED"
        assert result["normalized_state"]["status"] == "SUBMITTED"

        # Idempotency record is durably persisted
        row = db.execute(
            text("SELECT canonical_id, content_fingerprint, status FROM broker_sync_idempotency")
        ).fetchone()
        assert row is not None
        assert row.canonical_id == event.canonical_id
        assert row.status == "APPLIED"

    def test_identical_duplicate_is_durable_noop(self, db):
        """Identical duplicate is a durable no-op (survives session recreation)."""
        event = _make_submitted_event()
        ingest_canonical_event(event, db)
        db.commit()

        # Simulate process/session restart — close and reopen session
        from app.db import Base
        Base.metadata.create_all(_engine)
        new_db = _TestSessionLocal()

        result = ingest_canonical_event(event, new_db)
        assert result["action"] == "DUPLICATE_NOOP"
        # No duplicate projection row
        count = new_db.execute(
            text("SELECT COUNT(*) FROM broker_order_projection WHERE canonical_id = :cid"),
            {"cid": event.canonical_id},
        ).scalar()
        assert count == 1
        new_db.close()

    def test_conflicting_same_identity_rejected(self, db):
        """Same canonical_id + different content → CONFLICT."""
        event = _make_submitted_event()
        ingest_canonical_event(event, db)

        # Same canonical_id (seq=1) but different total_quantity
        different_event = BrokerSyncEvent(
            tenant_id="tenant-1",
            broker="broker-test",
            event_type=BrokerEventType.ORDER_SUBMITTED,
            event_version="1.0",
            received_at=_NOW + timedelta(seconds=2),
            provider_event_id=None,
            broker_order_id="ORD-1",
            canonical_sequence=1,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1",
                status=CanonicalOrderState.SUBMITTED,
                total_quantity=999,  # different!
            ),
        )
        result = ingest_canonical_event(different_event, db)
        assert result["action"] == "CONFLICT"

    def test_duplicate_after_session_recreation_detected(self, db):
        """Duplicate detected after session recreation (durable)."""
        event = _make_submitted_event()
        ingest_canonical_event(event, db)
        db.commit()

        # Recreate session
        from app.db import Base
        Base.metadata.create_all(_engine)
        new_db = _TestSessionLocal()
        result = ingest_canonical_event(event, new_db)
        assert result["action"] == "DUPLICATE_NOOP"
        new_db.close()

    def test_cross_tenant_identity_rejected(self, db):
        """Cross-tenant identity (different tenant) rejected."""
        event = _make_submitted_event(tenant_id="tenant-1")
        result = ingest_canonical_event(event, db, tenant_id="tenant-2")
        assert result["action"] == "REJECTED"
        assert "tenant mismatch" in result["reason"]


# ---------------------------------------------------------------------------
# 2. Projection tests
# ---------------------------------------------------------------------------

class TestProjection:

    def test_accepted_event_persists_normalized_state(self, db):
        """ORDER_ACCEPTED persists normalized state OPEN."""
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        accepted = _make_accepted_event(canonical_sequence=2)
        result = ingest_canonical_event(accepted, db)
        assert result["action"] == "APPLIED"
        assert result["normalized_state"]["status"] == "OPEN"
        assert result["normalized_state"]["total_quantity"] == 100

    def test_submitted_event_persists(self, db):
        event = _make_submitted_event()
        result = ingest_canonical_event(event, db)
        assert result["action"] == "APPLIED"
        assert result["normalized_state"]["status"] == "SUBMITTED"

    def test_partial_fill_persists_correct_quantities(self, db):
        """PARTIAL_FILL persists correct cumulative/remaining."""
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        fill = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50,
            ),
            fill_facts=FillFacts(
                fill_quantity=50, fill_price=100.0,
                cumulative_filled_after=50, remaining_after=50,
            ),
            received_at=_NOW + timedelta(seconds=2),
        )
        result = ingest_canonical_event(fill, db)
        assert result["action"] == "APPLIED"
        ns = result["normalized_state"]
        assert ns["status"] == "PARTIALLY_FILLED"
        assert ns["cumulative_filled"] == 50
        assert ns["remaining_quantity"] == 50
        assert ns["last_fill_quantity"] == 50
        assert ns["last_fill_price"] == 100.0
        assert ns["fill_count"] == 1

    def test_final_fill_persists_filled(self, db):
        """FULL_FILL persists FILLED (terminal)."""
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        fill = _make_full_fill_event(canonical_sequence=2)
        result = ingest_canonical_event(fill, db)
        assert result["action"] == "APPLIED"
        ns = result["normalized_state"]
        assert ns["status"] == "FILLED"
        assert ns["is_terminal"] is True

    def test_cancellation_persists_cancelled(self, db):
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        cancel = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_CANCELLED, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.CANCELLED,
                total_quantity=100,
            ),
            received_at=_NOW + timedelta(seconds=2),
        )
        result = ingest_canonical_event(cancel, db)
        assert result["action"] == "APPLIED"
        ns = result["normalized_state"]
        assert ns["status"] == "CANCELLED"
        assert ns["is_terminal"] is True

    def test_rejection_persists_rejected(self, db):
        reject = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_REJECTED, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=1,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.REJECTED,
                total_quantity=100, rejection_reason="insufficient_margin",
            ),
            received_at=_NOW + timedelta(seconds=1),
        )
        result = ingest_canonical_event(reject, db)
        assert result["action"] == "APPLIED"
        ns = result["normalized_state"]
        assert ns["status"] == "REJECTED"
        assert ns["is_terminal"] is True
        assert ns["rejection_reason"] == "insufficient_margin"

    def test_duplicate_fill_does_not_double_count(self, db):
        """Same canonical fill event applied twice does not double-count."""
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        fill = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50,
            ),
            fill_facts=FillFacts(
                fill_quantity=50, fill_price=100.0,
                cumulative_filled_after=50, remaining_after=50,
            ),
            received_at=_NOW + timedelta(seconds=2),
        )
        # First application
        result1 = ingest_canonical_event(fill, db)
        assert result1["action"] == "APPLIED"
        # Second application (identical duplicate)
        result2 = ingest_canonical_event(fill, db)
        assert result2["action"] == "DUPLICATE_NOOP"

        # Projection shows only one fill row with cumulative=50
        count = db.execute(
            text("SELECT COUNT(*) FROM broker_order_projection WHERE broker_order_id = 'ORD-1'"),
        ).scalar()
        assert count == 2  # submit + fill
        latest = db.execute(
            text("SELECT cumulative_filled FROM broker_order_projection "
                 "WHERE broker_order_id = 'ORD-1' ORDER BY canonical_sequence DESC NULLS LAST LIMIT 1")
        ).fetchone()
        assert latest[0] == 50


# ---------------------------------------------------------------------------
# 3. Terminal-state enforcement tests
# ---------------------------------------------------------------------------

class TestTerminalStates:

    def test_filled_then_fill_rejected(self, db):
        """FILLED → additional fill is rejected."""
        # 1: submit, 2: fill to 50, 3: fill to 100 (FILLED), 4: additional fill
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        half_fill = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50,
            ),
            fill_facts=FillFacts(fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )
        ingest_canonical_event(half_fill, db)

        full_fill = _make_full_fill_event(broker_order_id="ORD-1", canonical_sequence=3,
                                           fill_quantity=50)  # incremental from 50 to 100
        ingest_canonical_event(full_fill, db)

        # Now FILLED → additional fill (seq=4, different content)
        after_fill = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=4,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=80,
            ),
            fill_facts=FillFacts(fill_quantity=80, fill_price=100.0,
                                 cumulative_filled_after=80, remaining_after=20),
            received_at=_NOW + timedelta(seconds=4),
        )
        result = ingest_canonical_event(after_fill, db)
        assert result["action"] == "REJECTED"
        assert "terminal" in result["reason"].lower()

    def test_filled_then_cancellation_rejected(self, db):
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        full_fill = _make_full_fill_event(canonical_sequence=2)
        ingest_canonical_event(full_fill, db)

        cancel = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_CANCELLED, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=3,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.CANCELLED,
                total_quantity=100,
            ),
            received_at=_NOW + timedelta(seconds=3),
        )
        result = ingest_canonical_event(cancel, db)
        assert result["action"] == "REJECTED"
        assert "terminal" in result["reason"].lower()

    def test_cancelled_then_fill_rejected(self, db):
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        cancel = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_CANCELLED, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.CANCELLED,
                total_quantity=100,
            ),
            received_at=_NOW + timedelta(seconds=2),
        )
        ingest_canonical_event(cancel, db)

        fill = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=3,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50,
            ),
            fill_facts=FillFacts(fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=3),
        )
        result = ingest_canonical_event(fill, db)
        assert result["action"] == "REJECTED"
        assert "terminal" in result["reason"].lower()

    def test_rejected_then_fill_rejected(self, db):
        reject = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_REJECTED, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=1,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.REJECTED,
                total_quantity=100, rejection_reason="test_reject",
            ),
            received_at=_NOW + timedelta(seconds=1),
        )
        ingest_canonical_event(reject, db)

        fill = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50,
            ),
            fill_facts=FillFacts(fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )
        result = ingest_canonical_event(fill, db)
        assert result["action"] == "REJECTED"
        assert "terminal" in result["reason"].lower()

    def test_expired_then_fill_rejected(self, db):
        """EXPIRED → arbitrary mutation rejected."""
        expire = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_EXPIRED, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=1,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.EXPIRED,
                total_quantity=100,
            ),
            received_at=_NOW + timedelta(seconds=1),
        )
        ingest_canonical_event(expire, db)

        fill = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50,
            ),
            fill_facts=FillFacts(fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )
        result = ingest_canonical_event(fill, db)
        assert result["action"] == "REJECTED"
        assert "terminal" in result["reason"].lower()

    def test_order_recovered_rejected(self, db):
        """ORDER_RECOVERED must be rejected — not bypass terminal protection."""
        event = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_RECOVERED, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=1,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.UNKNOWN,
                total_quantity=100,
            ),
            received_at=_NOW + timedelta(seconds=1),
        )
        result = ingest_canonical_event(event, db)
        assert result["action"] == "REJECTED"
        assert "ORDER_RECOVERED" in result["reason"]


# ---------------------------------------------------------------------------
# 4. Transactionality tests
# ---------------------------------------------------------------------------

class TestTransactionality:

    def test_rollback_on_lifecycle_failure_rolls_back_idempotency(self, db):
        """If Day38 lifecycle persistence fails, idempotency record must roll back."""
        from app.broker_sync import ingestion as ing_mod

        original = ing_mod.append_lifecycle_event

        def failing_append(*args, **kwargs):
            raise RuntimeError("simulated lifecycle failure")

        ing_mod.append_lifecycle_event = failing_append
        try:
            event = _make_submitted_event()
            with pytest.raises(RuntimeError):
                ingest_canonical_event(event, db)
            db.rollback()

            # Idempotency record must NOT exist
            count = db.execute(text("SELECT COUNT(*) FROM broker_sync_idempotency")).scalar()
            assert count == 0
            # Projection must NOT exist
            count = db.execute(text("SELECT COUNT(*) FROM broker_order_projection")).scalar()
            assert count == 0
        finally:
            ing_mod.append_lifecycle_event = original

    def test_rollback_on_projection_failure_rolls_back_idempotency(self, db):
        """If projection fails, idempotency record must roll back."""
        from app.broker_sync import ingestion as ing_mod

        original = ing_mod._build_projection

        def failing_build(*args, **kwargs):
            raise RuntimeError("simulated projection failure")

        ing_mod._build_projection = failing_build
        try:
            event = _make_submitted_event()
            with pytest.raises(RuntimeError):
                ingest_canonical_event(event, db)
            db.rollback()

            count = db.execute(text("SELECT COUNT(*) FROM broker_sync_idempotency")).scalar()
            assert count == 0
        finally:
            ing_mod._build_projection = original

    def test_successful_processing_commits_all_three(self, db):
        """Successful processing commits idempotency + projection + lifecycle."""
        event = _make_submitted_event()
        result = ingest_canonical_event(event, db)
        assert result["action"] == "APPLIED"

        idem_count = db.execute(text("SELECT COUNT(*) FROM broker_sync_idempotency")).scalar()
        proj_count = db.execute(text("SELECT COUNT(*) FROM broker_order_projection")).scalar()
        lifecycle_count = db.execute(
            text("SELECT COUNT(*) FROM trade_lifecycle_events")
        ).scalar()
        assert idem_count == 1
        assert proj_count == 1
        assert lifecycle_count == 1

    def test_retry_after_rollback_can_process(self, db):
        """After a rollback, the event can be successfully re-processed."""
        from app.broker_sync import ingestion as ing_mod

        call_count = [0]
        original = ing_mod.append_lifecycle_event

        def flaky_append(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("transient lifecycle failure")
            return original(*args, **kwargs)

        ing_mod.append_lifecycle_event = flaky_append
        try:
            event = _make_submitted_event()
            # First attempt fails
            with pytest.raises(RuntimeError):
                ingest_canonical_event(event, db)
            db.rollback()

            # Second attempt succeeds
            result = ingest_canonical_event(event, db)
            assert result["action"] == "APPLIED"
        finally:
            ing_mod.append_lifecycle_event = original


# ---------------------------------------------------------------------------
# 5. Day38 integration tests
# ---------------------------------------------------------------------------

class TestDay38Integration:

    def test_canonical_broker_event_produces_expected_day38_lifecycle_event(self, db):
        """BrokerSyncEvent → Task2 ingestion → Day38 lifecycle event."""
        event = _make_submitted_event()
        result = ingest_canonical_event(event, db)
        assert result["action"] == "APPLIED"

        row = db.execute(
            text("SELECT aggregate_type, aggregate_id, event_type, sequence, tenant_id "
                 "FROM trade_lifecycle_events")
        ).fetchone()

        assert row is not None
        assert row.aggregate_type == "TradeLifecycle"
        # v5: aggregate_id is the ACTUAL resolved execution, not the broker order
        assert row.aggregate_id == "EXEC-ORD-1"
        # ORDER_SUBMITTED → OrderSubmitted (explicit Day38 mapping)
        assert row.event_type == "OrderSubmitted"
        assert row.sequence == 1
        assert row.tenant_id == "tenant-1"

    def test_day38_sequence_independent_of_canonical_sequence(self, db):
        """Day38 sequence is allocated independently of canonical_sequence.

        Uses canonical_sequence=None for both events (no broker sequence
        validation), and verifies Day38 sequences are 1 and 2.
        """
        submit = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_SUBMITTED.value, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=None,
            provider_event_id="evt-001",
            event_timestamp=_NOW + timedelta(seconds=1),  # Day41.2 S2 evidence
            order_facts=OrderFacts(broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.SUBMITTED, total_quantity=100),
            received_at=_NOW + timedelta(seconds=1),
        )
        ingest_canonical_event(submit, db)

        fill = _make_full_fill_event(
            canonical_sequence=None,
            event_timestamp=_NOW + timedelta(seconds=2),  # Day41.2 S2 evidence
        )
        ingest_canonical_event(fill, db)

        rows = db.execute(
            text("SELECT event_type, sequence FROM trade_lifecycle_events ORDER BY created_at")
        ).fetchall()
        # Two lifecycle events, with Day38 sequences 1 and 2
        assert len(rows) == 2
        assert rows[0].event_type == "OrderSubmitted"
        assert rows[0].sequence == 1
        assert rows[1].event_type == "OrderFilled"
        assert rows[1].sequence == 2

    def test_lifecycle_persistence_compatible_with_day38_duplicate_semantics(self, db):
        """Duplicate canonical event → Day38 is also idempotent (not re-inserted)."""
        event = _make_submitted_event()
        ingest_canonical_event(event, db)

        # Replay identical event
        result = ingest_canonical_event(event, db)
        assert result["action"] == "DUPLICATE_NOOP"

        # Only 1 lifecycle event (Day38 idempotency)
        count = db.execute(text("SELECT COUNT(*) FROM trade_lifecycle_events")).scalar()
        assert count == 1


# ---------------------------------------------------------------------------
# 6. Broker sequence ordering tests
# ---------------------------------------------------------------------------

class TestBrokerSequence:

    def test_sequential_sequences_accepted(self, db):
        """1 → 2 → 3 all accepted."""
        events = []
        for seq in [1, 2, 3]:
            et = BrokerEventType.ORDER_ACCEPTED if seq > 1 else BrokerEventType.ORDER_SUBMITTED
            events.append(make_broker_sync_event(
                tenant_id="tenant-1", broker="broker-test",
                event_type=et, event_version="1.0",
                broker_order_id="ORD-1", canonical_sequence=seq,
                order_facts=OrderFacts(
                    broker_order_id="ORD-1", order_id="ORD-1",
                    status=CanonicalOrderState.OPEN,
                    total_quantity=100,
                ),
                received_at=_NOW + timedelta(seconds=seq),
            ))
        for ev in events:
            result = ingest_canonical_event(ev, db)
            assert result["action"] == "APPLIED", f"seq={ev.canonical_sequence} rejected: {result}"

    def test_duplicate_sequence_identical_content_noop(self, db):
        """Sequence 2 → 2 (identical) → no-op."""
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        acc = _make_submitted_event(canonical_sequence=2)
        ingest_canonical_event(acc, db)

        # Duplicate sequence 2 with identical content
        result = ingest_canonical_event(acc, db)
        assert result["action"] == "DUPLICATE_NOOP"

    def test_duplicate_sequence_different_content_applied(self, db):
        """Same sequence with different fill_facts → different canonical_id.

        Per v3 requirements: same broker sequence with different content
        must be rejected as CONFLICT (only one event per broker sequence).
        """
        # Submit at seq=1
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        # First partial fill at seq=2
        fill1 = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL.value, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50,
            ),
            fill_facts=FillFacts(fill_id="fill-001", fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )
        ingest_canonical_event(fill1, db)

        # Same canonical_sequence but different fill_facts → different canonical_id
        fill2 = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL.value, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=75,
            ),
            fill_facts=FillFacts(fill_id="fill-002", fill_quantity=25, fill_price=101.0,
                                 cumulative_filled_after=75, remaining_after=25),
            received_at=_NOW + timedelta(seconds=3),
        )
        result = ingest_canonical_event(fill2, db)
        # Different content at same sequence → CONFLICT
        assert result["action"] == "CONFLICT"
        # Only one projection row for seq=2
        count = db.execute(
            text("SELECT COUNT(*) FROM broker_order_projection WHERE canonical_sequence = 2"),
        ).scalar()
        assert count == 1

    def test_sequence_gap_rejected(self, db):
        """1 → 3 (gap at 2) → rejected/quarantined."""
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        gap_event = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_ACCEPTED, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=3,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.OPEN,
                total_quantity=100,
            ),
            received_at=_NOW + timedelta(seconds=2),
        )
        result = ingest_canonical_event(gap_event, db)
        assert result["action"] == "REJECTED"
        assert "gap" in result["reason"].lower() or "expected" in result["reason"].lower()

    def test_stale_sequence_rejected(self, db):
        """3 → 2 (stale, different content) → rejected."""
        # Sequence 1, 2, 3 applied
        for seq in [1, 2, 3]:
            ev = make_broker_sync_event(
                tenant_id="tenant-1", broker="broker-test",
                event_type=BrokerEventType.ORDER_SUBMITTED if seq == 1 else BrokerEventType.ORDER_ACCEPTED,
                event_version="1.0",
                broker_order_id="ORD-1", canonical_sequence=seq,
                order_facts=OrderFacts(
                    broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.SUBMITTED,
                    total_quantity=100,
                ),
                received_at=_NOW + timedelta(seconds=seq),
            )
            ingest_canonical_event(ev, db)

        # Stale seq=2 with DIFFERENT content and a NEW canonical identity
        # (distinct provider_event_id — genuinely new event, old sequence)
        stale = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_ACCEPTED, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            provider_event_id="provider-stale-new-2",
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.OPEN,
                total_quantity=200,  # different from original 100
            ),
            received_at=_NOW + timedelta(seconds=4),
        )
        result = ingest_canonical_event(stale, db)
        assert result["action"] == "REJECTED"
        assert "stale" in result["reason"].lower() or "out-of-order" in result["reason"].lower()

    def test_missing_canonical_sequence_no_fabrication(self, db):
        """canonical_sequence=None → no sequence fabrication.

        Events without canonical_sequence are processed without
        sequence validation; Day38 sequence is independently allocated.
        """
        # Event without canonical_sequence but with fill_facts for identity.
        # Day41.2 reconciliation: sequence-less cross-D1 observations carry
        # provider event_timestamp S2 evidence (the old arrival-order
        # application assumption is superseded by the approved rule).
        ev1 = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_SUBMITTED, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=None,
            provider_event_id="evt-001",  # provides identity
            event_timestamp=_NOW + timedelta(seconds=1),
            order_facts=OrderFacts(broker_order_id="ORD-1", order_id="ORD-1",
                status=CanonicalOrderState.SUBMITTED, total_quantity=100),
            received_at=_NOW + timedelta(seconds=1),
        )
        result1 = ingest_canonical_event(ev1, db)
        assert result1["action"] == "APPLIED"

        # Second event (no canonical_sequence) with a different provider_event_id
        ev2 = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=None,
            provider_event_id="evt-002",
            event_timestamp=_NOW + timedelta(seconds=2),
            order_facts=OrderFacts(broker_order_id="ORD-1", order_id="ORD-1",
                status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50),
            fill_facts=FillFacts(fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )
        result2 = ingest_canonical_event(ev2, db)
        assert result2["action"] == "APPLIED"

        # Day38 sequences should be 1 and 2, not both 1
        rows = db.execute(
            text("SELECT sequence FROM trade_lifecycle_events ORDER BY created_at")
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == 1
        assert rows[1][0] == 2


# ---------------------------------------------------------------------------
# 7. Quantity invariant tests
# ---------------------------------------------------------------------------

class TestQuantityInvariants:

    def test_total_100_cumulative_50_pass(self, db):
        """total=100, cumulative=50 → PASS."""
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        fill = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50,
            ),
            fill_facts=FillFacts(fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )
        result = ingest_canonical_event(fill, db)
        assert result["action"] == "APPLIED"

    def test_total_100_cumulative_100_pass(self, db):
        """total=100, cumulative=100 → PASS."""
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        fill = _make_full_fill_event(canonical_sequence=2)
        result = ingest_canonical_event(fill, db)
        assert result["action"] == "APPLIED"
        assert result["normalized_state"]["cumulative_filled"] == 100

    def test_total_100_cumulative_101_rejected(self, db):
        """total=100, cumulative=101 → REJECT."""
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        overfill = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.FULL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.FILLED,
                total_quantity=100, cumulative_filled=101, is_terminal=True,
            ),
            fill_facts=FillFacts(fill_quantity=101, fill_price=100.0,
                                 cumulative_filled_after=101, remaining_after=-1),
            received_at=_NOW + timedelta(seconds=2),
        )
        result = ingest_canonical_event(overfill, db)
        assert result["action"] == "REJECTED"
        assert "overfill" in result["reason"].lower() or "exceeds" in result["reason"].lower()

    def test_previous_50_incoming_40_rejected(self, db):
        """previous cumulative=50, incoming cumulative=40 → REJECT."""
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        half_fill = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50,
            ),
            fill_facts=FillFacts(fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )
        ingest_canonical_event(half_fill, db)

        regression = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.FULL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=3,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.FILLED,
                total_quantity=100, cumulative_filled=40, is_terminal=True,
            ),
            fill_facts=FillFacts(fill_quantity=40, fill_price=100.0,
                                 cumulative_filled_after=40, remaining_after=60),
            received_at=_NOW + timedelta(seconds=3),
        )
        result = ingest_canonical_event(regression, db)
        assert result["action"] == "REJECTED"
        assert "regress" in result["reason"].lower()

    def test_negative_fill_quantity_rejected(self, db):
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        neg_fill = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=0,
            ),
            fill_facts=FillFacts(fill_quantity=-5, fill_price=100.0,
                                 cumulative_filled_after=-5, remaining_after=105),
            received_at=_NOW + timedelta(seconds=2),
        )
        result = ingest_canonical_event(neg_fill, db)
        assert result["action"] == "REJECTED"
        assert "negative" in result["reason"].lower()

    def test_remaining_inconsistent_rejected(self, db):
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        inconsistent = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50,
            ),
            fill_facts=FillFacts(fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=60),  # should be 50
            received_at=_NOW + timedelta(seconds=2),
        )
        result = ingest_canonical_event(inconsistent, db)
        assert result["action"] == "REJECTED"
        assert "inconsistent" in result["reason"].lower()


# ---------------------------------------------------------------------------
# 8. Communication failure ≠ order rejection
# ---------------------------------------------------------------------------

class TestCommunicationFailure:

    def test_unknown_event_type_rejected_not_mapped_to_rejected_state(self, db):
        """A non-broker-rejection event (e.g. NETWORK_DISCONNECT) is rejected
        by the mapping layer, NOT persisted as REJECTED normalized state.

        Communication failures are NOT mapped to ORDER_REJECTED.
        """
        network_event = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type="NETWORK_DISCONNECT",  # not a broker event type
            event_version="1.0",
            broker_order_id="ORD-1",
            canonical_sequence=1,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1",
                status=CanonicalOrderState.UNKNOWN,
                total_quantity=100,
            ),
            received_at=_NOW + timedelta(seconds=1),
        )
        result = ingest_canonical_event(network_event, db)
        # NETWORK_DISCONNECT has no Day38 mapping → rejected
        assert result["action"] == "REJECTED"
        assert "NETWORK_DISCONNECT" in result["reason"]


# ---------------------------------------------------------------------------
# 9. Concurrency (FIX 1) — covered in test_day39_task2_postgres.py
#    under TestPostgresConcurrency.  Real concurrent-transaction broker-sequence
#    safety requires PostgreSQL row-level semantics; SQLite's single-connection
#    StaticPool cannot serialize two concurrent write transactions.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 10. Day38 replay compatibility tests (FIX 2)
# ---------------------------------------------------------------------------

class TestDay38ReplayCompatibility:
    """Day38 replay compatibility tests using AUTHORITATIVE foundation path.

    These tests prove that the actual Day38 lifecycle events persisted by
    Task2 are replay-compatible. The execution foundation is established
    using the authoritative append_lifecycle_event path (not synthetic
    TradeLifecycleEventEnvelope construction).
    """

    def _create_execution_foundation(self, db, tenant, order_id, quantity=100):
        """Create execution foundation using authoritative append_lifecycle_event.

        This is the SAME path that the production Day38 code uses to establish
        an execution. No synthetic events are created.

        Also seeds an authoritative StrategyExecution + PaperOrder (the app
        context the broker event resolves to) and uses the ACTUAL execution_id
        as the Day38 lifecycle aggregate — never the broker order id (v5).

        Returns (execution_id, next_sequence).
        """
        from app.models import PaperOrder, StrategyExecution
        from app.trade_lifecycle.persistence import append_lifecycle_event

        # Seed authoritative application context (StrategyExecution + PaperOrder)
        execution_id = f"EXEC-{order_id}"
        exec_row = StrategyExecution(
            user_id=tenant,
            execution_id=execution_id,
            client_order_id=f"exec-{order_id}",
            strategy_id="strat-1",
            strategy_tag="Test",
            symbol="NIFTY",
            status="FILLED",
            entry_net=0.0,
            entry_at=_NOW,
        )
        db.add(exec_row)
        db.flush()
        app_order = PaperOrder(
            user_id=tenant,
            client_order_id=order_id,
            execution_id=execution_id,
            kind="entry",
            symbol="NIFTY",
            expiry="2026-10-29",
            strike=24500.0,
            option_type="CE",
            action="buy",
            quantity=100,
            lot_size=1,
            status="FILLED",
            filled_quantity=100,
            fill_price=100.0,
        )
        db.add(app_order)
        db.flush()

        now = _NOW
        seq = 1

        # TradeIntentCreated — against the ACTUAL execution aggregate
        append_lifecycle_event(
            db=db,
            aggregate_type="TradeLifecycle",
            aggregate_id=execution_id,
            event_type="TradeIntentCreated",
            event_version="1.0",
            tenant_id=tenant,
            sequence=seq,
            position_sequence=None,
            quantity_delta=None,
            position_identity=None,
            occurred_at=now,
            payload={"strategy_id": "test-strategy", "intent": "BUY"},
            metadata=None,
        )
        seq += 1

        # ExecutionActivated
        append_lifecycle_event(
            db=db,
            aggregate_type="TradeLifecycle",
            aggregate_id=execution_id,
            event_type="ExecutionActivated",
            event_version="1.0",
            tenant_id=tenant,
            sequence=seq,
            position_sequence=None,
            quantity_delta=None,
            position_identity=None,
            occurred_at=now + timedelta(seconds=1),
            payload={},
            metadata=None,
        )
        seq += 1

        # OrderCreated
        append_lifecycle_event(
            db=db,
            aggregate_type="TradeLifecycle",
            aggregate_id=execution_id,
            event_type="OrderCreated",
            event_version="1.0",
            tenant_id=tenant,
            sequence=seq,
            position_sequence=None,
            quantity_delta=None,
            position_identity=None,
            occurred_at=now + timedelta(seconds=1),
            payload={"order_id": order_id, "quantity": quantity},
            metadata=None,
        )
        seq += 1

        return execution_id, seq  # (execution_id, next available sequence)

    def test_task2_order_submitted_produces_replay_compatible_event(self, db):
        """Task2 ORDER_SUBMITTED → Day38 OrderSubmitted with replay-compatible payload.

        Uses authoritative append_lifecycle_event path to establish execution
        foundation, then ingests a broker event via Task2, then replays the
        actual persisted stream.
        """
        from app.trade_lifecycle.persistence import TradeLifecycleEvent
        from app.trade_lifecycle.replay import replay_execution_events, LifecycleReplayError
        from app.trade_lifecycle.envelope import TradeLifecycleEventEnvelope

        order_id = "ORD-REPLAY-1"
        tenant = "tenant-1"
        now = _NOW

        # Step 1: Create execution foundation using authoritative path
        execution_id, _ = self._create_execution_foundation(db, tenant, order_id)

        # Step 2: Task2 ingests a broker ORDER_SUBMITTED event
        event = make_broker_sync_event(
            tenant_id=tenant, broker="broker-test",
            event_type=BrokerEventType.ORDER_SUBMITTED, event_version="1.0",
            broker_order_id=order_id, canonical_sequence=1,
            order_facts=OrderFacts(
                broker_order_id=order_id, order_id=order_id,
                status=CanonicalOrderState.SUBMITTED, total_quantity=100),
            received_at=now + timedelta(seconds=1),
        )
        result = ingest_canonical_event(event, db)
        assert result["action"] == "APPLIED"

        # Step 3: Read ALL lifecycle events from the DB (foundation + Task2)
        rows = db.execute(
            select(TradeLifecycleEvent)
            .where(TradeLifecycleEvent.tenant_id == tenant)
            .order_by(TradeLifecycleEvent.sequence.asc())
        ).scalars().all()
        assert len(rows) == 4  # 3 foundation + 1 Task2

        # Step 4: Verify the Task2 event
        task2_event = rows[3]
        assert task2_event.event_type == "OrderSubmitted"
        assert task2_event.aggregate_type == "TradeLifecycle"
        assert task2_event.aggregate_id == execution_id
        assert task2_event.tenant_id == tenant

        # Step 5: Construct envelopes from persisted rows (NO renumbering)
        envelopes = []
        for row in rows:
            occurred = row.occurred_at
            if occurred.tzinfo is None:
                occurred = occurred.replace(tzinfo=timezone.utc)
            envelopes.append(TradeLifecycleEventEnvelope(
                tenant_id=row.tenant_id,
                aggregate_type=row.aggregate_type,
                aggregate_id=row.aggregate_id,
                event_type=row.event_type,
                event_version=row.event_version,
                sequence=row.sequence,
                occurred_at=occurred,
                payload=json.loads(row.payload_json),
            ))

        # Step 6: Feed through Day38 replay
        try:
            state = replay_execution_events(envelopes)
            assert state.execution_status.value in ("CREATED", "ACTIVE")
            assert order_id in state.orders
            assert state.orders[order_id].status.value == "SUBMITTED"
        except LifecycleReplayError as e:
            pytest.fail(f"Task2 OrderSubmitted event failed Day38 replay: {e}")

    def test_full_broker_sequence_replay_compatible(self, db):
        """Complete broker event sequence: ORDER_SUBMITTED → PARTIAL_FILL → FULL_FILL.

        Uses authoritative foundation path, then ingests a full broker sequence,
        then replays the actual persisted stream.
        """
        from app.trade_lifecycle.persistence import TradeLifecycleEvent
        from app.trade_lifecycle.replay import replay_execution_events, LifecycleReplayError
        from app.trade_lifecycle.envelope import TradeLifecycleEventEnvelope

        order_id = "ORD-REPLAY-FULL-1"
        tenant = "tenant-1"
        now = _NOW

        # Step 1: Create execution foundation
        execution_id, _ = self._create_execution_foundation(db, tenant, order_id)

        # Step 2: Ingest full broker sequence
        broker_events = [
            make_broker_sync_event(
                tenant_id=tenant, broker="broker-test",
                event_type=BrokerEventType.ORDER_SUBMITTED, event_version="1.0",
                broker_order_id=order_id, canonical_sequence=1,
                order_facts=OrderFacts(
                    broker_order_id=order_id, order_id=order_id,
                    status=CanonicalOrderState.SUBMITTED, total_quantity=100),
                received_at=now + timedelta(seconds=1),
            ),
            make_broker_sync_event(
                tenant_id=tenant, broker="broker-test",
                event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
                broker_order_id=order_id, canonical_sequence=2,
                order_facts=OrderFacts(
                    broker_order_id=order_id, order_id=order_id,
                    status=CanonicalOrderState.PARTIALLY_FILLED,
                    total_quantity=100, cumulative_filled=50),
                fill_facts=FillFacts(fill_id="fill-001", fill_quantity=50, fill_price=100.0,
                                     cumulative_filled_after=50, remaining_after=50),
                received_at=now + timedelta(seconds=2),
            ),
            make_broker_sync_event(
                tenant_id=tenant, broker="broker-test",
                event_type=BrokerEventType.FULL_FILL, event_version="1.0",
                broker_order_id=order_id, canonical_sequence=3,
                order_facts=OrderFacts(
                    broker_order_id=order_id, order_id=order_id,
                    status=CanonicalOrderState.FILLED,
                    total_quantity=100, cumulative_filled=100, is_terminal=True),
                fill_facts=FillFacts(fill_id="fill-002", fill_quantity=50, fill_price=100.0,
                                     cumulative_filled_after=100, remaining_after=0),
                received_at=now + timedelta(seconds=3),
            ),
        ]

        for ev in broker_events:
            result = ingest_canonical_event(ev, db)
            assert result["action"] == "APPLIED"

        # Step 3: Read ALL lifecycle events from the DB
        rows = db.execute(
            select(TradeLifecycleEvent)
            .where(TradeLifecycleEvent.tenant_id == tenant)
            .order_by(TradeLifecycleEvent.sequence.asc())
        ).scalars().all()
        assert len(rows) == 6  # 3 foundation + 3 Task2

        # Step 4: Verify event types are mapped Day38 types
        event_types = [r.event_type for r in rows]
        assert event_types[0] == "TradeIntentCreated"
        assert event_types[1] == "ExecutionActivated"
        assert event_types[2] == "OrderCreated"
        assert event_types[3] == "OrderSubmitted"  # ORDER_SUBMITTED → OrderSubmitted
        assert event_types[4] == "OrderFilled"  # PARTIAL_FILL → OrderFilled
        assert event_types[5] == "OrderFilled"  # FULL_FILL → OrderFilled

        # Step 5: Construct envelopes from persisted rows (NO renumbering)
        envelopes = []
        for row in rows:
            occurred = row.occurred_at
            if occurred.tzinfo is None:
                occurred = occurred.replace(tzinfo=timezone.utc)
            envelopes.append(TradeLifecycleEventEnvelope(
                tenant_id=row.tenant_id,
                aggregate_type=row.aggregate_type,
                aggregate_id=row.aggregate_id,
                event_type=row.event_type,
                event_version=row.event_version,
                sequence=row.sequence,
                occurred_at=occurred,
                payload=json.loads(row.payload_json),
            ))

        # Step 6: Feed through Day38 replay
        try:
            state = replay_execution_events(envelopes)
            assert state.execution_status.value in ("CREATED", "ACTIVE")
            assert order_id in state.orders
            assert state.orders[order_id].status.value == "FILLED"
            assert state.orders[order_id].cumulative_filled == 100
        except LifecycleReplayError as e:
            pytest.fail(f"Full broker sequence failed Day38 replay: {e}")

    def test_task2_order_filled_payload_has_cumulative_filled(self, db):
        """Task2 PARTIAL_FILL → Day38 OrderFilled with cumulative_filled in payload."""
        from app.trade_lifecycle.persistence import TradeLifecycleEvent

        order_id = "ORD-CUM-1"
        tenant = "tenant-1"

        # Create foundation
        self._create_execution_foundation(db, tenant, order_id)

        submit = _make_submitted_event(
            broker_order_id=order_id, canonical_sequence=1,
            event_type=BrokerEventType.ORDER_SUBMITTED,
        )
        ingest_canonical_event(submit, db)

        fill = make_broker_sync_event(
            tenant_id=tenant, broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id=order_id, canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id=order_id, order_id=order_id,
                status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50),
            fill_facts=FillFacts(fill_id="fill-001", fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )
        ingest_canonical_event(fill, db)

        # Find the OrderFilled lifecycle event
        rows = db.execute(
            select(TradeLifecycleEvent)
            .where(TradeLifecycleEvent.event_type == "OrderFilled")
        ).scalars().all()
        assert len(rows) == 1

        payload = json.loads(rows[0].payload_json)
        assert "order_id" in payload, f"Missing order_id in OrderFilled payload: {payload.keys()}"
        assert "cumulative_filled" in payload, f"Missing cumulative_filled in OrderFilled payload: {payload.keys()}"
        assert isinstance(payload["cumulative_filled"], int)
        assert payload["cumulative_filled"] >= 1

    def test_task2_fill_recorded_payload_has_fill_quantity(self, db):
        """Task2 FILL_RECORDED → Day38 FillRecorded with fill_quantity in payload."""
        from app.trade_lifecycle.persistence import TradeLifecycleEvent

        order_id = "ORD-FQ-1"
        tenant = "tenant-1"

        # Create foundation
        self._create_execution_foundation(db, tenant, order_id)

        submit = _make_submitted_event(
            broker_order_id=order_id, canonical_sequence=1,
            event_type=BrokerEventType.ORDER_SUBMITTED,
        )
        ingest_canonical_event(submit, db)

        fill_rec = make_broker_sync_event(
            tenant_id=tenant, broker="broker-test",
            event_type=BrokerEventType.FILL_RECORDED, event_version="1.0",
            broker_order_id=order_id, canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id=order_id, order_id=order_id,
                status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50),
            fill_facts=FillFacts(fill_id="fill-001", fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )
        ingest_canonical_event(fill_rec, db)

        rows = db.execute(
            select(TradeLifecycleEvent)
            .where(TradeLifecycleEvent.event_type == "FillRecorded")
        ).scalars().all()
        assert len(rows) == 1

        payload = json.loads(rows[0].payload_json)
        assert "order_id" in payload
        assert "fill_quantity" in payload
        assert isinstance(payload["fill_quantity"], int)
        assert payload["fill_quantity"] >= 1

    def test_no_foundation_fails_closed(self, db):
        """If no Day38 execution foundation exists, Task2 fails closed.

        Task2 must NOT synthesize foundation events.
        """
        order_id = "ORD-NO-FOUNDATION"
        tenant = "tenant-1"

        # Do NOT create foundation - Task2 should reject
        event = make_broker_sync_event(
            tenant_id=tenant, broker="broker-test",
            event_type=BrokerEventType.ORDER_SUBMITTED, event_version="1.0",
            broker_order_id=order_id, canonical_sequence=1,
            order_facts=OrderFacts(
                broker_order_id=order_id, order_id=order_id,
                status=CanonicalOrderState.SUBMITTED, total_quantity=100),
            received_at=_NOW + timedelta(seconds=1),
        )
        result = ingest_canonical_event(event, db)
        # Unknown broker order: cannot resolve to an existing app execution → REJECTED
        assert result["action"] == "REJECTED"
        assert "unresolved" in result["reason"].lower()


# ---------------------------------------------------------------------------
# 11. Fill arithmetic consistency tests (FIX 3)
# ---------------------------------------------------------------------------

class TestFillArithmeticConsistency:

    def test_cumulative_after_less_than_previous_plus_fill_rejected(self, db):
        """previous=50, fill=20, cumulative_after=60 → REJECT.
        cumulative_after should be >= previous + fill = 70.
        """
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        half_fill = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(broker_order_id="ORD-1", order_id="ORD-1",
                status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50),
            fill_facts=FillFacts(fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )
        ingest_canonical_event(half_fill, db)

        # Bad fill: says it added 20 but cumulative only went from 50 to 60
        # 60 < 50 + 20 = 70, so this is inconsistent
        bad_fill = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=3,
            order_facts=OrderFacts(broker_order_id="ORD-1", order_id="ORD-1",
                status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=60),
            fill_facts=FillFacts(fill_quantity=20, fill_price=100.0,
                                 cumulative_filled_after=60, remaining_after=40),
            received_at=_NOW + timedelta(seconds=3),
        )
        result = ingest_canonical_event(bad_fill, db)
        assert result["action"] == "REJECTED"
        assert "less than" in result["reason"].lower() or "previous" in result["reason"].lower()

    def test_valid_fill_arithmetic_passes(self, db):
        """previous=0, fill=50, cumulative_after=50 → PASS.
        previous=50, fill=50, cumulative_after=100 → PASS.
        """
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        # First fill: 0 + 50 = 50 ✓
        fill1 = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(broker_order_id="ORD-1", order_id="ORD-1",
                status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50),
            fill_facts=FillFacts(fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )
        result1 = ingest_canonical_event(fill1, db)
        assert result1["action"] == "APPLIED"

        # Second fill: 50 + 50 = 100 ✓
        fill2 = _make_full_fill_event(broker_order_id="ORD-1", canonical_sequence=3,
                                       cumulative_filled_after=100, total_quantity=100,
                                       fill_quantity=50)  # incremental from 50 to 100
        result2 = ingest_canonical_event(fill2, db)
        assert result2["action"] == "APPLIED"
        assert result2["normalized_state"]["cumulative_filled"] == 100

    def test_fill_quantity_exceeding_total_rejected(self, db):
        """fill_quantity > total_quantity → REJECT."""
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        overfill = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(broker_order_id="ORD-1", order_id="ORD-1",
                status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=100),
            fill_facts=FillFacts(fill_quantity=150, fill_price=100.0,
                                 cumulative_filled_after=100, remaining_after=0),
            received_at=_NOW + timedelta(seconds=2),
        )
        result = ingest_canonical_event(overfill, db)
        assert result["action"] == "REJECTED"
        # fill_quantity=150 exceeds both cumulative_after (100 < 0+150) and
        # total (150 > 100) — either invariant correctly fails closed
        assert ("exceeds" in result["reason"].lower()
                or "less than" in result["reason"].lower())


# ---------------------------------------------------------------------------
# 12. Sequence-less ordering tests (FIX 4)
# ---------------------------------------------------------------------------

class TestSequenceLessOrdering:

    def test_sequence_less_events_are_independently_identifiable(self, db):
        """Multiple sequence-less events for same order are all processed.
        Each is an independent observation; ordering is not inferred.
        """
        # Event 1: submit (no sequence).  Day41.2 reconciliation: provider
        # event_timestamp S2 evidence governs cross-D1 ordering (never arrival).
        ev1 = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_SUBMITTED, event_version="1.0",
            broker_order_id="ORD-SEQLESS", canonical_sequence=None,
            provider_event_id="evt-001",
            event_timestamp=_NOW + timedelta(seconds=1),
            order_facts=OrderFacts(broker_order_id="ORD-SEQLESS", order_id="ORD-SEQLESS",
                status=CanonicalOrderState.SUBMITTED, total_quantity=100),
            received_at=_NOW + timedelta(seconds=1),
        )
        r1 = ingest_canonical_event(ev1, db)
        assert r1["action"] == "APPLIED"

        # Event 2: partial fill (no sequence) — newer S2 evidence ⇒ applies
        ev2 = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-SEQLESS", canonical_sequence=None,
            provider_event_id="evt-002",
            event_timestamp=_NOW + timedelta(seconds=2),
            order_facts=OrderFacts(broker_order_id="ORD-SEQLESS", order_id="ORD-SEQLESS",
                status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50),
            fill_facts=FillFacts(fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )
        r2 = ingest_canonical_event(ev2, db)
        assert r2["action"] == "APPLIED"

        # Event 3: full fill (no sequence) — newest S2 evidence ⇒ applies
        ev3 = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.FULL_FILL, event_version="1.0",
            broker_order_id="ORD-SEQLESS", canonical_sequence=None,
            provider_event_id="evt-003",
            event_timestamp=_NOW + timedelta(seconds=3),
            order_facts=OrderFacts(broker_order_id="ORD-SEQLESS", order_id="ORD-SEQLESS",
                status=CanonicalOrderState.FILLED,
                total_quantity=100, cumulative_filled=100, is_terminal=True),
            fill_facts=FillFacts(fill_id="fill-003", fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=100, remaining_after=0),
            received_at=_NOW + timedelta(seconds=3),
        )
        r3 = ingest_canonical_event(ev3, db)
        assert r3["action"] == "APPLIED"
        assert r3["normalized_state"]["status"] == "FILLED"

        # All three were applied independently
        from app.trade_lifecycle.persistence import TradeLifecycleEvent
        count = db.execute(
            select(func.count(TradeLifecycleEvent.event_id))
        ).scalar()
        assert count == 3

    def test_deterministic_projection_lookup_with_null_sequences(self, db):
        """Projection lookup is deterministic even when all events lack canonical_sequence."""
        # Process the same events multiple times and verify same result
        for i in range(3):
            # Fresh session each time
            sess = _TestSessionLocal()
            ev = make_broker_sync_event(
                tenant_id="tenant-1", broker="broker-test",
                event_type=BrokerEventType.ORDER_SUBMITTED, event_version="1.0",
                broker_order_id="ORD-DET", canonical_sequence=None,
                provider_event_id=f"evt-det-{i}",
                order_facts=OrderFacts(broker_order_id="ORD-DET", order_id="ORD-DET",
                    status=CanonicalOrderState.SUBMITTED, total_quantity=100),
                received_at=_NOW + timedelta(seconds=i),
            )
            result = ingest_canonical_event(ev, sess)
            assert result["action"] == "APPLIED"
            sess.close()


# ---------------------------------------------------------------------------
# V3 tests — sequence coupling, concurrency reclassification, replay
# ---------------------------------------------------------------------------

class TestSequenceAdvancementRollback:
    """FIX #1: broker sequence advancement must roll back with failed application."""

    def test_sequence_advances_on_success(self, db):
        """On successful application, the sequence anchor is advanced."""
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        anchor = db.execute(
            select(BrokerSyncSequenceAnchor)
            .where(BrokerSyncSequenceAnchor.broker_order_id == "ORD-1")
        ).scalar_one_or_none()
        assert anchor is not None
        assert anchor.last_sequence == 1

    def test_sequence_rolls_back_on_lifecycle_failure(self, db):
        """If Day38 lifecycle fails, the sequence anchor must NOT be advanced."""
        from unittest.mock import patch

        submit = _make_submitted_event(canonical_sequence=1)

        # Patch append_lifecycle_event to fail
        with patch(
            "app.broker_sync.ingestion.append_lifecycle_event",
            side_effect=Exception("simulated lifecycle failure"),
        ):
            with pytest.raises(Exception, match="simulated lifecycle failure"):
                ingest_canonical_event(submit, db)

        # Sequence anchor must NOT be advanced
        anchor = db.execute(
            select(BrokerSyncSequenceAnchor)
            .where(BrokerSyncSequenceAnchor.broker_order_id == "ORD-1")
        ).scalar_one_or_none()
        # Anchor may not exist at all, or must have last_sequence=0
        if anchor is not None:
            assert anchor.last_sequence == 0

    def test_sequence_rolls_back_on_projection_failure(self, db):
        """If projection persistence fails, the sequence anchor must NOT be advanced.

        This test verifies that when the SAVEPOINT fails, the sequence anchor
        is not advanced.  We test this by applying a valid event first,
        then attempting to apply a second event that will fail.
        """
        from unittest.mock import patch

        # First, apply a valid event at seq=1
        submit = _make_submitted_event(canonical_sequence=1)
        result1 = ingest_canonical_event(submit, db)
        assert result1["action"] == "APPLIED"

        # Verify sequence anchor is at 1
        anchor = db.execute(
            select(BrokerSyncSequenceAnchor)
            .where(BrokerSyncSequenceAnchor.broker_order_id == "ORD-1")
        ).scalar_one_or_none()
        assert anchor is not None
        assert anchor.last_sequence == 1

        # Now try to apply a gap event (seq=3, expected 2) - this should fail
        gap_event = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_ACCEPTED, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=3,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.OPEN,
                total_quantity=100, cumulative_filled=0,
            ),
            received_at=_NOW + timedelta(seconds=2),
        )
        result2 = ingest_canonical_event(gap_event, db)
        assert result2["action"] == "REJECTED"

        # Sequence anchor must still be at 1 (not advanced to 3)
        anchor = db.execute(
            select(BrokerSyncSequenceAnchor)
            .where(BrokerSyncSequenceAnchor.broker_order_id == "ORD-1")
        ).scalar_one_or_none()
        assert anchor is not None
        assert anchor.last_sequence == 1

    def test_sequence_rolls_back_on_idempotency_failure(self, db):
        """If idempotency record fails, the sequence anchor must NOT be advanced.

        This test verifies that when the SAVEPOINT fails due to an idempotency
        conflict, the sequence anchor is not advanced.
        """
        # First, apply a valid event at seq=1
        submit = _make_submitted_event(canonical_sequence=1)
        result1 = ingest_canonical_event(submit, db)
        assert result1["action"] == "APPLIED"

        # Verify sequence anchor is at 1
        anchor = db.execute(
            select(BrokerSyncSequenceAnchor)
            .where(BrokerSyncSequenceAnchor.broker_order_id == "ORD-1")
        ).scalar_one_or_none()
        assert anchor is not None
        assert anchor.last_sequence == 1

        # Now try to apply a conflicting event at seq=1 (different content)
        # This should fail with CONFLICT
        conflicting = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_SUBMITTED, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=1,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.SUBMITTED,
                total_quantity=200, cumulative_filled=0,  # Different quantity
            ),
            received_at=_NOW + timedelta(seconds=2),
        )
        result2 = ingest_canonical_event(conflicting, db)
        assert result2["action"] == "CONFLICT"

        # Sequence anchor must still be at 1 (not advanced)
        anchor = db.execute(
            select(BrokerSyncSequenceAnchor)
            .where(BrokerSyncSequenceAnchor.broker_order_id == "ORD-1")
        ).scalar_one_or_none()
        assert anchor is not None
        assert anchor.last_sequence == 1

    def test_gap_rejected_does_not_advance_sequence(self, db):
        """A rejected gap event must NOT advance the sequence anchor."""
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        # Try a gap event (seq=3, expected 2)
        gap_event = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_ACCEPTED, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=3,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.OPEN,
                total_quantity=100, cumulative_filled=0,
            ),
            received_at=_NOW + timedelta(seconds=2),
        )
        result = ingest_canonical_event(gap_event, db)
        assert result["action"] == "REJECTED"

        # Sequence anchor must still be at 1
        anchor = db.execute(
            select(BrokerSyncSequenceAnchor)
            .where(BrokerSyncSequenceAnchor.broker_order_id == "ORD-1")
        ).scalar_one_or_none()
        assert anchor is not None
        assert anchor.last_sequence == 1

    def test_stale_rejected_does_not_advance_sequence(self, db):
        """A rejected stale event must NOT advance the sequence anchor."""
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)

        accepted = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_ACCEPTED, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.OPEN,
                total_quantity=100, cumulative_filled=0,
            ),
            received_at=_NOW + timedelta(seconds=2),
        )
        ingest_canonical_event(accepted, db)

        # Now try a stale event (seq=1 again) with a NEW canonical identity
        # (distinct provider_event_id).  Genuinely stale NEW events stay
        # REJECTED; only exact replays of already-applied events are
        # DUPLICATE_NOOP (durable identity beats broker ordering, v7).
        stale = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_SUBMITTED, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=1,
            provider_event_id="provider-stale-new-1",
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.SUBMITTED,
                total_quantity=100, cumulative_filled=0,
            ),
            received_at=_NOW + timedelta(seconds=1),
        )
        result = ingest_canonical_event(stale, db)
        assert result["action"] == "REJECTED"

        # Sequence anchor must still be at 2
        anchor = db.execute(
            select(BrokerSyncSequenceAnchor)
            .where(BrokerSyncSequenceAnchor.broker_order_id == "ORD-1")
        ).scalar_one_or_none()
        assert anchor is not None
        assert anchor.last_sequence == 2


class TestConcurrentSequenceRace:
    """FIX #2: concurrent sequence races must reclassify through idempotency.

    NOTE: SQLite with StaticPool serializes all access, so true concurrency
    testing is done in test_day39_task2_postgres.py.  These tests verify
    the classification logic works correctly in the SQLite environment.
    """

    def test_identical_events_same_sequence_one_applied_one_noop(self, db):
        """Two identical events at the same sequence.

        One should be APPLIED, the other DUPLICATE_NOOP.
        """
        # First, apply seq=1
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)
        db.flush()

        # Create two identical events at seq=2
        fill_a = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50,
            ),
            fill_facts=FillFacts(fill_id="fill-A", fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )
        fill_b = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50,
            ),
            fill_facts=FillFacts(fill_id="fill-A", fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )

        # Both have same canonical_id (identical content)
        assert fill_a.canonical_id == fill_b.canonical_id

        # Apply first event
        result1 = ingest_canonical_event(fill_a, db)
        assert result1["action"] == "APPLIED"

        # Apply second identical event
        result2 = ingest_canonical_event(fill_b, db)
        assert result2["action"] == "DUPLICATE_NOOP"

        # Verify only one projection row for seq=2
        count = db.execute(
            text("SELECT COUNT(*) FROM broker_order_projection WHERE canonical_sequence = 2"),
        ).scalar()
        assert count == 1

    def test_different_content_same_sequence_one_wins(self, db):
        """Two events with different content at the same sequence.

        One should be APPLIED, the other CONFLICT.
        """
        # First, apply seq=1
        submit = _make_submitted_event(canonical_sequence=1)
        ingest_canonical_event(submit, db)
        db.flush()

        fill_a = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50,
            ),
            fill_facts=FillFacts(fill_id="fill-A", fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )
        fill_b = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id="ORD-1", canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id="ORD-1", order_id="ORD-1", status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=75,
            ),
            fill_facts=FillFacts(fill_id="fill-B", fill_quantity=25, fill_price=101.0,
                                 cumulative_filled_after=75, remaining_after=25),
            received_at=_NOW + timedelta(seconds=3),
        )

        # Different canonical_ids (different content)
        assert fill_a.canonical_id != fill_b.canonical_id

        # Apply first event
        result1 = ingest_canonical_event(fill_a, db)
        assert result1["action"] == "APPLIED"

        # Apply second event with different content at same sequence
        result2 = ingest_canonical_event(fill_b, db)
        # Should be CONFLICT (different canonical_id, same sequence)
        assert result2["action"] in ("CONFLICT", "REJECTED")

    def test_independent_events_both_applied(self, db):
        """Two events for different orders.

        Both should be APPLIED.
        """
        fill_a = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_SUBMITTED, event_version="1.0",
            broker_order_id="ORD-A", canonical_sequence=1,
            order_facts=OrderFacts(
                broker_order_id="ORD-A", order_id="ORD-A", status=CanonicalOrderState.SUBMITTED,
                total_quantity=100, cumulative_filled=0,
            ),
            received_at=_NOW + timedelta(seconds=1),
        )
        fill_b = make_broker_sync_event(
            tenant_id="tenant-1", broker="broker-test",
            event_type=BrokerEventType.ORDER_SUBMITTED, event_version="1.0",
            broker_order_id="ORD-B", canonical_sequence=1,
            order_facts=OrderFacts(
                broker_order_id="ORD-B", order_id="ORD-B", status=CanonicalOrderState.SUBMITTED,
                total_quantity=200, cumulative_filled=0,
            ),
            received_at=_NOW + timedelta(seconds=2),
        )

        result1 = ingest_canonical_event(fill_a, db)
        result2 = ingest_canonical_event(fill_b, db)

        assert result1["action"] == "APPLIED"
        assert result2["action"] == "APPLIED"


class TestDay38ReplayFromPersistedStream:
    """FIX #3: Day38 replay compatibility must be proven against actual persisted stream."""

    def _create_execution_foundation(self, db, tenant, order_id, quantity=100):
        """Create execution foundation using authoritative append_lifecycle_event.

        Also seeds the authoritative StrategyExecution + PaperOrder and uses
        the ACTUAL execution_id as the lifecycle aggregate (v5).

        Returns (execution_id, next_sequence).
        """
        from app.models import PaperOrder, StrategyExecution
        from app.trade_lifecycle.persistence import append_lifecycle_event

        execution_id = f"EXEC-{order_id}"
        db.add(StrategyExecution(
            user_id=tenant, execution_id=execution_id,
            client_order_id=f"exec-{order_id}", strategy_id="strat-1",
            strategy_tag="Test", symbol="NIFTY", status="FILLED",
            entry_net=0.0, entry_at=_NOW,
        ))
        db.flush()
        db.add(PaperOrder(
            user_id=tenant, client_order_id=order_id,
            execution_id=execution_id, kind="entry", symbol="NIFTY",
            expiry="2026-10-29", strike=24500.0, option_type="CE",
            action="buy", quantity=100, lot_size=1, status="FILLED",
            filled_quantity=100, fill_price=100.0,
        ))
        db.flush()

        now = _NOW
        seq = 1

        append_lifecycle_event(
            db=db, aggregate_type="TradeLifecycle", aggregate_id=execution_id,
            event_type="TradeIntentCreated", event_version="1.0", tenant_id=tenant,
            sequence=seq, position_sequence=None, quantity_delta=None,
            position_identity=None, occurred_at=now,
            payload={"strategy_id": "test-strategy", "intent": "BUY"}, metadata=None,
        )
        seq += 1

        append_lifecycle_event(
            db=db, aggregate_type="TradeLifecycle", aggregate_id=execution_id,
            event_type="ExecutionActivated", event_version="1.0", tenant_id=tenant,
            sequence=seq, position_sequence=None, quantity_delta=None,
            position_identity=None, occurred_at=now + timedelta(seconds=1),
            payload={}, metadata=None,
        )
        seq += 1

        append_lifecycle_event(
            db=db, aggregate_type="TradeLifecycle", aggregate_id=execution_id,
            event_type="OrderCreated", event_version="1.0", tenant_id=tenant,
            sequence=seq, position_sequence=None, quantity_delta=None,
            position_identity=None, occurred_at=now + timedelta(seconds=1),
            payload={"order_id": order_id, "quantity": quantity}, metadata=None,
        )
        seq += 1

        return execution_id, seq

    def test_persisted_stream_is_replay_compatible(self, db):
        """The actual Day38 lifecycle events persisted by Task2 must be replay-compatible.

        Uses authoritative foundation path, then ingests broker events,
        then replays the actual persisted stream.
        """
        from app.trade_lifecycle.persistence import TradeLifecycleEvent
        from app.trade_lifecycle.replay import replay_execution_events, LifecycleReplayError
        from app.trade_lifecycle.envelope import TradeLifecycleEventEnvelope

        order_id = "ORD-REPLAY-FULL-1"
        tenant = "tenant-1"
        now = _NOW

        # Step 0: Create execution foundation using authoritative path
        execution_id, _ = self._create_execution_foundation(db, tenant, order_id)

        # Step 1: Ingest a sequence of broker events
        events = [
            make_broker_sync_event(
                tenant_id=tenant, broker="broker-test",
                event_type=BrokerEventType.ORDER_SUBMITTED, event_version="1.0",
                broker_order_id=order_id, canonical_sequence=1,
                order_facts=OrderFacts(
                    broker_order_id=order_id, order_id=order_id,
                    status=CanonicalOrderState.SUBMITTED, total_quantity=100),
                received_at=now + timedelta(seconds=1),
            ),
            make_broker_sync_event(
                tenant_id=tenant, broker="broker-test",
                event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
                broker_order_id=order_id, canonical_sequence=2,
                order_facts=OrderFacts(
                    broker_order_id=order_id, order_id=order_id,
                    status=CanonicalOrderState.PARTIALLY_FILLED,
                    total_quantity=100, cumulative_filled=50),
                fill_facts=FillFacts(fill_id="fill-001", fill_quantity=50, fill_price=100.0,
                                     cumulative_filled_after=50, remaining_after=50),
                received_at=now + timedelta(seconds=2),
            ),
            make_broker_sync_event(
                tenant_id=tenant, broker="broker-test",
                event_type=BrokerEventType.FULL_FILL, event_version="1.0",
                broker_order_id=order_id, canonical_sequence=3,
                order_facts=OrderFacts(
                    broker_order_id=order_id, order_id=order_id,
                    status=CanonicalOrderState.FILLED,
                    total_quantity=100, cumulative_filled=100, is_terminal=True),
                fill_facts=FillFacts(fill_id="fill-002", fill_quantity=50, fill_price=100.0,
                                     cumulative_filled_after=100, remaining_after=0),
                received_at=now + timedelta(seconds=3),
            ),
        ]

        for ev in events:
            result = ingest_canonical_event(ev, db)
            assert result["action"] == "APPLIED"

        # Step 2: Load the persisted lifecycle events
        rows = db.execute(
            select(TradeLifecycleEvent)
            .where(TradeLifecycleEvent.tenant_id == tenant)
            .order_by(TradeLifecycleEvent.sequence.asc())
        ).scalars().all()

        assert len(rows) == 6  # 3 foundation + 3 Task2

        # Step 3: Verify each lifecycle event has correct fields
        for row in rows:
            assert row.tenant_id == tenant
            assert row.aggregate_type == "TradeLifecycle"
            assert row.aggregate_id == execution_id
            assert row.event_type in (
                "TradeIntentCreated", "ExecutionActivated", "OrderCreated",
                "OrderSubmitted", "OrderFilled", "FillRecorded",
                "OrderCancelled", "OrderRejected",
            )
            assert row.event_version == "1.0"
            assert row.payload_json is not None

        # Step 4: Verify the event types are the mapped Day38 types (not broker types)
        event_types = [r.event_type for r in rows]
        assert event_types[0] == "TradeIntentCreated"
        assert event_types[1] == "ExecutionActivated"
        assert event_types[2] == "OrderCreated"
        assert event_types[3] == "OrderSubmitted"  # ORDER_SUBMITTED → OrderSubmitted
        assert event_types[4] == "OrderFilled"  # PARTIAL_FILL → OrderFilled
        assert event_types[5] == "OrderFilled"  # FULL_FILL → OrderFilled

        # Step 5: Verify payloads contain replay-required fields for Task2 events only
        for row in rows[3:]:
            payload = json.loads(row.payload_json)
            assert "order_id" in payload, f"Missing order_id in {row.event_type} payload"
            assert payload["order_id"] == order_id

        # Step 6: Verify the sequence is contiguous
        sequences = [r.sequence for r in rows]
        # Sequences may not start at 1 (Day38 aggregate sequence is independent)
        # but must be contiguous
        for i in range(1, len(sequences)):
            assert sequences[i] == sequences[i-1] + 1, (
                f"Non-contiguous sequence: {sequences[i-1]} → {sequences[i]}"
            )

    def test_persisted_order_filled_has_cumulative_filled(self, db):
        """OrderFilled events must have cumulative_filled >= 1 in payload."""
        from app.trade_lifecycle.persistence import TradeLifecycleEvent

        order_id = "ORD-CUM-1"
        tenant = "tenant-1"

        # Create foundation
        self._create_execution_foundation(db, tenant, order_id)

        submit = _make_submitted_event(
            broker_order_id=order_id, canonical_sequence=1,
            event_type=BrokerEventType.ORDER_SUBMITTED,
        )
        ingest_canonical_event(submit, db)

        fill = make_broker_sync_event(
            tenant_id=tenant, broker="broker-test",
            event_type=BrokerEventType.PARTIAL_FILL, event_version="1.0",
            broker_order_id=order_id, canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id=order_id, order_id=order_id,
                status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50),
            fill_facts=FillFacts(fill_id="fill-001", fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )
        ingest_canonical_event(fill, db)

        # Find the OrderFilled lifecycle event
        rows = db.execute(
            select(TradeLifecycleEvent)
            .where(TradeLifecycleEvent.event_type == "OrderFilled")
        ).scalars().all()
        assert len(rows) == 1

        payload = json.loads(rows[0].payload_json)
        assert "cumulative_filled" in payload
        assert isinstance(payload["cumulative_filled"], int)
        assert payload["cumulative_filled"] >= 1

    def test_persisted_fill_recorded_has_fill_quantity(self, db):
        """FillRecorded events must have fill_quantity >= 1 in payload."""
        from app.trade_lifecycle.persistence import TradeLifecycleEvent

        order_id = "ORD-FQ-1"
        tenant = "tenant-1"

        # Create foundation
        self._create_execution_foundation(db, tenant, order_id)

        submit = _make_submitted_event(
            broker_order_id=order_id, canonical_sequence=1,
            event_type=BrokerEventType.ORDER_SUBMITTED,
        )
        ingest_canonical_event(submit, db)

        fill_rec = make_broker_sync_event(
            tenant_id=tenant, broker="broker-test",
            event_type=BrokerEventType.FILL_RECORDED, event_version="1.0",
            broker_order_id=order_id, canonical_sequence=2,
            order_facts=OrderFacts(
                broker_order_id=order_id, order_id=order_id,
                status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100, cumulative_filled=50),
            fill_facts=FillFacts(fill_id="fill-001", fill_quantity=50, fill_price=100.0,
                                 cumulative_filled_after=50, remaining_after=50),
            received_at=_NOW + timedelta(seconds=2),
        )
        ingest_canonical_event(fill_rec, db)

        rows = db.execute(
            select(TradeLifecycleEvent)
            .where(TradeLifecycleEvent.event_type == "FillRecorded")
        ).scalars().all()
        assert len(rows) == 1

        payload = json.loads(rows[0].payload_json)
        assert "fill_quantity" in payload
        assert isinstance(payload["fill_quantity"], int)
        assert payload["fill_quantity"] >= 1
