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
    pit = PointInTimeDataset(db)
    decision = pit.nifty_candles_at(decision_timestamp)
    candles = pit.option_candles_at_many(
        [decision_timestamp],
        instrument_keys=instrument_keys,
    )
    greeks = pit.option_greeks_at_many(
        [decision_timestamp],
        calc_version=greeks_calc_version,
    )
    gex = pit.historical_gex_at(
        decision_timestamp,
        calc_version=gex_calc_version,
    )

    return PointInTimeStrategyInputs(
        decision_timestamp=decision_timestamp if isinstance(decision_timestamp, datetime)
        else decision[0].open_time if decision else pit._require_cutoff(decision_timestamp),
        spot=decision[0].close if decision else None,
        option_candles=tuple(candles),
        option_greeks=tuple(
            row for row in greeks if row.instrument_key in set(instrument_keys)
        ),
        historical_gex=tuple(gex),
    )
