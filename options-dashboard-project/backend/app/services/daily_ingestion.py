"""Daily incremental ingestion pipeline — Phase 7.24.6.

Fetches only the data that is missing after market close.  Designed to
run once per trading day via cron, task scheduler, or manual invocation.

Key rules:
  - **CLI-only**: never runs on server startup or code reload.
  - **Incremental**: only fetches data not already in the database.
  - **Idempotent**: running twice fetches zero duplicates.
  - **Selective**: only processes active/recent expiries.
  - **Safe**: checks token validity before any API call.
  - **One day at a time**: processes one trading day per invocation.

Architecture::

    run_daily.py  (or cron)
          │
          ▼
    DailyIngestionPipeline
          │
    ┌─────┴─────┐
    ▼           ▼
 UpstoxClient  Local Database
    │           │
    ▼           ▼
 Upstox API  nifty_candles (incremental)
             contract_specs (incremental)
             option_candles (incremental)
             ingestion_log
             ingestion_checkpoint
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ContractSpec,
    HistoricalGexSnapshot,
    IngestionCheckpoint,
    IngestionLog,
    NiftyCandle,
    OptionCandle,
)
from app.services.upstox_client import (
    UpstoxClient,
    UpstoxAuthenticationError,
    UpstoxClientError,
)
from app.utils.market_time import IST

logger = logging.getLogger(__name__)


def _alert_job_failure(db: Session, run_id: str, reason: str) -> None:
    """Day 46 (F13): the REAL ingestion failure boundary emits the
    platform-scoped operational alert. Observational only — never alters
    the pipeline result, and recording failures are swallowed."""
    try:
        from app.services.operations import record_job_failure

        record_job_failure(db, job="daily_ingestion", reason=reason[:200])
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"
NIFTY_SYMBOL = "NIFTY"
DEFAULT_INTERVAL_API = "3minute"
DEFAULT_INTERVAL_DB = "3min"

# How many recent expiries to refresh contract metadata for
ACTIVE_EXPIRY_COUNT = 3

# Rate-limit delay between requests
REQUEST_DELAY = 0.2

# Pipeline name
PIPELINE_DAILY_NIFTY = "daily_nifty"
PIPELINE_DAILY_OPTIONS = "daily_options"


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class DailyIngestionResult:
    """Result of a daily ingestion run."""
    run_id: str = ""
    status: str = "PENDING"
    started_at: str = ""
    completed_at: str = ""
    api_calls: int = 0
    nifty_candles_inserted: int = 0
    contracts_refreshed: int = 0
    option_candles_inserted: int = 0
    option_instruments_processed: int = 0
    # Phase 10A: Greek and GEX metrics
    greek_records_calculated: int = 0
    greek_records_skipped: int = 0
    greek_instruments_processed: int = 0
    gex_records_calculated: int = 0
    gex_records_skipped: int = 0
    gex_instruments_processed: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Trading day helpers
# ---------------------------------------------------------------------------

def _get_ist_now() -> datetime:
    """Return current time as naive IST datetime."""
    return datetime.now(IST).replace(tzinfo=None)


def _get_ist_date() -> date:
    """Return today's date in IST."""
    return _get_ist_now().date()


def _is_weekday(d: date) -> bool:
    """Check if date is a weekday (Mon-Fri)."""
    return d.weekday() < 5


def _get_previous_trading_day(from_date: date | None = None) -> date:
    """Get the most recent trading day before today.

    Skips weekends. Does NOT check Indian market holidays
    (that would require a holiday calendar).
    """
    if from_date is None:
        from_date = _get_ist_date()
    d = from_date - timedelta(days=1)
    while not _is_weekday(d):
        d -= timedelta(days=1)
    return d


def _is_after_market_close() -> bool:
    """Check if current IST time is after market close (15:30 IST).

    Returns True if it's after 16:00 IST to give a safety margin
    for Upstox data availability.
    """
    now = _get_ist_now()
    return now.hour >= 16


# ---------------------------------------------------------------------------
# NIFTY candle ingestion
# ---------------------------------------------------------------------------

