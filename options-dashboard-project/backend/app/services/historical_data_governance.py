"""Day 48 historical data governance services.

The governance layer is deliberately separate from ingestion mechanics. It
catalogs historical datasets, snapshots policy at ingestion-run time, observes
existing checkpoint/completeness state, and enforces retention only through a
small static allow-list of known tables/columns.

No function in this module contacts production services or performs deletion
unless the caller explicitly opts into execution and the catalog itself has
retention_enforced=True.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    ContractSpec,
    DataCompleteness,
    HistoricalDatasetGovernance,
    HistoricalGexSnapshot,
    HistoricalIngestionRun,
    IngestionCheckpoint,
    IngestionLog,
    NiftyCandle,
    OptionCandle,
    OptionGreeks,
)

# Policy states are intentionally small and machine-checkable.
ENTITLEMENT_VERIFIED = "VERIFIED"
ENTITLEMENT_REVIEW_REQUIRED = "REVIEW_REQUIRED"
ENTITLEMENT_NOT_APPLICABLE = "NOT_APPLICABLE"

LICENSE_INTERNAL = "INTERNAL"
LICENSE_REVIEW_REQUIRED = "REVIEW_REQUIRED"

USAGE_INTERNAL_ONLY = "INTERNAL_ONLY"
USAGE_USER_SCOPED = "USER_SCOPED"
USAGE_PUBLIC = "PUBLIC"

REDISTRIBUTION_ALLOWED = "ALLOWED"
REDISTRIBUTION_REVIEW_REQUIRED = "REVIEW_REQUIRED"
REDISTRIBUTION_PROHIBITED = "PROHIBITED"

RETENTION_KEEP = "KEEP"
RETENTION_DELETE_AFTER_DAYS = "DELETE_AFTER_DAYS"
RETENTION_LEGAL_HOLD = "LEGAL_HOLD"

RUN_RUNNING = "RUNNING"
RUN_SUCCEEDED = "SUCCEEDED"
RUN_FAILED = "FAILED"
RUN_PARTIAL = "PARTIAL"

PURPOSE_INTERNAL_RESEARCH = "INTERNAL_RESEARCH"
PURPOSE_BACKTEST = "BACKTEST"
PURPOSE_PRIVATE_USER = "PRIVATE_USER"
PURPOSE_PUBLIC = "PUBLIC"

_ALLOWED_PURPOSES = {
    PURPOSE_INTERNAL_RESEARCH,
    PURPOSE_BACKTEST,
    PURPOSE_PRIVATE_USER,
    PURPOSE_PUBLIC,
}

_STAGE_DATASET_KEYS = {
    "contracts": "UPSTOX_CONTRACT_SPECS",
    "nifty": "UPSTOX_NIFTY_CANDLES_3MIN",
    "options": "UPSTOX_OPTION_CANDLES_3MIN",
}


class HistoricalDataGovernanceError(ValueError):
    """Raised when a governance contract cannot be satisfied safely."""


@dataclass(frozen=True)
class RetentionPlan:
    dataset_key: str
    tier: str
    policy: str
    cutoff: datetime | None
    candidate_rows: int
    executable: bool
    reason: str | None = None
    deleted_rows: int = 0


# IMPORTANT: retention execution never trusts the table name stored in the
# catalog to construct SQL dynamically. Each governed dataset must map to a
# known SQLAlchemy model and timestamp field here.
_RETENTION_TARGETS: dict[str, tuple[type[Any], str]] = {
    "UPSTOX_CONTRACT_SPECS": (ContractSpec, "fetched_at"),
    "UPSTOX_NIFTY_CANDLES_3MIN": (NiftyCandle, "open_time"),
    "UPSTOX_OPTION_CANDLES_3MIN": (OptionCandle, "open_time"),
    "STRIKENOVA_OPTION_GREEKS": (OptionGreeks, "open_time"),
    "STRIKENOVA_HISTORICAL_GEX": (HistoricalGexSnapshot, "open_time"),
}


def dataset_keys_for_stages(stages: list[str]) -> list[str]:
    """Return deterministic governance keys for validated ingestion stages."""
    if not isinstance(stages, list) or not stages:
        raise HistoricalDataGovernanceError("stages must be a non-empty list")
    keys: list[str] = []
    for stage in stages:
        key = _STAGE_DATASET_KEYS.get(stage)
        if key is None:
            raise HistoricalDataGovernanceError(f"unknown ingestion stage: {stage!r}")
        if key not in keys:
            keys.append(key)
    return keys


def get_dataset(db: Session, dataset_key: str) -> HistoricalDatasetGovernance:
    row = db.scalar(
        select(HistoricalDatasetGovernance).where(
            HistoricalDatasetGovernance.dataset_key == dataset_key,
            HistoricalDatasetGovernance.active.is_(True),
        )
    )
    if row is None:
        raise HistoricalDataGovernanceError(
            f"unknown or inactive historical dataset: {dataset_key}"
        )
    return row


def _load_dependencies(row: HistoricalDatasetGovernance) -> list[str]:
    try:
        value = json.loads(row.dependencies_json or "[]")
    except (TypeError, ValueError) as exc:
        raise HistoricalDataGovernanceError(
            f"invalid dependencies_json for {row.dataset_key}: {exc}"
        ) from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise HistoricalDataGovernanceError(
            f"dependencies_json for {row.dataset_key} must be a string list"
        )
    return value


def assert_entitlement_ready(
    db: Session,
    dataset_key: str,
    *,
    allow_review_required: bool = False,
) -> None:
    """Fail closed when a dataset's source entitlement is unresolved."""
    row = get_dataset(db, dataset_key)
    if row.entitlement_status == ENTITLEMENT_VERIFIED:
        return
    if allow_review_required and row.entitlement_status == ENTITLEMENT_REVIEW_REQUIRED:
        return
    if row.entitlement_status == ENTITLEMENT_NOT_APPLICABLE:
        return
    raise HistoricalDataGovernanceError(
        f"historical dataset {dataset_key} has unresolved source entitlement: "
        f"{row.entitlement_status}"
    )


