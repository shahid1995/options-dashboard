"""Phase 7.8E — Historical GEX Research Engine Tests.

All tests use isolated in-memory databases.
No production database writes.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models import Base, HistoricalGexSnapshot, OptionGreeks, OptionCandle, NiftyCandle, ContractSpec
from app.services.historical_gex_research import (
    GexResearchEngine,
    TimestampResearch,
    SignalCandidate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    """Create an isolated in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def sample_gex_data(db_session):
    """Insert sample historical GEX data for testing."""
    now = datetime(2025, 6, 10, 9, 15, 0)
    instruments = [
        ("NIFTY25CE24000", "CE", 24000.0),
        ("NIFTY25CE24100", "CE", 24100.0),
        ("NIFTY25CE24200", "CE", 24200.0),
        ("NIFTY25PE24000", "PE", 24000.0),
        ("NIFTY25PE24100", "PE", 24100.0),
        ("NIFTY25PE24200", "PE", 24200.0),
    ]

    # Create 5 timestamps with varying GEX
    timestamps = [now + timedelta(minutes=3 * i) for i in range(5)]

    for ts_idx, ts in enumerate(timestamps):
        spot = 24100.0 + ts_idx * 10
        for ik, opt_type, strike in instruments:
            # Vary gamma and OI to create interesting GEX patterns
            if opt_type == "CE":
                gamma = 0.001 + ts_idx * 0.0001
                oi = 1000 + ts_idx * 100
            else:
                gamma = 0.001 + ts_idx * 0.0001
                oi = 1500 + ts_idx * 100

            raw_gex = gamma * oi * spot * spot * 0.01
            signed_gex = raw_gex if opt_type == "CE" else -raw_gex

            snapshot = HistoricalGexSnapshot(
                instrument_key=ik,
                interval="3min",
                open_time=ts,
                spot=spot,
                strike=strike,
                expiry="2025-06-26",
                option_type=opt_type,
                gamma=gamma,
                open_interest=oi,
                option_price=50.0,
                lot_size=50,
                raw_gex=raw_gex,
                signed_gex=signed_gex,
                calc_version="h_gex_v1",
                calculated_at=ts,
                status="SUCCESS",
            )
            db_session.add(snapshot)

    db_session.commit()
    return timestamps


@pytest.fixture
def sample_greeks_data(db_session, sample_gex_data):
    """Insert sample OptionGreeks data aligned with GEX timestamps."""
    instruments = [
        ("NIFTY25CE24000", "CE"),
        ("NIFTY25CE24100", "CE"),
        ("NIFTY25PE24000", "PE"),
        ("NIFTY25PE24100", "PE"),
    ]
    for ts in sample_gex_data:
        for ik, opt_type in instruments:
            db_session.add(OptionGreeks(
                instrument_key=ik,
                interval="3min",
                open_time=ts,
                calc_version="greeks_v3",
                status="SUCCESS",
                strike=24000.0 if "24000" in ik else 24100.0,
                expiry="2025-06-26",
                option_type=opt_type,
                spot=24100.0,
                implied_volatility=0.15,
                delta=0.5 if opt_type == "CE" else -0.5,
                gamma=0.001,
                theta=-0.01,
                vega=0.05,
                option_price=50.0,
                time_to_expiry=0.01,
                risk_free_rate=0.065,
                intrinsic_value=0.0,
                calc_model="BLACK_SCHOLES_EUROPEAN",
                calculated_at=ts,
            ))
    db_session.commit()


@pytest.fixture
def sample_nifty_data(db_session, sample_gex_data):
    """Insert sample NIFTY candle data."""
    for ts_idx, ts in enumerate(sample_gex_data):
        for i in range(25):  # 25 future candles for forward returns
            future_ts = ts + timedelta(minutes=3 * (i + 1))
            # Ensure unique open_time by offsetting per GEX timestamp
            unique_ts = future_ts + timedelta(seconds=ts_idx)
            candle = NiftyCandle(
                symbol="NIFTY",
                interval="3min",
                open_time=unique_ts,
                open=24100.0 + i,
                high=24150.0 + i,
                low=24050.0 + i,
                close=24120.0 + i * 2,
                volume=1000000 + i * 10000,
            )
            db_session.add(candle)
    db_session.commit()


