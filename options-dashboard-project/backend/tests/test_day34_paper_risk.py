"""Day 34 — Paper Trading Integration with Centralized Risk (TDD, genuine objects).

Day 34 is an ENFORCEMENT day. Every new paper strategy entry may reach a DB
mutation only through:

    genuine Opportunity → genuine Strategy Candidate (Day-32 gate)
        → Day-33 Central Risk → PASS → existing atomic paper execution

There is no candidate fabrication and no risk bypass: manual/custom and
template entries that carry NO genuine Strategy Candidate are REJECTED with
an explicit structured error and ZERO mutation. Any Day-33 verdict other
than PASS (BLOCKED / PARTIAL / UNAVAILABLE / INVALID) is terminal.

All upstream objects are genuinely constructed through the authoritative
engines/pipelines (reusing the Day-33 suite's real builders — Day-19
IntelligenceResult → Day-28 discover_opportunity → Day-30 rank_strikes →
Day-31 evaluate_strategy). No object.__new__ / __setattr__ stand-ins.

Enforcement policy (Control Center approved): PAPER_ENTRY_POLICY =
policy_version "paper-entry-policy-1.0", allow_unbounded_loss=True, every
numeric/quality/freshness cap unconfigured (None = rule not configured).
Tests that must exercise Day-33 BLOCKED/PARTIAL verdicts pass their own
explicit test-only policies (threshold values chosen in the test, never in
enforcement code).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.routers.deps import SESSION_COOKIE_NAME
from app.models import (
    Leg,
    PaperAccount,
    PaperOrder,
    PaperTransaction,
    Position,
    StrategyExecution,
    StrategyLegExposure,
    Trade,
)
from app.schemas import ExecutionRequestIn
from app.services.paper_execution import (
    PaperExecutionError,
    execute_strategy,
    exit_position,
)
from tests.test_day33_central_risk import (
    EXPIRY,
    NIFTY,
    _candidate_from_evaluation,
    _leg,
    _opportunity,
    _payoff,
    _policy,
    _ranked,
)
from tests.test_day33_central_risk import _evaluation as _day33_evaluation
from app.market_data.contracts import QualityState, Side
from app.quant.scenarios import PositionDirection
from app.strategy_evaluation.contracts import TailClass
from app.strategy_lifecycle.contracts import StrategyLifecycleState

# ---------------------------------------------------------------------------
# DB + HTTP fixtures (mirror the paper-suite conventions)
# ---------------------------------------------------------------------------

LOT = 50
FILL_LTP = 125.28  # deliberately off-tick: must normalize to 125.30


def _evaluation(*, opportunity=None, status_hint="success", **overrides):
    """A genuine Day-31 evaluation whose legs are expressed in CONTRACTS.

    ``OptionLeg.quantity`` is CONTRACTS, while an execution request states
    LOTS + ``lot_size`` (``ExecutionLegIn.lot_size`` = contracts per lot).  A
    seeded one-lot strategy is therefore ``LOT`` contracts on the candidate
    side: without this the risk verdict would describe a position ``LOT``
    times smaller than the one actually executed, and the request/candidate
    leg binding would compare incompatible units.  The Day-33 evidence itself
    is untouched — only the position size the evidence describes is expressed
    in the domain's contract unit.
    """
    from dataclasses import replace

    evaluation = _day33_evaluation(
        opportunity=opportunity, status_hint=status_hint, **overrides)
    return replace(evaluation, legs=tuple(
        replace(leg, quantity=float(leg.quantity) * LOT)
        for leg in evaluation.legs))


@pytest.fixture(autouse=True)
def _market_open_gate():
    status = SimpleNamespace(
        status="open", source="test", trade_date="2026-08-14",
        checked_at="2026-08-14T10:00:00+05:30", message="open",
        error=None, segment="INDEX_DERIVATIVES", session_state="OPEN",
        timezone="Asia/Kolkata", trading_allowed=True,
    )
    with patch("app.routers.paper.get_market_status",
               new=AsyncMock(return_value=status)):
        yield


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture
def client(db_session):
    def override_get_db():
        yield db_session
    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def logged_in(client, db_session):
    from tests.test_helpers import create_test_identity
    session_id, _ = create_test_identity(db_session, "tok-day34")
    # Canonical transport: the HttpOnly session cookie — never a header.
    client.cookies.set(SESSION_COOKIE_NAME, session_id)
    return session_id


def _headers(session_id):
    """Session credentials never travel in headers (repo AGENTS.md).  The
    canonical browser transport is the HttpOnly session cookie, which the
    ``logged_in`` fixture sets on the client; this stays a no-op so call
    sites read explicitly."""
    return {}


def _counts(db):
    """Row counts across every paper mutation surface."""
    return {
        "executions": db.query(StrategyExecution).count(),
        "orders": db.query(PaperOrder).count(),
        "positions": db.query(Position).count(),
        "transactions": db.query(PaperTransaction).count(),
        "trades": db.query(Trade).count(),
        "journal_legs": db.query(Leg).count(),
        "exposures": db.query(StrategyLegExposure).count(),
        "accounts": db.query(PaperAccount).count(),
    }


# ---------------------------------------------------------------------------
# Genuine upstream builders (through the real engines)
# ---------------------------------------------------------------------------


def _short_call_opp():
    """A genuine opportunity whose strategy is a naked short call (unbounded)."""
    return _opportunity("opp-d34-blocked")


def _blocking_evaluation(opportunity):
    """Genuine SUCCESS evaluation of a naked short call with unlimited loss.

    The leg is built through the authoritative Day-33 ``_leg`` builder so it
    carries genuine provenance and quality: the Day-18 engine will price it
    (GREEKS AVAILABLE -> Day-31 SUCCESS), while the authoritative payoff
    assessment declares UNLIMITED_LOSS -- exactly how the Day-33 suite
    models an unbounded-loss candidate that reaches Day-33 policy.
    """
    legs = (_leg(
        side=Side.CALL, strike=20000.0, quantity=1.0,
        direction=PositionDirection.SHORT, entry_price=100.0,
        implied_volatility=0.2, quality=QualityState.EXCELLENT),)
    return _evaluation(
        opportunity=opportunity,
        legs=legs,
        payoff=_payoff(max_profit=100.0, max_loss=None,
                       tail=TailClass.UNLIMITED_LOSS, breakevens=(20100.0,)),
    )


def _request_legs(evaluation, *, lot_size: int = LOT):
    """ExecutionLegIn legs mirroring the evaluation's genuine OptionLegs.

    The candidate carries CONTRACTS; the request states the same position as
    LOTS + ``lot_size``, so each leg is ``contracts / lot_size`` whole lots.
    """
    from app.schemas import ExecutionLegIn
    legs = []
    for leg in evaluation.legs:
        option = leg.option_type.value.lower()
        action = "buy" if leg.direction is PositionDirection.LONG else "sell"
        contracts = float(leg.quantity)
        lots = contracts / lot_size
        assert lots == int(lots) and int(lots) >= 1, (
            f"candidate contracts {contracts} are not a whole number of "
            f"{lot_size}-contract lots")
        legs.append(ExecutionLegIn(
            symbol=NIFTY, expiration_date=leg.expiry, strike_price=leg.strike,
            option_type=option, action=action,
            quantity=int(lots), lot_size=lot_size,
        ))
    return legs


def _prices_for(legs, *, ltp: float = FILL_LTP):
    return {(leg.expiration_date, leg.strike_price, leg.option_type): ltp
            for leg in legs}


# ---------------------------------------------------------------------------
# 1. Genuine candidate → PASS → paper fill (engine + ledger + journal intact)
# ---------------------------------------------------------------------------


class TestPassPath:
    def test_genuine_candidate_pass_fills_atomically(self, db_session):
        from app.services.paper_risk import (
            PAPER_ENTRY_POLICY,
            execute_gated_paper_entry,
        )
        opp = _opportunity("opp-d34-pass")
        ev = _evaluation(opportunity=opp)
        assert ev.status.value == "SUCCESS"
        ranked = _ranked(opportunity=opp)
        request_legs = _request_legs(ev)
        prices = _prices_for(request_legs)

        out = execute_gated_paper_entry(
            "user-1", db_session,
            client_order_id="d34-entry-0001",
            symbol=NIFTY,
            legs=request_legs,
            starting_capital=500000.0,
            opportunity=opp,
            ranked_strikes=ranked,
            evaluation=ev,
            prices=prices,
        )
        assert out.status == "FILLED"
        assert out.duplicated is False
        c = _counts(db_session)
        assert c["executions"] == 1 and c["orders"] == 1
        assert c["positions"] == 1 and c["transactions"] == 1
        assert c["trades"] == 1 and c["journal_legs"] == 1
        assert c["exposures"] == 1

        order = db_session.query(PaperOrder).one()
        # Tick normalization preserved (125.28 → 125.30).
        assert order.fill_price == 125.30
        assert order.status == "FILLED"
        position = db_session.query(Position).one()
        assert position.net_quantity == 1
        assert db_session.query(PaperTransaction).one().amount == round(
            -125.30 * 1 * LOT, 2)

    def test_position_netting_preserved_across_gated_entries(self, db_session):
        from app.services.paper_risk import execute_gated_paper_entry
        opp = _opportunity("opp-d34-net")
        ev = _evaluation(opportunity=opp)
        ranked = _ranked(opportunity=opp)
        request_legs = _request_legs(ev)
        prices = _prices_for(request_legs)
        execute_gated_paper_entry(
            "user-1", db_session, client_order_id="d34-net-0001",
            symbol=NIFTY, legs=request_legs, starting_capital=500000.0,
            opportunity=opp, ranked_strikes=ranked, evaluation=ev,
            prices=prices)
        # Second gated entry on the same instrument nets into ONE position.
        ev2 = _evaluation(opportunity=opp)
        execute_gated_paper_entry(
            "user-1", db_session, client_order_id="d34-net-0002",
            symbol=NIFTY, legs=_request_legs(ev2), starting_capital=500000.0,
            opportunity=opp, ranked_strikes=ranked, evaluation=ev2,
            prices=_prices_for(_request_legs(ev2)))
        positions = db_session.query(Position).all()
        assert len(positions) == 1
        assert positions[0].net_quantity == 2
        assert len(db_session.query(StrategyExecution).all()) == 2

    def test_exit_path_remains_ungated(self, db_session):
        """Exits close existing positions; Day-34 never gates them."""
        from app.services.paper_risk import execute_gated_paper_entry
        from app.schemas import ExitRequestIn
        opp = _opportunity("opp-d34-exit")
        ev = _evaluation(opportunity=opp)
        ranked = _ranked(opportunity=opp)
        request_legs = _request_legs(ev)
        execute_gated_paper_entry(
            "user-1", db_session, client_order_id="d34-exit-0001",
            symbol=NIFTY, legs=request_legs, starting_capital=500000.0,
            opportunity=opp, ranked_strikes=ranked, evaluation=ev,
            prices=_prices_for(request_legs))
        position = db_session.query(Position).one()
        result = exit_position(
            "user-1", position.id,
            ExitRequestIn(client_order_id="d34-exit-cls-0001", quantity=1),
            db_session, fill_price=100.0)
        assert result.duplicated is False
        assert db_session.query(Position).one().status == "closed"
        assert db_session.query(PaperOrder).count() == 2
        assert db_session.query(PaperOrder).filter_by(kind="exit").count() == 1

    def test_replay_returns_original_execution(self, db_session):
        from app.services.paper_risk import execute_gated_paper_entry
        opp = _opportunity("opp-d34-replay")
        ev = _evaluation(opportunity=opp)
        ranked = _ranked(opportunity=opp)
        request_legs = _request_legs(ev)
        first = execute_gated_paper_entry(
            "user-1", db_session, client_order_id="d34-rp-0001",
            symbol=NIFTY, legs=request_legs, starting_capital=500000.0,
            opportunity=opp, ranked_strikes=ranked, evaluation=ev,
            prices=_prices_for(request_legs))
        assert first.duplicated is False
        before = _counts(db_session)
        # A tightened policy (unbounded disallowed) must NOT change a replay:
        # replay detection precedes risk evaluation.
        replay = execute_gated_paper_entry(
            "user-1", db_session, client_order_id="d34-rp-0001",
            symbol=NIFTY, legs=request_legs, starting_capital=500000.0,
            opportunity=opp, ranked_strikes=ranked, evaluation=ev,
            prices=_prices_for(request_legs),
            policy=_policy(version="strict-test", allow_unbounded_loss=False))
        assert replay.duplicated is True
        assert replay.execution_id == first.execution_id
        assert _counts(db_session) == before


# ---------------------------------------------------------------------------
# 2-5. Non-PASS verdicts are terminal with ZERO mutation
# ---------------------------------------------------------------------------


class TestNonPassTerminal:
    def test_blocked_verdict_raises_and_writes_nothing(self, db_session):
        from app.services.paper_risk import execute_gated_paper_entry
        opp = _short_call_opp()
        ev = _blocking_evaluation(opp)
        ranked = _ranked(opportunity=opp)
        request_legs = _request_legs(ev)
        before = _counts(db_session)
        with pytest.raises(PaperExecutionError) as exc_info:
            execute_gated_paper_entry(
                "user-1", db_session, client_order_id="d34-block-0001",
                symbol=NIFTY, legs=request_legs, starting_capital=500000.0,
                opportunity=opp, ranked_strikes=ranked, evaluation=ev,
                prices=_prices_for(request_legs),
                # Test-only policy: naked short (unbounded loss) is forbidden.
                policy=_policy(version="test-block", allow_unbounded_loss=False))
        assert exc_info.value.code == "RISK_BLOCKED"
        assert _counts(db_session) == before

    def test_partial_verdict_raises_and_writes_nothing(self, db_session):
        from app.services.paper_risk import execute_gated_paper_entry
        # SUCCESS evaluation whose payoff carries no finite max-loss value:
        # a configured standalone-loss cap is unverifiable → Day-33 PARTIAL.
        opp = _opportunity("opp-d34-partial")
        ev = _evaluation(
            opportunity=opp,
            payoff=_payoff(max_profit=None, max_loss=None, breakevens=()))
        ranked = _ranked(opportunity=opp)
        request_legs = _request_legs(ev)
        before = _counts(db_session)
        with pytest.raises(PaperExecutionError) as exc_info:
            execute_gated_paper_entry(
                "user-1", db_session, client_order_id="d34-part-0001",
                symbol=NIFTY, legs=request_legs, starting_capital=500000.0,
                opportunity=opp, ranked_strikes=ranked, evaluation=ev,
                prices=_prices_for(request_legs),
                policy=_policy(version="test-part", maximum_standalone_loss=200.0))
        assert exc_info.value.code == "RISK_PARTIAL"
        assert _counts(db_session) == before

    def test_unavailable_candidate_raises_and_writes_nothing(self, db_session):
        """Defense-in-depth at the mutation choke point: a candidate whose
        Day-31 evaluation is UNAVAILABLE yields Day-33 UNAVAILABLE → no rows."""
        unavailable = _evaluation(status_hint="unavailable")
        candidate = _candidate_from_evaluation(unavailable)
        request = ExecutionRequestIn(
            client_order_id="d34-unav-0001", symbol=NIFTY,
            strategy_id=candidate.strategy_id,
            legs=[l for l in _request_legs(candidate.evaluation)],
        )
        before = _counts(db_session)
        with pytest.raises(PaperExecutionError) as exc_info:
            execute_strategy("user-1", request, db_session,
                             _prices_for(request.legs),
                             risk_candidate=candidate)
        assert exc_info.value.code == "RISK_UNAVAILABLE"
        assert _counts(db_session) == before

    def test_invalid_candidate_raises_and_writes_nothing(self, db_session):
        """A non-ELIGIBLE candidate can never reach a fill (Day-33 INVALID)."""
        opp = _opportunity("opp-d34-invalid")
        ev = _evaluation(opportunity=opp)
        candidate = _candidate_from_evaluation(
            ev, lifecycle=StrategyLifecycleState.BLOCKED)
        request = ExecutionRequestIn(
            client_order_id="d34-inv-0001", symbol=NIFTY,
            strategy_id=candidate.strategy_id,
            legs=[l for l in _request_legs(candidate.evaluation)],
        )
        before = _counts(db_session)
        with pytest.raises(PaperExecutionError) as exc_info:
            execute_strategy("user-1", request, db_session,
                             _prices_for(request.legs),
                             risk_candidate=candidate)
        assert exc_info.value.code == "RISK_INVALID"
        assert _counts(db_session) == before


# ---------------------------------------------------------------------------
# 6-7. Manual/custom + template entries carry NO candidate → REJECTED
# ---------------------------------------------------------------------------


class TestCandidateRequiredRejections:
    def test_manual_custom_entry_rejected_with_zero_mutation(
            self, client, logged_in, db_session):
        payload = {
            "client_order_id": "d34-manual-0001",
            "symbol": "NIFTY",
            "strategy_tag": "Custom",
            "strategy_id": None,
            "starting_capital": 500000,
            "legs": [{
                "symbol": "NIFTY", "expiration_date": EXPIRY,
                "strike_price": 20000.0, "option_type": "call",
                "action": "buy", "quantity": 1, "lot_size": LOT,
            }],
        }
        before = _counts(db_session)
        # Day 50 / Issue #118 Slice A: the route produces the genuine
        # candidate SERVER-SIDE before execution. A bare manual entry can no
        # longer reach the choke point at all: it fails closed at the
        # producer boundary (this test identity carries no broker
        # market-data credential, so the producer rejects with
        # MARKET_DATA_UNAUTHORIZED before acquiring any evidence). The
        # fail-closed guarantee and zero mutation are unchanged; the
        # choke-point-level STRATEGY_CANDIDATE_REQUIRED invariant for direct
        # service calls remains covered by the Day-50 suite.
        with patch("app.routers.paper.resolve_market_prices",
                   new_callable=AsyncMock) as mock_prices:
            mock_prices.return_value = {(EXPIRY, 20000.0, "call"): 100.0}
            resp = client.post("/paper/executions",
                               headers=_headers(logged_in), json=payload)
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        # Day 50 / Issue #118 Slice A: the route produces the genuine candidate
        # SERVER-SIDE before execution, so a bare manual entry can no longer
        # reach the choke point at all — it fails closed at the producer
        # boundary, because this identity carries no broker market-data
        # credential.  Pinned exactly, not as a disjunction: the earlier guard
        # is deterministic.  The fail-closed guarantee and zero mutation are
        # unchanged, and the choke-point-level STRATEGY_CANDIDATE_REQUIRED
        # invariant for direct service calls is covered by
        # test_candidate_production.py::test_choke_point_still_rejects_missing_candidate_directly.
        assert "MARKET_DATA_UNAUTHORIZED" in detail, detail
        assert _counts(db_session) == before

    def test_template_entry_rejected_with_zero_mutation(
            self, client, logged_in, db_session):
        # Create a real template (user-owned), then execute it bare.
        template_resp = client.post(
            "/paper/templates",
            headers=_headers(logged_in),
            json={"name": "Day34 Template", "symbol": "NIFTY", "legs": [{
                "action": "buy", "option_type": "call", "strike": 20000.0,
                "expiry": EXPIRY, "quantity": 1, "lot_size": LOT,
                "position": 0,
            }]},
        )
        assert template_resp.status_code == 201
        tid = template_resp.json()["id"]

        before = _counts(db_session)
        # The template has no genuine Opportunity/Evaluation/Candidate, but
        # market/chain resolution fully succeeds (mandate ordering: data
        # resolution precedes candidate resolution). Execution must still
        # REJECT pre-write at the Day-34 mutation choke point.
        resolved_leg = SimpleNamespace(
            position=0, action="buy", option_type="call", quantity=1,
            lot_size=LOT, resolved_strike=20000.0, resolved_expiry=EXPIRY,
            strike_mode_used="fixed", expiry_mode_used="fixed",
            current_price=100.0, price_status="available",
            quote_timestamp="2026-08-20T10:00:00+05:30", ltp=100.0,
            warnings=[], symbol="NIFTY", expiration_date=EXPIRY,
            strike_price=20000.0,
        )
        with patch("app.routers.paper.require_market_open",
                   new_callable=AsyncMock), \
             patch("app.services.template_resolution.resolve_legs",
                   new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.resolve_market_prices",
                   new_callable=AsyncMock) as mock_prices:
            mock_resolve.return_value = SimpleNamespace(
                status="RESOLVED", symbol="NIFTY", template_id=tid,
                template_name="Day34 Template", legs=[resolved_leg],
                errors=[], warnings=[], chain_strike_step=50.0,
            )
            mock_prices.return_value = {(EXPIRY, 20000.0, "call"): 100.0}
            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=_headers(logged_in),
                json={"client_order_id": "d34-tpl-0001",
                      "starting_capital": 500000},
            )
        assert resp.status_code == 409
        assert "STRATEGY_CANDIDATE_REQUIRED" in resp.json()["detail"]
        assert _counts(db_session) == before


# ---------------------------------------------------------------------------
# 15-16. Audit metadata on successful executions
# ---------------------------------------------------------------------------


class TestAuditMetadata:
    def test_metadata_contains_risk_audit_reference(self, db_session):
        from app.services.paper_risk import (
            PAPER_ENTRY_POLICY,
            execute_gated_paper_entry,
        )
        opp = _opportunity("opp-d34-meta")
        ev = _evaluation(opportunity=opp)
        ranked = _ranked(opportunity=opp)
        request_legs = _request_legs(ev)
        execute_gated_paper_entry(
            "user-1", db_session, client_order_id="d34-meta-0001",
            symbol=NIFTY, legs=request_legs, starting_capital=500000.0,
            opportunity=opp, ranked_strikes=ranked, evaluation=ev,
            prices=_prices_for(request_legs))
        execution = db_session.query(StrategyExecution).one()
        assert execution.execution_metadata
        meta = json.loads(execution.execution_metadata)
        assert meta["risk_status"] == "PASS"
        assert meta["risk_policy_version"] == PAPER_ENTRY_POLICY.policy_version
        assert meta["candidate_id"].startswith("candidate:")
        assert meta["opportunity_id"] == "opp-d34-meta"
        assert meta["risk_reference_timestamp"]
        assert meta["risk_calculation_version"]
        assert meta["risk_assessment_id"]

    def test_metadata_round_trip_valid(self, db_session):
        from app.services.paper_risk import execute_gated_paper_entry
        opp = _opportunity("opp-d34-meta2")
        ev = _evaluation(opportunity=opp)
        ranked = _ranked(opportunity=opp)
        request_legs = _request_legs(ev)
        execute_gated_paper_entry(
            "user-1", db_session, client_order_id="d34-meta2-0001",
            symbol=NIFTY, legs=request_legs, starting_capital=500000.0,
            opportunity=opp, ranked_strikes=ranked, evaluation=ev,
            prices=_prices_for(request_legs))
        execution = db_session.query(StrategyExecution).one()
        # Second serialization pass (read → dump → read) stays valid JSON.
        again = json.loads(json.dumps(json.loads(execution.execution_metadata)))
        assert again == json.loads(execution.execution_metadata)


# ---------------------------------------------------------------------------
# 17. Missing evidence never fabricated (no silent PASS from a gap)
# ---------------------------------------------------------------------------


class TestMissingEvidence:
    def test_incomplete_upstream_evaluation_never_fills(self, db_session):
        from app.services.paper_risk import execute_gated_paper_entry
        opp = _opportunity("opp-d34-gap")
        # Day-31 PARTIAL (liquidity incomplete): cannot be a genuine SUCCESS
        # candidate; the gate must reject before risk and before mutation.
        ev = _evaluation(status_hint="partial", opportunity=opp)
        ranked = _ranked(opportunity=opp)
        request_legs = _request_legs(ev)
        before = _counts(db_session)
        with pytest.raises(PaperExecutionError) as exc_info:
            execute_gated_paper_entry(
                "user-1", db_session, client_order_id="d34-gap-0001",
                symbol=NIFTY, legs=request_legs, starting_capital=500000.0,
                opportunity=opp, ranked_strikes=ranked, evaluation=ev,
                prices=_prices_for(request_legs))
        assert exc_info.value.code in ("CANDIDATE_NOT_ELIGIBLE", "RISK_PARTIAL")
        assert _counts(db_session) == before


class TestContractUnitBinding:
    """Request/candidate leg binding must compare CONTRACTS on both sides.

    ``ExecutionLegIn.quantity`` is LOTS and ``lot_size`` is CONTRACTS PER
    LOT, while a genuine candidate's ``OptionLeg.quantity`` is CONTRACTS:
    ``1 lot × 65 = 65 contracts``.  Without the conversion the request side
    would report ``1`` against the candidate's ``65``, so every genuine
    Day-32/33 entry would be rejected as a mismatched (unvetted) leg set —
    and a smuggled leg count could never be detected dimensionally.
    """

    def test_one_lot_binds_to_the_candidates_contracts(self):
        from app.services.paper_risk import legs_match_request

        ev = _evaluation()
        assert legs_match_request(ev.legs, _request_legs(ev)) is True

    def test_more_lots_than_the_candidate_is_a_different_position(self):
        from app.schemas import ExecutionLegIn
        from app.services.paper_risk import legs_match_request

        ev = _evaluation()
        request = _request_legs(ev)
        doubled = list(request)
        doubled[0] = ExecutionLegIn(
            symbol=doubled[0].symbol,
            expiration_date=doubled[0].expiration_date,
            strike_price=doubled[0].strike_price,
            option_type=doubled[0].option_type,
            action=doubled[0].action,
            quantity=doubled[0].quantity * 2,   # twice as many LOTS
            lot_size=doubled[0].lot_size,
        )
        assert legs_match_request(ev.legs, doubled) is False

    def test_lot_size_participates_in_the_binding(self):
        from app.schemas import ExecutionLegIn
        from app.services.paper_risk import legs_match_request

        ev = _evaluation()
        request = _request_legs(ev)
        unsized = list(request)
        unsized[0] = ExecutionLegIn(
            symbol=unsized[0].symbol,
            expiration_date=unsized[0].expiration_date,
            strike_price=unsized[0].strike_price,
            option_type=unsized[0].option_type,
            action=unsized[0].action,
            quantity=1,          # 1 lot × 1 contract — not the risked size
            lot_size=1,
        )
        assert legs_match_request(ev.legs, request) is True
        assert legs_match_request(ev.legs, unsized) is False
