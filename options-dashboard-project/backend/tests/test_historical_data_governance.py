"""Day 48 — historical data governance service tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    ContractSpec,
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


def test_run_without_run_scoped_evidence_stays_unknown(db):
    """Regression: a run that produced no evidence must stay UNKNOWN.

    The manifest may never report a run complete on the strength of rows it
    did not produce. This also guards the metric refresh against the
    aggregation path raising when there is nothing to aggregate, which would
    turn a successful ingestion into a falsely-reported failure.
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

    refreshed = hdg.refresh_ingestion_run_metrics(db, run.run_id)
    assert refreshed.expected_records is None
    assert refreshed.actual_records == 0
    assert refreshed.missing_records == 0
    assert refreshed.checkpoints_total == 0
    assert refreshed.checkpoints_completed == 0
    assert refreshed.completeness_status == "UNKNOWN"

    finished = hdg.finish_ingestion_run(db, run.run_id, status=hdg.RUN_SUCCEEDED)
    assert finished.status == hdg.RUN_SUCCEEDED
    assert finished.expected_records is None
    assert finished.completeness_status == "UNKNOWN"
    assert finished.completed_at is not None


def test_governance_datetimes_are_naive_utc(db):
    """Regression (CodeRabbit HIGH on 2beb38b): the governance service must
    store and compare naive UTC datetimes, matching the historical tables'
    timezone-naive DateTime columns. An aware `now` must be normalized
    before computing a retention cutoff, and completed_at must be naive.
    """
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
        retention_policy=hdg.RETENTION_DELETE_AFTER_DAYS,
        retention_days=30,
        retention_enforced=True,
        raw_immutable=False,
        recomputable=True,
        dependencies=["UPSTOX_OPTION_CANDLES_3MIN"],
    )
    _catalog(db, key="UPSTOX_OPTION_CANDLES_3MIN")

    def _greek(instrument_key, open_time):
        return _option_greek(instrument_key=instrument_key, open_time=open_time)

    # OptionGreeks.open_time is naive IST (Phase 7.24.4), so the cutoff must
    # be expressed in IST too. `now` is 2025-02-01 12:00 UTC = 17:30 IST, so
    # the 30-day cutoff is 2025-01-02 17:30 IST. A naive-UTC cutoff would be
    # 2025-01-02 12:00 and would silently retain the 12:00-17:30 rows.
    db.add_all(
        [
            # Far outside the window: candidate.
            _greek("NSE_FO|A|01-10-2026", datetime(2025, 1, 1, 3, 45)),
            # Between the naive-UTC and IST cutoffs: the row a naive-UTC
            # cutoff would have wrongly retained.
            _greek("NSE_FO|E|01-10-2026", datetime(2025, 1, 2, 12, 0, 0)),
            # One second before the IST cutoff: candidate (strict <).
            _greek("NSE_FO|B|01-10-2026", datetime(2025, 1, 2, 17, 29, 59)),
            # Exactly at the IST cutoff: NOT a candidate.
            _greek("NSE_FO|C|01-10-2026", datetime(2025, 1, 2, 17, 30, 0)),
            # One second after the IST cutoff: NOT a candidate.
            _greek("NSE_FO|D|01-10-2026", datetime(2025, 1, 2, 17, 30, 1)),
        ]
    )
    db.commit()

    # An AWARE `now` and its naive equivalent must produce identical plans.
    now_aware = datetime(2025, 2, 1, 12, 0, 0, tzinfo=timezone.utc)
    now_naive = datetime(2025, 2, 1, 12, 0, 0)
    plan_aware = hdg.plan_retention(db, key, now=now_aware)
    plan_naive = hdg.plan_retention(db, key, now=now_naive)

    assert plan_aware.cutoff == plan_naive.cutoff
    assert plan_aware.cutoff.tzinfo is None
    # Expressed in the column's own clock (IST), never naive UTC.
    assert plan_aware.cutoff == datetime(2025, 1, 2, 17, 30)
    assert plan_aware.candidate_rows == 3

    # finalize timestamps are stored naive UTC as well, and the Day 48
    # governance model defaults (started_at/created_at/updated_at) are naive.
    run = hdg.start_ingestion_run(db, dataset_keys=[key], run_id="run-naive-ts")
    finished = hdg.finish_ingestion_run(db, run.run_id, status=hdg.RUN_FAILED)
    assert finished.completed_at.tzinfo is None
    assert finished.started_at.tzinfo is None
    catalog_row = db.scalar(
        select(HistoricalDatasetGovernance).where(
            HistoricalDatasetGovernance.dataset_key == key
        )
    )
    assert catalog_row.created_at.tzinfo is None