async def _ingest_nifty_day(
    db: Session,
    client: UpstoxClient,
    target_date: date,
    run_id: str,
) -> tuple[int, list[str]]:
    """Fetch and persist NIFTY candles for a single trading day.

    Returns (rows_inserted, errors).
    """
    errors: list[str] = []
    inserted = 0

    date_str = target_date.isoformat()

    # Check if we already have NIFTY candles for this day
    day_start = datetime.combine(target_date, datetime.min.time())
    day_end = datetime.combine(target_date + timedelta(days=1), datetime.min.time())
    existing_count = db.scalar(
        select(func.count(NiftyCandle.id)).where(
            NiftyCandle.symbol == NIFTY_SYMBOL,
            NiftyCandle.open_time >= day_start,
            NiftyCandle.open_time < day_end,
        )
    ) or 0

    if existing_count > 0:
        logger.info("NIFTY candles for %s already exist (%d candles), skipping", date_str, existing_count)
        return 0, []

    try:
        # Fetch NIFTY candles for this day
        candles = await client.get_historical_candles(
            NIFTY_INDEX_KEY, date_str, date_str,
        )
        if not candles:
            logger.info("No NIFTY candles returned for %s", date_str)
            return 0, []

        from app.services.candle_ingestion import normalize_candles
        from app.services.nifty_candles import record_candles
        from app.services.candle_validation import validate_candle

        normalized = normalize_candles(candles, symbol=NIFTY_SYMBOL)
        valid = [c for c in normalized if validate_candle(c, 0).is_valid]
        inserted = record_candles(db, valid)

        logger.info("NIFTY %s: %d candles fetched, %d inserted", date_str, len(candles), inserted)
        return inserted, []

    except UpstoxAuthenticationError as e:
        errors.append(f"NIFTY auth failed: {e.message}")
        raise
    except UpstoxClientError as e:
        errors.append(f"NIFTY API error for {date_str}: {e.message}")
    except Exception as e:
        errors.append(f"NIFTY error for {date_str}: {e}")

    return inserted, errors


# ---------------------------------------------------------------------------
# Contract metadata refresh
# ---------------------------------------------------------------------------

async def _refresh_contracts(
    db: Session,
    client: UpstoxClient,
    run_id: str,
) -> tuple[int, list[str]]:
    """Refresh contract metadata for recent/expiring expiries.

    Returns (contracts_upserted, errors).
    """
    errors: list[str] = []
    inserted = 0

    try:
        # Get available expiries from Upstox
        raw_expiries = await client.get_expiries(NIFTY_INDEX_KEY)
        if not raw_expiries:
            logger.info("No expiries returned from Upstox")
            return 0, []

        expiries = sorted(raw_expiries)
        today_str = _get_ist_date().isoformat()

        # Filter to recent expiries (within last 6 months to ~3 months ahead)
        # We focus on expiries that are likely to have active data
        recent_expiries = [
            e for e in expiries
            if e >= (datetime.now(timezone.utc).date() - timedelta(days=180)).isoformat()
        ]

        # Take only the ACTIVE_EXPIRY_COUNT most recent
        # (including the ones that just expired, which might have new data)
        active = recent_expiries[-ACTIVE_EXPIRY_COUNT:] if len(recent_expiries) > ACTIVE_EXPIRY_COUNT else recent_expiries

        from app.services.contract_metadata import upsert_contract_specs, SOURCE_UPSTOX_EXPIRED

        for exp_date in active:
            try:
                raw_contracts = await client.get_contracts(NIFTY_INDEX_KEY, exp_date)
                if not raw_contracts:
                    continue

                source_ref = f"DAILY_INGESTION/{NIFTY_SYMBOL}/{exp_date}"
                results = upsert_contract_specs(
                    db, raw_contracts,
                    source=SOURCE_UPSTOX_EXPIRED,
                    source_reference=source_ref,
                )
                new_count = sum(1 for r in results if r.action == "inserted")
                inserted += new_count
                time.sleep(REQUEST_DELAY)

            except UpstoxAuthenticationError:
                raise
            except UpstoxClientError as e:
                errors.append(f"Contracts for {exp_date}: {e.message}")
            except Exception as e:
                errors.append(f"Contracts for {exp_date}: {e}")

    except UpstoxAuthenticationError as e:
        errors.append(f"Contract refresh auth failed: {e.message}")
        raise
    except Exception as e:
        errors.append(f"Contract refresh error: {e}")

    return inserted, errors


# ---------------------------------------------------------------------------
# Option candle ingestion (incremental)
# ---------------------------------------------------------------------------

