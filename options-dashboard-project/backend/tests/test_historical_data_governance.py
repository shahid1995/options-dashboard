"""Day 48 — historical data governance service tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    DataCompleteness,
    HistoricalDatasetGovernance,
    IngestionCheckpoint,
    IngestionLog,
    OptionGreeks,
    OptionCandle,
)
from app.services import historical_data_governance as hdg


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _catalog(
    db,
    *,
    key: str,
    tier: str = "RAW",
    pipeline: str | None = "backfill_options",
    completeness_data_type: str | None = "option_candles",
    source: str = "UPSTOX",
    entitlement_status: str = hdg.ENTITLEMENT_REVIEW_REQUIRED,
    license_status: str = hdg.LICENSE_REVIEW_REQUIRED,
    usage_policy: str = hdg.USAGE_INTERNAL_ONLY,
    redistribution_status: str = hdg.REDISTRIBUTION_REVIEW_REQUIRED,
    retention_policy: str = hdg.RETENTION_KEEP,
    retention_days: int | None = None,
    retention_enforced: bool = False,
    raw_immutable: bool = True,
    recomputable: bool = True,
    dependencies: list[str] | None = None,
):
    row = HistoricalDatasetGovernance(
        dataset_key=key,
        domain="MARKET_DATA" if tier == "RAW" else "QUANT",
        dataset_tier=tier,
        table_name="option_candles" if key.endswith("OPTION_CANDLES_3MIN") else (
            "option_greeks" if key.endswith("OPTION_GREEKS") else "historical_gex"
        ),
        pipeline=pipeline,
        completeness_data_type=completeness_data_type,
        source=source,
        source_reference="test",
        source_version="test-v1",
        entitlement_requirement="TEST",
        entitlement_status=entitlement_status,
        license_status=license_status,
        usage_policy=usage_policy,
        redistribution_status=redistribution_status,
        retention_policy=retention_policy,
        retention_days=retention_days,
        retention_enforced=retention_enforced,
        raw_immutable=raw_immutable,
        recomputable=recomputable,
        dependencies_json=json.dumps(dependencies or []),
        notes="test",
        active=True,
    )
    db.add(row)
    db.commit()
    return row


def test_entitlement_and_redistribution_fail_closed(db):
    key = "UPSTOX_OPTION_CANDLES_3MIN"
    _catalog(db, key=key)

    with pytest.raises(hdg.HistoricalDataGovernanceError):
        hdg.assert_entitlement_ready(db, key)

    hdg.assert_entitlement_ready(db, key, allow_review_required=True)

    with pytest.raises(hdg.HistoricalDataGovernanceError):
        hdg.assert_redistribution_allowed(db, key)

    with pytest.raises(hdg.HistoricalDataGovernanceError):
        hdg.assert_usage_allowed(db, key, purpose=hdg.PURPOSE_PUBLIC)

    hdg.assert_usage_allowed(db, key, purpose=hdg.PURPOSE_BACKTEST)


def test_stage_mapping_rejects_unknown_and_deduplicates(db):
    _catalog(db, key="UPSTOX_OPTION_CANDLES_3MIN")
    _catalog(
        db,
        key="UPSTOX_NIFTY_CANDLES_3MIN",
        pipeline="backfill_nifty",
        completeness_data_type="nifty_candles",
    )
    assert hdg.dataset_keys_for_stages(["options", "nifty", "options"]) == [
        "UPSTOX_OPTION_CANDLES_3MIN",
        "UPSTOX_NIFTY_CANDLES_3MIN",
    ]

    with pytest.raises(hdg.HistoricalDataGovernanceError):
        hdg.dataset_keys_for_stages(["unknown"])


def test_ingestion_run_snapshots_policy_and_metrics(db):
    key = "UPSTOX_OPTION_CANDLES_3MIN"
    _catalog(db, key=key)

    run = hdg.start_ingestion_run(
        db,
        dataset_keys=[key],
        background_job_id="job-1",
        coverage_start="2026-09-01",
        coverage_end="2026-09-02",
        metadata={"source_job": "test"},
        run_id="run-day48",
    )

    assert json.loads(run.dataset_keys_json) == [key]
    assert json.loads(run.source_snapshot_json)[key]["source"] == "UPSTOX"
    assert (
        json.loads(run.policy_snapshot_json)[key]["redistribution_status"]
        == hdg.REDISTRIBUTION_REVIEW_REQUIRED
    )

    db.add_all(
        [
            IngestionCheckpoint(
                pipeline="backfill_options",
                instrument_key="NSE_FO|TEST|01-10-2026",
                run_id=run.run_id,
                status="COMPLETED",
                items_processed=10,
                items_total=10,
            ),
            DataCompleteness(
                instrument_key="NSE_FO|TEST|01-10-2026",
                session_date="2026-09-01",
                data_type="option_candles",
                expected_count=10,
                actual_count=10,
                missing_count=0,
                status="COMPLETE",
            ),
            IngestionLog(
                run_id=run.run_id,
                operation="option_candles",
                started_at="2026-09-01T00:00:00+00:00",
                completed_at="2026-09-01T00:01:00+00:00",
                status="SUCCESS",
                rows_fetched=10,
                rows_inserted=10,
            ),
        ]
    )
    db.commit()

    refreshed = hdg.refresh_ingestion_run_metrics(db, run.run_id)
    assert refreshed.checkpoints_total == 1
    assert refreshed.checkpoints_completed == 1
    assert refreshed.expected_records == 10
    assert refreshed.actual_records == 10
    assert refreshed.missing_records == 0
    assert refreshed.completeness_status == "COMPLETE"

    finished = hdg.finish_ingestion_run(db, run.run_id, status=hdg.RUN_SUCCEEDED)
    assert finished.status == hdg.RUN_SUCCEEDED
    assert finished.completed_at is not None


def test_recomputation_contract_is_transitive(db):
    raw_key = "UPSTOX_OPTION_CANDLES_3MIN"
    model_key = "STRIKENOVA_OPTION_GREEKS"
    _catalog(db, key=raw_key)
    _catalog(
        db,
        key=model_key,
        tier="MODEL",
        pipeline=None,
        completeness_data_type=None,
        source="STRIKENOVA",
        entitlement_status=hdg.ENTITLEMENT_NOT_APPLICABLE,
        license_status=hdg.LICENSE_INTERNAL,
        usage_policy=hdg.USAGE_INTERNAL_ONLY,
        redistribution_status=hdg.REDISTRIBUTION_REVIEW_REQUIRED,
        retention_policy=hdg.RETENTION_DELETE_AFTER_DAYS,
        retention_days=365,
        raw_immutable=False,
        recomputable=True,
        dependencies=[raw_key],
    )

    hdg.assert_recomputation_safe(db, model_key)

    raw = db.scalar(
        select(HistoricalDatasetGovernance).where(
            HistoricalDatasetGovernance.dataset_key == raw_key
        )
    )
    raw.raw_immutable = False
    db.commit()

    with pytest.raises(hdg.HistoricalDataGovernanceError):
        hdg.assert_recomputation_safe(db, model_key)


def _option_greek(**overrides):
    values = dict(
        instrument_key="NSE_FO|TEST|01-10-2026",
        interval="3min",
        open_time=datetime(2026, 1, 1, 3, 45),
        spot=25000.0,
        strike=25000.0,
        expiry="2026-10-01",
        option_type="CE",
        option_price=100.0,
        lot_size=75,
        time_to_expiry=0.1,
        risk_free_rate=0.065,
        intrinsic_value=0.0,
        implied_volatility=0.2,
        delta=0.5,
        gamma=0.01,
        vega=10.0,
        theta=-5.0,
        calc_model="BLACK_SCHOLES_EUROPEAN",
        calc_version="test-v1",
        calculated_at=datetime(2026, 1, 1, 3, 46),
        status="SUCCESS",
    )
    values.update(overrides)
    return OptionGreeks(**values)


def test_retention_is_dry_run_first_and_requires_catalog_enablement(db):
    key = "STRIKENOVA_OPTION_GREEKS"
    _catalog(
        db,
        key=key,
        tier="MODEL",
        pipeline=None,
        completeness_data_type=None,
        source="STRIKENOVA",
        entitlement_status=hdg.ENTITLEMENT_NOT_APPLICABLE,
        license_status=hdg.LICENSE_INTERNAL,
        usage_policy=hdg.USAGE_INTERNAL_ONLY,
        retention_policy=hdg.RETENTION_DELETE_AFTER_DAYS,
        retention_days=30,
        raw_immutable=False,
        recomputable=True,
        dependencies=["UPSTOX_OPTION_CANDLES_3MIN"],
    )
    _catalog(db, key="UPSTOX_OPTION_CANDLES_3MIN")

    old_row = _option_greek(
        open_time=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60)
    )
    new_row = _option_greek(
        instrument_key="NSE_FO|TEST2|01-10-2026",
        open_time=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=5)
    )
    db.add_all([old_row, new_row])
    db.commit()

    preview = hdg.enforce_retention(
        db,
        key,
        now=datetime.now(timezone.utc),
        execute=False,
    )
    assert preview.candidate_rows == 1
    assert preview.deleted_rows == 0
    assert preview.executable is False

    governance = db.scalar(
        select(HistoricalDatasetGovernance).where(
            HistoricalDatasetGovernance.dataset_key == key
        )
    )
    governance.retention_enforced = True
    db.commit()

    applied = hdg.enforce_retention(
        db,
        key,
        now=datetime.now(timezone.utc),
        execute=True,
    )
    assert applied.candidate_rows == 1
    assert applied.deleted_rows == 1
    assert db.scalar(select(func.count()).select_from(OptionGreeks)) == 1


def test_raw_retention_is_not_executable_even_when_policy_is_destructive(db):
    key = "UPSTOX_OPTION_CANDLES_3MIN"
    _catalog(
        db,
        key=key,
        retention_policy=hdg.RETENTION_DELETE_AFTER_DAYS,
        retention_days=30,
        retention_enforced=True,
        raw_immutable=True,
    )
    db.add(
        OptionCandle(
            instrument_key="NSE_FO|TEST|01-10-2026",
            interval="3min",
            open_time=datetime(2025, 1, 1, 3, 45),
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
            open_interest=1.0,
            source="TEST",
            fetched_at=datetime(2025, 1, 1, 4, 0),
        )
    )
    db.commit()

    plan = hdg.enforce_retention(
        db,
        key,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        execute=True,
    )
    assert plan.deleted_rows == 0
    assert db.scalar(select(func.count()).select_from(OptionCandle)) == 1


def test_metrics_refresh_without_completeness_rows_stays_closed(db):
    """Regression: metrics refresh must not raise when no completeness rows exist.

    The SQL-level aggregation rewrite left a stale reference that raised
    NameError whenever a run had no DataCompleteness rows, which would have
    turned successful ingestions into falsely-reported failures at
    finalization time.
    """
    key = "UPSTOX_OPTION_CANDLES_3MIN"
    _catalog(db, key=key)

    run = hdg.start_ingestion_run(
        db,
        dataset_keys=[key],
        coverage_start="2026-09-01",
        coverage_end="2026-09-02",
        run_id="run-day48-empty",
    )
    db.add(
        IngestionCheckpoint(
            pipeline="backfill_options",
            instrument_key="NSE_FO|TEST|01-10-2026",
            run_id=run.run_id,
            status="PENDING",
            items_processed=0,
            items_total=5,
        )
    )
    db.commit()

    refreshed = hdg.refresh_ingestion_run_metrics(db, run.run_id)
    assert refreshed.expected_records is None
    assert refreshed.actual_records == 0
    assert refreshed.missing_records == 0
    assert refreshed.checkpoints_total == 1
    assert refreshed.checkpoints_completed == 0
    assert refreshed.completeness_status == "UNKNOWN"

    finished = hdg.finish_ingestion_run(db, run.run_id, status=hdg.RUN_SUCCEEDED)
    assert finished.status == hdg.RUN_SUCCEEDED
    assert finished.expected_records is None
    assert finished.completed_at is not None
