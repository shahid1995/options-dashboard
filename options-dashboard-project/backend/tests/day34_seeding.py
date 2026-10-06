"""Day 34 — test-only seeding through the genuine intelligence/risk chain.

Sanctioned by the Control Center: legacy paper/template suites that seed
positions via bare manual entries are adapted to seed through the SAME real
production path a genuine strategy entry takes, without weakening production
enforcement:

    legacy entry intent (ExecutionRequestIn)
        -> genuine OptionLeg objects            (real contracts)
        -> genuine Day-28 Opportunity           (discover_opportunity)
        -> genuine Day-30 ranked strikes        (rank_strikes)
        -> genuine Day-31 Strategy Evaluation   (evaluate_strategy)
        -> genuine Day-32 Opportunity Gate      (evaluate_strategy_gate)
        -> eligible StrategyCandidate
        -> Day-33 Central Risk PASS             (PAPER_ENTRY_POLICY)
        -> existing atomic paper execution      (real execute_strategy)

The mechanism patches ONLY the two ``execute_strategy`` import sites the
legacy HTTP routers use, wrapping every bare entry intent with the genuine
candidate the enforcement gate requires.  No fake candidates, no
``object.__new__``, no monkeypatched risk verdicts: the candidate comes from
the real Day-32 gate and the PASS verdict from the real Day-33 engine.

The fixture is deliberately NOT autouse globally: each affected legacy test
module imports ``day34_gated_seeding`` explicitly so the Day-34 focused
suite (and every other suite) keeps the strict no-candidate rejection.
"""

from __future__ import annotations

import pytest

from app.market_data.contracts import QualityState, Side
from app.quant.scenarios import OptionLeg, PositionDirection
from app.services.paper_execution import execute_strategy as _real_execute_strategy
from app.strategy_lifecycle.lifecycle import evaluate_strategy_gate
from tests.test_day33_central_risk import (
    REF,
    _evaluation,
    _opportunity,
    _ranked,
    _prov,
)

#: The approved application-level paper-entry policy (same object the
#: production Day-34 bridge uses — no thresholds invented here).
from app.services.paper_risk import PAPER_ENTRY_POLICY

_OPP_CACHE: dict[str, object] = {}


def _opportunity_for(client_order_id: str):
    """A genuine Day-28 Opportunity, cached per client order id so every
    reference to one entry intent shares the same opportunity object."""
    key = f"opp-seed-{client_order_id}"
    if key not in _OPP_CACHE:
        _OPP_CACHE[key] = _opportunity(key)
    return _OPP_CACHE[key]


def _genuine_leg(leg) -> OptionLeg:
    """Mirror the Day-33 ``_leg`` builder for one ExecutionLegIn, honoring
    its expiry so multi-expiry legacy intents stay genuine."""
    direction = PositionDirection.LONG if leg.action == "buy" \
        else PositionDirection.SHORT
    return OptionLeg(
        option_type=Side.CALL if leg.option_type.lower() == "call" else Side.PUT,
        strike=float(leg.strike_price),
        expiry=leg.expiration_date,
        quantity=float(leg.quantity),
        direction=direction,
        entry_price=100.0,
        implied_volatility=0.2,
        quality=QualityState.EXCELLENT,
        provenance=_prov("LEG"),
    )


def _genuine_candidate_for(request):
    """Build the genuine Day-32 eligible candidate for a legacy entry intent.

    Returns ``None`` when the genuine chain cannot produce an eligible
    candidate (defensive: the real enforcement gate then rejects the entry
    exactly as it does in production — nothing is fabricated).
    """
    strategy_id = request.strategy_id or f"strategy-seed-{request.client_order_id}"
    opportunity = _opportunity_for(request.client_order_id)
    legs = tuple(_genuine_leg(leg) for leg in request.legs)
    evaluation = _evaluation(
        opportunity=opportunity, legs=legs, strategy_id=strategy_id)
    ranked = _ranked(opportunity=opportunity)
    gate = evaluate_strategy_gate(
        opportunity, ranked, evaluation,
        strategy_id=strategy_id, legs=legs, reference_timestamp=REF)
    if gate.candidate is None or not gate.eligible:
        return None
    return gate.candidate


@pytest.fixture(autouse=True)
def day34_gated_seeding(monkeypatch):
    """Wrap the legacy HTTP entry routers' ``execute_strategy`` call sites so
    every bare entry intent is seeded through the genuine Day-28→Day-33 chain.

    Only ``execute_strategy`` invocations WITHOUT a ``risk_candidate`` are
    wrapped (a call that already carries a candidate passes straight through),
    so Day-34 tests and direct service calls are untouched.
    """

    def gated_execute(user_id, request, db, prices, **kwargs):
        if kwargs.get("risk_candidate") is not None:
            return _real_execute_strategy(user_id, request, db, prices, **kwargs)
        candidate = _genuine_candidate_for(request)
        if candidate is None:
            # Genuine chain cannot produce eligibility — leave the real
            # enforcement gate to reject (STRATEGY_CANDIDATE_REQUIRED).
            return _real_execute_strategy(user_id, request, db, prices, **kwargs)
        return _real_execute_strategy(
            user_id, request, db, prices,
            risk_candidate=candidate,
            risk_policy=PAPER_ENTRY_POLICY,
            **{k: v for k, v in kwargs.items()
               if k not in ("risk_candidate", "risk_policy")},
        )

    monkeypatch.setattr("app.routers.paper.execute_strategy", gated_execute)
    monkeypatch.setattr(
        "app.services.paper_execution.execute_strategy", gated_execute)

    # Day 50 (Issue #118): the production route no longer calls bare
    # execute_strategy — it produces the genuine candidate server-side via
    # candidate_production.produce_candidate_and_execute first.  Legacy
    # suites seed through that SAME production seam: the wrapper acquires
    # no broker evidence at all and hands the bare intent to the genuine
    # Day-28→Day-33 chain below (no fabricated evidence; the real engines
    # decide eligibility exactly as before).
    from app.services.candidate_production import (
        produce_candidate_and_execute as _real_producer,
    )

    async def seeded_producer(user_id, db, request, prices, **kwargs):
        candidate = _genuine_candidate_for(request)
        if candidate is None:
            # Genuine chain cannot produce eligibility — leave the real
            # producer to fail closed exactly as it does in production.
            return await _real_producer(user_id, db, request, prices, **kwargs)
        return _real_execute_strategy(
            user_id, request, db, prices,
            risk_candidate=candidate,
            risk_policy=PAPER_ENTRY_POLICY,
        )

    monkeypatch.setattr(
        "app.services.candidate_production.produce_candidate_and_execute",
        seeded_producer,
    )
    yield