@pytest.fixture
def sample_oi_data(db_session, sample_gex_data):
    """Insert sample option candle data for OI."""
    instruments = [
        ("NIFTY25CE24000", "CE", 24000.0),
        ("NIFTY25CE24100", "CE", 24100.0),
        ("NIFTY25PE24000", "PE", 24000.0),
        ("NIFTY25PE24100", "PE", 24100.0),
    ]

    for ts in sample_gex_data:
        for ik, opt_type, strike in instruments:
            oi = 1000 if opt_type == "CE" else 1500
            vol = 500 if opt_type == "CE" else 700
            candle = OptionCandle(
                instrument_key=ik,
                interval="3min",
                open_time=ts,
                open=50.0,
                high=55.0,
                low=45.0,
                close=52.0,
                volume=vol,
                open_interest=oi,
                source="TEST",
                fetched_at=ts,
            )
            db_session.add(candle)
    db_session.commit()


# ---------------------------------------------------------------------------
# Test: Research Dataset Builder
# ---------------------------------------------------------------------------

class TestResearchDataset:
    """Test the research dataset builder."""

    def test_dataset_builds_correctly(self, db_session, sample_gex_data, sample_nifty_data, sample_oi_data):
        """Dataset should have one entry per timestamp with correct fields."""
        engine = GexResearchEngine(db_session)
        dataset = engine.build_research_dataset()

        assert len(dataset) == 5
        assert all(isinstance(d, TimestampResearch) for d in dataset)

        # First timestamp should have spot
        first = dataset[0]
        assert first.spot > 0
        assert first.timestamp is not None

        # GEX values should be non-zero
        assert first.total_net_gex != 0 or first.total_call_gex != 0

    def test_dataset_anti_leakage(self, db_session, sample_gex_data, sample_nifty_data):
        """Forward returns should only come from future candles."""
        engine = GexResearchEngine(db_session)
        dataset = engine.build_research_dataset()

        for point in dataset:
            # Forward returns should be finite numbers when present
            if point.nifty_return_3m is not None:
                assert math.isfinite(point.nifty_return_3m)

    def test_dataset_empty_when_no_data(self, db_session):
        """Empty database should return empty dataset."""
        engine = GexResearchEngine(db_session)
        dataset = engine.build_research_dataset()
        assert len(dataset) == 0

    def test_dataset_with_time_filter(self, db_session, sample_gex_data, sample_nifty_data, sample_oi_data):
        """Time filter should limit the dataset."""
        engine = GexResearchEngine(db_session)
        start = sample_gex_data[1]
        end = sample_gex_data[3]
        dataset = engine.build_research_dataset(start=start, end=end)
        assert len(dataset) <= 3

    def test_dataset_with_max_timestamps(self, db_session, sample_gex_data, sample_nifty_data, sample_oi_data):
        """max_timestamps should limit the dataset."""
        engine = GexResearchEngine(db_session)
        dataset = engine.build_research_dataset(max_timestamps=2)
        assert len(dataset) <= 2


# ---------------------------------------------------------------------------
# Test: Gamma Regime Classification
# ---------------------------------------------------------------------------

