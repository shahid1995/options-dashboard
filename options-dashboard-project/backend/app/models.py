        return (
            f"<BackgroundJob id={self.id} type={self.job_type} "
            f"status={self.status} attempts={self.attempt_count}>"
        )

# ---------------------------------------------------------------------------
# Day 48 — Historical data governance
# ---------------------------------------------------------------------------


class HistoricalDatasetGovernance(Base):
    """Governance/catalog record for one historical dataset.

    This is control metadata, not market data. It records the source,
    entitlement/license/usage/redistribution state, raw-vs-derived contract,
    and retention policy needed to make historical acquisition auditable.
    """

    __tablename__ = "historical_dataset_governance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    domain: Mapped[str] = mapped_column(String(32))
    dataset_tier: Mapped[str] = mapped_column(String(16), index=True)
    table_name: Mapped[str] = mapped_column(String(64))
    pipeline: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completeness_data_type: Mapped[str | None] = mapped_column(String(32), nullable=True)

    source: Mapped[str] = mapped_column(String(64))
    source_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    entitlement_requirement: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entitlement_status: Mapped[str] = mapped_column(String(24))
    license_status: Mapped[str] = mapped_column(String(24))
    usage_policy: Mapped[str] = mapped_column(String(24))
    redistribution_status: Mapped[str] = mapped_column(String(24))

    retention_policy: Mapped[str] = mapped_column(String(24))
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retention_enforced: Mapped[bool] = mapped_column(default=False)

    raw_immutable: Mapped[bool] = mapped_column(default=False)
    recomputable: Mapped[bool] = mapped_column(default=False)
    dependencies_json: Mapped[str] = mapped_column(Text, default="[]")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class HistoricalIngestionRun(Base):
    """Auditable manifest for one historical ingestion job/run.

    Policy fields are snapshots copied from the dataset catalog so later
    policy changes do not rewrite the historical record of what applied to
    an earlier acquisition.
    """

    __tablename__ = "historical_ingestion_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    background_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    dataset_keys_json: Mapped[str] = mapped_column(Text, default="[]")
    source_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    entitlement_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    policy_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")

    purpose: Mapped[str] = mapped_column(String(32), default="INTERNAL_RESEARCH")
    coverage_start: Mapped[str | None] = mapped_column(String(10), nullable=True)
    coverage_end: Mapped[str | None] = mapped_column(String(10), nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="RUNNING", index=True)
    expected_records: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_records: Mapped[int] = mapped_column(Integer, default=0)
    missing_records: Mapped[int] = mapped_column(Integer, default=0)
    checkpoints_total: Mapped[int] = mapped_column(Integer, default=0)
    checkpoints_completed: Mapped[int] = mapped_column(Integer, default=0)
    completeness_status: Mapped[str] = mapped_column(String(16), default="UNKNOWN")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")

    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