def test_realistic_run_derives_completeness_from_pipeline_evidence(db):
    """A real backfill leaves run-scoped IngestionLog and IngestionCheckpoint
    rows behind. The manifest must report that evidence instead of ending
    UNKNOWN with expected_records=None, and must still report a real gap."""
    nifty = "UPSTOX_NIFTY_CANDLES_3MIN"
    options = "UPSTOX_OPTION_CANDLES_3MIN"
    _catalog(
        db,
        key=nifty,
        pipeline="backfill_nifty",
        completeness_data_type="nifty_candles",
    )
    _catalog(db, key=options)

    run = hdg.start_ingestion_run(
        db, dataset_keys=[nifty, options], run_id="run-realistic"
    )
    db.add_all(
        [
            IngestionLog(
                run_id=run.run_id,
                operation="nifty_candles",
                started_at="2026-09-01T00:00:00+00:00",
                completed_at="2026-09-01T00:01:00+00:00",
                status="SUCCESS",
                rows_fetched=75,
                rows_inserted=75,
            ),
            IngestionLog(
                run_id=run.run_id,
                operation="option_candles",
                started_at="2026-09-01T00:01:00+00:00",
                completed_at="2026-09-01T00:05:00+00:00",
                status="PARTIAL",
                rows_fetched=90,
                rows_inserted=80,
                error_message="429 rate limit",
            ),
            IngestionCheckpoint(
                pipeline="backfill_options",
                instrument_key="NSE_FO|X|01-10-2026",
                run_id=run.run_id,
                status="COMPLETED",
                items_processed=80,
                items_total=90,
            ),
        ]
    )
    db.commit()

    refreshed = hdg.refresh_ingestion_run_metrics(db, run.run_id)
    assert refreshed.expected_records == 90
    assert refreshed.actual_records == 165
    assert refreshed.missing_records == 0
    assert refreshed.checkpoints_total == 1
    assert refreshed.checkpoints_completed == 1
    # The option-candle operation came back PARTIAL, so the run is not whole.
    assert refreshed.completeness_status == "PARTIAL"

    finished = hdg.finish_ingestion_run(db, run.run_id, status=hdg.RUN_SUCCEEDED)
    assert finished.status == hdg.RUN_PARTIAL
    assert finished.completed_at is not None

    # A clean run over the same datasets reports COMPLETE and stays SUCCEEDED.
    clean = hdg.start_ingestion_run(
        db, dataset_keys=[options], run_id="run-realistic-clean"
    )
    db.add_all(
        [
            IngestionLog(
                run_id=clean.run_id,
                operation="option_candles",
                started_at="2026-09-02T00:00:00+00:00",
                completed_at="2026-09-02T00:05:00+00:00",
                status="SUCCESS",
                rows_fetched=90,
                rows_inserted=90,
            ),
            IngestionCheckpoint(
                pipeline="backfill_options",
                instrument_key="NSE_FO|Y|01-10-2026",
                run_id=clean.run_id,
                status="COMPLETED",
                items_processed=90,
                items_total=90,
            ),
        ]
    )
    db.commit()

    ok = hdg.finish_ingestion_run(db, clean.run_id, status=hdg.RUN_SUCCEEDED)
    assert ok.status == hdg.RUN_SUCCEEDED
    assert ok.completeness_status == "COMPLETE"
    assert ok.expected_records == 90
    assert ok.actual_records == 90
    assert ok.missing_records == 0


