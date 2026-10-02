"""Point-in-time strategy-input seam for historical evaluation.

This module deliberately does not reimplement strategy payoff, risk, or
scenario mathematics. It provides only decision-bounded market inputs that a
future backtest runner can feed into the existing shared calculation engines.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.services.point_in_time import PointInTimeDataset
from app.utils.market_time import to_ist_naive


@dataclass(frozen=True)
class PointInTimeStrategyInputs:
    """Decision-bounded market inputs for one historical strategy evaluation."""

    decision_timestamp: datetime
    spot: float | None
    option_candles: tuple
    option_greeks: tuple
    historical_gex: tuple


def build_point_in_time_strategy_inputs(
    db: Session,
    decision_timestamp: datetime | str,
    *,
    instrument_keys: list[str],
    gex_calc_version: str = "h_gex_v1",
    greeks_calc_version: str = "greeks_v3",
) -> PointInTimeStrategyInputs:
    """Build a PIT-safe strategy-input bundle without evaluating the strategy."""
    normalized_decision = to_ist_naive(decision_timestamp)
    if normalized_decision is None:
        raise ValueError("A valid decision timestamp is required.")

    pit = PointInTimeDataset(db)
    decision = pit.nifty_candles_at(normalized_decision)
    candle_selections = pit.option_candles_selections_at_many(
        [normalized_decision],
        instrument_keys=instrument_keys,
    )
    candle_selection = candle_selections[0][1] if candle_selections else {}
    greek_selections = pit.option_greeks_selections_at_many(
        [normalized_decision],
        instrument_keys=instrument_keys,
        calc_version=greeks_calc_version,
    )
    greek_selection = greek_selections[0][1] if greek_selections else {}
    gex = pit.historical_gex_at(
        normalized_decision,
        calc_version=gex_calc_version,
    )

    return PointInTimeStrategyInputs(
        decision_timestamp=normalized_decision,
        spot=decision[0].close if decision else None,
        option_candles=tuple(
            row for _, row in sorted(candle_selection.items())
        ),
        option_greeks=tuple(
            row for _, row in sorted(greek_selection.items())
        ),
        historical_gex=tuple(gex),
    )
