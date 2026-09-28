"""Lightweight per-session rate limiter (Phase 9B).

Provides session-scoped rate limiting for authenticated endpoints.
Designed for the current single-process architecture with a clear
migration path to Redis for multi-process deployment.

Architecture:
    session_id → {endpoint → [timestamps]}

Memory: O(active_sessions × endpoints × window_entries)
For 100 users × 5 endpoints × 60 entries = 30,000 floats ≈ 240KB

Limits are configurable per endpoint. Default: 60 requests/minute.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from fastapi import HTTPException, Request


@dataclass
class RateLimitRule:
    """Configuration for a rate-limited endpoint."""
    max_requests: int = 60
    window_seconds: int = 60


# Default rules for GEX/market-data endpoints
DEFAULT_RULES: dict[str, RateLimitRule] = {
    "/gex/live": RateLimitRule(max_requests=30, window_seconds=60),
    "/gex/capture": RateLimitRule(max_requests=10, window_seconds=60),
    "/gex/snapshots": RateLimitRule(max_requests=60, window_seconds=60),
    "/chains": RateLimitRule(max_requests=30, window_seconds=60),
}


class SessionRateLimiter:
    """Per-session rate limiter.

    Each session gets independent limits. One user's rate limit
    never affects another user.

    Usage in a FastAPI dependency::

        limiter = SessionRateLimiter()

        @router.get("/gex/live")
        async def get_live_gex(request: Request, session_id: str = Depends(get_session_id)):
            limiter.check(session_id, "/gex/live")
            ...
    """

    def __init__(self, rules: dict[str, RateLimitRule] | None = None):
        self._rules = rules or dict(DEFAULT_RULES)
        # client_key → endpoint → list of timestamps
        self._hits: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    def add_rule(self, endpoint: str, rule: RateLimitRule) -> None:
        """Add or update a rate limit rule for an endpoint.

        Parameters
        ----------
        endpoint : str
            The endpoint path (e.g. ``"/auth/login-email"``).
        rule : RateLimitRule
            The rate limit configuration.
        """
        self._rules[endpoint] = rule

    def check(self, session_id: str | None, endpoint: str, client_id: str | None = None) -> None:
        """Check rate limit. Raises HTTPException 429 if exceeded.

        For authenticated endpoints, pass ``session_id``.  For unauthenticated
        endpoints (login, register) where no session exists yet, pass
        ``client_id`` (typically the request IP or a fingerprint).

        Parameters
        ----------
        session_id : str, optional
            The authenticated session ID.
        endpoint : str
            The endpoint path (used for rule lookup).
        client_id : str, optional
            A client identifier for unauthenticated requests
            (e.g. ``"unauth:192.168.1.1"``).  Used as the rate-limit
            key when ``session_id`` is None.

        Raises
        ------
        HTTPException
            429 Too Many Requests if rate limit exceeded.
        """
        key = session_id or client_id
        if not key:
            return  # Cannot rate limit without any identifier

        rule = self._get_rule(endpoint)
        if rule is None:
            return  # No rate limit configured for this endpoint

        now = time.time()
        window_start = now - rule.window_seconds

        # Get hits for this client+endpoint
        hits = self._hits[key][endpoint]

        # Remove expired entries
        self._hits[key][endpoint] = [t for t in hits if t > window_start]

        # Check limit
        if len(self._hits[key][endpoint]) >= rule.max_requests:
            retry_after = int(rule.window_seconds - (now - self._hits[key][endpoint][0]))
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limit_exceeded",
                    "endpoint": endpoint,
                    "limit": rule.max_requests,
                    "window_seconds": rule.window_seconds,
                    "retry_after_seconds": max(1, retry_after),
                },
                headers={"Retry-After": str(max(1, retry_after))},
            )

        # Record this hit
        self._hits[key][endpoint].append(now)

    def cleanup(self, max_age_seconds: int = 600) -> int:
        """Remove stale entries for sessions inactive beyond max_age.

        Returns the number of session entries cleaned up.
        """
        now = time.time()
        cleaned = 0
        empty_sessions = []

        for session_id, endpoints in self._hits.items():
            session_active = False
            for endpoint, hits in list(endpoints.items()):
                # Remove old hits
                fresh = [t for t in hits if t > now - max_age_seconds]
                if fresh:
                    endpoints[endpoint] = fresh
                    session_active = True
                else:
                    del endpoints[endpoint]
            if not session_active:
                empty_sessions.append(session_id)
                cleaned += 1

        for sid in empty_sessions:
            del self._hits[sid]

        return cleaned

    def _get_rule(self, endpoint: str) -> RateLimitRule | None:
        """Find the matching rate limit rule for an endpoint."""
        # Exact match first
        if endpoint in self._rules:
            return self._rules[endpoint]
        # Prefix match (e.g. "/chains/NIFTY" matches "/chains")
        for prefix, rule in self._rules.items():
            if endpoint.startswith(prefix):
                return rule
        return None


# Global instance — scoped to the process
rate_limiter = SessionRateLimiter()


# ---------------------------------------------------------------------------
# Phase 7.24.8C — Global adaptive rate limiter for bulk backfills
# ---------------------------------------------------------------------------
import asyncio as _asyncio

_backfill_logger = logging.getLogger(__name__)


@dataclass
class RateLimiterConfig:
    """Tuning knobs for :class:`GlobalRateLimiter`.

    All durations are seconds.  The limiter paces acquisitions by a
    dynamic ``interval``; 429 responses trigger a global cooldown and
    widen the interval, sustained success narrows it again (down to
    ``min_interval * recovery_floor_pct``).  After
    ``reduce_concurrency_threshold`` consecutive 429s the concurrency
    ceiling is stepped down toward ``min_concurrency``.
    """

    initial_concurrency: int = 5
    min_concurrency: int = 1
    max_concurrency: int = 6

    initial_interval: float = 0.25
    min_interval: float = 0.05
    max_interval: float = 5.0

    cooldown_base: float = 1.0
    cooldown_max: float = 60.0
    cooldown_multiplier: float = 2.0

    recovery_step: float = 0.01
    recovery_floor_pct: float = 0.7

    reduce_concurrency_threshold: int = 3
    reduce_cooldown: float = 0.5


@dataclass
class RateLimiterMetrics:
    """Point-in-time snapshot of the adaptive limiter state.

    Contains only operational counters — never credentials or tokens.
    """

    current_concurrency: int = 0
    interval_s: float = 0.0
    cooldown_remaining_s: float = 0.0
    total_requests: int = 0
    successful_requests: int = 0
    rate_limit_429s: int = 0
    consecutive_429s: int = 0
    retries_from_client: int = 0
    total_cooldown_time_s: float = 0.0
    total_instruments: int = 0
    instruments_completed: int = 0
    instruments_remaining: int = 0
    instruments_failed: int = 0

    def to_dict(self) -> dict:
        return {
            "concurrency": self.current_concurrency,
            "interval_s": self.interval_s,
            "cooldown_remaining_s": self.cooldown_remaining_s,
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "rate_limit_429s": self.rate_limit_429s,
            "consecutive_429s": self.consecutive_429s,
            "retries_from_client": self.retries_from_client,
            "total_cooldown_time_s": self.total_cooldown_time_s,
            "total_instruments": self.total_instruments,
            "instruments_completed": self.instruments_completed,
            "instruments_remaining": self.instruments_remaining,
            "instruments_failed": self.instruments_failed,
        }


class GlobalRateLimiter:
    """Global adaptive rate limiter shared by all backfill workers.

    This is NOT the same as ``SessionRateLimiter`` (per-user HTTP
    throttling for authenticated endpoints).  ``GlobalRateLimiter``
    gates bulk Upstox backfill traffic: it caps concurrency with a
    semaphore, spaces acquisitions by a dynamic interval, enters a
    global cooldown on 429 responses (honouring ``Retry-After`` when
    supplied, exponential backoff otherwise), and gradually recovers
    throughput after sustained success.

    Deliberately in-process and dependency-free (no Redis) so the
    implementation can be swapped later without changing callers.
    """

    def __init__(
        self,
        config: RateLimiterConfig | None = None,
        max_concurrency: int | None = None,
        **_kwargs: object,
    ):
        self._config = config or RateLimiterConfig()
        cfg = self._config
        if max_concurrency is not None:
            # Legacy positional-style construction
            cfg.initial_concurrency = int(max_concurrency)
        self._concurrency: int = max(
            cfg.min_concurrency, min(cfg.initial_concurrency, cfg.max_concurrency)
        )
        self._semaphore: _asyncio.Semaphore = _asyncio.Semaphore(self._concurrency)

        self._interval: float = float(cfg.initial_interval)
        self._last_request: float | None = None
        self._cooldown_until: float = 0.0
        self._cooldown_total: float = 0.0
        self._consecutive_429s: int = 0
        # Adaptive state is worker-lifetime, but asyncio synchronization
        # primitives are event-loop-local. The worker currently executes
        # each job through asyncio.run(), so the lock is rebound lazily
        # when the next job enters a different event loop.
        self._lock = _asyncio.Lock()
        self._lock_loop = None

        self._total_requests: int = 0
        self._successful: int = 0
        self._rate_429s: int = 0
        self._retries_from_client: int = 0

        self._total_instruments: int = 0
        self._instruments_done: int = 0
        self._instruments_failed: int = 0

    # -- state ------------------------------------------------------------------

    @property
    def config(self) -> RateLimiterConfig:
        return self._config

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @property
    def interval(self) -> float:
        """Current pacing interval in seconds between acquisitions."""
        return self._interval

    @property
    def cooldown_remaining(self) -> float:
        """Seconds left in the global cooldown (0 when clear)."""
        return max(0.0, self._cooldown_until - time.monotonic())

    # -- worker lifecycle ---------------------------------------------------------

    async def acquire(self) -> None:
        """Acquire a worker slot after global pacing and cooldown.

        ``run_worker`` preserves this limiter across jobs, while each
        historical-ingestion job currently runs inside a fresh
        ``asyncio.run()`` event loop. The adaptive/cooldown state remains
        shared across those loops; only this async mutex is rebound when
        the loop changes.
        """
        loop = _asyncio.get_running_loop()
        if self._lock_loop is not loop:
            self._lock = _asyncio.Lock()
            self._lock_loop = loop
        async with self._lock:
            remaining = self._cooldown_until - time.monotonic()
            if remaining > 0:
                await _asyncio.sleep(remaining)
            if self._last_request is not None:
                gap = self._interval - (time.monotonic() - self._last_request)
                if gap > 0:
                    await _asyncio.sleep(gap)
            self._last_request = time.monotonic()
            self._total_requests += 1
        await self._semaphore.acquire()

    def release(self) -> None:
        try:
            self._semaphore.release()
        except ValueError:
            pass  # release when nothing acquired

    async def on_success(self) -> None:
        """Record a successful request and gradually recover throughput."""
        cfg = self._config
        self._successful += 1
        self._consecutive_429s = 0
        floor = cfg.min_interval * cfg.recovery_floor_pct
        if self._interval > floor:
            self._interval = max(floor, self._interval - cfg.recovery_step)
        await self._maybe_increase_concurrency()

    async def on_429(self, retry_after: float | None = None) -> None:
        """Record a 429: global cooldown, wider interval, adaptive concurrency.

        ``retry_after`` (from the ``Retry-After`` response header) is
        honoured when supplied and positive; otherwise an exponential
        backoff based on consecutive 429s is used.  Either way the
        cooldown is capped at ``cooldown_max``.
        """
        cfg = self._config
        self._rate_429s += 1
        self._consecutive_429s += 1

        if retry_after is not None and retry_after > 0:
            cooldown = float(retry_after)
        else:
            cooldown = cfg.cooldown_base * (
                cfg.cooldown_multiplier ** max(0, self._consecutive_429s - 1)
            )
        cooldown = min(cooldown, cfg.cooldown_max)

        self._cooldown_until = max(self._cooldown_until, time.monotonic() + cooldown)
        self._cooldown_total = cooldown

        # Widen the pacing interval in proportion to the cooldown severity
        self._interval = min(cfg.max_interval, max(self._interval, cooldown))

        # Step concurrency down after sustained 429 storms
        if (
            self._consecutive_429s >= cfg.reduce_concurrency_threshold
            and self._concurrency > cfg.min_concurrency
        ):
            self._concurrency -= 1
            self._semaphore = _asyncio.Semaphore(self._concurrency)

    async def on_error(self) -> None:
        """Record a non-429 instrument failure (never throttles)."""
        self._instruments_failed += 1

    async def on_client_retry(self) -> None:
        """Record a retry issued by the HTTP client layer."""
        self._retries_from_client += 1

    async def set_total_instruments(self, count: int) -> None:
        self._total_instruments = count

    async def mark_instrument_done(self) -> None:
        self._instruments_done += 1

    async def reset(self) -> None:
        """Clear all adaptive state, counters, and concurrency ceilings."""
        cfg = self._config
        self._interval = float(cfg.initial_interval)
        self._last_request = None
        self._cooldown_until = 0.0
        self._cooldown_total = 0.0
        self._consecutive_429s = 0
        self._total_requests = 0
        self._successful = 0
        self._rate_429s = 0
        self._retries_from_client = 0
        self._concurrency = max(
            cfg.min_concurrency, min(cfg.initial_concurrency, cfg.max_concurrency)
        )
        self._semaphore = _asyncio.Semaphore(self._concurrency)

    async def _maybe_increase_concurrency(self) -> None:
        """Step concurrency back toward its configured ceiling after success."""
        cfg = self._config
        ceiling = min(cfg.initial_concurrency, cfg.max_concurrency)
        if self._concurrency < ceiling:
            self._concurrency += 1
            self._semaphore = _asyncio.Semaphore(self._concurrency)

    # -- observability -------------------------------------------------------------

    def snapshot(self) -> RateLimiterMetrics:
        return RateLimiterMetrics(
            current_concurrency=self._concurrency,
            interval_s=self._interval,
            cooldown_remaining_s=self.cooldown_remaining,
            total_requests=self._total_requests,
            successful_requests=self._successful,
            rate_limit_429s=self._rate_429s,
            consecutive_429s=self._consecutive_429s,
            retries_from_client=self._retries_from_client,
            total_cooldown_time_s=self._cooldown_total,
            total_instruments=self._total_instruments,
            instruments_completed=self._instruments_done,
            instruments_remaining=max(0, self._total_instruments - self._instruments_done),
            instruments_failed=self._instruments_failed,
        )

    def log_status(self) -> None:
        """Log the current adaptive state at INFO level (operational data only)."""
        m = self.snapshot()
        _backfill_logger.info(
            "GlobalRateLimiter: concurrency=%d interval=%.3fs cooldown=%.1fs "
            "requests=%d success=%d 429s=%d consecutive_429s=%d instruments=%d/%d",
            m.current_concurrency, m.interval_s, m.cooldown_remaining_s,
            m.total_requests, m.successful_requests, m.rate_limit_429s,
            m.consecutive_429s, m.instruments_completed, m.total_instruments,
        )