async def _ingest_option_candles(
    db: Session,
    client: UpstoxClient,
    target_date: date,
    run_id: str,
) -> tuple[int, int, list[str]]:
    """Fetch option candles for instruments that are missing today's data.

    Only processes instruments that exist in contract_specs and are
    missing candle data for the target date.

    Returns (instruments_processed, candles_inserted, errors).
    """
    errors: list[str] = []
    inserted = 0
    processed = 0

    # Get instruments that already have candles for this date
    day_start = datetime.combine(target_date, datetime.min.time())
    day_end = datetime.combine(target_date + timedelta(days=1), datetime.min.time())

    instruments_with_data = set(
        db.execute(
            select(OptionCandle.instrument_key)
            .where(
                OptionCandle.open_time >= day_start,
                OptionCandle.open_time < day_end,
            )
            .distinct()
        ).scalars().all()
    )

    # Get active contract specs (recent expiries)
    today_str = target_date.isoformat()
    recent_cutoff = (target_date - timedelta(days=180)).isoformat()

    specs = db.execute(
        select(ContractSpec)
        .where(ContractSpec.underlying == NIFTY_SYMBOL)
        .where(ContractSpec.expiry >= recent_cutoff)
        .where(ContractSpec.expiry <= today_str)
        .order_by(ContractSpec.expiry.desc())
    ).scalars().all()

    # Only process instruments missing data
    remaining = [s for s in specs if s.instrument_key not in instruments_with_data]

    if not remaining:
        logger.info("All %d active instruments already have candles for %s", len(specs), target_date.isoformat())
        return 0, 0, []

    logger.info(
        "Option candles: %d instruments to process (%d already have data)",
        len(remaining), len(instruments_with_data),
    )

    for i, spec in enumerate(remaining, 1):
        ik = spec.instrument_key
        processed += 1

        # Use expiry as both from/to for expired contracts
        try:
            candles = await client.get_expired_historical_candles(
                ik, DEFAULT_INTERVAL_API, spec.expiry, spec.expiry,
            )

            if candles:
                from app.services.option_candles import (
                    normalize_option_candles,
                    record_option_candles,
                )
                from app.services.candle_validation import validate_candle

                normalized = normalize_option_candles(candles, instrument_key=ik)
                valid = [c for c in normalized if validate_candle(c, 0).is_valid]
                saved = record_option_candles(db, valid)
                inserted += saved

            time.sleep(REQUEST_DELAY)

        except UpstoxAuthenticationError:
            raise
        except UpstoxClientError as e:
            errors.append(f"Option {ik}: {e.message}")
        except Exception as e:
            errors.append(f"Option {ik}: {e}")

    return processed, inserted, errors


# ---------------------------------------------------------------------------
# Phase 10A — Stage 4: Historical Greeks
# ---------------------------------------------------------------------------


def _calculate_greeks(
    db: Session,
    run_id: str,
) -> tuple[int, int, int, list[str]]:
    """Calculate Greeks for option candles that are missing Greek records.

    Returns (instruments_processed, records_calculated, records_skipped, errors).
    """
    errors: list[str] = []
    try:
        from app.services.historical_greeks import HistoricalGreeksEngine
        engine = HistoricalGreeksEngine(db)
        result = engine.run_batch()

        instruments = result.get("instruments_processed", 0)
        calculated = result.get("total_persisted", 0)
        total = result.get("total_candles", 0)
        failed = result.get("total_failed", 0)
        skipped = max(0, total - calculated - failed)

        if failed > 0:
            errors.append(f"Greeks: {failed} candles failed")

        logger.info(
            "Greeks: %d instruments, %d calculated, %d skipped, %d failed",
            instruments, calculated, skipped, failed,
        )
        return instruments, calculated, skipped, errors

    except Exception as e:
        errors.append(f"Greek calculation error: {e}")
        logger.error("Greek calculation failed: %s", e, exc_info=True)
        return 0, 0, 0, errors


# ---------------------------------------------------------------------------
# Phase 10A — Stage 5: Historical GEX
# ---------------------------------------------------------------------------


def _calculate_gex(
    db: Session,
    run_id: str,
) -> tuple[int, int, int, list[str]]:
    """Calculate GEX for Greek records that are missing GEX.

    Returns (instruments_processed, records_calculated, records_skipped, errors).
    """
    errors: list[str] = []
    try:
        from app.services.historical_gex import HistoricalGexService
        service = HistoricalGexService(db)
        result = service.run_batch()

        instruments = result.get("instruments_processed", 0)
        calculated = result.get("gex_calculated", 0)
        skipped = result.get("gex_skipped", 0)
        failed = result.get("failed_instruments", 0)

        if failed > 0:
            errors.append(f"GEX: {failed} instruments failed")

        logger.info(
            "GEX: %d instruments, %d calculated, %d skipped, %d failed",
            instruments, calculated, skipped, failed,
        )
        return instruments, calculated, skipped, errors

    except Exception as e:
        errors.append(f"GEX calculation error: {e}")
        logger.error("GEX calculation failed: %s", e, exc_info=True)
        return 0, 0, 0, errors


