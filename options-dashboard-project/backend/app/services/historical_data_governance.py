"""Day 48 historical data governance services.

The governance layer is deliberately separate from ingestion mechanics. It
catalogs historical datasets, snapshots policy at ingestion-run time, derives
manifest metrics only from evidence a specific ingestion run produced, and
enforces retention only through a small static allow-list of known
tables/columns.

No function in this module contacts production services or performs deletion
unless the caller explicitly opts into execution and the catalog itself has
retention_enforced=True.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, delete, func, select
from sqlalchemy.orm import Session

import logging

logger = logging.getLogger(__name__)

from app.models import (
    ContractSpec,
    HistoricalDatasetGovernance,
    HistoricalGexSnapshot,
    HistoricalIngestionRun,
    IngestionCheckpoint,
    IngestionLog,
    NiftyCandle,
    OptionCandle,
    OptionGreeks,
)
from app.utils.market_time import to_ist_naive

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


def _utcnow_naive() -> datetime:
    """Naive UTC timestamp, matching the repository's storage convention.

    Historical tables use timezone-naive ``DateTime`` columns; writing or
    comparing with timezone-aware values breaks same-day boundaries on
    SQLite (lexicographic string comparison) and is session-TZ dependent
    on PostgreSQL, so every datetime this service stores or filters with
    is normalized to naive UTC.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive_utc(value: datetime) -> datetime:
    """Normalize an incoming datetime to naive UTC (idempotent for naive)."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


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
#
# The third element records the clock the *column actually stores*. The
# repository-wide convention (docs/PHASE_7_24_4_TIMEZONE_STANDARDIZATION.md)
# is that persisted market-data candle timestamps are naive IST, so every
# ``open_time`` target below is naive IST. ``ContractSpec.fetched_at`` is the
# one exception: it is written from ``datetime.now(timezone.utc)``. Comparing a
# naive-UTC cutoff against a naive-IST column is a 5h30 boundary error that can
# silently retain a deletable row or delete a retained one.
CLOCK_UTC = "UTC"
CLOCK_IST_NAIVE = "IST"

_RETENTION_TARGETS: dict[str, tuple[type[Any], str, str]] = {
    "UPSTOX_CONTRACT_SPECS": (ContractSpec, "fetched_at", CLOCK_UTC),
    "UPSTOX_NIFTY_CANDLES_3MIN": (NiftyCandle, "open_time", CLOCK_IST_NAIVE),
    "UPSTOX_OPTION_CANDLES_3MIN": (OptionCandle, "open_time", CLOCK_IST_NAIVE),
    "STRIKENOVA_OPTION_GREEKS": (OptionGreeks, "open_time", CLOCK_IST_NAIVE),
    "STRIKENOVA_HISTORICAL_GEX": (HistoricalGexSnapshot, "open_time", CLOCK_IST_NAIVE),
}

# Raw-data protection is a STATIC property of the target, recorded here next
# to the allow-list — never a property of the editable catalog row. The
# catalog's ``dataset_tier`` is governance metadata an operator can change;
# retiering a raw historical dataset to MODEL must not make its rows
# deletable. Only targets absent from this set (derived model/analytics
# output) can ever be executable, regardless of what the catalog says.
_RAW_RETENTION_TARGETS = frozenset(
    {
        "UPSTOX_CONTRACT_SPECS",
        "UPSTOX_NIFTY_CANDLES_3MIN",
        "UPSTOX_OPTION_CANDLES_3MIN",
    }
)


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


# Approved acquisition policy (DECISIONS.md, ADR-020). Unresolved source
# entitlement may be acquired ONLY for internal research or backtest, and
# only through the explicit ``allow_review_required`` escape hatch; the
# exception is recorded on the ingestion manifest rather than silently
# absorbed. Usage scope and redistribution rights stay fail-closed, and
# PRIVATE_USER / PUBLIC purposes never receive the exception.
ACQUISITION_REVIEW_REQUIRED_PURPOSES = frozenset(
    {PURPOSE_INTERNAL_RESEARCH, PURPOSE_BACKTEST}
)


@dataclass(frozen=True)
class AcquisitionPolicy:
    """Outcome of the acquisition gate, recorded on the manifest."""

    purpose: str
    entitlement_review_required: tuple[str, ...]
    redistribution_review_required: tuple[str, ...]


def assert_acquisition_allowed(
    db: Session,
    dataset_keys: list[str],
    *,
    purpose: str,
) -> AcquisitionPolicy:
    """Gate historical acquisition on the rights the catalog records.

    This is the enforcement point the durable ``HISTORICAL_INGESTION`` path
    calls before it acquires anything, so an unresolved or unauthorized
    catalog row fails the job instead of silently producing data.

    Acquiring data for internal analysis is not redistribution, so the
    redistribution gate blocks only when the declared purpose can publish
    (PUBLIC). Any dataset whose redistribution rights are unresolved is
    reported back so the caller can record it on the manifest.
    """
    if purpose not in _ALLOWED_PURPOSES:
        raise HistoricalDataGovernanceError(f"unknown usage purpose: {purpose}")
    if not dataset_keys:
        raise HistoricalDataGovernanceError("at least one dataset key is required")

    allow_review_required = purpose in ACQUISITION_REVIEW_REQUIRED_PURPOSES
    redistribution_gated = purpose == PURPOSE_PUBLIC

    review_required: list[str] = []
    redistribution_pending: list[str] = []
    for key in dict.fromkeys(dataset_keys):
        row = get_dataset(db, key)
        assert_entitlement_ready(
            db, key, allow_review_required=allow_review_required
        )
        if row.entitlement_status == ENTITLEMENT_REVIEW_REQUIRED:
            review_required.append(key)
        assert_usage_allowed(db, key, purpose=purpose)
        if row.redistribution_status != REDISTRIBUTION_ALLOWED:
            redistribution_pending.append(key)
            if redistribution_gated:
                assert_redistribution_allowed(db, key)

    return AcquisitionPolicy(
        purpose=purpose,
        entitlement_review_required=tuple(review_required),
        redistribution_review_required=tuple(redistribution_pending),
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
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Snapshot the immutable dataset-mapping a run will be interpreted against.

    The run-scoped metric derivation in
    :func:`refresh_ingestion_run_metrics` must be stable for the lifetime of a
    run: a catalog edit after the run begins (including deactivating a row) must
    not change how the run's evidence is interpreted, and deactivating a row
    must not break refresh of an existing run.

    The snapshot therefore records the minimum immutable mapping needed to
    reconstruct the per-dataset ``pipeline`` and ``completeness_data_type``
    assignments at run start (the two fields that tell refresh which ingestion
    operations and checkpoints belong to which dataset). The source/entitlement
    and usage/redistribution/retention policy snapshots are extended to include
    ``dataset_tier`` so the retention path can also be validated against the
    snapshot rather than a later catalog edit.
    """
    source: dict[str, Any] = {}
    entitlement: dict[str, Any] = {}
    policy: dict[str, Any] = {}
    dataset_mapping: dict[str, Any] = {}

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
        dataset_mapping[key] = {
            "pipeline": row.pipeline,
            "completeness_data_type": row.completeness_data_type,
            "dataset_tier": row.dataset_tier,
        }
    return source, entitlement, policy, dataset_mapping


