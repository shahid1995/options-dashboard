"""Day 48 — historical data governance service tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    HistoricalIngestionRun,

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
    table_name: str | None = None,
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
        table_name=table_name
        or (
            "option_candles"
            if key.endswith("OPTION_CANDLES_3MIN")
            else (
                "option_greeks"
                if key.endswith("OPTION_GREEKS")
                else "historical_gex"
            )
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


def test_start_ingestion_run_refresh_failure_leaves_manifest_failed(db):
    """Finding 1 regression: a manifest committed as RUNNING whose post-commit
    refresh then fails must NOT remain RUNNING. The failure remains observable
    (the original exception propagates), the run id is available independently
    of ORM refresh success, and terminalization occurs through the safe fallback
    path (force_terminal_ingestion_run) rather than being suppressed.
    """
    key = "UPSTOX_OPTION_CANDLES_3MIN"
    _catalog(db, key=key)

    # An explicit session local keeps a private reference to its refresh method
    # so the monkeypatch survives the call below (the fixture `db` is rebound
    # inside the function only after this point, never before).    # Patch the session's refresh so the first call (the post-commit refresh of
    # the newly created manifest) raises while the session otherwise still works.
    # Keep every reference through the same local `session` name so the monkeypatch
    # survives the call below.
    session = db
    real_refresh = session.refresh
    refresh_calls = {"n": 0}

    def refresh_fails(obj, *args, **kwargs):
        refresh_calls["n"] += 1
        if refresh_calls["n"] == 1:
            raise OperationalError("synthetic refresh failure", None, None)
        return real_refresh(obj, *args, **kwargs)

    session.refresh = refresh_fails

    run_id = "run-refresh-failed"
    with pytest.raises(OperationalError, match="synthetic refresh failure"):
        hdg.start_ingestion_run(session, dataset_keys=[key], run_id=run_id)

    # The original exception propagated (failure remains observable); the refresh
    # was attempted at least once (the post-commit refresh).
    assert refresh_calls["n"] >= 1


    # The manifest was committed, so read it back through an independent
    # session on the same engine.
    db2 = sessionmaker(bind=db.get_bind(), autocommit=False, autoflush=False)()
    try:
        manifest = db2.scalar(
            select(HistoricalIngestionRun).where(
                HistoricalIngestionRun.run_id == run_id
            )
        )
        # The manifest does not remain RUNNING -- the safe fallback terminalized
        # it as FAILED, because a committed manifest must never be left RUNNING
        # with no owner and no terminal error.
        assert manifest is not None, "manifest must exist after commit"
        assert manifest.status == hdg.RUN_FAILED, (
            "manifest must be terminal FAILED, not %s" % (manifest.status,)
        )
        assert manifest.completed_at is not None
        assert "post-commit refresh failed" in (manifest.error_message or "")

        # No manifest is left RUNNING.
        assert db2.scalar(
            select(func.count()).select_from(HistoricalIngestionRun).where(
                HistoricalIngestionRun.status == "RUNNING"
            )
        ) == 0, "no manifest may remain RUNNING"
    finally:
        db2.close()



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


# --------------------------------------------------------------------------
# Finding 3 — historical metric derivation must be snapshot-stable
# --------------------------------------------------------------------------


def test_refresh_uses_run_snapshot_not_current_catalog(db):
    """Finding 3 regression: ``refresh_ingestion_run_metrics`` must interpret
    a run's evidence using the dataset-mapping the run snapshotted at start,
    not the current catalog state.

    A run is created with mapping A (dataset X -> pipeline p-X, operation op-X).
    The catalog mapping for X is then changed to B (pipeline p-Y, operation op-Y),
    and a new run is created that snapshots mapping B. Refreshing the FIRST run
    must still use mapping A, while the SECOND run uses mapping B.
    """
    key_x = "UPSTOX_OPTION_CANDLES_3MIN"
    key_y = "UPSTOX_NIFTY_CANDLES_3MIN"

    # Mapping A: X -> backfill_options / option_candles
    _catalog(
        db,
        key=key_x,
        pipeline="backfill_options",
        completeness_data_type="option_candles",
    )
    _catalog(
        db,
        key=key_y,
        pipeline="backfill_nifty",
        completeness_data_type="nifty_candles",
    )

    run_a = hdg.start_ingestion_run(
        db, dataset_keys=[key_x, key_y], run_id="run-mapping-a"
    )

    # Add run-A evidence: one option_candles checkpoint+log (for key_x) and one
    # nifty_candles log (for key_y, no checkpoint).
    db.add_all(
        [
            IngestionCheckpoint(
                pipeline="backfill_options",
                instrument_key="NSE_FO|A|01-10-2026",
                run_id=run_a.run_id,
                status="COMPLETED",
                items_processed=10,
                items_total=10,
            ),
            IngestionLog(
                run_id=run_a.run_id,
                operation="option_candles",
                started_at="2026-09-01T00:00:00+00:00",
                status="SUCCESS",
                rows_fetched=10,
                rows_inserted=10,
            ),
            IngestionLog(
                run_id=run_a.run_id,
                operation="nifty_candles",
                started_at="2026-09-01T00:00:00+00:00",
                status="SUCCESS",
                rows_fetched=5,
                rows_inserted=5,
            ),
        ]
    )
    db.commit()

    refreshed_a = hdg.refresh_ingestion_run_metrics(db, run_a.run_id)
    assert refreshed_a.expected_records == 10
    assert refreshed_a.actual_records == 10
    assert refreshed_a.missing_records == 0
    assert refreshed_a.completeness_status == "COMPLETE"

    # Mutate the catalog AFTER run A started: change X's mapping to B
    # (pipeline backfill_nifty, operation nifty_candles) and deactivate Y.
    row_x = db.scalar(
        select(HistoricalDatasetGovernance).where(
            HistoricalDatasetGovernance.dataset_key == key_x
        )
    )
    row_x.pipeline = "backfill_nifty"
    row_x.completeness_data_type = "nifty_candles"
    row_x.table_name = "nifty_candles"
    row_y = db.scalar(
        select(HistoricalDatasetGovernance).where(
            HistoricalDatasetGovernance.dataset_key == key_y
        )
    )
    row_y.active = False
    db.commit()

    # Refresh run A again: must STILL use mapping A (option_candles), so the
    # nifty_candles log rows for run A are NOT measured against X's expectation,
    # and X's expectation (10) is still matched by the option_candles actual (10).
    refreshed_a_again = hdg.refresh_ingestion_run_metrics(db, run_a.run_id)
    assert refreshed_a_again.expected_records == 10
    assert refreshed_a_again.actual_records == 10
    assert refreshed_a_again.missing_records == 0
    assert refreshed_a_again.completeness_status == "COMPLETE"

    # A deactivated catalog row must not break refresh of an existing run.
    assert hdg.get_dataset(db, key_x).active is True  # X still active

    # A NEW run sees the current catalog mapping: X now maps to nifty_candles.
    run_b = hdg.start_ingestion_run(
        db, dataset_keys=[key_x], run_id="run-mapping-b"
    )
    db.add_all(
        [
            IngestionCheckpoint(
                pipeline="backfill_nifty",
                instrument_key="NSE_INDEX|NIFTY 50|B|01-10-2026",
                run_id=run_b.run_id,
                status="COMPLETED",
                items_processed=7,
                items_total=7,
            ),
            IngestionLog(
                run_id=run_b.run_id,
                operation="nifty_candles",
                started_at="2026-09-01T00:00:00+00:00",
                status="SUCCESS",
                rows_fetched=7,
                rows_inserted=7,
            ),
        ]
    )
    db.commit()

    refreshed_b = hdg.refresh_ingestion_run_metrics(db, run_b.run_id)
    assert refreshed_b.expected_records == 7
    assert refreshed_b.actual_records == 7
    assert refreshed_b.completeness_status == "COMPLETE"

    # Existing source/entitlement/policy snapshots on run A are unchanged.
    snapshot_a = json.loads(run_a.source_snapshot_json)
    assert snapshot_a[key_x]["source"] == "UPSTOX"

    policy_a = json.loads(run_a.policy_snapshot_json)
    assert policy_a[key_x]["dataset_tier"] == "RAW"


def test_deactivated_catalog_row_does_not_break_existing_run_refresh(db):
    """Finding 3 companion: deactivating a catalog row must not make refresh
    of an existing run fail, because the run snapshots its mapping at start.
    """
    key = "UPSTOX_OPTION_CANDLES_3MIN"
    _catalog(db, key=key, pipeline="backfill_options", completeness_data_type="option_candles")

    run = hdg.start_ingestion_run(
        db, dataset_keys=[key], run_id="run-deactivated"
    )
    db.add_all(
        [
            IngestionCheckpoint(
                pipeline="backfill_options",
                instrument_key="NSE_FO|DEACT|01-10-2026",
                run_id=run.run_id,
                status="COMPLETED",
                items_processed=4,
                items_total=4,
            ),
            IngestionLog(
                run_id=run.run_id,
                operation="option_candles",
                started_at="2026-09-01T00:00:00+00:00",
                status="SUCCESS",
                rows_fetched=4,
                rows_inserted=4,
            ),
        ]
    )
    db.commit()

    assert hdg.refresh_ingestion_run_metrics(db, run.run_id).completeness_status == "COMPLETE"

    # Deactivate the catalog row.
    row = db.scalar(
        select(HistoricalDatasetGovernance).where(
            HistoricalDatasetGovernance.dataset_key == key
        )
    )
    row.active = False
    db.commit()

    # Refresh must still work using the run's snapshot.
    refreshed = hdg.refresh_ingestion_run_metrics(db, run.run_id)
    assert refreshed.completeness_status == "COMPLETE"
    assert refreshed.expected_records == 4
    assert refreshed.actual_records == 4

    # A new run that tries to use the deactivated key must fail (current catalog).
    with pytest.raises(hdg.HistoricalDataGovernanceError, match="inactive historical dataset"):
        hdg.start_ingestion_run(db, dataset_keys=[key], run_id="run-after-deactivate")


def test_legacy_run_without_mapping_snapshot_fails_closed(db):
    """Finding 3 existing-data semantics: a run whose
    ``dataset_mapping_snapshot_json`` is empty (the migration's
    ``server_default="{}"`` for rows created before the snapshot field
    existed) must fail refresh with a clear governance error rather than
    silently falling back to the mutable current catalog.

    The current catalog here holds a VALID mapping, so a catalog fallback
    would silently succeed; the run must fail anyway — fabricated or
    retroactively reinterpreted mappings are the failure mode Finding 3
    exists to prevent. The run stays readable and terminalizable through the
    metrics-free fallback path.
    """
    key = "UPSTOX_OPTION_CANDLES_3MIN"
    # Valid, active current catalog mapping: a fallback to the catalog would
    # succeed — which is exactly what must not happen.
    _catalog(
        db,
        key=key,
        pipeline="backfill_options",
        completeness_data_type="option_candles",
    )

    run = hdg.start_ingestion_run(db, dataset_keys=[key], run_id="run-legacy")
    db.add_all(
        [
            IngestionCheckpoint(
                pipeline="backfill_options",
                instrument_key="NSE_FO|LEGACY|01-10-2026",
                run_id=run.run_id,
                status="COMPLETED",
                items_processed=4,
                items_total=4,
            ),
            IngestionLog(
                run_id=run.run_id,
                operation="option_candles",
                started_at="2026-09-01T00:00:00+00:00",
                status="SUCCESS",
                rows_fetched=4,
                rows_inserted=4,
            ),
        ]
    )
    db.commit()

    # Simulate a pre-snapshot legacy row: the column exists (migration
    # applied) but holds only the server default.
    run.dataset_mapping_snapshot_json = "{}"
    db.commit()
    with pytest.raises(
        hdg.HistoricalDataGovernanceError, match="missing dataset mapping"
    ):
        hdg.refresh_ingestion_run_metrics(db, run.run_id)

    # A blank value (no JSON written at all) fails the same way.
    run.dataset_mapping_snapshot_json = ""
    db.commit()
    with pytest.raises(
        hdg.HistoricalDataGovernanceError, match="missing dataset mapping"
    ):
        hdg.refresh_ingestion_run_metrics(db, run.run_id)

    # The run remains readable and terminalizable: the metrics-free fallback
    # (force_terminal_ingestion_run) never needs the snapshot, so a legacy
    # manifest can always be closed out instead of being stranded RUNNING.
    forced = hdg.force_terminal_ingestion_run(
        db,
        run.run_id,
        status=hdg.RUN_FAILED,
        error_message="legacy run closed without metric refresh",
    )
    assert forced.status == hdg.RUN_FAILED


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


def test_expected_and_actual_records_describe_the_same_population(db):
    """``expected_records`` and ``actual_records`` must be comparable.

    Only the options stage declares a checkpoint-backed expectation, so only
    its log rows belong in the expected/actual population. Contract-metadata
    and NIFTY rows are produced by operations that declare no expectation at
    all; counting them would report a surplus (an actual of 80 + 40 + 75
    against an expected 90) and hide the real 10-row shortfall.
    """
    contracts = "UPSTOX_CONTRACT_SPECS"
    nifty = "UPSTOX_NIFTY_CANDLES_3MIN"
    options = "UPSTOX_OPTION_CANDLES_3MIN"
    _catalog(
        db,
        key=contracts,
        table_name="contract_specs",
        pipeline="backfill_contracts",
        completeness_data_type="contract_metadata",
    )
    _catalog(
        db,
        key=nifty,
        pipeline="backfill_nifty",
        completeness_data_type="nifty_candles",
    )
    _catalog(db, key=options)

    run = hdg.start_ingestion_run(
        db,
        dataset_keys=[contracts, nifty, options],
        run_id="run-realistic",
    )
    db.add_all(
        [
            # Neither of these stages writes a checkpoint, so neither declares
            # an expectation.
            IngestionLog(
                run_id=run.run_id,
                operation="contract_metadata",
                started_at="2026-09-01T00:00:00+00:00",
                completed_at="2026-09-01T00:00:30+00:00",
                status="SUCCESS",
                rows_fetched=40,
                rows_inserted=40,
            ),
            IngestionLog(
                run_id=run.run_id,
                operation="nifty_candles",
                started_at="2026-09-01T00:00:30+00:00",
                completed_at="2026-09-01T00:01:00+00:00",
                status="SUCCESS",
                rows_fetched=75,
                rows_inserted=75,
            ),
            # The option stage is the checkpoint-backed one: it fetched 80 of
            # the 90 rows it declared.
            IngestionLog(
                run_id=run.run_id,
                operation="option_candles",
                started_at="2026-09-01T00:01:00+00:00",
                completed_at="2026-09-01T00:05:00+00:00",
                status="PARTIAL",
                rows_fetched=80,
                rows_inserted=80,
                error_message="429 rate limit",
            ),
            IngestionCheckpoint(
                pipeline="backfill_options",
                instrument_key="NSE_FO|X|01-10-2026",
                run_id=run.run_id,
                status="FAILED",
                items_processed=80,
                items_total=90,
                error_message="429 rate limit",
            ),
        ]
    )
    db.commit()

    refreshed = hdg.refresh_ingestion_run_metrics(db, run.run_id)
    # Expected is declared by the checkpointed options pipeline.
    assert refreshed.expected_records == 90
    # Actual is what that same pipeline's operation fetched. The contract (40)
    # and NIFTY (75) rows are outside the population, so this is 80 and not
    # 195.
    assert refreshed.actual_records == 80
    assert refreshed.missing_records == 10
    assert refreshed.checkpoints_total == 1
    assert refreshed.checkpoints_completed == 0
    # The PARTIAL option operation and the FAILED checkpoint are both real
    # run-scoped evidence, so the run is not whole.
    assert refreshed.completeness_status == "PARTIAL"

    finished = hdg.finish_ingestion_run(db, run.run_id, status=hdg.RUN_SUCCEEDED)
    assert finished.status == hdg.RUN_PARTIAL
    assert finished.completed_at is not None


def test_run_without_checkpoint_backed_evidence_declares_no_expectation(db):
    """A run whose operations declare no expectation has no population.

    Contract acquisition writes log rows but no checkpoint, so there is nothing
    to measure them against: expected stays None, actual stays 0, and no
    shortfall is invented. Its operations still decide completeness.
    """
    key = "UPSTOX_CONTRACT_SPECS"
    _catalog(
        db,
        key=key,
        table_name="contract_specs",
        pipeline="backfill_contracts",
        completeness_data_type="contract_metadata",
    )

    run = hdg.start_ingestion_run(db, dataset_keys=[key], run_id="run-contracts-only")
    db.add_all(
        [
            IngestionLog(
                run_id=run.run_id,
                operation="contract_metadata",
                started_at="2026-09-01T00:00:00+00:00",
                completed_at="2026-09-01T00:00:30+00:00",
                status="SUCCESS",
                rows_fetched=40,
                rows_inserted=40,
            ),
        ]
    )
    db.commit()

    refreshed = hdg.refresh_ingestion_run_metrics(db, run.run_id)
    assert refreshed.expected_records is None
    # The 40 rows it fetched belong to no declared expectation, so they are
    # not an actual of 40.
    assert refreshed.actual_records == 0
    assert refreshed.missing_records == 0
    assert refreshed.checkpoints_total == 0
    # The operation is itself run-scoped evidence, and it succeeded.
    assert refreshed.completeness_status == "COMPLETE"

    finished = hdg.finish_ingestion_run(db, run.run_id, status=hdg.RUN_SUCCEEDED)
    assert finished.status == hdg.RUN_SUCCEEDED


def test_missing_records_are_the_unprocessed_remainder(db):
    """A partially processed checkpoint is short by what it never processed.

    items_total=90 with items_processed=80 means 80 rows were obtained and 10
    were not, so missing_records is 10. Summing the declared total for an
    incomplete checkpoint would report 90 missing and claim the 80 rows that
    were actually retrieved never happened.
    """
    key = "UPSTOX_OPTION_CANDLES_3MIN"
    _catalog(db, key=key)

    partial = hdg.start_ingestion_run(
        db, dataset_keys=[key], run_id="run-partial-progress"
    )
    db.add_all(
        [
            IngestionCheckpoint(
                pipeline="backfill_options",
                instrument_key="NSE_FO|PARTIAL|01-10-2026",
                run_id=partial.run_id,
                status="FAILED",
                items_processed=80,
                items_total=90,
                error_message="429 rate limit",
            ),
            IngestionLog(
                run_id=partial.run_id,
                operation="option_candles",
                started_at="2026-09-01T00:00:00+00:00",
                status="PARTIAL",
                rows_fetched=80,
                rows_inserted=80,
                error_message="429 rate limit",
            ),
        ]
    )
    db.commit()

    refreshed = hdg.refresh_ingestion_run_metrics(db, partial.run_id)
    # The producer declared 90 and got through 80.
    assert refreshed.expected_records == 90
    # Actual stays consistent with what the ingestion log retrieved.
    assert refreshed.actual_records == 80
    # Missing is the unprocessed remainder (90 - 80), not the declared total.
    assert refreshed.missing_records == 10
    assert refreshed.completeness_status == "PARTIAL"

    finished = hdg.finish_ingestion_run(
        db, partial.run_id, status=hdg.RUN_SUCCEEDED
    )
    assert finished.status == hdg.RUN_PARTIAL
    assert finished.missing_records == 10
    assert finished.completed_at is not None

    # A completed checkpoint contributes its declared total but zero missing,
    # and the failed run above cannot contaminate it.
    complete = hdg.start_ingestion_run(
        db, dataset_keys=[key], run_id="run-complete-progress"
    )
    db.add_all(
        [
            IngestionCheckpoint(
                pipeline="backfill_options",
                instrument_key="NSE_FO|DONE|01-10-2026",
                run_id=complete.run_id,
                status="COMPLETED",
                items_processed=90,
                items_total=90,
            ),
            IngestionLog(
                run_id=complete.run_id,
                operation="option_candles",
                started_at="2026-09-02T00:00:00+00:00",
                status="SUCCESS",
                rows_fetched=90,
                rows_inserted=90,
            ),
        ]
    )
    db.commit()

    ok = hdg.finish_ingestion_run(db, complete.run_id, status=hdg.RUN_SUCCEEDED)
    assert ok.expected_records == 90
    assert ok.actual_records == 90
    assert ok.missing_records == 0
    assert ok.completeness_status == "COMPLETE"
    assert ok.status == hdg.RUN_SUCCEEDED


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


# --------------------------------------------------------------------------
# Day 48 Finding 7 — production-backed missing_records evidence
# --------------------------------------------------------------------------


def _seed_option_spec(db, instrument_key: str, *, expiry: str = "2026-07-28"):
    db.add(
        ContractSpec(
            instrument_key=instrument_key,
            underlying="NIFTY",
            underlying_key="NSE_FO|58124",
            expiry=expiry,
            strike_price=25000.0,
            instrument_type="CE",
            trading_symbol="NIFTY",
            segment="NSE_FO",
            exchange="NSE",
            source="TEST",
            source_reference="test",
            fetched_at=datetime(2026, 9, 1),
        )
    )
    db.commit()


def _raw_candle(timestamp: str) -> list:
    """A structurally valid raw Upstox option candle row."""
    return [timestamp, 150.0, 155.0, 148.0, 152.0, 5000, 325000]


def test_production_partial_checkpoint_declares_genuine_expectation(
    db, monkeypatch
):
    """Finding 7 — the REAL option-ingestion path must be able to produce
    the evidence ``missing_records`` measures.

    Through ``BackfillOrchestrator.run_options`` (the production writer of
    ``IngestionCheckpoint``):

    * instrument A fetches 5 raw rows, persists 4 (one is structurally
      invalid), then fails AFTER persistence (an injected limiter
      completion failure) — its FAILED checkpoint declares
      ``items_total=5`` / ``items_processed=4``;
    * instrument B fails AT the fetch and declares nothing (0/0): no
      expectation is invented for it.

    The run-scoped metrics must then read expected=5 (A's declaration and
    nothing else), actual=5 (this run's ``option_candles`` rows only), and
    missing=1 (5 - 4) — non-zero even though the declaring checkpoint is
    incomplete. Contract/NIFTY log rows from the same run and a foreign
    run's checkpoint must not move any of those numbers. On a retry run,
    neither the prior run's declaration nor a fabricated expectation may
    leak in.
    """
    import asyncio

    from app.services.backfill_orchestrator import (
        PIPELINE_OPTIONS,
        BackfillOrchestrator,
    )

    contracts = "UPSTOX_CONTRACT_SPECS"
    nifty = "UPSTOX_NIFTY_CANDLES_3MIN"
    options = "UPSTOX_OPTION_CANDLES_3MIN"
    _catalog(
        db,
        key=contracts,
        table_name="contract_specs",
        pipeline="backfill_contracts",
        completeness_data_type="contract_metadata",
    )
    _catalog(
        db,
        key=nifty,
        pipeline="backfill_nifty",
        completeness_data_type="nifty_candles",
    )
    _catalog(db, key=options)

    key_a = "NSE_FO|63935|28-07-2026"
    key_b = "NSE_FO|63936|28-07-2026"
    _seed_option_spec(db, key_a)
    _seed_option_spec(db, key_b, expiry="2026-07-28")

    class _Client:
        async def get_expired_historical_candles(
            self, instrument_key, interval, start, end
        ):
            if instrument_key == key_b:
                raise RuntimeError("synthetic fetch failure")
            # 5 raw rows; the last is structurally invalid and is dropped by
            # normalize_option_candles, so only 4 can ever be persisted.
            return [
                _raw_candle("2026-07-28T09:15:00+05:30"),
                _raw_candle("2026-07-28T09:18:00+05:30"),
                _raw_candle("2026-07-28T09:21:00+05:30"),
                _raw_candle("2026-07-28T09:24:00+05:30"),
                ["2026-07-28T09:27:00+05:30"],
            ]

    orchestrator = BackfillOrchestrator(db, _Client())

    # Smallest realistic post-persistence failure seam: the instrument has
    # fetched AND persisted its rows when the rate-limiter completion hook
    # raises. The production failure handler must record what this attempt
    # genuinely knows instead of zeroing the declaration.
    async def _boom():
        raise RuntimeError("synthetic completion write failure")

    monkeypatch.setattr(
        orchestrator._rate_limiter, "mark_instrument_done", _boom
    )

    dataset_keys = [contracts, nifty, options]
    run = hdg.start_ingestion_run(db, dataset_keys=dataset_keys, run_id="run-prod-partial")
    # Same wiring background_jobs applies: the orchestrator adopts the
    # governance run identity so its checkpoints are run-scoped evidence.
    orchestrator.run_id = run.run_id
    result = asyncio.run(orchestrator.run_options())
    assert result.status == "PARTIAL"

    # Same-run rows from the stages this test did not execute through their
    # own paths; they are legitimate run-scoped log rows a mixed run would
    # have, and must stay outside the checkpoint-backed population.
    db.add_all(
        [
            IngestionLog(
                run_id=run.run_id,
                operation="contract_metadata",
                started_at="2026-09-01T00:00:00+00:00",
                status="SUCCESS",
                rows_fetched=40,
                rows_inserted=40,
            ),
            IngestionLog(
                run_id=run.run_id,
                operation="nifty_candles",
                started_at="2026-09-01T00:00:30+00:00",
                status="SUCCESS",
                rows_fetched=75,
                rows_inserted=75,
            ),
            # A foreign run's checkpoint: run-scoped evidence must exclude it.
            IngestionCheckpoint(
                pipeline=PIPELINE_OPTIONS,
                instrument_key="NSE_FO|OTHER|28-07-2026",
                run_id="run-other",
                status="FAILED",
                items_processed=0,
                items_total=999,
                error_message="another acquisition",
            ),
        ]
    )
    db.commit()

    cp_a = db.scalar(
        select(IngestionCheckpoint).where(
            IngestionCheckpoint.run_id == run.run_id,
            IngestionCheckpoint.instrument_key == key_a,
        )
    )
    cp_b = db.scalar(
        select(IngestionCheckpoint).where(
            IngestionCheckpoint.run_id == run.run_id,
            IngestionCheckpoint.instrument_key == key_b,
        )
    )
    # Production wrote a genuine declared total on the incomplete checkpoint:
    # fetched 5, persisted 4. (Pre-fix production always wrote 0/0 here.)
    assert cp_a.status == "FAILED"
    assert cp_a.items_total == 5
    assert cp_a.items_processed == 4
    # The fetch-failed instrument knows no total and must declare none.
    assert cp_b.status == "FAILED"
    assert cp_b.items_total == 0
    assert cp_b.items_processed == 0

    refreshed = hdg.refresh_ingestion_run_metrics(db, run.run_id)
    # Only A declared an expectation; B and the foreign checkpoint contribute
    # nothing, and contract/NIFTY rows never enter the population.
    assert refreshed.expected_records == 5
    assert refreshed.actual_records == 5
    # The incomplete declaring checkpoint is short by its unprocessed row:
    # the metric does NOT collapse to zero merely because it is incomplete.
    assert refreshed.missing_records == 1
    assert refreshed.checkpoints_total == 2
    assert refreshed.checkpoints_completed == 0
    assert refreshed.completeness_status == "PARTIAL"

    finished = hdg.finish_ingestion_run(db, run.run_id, status=hdg.RUN_SUCCEEDED)
    assert finished.status == hdg.RUN_PARTIAL
    assert finished.expected_records == 5
    assert finished.missing_records == 1

    # --- idempotent retry: no inherited, invented, or leaked expectation ---
    # A now has candles and is skipped; B fails at the fetch again, so run 2
    # never learns a total and must declare none of its own.
    run2 = hdg.start_ingestion_run(db, dataset_keys=dataset_keys, run_id="run-prod-retry")
    orchestrator.run_id = run2.run_id
    asyncio.run(orchestrator.run_options())
    db.refresh(cp_a)
    db.refresh(cp_b)
    # Run 1's evidence is untouched by the retry.
    assert cp_a.run_id == run.run_id
    assert cp_a.items_total == 5
    assert cp_a.items_processed == 4
    # B's checkpoint now speaks for run 2, which declared nothing.
    assert cp_b.run_id == run2.run_id

    refreshed2 = hdg.refresh_ingestion_run_metrics(db, run2.run_id)
    assert refreshed2.expected_records is None
    assert refreshed2.actual_records == 0
    assert refreshed2.missing_records == 0

    # Re-reading run 1's stored metrics after the retry changes nothing.
    db.refresh(finished)
    assert finished.expected_records == 5
    assert finished.missing_records == 1


def test_fetch_failure_without_declaration_never_fabricates_expectation(db):
    """Finding 7 counterpart: when production never learns a total (every
    fetch fails before returning rows), the run declares no expectation and
    reports no invented shortfall — under-declaration stays the safe
    direction, and no zero is dressed up as a measured remainder."""
    import asyncio

    from app.services.backfill_orchestrator import BackfillOrchestrator

    options = "UPSTOX_OPTION_CANDLES_3MIN"
    _catalog(db, key=options)
    key_a = "NSE_FO|63935|28-07-2026"
    key_b = "NSE_FO|63936|28-07-2026"
    _seed_option_spec(db, key_a)
    _seed_option_spec(db, key_b)

    class _Client:
        async def get_expired_historical_candles(
            self, instrument_key, interval, start, end
        ):
            raise RuntimeError("synthetic fetch failure")

    orchestrator = BackfillOrchestrator(db, _Client())
    run = hdg.start_ingestion_run(db, dataset_keys=[options], run_id="run-fetch-fail")
    orchestrator.run_id = run.run_id
    asyncio.run(orchestrator.run_options())

    refreshed = hdg.refresh_ingestion_run_metrics(db, run.run_id)
    assert refreshed.expected_records is None
    assert refreshed.actual_records == 0
    assert refreshed.missing_records == 0
    assert refreshed.checkpoints_total == 2
    assert refreshed.completeness_status == "PARTIAL"

    finished = hdg.finish_ingestion_run(db, run.run_id, status=hdg.RUN_SUCCEEDED)
    assert finished.status == hdg.RUN_PARTIAL
    assert finished.expected_records is None