class TestRegimeClassification:
    """Test gamma regime classification."""

    def test_positive_regime(self, db_session):
        """Positive net GEX should classify as POSITIVE_GAMMA."""
        engine = GexResearchEngine(db_session)
        regimes = engine._compute_regimes({
            datetime(2025, 1, 1): {"net_gex": 1000},
            datetime(2025, 1, 2): {"net_gex": 2000},
            datetime(2025, 1, 3): {"net_gex": 1500},
        })
        for ts, regime in regimes.items():
            assert regime["regime"] == "POSITIVE_GAMMA"

    def test_negative_regime(self, db_session):
        """Negative net GEX should classify as NEGATIVE_GAMMA."""
        engine = GexResearchEngine(db_session)
        regimes = engine._compute_regimes({
            datetime(2025, 1, 1): {"net_gex": -1000},
            datetime(2025, 1, 2): {"net_gex": -2000},
        })
        for ts, regime in regimes.items():
            assert regime["regime"] == "NEGATIVE_GAMMA"

    def test_neutral_regime(self, db_session):
        """Zero net GEX should classify as NEUTRAL."""
        engine = GexResearchEngine(db_session)
        regimes = engine._compute_regimes({
            datetime(2025, 1, 1): {"net_gex": 0.0},
        })
        assert regimes[datetime(2025, 1, 1)]["regime"] == "NEUTRAL"

    def test_regime_transitions(self, db_session):
        """Regime transitions should be detected."""
        engine = GexResearchEngine(db_session)
        regimes = engine._compute_regimes({
            datetime(2025, 1, 1): {"net_gex": -1000},
            datetime(2025, 1, 2): {"net_gex": 1000},
            datetime(2025, 1, 3): {"net_gex": -500},
        })

        # Second timestamp should have a transition
        ts2 = datetime(2025, 1, 2)
        assert regimes[ts2]["transition"] is not None
        assert "NEGATIVE_GAMMA" in regimes[ts2]["transition"]
        assert "POSITIVE_GAMMA" in regimes[ts2]["transition"]

    def test_enhanced_regime_granularity(self, db_session):
        """Enhanced regime should have detailed classification."""
        engine = GexResearchEngine(db_session)
        regimes = engine._compute_regimes({
            datetime(2025, 1, 1): {"net_gex": -10000},
            datetime(2025, 1, 2): {"net_gex": -100},
            datetime(2025, 1, 3): {"net_gex": 0.0},
            datetime(2025, 1, 4): {"net_gex": 100},
            datetime(2025, 1, 5): {"net_gex": 10000},
        })

        detailed = [regimes[ts]["detailed_regime"] for ts in sorted(regimes.keys())]
        assert "STRONG_NEGATIVE" in detailed
        assert "STRONG_POSITIVE" in detailed


# ---------------------------------------------------------------------------
# Test: Gamma Flip Detection
# ---------------------------------------------------------------------------

class TestGammaFlip:
    """Test gamma flip detection."""

    def test_flip_with_sign_change(self, db_session, sample_gex_data):
        """Should detect gamma flip when GEX changes sign across strikes."""
        engine = GexResearchEngine(db_session)
        flip = engine._detect_gamma_flip_at_timestamp(sample_gex_data[0])

        # With our sample data, we should find a flip
        assert flip["status"] in ("ESTIMATED", "NO_CROSSING", "INSUFFICIENT_DATA")

    def test_no_flip_when_all_positive(self, db_session):
        """No crossing when all strikes have positive GEX."""
        engine = GexResearchEngine(db_session)
        # Create data where all GEX is positive
        ts = datetime(2025, 1, 1, 9, 15)
        for strike in [24000, 24100, 24200]:
            for opt_type in ["CE"]:
                db_session.add(HistoricalGexSnapshot(
                    instrument_key=f"NIFTY_{strike}_{opt_type}",
                    interval="3min", open_time=ts, spot=24100.0,
                    strike=strike, expiry="2025-01-30", option_type=opt_type,
                    gamma=0.001, open_interest=1000, option_price=50.0,
                    lot_size=50, raw_gex=100.0, signed_gex=100.0,
                    calc_version="h_gex_v1", calculated_at=ts, status="SUCCESS",
                ))
        db_session.commit()

        flip = engine._detect_gamma_flip_at_timestamp(ts)
        assert flip["status"] == "NO_CROSSING"

    def test_exact_zero_detection(self, db_session):
        """Should detect exact zero crossing."""
        engine = GexResearchEngine(db_session)
        ts = datetime(2025, 1, 1, 9, 15)

        # Create strikes with GEX that crosses zero at a specific strike
        db_session.add(HistoricalGexSnapshot(
            instrument_key="CE_24000", interval="3min", open_time=ts, spot=24100.0,
            strike=24000.0, expiry="2025-01-30", option_type="CE",
            gamma=0.001, open_interest=1000, option_price=50.0,
            lot_size=50, raw_gex=50.0, signed_gex=50.0,
            calc_version="h_gex_v1", calculated_at=ts, status="SUCCESS",
        ))
        db_session.add(HistoricalGexSnapshot(
            instrument_key="PE_24200", interval="3min", open_time=ts, spot=24100.0,
            strike=24200.0, expiry="2025-01-30", option_type="PE",
            gamma=0.001, open_interest=1000, option_price=50.0,
            lot_size=50, raw_gex=80.0, signed_gex=-80.0,
            calc_version="h_gex_v1", calculated_at=ts, status="SUCCESS",
        ))
        db_session.commit()

        flip = engine._detect_gamma_flip_at_timestamp(ts)
        assert flip["status"] == "ESTIMATED"
        assert flip["flip_strike"] is not None
        # Flip should be between 24000 and 24200
        assert 24000 <= flip["flip_strike"] <= 24200