def assert_redistribution_allowed(db: Session, dataset_key: str) -> None:
    """Fail closed unless redistribution rights are explicitly marked ALLOWED."""
    row = get_dataset(db, dataset_key)
    if row.redistribution_status != REDISTRIBUTION_ALLOWED:
        raise HistoricalDataGovernanceError(
            f"redistribution is not approved for {dataset_key}: "
            f"{row.redistribution_status}"
        )


def assert_usage_allowed(
    db: Session,
    dataset_key: str,
    *,
    purpose: str,
) -> None:
    """Enforce the dataset's recorded usage scope."""
    if purpose not in _ALLOWED_PURPOSES:
        raise HistoricalDataGovernanceError(f"unknown usage purpose: {purpose}")
    row = get_dataset(db, dataset_key)
    policy = row.usage_policy
    if policy == USAGE_PUBLIC:
        return
    if policy == USAGE_USER_SCOPED and purpose in {
        PURPOSE_INTERNAL_RESEARCH,
        PURPOSE_BACKTEST,
        PURPOSE_PRIVATE_USER,
    }:
        return
    if policy == USAGE_INTERNAL_ONLY and purpose in {
        PURPOSE_INTERNAL_RESEARCH,
        PURPOSE_BACKTEST,
    }:
        return
    raise HistoricalDataGovernanceError(
        f"usage purpose {purpose} is not permitted for {dataset_key}: {policy}"
    )


def assert_recomputation_safe(
    db: Session,
    dataset_key: str,
    *,
    _seen: set[str] | None = None,
) -> None:
    """Verify that a dataset is reconstructible from governed dependencies."""
    row = get_dataset(db, dataset_key)
    seen = set() if _seen is None else set(_seen)
    if dataset_key in seen:
        raise HistoricalDataGovernanceError(
            f"cyclic historical-data dependency detected at {dataset_key}"
        )
    seen.add(dataset_key)

    if row.recomputable is not True:
        raise HistoricalDataGovernanceError(
            f"dataset {dataset_key} is not marked recomputable"
        )

    if row.dataset_tier == "RAW":
        if row.raw_immutable is not True:
            raise HistoricalDataGovernanceError(
                f"raw dataset {dataset_key} is not marked immutable"
            )
        return

    dependencies = _load_dependencies(row)
    if not dependencies:
        raise HistoricalDataGovernanceError(
            f"derived dataset {dataset_key} has no governed dependencies"
        )
    for dependency in dependencies:
        assert_recomputation_safe(db, dependency, _seen=seen)


