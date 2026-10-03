"""Phase 7.9 - Real Upstox API Live Verification Tool.

Self-contained CLI tool that verifies our Phase 7.8 implementation against
the REAL production Upstox API.  Must be run AFTER the user has authenticated
through the existing project auth mechanism (visit /auth/login while the
backend server is running).

This tool:
  - Reuses the existing token_store and Upstox adapter functions
  - Never prints, logs, or stores access tokens or credentials
  - Performs deliberately small, controlled API calls
  - Probes a live option instrument key on request (--option-key, read-only)
  - Generates a comprehensive verification report

Usage::

    # Start the backend server first, then authenticate via /auth/login
    python -m app.tools.live_verification --all

    # Or run individual sections
    python -m app.tools.live_verification --candles
    python -m app.tools.live_verification --contracts
    python -m app.tools.live_verification --lot-sizes
    python -m app.tools.live_verification --round-trip
    python -m app.tools.live_verification --backfill

    python -m app.tools.live_verification --coverage

    # Probe a single unexpired option instrument key (intraday + bounded historical)
    python -m app.tools.live_verification --option-key "NSE_FO|<token>|<expiry dd-mm-yyyy>"

    # Dry-run (check auth only, don't call API)
    python -m app.tools.live_verification --dry-run

    # Show help
    python -m app.tools.live_verification --help

Security:
  - Access tokens are NEVER printed, logged, or stored in the report
  - API secrets are NEVER accessed or logged
  - The report file contains no credential material
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, date, timedelta, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Token access - uses the SAME in-memory token store as the running server.
# This means the backend server MUST be running with an authenticated session.
# ---------------------------------------------------------------------------

from app.services.token_store import get_token, get_all_session_ids  # noqa: F401


def _get_access_token() -> str:
    """Retrieve the access token from the in-memory token store.

    This uses the same mechanism as the running FastAPI server.
    The server MUST be running with an active authenticated session.

    Raises SystemExit if no token is available.
    """
    # Phase 8F: token store is session-keyed. Find the most recent active session.
    sessions = get_all_session_ids()
    if not sessions:
        token = None
    else:
        token = get_token(sessions[-1])
    if not token:
        print("=" * 70)
        print("ERROR: No active Upstox session found.")
        print()
        print("The backend server must be running with an authenticated session.")
        print()
        print("Steps:")
        print("  1. Start the backend:  cd backend && python -m uvicorn app.main:app --reload")
        print("  2. Visit:              http://localhost:8000/auth/login")
        print("  3. Complete OAuth login with your Upstox account")
        print("  4. Run this tool again: python -m app.tools.live_verification --all")
        print()
        print("The server stores the token in memory. If the server was restarted,")
        print("you need to re-authenticate.")
        print("=" * 70)
        sys.exit(1)
    return token


# ---------------------------------------------------------------------------
# Import our Phase 7.8 modules
# ---------------------------------------------------------------------------

from app.services.upstox import (
    get_historical_candles,
    get_intraday_candles,
    get_expired_expiries,
    get_expired_option_contracts,
    UpstoxError,
)
from app.services.candle_ingestion import (
    extract_candles_from_response,
    normalize_candles,
    normalize_candle_timestamp,
)
from app.services.candle_validation import validate_candle_batch
from app.utils.market_time import IST, to_ist_naive
from app.services.contract_metadata import (
    upsert_contract_spec,
    get_contract_specification,
    count_contract_specs,
    SOURCE_UPSTOX_EXPIRED,
)
from app.models import NiftyCandle, ContractSpec
from app.db import Base
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"
# Use a recent, known completed trading day for candle verification.
# 2025-08-20 (Wednesday) was a regular NSE trading day.
# Adjust if needed - the tool will detect weekends/holidays automatically.
DEFAULT_CANDLE_DATE = "2025-08-20"
# Use a historical expiry known to exist for contract verification.
DEFAULT_EXPIRY_DATE = "2025-04-17"
# Small backfill: just 3 calendar days to test the pipeline.
BACKFILL_DAYS = 3

# Report path
REPORT_PATH = "docs/PHASE_7_9_LIVE_VERIFICATION.md"

# Sanitization: patterns that should never appear in output
_SENSITIVE_PATTERNS = [
    "api_key", "api_secret", "access_token", "refresh_token",
    "Bearer eyJ", "authorization",
]


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

def _sanitize(text: str) -> str:
    """Remove any accidental credential material from output."""
    import re
    # Remove Bearer tokens
    result = re.sub(r'Bearer\s+eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+', 'Bearer [REDACTED]', text)
    # Remove bare JWTs (eyJ header)
    result = re.sub(r'eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}', '[REDACTED_JWT]', result)
    # Remove session tokens (token_urlsafe patterns)
    result = re.sub(r'[\w\-]{40,}', '[REDACTED_TOKEN]', result)
    return result


# ---------------------------------------------------------------------------
# Section 1: Historical Candle API Verification
# ---------------------------------------------------------------------------

async def verify_candle_api(token: str, candle_date: str, dry_run: bool = False) -> dict:
    """Fetch a single day of real NIFTY 3-minute candles from Upstox V3.

    Returns a verification result dict.
    """
    result = {
        "section": "Historical Candle API Verification",
        "status": "pending",
        "endpoint": f"GET /v3/historical-candle/{NIFTY_INDEX_KEY}/minutes/3/{candle_date}/{candle_date}",
        "candle_date": candle_date,
    }

    print(f"\n{'='*70}")
    print(f"SECTION 1: Historical Candle API Verification")
    print(f"{'='*70}")
    print(f"  Endpoint: V3 Historical Candle API")
    print(f"  Instrument: {NIFTY_INDEX_KEY}")
    print(f"  Date: {candle_date} (3-minute candles)")
    print()

    if dry_run:
        print("  [DRY RUN] Would fetch candles for this date.")
        result["status"] = "dry_run"
        return result

    try:
        print("  Fetching...")
        start = time.time()
        response = await get_historical_candles(
            token,
            instrument_key=NIFTY_INDEX_KEY,
            to_date=candle_date,
            from_date=candle_date,
            unit="minutes",
            interval=3,
        )
        elapsed = time.time() - start
        print(f"  [OK] Response received in {elapsed:.2f}s")

        # Analyze response structure
        result["http_status"] = "success (200)"
        result["response_keys"] = list(response.keys()) if isinstance(response, dict) else str(type(response))
        result["response_status"] = response.get("status")
        result["elapsed_seconds"] = round(elapsed, 2)

        # Extract candle data
        candles_raw = extract_candles_from_response(response)
        result["raw_candle_count"] = len(candles_raw)

        if candles_raw:
            first_candle = candles_raw[0]
            last_candle = candles_raw[-1]
            result["candle_array_length"] = len(first_candle)
            result["first_candle_raw"] = _sanitize(str(first_candle))
            result["last_candle_raw"] = _sanitize(str(last_candle))
            result["first_timestamp"] = str(first_candle[0]) if first_candle else None
            result["last_timestamp"] = str(last_candle[0]) if last_candle else None

            # Verify field types
            if len(first_candle) >= 7:
                result["field_types"] = {
                    "timestamp": type(first_candle[0]).__name__,
                    "open": type(first_candle[1]).__name__,
                    "high": type(first_candle[2]).__name__,
                    "low": type(first_candle[3]).__name__,
                    "close": type(first_candle[4]).__name__,
                    "volume": type(first_candle[5]).__name__,
                    "open_interest": type(first_candle[6]).__name__,
                }

            # Check timestamp format
            if isinstance(first_candle[0], str):
                ts = first_candle[0]
                result["timestamp_format"] = ts
                result["has_plus_0530"] = "+05:30" in ts
                result["has_z_suffix"] = ts.endswith("Z")

            # OHLC sanity
            result["sample_prices"] = {
                "open": first_candle[1],
                "high": first_candle[2],
                "low": first_candle[3],
                "close": first_candle[4],
                "volume": first_candle[5],
                "open_interest": first_candle[6] if len(first_candle) > 6 else None,
            }

            # Check API native order and normalized order
            if len(candles_raw) >= 2:
                timestamps = [c[0] for c in candles_raw if c[0]]
                ascending = sorted(timestamps)
                descending = sorted(timestamps, reverse=True)
                if timestamps == ascending:
                    result["api_native_order"] = "ascending"
                elif timestamps == descending:
                    result["api_native_order"] = "descending"
                else:
                    result["api_native_order"] = "mixed"
                result["api_order_is_valid"] = result["api_native_order"] in ("ascending", "descending")
                result["unique_timestamps"] = len(set(timestamps))
                result["has_duplicates"] = len(timestamps) != len(set(timestamps))

            # Verify normalization works
            normalized = normalize_candles(candles_raw, symbol="NIFTY", interval="3min")
            result["normalized_count"] = len(normalized)
            result["normalization_success"] = len(normalized) > 0

            if normalized:
                first_norm = normalized[0]
                result["normalized_first_candle"] = {
                    "openTime": first_norm.get("openTime"),
                    "open": first_norm.get("open"),
                    "high": first_norm.get("high"),
                    "low": first_norm.get("low"),
                    "close": first_norm.get("close"),
                    "volume": first_norm.get("volume"),
                }
                result["has_z_in_normalized"] = "Z" in str(first_norm.get("openTime", ""))

            # Check normalized order (should be ascending after normalization)
            if len(normalized) >= 2:
                norm_times = [c["openTime"] for c in normalized if c.get("openTime")]
                result["normalized_order"] = "ascending" if norm_times == sorted(norm_times) else "descending"
                result["normalized_is_ascending"] = norm_times == sorted(norm_times)

            # Validate
            report = validate_candle_batch(normalized, expected_interval_minutes=3)

            # Categorize warnings by type
            warning_counts: dict[str, int] = {}
            warning_examples: list[dict] = []
            for wr in report.get("warnings", []):
                for w_msg in (wr.warnings if hasattr(wr, 'warnings') else []):
                    # Extract the category (everything before the first ':')
                    cat = w_msg.split(":")[0] if ":" in w_msg else w_msg
                    warning_counts[cat] = warning_counts.get(cat, 0) + 1
                    if len(warning_examples) < 5:
                        warning_examples.append({
                            "candle_index": getattr(wr, 'candle_index', '?'),
                            "warning": w_msg,
                        })

            result["validation"] = {
                "total": report["total"],
                "valid": report["valid"],
                "invalid": report["invalid"],
                "warning_total": len(report.get("warnings", [])),
                "warning_counts_by_type": warning_counts,
                "warning_examples": warning_examples,
            }

        result["status"] = "success"
        print(f"  [OK] Raw candles: {result.get('raw_candle_count', 0)}")
        print(f"  [OK] Normalized:  {result.get('normalized_count', 0)}")
        print(f"  [OK] Valid:       {result.get('validation', {}).get('valid', '?')}")
        print(f"  [OK] Invalid:     {result.get('validation', {}).get('invalid', '?')}")
        print(f"  [OK] API order:   {result.get('api_native_order', '?')}")
        print(f"  [OK] Normalized order: {result.get('normalized_order', '?')}")
        print(f"  [OK] Duplicates:  {result.get('has_duplicates', '?')}")
        print(f"  [OK] Warnings by type: {result.get('validation', {}).get('warning_counts_by_type', {})}")
        print(f"  [OK] Timestamp format: {result.get('timestamp_format', '?')}")
        print(f"  [OK] OHLC fields: {result.get('field_types', {})}")

    except UpstoxError as e:
        result["status"] = "error"
        result["error"] = str(e)
        result["http_status"] = f"UpstoxError({e.status_code})"
        print(f"  [FAIL] UpstoxError({e.status_code}): {e.message}")
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        print(f"  [FAIL] Unexpected error: {e}")

    return result


# ---------------------------------------------------------------------------
# Section 2: Expired Contract API Verification
# ---------------------------------------------------------------------------

async def verify_contract_api(token: str, expiry_date: str, dry_run: bool = False) -> dict:
    """Fetch real expired option contract metadata from Upstox V2.

    Returns a verification result dict with actual API field inspection.
    """
    result = {
        "section": "Expired Contract API Verification",
        "status": "pending",
        "expiry_date": expiry_date,
    }

    print(f"\n{'='*70}")
    print(f"SECTION 2: Expired Contract API Verification")
    print(f"{'='*70}")
    print(f"  Endpoint: V2 Expired Option Contracts API")
    print(f"  Instrument: {NIFTY_INDEX_KEY}")
    print(f"  Expiry: {expiry_date}")
    print()

    if dry_run:
        print("  [DRY RUN] Would fetch expired contracts.")
        result["status"] = "dry_run"
        return result

    # First, get available expiries
    try:
        print("  Step 1: Fetching expired expiries...")
        expiries_resp = await get_expired_expiries(token, instrument_key=NIFTY_INDEX_KEY)

        result["expiries_response_keys"] = list(expiries_resp.keys()) if isinstance(expiries_resp, dict) else str(type(expiries_resp))
        expiries_data = expiries_resp.get("data", [])
        result["available_expiries_count"] = len(expiries_data) if isinstance(expiries_data, list) else 0
        result["available_expiries_sample"] = (expiries_data[:5] if isinstance(expiries_data, list) else [])[:5]

        print(f"  [OK] Found {result['available_expiries_count']} expired expiry dates")
        if expiries_data:
            print(f"    Sample: {result['available_expiries_sample']}")

        # Pick the expiry to use - prefer the requested one, fall back to the most recent available
        target_expiry = expiry_date
        if isinstance(expiries_data, list) and expiry_date not in expiries_data:
            if expiries_data:
                target_expiry = expiries_data[-1]  # most recent
                result["used_expiry"] = target_expiry
                result["requested_expiry_not_found"] = True
                print(f"  [INFO] Requested expiry {expiry_date} not in available list.")
                print(f"    Using most recent: {target_expiry}")
            else:
                result["status"] = "no_expiries"
                print("  [FAIL] No expired expiries available")
                return result
        else:
            result["used_expiry"] = target_expiry

    except UpstoxError as e:
        result["status"] = "error"
        result["error"] = f"Expiry fetch failed: {e}"
        result["http_status"] = f"UpstoxError({e.status_code})"
        print(f"  [FAIL] UpstoxError({e.status_code}): {e.message}")
        if e.status_code in (401, 403):
            print(f"  [INFO] This may require Upstox Plus plan subscription.")
        return result

    # Fetch contracts for the target expiry
    try:
        print(f"\n  Step 2: Fetching contracts for expiry {target_expiry}...")
        start = time.time()
        contracts_resp = await get_expired_option_contracts(
            token,
            instrument_key=NIFTY_INDEX_KEY,
            expiry_date=target_expiry,
        )
        elapsed = time.time() - start

        result["contracts_response_keys"] = list(contracts_resp.keys()) if isinstance(contracts_resp, dict) else str(type(contracts_resp))
        result["contracts_elapsed_seconds"] = round(elapsed, 2)

        contracts_data = contracts_resp.get("data", [])
        if not isinstance(contracts_data, list):
            contracts_data = []

        result["contract_count"] = len(contracts_data)
        print(f"  [OK] Received {len(contracts_data)} contracts in {elapsed:.2f}s")

        if contracts_data:
            # Inspect first contract structure
            sample = contracts_data[0]
            result["sample_contract_keys"] = sorted(sample.keys()) if isinstance(sample, dict) else []

            # Field-by-field verification
            expected_fields = {
                "instrument_key": "str",
                "trading_symbol": "str",
                "expiry": "str",
                "strike_price": "number",
                "instrument_type": "str",
                "lot_size": "int",
                "minimum_lot": "int",
                "freeze_quantity": "number",
                "tick_size": "number",
                "underlying_key": "str",
                "underlying_symbol": "str",
                "segment": "str",
                "exchange": "str",
                "weekly": "bool",
            }

            field_verification = {}
            for field, expected_type in expected_fields.items():
                if field in sample:
                    actual_value = sample[field]
                    actual_type = type(actual_value).__name__
                    field_verification[field] = {
                        "present": True,
                        "actual_type": actual_type,
                        "example_value": actual_value,
                        "matches_expected_type": _type_matches(actual_type, expected_type),
                    }
                else:
                    field_verification[field] = {
                        "present": False,
                    }
                    print(f"  [WARN] Field '{field}' NOT present in API response")

            result["field_verification"] = field_verification

            # Verify lot_sizes across contracts
            lot_sizes = {}
            for contract in contracts_data:
                ik = contract.get("instrument_key", "unknown")
                ls = contract.get("lot_size")
                ml = contract.get("minimum_lot")
                lot_sizes[ik] = {"lot_size": ls, "minimum_lot": ml}

            unique_lot_sizes = set(v["lot_size"] for v in lot_sizes.values() if v["lot_size"] is not None)
            result["unique_lot_sizes"] = sorted(unique_lot_sizes)
            result["lot_size_varies"] = len(unique_lot_sizes) > 1

            print(f"  [OK] Unique lot sizes found: {sorted(unique_lot_sizes)}")
            print(f"  [OK] Lot sizes vary across contracts: {result['lot_size_varies']}")

            # Provide context on lot-size availability
            if not result["lot_size_varies"]:
                result["lot_size_note"] = (
                    f"Only lot_size={sorted(unique_lot_sizes)} found for expiry {target_expiry}. "
                    f"The Upstox Expired Option Contracts API covers ~6 months of historical expiries. "
                    f"If all available expiries post-date the most recent NIFTY lot-size change, "
                    f"only the current lot size will be returned. This is expected behavior."
                )
                print(f"  [INFO] {result['lot_size_note']}")

            # CE/PE breakdown
            ce_count = sum(1 for c in contracts_data if c.get("instrument_type") == "CE")
            pe_count = sum(1 for c in contracts_data if c.get("instrument_type") == "PE")
            result["ce_count"] = ce_count
            result["pe_count"] = pe_count
            print(f"  [OK] CE: {ce_count}, PE: {pe_count}")

            # Sample contracts for lot-size table
            result["sample_contracts"] = []
            for c in contracts_data[:5]:
                result["sample_contracts"].append({
                    "instrument_key": c.get("instrument_key"),
                    "trading_symbol": c.get("trading_symbol"),
                    "strike": c.get("strike_price"),
                    "type": c.get("instrument_type"),
                    "lot_size": c.get("lot_size"),
                    "minimum_lot": c.get("minimum_lot"),
                })

        result["status"] = "success"

    except UpstoxError as e:
        result["status"] = "error"
        result["error"] = f"Contract fetch failed: {e}"
        print(f"  [FAIL] UpstoxError({e.status_code}): {e.message}")
        if e.status_code in (401, 403):
            print(f"  [INFO] This endpoint requires Upstox Plus plan subscription.")

    return result


# ---------------------------------------------------------------------------
# Section 3: Database Round-Trip
# ---------------------------------------------------------------------------

async def verify_db_roundtrip(token: str, candle_date: str, dry_run: bool = False) -> dict:
    """Verify complete pipeline: API -> normalize -> validate -> persist -> read back."""
    result = {
        "section": "Database Round-Trip Verification",
        "status": "pending",
    }

    print(f"\n{'='*70}")
    print(f"SECTION 3: Database Round-Trip Verification")
    print(f"{'='*70}")

    if dry_run:
        print("  [DRY RUN] Would verify database round-trip.")
        result["status"] = "dry_run"
        return result

    # Use a dedicated test database to avoid contaminating the main DB
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    db = TestSession()

    try:
        # Step 1: Fetch real candles
        print("  Step 1: Fetching real candles...")
        response = await get_historical_candles(
            token,
            instrument_key=NIFTY_INDEX_KEY,
            to_date=candle_date,
            from_date=candle_date,
        )
        raw_candles = extract_candles_from_response(response)
        print(f"  [OK] {len(raw_candles)} raw candles fetched")

        # Step 2: Normalize
        print("  Step 2: Normalizing...")
        normalized = normalize_candles(raw_candles, symbol="NIFTY", interval="3min")
        print(f"  [OK] {len(normalized)} candles normalized")

        # Step 3: Validate
        print("  Step 3: Validating...")
        report = validate_candle_batch(normalized, expected_interval_minutes=3)
        print(f"  [OK] Valid: {report['valid']}, Invalid: {report['invalid']}")

        # Step 4: Persist valid candles
        print("  Step 4: Persisting to database...")
        from app.services.nifty_candles import record_candles
        saved = record_candles(db, normalized)
        print(f"  [OK] {saved} candles persisted")

        # Step 5: Read back
        print("  Step 5: Reading back from database...")
        from app.services.nifty_candles import get_candles, count_candles
        db_count = count_candles(db, symbol="NIFTY", interval="3min")
        db_candles = get_candles(db, symbol="NIFTY", interval="3min", limit=10000)
        print(f"  [OK] {db_count} candles in database")

        # Step 6: Compare
        if db_candles:
            first_db = db_candles[0]
            last_db = db_candles[-1]
            result["db_candle_count"] = db_count
            result["first_db_candle"] = first_db
            result["last_db_candle"] = last_db
            result["openTime_has_z"] = "Z" in str(first_db.get("openTime", ""))

            # Verify Z suffix
            print(f"  [OK] First candle openTime: {first_db.get('openTime')}")
            print(f"  [OK] Last candle openTime:  {last_db.get('openTime')}")
            print(f"  [OK] Z suffix present: {result['openTime_has_z']}")

            # Verify OHLC fields preserved
            result["fields_preserved"] = all(
                k in first_db for k in ["open", "high", "low", "close", "volume", "openTime"]
            )
            print(f"  [OK] All OHLCV fields preserved: {result['fields_preserved']}")

        # Step 7: Contract metadata round-trip (using test data since real API may not be available)
        print("\n  Step 7: Contract metadata round-trip (synthetic)...")
        test_contracts = [
            {
                "instrument_key": "NSE_FO|TEST_A|2025-04-17",
                "underlying_symbol": "NIFTY",
                "underlying_key": "NSE_INDEX|Nifty 50",
                "expiry": "2025-04-17",
                "strike_price": 20400.0,
                "instrument_type": "PE",
                "lot_size": 75,
                "minimum_lot": 75,
                "freeze_quantity": 1800,
                "tick_size": 5.0,
                "trading_symbol": "NIFTY 20400 PE 17 APR 25",
                "segment": "INDICES",
                "exchange": "NSE_FO",
                "weekly": False,
            },
            {
                "instrument_key": "NSE_FO|TEST_B|2025-03-27",
                "underlying_symbol": "NIFTY",
                "underlying_key": "NSE_INDEX|Nifty 50",
                "expiry": "2025-03-27",
                "strike_price": 23000.0,
                "instrument_type": "PE",
                "lot_size": 50,
                "minimum_lot": 50,
                "freeze_quantity": 1250,
                "tick_size": 5.0,
                "trading_symbol": "NIFTY 23000 PE 27 MAR 25",
                "segment": "INDICES",
                "exchange": "NSE_FO",
                "weekly": False,
            },
            {
                "instrument_key": "NSE_FO|TEST_C|2025-07-10",
                "underlying_symbol": "NIFTY",
                "underlying_key": "NSE_INDEX|Nifty 50",
                "expiry": "2025-07-10",
                "strike_price": 25000.0,
                "instrument_type": "CE",
                "lot_size": 25,
                "minimum_lot": 25,
                "freeze_quantity": 625,
                "tick_size": 5.0,
                "trading_symbol": "NIFTY 25000 CE 10 JUL 25",
                "segment": "INDICES",
                "exchange": "NSE_FO",
                "weekly": False,
            },
        ]

        for contract in test_contracts:
            r = upsert_contract_spec(db, contract, source="VERIFICATION_TEST")
            print(f"  [OK] {contract['instrument_key']}: lot_size={contract['lot_size']} -> {r.action}")

        # Read back and verify
        for contract in test_contracts:
            spec = get_contract_specification(db, contract["instrument_key"])
            assert spec is not None
            assert spec["lot_size"] == contract["lot_size"]
            assert spec["minimum_lot"] == contract["minimum_lot"]
            print(f"  [OK] Read-back: {contract['instrument_key']} -> lot_size={spec['lot_size']}")

        # Verify immutability - try to overwrite lot_size
        overwrite_contract = test_contracts[0].copy()
        overwrite_contract["lot_size"] = 999  # different from stored 75
        r2 = upsert_contract_spec(db, overwrite_contract, source="OVERWRITE_ATTEMPT")
        spec_after = get_contract_specification(db, test_contracts[0]["instrument_key"])
        assert spec_after["lot_size"] == 75  # should NOT be overwritten
        print(f"  [OK] Immutability: lot_size preserved as 75 after overwrite attempt (action: {r2.action})")

        result["round_trip_candles"] = saved
        result["round_trip_contracts"] = len(test_contracts)
        result["immutability_verified"] = spec_after["lot_size"] == 75
        result["status"] = "success"

        print(f"\n  [OK] Database round-trip: PASS")

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        print(f"  [FAIL] Error: {e}")

    finally:
        db.close()
        Base.metadata.drop_all(engine)

    return result


# ---------------------------------------------------------------------------
# Report Generation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Section 4: Live Option Instrument Candle Verification
# ---------------------------------------------------------------------------
#
# Upstream capability probe for an UNEXPIRED option instrument key.
#
# Read-only: nothing is persisted, no new credential path is introduced, and
# the access token is never printed or stored.  Only the client functions
# already exported by app.services.upstox are used.
#
# The probe keeps FOUR claims independent and never conflates them:
#   1. the endpoint accepted the live instrument key;
#   2. the endpoint returned candles;
#   3. returned candles contained a non-null Open Interest at index 6;
#   4. the instrument was verified unexpired/current.
#
# Freshness (claim 4) is evaluated against the CURRENT IST date, never the
# historical probe date: a key that expired between the probe date and today
# can never be reported as unexpired.
#
# Endpoint acceptance alone does NOT establish live option OI support.

_OPTION_KEY_EXPIRY_PATTERNS = (
    re.compile(r"\|(\d{2})-(\d{2})-(\d{4})$"),  # NSE_FO|<token>|dd-mm-yyyy
    re.compile(r"\|(\d{4})-(\d{2})-(\d{2})$"),  # NSE_FO|<token>|yyyy-mm-dd
)


def _current_ist_date() -> str:
    """Current calendar date in IST (bounds the historical probe window)."""
    return datetime.now(timezone.utc).astimezone(IST).date().isoformat()


def _parse_expiry_from_option_key(instrument_key: str) -> str | None:
    """Best-effort extraction of the expiry embedded in an option key.

    Returns an ISO ``YYYY-MM-DD`` string, or ``None`` when the key carries no
    recognizable expiry.  This is key-format evidence only; it does not by
    itself prove that the instrument is live upstream.
    """
    if not instrument_key:
        return None
    key = instrument_key.strip()
    for pattern in _OPTION_KEY_EXPIRY_PATTERNS:
        match = pattern.search(key)
        if match is None:
            continue
        try:
            first, second, third = (int(group) for group in match.groups())
            if len(str(first)) == 4:
                return date(first, second, third).isoformat()  # yyyy-mm-dd
            return date(third, second, first).isoformat()  # dd-mm-yyyy
        except ValueError:
            return None
    return None


def _timestamp_offset_label(timestamp: str) -> str:
    """Classify the timezone encoding of a raw Upstox timestamp string."""
    if timestamp.endswith("Z"):
        return "Z"
    match = re.search(r"([+-]\d{2}:\d{2})$", timestamp)
    if match:
        return match.group(1)
    return "no-offset"


def _extract_option_candles(response: Any) -> tuple[list, str | None]:
    """Extract raw candles while keeping a credential-free failure reason.

    Unlike :func:`extract_candles_from_response`, an empty candle list and a
    malformed payload stay distinguishable ("empty" vs "malformed").
    """
    if not isinstance(response, dict):
        return [], f"response is {type(response).__name__}, expected object"
    status = response.get("status")
    if status != "success":
        return [], f"response status is {status!r}"
    data = response.get("data")
    if not isinstance(data, dict):
        return [], f"'data' is {type(data).__name__}, expected object"
    candles = data.get("candles")
    if not isinstance(candles, list):
        return [], f"'data.candles' is {type(candles).__name__}, expected list"
    return candles, None


def _analyse_option_candles(candles: list) -> dict:
    """Structural, non-sensitive evidence from a raw option candle array."""
    evidence: dict[str, Any] = {
        "candle_count": len(candles),
        "candle_array_length": None,
        "candle_array_lengths_observed": [],
        "malformed_row_count": 0,
        "open_interest_field_present": False,
        "open_interest_non_null_count": 0,
        "open_interest_sample": [],
        "timestamp_format_sample": None,
        "timezone_offsets_observed": [],
        "first_timestamp": None,
        "last_timestamp": None,
        "naive_ist_last_timestamp": None,
    }
    if not candles:
        return evidence

    row_lengths: list[int] = []
    timestamps: list[str] = []
    oi_values: list[Any] = []
    offsets: set[str] = set()

    for row in candles:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            evidence["malformed_row_count"] += 1
            continue
        timestamp = row[0]
        if not isinstance(timestamp, str) or not timestamp:
            evidence["malformed_row_count"] += 1
            continue
        if to_ist_naive(timestamp) is None:
            evidence["malformed_row_count"] += 1
            continue
        row_lengths.append(len(row))
        timestamps.append(timestamp)
        offsets.add(_timestamp_offset_label(timestamp))
        if len(row) > 6:
            evidence["open_interest_field_present"] = True
            if row[6] is not None:
                oi_values.append(row[6])

    if row_lengths:
        evidence["candle_array_length"] = row_lengths[0]
        evidence["candle_array_lengths_observed"] = sorted(set(row_lengths))
    if timestamps:
        evidence["timestamp_format_sample"] = timestamps[0]
        evidence["first_timestamp"] = timestamps[0]
        evidence["last_timestamp"] = timestamps[-1]
        naive_last = to_ist_naive(timestamps[-1])
        evidence["naive_ist_last_timestamp"] = naive_last.isoformat() if naive_last else None
    evidence["timezone_offsets_observed"] = sorted(offsets)
    evidence["open_interest_non_null_count"] = len(oi_values)
    evidence["open_interest_sample"] = oi_values[:3]
    return evidence


def _classify_option_endpoint(
    candles: list,
    extraction_error: str | None,
    malformed_row_count: int,
) -> str:
    """Classify one endpoint outcome; anything unexpected fails closed."""
    if extraction_error:
        return "malformed"
    if not candles:
        return "empty"
    if malformed_row_count:
        return "malformed"
    return "ok"


async def _probe_option_endpoint(label: str, request_description: str, fetch: Any) -> dict:
    """Run one read-only option candle request and analyse the response."""
    result: dict[str, Any] = {
        "label": label,
        "request": request_description,
        "status": "pending",
        "http_status": None,
        "error": None,
        "extraction_error": None,
    }
    result.update(_analyse_option_candles([]))
    try:
        response = await fetch()
        candles, extraction_error = _extract_option_candles(response)
        analysis = _analyse_option_candles(candles)
        result.update(analysis)
        result["extraction_error"] = extraction_error
        result["status"] = _classify_option_endpoint(
            candles, extraction_error, analysis["malformed_row_count"],
        )
        result["http_status"] = "success (200)"
    except UpstoxError as e:
        result["status"] = "error"
        result["http_status"] = f"UpstoxError({e.status_code})"
        result["error"] = _sanitize(str(e))
    except Exception as e:  # noqa: BLE001 - must never crash on a vendor response
        result["status"] = "error"
        result["http_status"] = None
        result["error"] = _sanitize(f"{type(e).__name__}: {e}")
    return result


def _assess_option_instrument_freshness(
    instrument_key: str,
    current_ist_date: str,
    probe_date_ist: str,
    intraday: dict,
    candles_returned: bool,
    api_accepted: bool,
) -> dict:
    """Assess whether the supplied key is unexpired/current as of today.

    Freshness is evaluated against the CURRENT IST date, never the historical
    probe date: a key that expired between the probe date and today must never
    be reported as unexpired.

    Evidence combination (fail closed):
      - expiry embedded in the instrument key (key-format evidence only);
      - the key expiry is on/after the current IST date;
      - a usable candle response was received for the exact key;
      - intraday candles timestamped in the current IST session are additional
        (not required) evidence, since the probe may run outside an NSE session.
    """
    expiry_from_key = _parse_expiry_from_option_key(instrument_key)
    try:
        current_date = date.fromisoformat(current_ist_date)
    except ValueError:
        current_date = None

    key_implies_unexpired: bool | None = None
    if expiry_from_key is not None and current_date is not None:
        try:
            key_implies_unexpired = date.fromisoformat(expiry_from_key) >= current_date
        except ValueError:
            key_implies_unexpired = None

    current_session: bool | None = None
    last_timestamp = intraday.get("last_timestamp")
    if isinstance(last_timestamp, str) and current_date is not None:
        naive_last = to_ist_naive(last_timestamp)
        if naive_last is not None:
            current_session = naive_last.date() == current_date

    if not api_accepted:
        verified: bool | None = None
        reason = "no usable upstream response; freshness could not be verified"
    elif current_session is True:
        verified = True
        reason = "intraday endpoint returned a candle in the current IST session"
    elif key_implies_unexpired is True and candles_returned:
        verified = True
        reason = (
            "candles were returned for a key whose embedded expiry is on/after "
            f"the current IST date ({current_ist_date})"
        )
    elif key_implies_unexpired is False:
        verified = False
        reason = f"the key embeds an expiry before the current IST date ({current_ist_date})"
    else:
        verified = None
        reason = "no current-session candle and no embedded future expiry; not established"

    return {
        "expiry_from_instrument_key": expiry_from_key,
        "key_implies_unexpired": key_implies_unexpired,
        "current_ist_date": current_ist_date,
        "probe_date_ist": probe_date_ist,
        "intraday_returned_current_session_candle": current_session,
        "verified_unexpired": verified,
        "verified_reason": reason,
        "method": (
            "instrument-key expiry parse vs current IST date + optional "
            "intraday current-session timestamp check"
        ),
    }


def _option_probe_conclusion(claims: dict, freshness: dict) -> str:
    """Interpretation that keeps the four claims strictly separate."""
    if claims["claim_1_endpoint_accepted_live_option_key"] is not True:
        return (
            "Upstox V3 did not accept the supplied option instrument key with a usable "
            "response (see the per-endpoint status and error fields); no upstream "
            "capability conclusion can be drawn from this probe."
        )
    if claims["claim_2_endpoint_returned_candles"] is not True:
        return (
            "The endpoint accepted the key but returned no candles; this does NOT establish "
            "that Upstox V3 serves live option candles or live option open interest."
        )
    if claims["claim_3_candles_contained_open_interest"] is not True:
        return (
            "Candles were returned but contained no usable open interest value; this does "
            "NOT establish that Upstox V3 serves live option open interest."
        )
    if claims["claim_4_instrument_verified_unexpired"] is not True:
        if freshness.get("verified_unexpired") is False:
            return (
                "Open interest was observed, but the key embeds an expiry before the "
                "current IST date; this probe does NOT establish live option open interest support."
            )
        return (
            "Open interest was observed, but the instrument could not be verified as "
            "unexpired/current as of the current IST date; live option open interest "
            "support is therefore NOT established."
        )
    return (
        "Open interest was observed on candles returned for an instrument verified "
        "unexpired/current as of the current IST date on the Upstox V3 candle endpoints."
    )


async def verify_option_candle_api(
    token: str,
    instrument_key: str,
    candle_date: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Probe Upstox V3 for 3-minute candles of a live option instrument key.

    Exercises ``get_intraday_candles`` (current session) and
    ``get_historical_candles`` (a single bounded date) with
    ``unit="minutes", interval=3``.

    Read-only: no data is persisted and no credential material is printed.
    The four claims are reported separately and endpoint acceptance alone is
    never reported as live option OI support.
    """
    probe_date = candle_date or _current_ist_date()
    current_date = _current_ist_date()

    result: dict[str, Any] = {
        "section": "Live Option Instrument Candle Verification",
        "status": "pending",
        "instrument_key": instrument_key,
        "instrument_key_source": "user-supplied CLI argument (--option-key)",
        "candle_interval": "3-minute (unit=minutes, interval=3)",
        "probe_date_ist": probe_date,
        "current_ist_date": current_date,
        "open_interest_field_index": 6,
    }

    print(f"\n{'='*70}")
    print("SECTION 4: Live Option Instrument Candle Verification")
    print(f"{'='*70}")
    print("  Upstream capability probe for an unexpired option instrument key")
    print("  Read-only: nothing is persisted; no credential material is printed")
    print(f"  Instrument key: {instrument_key}")
    print(f"  Probe date (IST, historical request): {probe_date}")
    print(f"  Current IST date (freshness): {current_date}")
    print("  Probe 1: get_intraday_candles (current session, 3-minute)")
    print(f"  Probe 2: get_historical_candles (single date {probe_date}, 3-minute)")
    print()

    if dry_run:
        print("  [DRY RUN] Would probe the option instrument key on Upstox V3.")
        result["status"] = "dry_run"
        return result

    async def _fetch_intraday() -> dict:
        return await get_intraday_candles(
            token, instrument_key=instrument_key, unit="minutes", interval=3,
        )

    async def _fetch_historical() -> dict:
        return await get_historical_candles(
            token,
            instrument_key=instrument_key,
            to_date=probe_date,
            from_date=probe_date,
            unit="minutes",
            interval=3,
        )

    intraday = await _probe_option_endpoint(
        "intraday",
        f"get_intraday_candles({instrument_key}, unit=minutes, interval=3)",
        _fetch_intraday,
    )
    historical = await _probe_option_endpoint(
        "historical",
        f"get_historical_candles({instrument_key}, to_date={probe_date}, "
        f"from_date={probe_date}, unit=minutes, interval=3)",
        _fetch_historical,
    )

    for endpoint in (intraday, historical):
        if endpoint["status"] in ("ok", "empty", "malformed"):
            print(
                f"  [{endpoint['status'].upper()}] {endpoint['label']}: "
                f"candles={endpoint['candle_count']}, "
                f"OI_non_null={endpoint['open_interest_non_null_count']}, "
                f"offsets={endpoint['timezone_offsets_observed']}"
            )
        else:
            print(f"  [FAIL] {endpoint['label']}: {endpoint['http_status']}")

    statuses = [intraday["status"], historical["status"]]
    usable = [status for status in statuses if status in ("ok", "empty")]
    if len(usable) == len(statuses):
        result["status"] = "success"
    elif usable:
        result["status"] = "partial"
    else:
        result["status"] = "error"

    claim_1 = bool(usable)
    claim_2 = any(endpoint["status"] == "ok" for endpoint in (intraday, historical))
    claim_3 = any(
        endpoint["open_interest_non_null_count"] > 0
        for endpoint in (intraday, historical)
        if endpoint["status"] == "ok"
    )

    freshness = _assess_option_instrument_freshness(
        instrument_key,
        current_date,
        probe_date,
        intraday,
        candles_returned=claim_2,
        api_accepted=claim_1,
    )

    claims = {
        "claim_1_endpoint_accepted_live_option_key": claim_1,
        "claim_2_endpoint_returned_candles": claim_2,
        "claim_3_candles_contained_open_interest": claim_3,
        "claim_4_instrument_verified_unexpired": freshness["verified_unexpired"],
    }

    if intraday["status"] == historical["status"]:
        endpoint_consistency = "consistent"
    else:
        endpoint_consistency = "inconsistent"

    intraday_has_oi = intraday["open_interest_non_null_count"] > 0
    historical_has_oi = historical["open_interest_non_null_count"] > 0
    if intraday["status"] == "ok" and historical["status"] == "ok":
        open_interest_consistency = (
            "consistent" if intraday_has_oi == historical_has_oi else "inconsistent"
        )
    else:
        open_interest_consistency = "not_comparable"

    result["intraday"] = intraday
    result["historical"] = historical
    result["endpoint_consistency"] = endpoint_consistency
    result["open_interest_consistency"] = open_interest_consistency
    result["instrument_freshness"] = freshness
    result["claims"] = claims
    result["live_option_oi_established"] = bool(
        claim_3 and freshness["verified_unexpired"] is True
    )
    result["conclusion"] = _option_probe_conclusion(claims, freshness)

    print()
    print("  Claims (each must be established independently):")
    for claim_name, claim_value in claims.items():
        print(f"    {claim_name}: {claim_value}")
    print(f"  Instrument freshness: {freshness['verified_unexpired']} ({freshness['verified_reason']})")
    print(f"  Live option OI established: {result['live_option_oi_established']}")
    print(f"  Conclusion: {result['conclusion']}")

    return result


