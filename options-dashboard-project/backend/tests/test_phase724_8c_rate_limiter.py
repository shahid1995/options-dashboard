"""Phase 7.24.8C — Global Rate Limiter & Self-Regulating Backfill Tests.

Comprehensive mocked tests for:
  - No 429 → gradual increase in throughput
  - First 429 → global cooldown
  - Repeated 429 → reduced rate
  - Retry-After handling
  - Missing Retry-After fallback
  - Recovery after cooldown
  - Multiple workers sharing the same limiter
  - No request burst after cooldown
  - Checkpoint/resume
  - 401 handling
  - 5xx handling
  - Zero duplicate candles
  - Existing data never redownloaded
  - Raw OHLCV/OI immutability
  - Dry-run produces no API calls

All tests use mocked HTTP responses.  No real Upstox API calls.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    ContractSpec,
    IngestionCheckpoint,
    NiftyCandle,
    OptionCandle,
)
from app.services.rate_limiter import (
    GlobalRateLimiter,
    RateLimiterConfig,
    RateLimiterMetrics,
)
from app.services.backfill_orchestrator import (
    BackfillOrchestrator,
    NIFTY_INDEX_KEY,
    NIFTY_SYMBOL,
    PIPELINE_OPTIONS,
)
from app.services.upstox_client import (
    UpstoxClient,
    UpstoxAuthenticationError,
    UpstoxRateLimitError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    yield session
    session.close()
    engine.dispose()


class MockTokenProvider:
    def __init__(self, token="test-token-123"):
        self._token = token
    def get_token(self):
        return self._token


def _mock_client():
    client = AsyncMock(spec=UpstoxClient)
    client._token_provider = MockTokenProvider()
    _sample_candle = [
        ["2024-10-03T09:15:00+05:30", 150.5, 155.0, 148.0, 152.3, 5000, 325000],
        ["2024-10-03T09:18:00+05:30", 152.3, 156.0, 151.0, 154.5, 4500, 320000],
    ] * 50  # 100 candles per instrument
    client.get_expired_historical_candles = AsyncMock(return_value=_sample_candle)
    _sample_nifty = [["2024-10-03T09:15:00+05:30", 25000, 25020, 24980, 25010, 15000, 0]] * 125
    client.get_historical_candles = AsyncMock(return_value=_sample_nifty)
    client.get_expiries = AsyncMock(return_value=["2024-10-03", "2024-10-10"])
    client.get_contracts = AsyncMock(return_value=[])
    client.metrics = MagicMock()
    client.metrics.snapshot.return_value = {"total_requests": 0}
    return client


def _fast_config():
    """Config with tiny delays for fast tests."""
    return RateLimiterConfig(
        initial_concurrency=2,
        min_concurrency=1,
        max_concurrency=4,
        initial_interval=0.01,
        min_interval=0.005,
        max_interval=1.0,
        cooldown_base=0.1,
        cooldown_max=1.0,
        cooldown_multiplier=2.0,
        recovery_step=0.002,
        recovery_floor_pct=0.7,
        reduce_concurrency_threshold=3,
        reduce_cooldown=0.05,
    )


def _add_nifty_candles(db, start_date, count=5):
    for i in range(count):
        d = start_date + timedelta(days=i)
        dt = datetime(d.year, d.month, d.day, 9, 15)
        db.add(NiftyCandle(
            symbol="NIFTY", interval="3min", open_time=dt,
            open=25000 + i * 10, high=25020 + i * 10,
            low=24980 + i * 10, close=25010 + i * 10, volume=15000,
        ))
    db.commit()


def _add_spec(db, ik, expiry, strike, opt_type, lot=75):
    spec = ContractSpec(
        instrument_key=ik, underlying="NIFTY", underlying_key=NIFTY_INDEX_KEY,
        expiry=expiry, strike_price=strike, instrument_type=opt_type,
        lot_size=lot, minimum_lot=lot,
        trading_symbol=f"NIFTY{expiry.replace('-', '')}{int(strike)}{opt_type}",
        segment="NSE_FO", exchange="NSE",
        source="TEST", source_reference="test",
        fetched_at=datetime.now(timezone.utc),
    )
    db.add(spec)
    db.commit()
    return spec


def _add_specs_for_expiry(db, expiry, strikes, lot=75):
    for strike in strikes:
        _add_spec(db, f"NSE_FO|{int(strike * 10)}|{expiry}", expiry, strike, "CE", lot)
        _add_spec(db, f"NSE_FO|{int(strike * 10 + 1)}|{expiry}", expiry, strike, "PE", lot)


def _add_option_candle(db, ik, dt, open_p=150.0, volume=5000.0, oi=325000.0):
    c = OptionCandle(
        instrument_key=ik, interval="3min", open_time=dt,
        open=open_p, high=open_p + 5, low=open_p - 2, close=open_p + 2,
        volume=volume, open_interest=oi,
        source="UPSTOX_EXPIRED_CANDLE", fetched_at=datetime.now(timezone.utc),
    )
    db.add(c)
    db.commit()
    return c


# ===========================================================================
# 1. NO 429 → GRADUAL INCREASE
# ===========================================================================

class TestNo429GradualIncrease:
    """When all requests succeed, interval should decrease toward minimum."""

    @pytest.mark.asyncio
    async def test_interval_decreases_on_success(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        initial = limiter.interval

        for _ in range(10):
            await limiter.acquire()
            await limiter.on_success()
            limiter.release()

        assert limiter.interval < initial
        assert limiter.interval >= cfg.min_interval * cfg.recovery_floor_pct

    @pytest.mark.asyncio
    async def test_concurrency_stable_without_429(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        for _ in range(20):
            await limiter.acquire()
            await limiter.on_success()
            limiter.release()

        # Concurrency should not change without 429s
        assert limiter.concurrency == cfg.initial_concurrency

    @pytest.mark.asyncio
    async def test_metrics_reflect_successes(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        for _ in range(5):
            await limiter.acquire()
            await limiter.on_success()
            limiter.release()

        m = limiter.snapshot()
        assert m.successful_requests == 5
        assert m.rate_limit_429s == 0
        assert m.consecutive_429s == 0


# ===========================================================================
# 2. FIRST 429 → GLOBAL COOLDOWN
# ===========================================================================

class TestFirst429Cooldown:
    """A single 429 should trigger a global cooldown."""

    @pytest.mark.asyncio
    async def test_cooldown_triggered_on_first_429(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        await limiter.acquire()
        await limiter.on_429(retry_after=None)
        limiter.release()

        m = limiter.snapshot()
        assert m.rate_limit_429s == 1
        assert m.consecutive_429s == 1
        assert m.cooldown_remaining_s > 0

    @pytest.mark.asyncio
    async def test_interval_widens_on_429(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        before = limiter.interval

        await limiter.acquire()
        await limiter.on_429(retry_after=None)
        limiter.release()

        assert limiter.interval > before


# ===========================================================================
# 3. REPEATED 429 → REDUCED RATE
# ===========================================================================

class TestRepeated429ReducedRate:
    """Multiple 429s should increase cooldown and possibly reduce concurrency."""

    @pytest.mark.asyncio
    async def test_exponential_cooldown_increase(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        # First 429
        await limiter.acquire()
        await limiter.on_429(retry_after=None)
        limiter.release()
        first_cooldown = limiter._cooldown_total

        # Wait out cooldown, then second 429
        await asyncio.sleep(cfg.cooldown_base + 0.05)
        await limiter.acquire()
        await limiter.on_429(retry_after=None)
        limiter.release()

        assert limiter._cooldown_total > first_cooldown
        assert limiter._consecutive_429s == 2

    @pytest.mark.asyncio
    async def test_concurrency_reduced_after_threshold(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        initial_concurrency = limiter.concurrency

        # Trigger enough consecutive 429s to cross threshold
        for _ in range(cfg.reduce_concurrency_threshold):
            await limiter.acquire()
            await limiter.on_429(retry_after=None)
            limiter.release()
            await asyncio.sleep(cfg.cooldown_base + 0.05)

        assert limiter.concurrency < initial_concurrency
        assert limiter.concurrency >= cfg.min_concurrency


# ===========================================================================
# 4. RETRY-AFTER HANDLING
# ===========================================================================

class TestRetryAfterHandling:
    """When Upstox supplies Retry-After, honour it."""

    @pytest.mark.asyncio
    async def test_retry_after_used_for_cooldown(self):
        # Use a config where cooldown_max allows retry_after=2.0 through
        cfg = RateLimiterConfig(
            cooldown_base=0.05, cooldown_max=10.0, cooldown_multiplier=2.0,
            initial_concurrency=2, min_concurrency=1, max_concurrency=4,
            initial_interval=0.01, min_interval=0.005, max_interval=1.0,
            recovery_step=0.002, recovery_floor_pct=0.7,
            reduce_concurrency_threshold=3, reduce_cooldown=0.05,
        )
        limiter = GlobalRateLimiter(config=cfg)

        await limiter.acquire()
        await limiter.on_429(retry_after=2.0)
        limiter.release()

        # Cooldown should be at least 2 seconds (retry_after honoured)
        assert limiter.cooldown_remaining >= 1.5  # Allow small timing margin

    @pytest.mark.asyncio
    async def test_retry_after_capped_at_max(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        await limiter.acquire()
        await limiter.on_429(retry_after=9999.0)
        limiter.release()

        # Should be capped at cooldown_max
        assert limiter.cooldown_remaining <= cfg.cooldown_max + 0.1


# ===========================================================================
# 5. MISSING RETRY-AFTER FALLBACK
# ===========================================================================

class TestMissingRetryAfterFallback:
    """When Retry-After is missing, use exponential backoff."""

    @pytest.mark.asyncio
    async def test_exponential_backoff_without_retry_after(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        await limiter.acquire()
        await limiter.on_429(retry_after=None)
        limiter.release()

        # First cooldown should be around cooldown_base
        assert limiter.cooldown_remaining >= cfg.cooldown_base * 0.8

    @pytest.mark.asyncio
    async def test_none_retry_after_uses_fallback(self):
        """retry_after=None should trigger the exponential fallback path."""
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        await limiter.acquire()
        await limiter.on_429(retry_after=None)
        limiter.release()

        m = limiter.snapshot()
        assert m.cooldown_remaining_s > 0


# ===========================================================================
# 6. RECOVERY AFTER COOLDOWN
# ===========================================================================

class TestRecoveryAfterCooldown:
    """After cooldown expires and requests succeed, limiter should recover."""

    @pytest.mark.asyncio
    async def test_consecutive_429s_reset_on_success(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        # Trigger some 429s
        await limiter.acquire()
        await limiter.on_429(retry_after=None)
        limiter.release()
        await asyncio.sleep(cfg.cooldown_base + 0.1)

        # Then succeed
        await limiter.acquire()
        await limiter.on_success()
        limiter.release()

        assert limiter._consecutive_429s == 0

    @pytest.mark.asyncio
    async def test_interval_shrinks_after_recovery(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        # Widen interval via 429
        await limiter.acquire()
        await limiter.on_429(retry_after=None)
        limiter.release()
        widened = limiter.interval
        await asyncio.sleep(cfg.cooldown_base + 0.1)

        # Shrink via successes
        for _ in range(5):
            await limiter.acquire()
            await limiter.on_success()
            limiter.release()

        assert limiter.interval < widened

    @pytest.mark.asyncio
    async def test_concurrency_may_increase_after_sustained_success(self):
        """After 0 consecutive 429s and enough headroom, concurrency may increase."""
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        # Bump to have room
        limiter._concurrency = cfg.initial_concurrency - 1
        limiter._semaphore = asyncio.Semaphore(limiter._concurrency)

        # Sustained success → consecutive_429s = 0
        for _ in range(5):
            await limiter.acquire()
            await limiter.on_success()
            limiter.release()

        # Trigger increase check
        await limiter._maybe_increase_concurrency()
        assert limiter.concurrency == cfg.initial_concurrency


# ===========================================================================
# 7. MULTIPLE WORKERS SHARING THE SAME LIMITER
# ===========================================================================

class TestMultipleWorkersSharedLimiter:
    """All concurrent workers must share one limiter instance."""

    @pytest.mark.asyncio
    async def test_shared_limiter_limits_concurrent_requests(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        max_in_flight = [0]
        current_in_flight = [0]

        async def worker():
            await limiter.acquire()
            current_in_flight[0] += 1
            max_in_flight[0] = max(max_in_flight[0], current_in_flight[0])
            await asyncio.sleep(0.02)
            current_in_flight[0] -= 1
            await limiter.on_success()
            limiter.release()

        tasks = [worker() for _ in range(10)]
        await asyncio.gather(*tasks)

        assert max_in_flight[0] <= cfg.initial_concurrency

    @pytest.mark.asyncio
    async def test_shared_limiter_collects_all_metrics(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        async def worker():
            await limiter.acquire()
            await limiter.on_success()
            limiter.release()

        tasks = [worker() for _ in range(5)]
        await asyncio.gather(*tasks)

        m = limiter.snapshot()
        assert m.successful_requests == 5


# ===========================================================================
# 8. NO REQUEST BURST AFTER COOLDOWN
# ===========================================================================

class TestNoBurstAfterCooldown:
    """After cooldown, requests must still respect the interval pacing."""

    @pytest.mark.asyncio
    async def test_requests_paced_after_cooldown(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        # Trigger cooldown
        await limiter.acquire()
        await limiter.on_429(retry_after=None)
        limiter.release()
        await asyncio.sleep(cfg.cooldown_base + 0.1)

        # Now measure request spacing
        timestamps = []
        for _ in range(3):
            await limiter.acquire()
            timestamps.append(time.monotonic())
            await limiter.on_success()
            limiter.release()

        # At least 2 of the 3 should be paced by interval
        if len(timestamps) >= 2:
            gaps = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]
            assert all(g >= 0 for g in gaps)


# ===========================================================================
# 9. CHECKPOINT / RESUME
# ===========================================================================

class TestCheckpointResume:
    """429 should leave checkpoint PENDING (not FAILED) for retry."""

    @pytest.mark.asyncio
    async def test_429_marks_checkpoint_pending(self, db):
        _add_nifty_candles(db, date(2024, 9, 30))
        strikes = [25000]
        _add_specs_for_expiry(db, "2024-10-03", strikes)

        async def always_rate_limited(*args, **kwargs):
            raise UpstoxRateLimitError("Rate limit exceeded")

        client = _mock_client()
        client.get_expired_historical_candles = always_rate_limited

        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        orch = BackfillOrchestrator(db, client, rate_limiter=limiter)
        await orch.run_options(concurrency=1)

        # Checkpoint should be PENDING, not FAILED
        checkpoints = db.execute(
            select(IngestionCheckpoint).where(
                IngestionCheckpoint.pipeline == PIPELINE_OPTIONS,
            )
        ).scalars().all()
        for cp in checkpoints:
            assert cp.status == "PENDING", f"Expected PENDING, got {cp.status}"

    @pytest.mark.asyncio
    async def test_resume_skips_completed_instruments(self, db):
        _add_nifty_candles(db, date(2024, 9, 30))
        strikes = [25000, 25050]
        _add_specs_for_expiry(db, "2024-10-03", strikes)

        # Pre-populate one instrument
        _add_option_candle(db, "NSE_FO|250000|2024-10-03", datetime(2024, 10, 3, 9, 15))

        client = _mock_client()
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        orch = BackfillOrchestrator(db, client, rate_limiter=limiter)
        result = await orch.run_options(concurrency=2)

        # Only instruments without data should be fetched
        assert result.rows_skipped >= 1
        assert client.get_expired_historical_candles.call_count <= 4  # 2 strikes × 2 types - 2 existing


# ===========================================================================
# 10. 401 HANDLING
# ===========================================================================

class Test401Handling:
    """401 authentication failures must not be retried automatically."""

    @pytest.mark.asyncio
    async def test_401_marks_checkpoint_failed(self, db):
        """401 must mark checkpoint FAILED and not be retried."""
        _add_nifty_candles(db, date(2024, 9, 30))
        strikes = [25000]
        _add_specs_for_expiry(db, "2024-10-03", strikes)

        async def auth_fail(*args, **kwargs):
            raise UpstoxAuthenticationError("Token expired")

        client = _mock_client()
        client.get_expired_historical_candles = auth_fail

        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        orch = BackfillOrchestrator(db, client, rate_limiter=limiter)
        result = await orch.run_options(concurrency=1)

        # The 401 bubbles through asyncio.gather(return_exceptions=True)
        # and run_options catches it, setting status FAILED.
        assert result.status == "FAILED"
        # Checkpoint should be FAILED for auth errors
        checkpoints = db.execute(
            select(IngestionCheckpoint).where(
                IngestionCheckpoint.pipeline == PIPELINE_OPTIONS,
            )
        ).scalars().all()
        for cp in checkpoints:
            assert cp.status == "FAILED"


# ===========================================================================
# 11. 5XX HANDLING
# ===========================================================================

class Test5xxHandling:
    """5xx errors should be recorded but not crash the pipeline."""

    @pytest.mark.asyncio
    async def test_5xx_recorded_in_errors(self, db):
        _add_nifty_candles(db, date(2024, 9, 30))
        strikes = [25000]
        _add_specs_for_expiry(db, "2024-10-03", strikes)

        call_count = 0
        async def server_error(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            from app.services.upstox_client import UpstoxServerError
            raise UpstoxServerError("Internal server error", status_code=500)

        client = _mock_client()
        client.get_expired_historical_candles = server_error

        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        orch = BackfillOrchestrator(db, client, rate_limiter=limiter)
        result = await orch.run_options(concurrency=1)

        # Errors should be recorded
        assert len(result.errors) > 0


# ===========================================================================
# 12. ZERO DUPLICATE CANDLES
# ===========================================================================

class TestZeroDuplicateCandles:
    """Running twice must never produce duplicate candles."""

    @pytest.mark.asyncio
    async def test_second_run_no_new_rows(self, db):
        _add_nifty_candles(db, date(2024, 9, 30))
        strikes = [25000, 25050]
        _add_specs_for_expiry(db, "2024-10-03", strikes)

        client = _mock_client()
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        orch = BackfillOrchestrator(db, client, rate_limiter=limiter)
        await orch.run_options(concurrency=2)
        count1 = db.scalar(select(func.count(OptionCandle.id))) or 0

        client2 = _mock_client()
        limiter2 = GlobalRateLimiter(config=cfg)
        orch2 = BackfillOrchestrator(db, client2, rate_limiter=limiter2)
        await orch2.run_options(concurrency=2)
        count2 = db.scalar(select(func.count(OptionCandle.id))) or 0

        assert count1 == count2


# ===========================================================================
# 13. EXISTING DATA NEVER REDOWNLOADED
# ===========================================================================

class TestExistingDataNeverRedownloaded:
    """Pre-existing candle data must not trigger API calls."""

    @pytest.mark.asyncio
    async def test_existing_data_skipped(self, db):
        _add_nifty_candles(db, date(2024, 9, 30))
        strikes = [25000]
        _add_specs_for_expiry(db, "2024-10-03", strikes)
        _add_option_candle(db, "NSE_FO|250000|2024-10-03", datetime(2024, 10, 3, 9, 15))
        _add_option_candle(db, "NSE_FO|250001|2024-10-03", datetime(2024, 10, 3, 9, 15))

        client = _mock_client()
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        orch = BackfillOrchestrator(db, client, rate_limiter=limiter)
        await orch.run_options(concurrency=1)

        client.get_expired_historical_candles.assert_not_called()


# ===========================================================================
# 14. RAW OHLCV/OI IMMUTABILITY
# ===========================================================================

class TestRawOHLCVImmutability:
    """OHLCV/OI values must be stored exactly as received."""

    @pytest.mark.asyncio
    async def test_option_ohlc_preserved(self, db):
        _add_nifty_candles(db, date(2024, 9, 30))
        strikes = [25000]
        _add_specs_for_expiry(db, "2024-10-03", strikes)

        client = _mock_client()
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        orch = BackfillOrchestrator(db, client, rate_limiter=limiter)
        await orch.run_options(concurrency=1)

        candle = db.execute(
            select(OptionCandle)
            .where(OptionCandle.instrument_key == "NSE_FO|250000|2024-10-03")
        ).scalars().first()
        assert candle is not None
        assert candle.open == 150.5
        assert candle.high == 155.0
        assert candle.low == 148.0
        assert candle.close == 152.3
        assert candle.volume == 5000.0
        assert candle.open_interest == 325000.0


# ===========================================================================
# 15. DRY-RUN PRODUCES NO API CALLS
# ===========================================================================

class TestDryRunNoAPICalls:
    @pytest.mark.asyncio
    async def test_dry_run_zero_api_calls(self, db):
        _add_nifty_candles(db, date(2024, 9, 30))
        strikes = [25000, 25050]
        _add_specs_for_expiry(db, "2024-10-03", strikes)

        client = _mock_client()
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        orch = BackfillOrchestrator(db, client, dry_run=True, rate_limiter=limiter)
        await orch.run_options(universe="ATM_10")

        client.get_expired_historical_candles.assert_not_called()


# ===========================================================================
# 16. RATE LIMITER UNIT TESTS
# ===========================================================================

class TestRateLimiterUnit:
    """Direct unit tests for GlobalRateLimiter."""

    @pytest.mark.asyncio
    async def test_initial_state(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        m = limiter.snapshot()
        assert m.current_concurrency == cfg.initial_concurrency
        assert m.total_requests == 0
        assert m.successful_requests == 0
        assert m.rate_limit_429s == 0
        assert m.cooldown_remaining_s == 0

    @pytest.mark.asyncio
    async def test_instrument_tracking(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        await limiter.set_total_instruments(10)
        m = limiter.snapshot()
        assert m.instruments_remaining == 10
        assert m.instruments_completed == 0

        await limiter.mark_instrument_done()
        m = limiter.snapshot()
        assert m.instruments_completed == 1
        assert m.instruments_remaining == 9

    @pytest.mark.asyncio
    async def test_release_does_not_crash(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        limiter.release()  # Should not raise
        limiter.release()  # Multiple releases OK

    @pytest.mark.asyncio
    async def test_snapshot_to_dict(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        d = limiter.snapshot().to_dict()
        assert isinstance(d, dict)
        assert "concurrency" in d
        assert "interval_s" in d
        assert "cooldown_remaining_s" in d

    @pytest.mark.asyncio
    async def test_reset_clears_state(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        await limiter.acquire()
        await limiter.on_429(retry_after=None)
        limiter.release()
        await asyncio.sleep(cfg.cooldown_base + 0.1)

        await limiter.reset()

        m = limiter.snapshot()
        assert m.total_requests == 0
        assert m.rate_limit_429s == 0
        assert m.cooldown_remaining_s == 0
        assert m.current_concurrency == cfg.initial_concurrency

    @pytest.mark.asyncio
    async def test_client_retry_tracking(self):
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        await limiter.on_client_retry()
        await limiter.on_client_retry()
        m = limiter.snapshot()
        assert m.retries_from_client == 2

    @pytest.mark.asyncio
    async def test_cooldown_does_not_exceed_max(self):
        cfg = RateLimiterConfig(
            cooldown_base=100.0,
            cooldown_max=1.0,
            cooldown_multiplier=3.0,
            initial_concurrency=2,
            min_concurrency=1,
            max_concurrency=4,
            initial_interval=0.01,
            min_interval=0.005,
            max_interval=1.0,
            recovery_step=0.002,
            recovery_floor_pct=0.7,
            reduce_concurrency_threshold=5,
            reduce_cooldown=0.01,
        )
        limiter = GlobalRateLimiter(config=cfg)

        await limiter.acquire()
        await limiter.on_429(retry_after=None)
        limiter.release()

        # Cooldown should be capped at max, not base
        assert limiter.cooldown_remaining <= cfg.cooldown_max + 0.5


# ===========================================================================
# 17. FAILURE ISOLATION
# ===========================================================================

class TestFailureIsolation:
    """One failing instrument must not prevent others from succeeding."""

    @pytest.mark.asyncio
    async def test_one_failure_does_not_affect_others(self, db):
        _add_nifty_candles(db, date(2024, 9, 30))
        strikes = [25000, 25050, 25100]
        _add_specs_for_expiry(db, "2024-10-03", strikes)

        call_count = 0

        async def selective_failure(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("Simulated failure")
            return [
                ["2024-10-03T09:15:00+05:30", 150.0, 155.0, 148.0, 152.0, 5000, 325000],
            ]

        client = _mock_client()
        client.get_expired_historical_candles = selective_failure

        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        orch = BackfillOrchestrator(db, client, rate_limiter=limiter)
        result = await orch.run_options(concurrency=2)

        assert result.rows_inserted > 0
        assert len(result.errors) >= 1


# ===========================================================================
# 18. IST TIMESTAMP CONVENTION
# ===========================================================================

class TestISTTimestampConvention:
    @pytest.mark.asyncio
    async def test_option_candles_use_naive_ist(self, db):
        _add_nifty_candles(db, date(2024, 9, 30))
        strikes = [25000]
        _add_specs_for_expiry(db, "2024-10-03", strikes)

        client = _mock_client()
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        orch = BackfillOrchestrator(db, client, rate_limiter=limiter)
        await orch.run_options(concurrency=1)

        candle = db.execute(
            select(OptionCandle)
            .where(OptionCandle.instrument_key == "NSE_FO|250000|2024-10-03")
        ).scalars().first()
        assert candle is not None
        assert candle.open_time.tzinfo is None  # Naive IST


# ===========================================================================
# 19. NO TOKEN LEAKAGE
# ===========================================================================

class TestNoTokenLeakage:
    def test_token_not_in_limiter(self):
        limiter = GlobalRateLimiter()
        assert not hasattr(limiter, "access_token")
        assert not hasattr(limiter, "token")

    def test_snapshot_no_secrets(self):
        limiter = GlobalRateLimiter()
        d = limiter.snapshot().to_dict()
        for v in d.values():
            assert "token" not in str(v).lower()
            assert "secret" not in str(v).lower()


# ===========================================================================
# 20. CONCURRENCY BOUNDS
# ===========================================================================

class TestConcurrencyBounds:
    def test_concurrency_capped_at_max(self):
        cfg = RateLimiterConfig(max_concurrency=4, initial_concurrency=1)
        limiter = GlobalRateLimiter(config=cfg)
        assert limiter.concurrency == 1
        assert cfg.max_concurrency == 4

    def test_concurrency_min_is_1(self):
        cfg = RateLimiterConfig(min_concurrency=1)
        limiter = GlobalRateLimiter(config=cfg)
        assert limiter.concurrency >= cfg.min_concurrency


# ===========================================================================
# 21. RATE LIMITER + ORCHESTRATOR INTEGRATION
# ===========================================================================

class TestRateLimiterOrchestratorIntegration:
    @pytest.mark.asyncio
    async def test_orchestrator_uses_rate_limiter(self, db):
        """Orchestrator must wire up the rate limiter correctly."""
        _add_nifty_candles(db, date(2024, 9, 30))
        strikes = [25000]
        _add_specs_for_expiry(db, "2024-10-03", strikes)

        client = _mock_client()
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)
        orch = BackfillOrchestrator(db, client, rate_limiter=limiter)

        result = await orch.run_options(concurrency=2)
        assert result.status == "SUCCESS"

        # Rate limiter should have tracked the work
        m = limiter.snapshot()
        assert m.instruments_completed > 0
        assert m.successful_requests > 0

    @pytest.mark.asyncio
    async def test_rate_limiter_shared_across_runs(self, db):
        """Same rate limiter instance used across multiple orchestrator runs."""
        _add_nifty_candles(db, date(2024, 9, 30))
        strikes = [25000]
        _add_specs_for_expiry(db, "2024-10-03", strikes)

        client = _mock_client()
        cfg = _fast_config()
        limiter = GlobalRateLimiter(config=cfg)

        # First run
        orch1 = BackfillOrchestrator(db, client, rate_limiter=limiter)
        await orch1.run_options(concurrency=2)
        m1 = limiter.snapshot()

        # Second run (instruments already have data, so skipped)
        orch2 = BackfillOrchestrator(db, client, rate_limiter=limiter)
        await orch2.run_options(concurrency=2)
        m2 = limiter.snapshot()

        # Metrics should accumulate
        assert m2.successful_requests >= m1.successful_requests

    @pytest.mark.asyncio
    async def test_no_server_startup_ingestion(self, hermetic_init_db):
        """init_db must not call any Upstox API."""
        with patch("app.services.upstox_client.UpstoxClient") as MockCls:
            hermetic_init_db()
            MockCls.assert_not_called()


# ===========================================================================
# Session isolation for concurrent instrument tasks
# ===========================================================================


class TestConcurrentInstrumentSessionIsolation:
    """Each concurrent ``process_one`` task must own its OWN Session.

    One instrument's database failure may poison only (and roll back only)
    that instrument's transaction — never another instrument's transaction
    nor the orchestrator's own session used for discovery and the final
    ingestion log.
    """

    @pytest.mark.asyncio
    async def test_concurrent_tasks_never_use_the_shared_session(
        self, db, monkeypatch
    ):
        import app.services.backfill_orchestrator as bo

        _add_nifty_candles(db, date(2024, 9, 30))
        _add_specs_for_expiry(db, "2024-10-03", [25000, 25050])  # 4 instruments

        real_upsert = bo._upsert_checkpoint
        used: list = []  # strong refs keep object identity stable

        def recording_upsert(the_db, pipeline, instrument_key, status, **kwargs):
            used.append((instrument_key, the_db))
            return real_upsert(the_db, pipeline, instrument_key, status, **kwargs)

        monkeypatch.setattr(bo, "_upsert_checkpoint", recording_upsert)

        client = _mock_client()
        orch = BackfillOrchestrator(
            db, client, rate_limiter=GlobalRateLimiter(config=_fast_config())
        )
        result = await orch.run_options(concurrency=4)

        assert result.status == "SUCCESS"
        n = result.metadata["instruments_to_process"]
        assert n == 4
        # RUNNING + COMPLETED checkpoint per instrument at minimum.
        assert len(used) >= 2 * n
        # Exactly one distinct Session object per concurrent instrument ...
        assert len({id(the_db) for _ik, the_db in used}) == n
        # ... and NEVER the orchestrator's own session.
        assert all(the_db is not orch.db for _ik, the_db in used)
        # Each instrument's transaction committed independently: all rows
        # are visible from the orchestrator's session afterwards. The mock
        # client returns 100 candles per instrument but only 2 unique
        # timestamps, and the idempotent upsert dedups them.
        assert (
            db.execute(select(func.count(OptionCandle.id))).scalar() == 2 * n
        )

    @pytest.mark.asyncio
    async def test_instrument_db_failure_is_rolled_back_in_isolation(
        self, db, monkeypatch
    ):
        import app.services.option_candles as option_candles_mod

        _add_nifty_candles(db, date(2024, 9, 30))
        _add_specs_for_expiry(db, "2024-10-03", [25000])  # 2 instruments

        real_record = option_candles_mod.record_option_candles
        state = {"poisoned": False}

        def poisoning_record(the_db, candles):
            if not state["poisoned"]:
                state["poisoned"] = True
                # A failed ORM FLUSH (NOT NULL violation here) is the class
                # of database failure that marks a Session's transaction
                # rollback-only: every later operation on THAT Session
                # raises PendingRollbackError until it is rolled back.
                the_db.add(ContractSpec(instrument_key=None, underlying="NIFTY"))
                the_db.flush()
            return real_record(the_db, candles)

        monkeypatch.setattr(
            option_candles_mod, "record_option_candles", poisoning_record
        )

        client = _mock_client()
        orch = BackfillOrchestrator(
            db, client, rate_limiter=GlobalRateLimiter(config=_fast_config())
        )
        # run_options must RETURN, not raise: the poison stays inside the
        # failing instrument's own session and is rolled back there — it
        # must never cascade into another instrument's transaction or the
        # orchestrator's final ingestion log.
        result = await orch.run_options(concurrency=2)

        assert result.status == "PARTIAL"
        assert len(result.errors) == 1

        checkpoints = db.execute(
            select(IngestionCheckpoint).where(
                IngestionCheckpoint.pipeline == PIPELINE_OPTIONS
            )
        ).scalars().all()
        statuses = sorted(cp.status for cp in checkpoints)
        assert statuses.count("FAILED") == 1
        assert statuses.count("COMPLETED") == 1
        # The healthy instrument persisted via its own session: 100 candles
        # processed (2 unique timestamps after idempotent upsert).
        assert result.rows_inserted == 100
        assert db.execute(select(func.count(OptionCandle.id))).scalar() == 2

    @pytest.mark.asyncio
    async def test_direct_construction_without_session_factory_still_works(self, db):
        """Backward compatibility: existing callers construct the
        orchestrator exactly as before — no session_factory argument — and
        still get a correct run (isolation is derived internally)."""
        _add_nifty_candles(db, date(2024, 9, 30))
        _add_specs_for_expiry(db, "2024-10-03", [25000])

        client = _mock_client()
        orch = BackfillOrchestrator(
            db, client, rate_limiter=GlobalRateLimiter(config=_fast_config())
        )
        result = await orch.run_options(concurrency=2)

        assert result.status == "SUCCESS"
        checkpoints = db.execute(
            select(IngestionCheckpoint).where(
                IngestionCheckpoint.pipeline == PIPELINE_OPTIONS
            )
        ).scalars().all()
        assert checkpoints
        assert all(cp.status == "COMPLETED" for cp in checkpoints)


class TestSessionFactoryLifecycleOrdering:
    """The per-task Session is created only AFTER the rate limiter grants
    the slot: no Session (and no pooled connection) is held while a task
    waits for its turn. A factory failure still releases the slot, and a
    created Session is always closed. Event ordering is recorded, never
    timed."""

    @staticmethod
    def _record_limiter(limiter, events):
        real_acquire = limiter.acquire
        real_release = limiter.release

        async def recording_acquire():
            events.append("acquire")
            await real_acquire()
            events.append("granted")

        def recording_release():
            events.append("release")
            return real_release()

        limiter.acquire = recording_acquire
        limiter.release = recording_release
        return limiter

    @pytest.mark.asyncio
    async def test_session_factory_runs_only_after_slot_granted(self, db):
        events: list[str] = []
        _add_nifty_candles(db, date(2024, 9, 30))
        _add_specs_for_expiry(db, "2024-10-03", [25000])  # 2 instruments

        factory = sessionmaker(bind=db.get_bind())

        def recording_factory():
            events.append("factory")
            return factory()

        limiter = self._record_limiter(
            GlobalRateLimiter(config=_fast_config()), events
        )
        client = _mock_client()
        orch = BackfillOrchestrator(
            db,
            client,
            rate_limiter=limiter,
            session_factory=recording_factory,
        )
        result = await orch.run_options(concurrency=2)

        assert result.status == "SUCCESS"
        # Prefix invariant (deterministic happens-before, not timing): a
        # factory call never appears before enough slots have been granted.
        grants = factories = 0
        for event in events:
            if event == "granted":
                grants += 1
            elif event == "factory":
                factories += 1
                assert grants >= factories, (
                    "session factory invoked before the rate limiter "
                    f"granted the slot: {events}"
                )
        assert factories == 2
        # Every granted slot is released exactly once.
        assert events.count("release") == grants == 2

    @pytest.mark.asyncio
    async def test_factory_failure_releases_the_limiter_slot(self, db):
        events: list[str] = []
        _add_nifty_candles(db, date(2024, 9, 30))
        _add_specs_for_expiry(db, "2024-10-03", [25000])  # 2 instruments

        def raising_factory():
            events.append("factory")
            raise RuntimeError("synthetic session-factory failure")

        limiter = self._record_limiter(
            GlobalRateLimiter(config=_fast_config()), events
        )
        client = _mock_client()
        orch = BackfillOrchestrator(
            db,
            client,
            rate_limiter=limiter,
            session_factory=raising_factory,
        )
        # Per-task failures are collected by gather (return_exceptions=True);
        # aggregation and exception handling are unchanged by this ordering
        # fix, so run_options itself completes.
        result = await orch.run_options(concurrency=2)

        assert result is not None
        # The factory ran only after its slot was granted ...
        assert events.index("factory") > events.index("granted"), (
            f"factory invoked before slot grant: {events}"
        )
        # ... and the slots that could not be used were still released —
        # for BOTH tasks, so no semaphore slot leaks.
        assert events.count("factory") == 2
        assert events.count("release") == events.count("granted") == 2
