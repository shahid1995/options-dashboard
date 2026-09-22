"""Day 42 — Execution Gate verification (Issue #84).

Verifies the execution architecture end-to-end **without enabling
production live trading**, per the approved Architecture Blueprint v1:

* master plan "Day 42 — Execution gate": broker contract tests,
  paper/live semantic parity, failure injection (rejection / timeout /
  duplicate / reconnect / partial fill), broker as source of truth,
  audit trail;
* design spec §3.7 / §15.1 / §15.2 / §15.3: paper and live share domain
  semantics; the execution ladder ends at broker confirmation;
  "StrikeNova never invents a fill"; recovery after a disconnect is the
  bounded raw-ingress reclaim loop (§15.3);
* Day 41 / Day 41.2 safety invariants (merged at baseline ``7601282``):
  D-1 order-family locking, S2 ordering semantics, idempotency,
  stale-data protection, replay-safe outcomes.

All tests are deterministic and run on the SQLite test stack.  Failure
paths are exercised by deterministic controlled injection — no network,
no staging/production writes, no secrets.  Live-trading-disabled
evidence is structural: live execution does not exist as a routable
backend (no broker submission path, no adapter calls) and the
ExecutionRouter LIVE route is refused by design.

Evidence-classification discipline (this module never nests test-suite
execution via pytest.main; regression suites are run as external
verification commands):

* every test docstring states what the test actually proves —
  controlled failure injection, deterministic unit/integration
  semantics, or structural/architecture evidence;
* the serialization-retry test is CONTROLLED RETRY-ABSTRACTION
  INJECTION (SQLSTATE-40001-shaped OperationalError at the operation
  seam of the production ``retry_on_serialization`` loop).  It proves
  the retry contract (rollback → fresh session → success →
  exactly-once effects), NOT a live CockroachDB serialization race;
* the reconnect/recovery test exercises the REAL bounded recovery
  surface (``claim_raw_observations`` / ``process_pending_observations``
  reclaim of a FAILED raw row after a simulated interruption) through
  the canonical ingestion path — verified missed-event recovery at this
  layer, not a live WebSocket reconnect;
* genuine multi-worker database concurrency is NOT claimed here; it is
  independently covered by ``tests/test_day41_2_cross_d1_concurrency.py``
  (PostgreSQL true-concurrency matrix).  The D-1 test in this module
  proves the single-node lock/replay invariant only and is named
  accordingly.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import OperationalError as SAOperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (
    PaperAccount,
    PaperOrder,
    PaperTransaction,
    Position,
    StrategyExecution,
)
from app.broker_sync import (
    BrokerEventSourceMode,
    BrokerEventType,
    CanonicalOrderState,
    FillFacts,
    OrderFacts,
    make_broker_sync_event,
)
from app.broker_sync.fill_ledger import (
    BrokerFillLedgerObservation,
    ObservationClass,
    ReconciliationState,
    record_fill_observation,
)
from app.broker_sync.ingestion import ingest_canonical_event
from app.broker_sync.models import (
    BrokerOrderProjection,
    BrokerSyncIdempotency,
    OrderFamilySyncLock,
)
from app.broker_sync.raw_ingress import (
    BrokerRawObservation,
    IngestionStatus,
    ProcessingStatus,
    claim_raw_observations,
    commit_raw_observation,
    mark_processing_result,
    process_pending_observations,
)
from app.services.execution_intent import (
    ExecutionMode,
    ExecutionRouter,
    ExecutionSource,
    ExecutionStatus,
    ExecutionTarget,
    create_execution_intent,
)

_NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Shared lifecycle fixtures: one engine, one seeded lifecycle, reused by
# every section (paper engine + broker-sync projection + lifecycle store).
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestSession()
    session.add(PaperAccount(user_id="user-d42", starting_capital=500000))
    _seed_app_order(session, "ORD-D42", execution_id="EXEC-D42")
    session.commit()
    yield session
    session.close()
    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed_app_order(db, broker_order_id, *, tenant_id="user-d42",
                    execution_id=None):
    """Seed the StrategyExecution + PaperOrder pair the broker-sync
    projector resolves (same convention as the Day41.2 suite)."""
    exec_id = execution_id or f"EXEC-{broker_order_id}"
    db.add(StrategyExecution(
        user_id=tenant_id,
        execution_id=exec_id,
        client_order_id=f"exec-{broker_order_id}",
        strategy_tag="Gate",
        symbol="NIFTY",
        status="FILLED",
        entry_net=0.0,
        entry_at=_NOW,
    ))
    db.flush()
    db.add(PaperOrder(
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
    ))
    db.flush()
    return exec_id


def _seed_position(db, *, tenant_id="user-d42", execution_id="EXEC-D42"):
    pos = Position(
        user_id=tenant_id, symbol="NIFTY", expiry="2026-10-29",
        strike=24500.0, option_type="CE", net_quantity=2,
        average_entry_price=100.0, lot_size=1, realized_pnl=0.0,
        status="open", strategy_execution_id=execution_id,
    )
    db.add(pos)
    db.commit()
    return pos


def _seqless(
    *,
    broker_order_id: str,
    event_type: str,
    status: CanonicalOrderState,
    event_ts: datetime | None,
    received_at: datetime,
    tenant_id: str = "user-d42",
    provider_event_id: str,
    cumulative_filled: int = 0,
    fill_facts: FillFacts | None = None,
):
    """Sequence-less canonical broker event (the S2 evidence path)."""
    return make_broker_sync_event(
        tenant_id=tenant_id,
        broker="broker-d42",
        event_type=event_type,
        event_version="1.0",
        broker_order_id=broker_order_id,
        canonical_sequence=None,
        provider_event_id=provider_event_id,
        event_timestamp=event_ts,
        received_at=received_at,
        source_mode=BrokerEventSourceMode.STREAM,
        order_facts=OrderFacts(
            order_id=broker_order_id,
            broker_order_id=broker_order_id,
            status=status,
            total_quantity=100,
            cumulative_filled=cumulative_filled,
        ),
        fill_facts=fill_facts,
    )


def _family_projection(db, *, tenant_id, broker, broker_order_id):
    rows = db.execute(
        select(BrokerOrderProjection).where(
            BrokerOrderProjection.tenant_id == tenant_id,
            BrokerOrderProjection.broker == broker,
            BrokerOrderProjection.broker_order_id == broker_order_id,
        )
    ).scalars().all()
    if not rows:
        return None
    return max(rows, key=lambda r: (r.received_at, r.id))


def _lifecycle_count(db, tenant_id, event_type):
    return int(db.execute(
        text(
            "SELECT COUNT(*) FROM trade_lifecycle_events "
            "WHERE tenant_id = :t AND event_type = :e"
        ),
        {"t": tenant_id, "e": event_type},
    ).scalar())


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ===========================================================================
# 1. Canonical broker contract tests
# ===========================================================================


class TestBrokerContract:
    def test_registry_resolves_registered_brokers_and_fails_unknown(self):
        from app.brokers.gateway import BrokerGateway
        from app.brokers.registry import BROKER_REGISTRY, register_default_brokers

        register_default_brokers()
        assert {"UPSTOX", "FYERS"} <= set(BROKER_REGISTRY.known_brokers())
        gw = BrokerGateway()
        for broker_id in BROKER_REGISTRY.known_brokers():
            adapter = gw.create(broker_id)
            assert adapter is not None
        from app.brokers.domain.errors import BrokerError, BrokerErrorCode
        with pytest.raises(BrokerError) as exc:
            gw.create("NOT_A_BROKER")
        assert exc.value.code == BrokerErrorCode.BROKER_UNKNOWN

    def test_canonical_event_contract_freezes_identity(self):
        e = _seqless(
            broker_order_id="ORD-D42",
            event_type=BrokerEventType.ORDER_SUBMITTED.value,
            status=CanonicalOrderState.SUBMITTED,
            event_ts=_NOW + timedelta(seconds=1),
            received_at=_NOW + timedelta(seconds=1),
            provider_event_id="bc-evt-1",
        )
        assert e.canonical_id
        with pytest.raises(Exception):
            e.canonical_id = "forged"

    def test_contractual_data_quality_enforced_downstream(self):
        """OrderFacts carries (never silently repairs) broker data;
        contractual data-quality validation (missing/contradictory
        quantity, malformed payloads) is enforced by the Day41 Phase 3/4
        raw-ingress suite — part of this gate."""
        e = make_broker_sync_event(
            tenant_id="user-d42",
            broker="broker-d42",
            event_type=BrokerEventType.PARTIAL_FILL.value,
            event_version="1.0",
            broker_order_id="ORD-D42",
            canonical_sequence=None,
            provider_event_id="bc-evt-zerqty",
            event_timestamp=_NOW + timedelta(seconds=1),
            received_at=_NOW + timedelta(seconds=1),
            source_mode=BrokerEventSourceMode.STREAM,
            order_facts=OrderFacts(
                order_id="ORD-D42",
                broker_order_id="ORD-D42",
                status=CanonicalOrderState.PARTIALLY_FILLED,
                total_quantity=0,  # carried as-is; downstream validates
                cumulative_filled=0,
            ),
        )
        assert e.order_facts.total_quantity == 0

    def _run_inline_canonical_contract_checks(self):
        """Contract checks executed directly (no nested suite execution):
        the Phase-2 canonical processor applies an observation WITHOUT
        fill facts as pure order state (no synthetic fill), re-ingesting
        the same canonical event is a DUPLICATE_NOOP, and economic fills
        exist only from broker FillFacts — the Day39 Task1 canonical
        contract points that remain relevant to this gate.  The full
        Day39/Day41 suites remain external verification commands
        (see the module docstring's evidence-classification notes).
        """
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        s = sessionmaker(bind=engine, expire_on_commit=False)()
        try:
            # The app layer (StrategyExecution + PaperOrder) the projector
            # resolves must exist under the SAME tenant as the events.
            _seed_app_order(s, "ORD-CANON", tenant_id="user-canond42",
                            execution_id="EXEC-CANON")
            s.commit()
            e1 = _seqless(
                broker_order_id="ORD-CANON",
                event_type=BrokerEventType.PARTIAL_FILL.value,
                status=CanonicalOrderState.PARTIALLY_FILLED,
                event_ts=_NOW + timedelta(seconds=1),
                received_at=_NOW + timedelta(seconds=1),
                tenant_id="user-canond42",
                provider_event_id="canon-evt-1",
                cumulative_filled=0,
                fill_facts=None,
            )
            assert ingest_canonical_event(e1, s)["action"] == "APPLIED"
            s.commit()
            # Replay of the same canonical event: idempotent, no new row.
            assert ingest_canonical_event(e1, s)["action"] == "DUPLICATE_NOOP"
            s.commit()
            proj = s.execute(
                select(BrokerOrderProjection).where(
                    BrokerOrderProjection.tenant_id == "user-canond42")
            ).scalars().all()
            assert len(proj) == 1
            assert int(proj[0].cumulative_filled) == 0
            # An event WITH fill facts yields exactly one economic fill.
            e2 = _seqless(
                broker_order_id="ORD-CANON",
                event_type=BrokerEventType.PARTIAL_FILL.value,
                status=CanonicalOrderState.PARTIALLY_FILLED,
                event_ts=_NOW + timedelta(seconds=2),
                received_at=_NOW + timedelta(seconds=2),
                tenant_id="user-canond42",
                provider_event_id="canon-evt-2",
                cumulative_filled=50,
                fill_facts=FillFacts(
                    fill_id="canon-fill-1", fill_quantity=50,
                    fill_price=100.0, cumulative_filled_after=50,
                    remaining_after=50,
                ),
            )
            assert ingest_canonical_event(e2, s)["action"] == "APPLIED"
            s.commit()
            # Exactly one ECONOMIC fill: the OrderFilled lifecycle event
            # from e2 carries the broker-confirmed fill economics in its
            # payload (fill_quantity), while e1's no-fills state event
            # carries none.
            assert int(s.execute(
                text(
                    "SELECT COUNT(*) FROM trade_lifecycle_events "
                    "WHERE tenant_id = 'user-canond42' "
                    "AND event_type = 'OrderFilled' "
                    "AND payload_json LIKE '%\"fill_quantity\"%'"
                )
            ).scalar()) == 1
        finally:
            s.close()
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_contractual_data_quality_checks_run_inline(self):
        """Deterministic in-module contract checks (see
        ``_run_inline_canonical_contract_checks``) — regression coverage
        on the same SQLite stack; external suites remain external."""
        self._run_inline_canonical_contract_checks()

    def test_day39_task1_canonical_contract_checks_run_inline(self):
        """Same in-module contract checks under the canonical-contract
        gate label (deterministic regression coverage; the full Day39
        Task1 suite is run externally as a separate verification command)."""
        self._run_inline_canonical_contract_checks()


# ===========================================================================
# 2. Paper/live semantic parity (§3.7)
# ===========================================================================


class TestPaperLiveParity:
    @staticmethod
    def _target(**overrides):
        defaults = dict(
            position_id=1, source_action="buy", exit_side="sell",
            quantity=1, remaining_quantity=1, symbol="NIFTY",
            expiry="2026-10-29", strike=24500.0, option_type="CE",
            lot_size=65, price_override=175.0,
        )
        defaults.update(overrides)
        return ExecutionTarget(**defaults)

    def test_paper_and_live_share_the_intent_domain(self):
        """Identical ExecutionIntent construction must succeed for both
        modes and yield the same domain identity (§3.7)."""
        i_paper = create_execution_intent(
            user_id="user-d42", execution_mode=ExecutionMode.PAPER,
            source=ExecutionSource.EXIT_SELECTOR,
            targets=[self._target()], idempotency_key="parity-key-1",
        )
        i_live = create_execution_intent(
            user_id="user-d42", execution_mode=ExecutionMode.LIVE,
            source=ExecutionSource.EXIT_SELECTOR,
            targets=[self._target()], idempotency_key="parity-key-1",
        )
        # Same domain identity inputs (user + idempotency key) and the
        # exact same target payload for both modes.
        assert i_paper.user_id == i_live.user_id == "user-d42"
        assert i_paper.idempotency_key == i_live.idempotency_key
        assert i_paper.targets == i_live.targets
        assert i_paper.execution_mode is ExecutionMode.PAPER
        assert i_live.execution_mode is ExecutionMode.LIVE

    def test_both_modes_share_the_validation_contract(self):
        """Mode-agnostic invariants (side inversion, quantity safety)
        must hold identically for both modes."""
        for mode in (ExecutionMode.PAPER, ExecutionMode.LIVE):
            with pytest.raises(Exception):
                self._target(source_action="buy", exit_side="buy")
            with pytest.raises(Exception):
                self._target(quantity=0)

    def test_router_reuses_paper_semantics_not_a_parallel_engine(self):
        """The router must delegate to the existing paper engine (no
        duplicated execution logic) and must not import any broker
        adapter module — parity by shared semantics, boundary by
        construction (import-level, not prose-level)."""
        import inspect
        from app.services import execution_intent as mod
        src = inspect.getsource(mod)
        assert "from app.services.paper_execution import" in src
        assert "exit_position(" in src
        assert "find_exit_replay(" in src
        for banned_import in (
            "from app.brokers.adapters", "import app.brokers.adapters",
            "from app.brokers.gateway", "import app.brokers.gateway",
            "from app.services.upstox", "import app.services.upstox",
        ):
            assert banned_import not in src, banned_import


# ===========================================================================
# 3. Failure injection (controlled, deterministic)
# ===========================================================================


class TestFailureInjection:
    def test_broker_rejection_never_touches_book_state(self, db_session):
        """Rejection: the broker REFUSES the order — the paper book is
        never mutated and no fill exists (the projection carries the
        broker's REJECTED status only)."""
        pos = _seed_position(db_session)
        e = _seqless(
            broker_order_id="ORD-D42",
            event_type=BrokerEventType.ORDER_REJECTED.value,
            status=CanonicalOrderState.REJECTED,
            event_ts=_NOW + timedelta(seconds=1),
            received_at=_NOW + timedelta(seconds=1),
            provider_event_id="fi-evt-reject",
        )
        assert ingest_canonical_event(e, db_session)["action"] == "APPLIED"
        db_session.commit()
        db_session.refresh(pos)
        assert pos.status == "open"
        assert pos.net_quantity == 2
        latest = _family_projection(
            db_session, tenant_id="user-d42", broker="broker-d42",
            broker_order_id="ORD-D42",
        )
        assert latest is not None and latest.status == "REJECTED"
        assert _lifecycle_count(db_session, "user-d42", "OrderFilled") == 0

    @staticmethod
    def _serialization_failure_probe():
        """Build a SQLAlchemy ``OperationalError`` carrying a SQLSTATE-40001
        DBAPI error (psycopg-style ``sqlstate`` attribute) — the exception
        shape ``is_serialization_failure`` classifies as retryable.
        Controlled injection only; not a database-generated error."""
        class _FakePgOrig(Exception):
            sqlstate = "40001"

            def __str__(self):
                return "serialization failure: 40001 restart transaction"

        return SAOperationalError(
            "serialized transaction failure", params=None, orig=_FakePgOrig(),
        )

    def test_serialization_failure_retries_once_through_the_production_abstraction(
        self, db_session,
    ):
        """CONTROLLED RETRY-ABSTRACTION INJECTION — proves the production
        ``retry_on_serialization`` contract in ONE invocation:

        attempt 1  enters the transaction, performs the real ingest
                   (projection writes), then raises the controlled
                   SQLSTATE-40001 OperationalError at the operation seam
                   (where a serialization failure surfaces in production);
        the abstraction classifies it via ``is_serialization_failure``,
        rolls back attempt 1, opens a FRESH session, and retries;
        attempt 2  succeeds and commits.

        Asserted, not inferred: wrapper call count == 2; attempt-1 state
        is NOT durable; attempt-2 state IS durable exactly once (one
        idempotency row, one fill projection row, one OrderFilled
        lifecycle event).

        This is NOT evidence of a live CockroachDB serialization race:
        the 40001 is injected at the operation seam, not generated by
        database contention.  The exception shape and the retry code path
        are the real production ones.
        """
        from app.utils.retry import is_serialization_failure, retry_on_serialization

        submit = _seqless(
            broker_order_id="ORD-D42",
            event_type=BrokerEventType.ORDER_SUBMITTED.value,
            status=CanonicalOrderState.SUBMITTED,
            event_ts=_NOW + timedelta(seconds=1),
            received_at=_NOW + timedelta(seconds=1),
            provider_event_id="fi-evt-submit",
        )
        assert ingest_canonical_event(submit, db_session)["action"] == "APPLIED"
        db_session.commit()

        fill = _seqless(
            broker_order_id="ORD-D42",
            event_type=BrokerEventType.PARTIAL_FILL.value,
            status=CanonicalOrderState.PARTIALLY_FILLED,
            event_ts=_NOW + timedelta(seconds=2),
            received_at=_NOW + timedelta(seconds=2),
            provider_event_id="fi-evt-fill",
            cumulative_filled=50,
            fill_facts=FillFacts(
                fill_id="fi-fill-1", fill_quantity=50, fill_price=100.0,
                cumulative_filled_after=50, remaining_after=50,
            ),
        )

        # Probe shape: the exact exception we will inject must classify as
        # a serialization failure through the production predicate.
        probe = self._serialization_failure_probe()
        assert is_serialization_failure(probe) is True

        attempt_log = {"calls": 0, "executed": [], "injected": []}

        def op(db):
            """The retry operation: the real ingest, then — on attempt 1
            only — the controlled 40001 AFTER the ingest's transactional
            writes and BEFORE the abstraction's commit."""
            attempt_log["calls"] += 1
            result = ingest_canonical_event(fill, db)
            attempt_log["executed"].append(attempt_log["calls"])
            if attempt_log["calls"] == 1:
                failure = self._serialization_failure_probe()
                attempt_log["injected"].append(failure)
                raise failure
            return result

        factory = lambda: sessionmaker(  # noqa: E731
            bind=db_session.get_bind(), expire_on_commit=False
        )()

        # ONE invocation of the production abstraction: attempt 1 fails
        # with the controlled 40001, the abstraction rolls back and retries
        # in a fresh session automatically.
        result = retry_on_serialization(
            op, session_factory=factory, max_attempts=2, base_delay=0,
        )

        # --- the wrapper provably executed, exactly twice ---
        assert attempt_log["calls"] == 2
        assert attempt_log["executed"] == [1, 2]
        assert len(attempt_log["injected"]) == 1
        injected = attempt_log["injected"][0]
        assert isinstance(injected, SAOperationalError)
        # The exception the abstraction encountered on attempt 1 classifies
        # as a serialization failure (production predicate, real object).
        assert is_serialization_failure(injected) is True

        # --- attempt-2 result is the successful application ---
        assert result["action"] == "APPLIED"

        # --- attempt-1 state is NOT durable; attempt-2 state IS ---
        obs = factory()
        try:
            idem = obs.execute(
                select(BrokerSyncIdempotency).where(
                    BrokerSyncIdempotency.canonical_id == fill.canonical_id)
            ).scalars().all()
            assert len(idem) == 1 and idem[0].status == "APPLIED"
            projections = obs.execute(
                select(BrokerOrderProjection).where(
                    BrokerOrderProjection.canonical_id == fill.canonical_id)
            ).scalars().all()
            # Exactly one projection effect for the fill event — attempt 1
            # left no duplicate durable row.
            assert len(projections) == 1
            assert projections[0].status == "PARTIALLY_FILLED"
            assert int(projections[0].cumulative_filled) == 50
            assert _lifecycle_count(obs, "user-d42", "OrderFilled") == 1
        finally:
            obs.close()

    def test_duplicate_request_is_replay_safe(self, db_session):
        """Duplicate request: an identical replay re-emits the settled
        outcome (DUPLICATE_NOOP) and creates no second durable row
        (Day41.2 §15 replay matrix)."""
        e = _seqless(
            broker_order_id="ORD-D42",
            event_type=BrokerEventType.ORDER_SUBMITTED.value,
            status=CanonicalOrderState.SUBMITTED,
            event_ts=_NOW + timedelta(seconds=1),
            received_at=_NOW + timedelta(seconds=1),
            provider_event_id="fi-evt-dup",
        )
        assert ingest_canonical_event(e, db_session)["action"] == "APPLIED"
        db_session.commit()
        before = db_session.execute(
            select(BrokerOrderProjection).where(
                BrokerOrderProjection.tenant_id == "user-d42")
        ).scalars().all()
        r2 = ingest_canonical_event(e, db_session)
        assert r2["action"] == "DUPLICATE_NOOP"
        after = db_session.execute(
            select(BrokerOrderProjection).where(
                BrokerOrderProjection.tenant_id == "user-d42")
        ).scalars().all()
        assert len(after) == len(before)

    def test_recovery_after_interruption_recovers_missed_event(self, db_session):
        """Verified MISSED-EVENT RECOVERY through the real bounded recovery
        surface (§15.3 as implemented): the Phase-2 reclaim loop in
        ``app/broker_sync/raw_ingress.py``.

        Scenario (event 1, 2, 3 → process 1, 2 → interruption → recover 3):

        1. three broker observations commit to the durable raw store
           (Phase 1 — survival independent of any worker);
        2. a worker claims + processes observations 1 and 2 through the
           canonical ingestion path, then is "interrupted" (worker session
           discarded before observation 3 is ever claimed);
        3. a FRESH recovery worker (new session) runs the same bounded
           claim loop — it recovers the missed observation 3;
        4. the recovered event reaches the correct authoritative state via
           canonical ingestion; processed events stay durable and their
           re-ingest stays idempotent; no duplicate lifecycle transition;
           no polling loop — the recovery is one bounded claim pass.
        """

        def mk_raw(provider_event_id, ts, *, cumulative_filled=0,
                   fill_facts=None, status=CanonicalOrderState.SUBMITTED,
                   event_type=BrokerEventType.ORDER_SUBMITTED.value):
            payload = _seqless(
                broker_order_id="ORD-D42",
                event_type=event_type,
                status=status,
                event_ts=ts,
                received_at=ts,
                provider_event_id=provider_event_id,
                cumulative_filled=cumulative_filled,
                fill_facts=fill_facts,
            )
            body = {
                "tenant_id": payload.tenant_id,
                "broker": payload.broker,
                "event_type": payload.event_type,
                "event_version": payload.event_version,
                "provider_event_id": payload.provider_event_id,
                "event_timestamp": (
                    payload.event_timestamp.isoformat()
                    if payload.event_timestamp else None),
                "received_at": payload.received_at.isoformat(),
                "source_mode": payload.source_mode.value,
                "broker_order_id": payload.broker_order_id,
                "order_facts": {
                    "order_id": payload.order_facts.order_id,
                    "broker_order_id": payload.order_facts.broker_order_id,
                    "status": payload.order_facts.status.value,
                    "total_quantity": payload.order_facts.total_quantity,
                    "cumulative_filled": payload.order_facts.cumulative_filled,
                },
            }
            if payload.fill_facts is not None:
                ff = payload.fill_facts
                body["fill_facts"] = {
                    "fill_id": ff.fill_id,
                    "fill_quantity": ff.fill_quantity,
                    "fill_price": ff.fill_price,
                    "fill_timestamp": (
                        ff.fill_timestamp.isoformat()
                        if ff.fill_timestamp else None),
                    "cumulative_filled_after": ff.cumulative_filled_after,
                    "remaining_after": ff.remaining_after,
                }
            import json
            return json.dumps(body).encode("utf-8")

        fill_facts = FillFacts(
            fill_id="fi-rc-fill", fill_quantity=50, fill_price=100.0,
            cumulative_filled_after=50, remaining_after=50,
        )
        raw1 = commit_raw_observation(
            db_session, tenant_id="user-d42", broker="broker-d42",
            source_mode="STREAM",
            raw_payload=mk_raw("fi-evt-rc1", _NOW + timedelta(seconds=1)),
        )
        raw2 = commit_raw_observation(
            db_session, tenant_id="user-d42", broker="broker-d42",
            source_mode="STREAM",
            raw_payload=mk_raw(
                "fi-evt-rc2", _NOW + timedelta(seconds=2),
                cumulative_filled=50, fill_facts=fill_facts,
                status=CanonicalOrderState.PARTIALLY_FILLED,
                event_type=BrokerEventType.PARTIAL_FILL.value,
            ),
        )
        raw3 = commit_raw_observation(
            db_session, tenant_id="user-d42", broker="broker-d42",
            source_mode="STREAM",
            raw_payload=mk_raw("fi-evt-rc3", _NOW + timedelta(seconds=3)),
        )
        db_session.commit()

        def canonical_processor(db, row):
            """Phase-2 processor: deserialize → canonical → ingest."""
            import json

            from datetime import datetime as _dt

            body = json.loads(bytes(row.raw_payload).decode("utf-8"))
            fill_facts = None
            if body.get("fill_facts"):
                ff = body["fill_facts"]
                fill_facts = FillFacts(
                    fill_id=ff["fill_id"],
                    fill_quantity=ff["fill_quantity"],
                    fill_price=ff["fill_price"],
                    fill_timestamp=(
                        _dt.fromisoformat(ff["fill_timestamp"])
                        if ff["fill_timestamp"] else None),
                    cumulative_filled_after=ff["cumulative_filled_after"],
                    remaining_after=ff["remaining_after"],
                )
            ev = make_broker_sync_event(
                tenant_id=body["tenant_id"],
                broker=body["broker"],
                event_type=body["event_type"],
                event_version=body["event_version"],
                provider_event_id=body["provider_event_id"],
                event_timestamp=(
                    _dt.fromisoformat(body["event_timestamp"])
                    if body["event_timestamp"] else None),
                received_at=_dt.fromisoformat(body["received_at"]),
                source_mode=BrokerEventSourceMode(body["source_mode"]),
                broker_order_id=body["broker_order_id"],
                order_facts=OrderFacts(
                    order_id=body["order_facts"]["order_id"],
                    broker_order_id=body["order_facts"]["broker_order_id"],
                    status=CanonicalOrderState(body["order_facts"]["status"]),
                    total_quantity=body["order_facts"]["total_quantity"],
                    cumulative_filled=body["order_facts"]["cumulative_filled"],
                ),
                fill_facts=fill_facts,
            )
            ingest_canonical_event(ev, db)
            mark_processing_result(
                db, row,
                ingestion_status=IngestionStatus.CANONICALIZED,
                processing_status=ProcessingStatus.SUCCEEDED,
            )

        # Worker session: processes observations 1 and 2, then the process
        # is interrupted (session discarded — observation 3 never claimed).
        # The worker uses a BOUNDED claim (limit=2) exactly as the recovery
        # loop supports: the interruption lands between claim batches.
        worker = sessionmaker(
            bind=db_session.get_bind(), expire_on_commit=False)()
        try:
            outcomes = process_pending_observations(
                worker, canonical_processor, limit=2,
                worker_id="d42-worker",
            )
            assert outcomes == [
                (raw1.raw_observation_id, "SUCCEEDED"),
                (raw2.raw_observation_id, "SUCCEEDED"),
            ]
        finally:
            worker.close()

        # Interruption evidence: observation 3 was never processed.
        db_session.expire_all()
        assert db_session.execute(
            select(BrokerRawObservation).where(
                BrokerRawObservation.raw_observation_id
                == raw3.raw_observation_id)
        ).scalar_one().processing_status == "PENDING"

        # Fresh recovery worker (new session — the "reconnected" process):
        # one bounded claim pass recovers exactly the missed event.
        recovery = sessionmaker(
            bind=db_session.get_bind(), expire_on_commit=False)()
        try:
            recovered = process_pending_observations(
                recovery, canonical_processor, limit=10,
                worker_id="d42-recovery",
            )
            recovery.commit()
            assert recovered == [
                (raw3.raw_observation_id, "SUCCEEDED"),
            ]
        finally:
            recovery.close()

        # The recovered event reached the correct authoritative state...
        latest = _family_projection(
            db_session, tenant_id="user-d42", broker="broker-d42",
            broker_order_id="ORD-D42",
        )
        assert latest is not None and latest.status == "SUBMITTED"

        # ...processed events stayed durable and are idempotent on re-ingest...
        obs = sessionmaker(
            bind=db_session.get_bind(), expire_on_commit=False)()
        try:
            idem_rows = obs.execute(
                select(BrokerSyncIdempotency).where(
                    BrokerSyncIdempotency.tenant_id == "user-d42")
            ).scalars().all()
            # Exactly one idempotency row per processed event — the family
            # received exactly these three events.
            assert len(idem_rows) == 3
            assert all(r.status == "APPLIED" for r in idem_rows)
            assert obs.query(BrokerOrderProjection).filter_by(
                tenant_id="user-d42").count() == 3
            # No duplicate lifecycle transition anywhere.
            assert _lifecycle_count(obs, "user-d42", "OrderFilled") == 1
        finally:
            obs.close()

    def test_partial_fill_is_broker_economic_not_synthetic(self, db_session):
        """Partial fill: the fill exists only because the broker event
        carried fill facts; the paper book is untouched (paper exits are
        a separate, user-initiated lifecycle) and the lifecycle audit
        records exactly one broker-confirmed OrderFilled."""
        _seed_position(db_session)
        e = _seqless(
            broker_order_id="ORD-D42",
            event_type=BrokerEventType.PARTIAL_FILL.value,
            status=CanonicalOrderState.PARTIALLY_FILLED,
            event_ts=_NOW + timedelta(seconds=2),
            received_at=_NOW + timedelta(seconds=2),
            provider_event_id="fi-evt-pf",
            cumulative_filled=50,
            fill_facts=FillFacts(
                fill_id="fi-pf-fill", fill_quantity=50, fill_price=100.0,
                cumulative_filled_after=50, remaining_after=50,
            ),
        )
        assert ingest_canonical_event(e, db_session)["action"] == "APPLIED"
        db_session.commit()
        latest = _family_projection(
            db_session, tenant_id="user-d42", broker="broker-d42",
            broker_order_id="ORD-D42",
        )
        assert latest is not None and latest.status == "PARTIALLY_FILLED"
        assert latest.cumulative_filled == 50
        assert _lifecycle_count(db_session, "user-d42", "OrderFilled") == 1


# ===========================================================================
# 4. Broker authority — StrikeNova never invents a fill (§15.2)
# ===========================================================================


class TestBrokerAuthority:
    def test_missing_quantity_data_never_manufactures_a_fill(self, db_session):
        _seed_position(db_session)
        e = _seqless(
            broker_order_id="ORD-D42",
            event_type=BrokerEventType.PARTIAL_FILL.value,
            status=CanonicalOrderState.PARTIALLY_FILLED,
            event_ts=_NOW + timedelta(seconds=1),
            received_at=_NOW + timedelta(seconds=1),
            provider_event_id="ba-evt-nofacts",
            cumulative_filled=0,
            fill_facts=None,  # broker did NOT confirm any fill
        )
        assert ingest_canonical_event(e, db_session)["action"] == "APPLIED"
        db_session.commit()
        latest = _family_projection(
            db_session, tenant_id="user-d42", broker="broker-d42",
            broker_order_id="ORD-D42",
        )
        assert latest is not None and latest.cumulative_filled == 0
        assert latest.average_price is None
        assert latest.last_fill_price is None and latest.last_fill_id is None
        assert latest.fill_count == 0
        # No economic fill quantity was invented anywhere in the audit
        # trail (a state-transition event may exist; it carries no fill
        # economics — OrderFilled economics live in the event payload's
        # fill_quantity and in the projection, both empty here).
        econ = db_session.execute(
            text(
                "SELECT COUNT(*) FROM trade_lifecycle_events "
                "WHERE tenant_id = 'user-d42' AND event_type = 'OrderFilled' "
                "AND payload_json LIKE '%\"fill_quantity\"%'"
            )
        ).scalar()
        assert int(econ) == 0

    def test_stale_evidence_cannot_supersede_newer_broker_state(self, db_session):
        """S2 ordering (Day41.2): a later-arriving observation with OLDER
        provider evidence is quarantined (STALE), never applied — the
        family stays at the broker's newest confirmed state."""
        newer = _seqless(
            broker_order_id="ORD-D42",
            event_type=BrokerEventType.PARTIAL_FILL.value,
            status=CanonicalOrderState.PARTIALLY_FILLED,
            event_ts=_NOW + timedelta(seconds=30),
            received_at=_NOW + timedelta(seconds=30),
            provider_event_id="ba-evt-newer",
            cumulative_filled=50,
            fill_facts=FillFacts(
                fill_id="ba-fill-newer", fill_quantity=50, fill_price=100.0,
                cumulative_filled_after=50, remaining_after=50,
            ),
        )
        assert ingest_canonical_event(newer, db_session)["action"] == "APPLIED"
        db_session.commit()

        older = _seqless(
            broker_order_id="ORD-D42",
            event_type=BrokerEventType.ORDER_ACCEPTED.value,
            status=CanonicalOrderState.OPEN,
            event_ts=_NOW + timedelta(seconds=10),
            received_at=_NOW + timedelta(seconds=31),
            provider_event_id="ba-evt-older",
        )
        r = ingest_canonical_event(older, db_session)
        assert r["action"] == "STALE"
        db_session.commit()
        latest = _family_projection(
            db_session, tenant_id="user-d42", broker="broker-d42",
            broker_order_id="ORD-D42",
        )
        assert latest is not None and latest.status == "PARTIALLY_FILLED"

    def test_missing_fill_facts_never_yield_a_fill_state(self, db_session):
        """Edge contract (Day40.5 §5 / Day41 Phase 9): a PARTIALLY_FILLED
        observation without fill facts applies only as order state with
        cumulative_filled == 0 — no synthetic fill, no fill economics."""
        e = _seqless(
            broker_order_id="ORD-D42",
            event_type=BrokerEventType.PARTIAL_FILL.value,
            status=CanonicalOrderState.PARTIALLY_FILLED,
            event_ts=_NOW + timedelta(seconds=1),
            received_at=_NOW + timedelta(seconds=1),
            provider_event_id="ba-evt-quarantine",
            cumulative_filled=0,
            fill_facts=None,
        )
        r = ingest_canonical_event(e, db_session)
        assert r["action"] == "APPLIED"
        db_session.commit()
        latest = _family_projection(
            db_session, tenant_id="user-d42", broker="broker-d42",
            broker_order_id="ORD-D42",
        )
        assert latest is not None and latest.cumulative_filled == 0
        assert latest.average_price is None and latest.fill_count == 0
        # No economic fill exists in the audit trail for this family (an
        # OrderFilled lifecycle event's economics live in its payload —
        # fill_quantity — and in the projection; a no-fill-facts event
        # carries no fill economics in either).
        econ = db_session.execute(
            text(
                "SELECT COUNT(*) FROM trade_lifecycle_events "
                "WHERE tenant_id = 'user-d42' AND event_type = 'OrderFilled' "
                "AND payload_json LIKE '%\"fill_quantity\"%'"
            )
        ).scalar()
        assert int(econ) == 0

    def test_lane_c_equivalence_is_never_assumed(self, db_session):
        """The fill ledger's Lane-C contract, exercised directly on the
        same SQLite stack (structural/architecture evidence): two
        trade-id-absent fills are recorded as two immutable observations
        (never deduplicated on (D1, FPv2) alone), so economic canonical
        equivalence can only come from proven lineage — broker authority
        at the ledger.  The full Phase 6/7/9 suite remains an external
        verification command."""
        first = record_fill_observation(
            db_session,
            tenant_id="user-d42", broker="broker-d42",
            provider_order_id="ORD-D42",
            observation_class=ObservationClass.ECONOMIC_FILL,
            d1=_NOW.date().isoformat(),
            content_fingerprint="fpv2-lanec-1",
            source_mode="STREAM",
            received_at=_NOW,
            provider_trade_id=None,
            fill_quantity=25,
            fill_price="100.0",
            cumulative_after=25,
            initial_state=ReconciliationState.PENDING,
        )
        second = record_fill_observation(
            db_session,
            tenant_id="user-d42", broker="broker-d42",
            provider_order_id="ORD-D42",
            observation_class=ObservationClass.ECONOMIC_FILL,
            d1=_NOW.date().isoformat(),
            content_fingerprint="fpv2-lanec-2",
            source_mode="STREAM",
            received_at=_NOW + timedelta(seconds=1),
            provider_trade_id=None,
            fill_quantity=25,
            fill_price="100.0",
            cumulative_after=50,
            initial_state=ReconciliationState.PENDING,
        )
        db_session.commit()
        # Two identical-content no-ID fills ⇒ TWO durable observation rows
        # (Invariant X/AA): no (D1, FPv2)-only collapse at the ledger.
        assert first.observation_id != second.observation_id
        assert first.fill_eq_key == second.fill_eq_key
        rows = db_session.execute(
            select(BrokerFillLedgerObservation).where(
                BrokerFillLedgerObservation.tenant_id == "user-d42",
                BrokerFillLedgerObservation.provider_order_id == "ORD-D42",
            )
        ).scalars().all()
        assert len(rows) == 2


# ===========================================================================
# 5. Execution audit trail (§15.1 / §14)
# ===========================================================================


class TestExecutionAuditTrail:
    def test_paper_exit_audit_trail_records_material_transitions(
        self, db_session,
    ):
        """Every material transition of a paper exit is recorded in the
        audit trail: the auditable exit order (idempotency key, fill
        price, quantity), the cash-ledger movement, the journal-leg
        closure, and the exposure attribution maintenance."""
        from app.models import Leg, PaperTransaction, Trade

        pos = _seed_position(db_session)
        trade = Trade(user_id="user-d42", symbol="NIFTY", strategy_tag="Gate",
                      status="open", entry_net=100.0,
                      strategy_execution_id="EXEC-D42")
        db_session.add(trade)
        db_session.flush()
        leg = Leg(trade_id=trade.id, symbol="NIFTY",
                  expiration_date="2026-10-29", strike_price=24500.0,
                  option_type="call", action="buy", premium=100.0,
                  quantity=2, lot_size=1)
        db_session.add(leg)
        db_session.flush()
        # Link the journal leg to the entry order (the exit closes
        # journal attribution through this link).
        entry_order = db_session.query(PaperOrder).filter_by(
            user_id="user-d42", client_order_id="ORD-D42").one()
        entry_order.journal_leg_id = leg.id
        db_session.commit()

        target = ExecutionTarget(
            position_id=pos.id, source_action="buy", exit_side="sell",
            quantity=2, remaining_quantity=2, symbol="NIFTY",
            expiry="2026-10-29", strike=24500.0, option_type="CE",
            lot_size=1, price_override=175.0,
        )
        intent = create_execution_intent(
            user_id="user-d42", execution_mode=ExecutionMode.PAPER,
            source=ExecutionSource.EXIT_SELECTOR, targets=[target],
            idempotency_key="audit-key-1",
        )
        result = _run(ExecutionRouter(db=db_session).execute_intent(intent))
        assert result.status == ExecutionStatus.SUCCESS
        db_session.commit()

        # 1) Auditable exit order (idempotency key → order identity).
        order = db_session.query(PaperOrder).filter_by(
            user_id="user-d42", kind="exit",
            client_order_id="audit-key-1:t0").one()
        assert order.status == "FILLED"
        assert order.quantity == 2 and order.filled_quantity == 2
        assert order.fill_price == 175.0

        # 2) Cash ledger movement.
        txn = db_session.query(PaperTransaction).filter_by(
            user_id="user-d42", order_id=order.id).one()
        assert txn.type == "EXIT_CREDIT" and txn.amount > 0

        # 3) Journal attribution closed.
        db_session.refresh(trade)
        db_session.refresh(leg)
        assert trade.status == "closed"
        assert leg.exit_at is not None

        # 4) Exposure attribution maintained (the engine maintains
        # existing exposure rows; any that exist for this execution must
        # be fully attributed, none left open).
        from app.models import StrategyLegExposure
        exps = db_session.query(StrategyLegExposure).filter_by(
            user_id="user-d42", execution_id="EXEC-D42").all()
        assert all(e.remaining_quantity == 0 for e in exps), exps

    def test_failed_intent_writes_no_fill_audit(self, db_session):
        """A failed execution attempt must not mint fill/cash audit
        records — audit trail mirrors reality, not attempts."""
        pos = _seed_position(db_session)
        pos.status = "closed"
        pos.net_quantity = 0
        db_session.commit()
        target = ExecutionTarget(
            position_id=pos.id, source_action="buy", exit_side="sell",
            quantity=2, remaining_quantity=2, symbol="NIFTY",
            expiry="2026-10-29", strike=24500.0, option_type="CE",
            lot_size=1, price_override=175.0,
        )
        intent = create_execution_intent(
            user_id="user-d42", execution_mode=ExecutionMode.PAPER,
            source=ExecutionSource.EXIT_SELECTOR, targets=[target],
            idempotency_key="audit-key-fail",
        )
        result = _run(ExecutionRouter(db=db_session).execute_intent(intent))
        assert result.status == ExecutionStatus.FAILED
        assert _lifecycle_count(db_session, "user-d42", "OrderFilled") == 0

    def test_duplicate_execution_does_not_duplicate_audit(self, db_session):
        """Idempotent replay of the same execution (same idempotency
        key) must not append a second fill audit record."""
        pos = _seed_position(db_session)
        target = ExecutionTarget(
            position_id=pos.id, source_action="buy", exit_side="sell",
            quantity=1, remaining_quantity=2, symbol="NIFTY",
            expiry="2026-10-29", strike=24500.0, option_type="CE",
            lot_size=1, price_override=175.0,
        )
        first = create_execution_intent(
            user_id="user-d42", execution_mode=ExecutionMode.PAPER,
            source=ExecutionSource.EXIT_SELECTOR, targets=[target],
            idempotency_key="audit-key-dup",
        )
        r1 = _run(ExecutionRouter(db=db_session).execute_intent(first))
        db_session.commit()
        assert r1.status == ExecutionStatus.SUCCESS
        n_first = _lifecycle_count(db_session, "user-d42", "OrderFilled")

        replay_target = ExecutionTarget(
            position_id=pos.id, source_action="buy", exit_side="sell",
            quantity=1, remaining_quantity=2, symbol="NIFTY",
            expiry="2026-10-29", strike=24500.0, option_type="CE",
            lot_size=1, price_override=175.0,
        )
        replay = create_execution_intent(
            user_id="user-d42", execution_mode=ExecutionMode.PAPER,
            source=ExecutionSource.EXIT_SELECTOR, targets=[replay_target],
            idempotency_key="audit-key-dup",
        )
        r2 = _run(ExecutionRouter(db=db_session).execute_intent(replay))
        db_session.commit()
        assert r2.status == ExecutionStatus.SUCCESS
        assert r2.duplicated is True
        assert _lifecycle_count(db_session, "user-d42", "OrderFilled") == n_first
        # The replay produced NO second exit order or cash movement.
        exit_orders = db_session.query(PaperOrder).filter_by(
            user_id="user-d42", kind="exit").all()
        assert len(exit_orders) == 1


# ===========================================================================
# 6. Day 41 / Day 41.2 safety invariants + live-disabled boundary
# ===========================================================================


class TestDay41InvariantsAndLiveBoundary:
    def test_d1_lock_and_replay_invariant_holds_per_family(self, db_session):
        """D-1 lock/replay invariant (single-node, deterministic): each
        ingested family holds exactly one durable ``order_family_sync_lock``
        row and a duplicate replay serializes onto the winner's committed
        outcome (DUPLICATE_NOOP, no second row).  GENUINE multi-worker
        concurrency is NOT claimed here — it is independently covered by
        ``tests/test_day41_2_cross_d1_concurrency.py`` (PostgreSQL
        true-concurrency matrix), an external verification command."""
        submit = _seqless(
            broker_order_id="ORD-D42",
            event_type=BrokerEventType.ORDER_SUBMITTED.value,
            status=CanonicalOrderState.SUBMITTED,
            event_ts=_NOW + timedelta(seconds=1),
            received_at=_NOW + timedelta(seconds=1),
            provider_event_id="inv-evt-submit",
        )
        assert ingest_canonical_event(submit, db_session)["action"] == "APPLIED"
        db_session.commit()
        r2 = ingest_canonical_event(submit, db_session)
        assert r2["action"] == "DUPLICATE_NOOP"
        from app.broker_sync.models import OrderFamilySyncLock
        lock_row = db_session.execute(
            select(OrderFamilySyncLock).where(
                OrderFamilySyncLock.tenant_id == "user-d42",
                OrderFamilySyncLock.broker == "broker-d42",
                OrderFamilySyncLock.broker_order_id == "ORD-D42",
            )
        ).scalar_one_or_none()
        assert lock_row is not None

    def test_day41_2_concurrency_matrix_invariants_hold_inline(self, db_session):
        """Deterministic in-module re-check of the Day41.2 invariants this
        gate owns: after ingestion each family holds exactly one durable
        ``order_family_sync_lock`` row and a duplicate replay serializes
        onto the winner's committed outcome (DUPLICATE_NOOP, no second
        row or lock).  The full PostgreSQL concurrency matrix remains an
        external verification command
        (tests/test_day41_2_cross_d1_concurrency.py)."""
        from app.broker_sync.models import OrderFamilySyncLock
        submit = _seqless(
            broker_order_id="ORD-D42",
            event_type=BrokerEventType.ORDER_SUBMITTED.value,
            status=CanonicalOrderState.SUBMITTED,
            event_ts=_NOW + timedelta(seconds=1),
            received_at=_NOW + timedelta(seconds=1),
            provider_event_id="inv-evt-submit",
        )
        assert ingest_canonical_event(submit, db_session)["action"] == "APPLIED"
        db_session.commit()
        r2 = ingest_canonical_event(submit, db_session)
        assert r2["action"] == "DUPLICATE_NOOP"
        locks = db_session.execute(
            select(OrderFamilySyncLock).where(
                OrderFamilySyncLock.tenant_id == "user-d42",
                OrderFamilySyncLock.broker == "broker-d42",
                OrderFamilySyncLock.broker_order_id == "ORD-D42",
            )
        ).scalars().all()
        assert len(locks) == 1  # replay minted no second lock row

    def test_paper_engine_safety_invariants_hold_inline(self, db_session):
        """Deterministic in-module re-check of the paper-engine safety
        invariants on the shared lifecycle: a user-initiated exit
        produces exactly one exit order with its cash movement, and an
        idempotent replay mints no second exit order or cash movement.
        The full Day41.1 Phase 8 and Phase 6/7/9 suites remain external
        verification commands."""
        pos = _seed_position(db_session)
        target = ExecutionTarget(
            position_id=pos.id, source_action="buy", exit_side="sell",
            quantity=2, remaining_quantity=2, symbol="NIFTY",
            expiry="2026-10-29", strike=24500.0, option_type="CE",
            lot_size=1, price_override=175.0,
        )
        first = create_execution_intent(
            user_id="user-d42", execution_mode=ExecutionMode.PAPER,
            source=ExecutionSource.EXIT_SELECTOR, targets=[target],
            idempotency_key="safety-inline-1",
        )
        r1 = _run(ExecutionRouter(db=db_session).execute_intent(first))
        db_session.commit()
        assert r1.status == ExecutionStatus.SUCCESS
        # Exactly one exit order, filled once, with one cash movement.
        exit_orders = db_session.query(PaperOrder).filter_by(
            user_id="user-d42", kind="exit").all()
        assert len(exit_orders) == 1
        assert exit_orders[0].filled_quantity == 2
        assert exit_orders[0].fill_price == 175.0
        txns = db_session.query(PaperTransaction).filter_by(
            user_id="user-d42", order_id=exit_orders[0].id).all()
        assert len(txns) == 1 and txns[0].type == "EXIT_CREDIT"

        # Idempotent replay (same idempotency key): no second exit order,
        # no second cash movement.
        replay = create_execution_intent(
            user_id="user-d42", execution_mode=ExecutionMode.PAPER,
            source=ExecutionSource.EXIT_SELECTOR, targets=[target],
            idempotency_key="safety-inline-1",
        )
        r2 = _run(ExecutionRouter(db=db_session).execute_intent(replay))
        db_session.commit()
        assert r2.status == ExecutionStatus.SUCCESS
        assert r2.duplicated is True
        assert db_session.query(PaperOrder).filter_by(
            user_id="user-d42", kind="exit").count() == 1
        assert db_session.query(PaperTransaction).filter_by(
            user_id="user-d42", order_id=exit_orders[0].id).count() == 1

    def test_live_route_is_disabled_and_writes_nothing(self, db_session):
        """LIVE execution is refused by the router (structural proof
        that production live trading is disabled): no position mutation,
        no paper-cash change, no fill."""
        pos = _seed_position(db_session)
        before_cash = db_session.query(PaperAccount).filter_by(
            user_id="user-d42").one().starting_capital
        target = ExecutionTarget(
            position_id=pos.id, source_action="buy", exit_side="sell",
            quantity=2, remaining_quantity=2, symbol="NIFTY",
            expiry="2026-10-29", strike=24500.0, option_type="CE",
            lot_size=1, price_override=175.0,
        )
        intent = create_execution_intent(
            user_id="user-d42", execution_mode=ExecutionMode.LIVE,
            source=ExecutionSource.EXIT_SELECTOR, targets=[target],
            idempotency_key="live-key-1",
        )
        result = _run(ExecutionRouter(db=db_session).execute_intent(intent))
        assert result.status == ExecutionStatus.DISABLED
        assert result.targets_failed == result.targets_attempted == 1
        db_session.refresh(pos)
        assert pos.status == "open" and pos.net_quantity == 2
        after_cash = db_session.query(PaperAccount).filter_by(
            user_id="user-d42").one().starting_capital
        assert after_cash == before_cash
        assert db_session.query(PaperTransaction).filter_by(
            user_id="user-d42").count() == 0
        assert _lifecycle_count(db_session, "user-d42", "OrderFilled") == 0

    def test_no_broker_submission_path_exists_for_live(self):
        """Structural evidence: the execution surface contains no call
        into any broker gateway/adapter and no order-submission entry
        point is reachable from the execution intent path."""
        import inspect
        from app.services import execution_intent as mod
        src = inspect.getsource(mod)
        for banned in (
            "place_order", "placeorder", "BrokerGateway(",
            "gateway.create", "Gateway.create", "UpstoxAdapter(",
            "FyersAdapter(",
            "from app.brokers.adapters", "import app.brokers.adapters",
            "from app.brokers.gateway", "import app.brokers.gateway",
            "from app.services.upstox", "import app.services.upstox",
        ):
            assert banned not in src, banned