def generate_report(results: list[dict]) -> str:
    """Generate the Phase 7.9 verification report in markdown."""
    lines = [
        "# Phase 7.9 - Live Upstox API Verification Report",
        "",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        "---",
        "",
    ]

    for r in results:
        lines.append(f"## {r.get('section', 'Unknown Section')}")
        lines.append("")
        lines.append(f"**Status:** {r.get('status', 'unknown')}")
        lines.append("")

        # Key findings
        for key, value in r.items():
            if key in ("section", "status"):
                continue
            if isinstance(value, dict):
                lines.append(f"### {key}")
                for k2, v2 in value.items():
                    lines.append(f"- **{k2}:** `{_sanitize(str(v2))}`")
                lines.append("")
            elif isinstance(value, list):
                lines.append(f"- **{key}:** {_sanitize(json.dumps(value, default=str))}")
            else:
                lines.append(f"- **{key}:** `{_sanitize(str(value))}`")
        lines.append("")
        lines.append("---")
        lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append("")
    all_pass = all(r.get("status") in ("success", "dry_run") for r in results)
    for r in results:
        status_icon = "[PASS]" if r.get("status") in ("success", "dry_run") else "[FAIL]"
        lines.append(f"- {status_icon} {r.get('section', '?')}: {r.get('status', 'unknown')}")
    lines.append("")
    lines.append(f"**Overall:** {'[PASS] PASS' if all_pass else '[FAIL] ISSUES FOUND'}")
    lines.append("")

    # Disclaimer
    lines.append("---")
    lines.append("")
    lines.append("*This report was generated by the Phase 7.9 live verification tool.*")
    lines.append("*No access tokens, API secrets, or credentials are included.*")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _type_matches(actual: str, expected: str) -> bool:
    """Check if actual Python type name matches expected."""
    mapping = {
        "str": ("str",),
        "number": ("int", "float"),
        "int": ("int",),
        "bool": ("bool",),
        "list": ("list",),
    }
    return actual in mapping.get(expected, ())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(
        description="Phase 7.9 - Real Upstox API Live Verification",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m app.tools.live_verification --all
  python -m app.tools.live_verification --candles
  python -m app.tools.live_verification --contracts
  python -m app.tools.live_verification --round-trip
  python -m app.tools.live_verification --option-key "NSE_FO|47983|31-12-2099"
  python -m app.tools.live_verification --dry-run
        """,
    )
    parser.add_argument("--all", action="store_true", help="Run all verification sections")
    parser.add_argument("--candles", action="store_true", help="Verify historical candle API")
    parser.add_argument("--contracts", action="store_true", help="Verify expired contract API")
    parser.add_argument("--round-trip", action="store_true", help="Verify database round-trip")
    parser.add_argument("--option-key", default=None, help="Unexpired option instrument key to probe (read-only live option candle/OI capability probe)")
    parser.add_argument("--option-candle-date", default=None, help="Single date for the historical option probe (default: current IST date)")
    parser.add_argument("--candle-date", default=DEFAULT_CANDLE_DATE, help=f"Candle verification date (default: {DEFAULT_CANDLE_DATE})")
    parser.add_argument("--expiry-date", default=DEFAULT_EXPIRY_DATE, help=f"Contract expiry date (default: {DEFAULT_EXPIRY_DATE})")
    parser.add_argument("--dry-run", action="store_true", help="Check authentication only, don't call API")
    parser.add_argument("--report", action="store_true", help="Generate verification report file")
    parser.add_argument("--report-path", default=REPORT_PATH, help=f"Report output path (default: {REPORT_PATH})")

    args = parser.parse_args()

    # Require at least one action
    if not any([args.all, args.candles, args.contracts, args.round_trip, args.dry_run, args.option_key]):
        parser.print_help()
        print("\nERROR: Specify at least one of --all, --candles, --contracts, --round-trip, --option-key, or --dry-run")
        sys.exit(1)

    logging.basicConfig(level=logging.WARNING)

    print("=" * 70)
    print("Phase 7.9 - Real Upstox API Live Verification")
    print("=" * 70)
    print()

    # Get token
    if not args.dry_run:
        token = _get_access_token()
        print("[OK] Authenticated session found")
    else:
        token = "dry-run-placeholder"
        print("[OK] Dry-run mode - no API calls will be made")

    print(f"  Candle date:  {args.candle_date}")
    print(f"  Expiry date:  {args.expiry_date}")
    if args.option_key:
        print(f"  Option key:   {args.option_key}")
        print(f"  Option probe date: {args.option_candle_date or _current_ist_date()}")
    print()

    results = []

    # Run sections
    if args.all or args.candles:
        r = await verify_candle_api(token, args.candle_date, dry_run=args.dry_run)
        results.append(r)

    if args.all or args.contracts:
        r = await verify_contract_api(token, args.expiry_date, dry_run=args.dry_run)
        results.append(r)

    if args.all or args.round_trip:
        r = await verify_db_roundtrip(token, args.candle_date, dry_run=args.dry_run)
        results.append(r)

    if (args.all or args.option_key) and args.option_key:
        r = await verify_option_candle_api(
            token,
            args.option_key,
            args.option_candle_date,
            dry_run=args.dry_run,
        )
        results.append(r)

    # Generate report
    if results and (args.report or args.all):
        report = generate_report(results)
        report_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            args.report_path,
        )
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w") as f:
            f.write(report)
        print(f"\n[OK] Report saved to: {args.report_path}")

    # Final summary
    print(f"\n{'='*70}")
    print(f"Verification Complete")
    print(f"{'='*70}")
    for r in results:
        icon = "[OK]" if r.get("status") in ("success", "dry_run") else "[FAIL]"
        print(f"  {icon} {r.get('section', '?')}: {r.get('status', 'unknown')}")
    print()

    if any(r.get("status") not in ("success", "dry_run") for r in results):
        print("Some sections had issues. Review the report for details.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