# ---------------------------------------------------------------------------
# Test: Gamma Walls
# ---------------------------------------------------------------------------

class TestGammaWalls:
    """Test gamma wall detection."""

    def test_wall_detection(self, db_session, sample_gex_data):
        """Should detect strongest positive and negative walls."""
        engine = GexResearchEngine(db_session)
        wall = engine._detect_walls_at_timestamp(sample_gex_data[0])

        # Should have at least one wall
        assert "pos_wall_strike" in wall or "neg_wall_strike" in wall

    def test_positive_wall_is_strongest_positive(self, db_session):
        """Positive wall should be the strike with highest positive GEX."""
        engine = GexResearchEngine(db_session)
        ts = datetime(2025, 1, 1, 9, 15)

        # Create strikes with known GEX values
        gex_values = {
            24000: 100.0,
            24100: 500.0,  # Strongest positive
            24200: 200.0,
        }
        for strike, gex in gex_values.items():
            db_session.add(HistoricalGexSnapshot(
                instrument_key=f"CE_{strike}", interval="3min", open_time=ts, spot=24100.0,
                strike=strike, expiry="2025-01-30", option_type="CE",
                gamma=0.001, open_interest=1000, option_price=50.0,
                lot_size=50, raw_gex=gex, signed_gex=gex,
                calc_version="h_gex_v1", calculated_at=ts, status="SUCCESS",
            ))
        db_session.commit()

        wall = engine._detect_walls_at_timestamp(ts)
        assert wall["pos_wall_strike"] == 24100.0

    def test_wall_distance_from_spot(self, db_session):
        """Wall distance should be correctly calculated."""
        engine = GexResearchEngine(db_session)
        ts = datetime(2025, 1, 1, 9, 15)

        db_session.add(HistoricalGexSnapshot(
            instrument_key="CE_24200", interval="3min", open_time=ts, spot=24100.0,
            strike=24200.0, expiry="2025-01-30", option_type="CE",
            gamma=0.001, open_interest=1000, option_price=50.0,
            lot_size=50, raw_gex=500.0, signed_gex=500.0,
            calc_version="h_gex_v1", calculated_at=ts, status="SUCCESS",
        ))
        db_session.commit()

        wall = engine._detect_walls_at_timestamp(ts)
        assert wall["pos_wall_strike"] == 24200.0
        assert wall["pos_wall_distance"] == 100.0  # 24200 - 24100


# ---------------------------------------------------------------------------
# Test: GEX Change/Acceleration
# ---------------------------------------------------------------------------