def recover_abandoned_manifest(
    db: Session,
    background_job_id: str,
    *,
    worker_id: str | None = None,
    now: datetime | None = None,
) -> int:
    """Terminalize a crashed attempt's manifest when ownership transfers.

    When a durable ``HISTORICAL_INGESTION`` job's lease expires and another
    worker reclaims it, the new attempt must not coexist with the old attempt's
    still-RUNNING manifest. This helper is called by the durable execution path
    AFTER the new worker has claimed ownership and BEFORE it creates its own
    manifest, so the old attempt's manifest is terminalized as FAILED (abandoned)
    and the replacement attempt becomes the sole RUNNING manifest for the job.

    A stale worker whose lease has expired (or whose job was reclaimed) is never
    allowed to touch manifests: ownership is verified with the same conditional
    lease check ``complete_job`` uses, and unverified callers are no-ops.

    The old manifests are identified by ``started_at < now`` so the manifest the
    replacement attempt is about to create (whose ``started_at`` will be ``>= now``)
    is never touched.
    """
    if not worker_id:
        return 0
    now = now or _utcnow_naive()

    # Ownership check: the same conditional lease contract ``complete_job`` uses.
    # If this worker no longer owns the job, it must not touch any manifests.
    from app.models import BackgroundJob, JobStatus

    still_owned = db.scalar(
        select(BackgroundJob.id).where(
            BackgroundJob.id == background_job_id,
            BackgroundJob.status == JobStatus.RUNNING.value,
            BackgroundJob.lease_owner == worker_id,
            BackgroundJob.lease_expires_at.isnot(None),
            BackgroundJob.lease_expires_at > now,
        )
    )
    if still_owned is None:
        return 0

    from sqlalchemy import update as _sqlalchemy_update

    result = db.execute(
        _sqlalchemy_update(HistoricalIngestionRun)
        .where(HistoricalIngestionRun.background_job_id == background_job_id)
        .where(HistoricalIngestionRun.status == RUN_RUNNING)
        .where(HistoricalIngestionRun.started_at < now)
        .values(
            status=RUN_FAILED,
            error_message="abandoned by worker recovery: previous attempt did not finalize",
            completed_at=now,
        )
    )
    db.commit()
    return result.rowcount or 0


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
    """Create an auditable run manifest with immutable policy snapshots.

    The returned run id is the immutable identity of the manifest and is
    available even when the post-commit refresh fails (see the refresh-error
    handling in this function).

    When called by the durable ``HISTORICAL_INGESTION`` path with a
    ``background_job_id``, the caller is responsible for calling
    :func:`recover_abandoned_manifest` BEFORE this function so a crashed
    attempt's manifest is terminalized before the replacement attempt creates
    its own manifest.
    """
    if not dataset_keys:
        raise HistoricalDataGovernanceError("at least one dataset key is required")
    if purpose not in _ALLOWED_PURPOSES:
        raise HistoricalDataGovernanceError(f"unknown usage purpose: {purpose}")

    unique_keys = list(dict.fromkeys(dataset_keys))
    for key in unique_keys:
        get_dataset(db, key)

    source, entitlement, policy, dataset_mapping = _snapshot_policy(db, unique_keys)
    manifest = HistoricalIngestionRun(
        run_id=run_id or uuid4().hex,
        background_job_id=background_job_id,
        dataset_keys_json=json.dumps(unique_keys, sort_keys=True),
        source_snapshot_json=json.dumps(source, sort_keys=True),
        entitlement_snapshot_json=json.dumps(entitlement, sort_keys=True),
        policy_snapshot_json=json.dumps(policy, sort_keys=True),
        dataset_mapping_snapshot_json=json.dumps(dataset_mapping, sort_keys=True),
        purpose=purpose,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        status=RUN_RUNNING,
        metadata_json=json.dumps(metadata or {}, sort_keys=True),
    )
    db.add(manifest)
    db.commit()
    # The run id is the immutable identity of this manifest. Keep it
    # available independently of the post-commit refresh below, so a
    # refresh failure cannot strand a committed RUNNING manifest and so
    # the caller never depends on ORM state to know which run was created.
    manifest_run_id = manifest.run_id
    try:
        db.refresh(manifest)
    except Exception:
        logger.exception(
            "historical ingestion manifest %s committed RUNNING but the "
            "post-commit refresh failed; terminalizing as failed instead of "
            "leaving it RUNNING",
            manifest_run_id,
        )
        try:
            force_terminal_ingestion_run(
                db,
                manifest_run_id,
                status=RUN_FAILED,
                error_message="historical ingestion manifest committed RUNNING but post-commit refresh failed",
            )
        except Exception:
            logger.exception(
                "could not terminalize historical ingestion manifest %s "
                "after a refresh failure; the manifest may be stranded RUNNING",
                manifest_run_id,
            )
        raise
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
    """Derive manifest metrics from evidence this run produced.

    Every count below is scoped to ``run_id``. ``DataCompleteness`` is
    deliberately NOT consulted: it is a cumulative table keyed on
    ``(instrument_key, session_date, data_type)`` with no run identity, so its
    rows describe the table's current state rather than any single
    acquisition. Aggregating it by dataset and date window let an unrelated
    historical run's rows downgrade this run to ``PARTIAL``.

    The authoritative run-scoped evidence is what the ingestion pipeline
    already writes:

    * ``IngestionLog`` — one append-only row per acquisition operation,
      carrying the run id, the operation name, its terminal status and the
      rows it retrieved.
    * ``IngestionCheckpoint`` — instrument-level progress carrying the run
      id, the declared item total, and completion state.

    A run that produced neither stays ``UNKNOWN`` rather than being reported
    complete.

    ``expected_records`` and ``actual_records`` describe the SAME population:
    the work covered by this run's checkpoints. ``expected_records`` is the
    declared ``items_total`` of the run's checkpoint-backed pipelines, and
    ``actual_records`` is the ``rows_fetched`` reported by the IngestionLog
    operations that publish exactly those pipelines. Rows fetched by an
    operation with no checkpoint-backed expectation (contract metadata, NIFTY
    candles) are excluded, because measuring them against an expectation they
    were never part of would report a surplus rather than a shortfall. Those
    operations still count towards ``completeness_status``.
    """
    run = db.scalar(
        select(HistoricalIngestionRun).where(HistoricalIngestionRun.run_id == run_id)
    )
    if run is None:
        raise HistoricalDataGovernanceError(f"unknown ingestion run: {run_id}")

    dataset_keys = _run_dataset_keys(run)

    # Historical metric interpretation must be stable for the lifetime of a run.
    # The run snapshots the immutable dataset-mapping at run start (pipeline +
    # completeness_data_type per dataset), so a catalog edit after the run begins
    # (including deactivating a row) cannot alter how this run's evidence is
    # interpreted, and deactivating a row cannot break refresh of an existing run.
    # A NEW run will snapshot the then-current catalog, so it sees any later
    # mapping; an existing run keeps the mapping it started with.
    mapping = json.loads(run.dataset_mapping_snapshot_json or "{}")
    if not isinstance(mapping, dict):
        raise HistoricalDataGovernanceError(
            f"invalid dataset_mapping_snapshot_json for run {run_id}"
        )

    pipelines: list[str] = []
    operations: list[str] = []
    # Run-scoped pipeline -> the IngestionLog operation that publishes it. This
    # is what resolves a checkpoint-backed pipeline back to the log rows that
    # retrieved its rows.
    operation_by_pipeline: dict[str, str] = {}
    for key in dataset_keys:
        mapping_for_key = mapping.get(key)
        if not isinstance(mapping_for_key, dict):
            raise HistoricalDataGovernanceError(
                f"missing dataset mapping for {key!r} in run {run_id}"
            )
        pipeline = mapping_for_key.get("pipeline")
        data_type = mapping_for_key.get("completeness_data_type")
        if pipeline:
            pipelines.append(pipeline)
        if data_type:
            operations.append(data_type)
            if pipeline:
                operation_by_pipeline.setdefault(pipeline, data_type)

    # The pipelines that actually produced run-scoped checkpoint rows. Only
    # these declared an expectation, so only these have an actual that can be
    # measured against it. This is `pipelines` narrowed to the ones that wrote
    # a checkpoint under this run id, so the checkpoint counts below are
    # unchanged by the narrowing.
    checkpoint_pipelines: list[str] = []
    if pipelines:
        checkpoint_pipelines = list(
            db.execute(
                select(IngestionCheckpoint.pipeline).where(
                    IngestionCheckpoint.run_id == run_id,
                    IngestionCheckpoint.pipeline.in_(pipelines),
                ).distinct()
            ).scalars()
        )

    checkpoint_total = 0
    checkpoint_completed = 0
    if checkpoint_pipelines:
        checkpoint_total = int(
            db.scalar(
                select(func.count(IngestionCheckpoint.id)).where(
                    IngestionCheckpoint.run_id == run_id,
                    IngestionCheckpoint.pipeline.in_(checkpoint_pipelines),
                )
            )
            or 0
        )
        checkpoint_completed = int(
            db.scalar(
                select(func.count(IngestionCheckpoint.id)).where(
                    IngestionCheckpoint.run_id == run_id,
                    IngestionCheckpoint.pipeline.in_(checkpoint_pipelines),
                    IngestionCheckpoint.status == "COMPLETED",
                )
            )
            or 0
        )

    # --- run-scoped operation evidence -------------------------------------
    operations_total = 0
    operations_incomplete = 0
    if operations:
        operations_total = int(
            db.scalar(
                select(func.count(IngestionLog.id)).where(
                    IngestionLog.run_id == run_id,
                    IngestionLog.operation.in_(operations),
                )
            )
            or 0
        )
        operations_incomplete = int(
            db.scalar(
                select(func.count(IngestionLog.id)).where(
                    IngestionLog.run_id == run_id,
                    IngestionLog.operation.in_(operations),
                    IngestionLog.status.in_(("PARTIAL", "FAILED")),
                )
            )
            or 0
        )

    # --- run-scoped instrument evidence ------------------------------------
    # `items_total` is the row total the producer declared for an instrument
    # and `items_processed` is how much of it it actually got through. Only
    # rows whose run id matches are counted, so a checkpoint left behind by an
    # earlier run can never speak for this one.
    #
    # Missing rows are the unprocessed remainder, not the declared total: a
    # checkpoint that retrieved 80 of 90 rows contributed 80 rows and is 10
    # short, so counting its full `items_total` would report work that
    # actually happened as missing. `case` (rather than a two-argument max)
    # keeps this portable across SQLite, PostgreSQL and CockroachDB.
    declared_total = 0
    unprocessed_total = 0
    if checkpoint_pipelines:
        declared_total = int(
            db.scalar(
                select(func.coalesce(func.sum(IngestionCheckpoint.items_total), 0)).where(
                    IngestionCheckpoint.run_id == run_id,
                    IngestionCheckpoint.pipeline.in_(checkpoint_pipelines),
                )
            )
            or 0
        )
        unprocessed = (
            func.coalesce(IngestionCheckpoint.items_total, 0)
            - func.coalesce(IngestionCheckpoint.items_processed, 0)
        )
        unprocessed_total = int(
            db.scalar(
                select(
                    func.coalesce(
                        func.sum(case((unprocessed > 0, unprocessed), else_=0)), 0
                    )
                ).where(
                    IngestionCheckpoint.run_id == run_id,
                    IngestionCheckpoint.pipeline.in_(checkpoint_pipelines),
                    IngestionCheckpoint.status != "COMPLETED",
                )
            )
            or 0
        )

    # The log operations that publish the checkpoint-backed pipelines. Rows
    # fetched by any other operation in this run have no declared expectation
    # to be measured against, so they are outside the expected/actual
    # population and must not inflate it. Their operations still count
    # towards completeness_status above.
    checkpointed_operations = [
        operation_by_pipeline[pipeline]
        for pipeline in checkpoint_pipelines
        if pipeline in operation_by_pipeline
    ]

    actual = 0
    if checkpointed_operations:
        fetched = db.scalar(
            select(func.coalesce(func.sum(IngestionLog.rows_fetched), 0)).where(
                IngestionLog.run_id == run_id,
                IngestionLog.operation.in_(checkpointed_operations),
            )
        )
        actual = int(fetched or 0)

    # Rows this run expected to retrieve. Declared by the producer on the
    # checkpoints of the checkpoint-backed pipelines, and measured against the
    # rows those same pipelines' operations fetched. Absent means the run
    # declared no expectation, which is not the same as an expectation of zero.
    expected = declared_total if declared_total > 0 else None
    # Rows this run declared but did not obtain: the unprocessed remainder of
    # the checkpoints that did not complete. A completed checkpoint
    # contributes nothing. Under-counting stays the safe direction: an
    # instrument that failed before declaring a total has no remainder to
    # report and contributes nothing rather than an invented number.
    missing = int(unprocessed_total or 0)

    incomplete_units = operations_incomplete + (checkpoint_total - checkpoint_completed)
    evidence_units = operations_total + checkpoint_total
    if evidence_units == 0:
        completeness_status = "UNKNOWN"
    elif incomplete_units > 0:
        completeness_status = "PARTIAL"
    else:
        completeness_status = "COMPLETE"

    run.expected_records = expected
    run.actual_records = actual
    run.missing_records = missing
    run.checkpoints_total = checkpoint_total
    run.checkpoints_completed = checkpoint_completed
    run.completeness_status = completeness_status
    db.commit()
    db.refresh(run)
    return run