def test_completeness_ignores_rows_the_run_did_not_produce(db):
    """DataCompleteness is cumulative and carries no run identity, so its rows
    can never be attributed to a specific acquisition. A run with no evidence
    of its own stays UNKNOWN even when matching rows exist for its dataset and
    window — including a PARTIAL row that would otherwise downgrade it."""
    key = "UPSTOX_NIFTY_CANDLES_3MIN"
    _catalog(
        db,
        key=key,
        pipeline="backfill_nifty",
        completeness_data_type="nifty_candles",
    )

    run = hdg.start_ingestion_run(
        db,
        dataset_keys=[key],
        coverage_start="2026-09-01",
        coverage_end="2026-09-02",
        run_id="run-no-evidence",
    )
    db.add_all(
        [
            # Both rows sit inside this run's dataset and date window, but
            # neither was produced by this run.
            DataCompleteness(
                instrument_key="NSE_INDEX|NIFTY 50",
                session_date="2026-09-01",
                data_type="nifty_candles",
                expected_count=500,
                actual_count=0,
                missing_count=500,
                status="PARTIAL",
            ),
            DataCompleteness(
                instrument_key="NSE_INDEX|NIFTY 50",
                session_date="2026-09-02",
                data_type="nifty_candles",
                expected_count=75,
                actual_count=75,
                missing_count=0,
                status="COMPLETE",
            ),
        ]
    )
    db.commit()

    refreshed = hdg.refresh_ingestion_run_metrics(db, run.run_id)
    assert refreshed.completeness_status == "UNKNOWN"
    assert refreshed.expected_records is None
    assert refreshed.missing_records == 0

    finished = hdg.finish_ingestion_run(db, run.run_id, status=hdg.RUN_SUCCEEDED)
    assert finished.status == hdg.RUN_SUCCEEDED


def test_retention_cutoff_matches_each_targets_stored_clock(db):
    """Every retention target's cutoff must be expressed in the clock that
    target actually stores — in the same plan_retention call.

    OptionGreeks.open_time is naive IST; ContractSpec.fetched_at is naive UTC.
    Comparing both against naive UTC is a 5h30 boundary error that silently
    retains rows it should delete.
    """
    greeks_key = "STRIKENOVA_OPTION_GREEKS"
    contracts_key = "UPSTOX_CONTRACT_SPECS"
    _catalog(
        db,
        key=greeks_key,
        tier="MODEL",
        pipeline=None,
        completeness_data_type=None,
        source="STRIKENOVA",
        entitlement_status=hdg.ENTITLEMENT_NOT_APPLICABLE,
        license_status=hdg.LICENSE_INTERNAL,
        retention_policy=hdg.RETENTION_DELETE_AFTER_DAYS,
        retention_days=30,
        retention_enforced=True,
        raw_immutable=False,
        recomputable=True,
        dependencies=[contracts_key],
    )
    _catalog(
        db,
        key=contracts_key,
        pipeline="backfill_contracts",
        completeness_data_type="contract_metadata",
        retention_policy=hdg.RETENTION_DELETE_AFTER_DAYS,
        retention_days=30,
        retention_enforced=True,
    )

    def _contract(instrument_key, fetched_at):
        return ContractSpec(
            instrument_key=instrument_key,
            underlying="NIFTY",
            underlying_key="NSE_FO|58124",
            expiry="2026-10-01",
            strike_price=25000.0,
            instrument_type="CE",
            trading_symbol="NIFTY25OCT25000CE",
            segment="NSE_FO",
            exchange="NSE",
            source="TEST",
            source_reference="test",
            fetched_at=fetched_at,
        )

    db.add_all(
        [
            # Naive IST, five and a half hours after the naive-UTC cutoff.
            _option_greek(
                instrument_key="NSE_FO|IST|01-10-2026",
                open_time=datetime(2026, 9, 4, 10, 0, 0),
            ),
            _contract("NSE_FO|UTC-BEFORE|01-10-2026", datetime(2026, 9, 4, 8, 59, 59)),
            _contract("NSE_FO|UTC-AT|01-10-2026", datetime(2026, 9, 4, 9, 0, 0)),
        ]
    )
    db.commit()

    now = datetime(2026, 10, 4, 9, 0, 0, tzinfo=timezone.utc)

    # IST-stored target: cutoff is the UTC instant expressed in IST.
    greeks_plan = hdg.plan_retention(db, greeks_key, now=now)
    assert greeks_plan.cutoff == datetime(2026, 9, 4, 14, 30)
    # The 10:00 IST row is older than 14:30 IST and must be a candidate. A
    # naive-UTC cutoff of 09:00 would have retained it.
    assert greeks_plan.candidate_rows == 1

    # UTC-stored target keeps naive UTC.
    contracts_plan = hdg.plan_retention(db, contracts_key, now=now)
    assert contracts_plan.cutoff == datetime(2026, 9, 4, 9, 0)
    assert contracts_plan.candidate_rows == 1

    # Executing the IST plan deletes exactly the row it selected.
    applied = hdg.enforce_retention(db, greeks_key, now=now, execute=True)
    assert applied.deleted_rows == 1
    remaining = db.scalars(select(OptionGreeks)).all()
    assert [r.instrument_key for r in remaining] == []


