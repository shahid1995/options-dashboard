"""Point-in-time access to historical market data for backtests and research.

The interface centralizes the one temporal invariant that backtesting depends on:
for a decision timestamp T, feature observations must have an observation time
<= T. Forward labels are intentionally outside this interface.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models import (
    HistoricalGexSnapshot,
    IVObservation,
    NiftyCandle,
    OptionCandle,
    OptionGreeks,
)
from app.utils.market_time import to_ist_naive


def _require_cutoff(value: datetime | str) -> datetime:
    """Normalize and validate a required point-in-time cutoff."""
    cutoff = to_ist_naive(value)
    if cutoff is None:
        raise ValueError("A valid decision timestamp is required for PIT queries.")
    return cutoff


def _with_cutoff(statement: Select, column, cutoff: datetime) -> Select:
    """Apply the inclusive PIT predicate to a SQLAlchemy statement."""
    return statement.where(column <= cutoff)


class PointInTimeDataset:
    """Server-side historical feature access bounded by a decision timestamp.

    The returned rows are feature candidates only. Forward-looking labels are
    intentionally not exposed by this interface.
    """

    def __init__(self, db: Session):
        self.db = db

    def nifty_candles(
        self,
        symbol: str,
        decision_timestamp: datetime | str,
        *,
        interval: str = "3min",
        since: datetime | str | None = None,
        limit: int = 500,
    ) -> list[NiftyCandle]:
        """Return NIFTY candles available at or before the decision time."""
        cutoff = _require_cutoff(decision_timestamp)
        statement = _with_cutoff(
            select(NiftyCandle).where(
                NiftyCandle.symbol == symbol.upper(),
                NiftyCandle.interval == interval,
            ),
            NiftyCandle.open_time,
            cutoff,
        )
        if since is not None:
            start = _require_cutoff(since)
            statement = statement.where(NiftyCandle.open_time >= start)
        statement = (
            statement.order_by(NiftyCandle.open_time.asc())
            .limit(max(1, limit))
        )
        return list(self.db.scalars(statement))

    def option_candles(
        self,
        instrument_key: str,
        decision_timestamp: datetime | str,
        *,
        interval: str = "3min",
        since: datetime | str | None = None,
        limit: int = 10000,
    ) -> list[OptionCandle]:
        """Return option candles available at or before the decision time."""
        cutoff = _require_cutoff(decision_timestamp)
        statement = _with_cutoff(
            select(OptionCandle).where(
                OptionCandle.instrument_key == instrument_key,
                OptionCandle.interval == interval,
            ),
            OptionCandle.open_time,
            cutoff,
        )
        if since is not None:
            start = _require_cutoff(since)
            statement = statement.where(OptionCandle.open_time >= start)
        statement = (
            statement.order_by(OptionCandle.open_time.asc())
            .limit(max(1, limit))
        )
        return list(self.db.scalars(statement))

    def iv_observations(
        self,
        symbol: str,
        decision_timestamp: datetime | str,
        *,
        expiry: str | None = None,
        option_type: str | None = None,
        limit: int = 1000,
    ) -> list[IVObservation]:
        """Return IV observations available at or before the decision time."""
        cutoff = _require_cutoff(decision_timestamp)
        statement = _with_cutoff(
            select(IVObservation).where(IVObservation.symbol == symbol.upper()),
            IVObservation.observed_at,
            cutoff,
        )
        if expiry:
            statement = statement.where(IVObservation.expiry == expiry)
        if option_type:
            statement = statement.where(
                IVObservation.option_type == option_type.lower()
            )
        statement = (
            statement.order_by(IVObservation.observed_at.asc())
            .limit(max(1, limit))
        )
        return list(self.db.scalars(statement))

    def option_greeks(
        self,
        instrument_key: str,
        decision_timestamp: datetime | str,
        *,
        interval: str = "3min",
        calc_version: str | None = None,
        limit: int = 10000,
    ) -> list[OptionGreeks]:
        """Return reconstructed Greeks whose market timestamp is not in the future."""
        cutoff = _require_cutoff(decision_timestamp)
        statement = _with_cutoff(
            select(OptionGreeks).where(
                OptionGreeks.instrument_key == instrument_key,
                OptionGreeks.interval == interval,
            ),
            OptionGreeks.open_time,
            cutoff,
        )
        if calc_version:
            statement = statement.where(OptionGreeks.calc_version == calc_version)
        statement = (
            statement.order_by(OptionGreeks.open_time.asc())
            .limit(max(1, limit))
        )
        return list(self.db.scalars(statement))

    def nifty_candles_at(
        self,
        decision_timestamp: datetime | str,
        *,
        symbol: str = "NIFTY",
        interval: str = "3min",
    ) -> list[NiftyCandle]:
        """Return NIFTY candles whose observation timestamp equals the decision time."""
        cutoff = _require_cutoff(decision_timestamp)
        statement = select(NiftyCandle).where(
            NiftyCandle.symbol == symbol.upper(),
            NiftyCandle.interval == interval,
            NiftyCandle.open_time == cutoff,
        )
        return list(self.db.scalars(statement))

    def option_candles_at(
        self,
        decision_timestamp: datetime | str,
        *,
        instrument_keys: list[str] | None = None,
        interval: str = "3min",
    ) -> list[OptionCandle]:
        """Return option candles observed exactly at the decision time."""
        cutoff = _require_cutoff(decision_timestamp)
        statement = select(OptionCandle).where(
            OptionCandle.interval == interval,
            OptionCandle.open_time == cutoff,
        )
        if instrument_keys:
            statement = statement.where(OptionCandle.instrument_key.in_(instrument_keys))
        return list(self.db.scalars(statement))

    def option_greeks_at(
        self,
        decision_timestamp: datetime | str,
        *,
        instrument_keys: list[str] | None = None,
        interval: str = "3min",
        calc_version: str | None = None,
    ) -> list[OptionGreeks]:
        """Return reconstructed Greeks observed exactly at the decision time."""
        cutoff = _require_cutoff(decision_timestamp)
        statement = select(OptionGreeks).where(
            OptionGreeks.interval == interval,
            OptionGreeks.open_time == cutoff,
        )
        if instrument_keys:
            statement = statement.where(OptionGreeks.instrument_key.in_(instrument_keys))
        if calc_version:
            statement = statement.where(OptionGreeks.calc_version == calc_version)
        return list(self.db.scalars(statement))

    def option_greeks_at_many(
        self,
        decision_timestamps: list[datetime | str],
        *,
        interval: str = "3min",
        calc_version: str | None = None,
    ) -> list[OptionGreeks]:
        """Return Greeks observed at any of the supplied decision timestamps."""
        cutoffs = [_require_cutoff(ts) for ts in decision_timestamps]
        if not cutoffs:
            return []
        statement = select(OptionGreeks).where(
            OptionGreeks.interval == interval,
            OptionGreeks.open_time.in_(cutoffs),
        )
        if calc_version:
            statement = statement.where(OptionGreeks.calc_version == calc_version)
        return list(self.db.scalars(statement))

    def option_candles_at_many(
        self,
        decision_timestamps: list[datetime | str],
        *,
        instrument_keys: list[str] | None = None,
        interval: str = "3min",
    ) -> list[OptionCandle]:
        """Return option candles observed at any supplied decision timestamp."""
        cutoffs = [_require_cutoff(ts) for ts in decision_timestamps]
        if not cutoffs:
            return []
        statement = select(OptionCandle).where(
            OptionCandle.interval == interval,
            OptionCandle.open_time.in_(cutoffs),
        )
        if instrument_keys:
            statement = statement.where(OptionCandle.instrument_key.in_(instrument_keys))
        return list(self.db.scalars(statement))

    def historical_gex_at(
        self,
        decision_timestamp: datetime | str,
        *,
        interval: str = "3min",
        calc_version: str = "h_gex_v1",
        successful_only: bool = True,
    ) -> list[HistoricalGexSnapshot]:
        """Return historical GEX observations whose market time equals T."""
        cutoff = _require_cutoff(decision_timestamp)
        statement = select(HistoricalGexSnapshot).where(
            HistoricalGexSnapshot.interval == interval,
            HistoricalGexSnapshot.open_time == cutoff,
            HistoricalGexSnapshot.calc_version == calc_version,
        )
        if successful_only:
            statement = statement.where(HistoricalGexSnapshot.status == "SUCCESS")
        return list(self.db.scalars(statement))

    def historical_gex(
        self,
        instrument_key: str,
        decision_timestamp: datetime | str,
        *,
        interval: str = "3min",
        calc_version: str = "h_gex_v1",
        limit: int = 10000,
    ) -> list[HistoricalGexSnapshot]:
        """Return reconstructed GEX whose market timestamp is not in the future."""
        cutoff = _require_cutoff(decision_timestamp)
        statement = _with_cutoff(
            select(HistoricalGexSnapshot).where(
                HistoricalGexSnapshot.instrument_key == instrument_key,
                HistoricalGexSnapshot.interval == interval,
                HistoricalGexSnapshot.calc_version == calc_version,
                HistoricalGexSnapshot.status == "SUCCESS",
            ),
            HistoricalGexSnapshot.open_time,
            cutoff,
        )
        statement = (
            statement.order_by(HistoricalGexSnapshot.open_time.asc())
            .limit(max(1, limit))
        )
        return list(self.db.scalars(statement))