def _snapshot_policy(
    db: Session,
    dataset_keys: list[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    source: dict[str, Any] = {}
    entitlement: dict[str, Any] = {}
    policy: dict[str, Any] = {}

    for key in dataset_keys:
        row = get_dataset(db, key)
        source[key] = {
            "source": row.source,
            "source_reference": row.source_reference,
            "source_version": row.source_version,
        }
        entitlement[key] = {
            "requirement": row.entitlement_requirement,
            "status": row.entitlement_status,
            "license_status": row.license_status,
        }
        policy[key] = {
            "usage_policy": row.usage_policy,
            "redistribution_status": row.redistribution_status,
            "retention_policy": row.retention_policy,
            "retention_days": row.retention_days,
            "retention_enforced": row.retention_enforced,
            "dataset_tier": row.dataset_tier,
        }
    return source, entitlement, policy


def start_ingestion_run(
    db: Session,
    *,
    dataset_keys: list[str],
    background_job_id: str | None = None,
    purpose: str = PURPOSE_INTERNAL_RESEARCH,
    coverage_start: str | None = None,
    coverage_end: str | None = None,
    run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> HistoricalIngestionRun:
    """Create an auditable run manifest with immutable policy snapshots."""
    if not dataset_keys:
        raise HistoricalDataGovernanceError("at least one dataset key is required")
    if purpose not in _ALLOWED_PURPOSES:
        raise HistoricalDataGovernanceError(f"unknown usage purpose: {purpose}")

    unique_keys = list(dict.fromkeys(dataset_keys))
    for key in unique_keys:
        get_dataset(db, key)

    source, entitlement, policy = _snapshot_policy(db, unique_keys)
    manifest = HistoricalIngestionRun(
        run_id=run_id or __import__("uuid").uuid4().hex,
        background_job_id=background_job_id,
        dataset_keys_json=json.dumps(unique_keys, sort_keys=True),
        source_snapshot_json=json.dumps(source, sort_keys=True),
        entitlement_snapshot_json=json.dumps(entitlement, sort_keys=True),
        policy_snapshot_json=json.dumps(policy, sort_keys=True),
        purpose=purpose,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        status=RUN_RUNNING,
        metadata_json=json.dumps(metadata or {}, sort_keys=True),
    )
    db.add(manifest)
    db.commit()
    db.refresh(manifest)
    return manifest


def _run_dataset_keys(run: HistoricalIngestionRun) -> list[str]:
    try:
        keys = json.loads(run.dataset_keys_json or "[]")
    except (TypeError, ValueError) as exc:
        raise HistoricalDataGovernanceError(
            f"invalid dataset_keys_json for run {run.run_id}: {exc}"
        ) from exc
    if not isinstance(keys, list) or any(not isinstance(item, str) for item in keys):
        raise HistoricalDataGovernanceError(
            f"dataset_keys_json for run {run.run_id} must be a string list"
        )
    return keys


def refresh_ingestion_run_metrics(
    db: Session,
    run_id: str,
) -> HistoricalIngestionRun:
    """Snapshot existing checkpoints, completeness, and ingestion-log metrics."""
    run = db.scalar(
        select(HistoricalIngestionRun).where(HistoricalIngestionRun.run_id == run_id)
    )
    if run is None:
        raise HistoricalDataGovernanceError(f"unknown ingestion run: {run_id}")

    dataset_keys = _run_dataset_keys(run)
    pipelines: list[str] = []
    data_types: list[str] = []
    for key in dataset_keys:
        row = get_dataset(db, key)
        if row.pipeline:
            pipelines.append(row.pipeline)
        if row.completeness_data_type:
            data_types.append(row.completeness_data_type)

    checkpoint_total = 0
    checkpoint_completed = 0
    if pipelines:
        checkpoint_total = int(
            db.scalar(
                select(func.count(IngestionCheckpoint.id)).where(
                    IngestionCheckpoint.run_id == run_id,
                    IngestionCheckpoint.pipeline.in_(pipelines),
                )
            )
            or 0
        )
        checkpoint_completed = int(
            db.scalar(
                select(func.count(IngestionCheckpoint.id)).where(
                    IngestionCheckpoint.run_id == run_id,
                    IngestionCheckpoint.pipeline.in_(pipelines),
                    IngestionCheckpoint.status == "COMPLETED",
                )
            )
            or 0
        )

    completeness_rows = []
    if data_types:
        stmt = select(DataCompleteness).where(
            DataCompleteness.data_type.in_(data_types)
        )
        if run.coverage_start:
            stmt = stmt.where(DataCompleteness.session_date >= run.coverage_start)
        if run.coverage_end:
            stmt = stmt.where(DataCompleteness.session_date <= run.coverage_end)
        completeness_rows = list(db.scalars(stmt).all())

    expected = sum(
        int(row.expected_count or 0) for row in completeness_rows
    )
    missing = sum(int(row.missing_count or 0) for row in completeness_rows)

    fetched = db.scalar(
        select(func.coalesce(func.sum(IngestionLog.rows_fetched), 0)).where(
            IngestionLog.run_id == run_id
        )
    )
    actual = int(fetched or 0)
    if actual == 0 and completeness_rows:
        actual = sum(int(row.actual_count or 0) for row in completeness_rows)

    if not completeness_rows:
        completeness_status = "UNKNOWN"
    elif missing > 0:
        completeness_status = "PARTIAL"
    elif expected > 0:
        completeness_status = "COMPLETE"
    elif any(row.status == "UNAVAILABLE" for row in completeness_rows):
        completeness_status = "UNAVAILABLE"
    else:
        completeness_status = "UNKNOWN"

    run.expected_records = expected if completeness_rows else None
    run.actual_records = actual
    run.missing_records = missing
    run.checkpoints_total = checkpoint_total
    run.checkpoints_completed = checkpoint_completed
    run.completeness_status = completeness_status
    db.commit()
    db.refresh(run)
    return run


def finish_ingestion_run(
    db: Session,
    run_id: str,
    *,
    status: str,
    error_message: str | None = None,
) -> HistoricalIngestionRun:
    if status not in {RUN_SUCCEEDED, RUN_FAILED, RUN_PARTIAL}:
        raise HistoricalDataGovernanceError(f"invalid ingestion-run status: {status}")
    run = refresh_ingestion_run_metrics(db, run_id)
    run.status = status
    run.error_message = error_message
    if status == RUN_SUCCEEDED and run.completeness_status == "PARTIAL":
        run.status = RUN_PARTIAL
    run.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)
    return run


def plan_retention(
    db: Session,
    dataset_key: str,
    *,
    now: datetime | None = None,
) -> RetentionPlan:
    """Create a non-destructive retention plan for one governed dataset."""
    row = get_dataset(db, dataset_key)
    if row.retention_policy in {RETENTION_KEEP, RETENTION_LEGAL_HOLD}:
        return RetentionPlan(
            dataset_key=dataset_key,
            tier=row.dataset_tier,
            policy=row.retention_policy,
            cutoff=None,
            candidate_rows=0,
            executable=False,
            reason="retention policy does not permit deletion",
        )

    if row.retention_policy != RETENTION_DELETE_AFTER_DAYS:
        raise HistoricalDataGovernanceError(
            f"unsupported retention policy for {dataset_key}: {row.retention_policy}"
        )
    if row.retention_days is None or row.retention_days < 1:
        raise HistoricalDataGovernanceError(
            f"retention_days must be >= 1 for {dataset_key}"
        )
    if dataset_key not in _RETENTION_TARGETS:
        raise HistoricalDataGovernanceError(
            f"retention target is not allow-listed: {dataset_key}"
        )

    model, timestamp_name = _RETENTION_TARGETS[dataset_key]
    clock = now or datetime.now(timezone.utc)
    cutoff = clock - timedelta(days=row.retention_days)
    timestamp_column = getattr(model, timestamp_name)
    candidate_rows = int(
        db.scalar(select(func.count()).select_from(model).where(timestamp_column < cutoff))
        or 0
    )
    reason = None if row.retention_enforced else "catalog enforcement is disabled"
    return RetentionPlan(
        dataset_key=dataset_key,
        tier=row.dataset_tier,
        policy=row.retention_policy,
        cutoff=cutoff,
        candidate_rows=candidate_rows,
        executable=row.retention_enforced and row.dataset_tier != "RAW",
        reason=reason,
    )


def enforce_retention(
    db: Session,
    dataset_key: str,
    *,
    now: datetime | None = None,
    execute: bool = False,
) -> RetentionPlan:
    """Apply an allow-listed retention policy only on explicit opt-in."""
    plan = plan_retention(db, dataset_key, now=now)
    if not execute or not plan.executable:
        return plan

    row = get_dataset(db, dataset_key)
    if row.dataset_tier == "RAW":
        raise HistoricalDataGovernanceError(
            f"raw historical data cannot be deleted by the Day 48 retention service: "
            f"{dataset_key}"
        )
    if plan.cutoff is None:
        return plan

    model, timestamp_name = _RETENTION_TARGETS[dataset_key]
    timestamp_column = getattr(model, timestamp_name)
    result = db.execute(
        delete(model).where(timestamp_column < plan.cutoff)
    )
    db.commit()
    return RetentionPlan(
        dataset_key=plan.dataset_key,
        tier=plan.tier,
        policy=plan.policy,
        cutoff=plan.cutoff,
        candidate_rows=plan.candidate_rows,
        executable=plan.executable,
        reason=plan.reason,
        deleted_rows=int(result.rowcount or 0),
    )
