"""Tests for Historical GEX Analytics Engine — Phase 7.8D.

All tests use isolated in-memory databases. No production DB writes.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sqlalchemy import select
from app.db import Base
from app.models import HistoricalGexSnapshot, NiftyCandle
from app.services.historical_gex_analytics import (
    GexAnalyticsEngine,
    FORWARD_RETURN_INTERVALS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _insert_gex(db, timestamp, strike, option_type, signed_gex, spot, expiry="2024-10-03", calc_version="h_gex_v1", interval="3min"):
    """Insert a historical GEX row."""
    # Use unique instrument_key per strike+type to avoid unique constraint conflicts
    suffix = "CE" if option_type == "CE" else "PE"
    db.add(HistoricalGexSnapshot(
        instrument_key=f"NSE_FO|{int(strike)}{suffix}|{expiry}",
        interval=interval,
        open_time=timestamp,
        spot=spot,
        strike=strike,
        expiry=expiry,
        option_type=option_type,
        gamma=0.001,
        open_interest=1000,
        option_price=100.0,
        lot_size=75,
        raw_gex=abs(signed_gex),
        signed_gex=signed_gex,
        calc_version=calc_version,
        calculated_at=datetime.now(timezone.utc),
        status="SUCCESS",
    ))
    db.commit()


def _insert_nifty(db, timestamp, close, interval="3min"):
    """Insert a NIFTY candle."""
    db.add(NiftyCandle(
        symbol="NIFTY",
        interval=interval,
        open_time=timestamp,
        open=close - 10,
        high=close + 10,
        low=close - 20,
        close=close,
        volume=1000,
    ))
    db.commit()


# ---------------------------------------------------------------------------
# A. Formula/aggregation tests
# ---------------------------------------------------------------------------

class TestAggregation:
    def test_ce_aggregation(self, db):
        ts = datetime(2024, 10, 3, 9, 15)
        _insert_gex(db, ts, 25000, "CE", 1000000.0, 25000)
        _insert_gex(db, ts, 25100, "CE", 500000.0, 25000)
        engine = GexAnalyticsEngine(db)
        agg = engine.aggregate_timestamp(ts)
        assert agg is not None
        assert agg.call_gex == pytest.approx(1500000.0)

    def test_pe_aggregation(self, db):
        ts = datetime(2024, 10, 3, 9, 15)
        _insert_gex(db, ts, 25000, "PE", -800000.0, 25000)
        _insert_gex(db, ts, 24900, "PE", -200000.0, 25000)
        engine = GexAnalyticsEngine(db)
        agg = engine.aggregate_timestamp(ts)
        assert agg is not None
        assert agg.put_gex == pytest.approx(-1000000.0)

    def test_net_gex(self, db):
        ts = datetime(2024, 10, 3, 9, 15)
        _insert_gex(db, ts, 25000, "CE", 1000000.0, 25000)
        _insert_gex(db, ts, 25000, "PE", -800000.0, 25000)
        engine = GexAnalyticsEngine(db)
        agg = engine.aggregate_timestamp(ts)
        assert agg.net_gex == pytest.approx(200000.0)

    def test_absolute_gex(self, db):
        ts = datetime(2024, 10, 3, 9, 15)
        _insert_gex(db, ts, 25000, "PE", -500000.0, 25000)
        engine = GexAnalyticsEngine(db)
        agg = engine.aggregate_timestamp(ts)
        assert agg.absolute_gex == pytest.approx(500000.0)

    def test_strike_aggregation(self, db):
        ts = datetime(2024, 10, 3, 9, 15)
        _insert_gex(db, ts, 25000, "CE", 1000000.0, 25000)
        _insert_gex(db, ts, 25000, "PE", -300000.0, 25000)
        _insert_gex(db, ts, 25100, "CE", 200000.0, 25000)
        engine = GexAnalyticsEngine(db)
        strikes = engine.aggregate_strike(ts)
        assert len(strikes) == 2
        s25000 = next(s for s in strikes if s.strike == 25000)
        assert s25000.net_gex == pytest.approx(700000.0)
        assert s25000.rank == 1  # Highest absolute GEX

    def test_expiry_aggregation(self, db):
        ts = datetime(2024, 10, 3, 9, 15)
        _insert_gex(db, ts, 25000, "CE", 1000000.0, 25000, expiry="2024-10-03")
        _insert_gex(db, ts, 25100, "CE", 500000.0, 25000, expiry="2024-10-10")
        engine = GexAnalyticsEngine(db)
        expiries = engine.aggregate_expiry(ts)
        assert len(expiries) == 2
        assert expiries[0].expiry == "2024-10-03"  # Higher absolute GEX first

    def test_empty_timestamp(self, db):
        engine = GexAnalyticsEngine(db)
        agg = engine.aggregate_timestamp(datetime(2024, 1, 1))
        assert agg is None

    def test_empty_strike_aggregation(self, db):
        engine = GexAnalyticsEngine(db)
        strikes = engine.aggregate_strike(datetime(2024, 1, 1))
        assert strikes == []


# ---------------------------------------------------------------------------
# B. Time series tests
# ---------------------------------------------------------------------------

class TestTimeSeries:
    def test_chronological_ordering(self, db):
        t1 = datetime(2024, 10, 3, 9, 15)
        t2 = datetime(2024, 10, 3, 9, 18)
        _insert_gex(db, t1, 25000, "CE", 1000000.0, 25000)
        _insert_gex(db, t2, 25000, "CE", 1200000.0, 25000)
        engine = GexAnalyticsEngine(db)
        timestamps = engine.get_timestamps()
        assert timestamps == [t1, t2]

    def test_gex_change(self, db):
        t1 = datetime(2024, 10, 3, 9, 15)
        t2 = datetime(2024, 10, 3, 9, 18)
        _insert_gex(db, t1, 25000, "CE", 1000000.0, 25000)
        _insert_gex(db, t2, 25000, "CE", 1200000.0, 25000)
        engine = GexAnalyticsEngine(db)
        agg1 = engine.aggregate_timestamp(t1)
        agg2 = engine.aggregate_timestamp(t2)
        change = engine.compute_gex_change(agg2, agg1)
        assert change.gex_change == pytest.approx(200000.0)

    def test_zero_change(self, db):
        t1 = datetime(2024, 10, 3, 9, 15)
        t2 = datetime(2024, 10, 3, 9, 18)
        _insert_gex(db, t1, 25000, "CE", 1000000.0, 25000)
        _insert_gex(db, t2, 25000, "CE", 1000000.0, 25000)
        engine = GexAnalyticsEngine(db)
        agg1 = engine.aggregate_timestamp(t1)
        agg2 = engine.aggregate_timestamp(t2)
        change = engine.compute_gex_change(agg2, agg1)
        assert change.gex_change == pytest.approx(0.0)

    def test_first_timestamp_no_change(self, db):
        t1 = datetime(2024, 10, 3, 9, 15)
        _insert_gex(db, t1, 25000, "CE", 1000000.0, 25000)
        engine = GexAnalyticsEngine(db)
        agg1 = engine.aggregate_timestamp(t1)
        change = engine.compute_gex_change(agg1, None)
        assert change.gex_change is None
        assert change.previous_net_gex is None

    def test_acceleration(self, db):
        engine = GexAnalyticsEngine(db)
        ch1 = type('GexChange', (), {'gex_change': 100, 'timestamp': datetime(2024, 10, 3, 9, 15)})()
        ch2 = type('GexChange', (), {'gex_change': 250, 'timestamp': datetime(2024, 10, 3, 9, 18)})()
        acc = engine.compute_gex_acceleration(ch2, ch1)
        assert acc.acceleration == pytest.approx(150.0)

    def test_acceleration_first_observation(self, db):
        engine = GexAnalyticsEngine(db)
        ch1 = type('GexChange', (), {'gex_change': 100, 'timestamp': datetime(2024, 10, 3, 9, 15)})()
        acc = engine.compute_gex_acceleration(ch1, None)
        assert acc.acceleration is None

    def test_get_timestamps_with_range(self, db):
        t1 = datetime(2024, 10, 3, 9, 15)
        t2 = datetime(2024, 10, 3, 9, 18)
        t3 = datetime(2024, 10, 3, 9, 21)
        _insert_gex(db, t1, 25000, "CE", 1000000.0, 25000)
        _insert_gex(db, t2, 25000, "CE", 1000000.0, 25000)
        _insert_gex(db, t3, 25000, "CE", 1000000.0, 25000)
        engine = GexAnalyticsEngine(db)
        timestamps = engine.get_timestamps(start=t2)
        assert timestamps == [t2, t3]

    def test_get_timestamps_only_returns_analytics_interval(self, db):
        """Interval contract (Greptile P1, Day 49): the engine is a
        3-minute analytics engine -- DEFAULT_INTERVAL drives every read
        and FORWARD_RETURN_INTERVALS is expressed in 3-minute candles --
        so timestamp discovery must never surface snapshots from another
        interval. A 5-minute snapshot would otherwise be discovered and
        then silently dropped by the exact-observation PIT accessor.
        """
        t_3min = datetime(2024, 10, 3, 9, 15)
        t_5min = datetime(2024, 10, 3, 9, 25)
        _insert_gex(db, t_3min, 25000, "CE", 1000000.0, 25000)
        _insert_gex(db, t_5min, 25000, "CE", 1000000.0, 25000, interval="5min")
        engine = GexAnalyticsEngine(db)

        assert engine.get_timestamps() == [t_3min]
        assert engine.aggregate_timestamp(t_5min) is None

    def test_get_timestamps_interval_contract_is_insert_order_independent(self, db):
        """Inverse insertion order: a 5-minute snapshot inserted before
        any 3-minute row must not change interval-correct discovery."""
        t_5min = datetime(2024, 10, 3, 9, 25)
        t_3min = datetime(2024, 10, 3, 9, 15)
        _insert_gex(db, t_5min, 25000, "CE", 1000000.0, 25000, interval="5min")
        _insert_gex(db, t_3min, 25000, "CE", 1000000.0, 25000)
        engine = GexAnalyticsEngine(db)

        assert engine.get_timestamps() == [t_3min]

    def test_get_timestamps_range_honors_interval_contract(self, db):
        """Ranged discovery applies the same interval predicate."""
        t_3min = datetime(2024, 10, 3, 9, 15)
        t_5min = datetime(2024, 10, 3, 9, 25)
        _insert_gex(db, t_3min, 25000, "CE", 1000000.0, 25000)
        _insert_gex(db, t_5min, 25000, "CE", 1000000.0, 25000, interval="5min")
        engine = GexAnalyticsEngine(db)

        assert engine.get_timestamps(start=t_3min) == [t_3min]


# ---------------------------------------------------------------------------
# C. Regime tests
# ---------------------------------------------------------------------------

class TestRegime:
    def test_positive_gamma(self, db):
        engine = GexAnalyticsEngine(db)
        assert engine.classify_regime(1000000.0) == "POSITIVE_GAMMA"

    def test_negative_gamma(self, db):
        engine = GexAnalyticsEngine(db)
        assert engine.classify_regime(-1000000.0) == "NEGATIVE_GAMMA"

    def test_neutral(self, db):
        engine = GexAnalyticsEngine(db)
        assert engine.classify_regime(0.0) == "NEUTRAL"

    def test_transitions(self, db):
        t1 = datetime(2024, 10, 3, 9, 15)
        t2 = datetime(2024, 10, 3, 9, 18)
        t3 = datetime(2024, 10, 3, 9, 21)
        _insert_gex(db, t1, 25000, "PE", -1000000.0, 25000)  # NEGATIVE
        _insert_gex(db, t2, 25000, "CE", 500000.0, 25000)    # POSITIVE
        _insert_gex(db, t3, 25000, "CE", 500000.0, 25000)    # POSITIVE
        engine = GexAnalyticsEngine(db)
        aggs = engine.aggregate_timestamps_bulk([t1, t2, t3])
        regimes = engine.compute_regime_series(aggs)
        assert regimes[0].regime == "NEGATIVE_GAMMA"
        assert regimes[1].regime == "POSITIVE_GAMMA"
        assert regimes[1].regime_transition == "NEGATIVE_GAMMA→POSITIVE_GAMMA"
        assert regimes[2].regime == "POSITIVE_GAMMA"
        assert regimes[2].regime_duration == 2

    def test_persistence(self, db):
        t1 = datetime(2024, 10, 3, 9, 15)
        t2 = datetime(2024, 10, 3, 9, 18)
        t3 = datetime(2024, 10, 3, 9, 21)
        _insert_gex(db, t1, 25000, "CE", 500000.0, 25000)
        _insert_gex(db, t2, 25000, "CE", 500000.0, 25000)
        _insert_gex(db, t3, 25000, "CE", 500000.0, 25000)
        engine = GexAnalyticsEngine(db)
        aggs = engine.aggregate_timestamps_bulk([t1, t2, t3])
        regimes = engine.compute_regime_series(aggs)
        assert regimes[2].regime_duration == 3
        assert regimes[2].regime_transition is None


# ---------------------------------------------------------------------------
# D. Gamma flip tests
# ---------------------------------------------------------------------------

class TestGammaFlip:
    def test_sign_crossing(self, db):
        ts = datetime(2024, 10, 3, 9, 15)
        _insert_gex(db, ts, 24900, "PE", -1000000.0, 25000)
        _insert_gex(db, ts, 25100, "CE", 1000000.0, 25000)
        engine = GexAnalyticsEngine(db)
        flip = engine.detect_gamma_flip(ts)
        assert flip.status == "ESTIMATED"
        assert flip.flip_strike is not None
        assert 24900 < flip.flip_strike < 25100

    def test_no_crossing(self, db):
        ts = datetime(2024, 10, 3, 9, 15)
        _insert_gex(db, ts, 25000, "CE", 1000000.0, 25000)
        _insert_gex(db, ts, 25100, "CE", 500000.0, 25000)
        engine = GexAnalyticsEngine(db)
        flip = engine.detect_gamma_flip(ts)
        assert flip.status == "NO_CROSSING"

    def test_insufficient_data(self, db):
        engine = GexAnalyticsEngine(db)
        flip = engine.detect_gamma_flip(datetime(2024, 1, 1))
        assert flip.status == "INSUFFICIENT_DATA"

    def test_multiple_sign_changes(self, db):
        ts = datetime(2024, 10, 3, 9, 15)
        _insert_gex(db, ts, 24800, "PE", -500000.0, 25000)
        _insert_gex(db, ts, 24900, "CE", 300000.0, 25000)
        _insert_gex(db, ts, 25100, "PE", -200000.0, 25000)
        _insert_gex(db, ts, 25200, "CE", 400000.0, 25000)
        engine = GexAnalyticsEngine(db)
        flip = engine.detect_gamma_flip(ts)
        assert flip.num_sign_changes >= 2
        assert flip.flip_strike is not None


# ---------------------------------------------------------------------------
# E. Gamma walls tests
# ---------------------------------------------------------------------------

class TestGammaWalls:
    def test_positive_wall(self, db):
        ts = datetime(2024, 10, 3, 9, 15)
        _insert_gex(db, ts, 25000, "CE", 5000000.0, 25000)
        _insert_gex(db, ts, 25100, "CE", 1000000.0, 25000)
        engine = GexAnalyticsEngine(db)
        walls = engine.detect_walls(ts)
        assert walls.strongest_positive is not None
        assert walls.strongest_positive.strike == 25000
        assert walls.strongest_positive.gex == pytest.approx(5000000.0)

    def test_negative_wall(self, db):
        ts = datetime(2024, 10, 3, 9, 15)
        _insert_gex(db, ts, 24900, "PE", -3000000.0, 25000)
        _insert_gex(db, ts, 24800, "PE", -500000.0, 25000)
        engine = GexAnalyticsEngine(db)
        walls = engine.detect_walls(ts)
        assert walls.strongest_negative is not None
        assert walls.strongest_negative.strike == 24900

    def test_distance_from_spot(self, db):
        ts = datetime(2024, 10, 3, 9, 15)
        _insert_gex(db, ts, 25500, "CE", 5000000.0, 25000)
        engine = GexAnalyticsEngine(db)
        walls = engine.detect_walls(ts)
        assert walls.strongest_positive.distance_from_spot == pytest.approx(500.0)
        assert walls.strongest_positive.distance_pct == pytest.approx(2.0)

    def test_wall_movement(self, db):
        t1 = datetime(2024, 10, 3, 9, 15)
        t2 = datetime(2024, 10, 3, 9, 18)
        _insert_gex(db, t1, 25000, "CE", 5000000.0, 25000)
        _insert_gex(db, t2, 25100, "CE", 5000000.0, 25000)
        engine = GexAnalyticsEngine(db)
        walls1 = engine.detect_walls(t1)
        walls2 = engine.detect_walls(t2, previous_walls=walls1)
        assert walls2.wall_movement is not None
        assert walls2.wall_movement["positive_strike_change"] == pytest.approx(100.0)

    def test_top_n_configurable(self, db):
        ts = datetime(2024, 10, 3, 9, 15)
        for strike in range(24800, 25300, 100):  # 5 strikes: 24800-25200
            _insert_gex(db, ts, strike, "CE", 100000.0, 25000)
        engine = GexAnalyticsEngine(db)
        walls = engine.detect_walls(ts, top_n=5)
        assert len(walls.positive_walls) == 5

    def test_wall_spot_uses_exact_observation_timestamp(self, db):
        """Mixed-time wall-spot regression (CodeRabbit): strike_data is
        restricted to the observation timestamp, but the spot lookup took
        the first row of the per-instrument-fallback PIT accessor, so a
        stale fallback row's spot could drive wall distances. The wall
        spot must come from an exact ``open_time == ts`` row."""
        ts = datetime(2024, 10, 3, 9, 15)
        stale = datetime(2024, 10, 3, 9, 12)
        # Stale PE row (inserted first): no 9:15 PE row exists, so the
        # per-instrument fallback returns this 9:12 row with spot 24100.
        _insert_gex(db, stale, 24900, "PE", -3000000.0, 24100)
        # Exact 9:15 CE row supplies the true current spot 25000.
        _insert_gex(db, ts, 25500, "CE", 5000000.0, 25000)

        engine = GexAnalyticsEngine(db)
        walls = engine.detect_walls(ts)

        # Strike from the exact 9:15 observation; distance measured from
        # the exact 9:15 spot (25500 - 25000), never the stale 24100 spot.
        assert walls.strongest_positive.strike == 25500
        assert walls.spot == pytest.approx(25000.0)
        assert walls.strongest_positive.distance_from_spot == pytest.approx(500.0)
        assert walls.strongest_positive.distance_pct == pytest.approx(2.0)

    def test_wall_spot_without_exact_rows_stays_empty(self, db):
        """No exact-timestamp rows: the stale fallback row must not become
        the wall spot for timestamp T."""
        stale = datetime(2024, 10, 3, 9, 12)
        _insert_gex(db, stale, 24900, "PE", -3000000.0, 24100)

        engine = GexAnalyticsEngine(db)
        walls = engine.detect_walls(datetime(2024, 10, 3, 9, 15))

        # No 9:15 strike data at all: existing empty-result behavior.
        assert walls.strongest_positive is None
        assert walls.strongest_negative is None
        assert walls.spot == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# F. Forward returns tests
# ---------------------------------------------------------------------------

class TestForwardReturns:
    def test_correct_timestamp_alignment(self, db):
        t1 = datetime(2024, 10, 3, 9, 15)
        t2 = datetime(2024, 10, 3, 9, 18)
        t3 = datetime(2024, 10, 3, 9, 21)
        _insert_nifty(db, t1, 25000)
        _insert_nifty(db, t2, 25100)
        _insert_nifty(db, t3, 25200)
        engine = GexAnalyticsEngine(db)
        all_candles = engine.db.execute(select(NiftyCandle).order_by(NiftyCandle.open_time)).scalars().all()
        fr = engine.compute_forward_returns(t1, 25000, all_candles)
        assert 1 in fr.returns  # 3min return

    def test_no_future_leakage(self, db):
        t1 = datetime(2024, 10, 3, 9, 15)
        _insert_nifty(db, t1, 25000)
        engine = GexAnalyticsEngine(db)
        nifty = [engine.db.execute(select(NiftyCandle)).scalars().all()[0]]
        fr = engine.compute_forward_returns(t1, 25000, nifty)
        assert len(fr.returns) == 0  # No future candles

    def test_unavailable_future_data(self, db):
        engine = GexAnalyticsEngine(db)
        fr = engine.compute_forward_returns(datetime(2024, 1, 1), 25000, [])
        assert fr.returns == {}
        assert fr.max_favorable is None

    def test_max_favorable_and_adverse(self, db):
        t1 = datetime(2024, 10, 3, 9, 15)
        t2 = datetime(2024, 10, 3, 9, 18)
        t3 = datetime(2024, 10, 3, 9, 21)
        _insert_nifty(db, t1, 25000)
        _insert_nifty(db, t2, 25100)  # Up
        _insert_nifty(db, t3, 24900)  # Down
        engine = GexAnalyticsEngine(db)
        candles = [r[0] for r in engine.db.execute(select(NiftyCandle).order_by(NiftyCandle.open_time)).all()]
        fr = engine.compute_forward_returns(t1, 25000, candles)
        assert fr.max_favorable is not None
        assert fr.max_adverse is not None


# ---------------------------------------------------------------------------
# G. Statistical analysis tests
# ---------------------------------------------------------------------------

class TestStatistics:
    def test_basic_stats(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        stats = GexAnalyticsEngine.compute_stats(values)
        assert stats.count == 5
        assert stats.mean == pytest.approx(3.0)
        assert stats.median == pytest.approx(3.0)
        assert stats.win_pct == pytest.approx(100.0)  # all positive

    def test_empty_values(self):
        stats = GexAnalyticsEngine.compute_stats([])
        assert stats.count == 0

    def test_all_positive(self):
        values = [1.0, 2.0, 3.0]
        stats = GexAnalyticsEngine.compute_stats(values)
        assert stats.win_pct == pytest.approx(100.0)

    def test_all_negative(self):
        values = [-1.0, -2.0, -3.0]
        stats = GexAnalyticsEngine.compute_stats(values)
        assert stats.win_pct == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# H. Version isolation tests
# ---------------------------------------------------------------------------

class TestVersionIsolation:
    def test_different_version_excluded(self, db):
        ts = datetime(2024, 10, 3, 9, 15)
        _insert_gex(db, ts, 25000, "CE", 1000000.0, 25000, calc_version="h_gex_v1")
        _insert_gex(db, ts, 25000, "CE", 2000000.0, 25000, calc_version="h_gex_v2")
        engine_v1 = GexAnalyticsEngine(db, calc_version="h_gex_v1")
        engine_v2 = GexAnalyticsEngine(db, calc_version="h_gex_v2")
        agg1 = engine_v1.aggregate_timestamp(ts)
        agg2 = engine_v2.aggregate_timestamp(ts)
        assert agg1.call_gex == pytest.approx(1000000.0)
        assert agg2.call_gex == pytest.approx(2000000.0)


# ---------------------------------------------------------------------------
# I. Full pipeline integration test
# ---------------------------------------------------------------------------

class TestFullPipeline:
    def test_build_price_gex_series(self, db):
        """Test the complete pipeline with multiple timestamps."""
        times = [datetime(2024, 10, 3, 9, 15) + timedelta(minutes=3 * i) for i in range(5)]

        # Insert GEX data
        for t in times:
            _insert_gex(db, t, 24900, "PE", -500000.0, 25000)
            _insert_gex(db, t, 25000, "CE", 800000.0, 25000)
            _insert_gex(db, t, 25100, "CE", 300000.0, 25000)

        # Insert NIFTY candles
        for i, t in enumerate(times):
            _insert_nifty(db, t, 25000 + i * 10)
        # Add some future candles for forward returns
        for i in range(5):
            _insert_nifty(db, times[-1] + timedelta(minutes=3 * (i + 1)), 25000 + 50 + i * 5)

        engine = GexAnalyticsEngine(db)
        series = engine.build_price_gex_series()

        assert len(series) == 5
        # First point has no change
        assert series[0].gex_change is None
        # Subsequent points have changes
        assert series[1].gex_change is not None
        # All have regime
        for point in series:
            assert point.gamma_regime in ("POSITIVE_GAMMA", "NEGATIVE_GAMMA", "NEUTRAL")

    def test_analyze_by_regime(self, db):
        """Test regime-based analysis."""
        from app.services.historical_gex_analytics import PriceGexRelationship

        series = [
            PriceGexRelationship(
                timestamp=datetime(2024, 10, 3, 9, 15),
                spot=25000, net_gex=100000, gamma_regime="POSITIVE_GAMMA",
                spot_return_15m=0.5,
            ),
            PriceGexRelationship(
                timestamp=datetime(2024, 10, 3, 9, 18),
                spot=25010, net_gex=-100000, gamma_regime="NEGATIVE_GAMMA",
                spot_return_15m=-0.3,
            ),
        ]
        engine = GexAnalyticsEngine(db)
        result = engine.analyze_by_regime(series)
        assert "POSITIVE_GAMMA" in result
        assert "NEGATIVE_GAMMA" in result
        assert result["POSITIVE_GAMMA"].win_pct == pytest.approx(100.0)
        assert result["NEGATIVE_GAMMA"].win_pct == pytest.approx(0.0)