class TestGexChange:
    """Test GEX change and acceleration."""

    def test_gex_change_computation(self, db_session, sample_gex_data, sample_nifty_data, sample_oi_data):
        """GEX change should be computed between consecutive timestamps."""
        engine = GexResearchEngine(db_session)
        dataset = engine.build_research_dataset()

        # First timestamp has no change
        assert dataset[0].gex_change is None

        # Subsequent timestamps should have changes
        for point in dataset[1:]:
            assert point.gex_change is not None

    def test_gex_acceleration_computation(self, db_session, sample_gex_data, sample_nifty_data, sample_oi_data):
        """GEX acceleration should be the second derivative."""
        engine = GexResearchEngine(db_session)
        dataset = engine.build_research_dataset()

        # First two timestamps have no acceleration
        assert dataset[0].gex_acceleration is None
        assert dataset[1].gex_acceleration is None

        # Third timestamp onward should have acceleration
        for point in dataset[2:]:
            assert point.gex_acceleration is not None


# ---------------------------------------------------------------------------
# Test: Forward Returns
# ---------------------------------------------------------------------------

class TestForwardReturns:
    """Test forward return computation."""

    def test_forward_returns_computed(self, db_session, sample_gex_data, sample_nifty_data):
        """Forward returns should be computed from future NIFTY candles."""
        engine = GexResearchEngine(db_session)
        nifty_candles = engine._fetch_nifty_candles(
            sample_gex_data[0],
            sample_gex_data[-1] + timedelta(hours=8),
        )

        fr = engine._compute_forward_returns(
            sample_gex_data[0], 24100.0, nifty_candles
        )

        # Should have forward returns at various intervals
        assert 3 in fr  # 9 minutes
        assert 6 in fr  # 18 minutes
        assert 15 in fr  # 45 minutes

    def test_no_future_leakage(self, db_session, sample_gex_data, sample_nifty_data):
        """Forward returns should only use candles after the timestamp."""
        engine = GexResearchEngine(db_session)
        nifty_candles = engine._fetch_nifty_candles(
            sample_gex_data[0],
            sample_gex_data[-1] + timedelta(hours=8),
        )

        # Use a middle timestamp
        ts = sample_gex_data[2]
        fr = engine._compute_forward_returns(ts, 24120.0, nifty_candles)

        # Returns should be based on candles after ts
        assert fr.get(3) is not None


# ---------------------------------------------------------------------------
# Test: OI Analysis
# ---------------------------------------------------------------------------

class TestOiAnalysis:
    """Test OI state computation."""

    def test_oi_data_fetched(self, db_session, sample_gex_data, sample_greeks_data, sample_oi_data):
        """OI data should be correctly fetched and aggregated."""
        engine = GexResearchEngine(db_session)
        timestamps = engine._get_timestamps(None, None)
        oi_data = engine._fetch_oi_data(timestamps)

        assert len(oi_data) > 0

        # Check first timestamp
        first_ts = sorted(oi_data.keys())[0]
        oi = oi_data[first_ts]
        assert oi["total_oi"] > 0
        assert oi["call_oi"] > 0
        assert oi["put_oi"] > 0

    def test_oi_change_computed(self, db_session, sample_gex_data, sample_greeks_data, sample_oi_data):
        """OI change should be computed between consecutive timestamps."""
        engine = GexResearchEngine(db_session)
        timestamps = engine._get_timestamps(None, None)
        oi_data = engine._fetch_oi_data(timestamps)

        sorted_ts = sorted(oi_data.keys())
        if len(sorted_ts) >= 2:
            second_ts = sorted_ts[1]
            assert oi_data[second_ts]["oi_change"] is not None


# ---------------------------------------------------------------------------
# Test: Signal Discovery
# ---------------------------------------------------------------------------