def _effective_terminal_status(requested: str, completeness_status: str) -> str:
    """Derive the status actually stored for a terminal manifest.

    A run asked to finish ``SUCCEEDED`` whose own refreshed evidence says the
    acquisition was ``PARTIAL`` must not be recorded as a clean success. Both
    the normal finalizer and the last-resort fallback derive the stored status
    here, and the fallback reads the completeness the metric refresh already
    committed, so the two entry points cannot disagree about the same run.
    """
    if requested == RUN_SUCCEEDED and completeness_status == "PARTIAL":
        return RUN_PARTIAL
    return requested


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
    run.status = _effective_terminal_status(status, run.completeness_status)
    run.error_message = error_message
    run.completed_at = _utcnow_naive()
    db.commit()
    db.refresh(run)
    return run


def force_terminal_ingestion_run(
    db: Session,
    run_id: str,
    *,
    status: str,
    error_message: str | None = None,
) -> HistoricalIngestionRun:
    """Record a terminal state without refreshing metrics.

    Last-resort terminalization for a caller whose metric refresh or commit
    failed: a manifest must never be left RUNNING with no owner and no
    terminal error. Evidence metrics are left exactly as the failed refresh
    left them rather than being guessed.

    The stored status is derived from the completeness the metric refresh
    already committed, so a run whose persisted evidence says PARTIAL is not
    recorded as a clean SUCCEEDED here: the fallback and the normal finalizer
    agree on the same derived terminal status.
    """
    if status not in {RUN_SUCCEEDED, RUN_FAILED, RUN_PARTIAL}:
        raise HistoricalDataGovernanceError(f"invalid ingestion-run status: {status}")
    db.rollback()
    run = db.scalar(
        select(HistoricalIngestionRun).where(HistoricalIngestionRun.run_id == run_id)
    )
    if run is None:
        raise HistoricalDataGovernanceError(f"unknown ingestion run: {run_id}")
    run.status = _effective_terminal_status(status, run.completeness_status)
    run.error_message = error_message
    run.completed_at = _utcnow_naive()
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

    model, timestamp_name, clock_convention = _RETENTION_TARGETS[dataset_key]
    # ``now`` is an instant; it is normalized to naive UTC first and only then
    # expressed in the clock the target column actually stores.
    clock = _naive_utc(now) if now is not None else _utcnow_naive()
    if clock_convention == CLOCK_IST_NAIVE:
        cutoff = to_ist_naive(clock.replace(tzinfo=timezone.utc)) - timedelta(
            days=row.retention_days
        )
    else:
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
        executable=(
            row.retention_enforced
            and row.dataset_tier != "RAW"
            and dataset_key not in _RAW_RETENTION_TARGETS
        ),
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
    if dataset_key in _RAW_RETENTION_TARGETS or row.dataset_tier == "RAW":
        raise HistoricalDataGovernanceError(
            f"raw historical data cannot be deleted by the Day 48 retention service: "
            f"{dataset_key}"
        )
    if plan.cutoff is None:
        return plan

    # Finding 5: executable deletion must satisfy the EXISTING recomputation
    # contract before a single row is removed. This reuses
    # ``assert_recomputation_safe`` — the single dependency-validation
    # authority — so a non-recomputable dataset, a missing or inactive
    # dependency, or a cyclic chain blocks deletion instead of destroying
    # the last copy. Raw targets never reach this point: the static guard
    # above refuses them independently of this check.
    assert_recomputation_safe(db, dataset_key)

    model, timestamp_name, _clock_convention = _RETENTION_TARGETS[dataset_key]
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