# ---------------------------------------------------------------------------
# Phase 10A — Stage 6: Validation
# ---------------------------------------------------------------------------


def _validate_pipeline(db: Session, target_date: date) -> dict:
    """Validate pipeline results and produce a coverage report."""
    from sqlalchemy import func

    day_start = datetime.combine(target_date, datetime.min.time())
    day_end = datetime.combine(target_date + timedelta(days=1), datetime.min.time())

    # Count candles for target date
    candles_today = db.scalar(
        select(func.count(OptionCandle.id)).where(
            OptionCandle.open_time >= day_start,
            OptionCandle.open_time < day_end,
        )
    ) or 0

    # Count total candles
    total_candles = db.scalar(select(func.count(OptionCandle.id))) or 0

    # NOTE: No Greeks here by policy — daily ingestion must not touch the
    # option_greeks table (Phase 7.24 "No Greeks" separation policy).

    # Count GEX
    total_gex = db.scalar(select(func.count(HistoricalGexSnapshot.id))) or 0

    # Count instruments
    instruments = db.scalar(
        select(func.count(func.distinct(OptionCandle.instrument_key)))
    ) or 0

    # Count expiries
    expiries = db.scalar(
        select(func.count(func.distinct(ContractSpec.expiry)))
    ) or 0

    gex_coverage = total_gex

    return {
        "target_date": target_date.isoformat(),
        "candles_today": candles_today,
        "total_candles": total_candles,
        "total_gex": total_gex,
        "instruments": instruments,
        "expiries": expiries,
        "gex_snapshots": gex_coverage,
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

class DailyIngestionPipeline:
    """Daily incremental ingestion pipeline.

    Fetches only missing data for the most recent trading day.

    Usage::

        pipeline = DailyIngestionPipeline(db, client)
        result = await pipeline.run()

    Or for a specific date::

        result = await pipeline.run(target_date=date(2026, 8, 22))
    """

    def __init__(
        self,
        db: Session,
        client: UpstoxClient,
        *,
        target_date: date | None = None,
        skip_nifty: bool = False,
        skip_contracts: bool = False,
        skip_options: bool = False,
        skip_greeks: bool = False,
        skip_gex: bool = False,
    ):
        self.db = db
        self.client = client
        self.target_date = target_date
        self.skip_nifty = skip_nifty
        self.skip_contracts = skip_contracts
        self.skip_options = skip_options
        self.skip_greeks = skip_greeks
        self.skip_gex = skip_gex
        self.run_id = f"daily_{uuid.uuid4().hex[:12]}"

    async def run(self) -> DailyIngestionResult:
        """Execute the daily ingestion pipeline."""
        result = DailyIngestionResult(
            run_id=self.run_id,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        start_time = time.time()

        # Determine target date
        if self.target_date is None:
            target = _get_previous_trading_day()
        else:
            target = self.target_date

        result.metadata["target_date"] = target.isoformat()
        result.metadata["is_weekday"] = _is_weekday(target)

        if not _is_weekday(target):
            result.status = "SKIPPED"
            result.metadata["reason"] = "Target date is not a weekday"
            result.completed_at = datetime.now(timezone.utc).isoformat()
            result.elapsed_seconds = round(time.time() - start_time, 2)
            return result

        # Check token validity before any API calls
        token = self.client._token_provider.get_token()
        if not token:
            result.status = "FAILED"
            result.errors.append("No valid access token. Please authenticate first.")
            _log_daily_ingestion(
                self.db, self.run_id, "FAILED",
                error_message="No valid access token",
            )
            _alert_job_failure(self.db, self.run_id, "No valid access token")
            self.db.commit()
            result.completed_at = datetime.now(timezone.utc).isoformat()
            result.elapsed_seconds = round(time.time() - start_time, 2)
            return result

        try:
            # Stage 1: NIFTY candles
            if not self.skip_nifty:
                logger.info("=== Stage 1: NIFTY candles for %s ===", target.isoformat())
                nifty_inserted, nifty_errors = await _ingest_nifty_day(
                    self.db, self.client, target, self.run_id,
                )
                result.nifty_candles_inserted = nifty_inserted
                result.errors.extend(nifty_errors)
                self.db.commit()

            # Stage 2: Contract metadata refresh
            if not self.skip_contracts:
                logger.info("=== Stage 2: Contract metadata refresh ===")
                contracts_inserted, contract_errors = await _refresh_contracts(
                    self.db, self.client, self.run_id,
                )
                result.contracts_refreshed = contracts_inserted
                result.errors.extend(contract_errors)
                self.db.commit()

            # Stage 3: Option candles
            if not self.skip_options:
                logger.info("=== Stage 3: Option candles for %s ===", target.isoformat())
                opts_processed, opts_inserted, opts_errors = await _ingest_option_candles(
                    self.db, self.client, target, self.run_id,
                )
                result.option_instruments_processed = opts_processed
                result.option_candles_inserted = opts_inserted
                result.errors.extend(opts_errors)
                self.db.commit()

            # Phase 10A — Stage 4: Historical Greeks
            if not self.skip_greeks:
                logger.info("=== Stage 4: Historical Greeks ===")
                greek_instruments, greek_calc, greek_skip, greek_errors = _calculate_greeks(
                    self.db, self.run_id,
                )
                result.greek_instruments_processed = greek_instruments
                result.greek_records_calculated = greek_calc
                result.greek_records_skipped = greek_skip
                result.errors.extend(greek_errors)
                self.db.commit()

            # Phase 10A — Stage 5: Historical GEX
            if not self.skip_gex:
                logger.info("=== Stage 5: Historical GEX ===")
                gex_instruments, gex_calc, gex_skip, gex_errors = _calculate_gex(
                    self.db, self.run_id,
                )
                result.gex_instruments_processed = gex_instruments
                result.gex_records_calculated = gex_calc
                result.gex_records_skipped = gex_skip
                result.errors.extend(gex_errors)
                self.db.commit()

            # Phase 10A — Stage 6: Validation
            logger.info("=== Stage 6: Validation ===")
            validation = _validate_pipeline(self.db, target)
            result.metadata["validation"] = validation

            result.status = "SUCCESS" if not result.errors else "PARTIAL"

        except UpstoxAuthenticationError as e:
            result.status = "FAILED"
            result.errors.append(f"Authentication failed: {e.message}")
            _log_daily_ingestion(
                self.db, self.run_id, "FAILED",
                error_category="AUTH_EXPIRED",
                error_message=f"Auth failed: {e.message}",
            )
            _alert_job_failure(self.db, self.run_id, f"Auth failed: {e.message}")
            self.db.commit()
        except Exception as e:
            result.status = "FAILED"
            result.errors.append(f"Unexpected error: {e}")
            _log_daily_ingestion(
                self.db, self.run_id, "FAILED",
                error_category="UNKNOWN",
                error_message=str(e)[:500],
            )
            _alert_job_failure(self.db, self.run_id, f"Unexpected error: {e}")
            self.db.commit()

        result.completed_at = datetime.now(timezone.utc).isoformat()
        result.elapsed_seconds = round(time.time() - start_time, 2)

        # Final log
        _log_daily_ingestion(
            self.db, self.run_id, result.status,
            nifty_inserted=result.nifty_candles_inserted,
            contracts_refreshed=result.contracts_refreshed,
            options_inserted=result.option_candles_inserted,
            options_instruments=result.option_instruments_processed,
            api_calls=result.api_calls,
            error_message=result.errors[0] if result.errors else None,
        )
        self.db.commit()

        return result


def _log_daily_ingestion(
    db: Session,
    run_id: str,
    status: str,
    *,
    error_category: str | None = None,
    error_message: str | None = None,
    nifty_inserted: int = 0,
    contracts_refreshed: int = 0,
    options_inserted: int = 0,
    options_instruments: int = 0,
    api_calls: int = 0,
) -> None:
    """Write an ingestion log entry for daily ingestion."""
    now_str = datetime.now(timezone.utc).isoformat()
    metadata = {
        "nifty_inserted": nifty_inserted,
        "contracts_refreshed": contracts_refreshed,
        "options_inserted": options_inserted,
        "options_instruments": options_instruments,
    }
    import json
    log = IngestionLog(
        run_id=run_id,
        operation="daily_ingestion",
        started_at=now_str,
        completed_at=now_str if status in ("SUCCESS", "FAILED", "PARTIAL", "SKIPPED") else None,
        status=status,
        api_calls=api_calls,
        rows_inserted=nifty_inserted + options_inserted,
        error_category=error_category,
        error_message=error_message,
        metadata_json=json.dumps(metadata),
    )
    db.add(log)
    db.flush()