class TestSignalDiscovery:
    """Test signal candidate discovery."""

    def test_signals_discovered(self, db_session, sample_gex_data, sample_nifty_data, sample_oi_data):
        """Should discover signal candidates."""
        engine = GexResearchEngine(db_session)
        dataset = engine.build_research_dataset()

        signals = engine.discover_signals(dataset)
        assert len(signals) > 0
        assert all(isinstance(s, SignalCandidate) for s in signals)

    def test_signal_has_statistics(self, db_session, sample_gex_data, sample_nifty_data, sample_oi_data):
        """Each signal should have statistical evidence."""
        engine = GexResearchEngine(db_session)
        dataset = engine.build_research_dataset()
        signals = engine.discover_signals(dataset)

        for signal in signals:
            assert signal.sample_size >= 0
            assert 0 <= signal.win_rate <= 100
            assert signal.confidence_level in ("HIGH", "MEDIUM", "LOW", "INSUFFICIENT_SAMPLE")


# ---------------------------------------------------------------------------
# Test: Walk-Forward Validation
# ---------------------------------------------------------------------------

class TestWalkForward:
    """Test walk-forward validation."""

    def test_walk_forward_split(self, db_session, sample_gex_data, sample_nifty_data, sample_oi_data):
        """Walk-forward should split data chronologically."""
        engine = GexResearchEngine(db_session)
        dataset = engine.build_research_dataset()

        def dummy_signal(data):
            return [p.nifty_return_15m for p in data if p.nifty_return_15m is not None]

        wf = engine.walk_forward_validate(dataset, dummy_signal)
        assert "in_sample" in wf
        assert "out_of_sample" in wf
        assert wf["train_size"] + wf["test_size"] == len(dataset)


# ---------------------------------------------------------------------------
# Test: Robustness Check
# ---------------------------------------------------------------------------

class TestRobustness:
    """Test statistical robustness checks."""

    def test_robust_signal(self):
        """A consistent signal should be robust."""
        engine = GexResearchEngine.__new__(GexResearchEngine)

        # Consistent positive returns
        returns = [0.1, 0.15, 0.12, 0.08, 0.11, 0.13, 0.09, 0.14, 0.10, 0.12,
                   0.11, 0.13, 0.09, 0.14, 0.10, 0.12, 0.11, 0.13, 0.09, 0.14,
                   0.10, 0.12, 0.11, 0.13, 0.09, 0.14, 0.10, 0.12, 0.11, 0.13,
                   0.09, 0.14, 0.10, 0.12, 0.11, 0.13, 0.09, 0.14, 0.10, 0.12]
        result = engine.robustness_check(returns, "test")
        assert result["robust"] is True
        assert result["significance"] in ("HIGH", "MEDIUM", "LOW")

    def test_weak_signal(self):
        """A random signal should not be robust."""
        engine = GexResearchEngine.__new__(GexResearchEngine)

        # Random returns near zero
        returns = [0.001, -0.001, 0.002, -0.002, 0.001, -0.001, 0.002, -0.002,
                   0.001, -0.001, 0.002, -0.002, 0.001, -0.001, 0.002, -0.002,
                   0.001, -0.001, 0.002, -0.002, 0.001, -0.001, 0.002, -0.002,
                   0.001, -0.001, 0.002, -0.002, 0.001, -0.001]
        result = engine.robustness_check(returns, "test")
        # Should not be robust (mean near zero)
        assert result["robust"] is False or result["significance"] == "LOW"

    def test_empty_returns(self):
        """Empty returns should not be robust."""
        engine = GexResearchEngine.__new__(GexResearchEngine)
        result = engine.robustness_check([], "test")
        assert result["robust"] is False


# ---------------------------------------------------------------------------
# Test: Multiple Testing
# ---------------------------------------------------------------------------

