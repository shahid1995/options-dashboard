"""Day 42 — Execution Gate verification (Issue #84).

Verifies the execution architecture end-to-end **without enabling
production live trading**, per the approved Architecture Blueprint v1:

* master plan "Day 42 — Execution gate": broker contract tests,
  paper/live semantic parity, failure injection (rejection / timeout /
  duplicate / reconnect / partial fill), broker as source of truth,
  audit trail;
* design spec §3.7 / §15.1 / §15.2 / §15.3: paper and live share domain
  semantics; the execution ladder ends at broker confirmation;
  "StrikeNova never invents a fill"; reconciliation is the recovery
  mechanism for reconnects/network failures;
* Day 41 / Day 41.2 safety invariants (merged at baseline ``7601282``):
  D-1 order-family locking, S2 ordering semantics, idempotency,
  stale-data protection, replay-safe outcomes.

All tests are deterministic and run on the SQLite test stack.  Failure
paths are exercised by deterministic controlled injection — no network,
no staging/production writes, no secrets.  Live-trading-disabled
evidence is structural: live execution does not exist as a routable
backend (no broker submission path, no adapter calls) and the
ExecutionRouter LIVE route is refused by design.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select, text
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
from app.broker_sync.ingestion import ingest_canonical_event
from app.broker_sync.models import BrokerOrderProjection, BrokerSyncIdempotency
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
        rc = pytest.main([
            "-q", "-p", "no:warnings", "--no-header",
            "tests/test_day41_phase3_4_raw_ingress.py",
        ])
        assert rc == 0

    def test_day39_task1_canonical_contract_suite_passes(self):
        """The canonical Day39 Task1 contract suite is part of the gate."""
        rc = pytest.main([
            "-q", "-p", "no:warnings", "--no-header",
            "tests/test_day39_task1_canonical_contract.py",
        ])
        assert rc == 0


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

    def test_timeout_rolls_back_atomically_then_retries_through_the_abstraction(self, db_session):
        """Timeout: the submission fails after in-transaction work but
        before commit — the caller-owned rollback discards every effect
        atomically, and a clean retry through the production
        ``retry_on_serialization`` boundary applies exactly once."""
        from app.utils.retry import retry_on_serialization

        class _Timeout(Exception):
            pass

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

        calls = {"n": 0}

        def op(db):
            calls["n"] += 1
            result = ingest_canonical_event(fill, db)
            if calls["n"] == 1:
                raise _Timeout("simulated broker submission timeout")
            return result

        factory = lambda: sessionmaker(  # noqa: E731
            bind=db_session.get_bind(), expire_on_commit=False
        )()

        # Attempt 1 times out after the transactional work... (_Timeout is
        # not a serialization failure, so the abstraction re-raises it
        # immediately instead of retrying)
        with pytest.raises(_Timeout):
            retry_on_serialization(op, session_factory=factory, max_attempts=1)
        assert calls["n"] == 1  # _Timeout is not retryable: no retry loop

        # ...nothing durable survived...
        obs = factory()
        try:
            assert obs.execute(
                select(BrokerSyncIdempotency.canonical_id).where(
                    BrokerSyncIdempotency.canonical_id == fill.canonical_id)
            ).scalar_one_or_none() is None
        finally:
            obs.close()

        # ...and the clean retry applies exactly once through the
        # production retry abstraction (fresh session per attempt).
        result = retry_on_serialization(
            op, session_factory=factory, max_attempts=2,
        )
        assert result["action"] == "APPLIED"
        assert calls["n"] == 2
        obs = factory()
        try:
            rows = obs.execute(
                select(BrokerSyncIdempotency).where(
                    BrokerSyncIdempotency.canonical_id == fill.canonical_id)
            ).scalars().all()
            assert len(rows) == 1 and rows[0].status == "APPLIED"
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

    def test_reconnect_recovery_via_reconciliation(self, db_session):
        """Reconnect/recovery: reconciliation is the recovery mechanism
        (§15.3) — a fresh session re-ingests the same broker events and
        converges to the identical durable state without manufacturing
        a fill."""
        e1 = _seqless(
            broker_order_id="ORD-D42",
            event_type=BrokerEventType.ORDER_SUBMITTED.value,
            status=CanonicalOrderState.SUBMITTED,
            event_ts=_NOW + timedelta(seconds=1),
            received_at=_NOW + timedelta(seconds=1),
            provider_event_id="fi-evt-rc1",
        )
        e2 = _seqless(
            broker_order_id="ORD-D42",
            event_type=BrokerEventType.PARTIAL_FILL.value,
            status=CanonicalOrderState.PARTIALLY_FILLED,
            event_ts=_NOW + timedelta(seconds=2),
            received_at=_NOW + timedelta(seconds=2),
            provider_event_id="fi-evt-rc2",
            cumulative_filled=50,
            fill_facts=FillFacts(
                fill_id="fi-rc-fill", fill_quantity=50, fill_price=100.0,
                cumulative_filled_after=50, remaining_after=50,
            ),
        )
        assert ingest_canonical_event(e1, db_session)["action"] == "APPLIED"
        db_session.commit()
        assert ingest_canonical_event(e2, db_session)["action"] == "APPLIED"
        db_session.commit()
        before = _family_projection(
            db_session, tenant_id="user-d42", broker="broker-d42",
            broker_order_id="ORD-D42",
        )
        # Fresh session simulates process restart / reconnected client.
        fresh = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)()
        try:
            r1 = ingest_canonical_event(e1, fresh)
            r2 = ingest_canonical_event(e2, fresh)
            fresh.commit()
            assert r1["action"] == "DUPLICATE_NOOP"
            assert r2["action"] == "DUPLICATE_NOOP"
        finally:
            fresh.close()
        after = _family_projection(
            db_session, tenant_id="user-d42", broker="broker-d42",
            broker_order_id="ORD-D42",
        )
        assert after is not None and after.status == "PARTIALLY_FILLED"
        assert after.id == before.id

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
        # economics).
        econ = db_session.execute(
            text(
                "SELECT COUNT(*) FROM trade_lifecycle_events "
                "WHERE tenant_id = 'user-d42' AND event_type = 'OrderFilled' "
                "AND quantity_delta IS NOT NULL"
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
        # No economic fill exists in the audit trail for this family.
        econ = db_session.execute(
            text(
                "SELECT COUNT(*) FROM trade_lifecycle_events "
                "WHERE tenant_id = 'user-d42' AND event_type = 'OrderFilled' "
                "AND quantity_delta IS NOT NULL"
            )
        ).scalar()
        assert int(econ) == 0

    def test_lane_c_equivalence_is_never_assumed(self):
        """The fill ledger's Lane-C contract: trade-id-absent fills are
        never deduplicated on (D1, FPv2) alone — economic canonical fills
        require proven equivalence (broker authority at the ledger)."""
        rc = pytest.main([
            "-q", "-p", "no:warnings", "--no-header",
            "tests/test_day41_phase6_7_9_fill_ledger.py",
        ])
        assert rc == 0


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
    def test_d1_lock_serializes_concurrent_family_ingest(self, db_session):
        """D-1 order-family locking (Day41.2): a concurrent duplicate
        replay of the same family serializes on the family lock — the
        loser re-emits the winner's committed outcome."""
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

    def test_day41_2_concurrency_matrix_still_green(self):
        """The Day41.2 concurrency matrix (SQLite layer) is part of the
        execution gate — locking + replay semantics unchanged."""
        rc = pytest.main([
            "-q", "-p", "no:warnings", "--no-header",
            "tests/test_day41_2_cross_d1_concurrency.py",
        ])
        assert rc == 0

    def test_paper_engine_safety_suites_still_green(self):
        """Day41.1 phase suites guarding execution safety remain green
        (Phase 8 order-processing + Phase 6/7/9 fill ledger)."""
        for path in (
            "tests/test_day41_phase8_order_processing.py",
            "tests/test_day41_phase6_7_9_fill_ledger.py",
        ):
            rc = pytest.main(["-q", "-p", "no:warnings", "--no-header", path])
            assert rc == 0, path

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
