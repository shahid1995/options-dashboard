"""Point-in-time access to historical market data for backtests and research.

The interface centralizes the temporal invariant that backtesting depends on.

Point observations (such as IV quotes) are available when their observation
timestamp is <= the decision time. Candle-derived rows are available only after
their interval has completed, so only the latest candle whose full interval
has completed by the decision time is eligible. Forward labels are intentionally outside this interface.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import Select, func, select
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


def _with_completed_candle_cutoff(
    statement: Select,
    column,
    cutoff: datetime,
    interval: str,
) -> Select:
    """Apply the PIT predicate for fully completed interval-derived candles."""
    completed_through = _completed_bar_open_time(cutoff, interval)
    return statement.where(column <= completed_through)


def _completed_bar_open_time(decision_timestamp: datetime, interval: str) -> datetime:
    """Return the latest fully completed candle's open time at a decision cutoff."""
    # The persisted candle intervals are the repository's ``VALID_INTERVALS``
    # set (``app/services/nifty_candles.py``, ``option_candles.py``,
    # ``app/routers/candles.py``): 1min, 3min, 5min, 15min, 30min, 1hour, 1day.
    # The map previously recognized a bare "day" and omitted "1hour", so a PIT
    # read against valid persisted 1hour/1day rows raised ValueError
    # (Day 49 correction). "day" is retained only as a legacy alias.
    durations = {
        "1min": 60,
        "3min": 180,
        "5min": 300,
        "15min": 900,
        "30min": 1800,
        "1hour": 3600,
        "1day": 86400,
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
            interval,
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
            interval,
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
            interval,
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
                NiftyCandle.open_time <= target,
            )
            .order_by(NiftyCandle.open_time.desc())
            .limit(1)
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
        if instrument_keys is not None and not instrument_keys:
            return []

        # Grouped greatest-n-per-group selection (Codacy HIGH, Day 49):
        # one GROUP BY/max-join — no correlated scalar MAX. The
        # eligibility predicates apply inside the aggregation, and the
        # outer scan re-applies them (plus the explicit target bound) so
        # the selected row is itself eligible. Uniqueness of
        # (instrument_key, interval, open_time) makes the join-back
        # unambiguous.
        latest_opens = (
            select(
                OptionCandle.instrument_key.label("latest_instrument_key"),
                func.max(OptionCandle.open_time).label("latest_open_time"),
            )
            .where(
                OptionCandle.interval == interval,
                OptionCandle.open_time <= target,
                *(
                    [OptionCandle.instrument_key.in_(instrument_keys)]
                    if instrument_keys is not None
                    else []
                ),
            )
            .group_by(OptionCandle.instrument_key)
            .subquery()
        )

        statement = select(OptionCandle).where(
            OptionCandle.interval == interval,
            OptionCandle.open_time <= target,
            OptionCandle.instrument_key == latest_opens.c.latest_instrument_key,
            OptionCandle.open_time == latest_opens.c.latest_open_time,
        )
        statement = statement.order_by(OptionCandle.instrument_key)
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
        if instrument_keys is not None and not instrument_keys:
            return []

        version = calc_version or DEFAULT_GREEKS_CALC_VERSION
        # Grouped greatest-n-per-group selection (Codacy HIGH, Day 49):
        # one GROUP BY/max-join — no correlated scalar MAX. Eligibility
        # predicates apply inside the aggregation and are mirrored on the
        # outer scan (plus the explicit target bound); uniqueness of
        # (instrument_key, interval, open_time, calc_version) makes the
        # join-back unambiguous.
        latest_opens = (
            select(
                OptionGreeks.instrument_key.label("latest_instrument_key"),
                func.max(OptionGreeks.open_time).label("latest_open_time"),
            )
            .where(
                OptionGreeks.interval == interval,
                OptionGreeks.open_time <= target,
                OptionGreeks.status == "SUCCESS",
                OptionGreeks.calc_version == version,
                *(
                    [OptionGreeks.instrument_key.in_(instrument_keys)]
                    if instrument_keys is not None
                    else []
                ),
            )
            .group_by(OptionGreeks.instrument_key)
            .subquery()
        )

        statement = select(OptionGreeks).where(
            OptionGreeks.interval == interval,
            OptionGreeks.open_time <= target,
            OptionGreeks.status == "SUCCESS",
            OptionGreeks.calc_version == version,
            OptionGreeks.instrument_key == latest_opens.c.latest_instrument_key,
            OptionGreeks.open_time == latest_opens.c.latest_open_time,
        )
        statement = statement.order_by(OptionGreeks.instrument_key)
        return list(self.db.scalars(statement))

    def _load_bounded_histories(
        self,
        model,
        targets: list[datetime],
        histories: dict[str, list[tuple[datetime, object]]],
        eligibility_filters: Callable[[type], list],
    ) -> None:
        """CodeRabbit #3: bounded bulk history load.

        Loads exactly what the selection algorithm can use — no more:

        1. the latest eligible row per instrument at or before
           ``lower = min(targets)`` (the fallback seed for the earliest
           decision), via one portable GROUP BY/max-join bulk query —
           the eligibility filters apply inside the grouped aggregation so
           the seed timestamp always comes from an eligible row;
        2. all eligible rows for the relevant instruments in the window
           ``(lower, upper]`` via one bulk query.

        Rows strictly below the seed or above ``upper`` can never be
        selected and are never fetched.  Histories are appended per
        instrument in ascending ``open_time`` order (seed row first, then
        window rows), matching the in-memory index the binary-search
        resolver expects.  Selection semantics are identical to the
        previous ``open_time <= max(targets)`` load; only how much history
        is materialized changes.
        """
        if not targets:
            return
        lower = min(targets)
        upper = max(targets)

        # 1. Fallback seed: the latest eligible row per instrument at or
        #    before the earliest target — one portable grouped max-join bulk
        #    query (Greptile P2). The grouped subquery applies the eligibility
        #    filters inside the aggregation, so the seed timestamp always
        #    comes from an eligible row; the join back on both instrument
        #    identity and max open_time materializes exactly one seed row per
        #    instrument, and the outer scan itself carries the explicit
        #    open_time <= lower bound so it can never consider rows newer
        #    than the earliest requested target.
        seed_opens = (
            select(
                model.instrument_key.label("seed_instrument_key"),
                func.max(model.open_time).label("seed_open_time"),
            )
            .where(
                model.open_time <= lower,
                *eligibility_filters(model),
            )
            .group_by(model.instrument_key)
            .subquery()
        )
        seed_statement = select(model).where(
            model.open_time <= lower,
            model.instrument_key == seed_opens.c.seed_instrument_key,
            model.open_time == seed_opens.c.seed_open_time,
            *eligibility_filters(model),
        )
        for row in self.db.scalars(seed_statement):
            histories.setdefault(row.instrument_key, []).append(
                (row.open_time, row))

        # 2. Window rows strictly after the floor up to the latest target
        #    (one bulk query).
        window_statement = select(model).where(
            model.open_time > lower,
            model.open_time <= upper,
            *eligibility_filters(model),
        )
        for row in self.db.scalars(window_statement.order_by(
            model.instrument_key, model.open_time
        )):
            histories.setdefault(row.instrument_key, []).append(
                (row.open_time, row))

    @staticmethod
    def _resolve_bulk_selections(
        histories: dict[str, list[tuple[datetime, object]]],
        targets: list[datetime],
    ) -> list[dict[str, object]]:
        """Pair each decision target with its selected source rows.

        ``histories`` maps each instrument to its (open_time, row) history
        sorted ascending. The result is decision-major: element i belongs to
        ``targets[i]`` and maps each instrument to its selected row. A fallback
        bar is selected with its ORIGINAL source open_time — it is never
        relabeled to the requesting decision time. Histories are indexed once
        and probed with binary search, so a bulk request traverses each
        instrument's history a single time.
        """
        per_instrument_opens = {
            instrument_key: [open_time for open_time, _ in history]
            for instrument_key, history in histories.items()
        }
        sorted_instruments = sorted(per_instrument_opens)

        selections: list[dict[str, object]] = []
        for target in targets:
            selection: dict[str, object] = {}
            for instrument_key in sorted_instruments:
                opens = per_instrument_opens[instrument_key]
                index = bisect_right(opens, target) - 1
                if index >= 0:
                    selection[instrument_key] = histories[instrument_key][index][1]
            selections.append(selection)
        return selections

    def option_greeks_selections_at_many(
        self,
        decision_timestamps: list[datetime | str],
        *,
        instrument_keys: list[str] | None = None,
        interval: str = "3min",
        calc_version: str = DEFAULT_GREEKS_CALC_VERSION,
        successful_only: bool = True,
    ) -> list[tuple[datetime, dict[str, OptionGreeks]]]:
        """Pair each decision timestamp with its selected Greeks per instrument.

        Element i is ``(decision_timestamps[i], {instrument_key: row})`` where
        row is the latest completed row (of the requested calc version)
        available by that decision. By default only ``SUCCESS`` rows are
        eligible; ``successful_only=False`` widens the selection to rows whose
        implied-volatility solve did not succeed, which consumers use purely
        for contract identity/metadata (option type, expiry) without letting a
        non-SUCCESS row stand in for a Greek value. A missing entry means no
        completed row existed for that pair yet. When no fresh bar exists, the
        latest earlier completed bar is selected with its original source
        ``open_time`` intact, so consumers can distinguish the requested
        decision timestamp from the source observation timestamp.
        """
        cutoffs = [_require_cutoff(ts) for ts in decision_timestamps]
        if not cutoffs:
            return []
        if instrument_keys is not None and not instrument_keys:
            return []

        targets = [_completed_bar_open_time(cutoff, interval) for cutoff in cutoffs]

        def _greeks_eligibility(entity):
            predicates = [entity.interval == interval]
            if successful_only:
                predicates.append(entity.status == "SUCCESS")
            predicates.append(
                entity.calc_version
                == (calc_version or DEFAULT_GREEKS_CALC_VERSION)
            )
            if instrument_keys is not None:
                predicates.append(
                    entity.instrument_key.in_(instrument_keys))
            return predicates

        histories: dict[str, list[tuple[datetime, OptionGreeks]]] = {}
        self._load_bounded_histories(
            OptionGreeks, targets, histories, _greeks_eligibility)

        return list(
            zip(cutoffs, self._resolve_bulk_selections(histories, targets))
        )

    def option_candles_selections_at_many(
        self,
        decision_timestamps: list[datetime | str],
        *,
        instrument_keys: list[str] | None = None,
        interval: str = "3min",
    ) -> list[tuple[datetime, dict[str, OptionCandle]]]:
        """Pair each decision timestamp with its selected candles per instrument.

        Element i is ``(decision_timestamps[i], {instrument_key: row})`` where
        row is the latest completed candle available by that decision. A
        fallback bar keeps its original source ``open_time`` — consumers must
        attribute it to that source observation time, never to the requesting
        decision time.
        """
        cutoffs = [_require_cutoff(ts) for ts in decision_timestamps]
        if not cutoffs:
            return []
        if instrument_keys is not None and not instrument_keys:
            return []

        targets = [_completed_bar_open_time(cutoff, interval) for cutoff in cutoffs]

        def _candle_eligibility(entity):
            predicates = [entity.interval == interval]
            if instrument_keys is not None:
                predicates.append(
                    entity.instrument_key.in_(instrument_keys))
            return predicates

        histories: dict[str, list[tuple[datetime, OptionCandle]]] = {}
        self._load_bounded_histories(
            OptionCandle, targets, histories, _candle_eligibility)

        return list(
            zip(cutoffs, self._resolve_bulk_selections(histories, targets))
        )

    def option_greeks_at_many(
        self,
        decision_timestamps: list[datetime | str],
        *,
        instrument_keys: list[str] | None = None,
        interval: str = "3min",
        calc_version: str = DEFAULT_GREEKS_CALC_VERSION,
        successful_only: bool = True,
    ) -> list[OptionGreeks]:
        """Return completed Greeks paired with the supplied decision timestamps.

        Element i is the latest completed row (of the requested calc version)
        for decision_timestamps[i], one entry per instrument; by default only
        ``SUCCESS`` rows are eligible. When no fresh bar exists by a decision
        time, the latest earlier completed bar is returned with its original
        source ``open_time`` intact.
        """
        cutoffs = [_require_cutoff(ts) for ts in decision_timestamps]
        if not cutoffs:
            return []

        selections = self.option_greeks_selections_at_many(
            decision_timestamps,
            instrument_keys=instrument_keys,
            interval=interval,
            calc_version=calc_version,
            successful_only=successful_only,
        )
        return [
            row
            for _, selection in selections
            for _, row in sorted(selection.items())
        ]

    def option_candles_at_many(
        self,
        decision_timestamps: list[datetime | str],
        *,
        instrument_keys: list[str] | None = None,
        interval: str = "3min",
    ) -> list[OptionCandle]:
        """Return option candles paired with the supplied decision timestamps.

        Element i is the latest completed candle for decision_timestamps[i],
        one entry per instrument. When no fresh bar exists by a decision time,
        the latest earlier completed bar is        returned with its original source
        ``open_time`` intact — consumers must attribute it to that source
        observation time, never to the requesting decision time.
        """
        cutoffs = [_require_cutoff(ts) for ts in decision_timestamps]
        if not cutoffs:
            return []

        selections = self.option_candles_selections_at_many(
            decision_timestamps,
            instrument_keys=instrument_keys,
            interval=interval,
        )
        return [
            row
            for _, selection in selections
            for _, row in sorted(selection.items())
        ]

    def historical_gex_at(
        self,
        decision_timestamp: datetime | str,
        *,
        interval: str = "3min",
        calc_version: str = "h_gex_v1",
        successful_only: bool = True,
    ) -> list[HistoricalGexSnapshot]:
        """Return completed historical GEX whose source candle precedes T.

        Selection is per instrument (Codacy Finding A): each instrument
        contributes its own latest eligible snapshot at or before the
        completed-bar target, mirroring ``option_greeks_at``. A global
        maximum would drop every instrument whose freshest eligible
        snapshot predates another instrument's.

        Implementation note (Codacy HIGH, Day 49): the per-instrument
        maximum is a portable GROUP BY / max-join selection — the same
        shape as ``_load_bounded_histories`` — instead of a correlated
        scalar MAX, which re-executes a bounded top-1 probe for every
        scanned row and degrades linearly with history size on
        PostgreSQL and CockroachDB alike.
        """
        cutoff = _require_cutoff(decision_timestamp)
        target = _completed_bar_open_time(cutoff, interval)

        # Grouped greatest-n-per-group selection (Codacy HIGH, Day 49):
        # one GROUP BY/max-join — no correlated scalar MAX. Eligibility
        # predicates apply inside the aggregation and are mirrored on the
        # outer scan (plus the explicit target bound); uniqueness of
        # (instrument_key, interval, open_time, calc_version) makes the
        # join-back unambiguous.
        eligibility = [
            HistoricalGexSnapshot.interval == interval,
            HistoricalGexSnapshot.open_time <= target,
            HistoricalGexSnapshot.calc_version == calc_version,
        ]
        if successful_only:
            eligibility.append(HistoricalGexSnapshot.status == "SUCCESS")
        latest_opens = (
            select(
                HistoricalGexSnapshot.instrument_key.label(
                    "latest_instrument_key"),
                func.max(HistoricalGexSnapshot.open_time).label(
                    "latest_open_time"),
            )
            .where(*eligibility)
            .group_by(HistoricalGexSnapshot.instrument_key)
            .subquery()
        )

        statement = select(HistoricalGexSnapshot).where(
            *eligibility,
            HistoricalGexSnapshot.instrument_key == latest_opens.c.latest_instrument_key,
            HistoricalGexSnapshot.open_time == latest_opens.c.latest_open_time,
        )
        return list(self.db.scalars(statement))

    def historical_gex_observed_at(
        self,
        decision_timestamp: datetime | str,
        observation_time: datetime | str,
        *,
        interval: str = "3min",
        calc_version: str = "h_gex_v1",
        successful_only: bool = True,
    ) -> list[HistoricalGexSnapshot]:
        """Return rows whose source bar is exactly ``observation_time``.

        This is the exact-observation seam for analytics (Codacy
        performance follow-up, Day 49): unlike ``historical_gex_at``
        there is deliberately no per-instrument fallback - an
        instrument without a snapshot at exactly ``observation_time``
        contributes nothing.

        Visibility still follows the PIT contract: the request is
        served only while ``observation_time`` is a completed
        observation by the decision cutoff (``observation_time <=
        _completed_bar_open_time(cutoff, interval)``); otherwise, or
        when no row matches, the result is empty.
        """
        cutoff = _require_cutoff(decision_timestamp)
        observation = _require_cutoff(observation_time)
        target = _completed_bar_open_time(cutoff, interval)
        if observation > target:
            return []

        statement = select(HistoricalGexSnapshot).where(
            HistoricalGexSnapshot.interval == interval,
            HistoricalGexSnapshot.open_time == observation,
            HistoricalGexSnapshot.calc_version == calc_version,
        )
        if successful_only:
            statement = statement.where(
                HistoricalGexSnapshot.status == "SUCCESS")
        statement = statement.order_by(HistoricalGexSnapshot.instrument_key)
        return list(self.db.scalars(statement))

    def historical_gex_selections_at_many(
        self,
        decision_timestamps: list[datetime | str],
        *,
        instrument_keys: list[str] | None = None,
        interval: str = "3min",
        calc_version: str = "h_gex_v1",
        successful_only: bool = True,
    ) -> list[tuple[datetime, dict[str, HistoricalGexSnapshot]]]:
        """Pair each decision timestamp with its selected GEX per instrument.

        Element i is ``(decision_timestamps[i], {instrument_key: row})``
        where row is the latest eligible snapshot at or before that
        decision's completed-bar target — the same per-instrument contract
        as the candle and greeks selections accessors, served by the same
        bounded bulk load. A missing entry means no eligible snapshot
        existed for that pair yet; a fallback snapshot keeps its original
        source ``open_time`` — consumers must attribute it to that source
        observation time, never to the requesting decision time.
        """
        cutoffs = [_require_cutoff(ts) for ts in decision_timestamps]
        if not cutoffs:
            return []
        if instrument_keys is not None and not instrument_keys:
            return []

        targets = [_completed_bar_open_time(cutoff, interval) for cutoff in cutoffs]

        def _gex_eligibility(entity):
            predicates = [
                entity.interval == interval,
                entity.calc_version == calc_version,
            ]
            if successful_only:
                predicates.append(entity.status == "SUCCESS")
            if instrument_keys is not None:
                predicates.append(
                    entity.instrument_key.in_(instrument_keys))
            return predicates

        histories: dict[str, list[tuple[datetime, HistoricalGexSnapshot]]] = {}
        self._load_bounded_histories(
            HistoricalGexSnapshot, targets, histories, _gex_eligibility)

        return list(
            zip(cutoffs, self._resolve_bulk_selections(histories, targets))
        )

    def historical_gex_at_many(
        self,
        decision_timestamps: list[datetime | str],
        *,
        instrument_keys: list[str] | None = None,
        interval: str = "3min",
        calc_version: str = "h_gex_v1",
        successful_only: bool = True,
    ) -> list[HistoricalGexSnapshot]:
        """Return historical GEX paired with the supplied decision timestamps.

        Element i is the latest eligible snapshot for decision_timestamps[i],
        one entry per instrument. When no fresh snapshot exists by a decision
        time, the latest earlier eligible snapshot is returned with its
        original source ``open_time`` intact.
        """
        cutoffs = [_require_cutoff(ts) for ts in decision_timestamps]
        if not cutoffs:
            return []

        selections = self.historical_gex_selections_at_many(
            decision_timestamps,
            instrument_keys=instrument_keys,
            interval=interval,
            calc_version=calc_version,
            successful_only=successful_only,
        )
        return [
            row
            for _, selection in selections
            for _, row in sorted(selection.items())
        ]

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
            interval,
        )
        statement = statement.order_by(HistoricalGexSnapshot.open_time.desc()).limit(max(1, limit))
        rows = list(self.db.scalars(statement))
        rows.reverse()
        return rows