def test_acquisition_gate_follows_the_approved_rights_policy(db):
    """DECISIONS.md ADR-020: unresolved entitlement may be acquired only for
    internal research or backtest, and only through the explicit
    allow_review_required escape hatch. Usage stays fail-closed, redistribution
    blocks any acquisition that can publish, and neither PRIVATE_USER nor
    PUBLIC receives the exception."""
    key = "UPSTOX_OPTION_CANDLES_3MIN"
    _catalog(db, key=key)

    # Seeded REVIEW_REQUIRED / INTERNAL_ONLY: internal research proceeds and
    # the exception is reported back so the manifest can record it.
    policy = hdg.assert_acquisition_allowed(
        db, [key], purpose=hdg.PURPOSE_INTERNAL_RESEARCH
    )
    assert policy.entitlement_review_required == (key,)
    assert policy.redistribution_review_required == (key,)
    assert hdg.assert_acquisition_allowed(
        db, [key], purpose=hdg.PURPOSE_BACKTEST
    ).entitlement_review_required == (key,)

    # PUBLIC never receives the entitlement exception.
    with pytest.raises(hdg.HistoricalDataGovernanceError):
        hdg.assert_acquisition_allowed(db, [key], purpose=hdg.PURPOSE_PUBLIC)

    # With entitlement resolved, the remaining gates still bite.
    row = db.scalar(
        select(HistoricalDatasetGovernance).where(
            HistoricalDatasetGovernance.dataset_key == key
        )
    )
    row.entitlement_status = hdg.ENTITLEMENT_VERIFIED
    db.commit()

    # INTERNAL_ONLY does not permit a private-user purpose.
    with pytest.raises(hdg.HistoricalDataGovernanceError):
        hdg.assert_acquisition_allowed(db, [key], purpose=hdg.PURPOSE_PRIVATE_USER)
    # Nor a publishing purpose, whose unresolved redistribution blocks it.
    with pytest.raises(hdg.HistoricalDataGovernanceError):
        hdg.assert_acquisition_allowed(db, [key], purpose=hdg.PURPOSE_PUBLIC)

    # An entitlement state that is neither verified nor review-required is
    # refused outright, exception or not.
    row.entitlement_status = "DENIED"
    db.commit()
    with pytest.raises(hdg.HistoricalDataGovernanceError):
        hdg.assert_acquisition_allowed(
            db, [key], purpose=hdg.PURPOSE_INTERNAL_RESEARCH
        )