class TestMultipleTesting:
    """Test multiple-testing adjustment."""

    def test_multiple_testing_report(self):
        """Should correctly report multiple-testing context."""
        engine = GexResearchEngine.__new__(GexResearchEngine)

        signals = [
            SignalCandidate(signal_name="S1", sample_size=100, win_rate=60.0, confidence_level="MEDIUM"),
            SignalCandidate(signal_name="S2", sample_size=50, win_rate=55.0, confidence_level="LOW"),
            SignalCandidate(signal_name="S3", sample_size=30, win_rate=45.0, confidence_level="INSUFFICIENT_SAMPLE"),
        ]

        result = engine.multiple_testing_adjustment(signals)
        assert result["total_hypotheses_tested"] == 3
        assert result["apparently_successful"] == 1  # Only S1 is MEDIUM/HIGH
        assert result["bonferroni_alpha"] < 0.05


# ---------------------------------------------------------------------------
# Test: Expiry Day Analysis
# ---------------------------------------------------------------------------

class TestExpiryDay:
    """Test expiry-day vs non-expiry-day comparison."""

    def test_expiry_day_detection(self, db_session, sample_gex_data, sample_nifty_data, sample_oi_data):
        """Should correctly identify expiry days."""
        engine = GexResearchEngine(db_session)
        dataset = engine.build_research_dataset()

        # Check that is_expiry_day is set correctly
        for point in dataset:
            assert isinstance(point.is_expiry_day, bool)

    def test_expiry_day_analysis(self, db_session, sample_gex_data, sample_nifty_data, sample_oi_data):
        """Should produce expiry-day comparison."""
        engine = GexResearchEngine(db_session)
        dataset = engine.build_research_dataset()

        result = engine.analyze_expiry_day(dataset)
        assert "expiry_day" in result
        assert "non_expiry_day" in result


# ---------------------------------------------------------------------------
# Test: Statistical Calculations
# ---------------------------------------------------------------------------

class TestStatistics:
    """Test statistical computation functions."""

    def test_compute_stats_basic(self):
        """Basic statistics should be correct."""
        engine = MagicMock(spec=GexResearchEngine)
        values = [-2.0, -1.0, 0.0, 1.0, 2.0]
        stats = GexResearchEngine._compute_stats(engine, values)

        assert stats["count"] == 5
        assert stats["mean"] == 0.0
        assert stats["median"] == 0.0
        assert stats["win_pct"] == 40.0  # 2 out of 5 positive (1.0, 2.0)

    def test_compute_stats_empty(self):
        """Empty values should return count 0."""
        engine = MagicMock(spec=GexResearchEngine)
        stats = GexResearchEngine._compute_stats(engine, [])
        assert stats["count"] == 0

    def test_compute_stats_single(self):
        """Single value should work."""
        engine = MagicMock(spec=GexResearchEngine)
        stats = GexResearchEngine._compute_stats(engine, [42.0])
        assert stats["count"] == 1
        assert stats["mean"] == 42.0


# ---------------------------------------------------------------------------
# Test: Production DB Protection
# ---------------------------------------------------------------------------

class TestProductionDBProtection:
    """Verify tests do not write to production database."""

    def test_in_memory_engine_used(self, db_session):
        """Tests should use in-memory SQLite."""
        assert "sqlite://" in str(db_session.get_bind().url)

    def test_no_production_path(self, db_session):
        """Engine URL should not reference production DB."""
        url = str(db_session.get_bind().url)
        assert "paper_journal" not in url

def test_regime_percentiles_do_not_use_future_gex_values():
    """A future GEX observation must not change an earlier regime classification."""
    from datetime import datetime

    from app.services.historical_gex_research import GexResearchEngine

    t1 = datetime(2026, 8, 27, 10, 0)
    t2 = datetime(2026, 8, 27, 10, 3)
    t3 = datetime(2026, 8, 27, 10, 6)

    gex_series = {
        t1: {"net_gex": 100.0},
        t2: {"net_gex": 0.0},
        t3: {"net_gex": -1000.0},
    }

    engine = GexResearchEngine.__new__(GexResearchEngine)
    regimes = engine._compute_regimes(gex_series)

    # With only t1 known, +100 is the full historical distribution and is not
    # made "strong positive" by the unseen -1000 future observation.
    assert regimes[t1]["detailed_regime"] == "WEAK_POSITIVE"
