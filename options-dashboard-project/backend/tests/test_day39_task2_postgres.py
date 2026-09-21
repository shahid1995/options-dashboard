"""Day 39 Task 2 — PostgreSQL verification suite.

Requires TEST_DATABASE_URL pointing to a disposable PostgreSQL database.
Tests concurrency, transactionality, and migration correctness that
SQLite cannot verify.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
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
from app.broker_sync.models import (
    BrokerOrderProjection,
    BrokerSyncIdempotency,
    BrokerSyncSequenceAnchor,
)
from app.db import Base
import app.models  # noqa: F401
from app.trade_lifecycle.persistence import TradeLifecycleEvent

PG_DB_URL = os.getenv("TEST_DATABASE_URL", "")
_pg_available = bool(
    PG_DB_URL
    and PG_DB_URL.startswith(("postgresql+psycopg://", "postgresql://"))
)

_NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_engine():
    """Module-scoped PostgreSQL engine for disposable testing."""
    if not _pg_available:
        pytest.skip("TEST_DATABASE_URL must point to PostgreSQL")
    engine = create_engine(
        PG_DB_URL, pool_pre_ping=True, pool_size=5, max_overflow=5
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed_app_order(db, *, tenant, client_order_id, execution_id):
    from app.models import PaperOrder, StrategyExecution
    exists = db.execute(select(StrategyExecution).where(
        StrategyExecution.user_id == tenant,
        StrategyExecution.execution_id == execution_id,
    )).scalar_one_or_none()
    if exists is None:
        db.add(StrategyExecution(
            user_id=tenant, execution_id=execution_id,
            client_order_id=f"exec-{execution_id}",
            strategy_id="strat-pg", strategy_tag="Test",
            symbol="NIFTY", status="PENDING", entry_net=0.0, entry_at=_NOW,
        ))
    db.flush()
    exists = db.execute(select(PaperOrder).where(
        PaperOrder.user_id == tenant,
        PaperOrder.client_order_id == client_order_id,
    )).scalar_one_or_none()
    if exists is None:
        db.add(PaperOrder(
            user_id=tenant, client_order_id=client_order_id,
            execution_id=execution_id, kind="entry", symbol="NIFTY",
            expiry="2026-10-29", strike=24500.0, option_type="CE",
            action="buy", quantity=100, lot_size=1, status="PENDING",
            filled_quantity=0,
        ))
    db.flush()

PG_ORDER_IDS = [
    "ORD-PG-1", "ORD-PG-CONC1", "ORD-PG-CONC2", "ORD-PG-DUP",
    "ORD-PG-FI-1", "ORD-PG-FI-2", "ORD-PG-FI-3", "ORD-PG-FI-4",
    "ORD-PG-GAP", "ORD-PG-LC", "ORD-PG-NOSEQ", "ORD-PG-PROJ-C",
    "ORD-PG-PROJ-FF", "ORD-PG-PROJ-PF", "ORD-PG-PROJ-S", "ORD-PG-PROJ-TB",
    "ORD-PG-SEQ", "ORD-PG-SEQ-IND", "ORD-PG-STALE", "ORD-PG-TXN",
    "ORD-PG-TXN-R", "ORD-PG-TXN-RB", "ORD-PG-UC", "ORD-PG-REPLAY",
    "ORD-PG-IND1", "ORD-PG-IND2",
]

@pytest.fixture()
def pg_db(pg_engine):
    Session = sessionmaker(bind=pg_engine, expire_on_commit=False)
    session = Session()
    for t in (BrokerSyncIdempotency.__table__, BrokerOrderProjection.__table__,
              BrokerSyncSequenceAnchor.__table__):
        session.execute(t.delete())
    session.execute(text("DELETE FROM trade_lifecycle_events WHERE tenant_id = 'tenant-pg-1'"))
    session.execute(text("DELETE FROM paper_orders WHERE user_id = 'tenant-pg-1'"))
    session.execute(text("DELETE FROM strategy_executions WHERE user_id = 'tenant-pg-1'"))
    session.flush()
    for oid in PG_ORDER_IDS:
        _seed_app_order(session, tenant="tenant-pg-1",
                        client_order_id=oid, execution_id=f"EXEC-{oid}")
    session.commit()
    yield session
    session.rollback()
    session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_submitted_event(
    broker_order_id: str = "ORD-PG-1",
    tenant_id: str = "tenant-pg-1",
    broker: str = "broker-pg",
    canonical_sequence: int | None = 1,
    total_quantity: int = 100,
    received_at: datetime | None = None,
) -> BrokerSyncEvent:
    return make_broker_sync_event(
        tenant_id=tenant_id,
        broker=broker,
        event_type=BrokerEventType.ORDER_SUBMITTED.value,
        event_version="1.0",
        broker_order_id=broker_order_id,
        canonical_sequence=canonical_sequence,
        received_at=received_at or (_NOW + timedelta(seconds=1)),
        order_facts=OrderFacts(order_id=broker_order_id, 
            broker_order_id=broker_order_id,
            status=CanonicalOrderState.SUBMITTED,
            total_quantity=total_quantity,
            cumulative_filled=0,
        ),
    )


def _make_fill_event(
    broker_order_id: str = "ORD-PG-1",
    tenant_id: str = "tenant-pg-1",
    broker: str = "broker-pg",
    canonical_sequence: int = 2,
    fill_id: str = "fill-pg-1",
    fill_quantity: int = 50,
    cumulative_after: int = 50,
    total_quantity: int = 100,
    received_at: datetime | None = None,
) -> BrokerSyncEvent:
    return make_broker_sync_event(
        tenant_id=tenant_id,
        broker=broker,
        event_type=BrokerEventType.PARTIAL_FILL,
        event_version="1.0",
        broker_order_id=broker_order_id,
        canonical_sequence=canonical_sequence,
        received_at=received_at or (_NOW + timedelta(seconds=2)),
        order_facts=OrderFacts(order_id=broker_order_id, 
            broker_order_id=broker_order_id,
            status=CanonicalOrderState.PARTIALLY_FILLED,
            total_quantity=total_quantity,
            cumulative_filled=cumulative_after,
        ),
        fill_facts=FillFacts(
            fill_id=fill_id,
            fill_quantity=fill_quantity,
            fill_price=100.0,
            cumulative_filled_after=cumulative_after,
            remaining_after=total_quantity - cumulative_after,
        ),
    )


# ---------------------------------------------------------------------------
# 1. Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _pg_available, reason="requires PostgreSQL")
class TestPostgresIdempotency:
    def test_first_event_persists_and_applies(self, pg_db):
        event = _make_submitted_event()
        result = ingest_canonical_event(event, pg_db)
        assert result["action"] == "APPLIED"
        pg_db.flush()
        row = pg_db.execute(
            select(BrokerSyncIdempotency).where(
                BrokerSyncIdempotency.canonical_id == event.canonical_id
            )
        ).scalar_one_or_none()
        assert row is not None
        assert row.status == "APPLIED"

    def test_identical_duplicate_is_durable_noop(self, pg_db):
        event = _make_submitted_event()
        ingest_canonical_event(event, pg_db)
        pg_db.commit()

        Session = sessionmaker(bind=pg_db.get_bind(), expire_on_commit=False)
        new_db = Session()
        try:
            result = ingest_canonical_event(event, new_db)
            assert result["action"] == "DUPLICATE_NOOP"
        finally:
            new_db.close()

    def test_conflicting_duplicate_rejected(self, pg_db):
        event = _make_submitted_event()
        ingest_canonical_event(event, pg_db)

        different = make_broker_sync_event(
            tenant_id="tenant-pg-1",
            broker="broker-pg",
            event_type=BrokerEventType.ORDER_SUBMITTED,
            event_version="1.0",
            broker_order_id="ORD-PG-1",
            canonical_sequence=1,
            received_at=_NOW + timedelta(seconds=2),
            order_facts=OrderFacts(order_id="ORD-PG-1", 
                broker_order_id="ORD-PG-1",
                status=CanonicalOrderState.SUBMITTED,
                total_quantity=999,
            ),
        )
        result = ingest_canonical_event(different, pg_db)
        assert result["action"] == "CONFLICT"

    def test_duplicate_after_session_recreation(self, pg_db):
        event = _make_submitted_event()
        ingest_canonical_event(event, pg_db)
        pg_db.commit()

        Session = sessionmaker(bind=pg_db.get_bind(), expire_on_commit=False)
        new_db = Session()
        try:
            result = ingest_canonical_event(event, new_db)
            assert result["action"] == "DUPLICATE_NOOP"
        finally:
            new_db.close()


# ---------------------------------------------------------------------------
# 2. Ordering
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _pg_available, reason="requires PostgreSQL")
class TestPostgresOrdering:
    def test_sequential_1_2_3_accepted(self, pg_db):
        for seq in [1, 2, 3]:
            et = (
                BrokerEventType.ORDER_SUBMITTED
                if seq == 1
                else BrokerEventType.ORDER_ACCEPTED
            )
            ev = make_broker_sync_event(
                tenant_id="tenant-pg-1",
                broker="broker-pg",
                event_type=et,
                event_version="1.0",
                broker_order_id="ORD-PG-SEQ",
                canonical_sequence=seq,
                received_at=_NOW + timedelta(seconds=seq),
                order_facts=OrderFacts(order_id="ORD-PG-SEQ", 
                    broker_order_id="ORD-PG-SEQ",
                    status=CanonicalOrderState.OPEN,
                    total_quantity=100,
                ),
            )
            result = ingest_canonical_event(ev, pg_db)
            assert result["action"] == "APPLIED"

    def test_duplicate_sequence_identical_content_noop(self, pg_db):
        submit = _make_submitted_event(broker_order_id="ORD-PG-DUP")
        ingest_canonical_event(submit, pg_db)

        acc = _make_submitted_event(
            broker_order_id="ORD-PG-DUP", canonical_sequence=2
        )
        acc2 = make_broker_sync_event(
            tenant_id="tenant-pg-1",
            broker="broker-pg",
            event_type=BrokerEventType.ORDER_ACCEPTED,
            event_version="1.0",
            broker_order_id="ORD-PG-DUP",
            canonical_sequence=2,
            received_at=_NOW + timedelta(seconds=2),
            order_facts=OrderFacts(order_id="ORD-PG-DUP", 
                broker_order_id="ORD-PG-DUP",
                status=CanonicalOrderState.OPEN,
                total_quantity=100,
            ),
        )
        ingest_canonical_event(acc2, pg_db)
        result = ingest_canonical_event(acc2, pg_db)
        assert result["action"] == "DUPLICATE_NOOP"

    def test_sequence_gap_rejected(self, pg_db):
        submit = _make_submitted_event(broker_order_id="ORD-PG-GAP")
        ingest_canonical_event(submit, pg_db)

        gap = make_broker_sync_event(
            tenant_id="tenant-pg-1",
            broker="broker-pg",
            event_type=BrokerEventType.ORDER_ACCEPTED,
            event_version="1.0",
            broker_order_id="ORD-PG-GAP",
            canonical_sequence=3,
            received_at=_NOW + timedelta(seconds=2),
            order_facts=OrderFacts(order_id="ORD-PG-GAP", 
                broker_order_id="ORD-PG-GAP",
                status=CanonicalOrderState.OPEN,
                total_quantity=100,
            ),
        )
        result = ingest_canonical_event(gap, pg_db)
        assert result["action"] == "REJECTED"
        assert "gap" in result["reason"].lower()

    def test_stale_sequence_rejected(self, pg_db):
        for seq in [1, 2, 3]:
            et = (
                BrokerEventType.ORDER_SUBMITTED
                if seq == 1
                else BrokerEventType.ORDER_ACCEPTED
            )
            ev = make_broker_sync_event(
                tenant_id="tenant-pg-1",
                broker="broker-pg",
                event_type=et,
                event_version="1.0",
                broker_order_id="ORD-PG-STALE",
                canonical_sequence=seq,
                received_at=_NOW + timedelta(seconds=seq),
                order_facts=OrderFacts(order_id="ORD-PG-STALE", 
                    broker_order_id="ORD-PG-STALE",
                    status=CanonicalOrderState.SUBMITTED,
                    total_quantity=100,
                ),
            )
            ingest_canonical_event(ev, pg_db)

        stale = make_broker_sync_event(
            tenant_id="tenant-pg-1",
            broker="broker-pg",
            event_type=BrokerEventType.ORDER_ACCEPTED,
            event_version="1.0",
            broker_order_id="ORD-PG-STALE",
            canonical_sequence=2,
            provider_event_id="pg-provider-stale-1",  # NEW canonical identity
            received_at=_NOW + timedelta(seconds=4),
            order_facts=OrderFacts(order_id="ORD-PG-STALE", 
                broker_order_id="ORD-PG-STALE",
                status=CanonicalOrderState.OPEN,
                total_quantity=200,
            ),
        )
        result = ingest_canonical_event(stale, pg_db)
        assert result["action"] == "REJECTED"
        assert "stale" in result["reason"].lower()

    def test_missing_sequence_no_fabrication(self, pg_db):
        # Day41.2 reconciliation: sequence-less cross-D1 observations carry
        # provider event_timestamp S2 evidence — the approved rule classifies
        # by S2 (missing ⇒ UNRESOLVED), never by arrival order.
        ev1 = make_broker_sync_event(
            tenant_id="tenant-pg-1",
            broker="broker-pg",
            event_type=BrokerEventType.ORDER_SUBMITTED,
            event_version="1.0",
            broker_order_id="ORD-PG-NOSEQ",
            canonical_sequence=None,
            provider_event_id="pg-evt-001",
            event_timestamp=_NOW + timedelta(seconds=1),
            received_at=_NOW + timedelta(seconds=1),
            order_facts=OrderFacts(order_id="ORD-PG-NOSEQ", 
                broker_order_id="ORD-PG-NOSEQ",
                status=CanonicalOrderState.SUBMITTED,
                total_quantity=100,
            ),
        )
        r1 = ingest_canonical_event(ev1, pg_db)
        assert r1["action"] == "APPLIED"

        ev2 = make_broker_sync_event(
            tenant_id="tenant-pg-1",
            broker="broker-pg",
            event_type=BrokerEventType.PARTIAL_FILL,
            event_version="1.0",
            broker_order_id="ORD-PG-NOSEQ",
            canonical_sequence=None,
            provider_event_id="pg-evt-002",
            event_timestamp=_NOW + timedelta(seconds=2),
            received_at=_NOW + timedelta(seconds=2),
            order_facts=OrderFacts(order_id="ORD-PG-NOSEQ", 
                broker_order_id="ORD-PG-NOSEQ",
                status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100,
                cumulative_filled=50,
            ),
            fill_facts=FillFacts(
                fill_quantity=50,
                fill_price=100.0,
                cumulative_filled_after=50,
                remaining_after=50,
            ),
        )
        r2 = ingest_canonical_event(ev2, pg_db)
        assert r2["action"] == "APPLIED"


# ---------------------------------------------------------------------------
# 3. Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _pg_available, reason="requires PostgreSQL")
class TestPostgresConcurrency:
    def test_concurrent_same_event_one_applied_one_noop(self, pg_db):
        submit = _make_submitted_event(broker_order_id="ORD-PG-CONC1")
        ingest_canonical_event(submit, pg_db)
        pg_db.commit()

        fill_event = _make_fill_event(
            broker_order_id="ORD-PG-CONC1",
            canonical_sequence=2,
            fill_id="fill-conc-same",
        )

        results = [None, None]
        errors = [None, None]
        Engine = pg_db.get_bind()

        def worker(idx):
            Session = sessionmaker(bind=Engine, expire_on_commit=False)
            sess = Session()
            try:
                results[idx] = ingest_canonical_event(fill_event, sess)
                sess.commit()
            except Exception as e:
                errors[idx] = e
                sess.rollback()
            finally:
                sess.close()

        t1 = threading.Thread(target=worker, args=(0,))
        t2 = threading.Thread(target=worker, args=(1,))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert errors[0] is None, f"Worker 0: {errors[0]}"
        assert errors[1] is None, f"Worker 1: {errors[1]}"
        actions = {results[0]["action"], results[1]["action"]}
        assert "APPLIED" in actions
        assert "DUPLICATE_NOOP" in actions

    def test_concurrent_different_events_same_sequence(self, pg_db):
        submit = _make_submitted_event(broker_order_id="ORD-PG-CONC2")
        ingest_canonical_event(submit, pg_db)
        pg_db.commit()

        fill_a = _make_fill_event(
            broker_order_id="ORD-PG-CONC2",
            canonical_sequence=2,
            fill_id="fill-conc-a",
            fill_quantity=50,
            cumulative_after=50,
        )
        fill_b = _make_fill_event(
            broker_order_id="ORD-PG-CONC2",
            canonical_sequence=2,
            fill_id="fill-conc-b",
            fill_quantity=25,
            cumulative_after=75,
        )

        results = [None, None]
        errors = [None, None]
        Engine = pg_db.get_bind()

        def worker(idx, event):
            Session = sessionmaker(bind=Engine, expire_on_commit=False)
            sess = Session()
            try:
                results[idx] = ingest_canonical_event(event, sess)
                sess.commit()
            except Exception as e:
                errors[idx] = e
                sess.rollback()
            finally:
                sess.close()

        t1 = threading.Thread(target=worker, args=(0, fill_a))
        t2 = threading.Thread(target=worker, args=(1, fill_b))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert errors[0] is None, f"Worker 0: {errors[0]}"
        assert errors[1] is None, f"Worker 1: {errors[1]}"
        applied = sum(
            1 for r in results if r and r["action"] == "APPLIED"
        )
        assert applied == 1

    def test_concurrent_independent_events_both_applied(self, pg_db):
        results = [None, None]
        errors = [None, None]
        Engine = pg_db.get_bind()

        def worker(idx, order_id, seq):
            Session = sessionmaker(bind=Engine, expire_on_commit=False)
            sess = Session()
            try:
                ev = make_broker_sync_event(
                    tenant_id="tenant-pg-1",
                    broker="broker-pg",
                    event_type=BrokerEventType.ORDER_SUBMITTED,
                    event_version="1.0",
                    broker_order_id=order_id,
                    canonical_sequence=seq,
                    received_at=_NOW + timedelta(seconds=1),
                    order_facts=OrderFacts(order_id=order_id, 
                        broker_order_id=order_id,
                        status=CanonicalOrderState.SUBMITTED,
                        total_quantity=100,
                    ),
                )
                results[idx] = ingest_canonical_event(ev, sess)
                sess.commit()
            except Exception as e:
                errors[idx] = e
                sess.rollback()
            finally:
                sess.close()

        t1 = threading.Thread(target=worker, args=(0, "ORD-PG-IND1", 1))
        t2 = threading.Thread(target=worker, args=(1, "ORD-PG-IND2", 1))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert errors[0] is None, f"Worker 0: {errors[0]}"
        assert errors[1] is None, f"Worker 1: {errors[1]}"
        assert results[0]["action"] == "APPLIED"
        assert results[1]["action"] == "APPLIED"


# ---------------------------------------------------------------------------
# 4. Transactionality
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _pg_available, reason="requires PostgreSQL")
class TestPostgresTransactionality:
    def test_commit_persists_all_three(self, pg_db):
        event = _make_submitted_event(broker_order_id="ORD-PG-TXN")
        result = ingest_canonical_event(event, pg_db)
        assert result["action"] == "APPLIED"
        pg_db.flush()

        idem = pg_db.execute(
            select(func.count(BrokerSyncIdempotency.canonical_id))
        ).scalar()
        proj = pg_db.execute(
            select(func.count(BrokerOrderProjection.id))
        ).scalar()
        lc = pg_db.execute(
            select(func.count(TradeLifecycleEvent.event_id))
        ).scalar()
        assert idem == 1
        assert proj == 1
        assert lc == 1

    def test_rollback_on_lifecycle_failure_rolls_back_all(self, pg_db):
        from app.broker_sync import ingestion as ing_mod

        original = ing_mod.append_lifecycle_event

        def failing(*a, **kw):
            raise RuntimeError("simulated lifecycle failure")

        ing_mod.append_lifecycle_event = failing
        try:
            event = _make_submitted_event(broker_order_id="ORD-PG-TXN-RB")
            with pytest.raises(RuntimeError):
                ingest_canonical_event(event, pg_db)
            pg_db.rollback()

            idem = pg_db.execute(
                select(func.count(BrokerSyncIdempotency.canonical_id))
            ).scalar()
            proj = pg_db.execute(
                select(func.count(BrokerOrderProjection.id))
            ).scalar()
            lc = pg_db.execute(
                select(func.count(TradeLifecycleEvent.event_id))
            ).scalar()
            assert idem == 0
            assert proj == 0
            assert lc == 0
        finally:
            ing_mod.append_lifecycle_event = original

    def test_retry_after_rollback_succeeds(self, pg_db):
        from app.broker_sync import ingestion as ing_mod

        original = ing_mod.append_lifecycle_event
        call_count = [0]

        def flaky(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("transient failure")
            return original(*a, **kw)

        ing_mod.append_lifecycle_event = flaky
        try:
            event = _make_submitted_event(broker_order_id="ORD-PG-TXN-R")
            with pytest.raises(RuntimeError):
                ingest_canonical_event(event, pg_db)
            pg_db.rollback()

            result = ingest_canonical_event(event, pg_db)
            assert result["action"] == "APPLIED"
        finally:
            ing_mod.append_lifecycle_event = original


# ---------------------------------------------------------------------------
# 5. Projection
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _pg_available, reason="requires PostgreSQL")
class TestPostgresProjection:
    def test_submitted_projection(self, pg_db):
        event = _make_submitted_event(broker_order_id="ORD-PG-PROJ-S")
        result = ingest_canonical_event(event, pg_db)
        assert result["action"] == "APPLIED"
        assert result["normalized_state"]["status"] == "SUBMITTED"
        assert result["normalized_state"]["total_quantity"] == 100

    def test_partial_fill_projection(self, pg_db):
        submit = _make_submitted_event(broker_order_id="ORD-PG-PROJ-PF")
        ingest_canonical_event(submit, pg_db)

        fill = _make_fill_event(
            broker_order_id="ORD-PG-PROJ-PF",
            canonical_sequence=2,
            fill_quantity=30,
            cumulative_after=30,
        )
        result = ingest_canonical_event(fill, pg_db)
        assert result["action"] == "APPLIED"
        ns = result["normalized_state"]
        assert ns["status"] == "PARTIALLY_FILLED"
        assert ns["cumulative_filled"] == 30
        assert ns["remaining_quantity"] == 70

    def test_full_fill_terminal_projection(self, pg_db):
        submit = _make_submitted_event(broker_order_id="ORD-PG-PROJ-FF")
        ingest_canonical_event(submit, pg_db)

        fill = make_broker_sync_event(
            tenant_id="tenant-pg-1",
            broker="broker-pg",
            event_type=BrokerEventType.FULL_FILL,
            event_version="1.0",
            broker_order_id="ORD-PG-PROJ-FF",
            canonical_sequence=2,
            received_at=_NOW + timedelta(seconds=2),
            order_facts=OrderFacts(order_id="ORD-PG-PROJ-FF", 
                broker_order_id="ORD-PG-PROJ-FF",
                status=CanonicalOrderState.FILLED,
                total_quantity=100,
                cumulative_filled=100,
                is_terminal=True,
            ),
            fill_facts=FillFacts(
                fill_quantity=100,
                fill_price=100.0,
                cumulative_filled_after=100,
                remaining_after=0,
            ),
        )
        result = ingest_canonical_event(fill, pg_db)
        assert result["action"] == "APPLIED"
        assert result["normalized_state"]["status"] == "FILLED"
        assert result["normalized_state"]["is_terminal"] is True

    def test_cancelled_terminal_projection(self, pg_db):
        submit = _make_submitted_event(broker_order_id="ORD-PG-PROJ-C")
        ingest_canonical_event(submit, pg_db)

        cancel = make_broker_sync_event(
            tenant_id="tenant-pg-1",
            broker="broker-pg",
            event_type=BrokerEventType.ORDER_CANCELLED,
            event_version="1.0",
            broker_order_id="ORD-PG-PROJ-C",
            canonical_sequence=2,
            received_at=_NOW + timedelta(seconds=2),
            order_facts=OrderFacts(order_id="ORD-PG-PROJ-C", 
                broker_order_id="ORD-PG-PROJ-C",
                status=CanonicalOrderState.CANCELLED,
                total_quantity=100,
            ),
        )
        result = ingest_canonical_event(cancel, pg_db)
        assert result["action"] == "APPLIED"
        assert result["normalized_state"]["status"] == "CANCELLED"
        assert result["normalized_state"]["is_terminal"] is True

    def test_terminal_state_blocks_further_events(self, pg_db):
        submit = _make_submitted_event(broker_order_id="ORD-PG-PROJ-TB")
        ingest_canonical_event(submit, pg_db)

        cancel = make_broker_sync_event(
            tenant_id="tenant-pg-1",
            broker="broker-pg",
            event_type=BrokerEventType.ORDER_CANCELLED,
            event_version="1.0",
            broker_order_id="ORD-PG-PROJ-TB",
            canonical_sequence=2,
            received_at=_NOW + timedelta(seconds=2),
            order_facts=OrderFacts(order_id="ORD-PG-PROJ-TB", 
                broker_order_id="ORD-PG-PROJ-TB",
                status=CanonicalOrderState.CANCELLED,
                total_quantity=100,
            ),
        )
        ingest_canonical_event(cancel, pg_db)

        fill = _make_fill_event(
            broker_order_id="ORD-PG-PROJ-TB",
            canonical_sequence=3,
            fill_quantity=50,
            cumulative_after=50,
        )
        result = ingest_canonical_event(fill, pg_db)
        assert result["action"] == "REJECTED"
        assert "terminal" in result["reason"].lower()


# ---------------------------------------------------------------------------
# 6. Day38 lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _pg_available, reason="requires PostgreSQL")
class TestPostgresDay38:
    def test_lifecycle_event_persisted_with_correct_fields(self, pg_db):
        event = _make_submitted_event(broker_order_id="ORD-PG-LC")
        result = ingest_canonical_event(event, pg_db)
        assert result["action"] == "APPLIED"
        pg_db.flush()

        row = pg_db.execute(
            select(TradeLifecycleEvent)
            .where(TradeLifecycleEvent.tenant_id == "tenant-pg-1")
            .order_by(TradeLifecycleEvent.sequence.desc())
            .limit(1)
        ).scalar_one()
        assert row.aggregate_type == "TradeLifecycle"
        assert row.aggregate_id == "EXEC-ORD-PG-LC"
        assert row.event_type == "OrderSubmitted"
        assert row.tenant_id == "tenant-pg-1"
        assert row.event_version == "1.0"
        assert row.sequence >= 1

    def test_day38_sequence_independent_of_broker_sequence(self, pg_db):
        # Day41.2 reconciliation: provider event_timestamp S2 evidence —
        # sequence-less cross-D1 classification never uses arrival order.
        submit = make_broker_sync_event(
            tenant_id="tenant-pg-1",
            broker="broker-pg",
            event_type=BrokerEventType.ORDER_SUBMITTED.value,
            event_version="1.0",
            broker_order_id="ORD-PG-SEQ-IND",
            canonical_sequence=None,
            provider_event_id="pg-seq-ind-001",
            event_timestamp=_NOW + timedelta(seconds=1),
            received_at=_NOW + timedelta(seconds=1),
            order_facts=OrderFacts(order_id="ORD-PG-SEQ-IND", 
                broker_order_id="ORD-PG-SEQ-IND",
                status=CanonicalOrderState.SUBMITTED,
                total_quantity=100,
            ),
        )
        ingest_canonical_event(submit, pg_db)

        fill = make_broker_sync_event(
            tenant_id="tenant-pg-1",
            broker="broker-pg",
            event_type=BrokerEventType.FULL_FILL.value,
            event_version="1.0",
            broker_order_id="ORD-PG-SEQ-IND",
            canonical_sequence=None,
            provider_event_id="pg-seq-ind-002",
            event_timestamp=_NOW + timedelta(seconds=2),
            received_at=_NOW + timedelta(seconds=2),
            order_facts=OrderFacts(order_id="ORD-PG-SEQ-IND", 
                broker_order_id="ORD-PG-SEQ-IND",
                status=CanonicalOrderState.FILLED,
                total_quantity=100,
                cumulative_filled=100,
                is_terminal=True,
            ),
            fill_facts=FillFacts(
                fill_quantity=100,
                fill_price=100.0,
                cumulative_filled_after=100,
                remaining_after=0,
            ),
        )
        ingest_canonical_event(fill, pg_db)

        rows = pg_db.execute(
            select(TradeLifecycleEvent)
            .where(TradeLifecycleEvent.aggregate_id == "EXEC-ORD-PG-SEQ-IND")
            .order_by(TradeLifecycleEvent.sequence.asc())
        ).scalars().all()
        assert len(rows) == 2
        assert rows[0].sequence == 1
        assert rows[1].sequence == 2
        # Day38 sequence is independent of canonical_sequence (which is None)
        assert rows[0].event_type == "OrderSubmitted"
        assert rows[1].event_type == "OrderFilled"

    def test_day38_replay_compatibility(self, pg_db):
        import json
        from datetime import timedelta

        from app.trade_lifecycle.envelope import TradeLifecycleEventEnvelope
        from app.trade_lifecycle.persistence import (
            append_lifecycle_event as direct_append,
        )
        from app.trade_lifecycle.replay import (
            LifecycleReplayError,
            replay_execution_events,
        )

        order_id = "ORD-PG-REPLAY"
        exec_id = "EXEC-ORD-PG-REPLAY"
        tenant = "tenant-pg-1"

        # Build foundation events directly
        base = _NOW
        foundation = []
        foundation.append(
            TradeLifecycleEventEnvelope(
                tenant_id=tenant,
                aggregate_type="TradeLifecycle",
                aggregate_id=exec_id,
                event_type="TradeIntentCreated",
                event_version="1.0",
                sequence=1,
                occurred_at=base,
                payload={"strategy_id": "test"},
            )
        )
        foundation.append(
            TradeLifecycleEventEnvelope(
                tenant_id=tenant,
                aggregate_type="TradeLifecycle",
                aggregate_id=exec_id,
                event_type="ExecutionActivated",
                event_version="1.0",
                sequence=2,
                occurred_at=base + timedelta(seconds=1),
                payload={},
            )
        )
        foundation.append(
            TradeLifecycleEventEnvelope(
                tenant_id=tenant,
                aggregate_type="TradeLifecycle",
                aggregate_id=exec_id,
                event_type="OrderCreated",
                event_version="1.0",
                sequence=3,
                occurred_at=base + timedelta(seconds=1),
                payload={"order_id": order_id, "quantity": 100},
            )
        )

        # Persist foundation
        for ev in foundation:
            direct_append(
                db=pg_db,
                aggregate_type=ev.aggregate_type,
                aggregate_id=ev.aggregate_id,
                event_type=ev.event_type,
                event_version=ev.event_version,
                tenant_id=ev.tenant_id,
                sequence=ev.sequence,
                position_sequence=None,
                quantity_delta=None,
                position_identity=None,
                occurred_at=ev.occurred_at,
                payload=dict(ev.payload),
            )
        pg_db.flush()

        # Ingest broker ORDER_SUBMITTED via Task2
        broker_event = make_broker_sync_event(
            tenant_id=tenant,
            broker="broker-pg",
            event_type=BrokerEventType.ORDER_SUBMITTED,
            event_version="1.0",
            broker_order_id=order_id,
            canonical_sequence=1,
            order_facts=OrderFacts(
                broker_order_id=order_id,
                order_id=order_id,
                status=CanonicalOrderState.SUBMITTED,
                total_quantity=100,
            ),
            received_at=base + timedelta(seconds=2),
        )
        result = ingest_canonical_event(broker_event, pg_db)
        assert result["action"] == "APPLIED"

        # Load ALL lifecycle events
        rows = pg_db.execute(
            select(TradeLifecycleEvent)
            .where(TradeLifecycleEvent.tenant_id == tenant)
            .where(TradeLifecycleEvent.aggregate_id == exec_id)
            .order_by(TradeLifecycleEvent.sequence.asc())
        ).scalars().all()
        assert len(rows) == 4

        # Build envelope stream for replay
        envelope_stream = []
        for r in rows:
            envelope_stream.append(
                TradeLifecycleEventEnvelope(
                    tenant_id=r.tenant_id,
                    aggregate_type=r.aggregate_type,
                    aggregate_id=r.aggregate_id,
                    event_type=r.event_type,
                    event_version=r.event_version,
                    sequence=r.sequence,
                    occurred_at=r.occurred_at,
                    payload=json.loads(r.payload_json),
                )
            )

        # Feed through Day38 replay
        try:
            state = replay_execution_events(envelope_stream)
            assert state.execution_status.value in ("CREATED", "ACTIVE")
            assert order_id in state.orders
            assert state.orders[order_id].status.value == "SUBMITTED"
        except LifecycleReplayError as e:
            pytest.fail(
                f"Task2 OrderSubmitted failed Day38 replay: {e}"
            )


# ---------------------------------------------------------------------------
# 7. Migration / schema
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _pg_available, reason="requires PostgreSQL")
class TestPostgresMigration:
    def test_all_expected_tables_exist(self, pg_db):
        insp = inspect(pg_db.get_bind())
        tables = set(insp.get_table_names())
        for name in [
            "broker_sync_idempotency",
            "broker_order_projection",
            "broker_sync_sequence_anchor",
            "trade_lifecycle_events",
        ]:
            assert name in tables, f"Missing table: {name}"

    def test_idempotency_unique_constraint_enforced(self, pg_db):
        event = _make_submitted_event(broker_order_id="ORD-PG-UC")
        ingest_canonical_event(event, pg_db)
        pg_db.flush()

        dup = BrokerSyncIdempotency(
            canonical_id=event.canonical_id,
            tenant_id="tenant-pg-1",
            broker="broker-pg",
            broker_order_id="ORD-PG-UC",
            canonical_sequence=1,
            event_type="ORDER_SUBMITTED",
            event_version="1.0",
            content_fingerprint="fake",
            source_mode="STREAM",
            received_at=_NOW,
            status="APPLIED",
        )
        pg_db.add(dup)
        with pytest.raises(SAIntegrityError):
            pg_db.flush()
        pg_db.rollback()


# ---------------------------------------------------------------------------
# 8. Fill integrity
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _pg_available, reason="requires PostgreSQL")
class TestPostgresFillIntegrity:
    def test_fill_arithmetic_consistency_enforced(self, pg_db):
        submit = _make_submitted_event(broker_order_id="ORD-PG-FI-1")
        ingest_canonical_event(submit, pg_db)

        half = _make_fill_event(
            broker_order_id="ORD-PG-FI-1",
            canonical_sequence=2,
            fill_quantity=50,
            cumulative_after=50,
        )
        ingest_canonical_event(half, pg_db)

        bad = _make_fill_event(
            broker_order_id="ORD-PG-FI-1",
            canonical_sequence=3,
            fill_quantity=20,
            cumulative_after=60,
        )
        result = ingest_canonical_event(bad, pg_db)
        assert result["action"] == "REJECTED"
        assert "less than" in result["reason"].lower()

    def test_overfill_rejected(self, pg_db):
        submit = _make_submitted_event(broker_order_id="ORD-PG-FI-2")
        ingest_canonical_event(submit, pg_db)

        over = make_broker_sync_event(
            tenant_id="tenant-pg-1",
            broker="broker-pg",
            event_type=BrokerEventType.FULL_FILL,
            event_version="1.0",
            broker_order_id="ORD-PG-FI-2",
            canonical_sequence=2,
            received_at=_NOW + timedelta(seconds=2),
            order_facts=OrderFacts(order_id="ORD-PG-FI-2", 
                broker_order_id="ORD-PG-FI-2",
                status=CanonicalOrderState.FILLED,
                total_quantity=100,
                cumulative_filled=101,
                is_terminal=True,
            ),
            fill_facts=FillFacts(
                fill_quantity=101,
                fill_price=100.0,
                cumulative_filled_after=101,
                remaining_after=-1,
            ),
        )
        result = ingest_canonical_event(over, pg_db)
        assert result["action"] == "REJECTED"
        assert "overfill" in result["reason"].lower() or "exceeds" in result["reason"].lower()

    def test_negative_fill_rejected(self, pg_db):
        submit = _make_submitted_event(broker_order_id="ORD-PG-FI-3")
        ingest_canonical_event(submit, pg_db)

        neg = make_broker_sync_event(
            tenant_id="tenant-pg-1",
            broker="broker-pg",
            event_type=BrokerEventType.PARTIAL_FILL,
            event_version="1.0",
            broker_order_id="ORD-PG-FI-3",
            canonical_sequence=2,
            received_at=_NOW + timedelta(seconds=2),
            order_facts=OrderFacts(order_id="ORD-PG-FI-3", 
                broker_order_id="ORD-PG-FI-3",
                status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100,
                cumulative_filled=0,
            ),
            fill_facts=FillFacts(
                fill_quantity=-5,
                fill_price=100.0,
                cumulative_filled_after=-5,
                remaining_after=105,
            ),
        )
        result = ingest_canonical_event(neg, pg_db)
        assert result["action"] == "REJECTED"
        assert "negative" in result["reason"].lower()

    def test_remaining_inconsistency_rejected(self, pg_db):
        submit = _make_submitted_event(broker_order_id="ORD-PG-FI-4")
        ingest_canonical_event(submit, pg_db)

        inc = make_broker_sync_event(
            tenant_id="tenant-pg-1",
            broker="broker-pg",
            event_type=BrokerEventType.PARTIAL_FILL,
            event_version="1.0",
            broker_order_id="ORD-PG-FI-4",
            canonical_sequence=2,
            received_at=_NOW + timedelta(seconds=2),
            order_facts=OrderFacts(order_id="ORD-PG-FI-4", 
                broker_order_id="ORD-PG-FI-4",
                status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=100,
                cumulative_filled=50,
            ),
            fill_facts=FillFacts(
                fill_quantity=50,
                fill_price=100.0,
                cumulative_filled_after=50,
                remaining_after=60,
            ),
        )
        result = ingest_canonical_event(inc, pg_db)
        assert result["action"] == "REJECTED"
        assert "inconsistent" in result["reason"].lower()
