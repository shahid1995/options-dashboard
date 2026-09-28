"""Unified historical backfill orchestrator — Phase 7.24.5 / 7.24.8B.

Single, reliable, resumable CLI entry point for all historical data
ingestion into the local database.

Key architectural rules:
  - **CLI-only**: never runs on server startup, restart, or reload.
  - **Local-first**: checks existing data before requesting from Upstox.
  - **Idempotent**: repeated execution produces zero duplicates.
  - **Resumable**: interrupted backfills continue from checkpoints.
  - **One instrument at a time**: each instrument has independent
    transaction boundary, logging, and checkpoint.
  - **Raw data immutable**: never overwrites existing OHLCV/OI.
  - **No Greeks**: Greeks reconstruction is a separate pipeline.
  - **Bounded concurrency**: asyncio.Semaphore limits parallel requests.
  - **Adaptive**: reduces concurrency on 429 rate-limit responses.

Architecture::

    CLI (run_backfill.py)
          │
          ▼
    BackfillOrchestrator
          │
    ┌─────┴─────┐
    ▼           ▼
 UpstoxClient  Local Database
    │           │
    ▼           ▼
 Upstox API  contract_specs
             nifty_candles
             option_candles
             ingestion_log
             ingestion_checkpoint
             data_completeness
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ContractSpec,
    IngestionCheckpoint,
    IngestionLog,
    NiftyCandle,
    OptionCandle,
    DataCompleteness,
)
from app.services.upstox_client import (
    UpstoxClient,
    UpstoxAuthenticationError,
    UpstoxClientError,
    UpstoxRateLimitError,
)
from app.services.rate_limiter import GlobalRateLimiter, RateLimiterConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"
NIFTY_SYMBOL = "NIFTY"
DEFAULT_INTERVAL_STR = "3min"
DEFAULT_INTERVAL_API = "3minute"
CANDLE_CHUNK_DAYS = 28  # Upstox 3-min limit: 1 month
REQUEST_DELAY_SECONDS = 3.0  # Conservative delay to avoid rate limiting

# Phase 7.24.8C: Concurrency constants (rate limiter replaces old adaptive logic)
DEFAULT_CONCURRENCY = 1       # Start conservatively; rate limiter auto-widens
MAX_CONCURRENCY = 6           # Safe ceiling from Phase 7.24.8A benchmarks
ADAPTIVE_REDUCE_THRESHOLD = 3  # Kept for backward compat; rate limiter is primary
ADAPTIVE_WINDOW_SIZE = 20
ADAPTIVE_COOLDOWN_SECONDS = 5.0

# Pipeline names for checkpoint
PIPELINE_CONTRACTS = "backfill_contracts"
PIPELINE_NIFTY = "backfill_nifty"
PIPELINE_OPTIONS = "backfill_options"


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class BackfillResult:
    """Aggregated result of a backfill operation."""
    operation: str
    started_at: str = ""
    completed_at: str = ""
    status: str = "PENDING"
    api_calls: int = 0
    rows_fetched: int = 0
    rows_inserted: int = 0
    rows_skipped: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Token bridge — connects in-memory token_store to UpstoxTokenManager
# ---------------------------------------------------------------------------

class TokenBridge:
    """Provides access token by checking persistent cache then in-memory store.

    This allows the CLI to work both with and without a running FastAPI server.
    """

    def __init__(self, session_id: str | None = None):
        self._session_id = session_id

    def get_token(self) -> str | None:
        # 1. Try persistent token cache
        from app.services.upstox_token_manager import UpstoxTokenManager
        manager = UpstoxTokenManager()
        token = manager.get_token()
        if token:
            return token

        # 2. Try in-memory token store (server running)
        from app.services.token_store import get_token
        return get_token(self._session_id) if self._session_id else None


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _get_checkpoint(
    db: Session, pipeline: str, instrument_key: str,
) -> IngestionCheckpoint | None:
    """Get existing checkpoint for a pipeline+instrument."""
    return db.execute(
        select(IngestionCheckpoint).where(
            IngestionCheckpoint.pipeline == pipeline,
            IngestionCheckpoint.instrument_key == instrument_key,
        )
    ).scalar_one_or_none()


def _upsert_checkpoint(
    db: Session,
    pipeline: str,
    instrument_key: str,
    status: str,
    run_id: str | None = None,
    items_processed: int = 0,
    items_total: int = 0,
    error_message: str | None = None,
) -> IngestionCheckpoint:
    """Create or update a checkpoint record."""
    now_str = datetime.now(timezone.utc).isoformat()
    existing = _get_checkpoint(db, pipeline, instrument_key)

    if existing:
        existing.status = status
        existing.items_processed = items_processed
        existing.items_total = items_total
        existing.updated_at = now_str
        if run_id:
            existing.run_id = run_id
        if error_message:
            existing.error_message = error_message
        if status == "COMPLETED":
            existing.completed_at = now_str
        elif status == "RUNNING" and not existing.started_at:
            existing.started_at = now_str
        return existing
    else:
        cp = IngestionCheckpoint(
            pipeline=pipeline,
            instrument_key=instrument_key,
            run_id=run_id,
            status=status,
            items_processed=items_processed,
            items_total=items_total,
            error_message=error_message,
            started_at=now_str if status == "RUNNING" else None,
            completed_at=now_str if status == "COMPLETED" else None,
            updated_at=now_str,
        )
        db.add(cp)
        db.flush()
        return cp


def _log_ingestion(
    db: Session,
    run_id: str,
    operation: str,
    status: str,
    instrument_key: str | None = None,
    expiry_date: str | None = None,
    session_date: str | None = None,
    api_calls: int = 0,
    rows_fetched: int = 0,
    rows_inserted: int = 0,
    rows_skipped: int = 0,
    duplicates: int = 0,
    error_category: str | None = None,
    error_message: str | None = None,
    metadata_json: str | None = None,
) -> None:
    """Write an ingestion log entry."""
    now_str = datetime.now(timezone.utc).isoformat()
    log = IngestionLog(
        run_id=run_id,
        operation=operation,
        instrument_key=instrument_key,
        expiry_date=expiry_date,
        session_date=session_date,
        started_at=now_str,
        completed_at=now_str if status in ("SUCCESS", "FAILED", "PARTIAL") else None,
        status=status,
        api_calls=api_calls,
        rows_fetched=rows_fetched,
        rows_inserted=rows_inserted,
        rows_skipped=rows_skipped,
        duplicates=duplicates,
        error_category=error_category,
        error_message=error_message,
        metadata_json=metadata_json,
    )
    db.add(log)
    db.flush()


# ---------------------------------------------------------------------------
# Backfill orchestrator
# ---------------------------------------------------------------------------

class BackfillOrchestrator:
    """Unified historical data backfill orchestrator.

    Usage::

        token_bridge = TokenBridge(session_id=sid)
        client = UpstoxClient(token_provider=token_bridge)
        orchestrator = BackfillOrchestrator(db, client)

        # Dry run
        await orchestrator.run_dry_run()

        # Full backfill
        await orchestrator.run_all()

        # ATM ±10 universe with concurrency
        await orchestrator.run_options(universe="ATM_10", concurrency=4)
    """

    def __init__(
        self,
        db: Session,
        client: UpstoxClient,
        *,
        dry_run: bool = False,
        force: bool = False,
        rate_limiter: GlobalRateLimiter | None = None,
    ):
        self.db = db
        self.client = client
        self.dry_run = dry_run
        self.force = force
        self.run_id = f"backfill_{uuid.uuid4().hex[:12]}"
        self._api_calls = 0
        # Phase 7.24.8C: Global rate limiter (shared across workers)
        self._rate_limiter = rate_limiter or GlobalRateLimiter()
        # Backward-compat aliases used by existing tests
        self._current_concurrency = self._rate_limiter.concurrency
        self._recent_429_count = 0
        self._recent_request_count = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_dry_run(self) -> dict:
        """Inspect database and report what would be fetched. Zero API calls."""
        plan: dict[str, Any] = {}

        # 1. Contract metadata status
        total_specs = self.db.scalar(select(func.count(ContractSpec.id))) or 0
        nifty_specs = self.db.scalar(
            select(func.count(ContractSpec.id)).where(ContractSpec.underlying == NIFTY_SYMBOL)
        ) or 0
        plan["contracts"] = {
            "total_in_registry": total_specs,
            "nifty_in_registry": nifty_specs,
        }

        # 2. NIFTY candle status
        nifty_count = self.db.scalar(select(func.count(NiftyCandle.id))) or 0
        nifty_earliest = self.db.scalar(
            select(NiftyCandle.open_time).order_by(NiftyCandle.open_time.asc()).limit(1)
        )
        nifty_latest = self.db.scalar(
            select(NiftyCandle.open_time).order_by(NiftyCandle.open_time.desc()).limit(1)
        )
        plan["nifty_candles"] = {
            "total": nifty_count,
            "earliest": str(nifty_earliest) if nifty_earliest else None,
            "latest": str(nifty_latest) if nifty_latest else None,
        }

        # 3. Option candle status
        option_count = self.db.scalar(select(func.count(OptionCandle.id))) or 0
        instruments_with_data = self.db.execute(
            select(OptionCandle.instrument_key).distinct()
        ).scalars().all()
        plan["option_candles"] = {
            "total": option_count,
            "instruments_with_data": len(instruments_with_data),
            "in_registry_missing_data": max(0, nifty_specs - len(instruments_with_data)),
        }

        # 4. Checkpoint status
        completed_checkpoints = self.db.scalar(
            select(func.count(IngestionCheckpoint.id)).where(
                IngestionCheckpoint.status == "COMPLETED"
            )
        ) or 0
        plan["checkpoints"] = {
            "completed": completed_checkpoints,
        }

        # 5. Estimated work
        contracts_to_fetch = max(0, nifty_specs - option_count)
        plan["estimated_work"] = {
            "contracts_needing_candles": contracts_to_fetch,
            "estimated_api_calls": contracts_to_fetch * 2,  # ~1-2 per contract
        }

        return plan

    async def run_all(
        self,
        *,
        stages: list[str] | None = None,
        nifty_start_date: date | None = None,
        options_concurrency: int | None = None,
    ) -> BackfillResult:
        """Run the full backfill pipeline.

        Parameters
        ----------
        stages:
            Which stages to run. Default: ["contracts", "nifty", "options"].
        nifty_start_date:
            Override start date for NIFTY backfill.  When *None*,
            the default covers the full contract-registry range.
        """
        if stages is None:
            stages = ["contracts", "nifty", "options"]

        result = BackfillResult(
            operation="backfill_all",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        start_time = time.time()

        try:
            if "contracts" in stages:
                contract_result = await self.run_contracts()
                result.api_calls += contract_result.api_calls
                result.rows_fetched += contract_result.rows_fetched
                result.rows_inserted += contract_result.rows_inserted
                result.errors.extend(contract_result.errors)

            if "nifty" in stages:
                nifty_result = await self.run_nifty(start_date=nifty_start_date)
                result.api_calls += nifty_result.api_calls
                result.rows_fetched += nifty_result.rows_fetched
                result.rows_inserted += nifty_result.rows_inserted
                result.errors.extend(nifty_result.errors)

            if "options" in stages:
                # Forward an explicitly requested concurrency so the option
                # stage's limiter ceiling reflects the caller's request;
                # when omitted, run_options keeps its own default (existing
                # CLI behavior is unchanged).
                if options_concurrency is not None:
                    options_result = await self.run_options(
                        concurrency=options_concurrency
                    )
                else:
                    options_result = await self.run_options()
                result.api_calls += options_result.api_calls
                result.rows_fetched += options_result.rows_fetched
                result.rows_inserted += options_result.rows_inserted
                result.rows_skipped += options_result.rows_skipped
                result.errors.extend(options_result.errors)

            result.status = "SUCCESS" if not result.errors else "PARTIAL"
        except UpstoxAuthenticationError:
            result.status = "FAILED"
            result.errors.append("Authentication failed. Please re-authenticate.")
            _log_ingestion(
                self.db, self.run_id, result.operation, "FAILED",
                error_category="AUTH_EXPIRED",
                error_message="Access token expired or invalid",
            )
            self.db.commit()
        except Exception as e:
            result.status = "FAILED"
            result.errors.append(f"Unexpected error: {e}")
            _log_ingestion(
                self.db, self.run_id, result.operation, "FAILED",
                error_category="UNKNOWN",
                error_message=str(e)[:500],
            )
            self.db.commit()

        result.completed_at = datetime.now(timezone.utc).isoformat()
        result.elapsed_seconds = round(time.time() - start_time, 2)
        return result

    # ------------------------------------------------------------------
    # Stage 1: Contract metadata
    # ------------------------------------------------------------------

    async def run_contracts(self, expiry: str | None = None) -> BackfillResult:
        """Discover and persist expired contract metadata from Upstox."""
        result = BackfillResult(operation="contract_metadata")
        start_time = time.time()

        try:
            # Discover available expiries
            raw_expiries = await self.client.get_expiries(NIFTY_INDEX_KEY)
            result.api_calls += 1
            self._api_calls += 1

            if not raw_expiries:
                result.status = "SUCCESS"
                result.completed_at = datetime.now(timezone.utc).isoformat()
                result.elapsed_seconds = round(time.time() - start_time, 2)
                return result

            expiries = sorted(raw_expiries)
            if expiry:
                expiries = [e for e in expiries if e == expiry]

            result.metadata["expiries_discovered"] = len(expiries)

            if self.dry_run:
                result.status = "DRY_RUN"
                result.metadata["expiries"] = expiries
                result.completed_at = datetime.now(timezone.utc).isoformat()
                result.elapsed_seconds = round(time.time() - start_time, 2)
                return result

            for exp_date in expiries:
                try:
                    raw_contracts = await self.client.get_contracts(
                        NIFTY_INDEX_KEY, exp_date
                    )
                    result.api_calls += 1
                    self._api_calls += 1
                    result.rows_fetched += len(raw_contracts)

                    if not raw_contracts:
                        continue

                    # Upsert contracts
                    inserted, idempotent = self._upsert_contracts(
                        raw_contracts, exp_date,
                    )
                    result.rows_inserted += inserted
                    result.rows_skipped += idempotent

                    time.sleep(REQUEST_DELAY_SECONDS)

                except UpstoxAuthenticationError:
                    raise
                except UpstoxClientError as e:
                    result.errors.append(f"Expiry {exp_date}: {e.message}")
                    logger.warning("Contract fetch failed for %s: %s", exp_date, e.message)
                except Exception as e:
                    result.errors.append(f"Expiry {exp_date}: {e}")

            result.status = "SUCCESS" if not result.errors else "PARTIAL"

        except UpstoxAuthenticationError as e:
            result.status = "FAILED"
            result.errors.append(f"Authentication failed: {e.message}")
        except Exception as e:
            result.status = "FAILED"
            result.errors.append(str(e))

        _log_ingestion(
            self.db, self.run_id, "contract_metadata", result.status,
            api_calls=result.api_calls,
            rows_fetched=result.rows_fetched,
            rows_inserted=result.rows_inserted,
            rows_skipped=result.rows_skipped,
            error_message=result.errors[0] if result.errors else None,
        )
        self.db.commit()
        result.completed_at = datetime.now(timezone.utc).isoformat()
        result.elapsed_seconds = round(time.time() - start_time, 2)
        return result

    def _upsert_contracts(
        self, raw_contracts: list[dict], expiry: str,
    ) -> tuple[int, int]:
        """Upsert contracts into contract_specs. Returns (inserted, idempotent)."""
        from app.services.contract_metadata import upsert_contract_specs, SOURCE_UPSTOX_EXPIRED

        source_ref = f"BACKFILL_ORCHESTRATOR/{NIFTY_SYMBOL}/{expiry}"
        results = upsert_contract_specs(
            self.db, raw_contracts,
            source=SOURCE_UPSTOX_EXPIRED,
            source_reference=source_ref,
        )
        inserted = sum(1 for r in results if r.action == "inserted")
        idempotent = sum(1 for r in results if r.action in ("idempotent", "filled_lot_size"))
        return inserted, idempotent

    # ------------------------------------------------------------------
    # Stage 2: NIFTY index candles
    # ------------------------------------------------------------------

    async def run_nifty(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> BackfillResult:
        """Fetch and persist NIFTY index candles."""
        result = BackfillResult(operation="nifty_candles")
        start_time = time.time()

        try:
            today = datetime.now(timezone.utc).date()
            if end_date is None:
                end_date = today

            # Default start_date: earliest contract expiry date minus 3 day buffer,
            # so we always have NIFTY candles for ATM calculation of all expiries.
            # Falls back to 365 days ago if registry is empty.
            if start_date is None:
                from datetime import timedelta as _td
                earliest_expiry_str = self.db.scalar(
                    select(func.min(ContractSpec.expiry)).where(
                        ContractSpec.underlying == NIFTY_SYMBOL
                    )
                )
                if earliest_expiry_str:
                    earliest_expiry = datetime.strptime(
                        earliest_expiry_str, "%Y-%m-%d"
                    ).date()
                    start_date = earliest_expiry - _td(days=3)
                else:
                    start_date = today - _td(days=365)

            # Generate chunks
            chunks = _generate_date_chunks(start_date, end_date, CANDLE_CHUNK_DAYS)
            result.metadata["total_chunks"] = len(chunks)

            if self.dry_run:
                result.status = "DRY_RUN"
                result.metadata["chunks"] = [
                    {"from": str(cs), "to": str(ce)} for cs, ce in chunks
                ]
                result.completed_at = datetime.now(timezone.utc).isoformat()
                result.elapsed_seconds = round(time.time() - start_time, 2)
                return result

            for chunk_start, chunk_end in chunks:
                # Resume: check if this chunk has data
                if not self.force and _chunk_has_data(
                    self.db, NIFTY_SYMBOL, chunk_start, chunk_end,
                ):
                    result.rows_skipped += 1
                    continue

                try:
                    to_str = chunk_end.isoformat()
                    from_str = chunk_start.isoformat()

                    candles = await self.client.get_historical_candles(
                        NIFTY_INDEX_KEY, to_str, from_str,
                    )
                    result.api_calls += 1
                    self._api_calls += 1
                    result.rows_fetched += len(candles)

                    if candles:
                        from app.services.candle_ingestion import normalize_candles
                        from app.services.nifty_candles import record_candles
                        from app.services.candle_validation import validate_candle

                        normalized = normalize_candles(candles, symbol=NIFTY_SYMBOL)
                        valid = [c for c in normalized if validate_candle(c, 0).is_valid]
                        inserted = record_candles(self.db, valid)
                        result.rows_inserted += inserted

                    time.sleep(REQUEST_DELAY_SECONDS)

                except UpstoxAuthenticationError:
                    raise
                except UpstoxClientError as e:
                    result.errors.append(f"Chunk {chunk_start}: {e.message}")
                except Exception as e:
                    result.errors.append(f"Chunk {chunk_start}: {e}")

            result.status = "SUCCESS" if not result.errors else "PARTIAL"

        except UpstoxAuthenticationError as e:
            result.status = "FAILED"
            result.errors.append(f"Authentication failed: {e.message}")
        except Exception as e:
            result.status = "FAILED"
            result.errors.append(str(e))

        _log_ingestion(
            self.db, self.run_id, "nifty_candles", result.status,
            api_calls=result.api_calls,
            rows_fetched=result.rows_fetched,
            rows_inserted=result.rows_inserted,
            rows_skipped=result.rows_skipped,
            error_message=result.errors[0] if result.errors else None,
        )
        self.db.commit()
        result.completed_at = datetime.now(timezone.utc).isoformat()
        result.elapsed_seconds = round(time.time() - start_time, 2)
        return result

    # ------------------------------------------------------------------
    # Stage 3: Option candles (one instrument at a time)
    # ------------------------------------------------------------------

    async def run_options(
        self,
        expiry: str | None = None,
        max_instruments: int | None = None,
        universe: str | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> BackfillResult:
        """Fetch and persist option candles for instruments in contract_specs.

        Processes instruments with bounded concurrency and independent checkpointing.
        Uses the global rate limiter for adaptive request pacing and 429 handling.

        Parameters
        ----------
        expiry:
            Optional filter to a specific expiry date.
        max_instruments:
            Optional cap on number of instruments to process.
        universe:
            "ATM_5", "ATM_10", "ATM_20", "ATM_30", or None for all.
        concurrency:
            Number of concurrent workers (default 1, max 6).  The rate
            limiter may adjust this dynamically at runtime.
        """
        result = BackfillResult(operation="option_candles")
        start_time = time.time()
        concurrency = max(1, min(concurrency, MAX_CONCURRENCY))
        # Update the rate limiter's concurrency ceiling
        self._rate_limiter._concurrency = concurrency
        self._rate_limiter._semaphore = asyncio.Semaphore(concurrency)
        self._current_concurrency = concurrency

        try:
            # Discover instruments from contract_specs
            stmt = select(ContractSpec).where(ContractSpec.underlying == NIFTY_SYMBOL)
            if expiry:
                stmt = stmt.where(ContractSpec.expiry == expiry)

            specs = self.db.execute(stmt).scalars().all()
            if not specs:
                result.status = "SUCCESS"
                result.completed_at = datetime.now(timezone.utc).isoformat()
                result.elapsed_seconds = round(time.time() - start_time, 2)
                return result

            # Phase 7.24.8B: Apply universe filter
            if universe:
                specs = self._filter_by_universe(specs, universe)
                result.metadata["universe"] = universe
                result.metadata["universe_instruments"] = len(specs)

            # Check which instruments already have data
            instruments_with_data = set(
                self.db.execute(
                    select(OptionCandle.instrument_key).distinct()
                ).scalars().all()
            )

            # Filter to instruments needing work
            if not self.force:
                remaining = [
                    s for s in specs
                    if s.instrument_key not in instruments_with_data
                ]
            else:
                remaining = list(specs)

            result.rows_skipped = len(specs) - len(remaining)
            result.metadata["total_instruments"] = len(specs)
            result.metadata["instruments_with_data"] = len(instruments_with_data)
            result.metadata["instruments_to_process"] = len(remaining)

            if max_instruments:
                remaining = remaining[:max_instruments]

            # Phase 7.24.8C: Set total instrument count for rate-limiter metrics
            await self._rate_limiter.set_total_instruments(len(remaining))

            if self.dry_run:
                result.status = "DRY_RUN"
                result.metadata["sample_instruments"] = [
                    {
                        "key": s.instrument_key,
                        "expiry": s.expiry,
                        "strike": s.strike_price,
                        "type": s.instrument_type,
                    }
                    for s in remaining[:10]
                ]
                result.completed_at = datetime.now(timezone.utc).isoformat()
                result.elapsed_seconds = round(time.time() - start_time, 2)
                return result

            # Phase 7.24.8C: Process with global rate limiter
            # The rate limiter handles both concurrency and pacing
            await self._run_options_rate_limited(result, remaining)

            result.status = "SUCCESS" if not result.errors else "PARTIAL"

        except UpstoxAuthenticationError as e:
            result.status = "FAILED"
            result.errors.append(f"Authentication failed: {e.message}")
        except Exception as e:
            result.status = "FAILED"
            result.errors.append(str(e))

        _log_ingestion(
            self.db, self.run_id, "option_candles", result.status,
            api_calls=result.api_calls,
            rows_fetched=result.rows_fetched,
            rows_inserted=result.rows_inserted,
            rows_skipped=result.rows_skipped,
            error_message=result.errors[0] if result.errors else None,
        )
        self.db.commit()
        result.completed_at = datetime.now(timezone.utc).isoformat()
        result.elapsed_seconds = round(time.time() - start_time, 2)
        return result

    # ------------------------------------------------------------------
    # Phase 7.24.8B: Universe filtering
    # ------------------------------------------------------------------

    def _filter_by_universe(
        self,
        specs: list[ContractSpec],
        universe: str,
    ) -> list[ContractSpec]:
        """Filter contract specs to ATM ± offset.

        Supported universes: ATM_5, ATM_10, ATM_20, ATM_30.
        Uses local NIFTY candles to compute historical ATM — no API calls.
        """
        offset_map = {
            "ATM_5": 5,
            "ATM_10": 10,
            "ATM_20": 20,
            "ATM_30": 30,
        }
        offset = offset_map.get(universe)
        if offset is None:
            logger.warning("Unknown universe '%s', returning all instruments", universe)
            return specs

        # Group specs by expiry
        expiry_specs: dict[str, list[ContractSpec]] = {}
        for s in specs:
            expiry_specs.setdefault(s.expiry, []).append(s)

        selected: list[ContractSpec] = []
        for exp, exp_specs in expiry_specs.items():
            atm = self._calculate_historical_atm(exp)
            if atm is None:
                # Cannot determine ATM — skip this expiry
                logger.warning("Cannot determine ATM for expiry %s, skipping", exp)
                continue

            # Get sorted unique strikes for this expiry
            strikes = sorted(set(s.strike_price for s in exp_specs))
            if not strikes:
                continue

            # Find ATM index
            atm_index = min(range(len(strikes)), key=lambda i: abs(strikes[i] - atm))

            # Calculate range
            low_idx = max(0, atm_index - offset)
            high_idx = min(len(strikes) - 1, atm_index + offset)
            range_strikes = set(strikes[low_idx:high_idx + 1])

            # Select instruments in range
            for s in exp_specs:
                if s.strike_price in range_strikes:
                    selected.append(s)

        logger.info(
            "Universe %s: selected %d instruments from %d total",
            universe, len(selected), len(specs),
        )
        return selected

    def _calculate_historical_atm(self, expiry: str) -> float | None:
        """Calculate historical ATM strike for a given expiry using local NIFTY candles.

        No API calls — uses only local database data.

        The ATM reference price is the NIFTY opening price ON the expiry date
        itself (the 09:15 opening candle), not the previous trading day's close.
        """
        try:
            exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

        from datetime import timedelta
        day_start = datetime.combine(exp_date, datetime.min.time())
        day_end = datetime.combine(exp_date + timedelta(days=1), datetime.min.time())

        # Find the first NIFTY candle on the expiry date (opening candle at 09:15)
        nifty_row = self.db.execute(
            select(NiftyCandle.open_time, NiftyCandle.open)
            .where(NiftyCandle.symbol == NIFTY_SYMBOL)
            .where(NiftyCandle.interval == DEFAULT_INTERVAL_STR)
            .where(NiftyCandle.open_time >= day_start)
            .where(NiftyCandle.open_time < day_end)
            .order_by(NiftyCandle.open_time.asc())
            .limit(1)
        ).first()

        if not nifty_row:
            return None

        ref_price = nifty_row[1]

        # Find nearest strike in contract_specs for this expiry
        specs = self.db.execute(
            select(ContractSpec.strike_price)
            .where(ContractSpec.expiry == expiry)
            .where(ContractSpec.underlying == NIFTY_SYMBOL)
        ).scalars().all()

        if not specs:
            return None

        strikes = sorted(set(specs))
        return min(strikes, key=lambda s: abs(s - ref_price))

    # ------------------------------------------------------------------
    # Phase 7.24.8C: Rate-limited concurrent processing
    # ------------------------------------------------------------------

    async def _run_options_rate_limited(
        self,
        result: BackfillResult,
        remaining: list[ContractSpec],
    ) -> None:
        """Process instruments with the global rate limiter.

        Architecture:
          - The ``GlobalRateLimiter`` gates both concurrency (semaphore)
            and request rate (pacing timer).
          - Each worker calls ``acquire()`` before the API request and
            ``release()`` after.
          - On 429 the limiter enters global cooldown; on success it
            gradually widens the window.
          - Each instrument still has an independent DB transaction.
        """
        lock = asyncio.Lock()  # Protects shared result counters
        progress = [0]
        total = len(remaining)
        limiter = self._rate_limiter

        async def process_one(spec: ContractSpec) -> None:
            ik = spec.instrument_key
            # Phase 7.24.8C: Wait for rate limiter permission
            await limiter.acquire()
            try:
                # Log progress
                progress[0] += 1
                if progress[0] % 10 == 0 or progress[0] == total:
                    limiter.log_status()
                    logger.info(
                        "[%d/%d] Progress (%.0f%%)",
                        progress[0], total,
                        progress[0] / total * 100,
                    )

                # Set checkpoint
                _upsert_checkpoint(
                    self.db, PIPELINE_OPTIONS, ik,
                    status="RUNNING", run_id=self.run_id,
                )
                self.db.commit()

                # Fetch candles
                candles = await self.client.get_expired_historical_candles(
                    ik, DEFAULT_INTERVAL_API, spec.expiry, spec.expiry,
                )

                # Signal success to the rate limiter
                await limiter.on_success()

                async with lock:
                    result.api_calls += 1
                    self._api_calls += 1
                    result.rows_fetched += len(candles)

                # Normalize and persist
                if candles:
                    from app.services.option_candles import (
                        normalize_option_candles,
                        record_option_candles,
                    )
                    from app.services.candle_validation import validate_candle

                    normalized = normalize_option_candles(
                        candles, instrument_key=ik,
                    )
                    valid = [c for c in normalized if validate_candle(c, 0).is_valid]
                    inserted = record_option_candles(self.db, valid)

                    async with lock:
                        result.rows_inserted += inserted

                    _upsert_checkpoint(
                        self.db, PIPELINE_OPTIONS, ik,
                        status="COMPLETED", run_id=self.run_id,
                        items_processed=inserted,
                        items_total=len(candles),
                    )
                else:
                    _upsert_checkpoint(
                        self.db, PIPELINE_OPTIONS, ik,
                        status="COMPLETED", run_id=self.run_id,
                        items_processed=0,
                        items_total=0,
                    )

                self.db.commit()
                await limiter.mark_instrument_done()

            except UpstoxAuthenticationError:
                # 401 is never retried; instrument stays FAILED
                _upsert_checkpoint(
                    self.db, PIPELINE_OPTIONS, ik,
                    status="FAILED", run_id=self.run_id,
                    error_message="Authentication expired",
                )
                self.db.commit()
                raise

            except UpstoxRateLimitError as e:
                # Phase 7.24.8C: 429 goes through the global rate limiter,
                # NOT the instrument failure path.  The instrument remains
                # PENDING for retry on the next run.
                await limiter.on_429(retry_after=e.retry_after)

                async with lock:
                    result.errors.append(f"{ik}: 429 rate limit")

                # Mark as PENDING (not FAILED) so checkpoint/resume retries it
                _upsert_checkpoint(
                    self.db, PIPELINE_OPTIONS, ik,
                    status="PENDING", run_id=self.run_id,
                    error_message=f"Rate limited (will retry): {e.message}",
                )
                self.db.commit()
                logger.warning("Instrument %s rate-limited (will retry)", ik)

            except Exception as e:
                await limiter.on_error()

                async with lock:
                    result.errors.append(f"{ik}: {e}")

                _upsert_checkpoint(
                    self.db, PIPELINE_OPTIONS, ik,
                    status="FAILED", run_id=self.run_id,
                    error_message=str(e)[:500],
                )
                self.db.commit()
                logger.warning("Instrument %s failed: %s", ik, e)

            finally:
                # Always release the semaphore slot
                limiter.release()

        # Launch all tasks — rate limiter gates concurrency and pacing
        tasks = [process_one(spec) for spec in remaining]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        # Phase 7.24.8C: Re-raise401 so run_options top-level handler catches it
        for exc in gathered:
            if isinstance(exc, UpstoxAuthenticationError):
                raise exc

    # ------------------------------------------------------------------
    # Backward-compat aliases
    # ------------------------------------------------------------------

    async def _run_options_sequential(
        self,
        result: BackfillResult,
        remaining: list[ContractSpec],
    ) -> None:
        """Sequential path — delegates to rate-limited path."""
        await self._run_options_rate_limited(result, remaining)

    async def _run_options_concurrent(
        self,
        result: BackfillResult,
        remaining: list[ContractSpec],
        concurrency: int,
    ) -> None:
        """Legacy concurrent path — delegates to rate-limited path."""
        await self._run_options_rate_limited(result, remaining)

    async def _maybe_reduce_concurrency(self) -> None:
        """Legacy adapter — delegates to the global rate limiter."""
        pass


# ---------------------------------------------------------------------------
# Date chunk generation
# ---------------------------------------------------------------------------

def _generate_date_chunks(
    start: date, end: date, max_days: int = CANDLE_CHUNK_DAYS,
) -> list[tuple[date, date]]:
    """Generate contiguous (from, to) date pairs, each at most max_days apart."""
    if start > end:
        return []
    chunks: list[tuple[date, date]] = []
    current = start
    from datetime import timedelta
    while current <= end:
        chunk_end = min(current + timedelta(days=max_days - 1), end)
        chunks.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return chunks


def _chunk_has_data(
    db: Session, symbol: str, start: date, end: date,
) -> bool:
    """Check if NIFTY candles already exist for a date range."""
    from datetime import timedelta
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time())
    count = db.scalar(
        select(func.count(NiftyCandle.id)).where(
            NiftyCandle.symbol == symbol,
            NiftyCandle.open_time >= start_dt,
            NiftyCandle.open_time < end_dt,
        )
    ) or 0
    return count > 0
