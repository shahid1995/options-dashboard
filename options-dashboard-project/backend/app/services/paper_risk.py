"""Day 34 — Paper-entry integration with centralized risk (enforcement).

Day 34 adds NO risk mathematics. It is the enforcement bridge between the
approved Day-28 → Day-32 → Day-33 intelligence chain and the existing
server-authoritative paper execution engine:

    genuine Opportunity + ranked strikes + Day-31 evaluation
        → Day-32 Opportunity Gate        (evaluate_strategy_gate)
        → eligible StrategyCandidate
        → Day-33 Central Risk            (assess_candidate_risk)
        → PASS
        → existing atomic paper execution
        → audit metadata on StrategyExecution.execution_metadata

Rules enforced here
-------------------
* A paper entry is possible ONLY from a genuine candidate produced by the
  Day-32 gate.  Manual/custom/template entries that carry no genuine
  candidate are rejected with ``STRATEGY_CANDIDATE_REQUIRED`` (zero rows).
* Only ``CentralRiskStatus.PASS`` may reach a DB mutation.  Every other
  verdict is terminal (RISK_BLOCKED / RISK_PARTIAL / RISK_UNAVAILABLE /
  RISK_INVALID) and writes nothing.
* Replay detection stays BEFORE risk evaluation: a previously successful
  ``client_order_id`` returns its ORIGINAL execution untouched, even if the
  current policy would block a fresh entry.
* Risk evaluation happens in ``execute_strategy`` BEFORE the first DB write
  (including ``_get_or_create_account``), at the single mutation choke point.
* The audit reference is stored on the existing ``execution_metadata``
  column — no migration, no duplication of the full risk result.
* Exits are untouched: this module only ever gates NEW strategy entries.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.central_risk.contracts import RiskPolicy
from app.models import StrategyExecution
from app.opportunity.contracts import Opportunity
from app.quant.scenarios import OptionLeg, PositionDirection
from app.schemas import ExecutionOut, ExecutionRequestIn
from app.strike_ranking.contracts import StrikeRankingResult
from app.strategy_evaluation.contracts import StrategyEvaluationResult
from app.strategy_lifecycle.lifecycle import evaluate_strategy_gate

# Control Center approved paper-entry policy (explicit, versioned):
# unbounded standalone loss explicitly permitted, and every numeric /
# quality / freshness rule left UNCONFIGURED (None = rule not configured in
# Day-33 semantics).  No threshold is invented here.
PAPER_ENTRY_POLICY = RiskPolicy(
    policy_version="paper-entry-policy-1.0",
    allow_unbounded_loss=True,
    maximum_standalone_loss=None,
    maximum_scenario_loss=None,
    minimum_quality=None,
    maximum_data_age_seconds=None,
)


def _request_leg_keys(legs: list) -> list[tuple]:
    """Normalized per-leg identity for an ExecutionLegIn request list.

    ``ExecutionLegIn.quantity`` is LOTS and ``lot_size`` is CONTRACTS PER
    LOT, while a genuine candidate's ``OptionLeg.quantity`` is CONTRACTS, so
    the request's position size is ``quantity × lot_size`` contracts.  The
    comparison stays EXACT: a different quantity or a different lot size is a
    different position, so the leg binding is never weakened.
    """
    keys = []
    for leg in legs:
        direction = PositionDirection.LONG if leg.action == "buy" \
            else PositionDirection.SHORT
        keys.append((leg.expiration_date, leg.strike_price,
                     leg.option_type.lower(), direction,
                     float(leg.quantity) * float(leg.lot_size)))
    return keys


def _candidate_leg_keys(legs: tuple[OptionLeg, ...]) -> list[tuple]:
    """Normalized per-leg identity for genuine domain OptionLegs (CONTRACTS)."""
    return [(leg.expiry, leg.strike, leg.option_type.value.lower(),
             leg.direction, float(leg.quantity)) for leg in legs]


def legs_match_request(legs: tuple[OptionLeg, ...], request_legs: list) -> bool:
    """The executed request legs must be EXACTLY the candidate's legs.

    A risk verdict describes the candidate's legs; executing a different leg
    set would silently run an unvetted fill.  Multiset comparison so leg
    order cannot be used to smuggle a mismatched leg.
    """
    if len(legs) != len(request_legs):
        return False
    return sorted(_candidate_leg_keys(legs)) == \
        sorted(_request_leg_keys(request_legs))


def _not_eligible(message: str) -> "PaperExecutionError":
    from app.services.paper_execution import PaperExecutionError
    return PaperExecutionError("CANDIDATE_NOT_ELIGIBLE", message)


def execute_gated_paper_entry(
    user_id: str,
    db: Session,
    *,
    client_order_id: str,
    symbol: str,
    legs: list,
    opportunity: Opportunity,
    ranked_strikes: StrikeRankingResult,
    evaluation: StrategyEvaluationResult,
    prices: dict,
    strategy_id: str | None = None,
    strategy_tag: str | None = None,
    starting_capital: float | None = None,
    policy: RiskPolicy | None = None,
    reference_timestamp: datetime | None = None,
) -> ExecutionOut:
    """The ONLY sanctioned new-entry path: genuine chain → gate → risk →
    existing atomic paper execution.

    ``legs`` must be ``ExecutionLegIn``-compatible legs that EXACTLY match
    the genuine evaluation's legs (the executed legs equal the risked legs).
    """
    from app.services.paper_execution import (
        PaperExecutionError,
        execute_strategy,
    )

    symbol = str(symbol).upper()
    strategy_identity = strategy_id or evaluation.strategy_id
    if strategy_id is not None and evaluation.strategy_id is not None \
            and strategy_id != evaluation.strategy_id:
        raise _not_eligible(
            "strategy_id does not match the genuine Day-31 evaluation "
            "strategy identity")

    if not legs_match_request(evaluation.legs, legs):
        raise PaperExecutionError(
            "CANDIDATE_LEG_MISMATCH",
            "The requested execution legs do not exactly match the genuine "
            "strategy evaluation legs. Paper order was not executed.")

    request = ExecutionRequestIn(
        client_order_id=client_order_id,
        symbol=symbol,
        strategy_id=strategy_identity,
        strategy_tag=strategy_tag or strategy_identity or "Custom",
        starting_capital=starting_capital,
        legs=list(legs),
    )

    # 1. Replay detection stays BEFORE gate/risk: a previously successful
    #    client_order_id returns the ORIGINAL execution untouched (the
    #    engine's replay branch precedes the candidate-required gate).
    existing = db.scalar(
        select(StrategyExecution).where(
            StrategyExecution.user_id == user_id,
            StrategyExecution.client_order_id == client_order_id,
        )
    )
    if existing is not None:
        return execute_strategy(user_id, request, db, prices)

    # 2. Day-32 Opportunity Gate on the genuine upstream objects.
    gate = evaluate_strategy_gate(
        opportunity,
        ranked_strikes,
        evaluation,
        strategy_id=strategy_identity,
        legs=evaluation.legs,
        reference_timestamp=reference_timestamp,
    )
    if gate.candidate is None or not gate.eligible:
        reasons = "; ".join(r.message for r in gate.blocking_reasons) \
            or "candidate is not eligible"
        raise _not_eligible(reasons)

    # 3. Day-33 Central Risk runs INSIDE execute_strategy, AFTER the replay
    #    branch and BEFORE the first DB write (risk-before-mutation at the
    #    single choke point).
    return execute_strategy(
        user_id, request, db, prices,
        risk_candidate=gate.candidate,
        risk_policy=policy if policy is not None else PAPER_ENTRY_POLICY,
        reference_timestamp=reference_timestamp,
    )
