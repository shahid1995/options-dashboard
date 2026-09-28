"""Point-in-time access to historical market data for backtests and research.

The interface centralizes the temporal invariant that backtesting depends on.

Point observations (such as IV quotes) are available when their observation
timestamp is <= the decision time. Candle-derived rows are available only after
their interval has completed, so their open_time must be strictly before the
decision time. Forward labels are intentionally outside this interface.
"""

from __future__ import annotations

from datetime import datetime, timedelta

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

DEFAULT_GREEKS_CALC_VERSION = "greeks_v3"


def _require_cutoff(value: datetime | str) -> datetime:
    """Normalize and validate a required point-in-time cutoff."""
    cutoff = to_ist_naive(value)
    if cutoff is None:
        raise ValueError("A valid decision timestamp is required for PIT queries.")
    return cutoff


def _with_cutoff(statement: Select, column, cutoff: datetime) -> Select:
    """Apply an inclusive PIT predicate for point observations."""
    return statement.where(column <= cutoff)


def _with_completed_candle_cutoff(statement: Select, column, cutoff: datetime) -> Select:
    """Apply the PIT predicate for interval-derived candle observations."""
    return statement.where(column < cutoff)


def _completed_bar_open_time(decision_timestamp: datetime, interval: str) -> datetime:
    """Return the latest fully completed candle's open time at a decision cutoff."""
    durations = {
        "1min": 60,
        "3min": 180,
        "5min": 300,
        "15min": 900,
        "30min": 1800,
        "day": 86400,
    }
    seconds = durations.get(interval)
    if seconds is None:
        raise ValueError(f"Unsupported PIT candle interval: {interval}")
    return decision_timestamp - timedelta(seconds=seconds)


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
        """Return completed NIFTY candles available by the decision time."""
        cutoff = _require_cutoff(decision_timestamp)
        statement = _with_completed_candle_cutoff(
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
        statement = statement.order_by(NiftyCandle.open_time.desc()).limit(max(1, limit))
        rows = list(self.db.scalars(statement))
        rows.reverse()
        return rows

    def option_candles(
        self,
        instrument_key: str,
        decision_timestamp: datetime | str,
        *,
        interval: str = "3min",
        since: datetime | str | None = None,
        limit: int = 10000,
    ) -> list[OptionCandle]:
        """Return completed option candles available by the decision time."""
        cutoff = _require_cutoff(decision_timestamp)
        statement = _with_completed_candle_cutoff(
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
        statement = statement.order_by(OptionCandle.open_time.desc()).limit(max(1, limit))
        rows = list(self.db.scalars(statement))
        rows.reverse()
        return rows

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
            statement.order_by(IVObservation.observed_at.desc())
            .limit(max(1, limit))
        )
        rows = list(self.db.scalars(statement))
        rows.reverse()
        return rows

    def option_greeks(
        self,
        instrument_key: str,
        decision_timestamp: datetime | str,
        *,
        interval: str = "3min",
        calc_version: str = DEFAULT_GREEKS_CALC_VERSION,
        limit: int = 10000,
    ) -> list[OptionGreeks]:
        """Return completed reconstructed Greeks available by the decision time."""
        cutoff = _require_cutoff(decision_timestamp)
        statement = _with_completed_candle_cutoff(
            select(OptionGreeks).where(
                OptionGreeks.instrument_key == instrument_key,
                OptionGreeks.interval == interval,
                OptionGreeks.status == "SUCCESS",
            ),
            OptionGreeks.open_time,
            cutoff,
        )
        statement = statement.where(OptionGreeks.calc_version == calc_version)
        statement = (
            statement.order_by(OptionGreeks.open_time.desc())
            .limit(max(1, limit))
        )
        rows = list(self.db.scalars(statement))
        rows.reverse()
        return rows

    def nifty_candles_at(
        self,
        decision_timestamp: datetime | str,
        *,
        symbol: str = "NIFTY",
        interval: str = "3min",
    ) -> list[NiftyCandle]:
        """Return the completed NIFTY candle immediately preceding the decision time."""
        cutoff = _require_cutoff(decision_timestamp)
        target = _completed_bar_open_time(cutoff, interval)
        statement = (
            select(NiftyCandle)
            .where(
                NiftyCandle.symbol == symbol.upper(),
                NiftyCandle.interval == interval,
                NiftyCandle.open_time == target,
            )
        )
        return list(self.db.scalars(statement))

    def option_candles_at(
        self,
        decision_timestamp: datetime | str,
        *,
        instrument_keys: list[str] | None = None,
        interval: str = "3min",
    ) -> list[OptionCandle]:
        """Return completed option candles available immediately before the decision time."""
        cutoff = _require_cutoff(decision_timestamp)
        target = _completed_bar_open_time(cutoff, interval)
        statement = select(OptionCandle).where(
            OptionCandle.interval == interval,
            OptionCandle.open_time == target,
        )
        if instrument_keys:
            statement = statement.where(OptionCandle.instrument_key.in_(instrument_keys))
        statement = statement.order_by(OptionCandle.open_time.desc())
        return list(self.db.scalars(statement))

    def option_greeks_at(
        self,
        decision_timestamp: datetime | str,
        *,
        instrument_keys: list[str] | None = None,
        interval: str = "3min",
        calc_version: str = DEFAULT_GREEKS_CALC_VERSION,
    ) -> list[OptionGreeks]:
        """Return completed reconstructed Greeks available before the decision time."""
        cutoff = _require_cutoff(decision_timestamp)
        target = _completed_bar_open_time(cutoff, interval)
        statement = select(OptionGreeks).where(
            OptionGreeks.interval == interval,
            OptionGreeks.open_time == target,
            OptionGreeks.status == "SUCCESS",
        )
        if instrument_keys:
            statement = statement.where(OptionGreeks.instrument_key.in_(instrument_keys))
        statement = statement.where(OptionGreeks.calc_version == (calc_version or DEFAULT_GREEKS_CALC_VERSION))
        statement = statement.order_by(OptionGreeks.open_time.desc())
        return list(self.db.scalars(statement))

    def option_greeks_at_many(
        self,
        decision_timestamps: list[datetime | str],
        *,
        interval: str = "3min",
        calc_version: str = DEFAULT_GREEKS_CALC_VERSION,
    ) -> list[OptionGreeks]:
        """Return completed Greeks for supplied decision timestamps."""
        cutoffs = [_require_cutoff(ts) for ts in decision_timestamps]
        if not cutoffs:
            return []
        rows: list[OptionGreeks] = []
        for offset in range(0, len(cutoffs), 500):
            chunk = [_completed_bar_open_time(ts, interval) for ts in cutoffs[offset:offset + 500]]
            statement = select(OptionGreeks).where(
                OptionGreeks.interval == interval,
                OptionGreeks.open_time.in_(chunk),
                OptionGreeks.status == "SUCCESS",
            )
            statement = statement.where(OptionGreeks.calc_version == calc_version)
            rows.extend(self.db.scalars(statement))
        return rows

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
        rows: list[OptionCandle] = []
        for offset in range(0, len(cutoffs), 500):
            chunk = [_completed_bar_open_time(ts, interval) for ts in cutoffs[offset:offset + 500]]
            statement = select(OptionCandle).where(
                OptionCandle.interval == interval,
                OptionCandle.open_time.in_(chunk),
            )
            if instrument_keys:
                statement = statement.where(OptionCandle.instrument_key.in_(instrument_keys))
            rows.extend(self.db.scalars(statement))
        return rows

    def historical_gex_at(
        self,
        decision_timestamp: datetime | str,
        *,
        interval: str = "3min",
        calc_version: str = "h_gex_v1",
        successful_only: bool = True,
    ) -> list[HistoricalGexSnapshot]:
        """Return completed historical GEX whose source candle precedes T."""
        cutoff = _require_cutoff(decision_timestamp)
        target = _completed_bar_open_time(cutoff, interval)
        statement = select(HistoricalGexSnapshot).where(
            HistoricalGexSnapshot.interval == interval,
            HistoricalGexSnapshot.open_time == target,
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
        """Return completed historical GEX available by the decision time."""
        cutoff = _require_cutoff(decision_timestamp)
        statement = _with_completed_candle_cutoff(
            select(HistoricalGexSnapshot).where(
                HistoricalGexSnapshot.instrument_key == instrument_key,
                HistoricalGexSnapshot.interval == interval,
                HistoricalGexSnapshot.calc_version == calc_version,
                HistoricalGexSnapshot.status == "SUCCESS",
            ),
            HistoricalGexSnapshot.open_time,
            cutoff,
        )
        statement = statement.order_by(HistoricalGexSnapshot.open_time.desc()).limit(max(1, limit))
        rows = list(self.db.scalars(statement))
        rows.reverse()
        return rows
