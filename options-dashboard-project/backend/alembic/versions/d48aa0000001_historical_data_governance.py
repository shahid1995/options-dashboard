"""Day 48 — historical data governance catalog and ingestion manifests.

Additive-only governance metadata for historical datasets and ingestion runs.
The catalog records provenance, entitlement/license/usage/redistribution
decisions, immutability/recomputation contracts, and retention policy without
changing existing raw/model/analytics tables.

Alembic remains the sole schema authority. Plain column types keep SQLite,
PostgreSQL, and CockroachDB compatibility.
"""

from alembic import op
import sqlalchemy as sa

revision = "d48aa0000001"
down_revision = "d47aa0000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "historical_dataset_governance",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_key", sa.String(length=64), nullable=False),
        sa.Column("domain", sa.String(length=32), nullable=False),
        sa.Column("dataset_tier", sa.String(length=16), nullable=False),
        sa.Column("table_name", sa.String(length=64), nullable=False),
        sa.Column("pipeline", sa.String(length=64), nullable=True),
        sa.Column("completeness_data_type", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_reference", sa.String(length=255), nullable=True),
        sa.Column("source_version", sa.String(length=64), nullable=True),
        sa.Column("entitlement_requirement", sa.String(length=64), nullable=True),
        sa.Column("entitlement_status", sa.String(length=24), nullable=False),
        sa.Column("license_status", sa.String(length=24), nullable=False),
        sa.Column("usage_policy", sa.String(length=24), nullable=False),
        sa.Column("redistribution_status", sa.String(length=24), nullable=False),
        sa.Column("retention_policy", sa.String(length=24), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("retention_enforced", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw_immutable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recomputable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dependencies_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("dataset_key", name="uq_historical_dataset_governance_key"),
    )
    op.create_index(
        "ix_historical_dataset_governance_tier",
        "historical_dataset_governance",
        ["dataset_tier"],
    )
    op.create_index(
        "ix_historical_dataset_governance_source",
        "historical_dataset_governance",
        ["source"],
    )

    op.create_table(
        "historical_ingestion_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("background_job_id", sa.String(length=36), nullable=True),
        sa.Column("dataset_keys_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("source_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("entitlement_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("policy_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("purpose", sa.String(length=32), nullable=False, server_default="INTERNAL_RESEARCH"),
        sa.Column("coverage_start", sa.String(length=10), nullable=True),
        sa.Column("coverage_end", sa.String(length=10), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="RUNNING"),
        sa.Column("expected_records", sa.Integer(), nullable=True),
        sa.Column("actual_records", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("missing_records", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checkpoints_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checkpoints_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completeness_status", sa.String(length=16), nullable=False, server_default="UNKNOWN"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("run_id", name="uq_historical_ingestion_runs_run_id"),
    )
    op.create_index(
        "ix_historical_ingestion_runs_job",
        "historical_ingestion_runs",
        ["background_job_id"],
    )
    op.create_index(
        "ix_historical_ingestion_runs_status",
        "historical_ingestion_runs",
        ["status"],
    )

    governance_table = sa.table(
        "historical_dataset_governance",
        sa.column("dataset_key", sa.String()),
        sa.column("domain", sa.String()),
        sa.column("dataset_tier", sa.String()),
        sa.column("table_name", sa.String()),
        sa.column("pipeline", sa.String()),
        sa.column("completeness_data_type", sa.String()),
        sa.column("source", sa.String()),
        sa.column("source_reference", sa.String()),
        sa.column("source_version", sa.String()),
        sa.column("entitlement_requirement", sa.String()),
        sa.column("entitlement_status", sa.String()),
        sa.column("license_status", sa.String()),
        sa.column("usage_policy", sa.String()),
        sa.column("redistribution_status", sa.String()),
        sa.column("retention_policy", sa.String()),
        sa.column("retention_days", sa.Integer()),
        sa.column("retention_enforced", sa.Boolean()),
        sa.column("raw_immutable", sa.Boolean()),
        sa.column("recomputable", sa.Boolean()),
        sa.column("dependencies_json", sa.Text()),
        sa.column("notes", sa.Text()),
        sa.column("active", sa.Boolean()),
    )
    op.bulk_insert(
        governance_table,
        [
            {
                "dataset_key": "UPSTOX_CONTRACT_SPECS",
                "domain": "MARKET_DATA",
                "dataset_tier": "RAW",
                "table_name": "contract_specs",
                "pipeline": "backfill_contracts",
                "completeness_data_type": "contract_metadata",
                "source": "UPSTOX",
                "source_reference": "Upstox official API documentation",
                "source_version": "current-public-docs",
                "entitlement_requirement": "BROKER_API_ENTITLEMENT",
                "entitlement_status": "REVIEW_REQUIRED",
                "license_status": "REVIEW_REQUIRED",
                "usage_policy": "INTERNAL_ONLY",
                "redistribution_status": "REVIEW_REQUIRED",
                "retention_policy": "KEEP",
                "retention_days": None,
                "retention_enforced": False,
                "raw_immutable": True,
                "recomputable": True,
                "dependencies_json": "[]",
                "notes": "Historical contract metadata is source truth for lot size, strike, expiry and instrument identity. Redistribution rights are not inferred from API availability.",
                "active": True,
            },
            {
                "dataset_key": "UPSTOX_NIFTY_CANDLES_3MIN",
                "domain": "MARKET_DATA",
                "dataset_tier": "RAW",
                "table_name": "nifty_candles",
                "pipeline": "backfill_nifty",
                "completeness_data_type": "nifty_candles",
                "source": "UPSTOX",
                "source_reference": "Upstox official API documentation",
                "source_version": "current-public-docs",
                "entitlement_requirement": "BROKER_API_ENTITLEMENT",
                "entitlement_status": "REVIEW_REQUIRED",
                "license_status": "REVIEW_REQUIRED",
                "usage_policy": "INTERNAL_ONLY",
                "redistribution_status": "REVIEW_REQUIRED",
                "retention_policy": "KEEP",
                "retention_days": None,
                "retention_enforced": False,
                "raw_immutable": True,
                "recomputable": True,
                "dependencies_json": "[]",
                "notes": "Raw market observations retained as the recomputation source. Do not infer redistribution permission from storage rights.",
                "active": True,
            },
            {
                "dataset_key": "UPSTOX_OPTION_CANDLES_3MIN",
                "domain": "MARKET_DATA",
                "dataset_tier": "RAW",
                "table_name": "option_candles",
                "pipeline": "backfill_options",
                "completeness_data_type": "option_candles",
                "source": "UPSTOX",
                "source_reference": "https://upstox.com/developer/api-documentation/get-expired-historical-candle-data/",
                "source_version": "current-public-docs",
                "entitlement_requirement": "UPSTOX_PLUS_OR_EQUIVALENT",
                "entitlement_status": "REVIEW_REQUIRED",
                "license_status": "REVIEW_REQUIRED",
                "usage_policy": "INTERNAL_ONLY",
                "redistribution_status": "REVIEW_REQUIRED",
                "retention_policy": "KEEP",
                "retention_days": None,
                "retention_enforced": False,
                "raw_immutable": True,
                "recomputable": True,
                "dependencies_json": "[]",
                "notes": "Expired historical candle access is entitlement-gated. Current public documentation does not establish redistribution rights; treat as review-required.",
                "active": True,
            },
            {
                "dataset_key": "STRIKENOVA_OPTION_GREEKS",
                "domain": "QUANT",
                "dataset_tier": "MODEL",
                "table_name": "option_greeks",
                "pipeline": None,
                "completeness_data_type": None,
                "source": "STRIKENOVA",
                "source_reference": "Internal quantitative engine",
                "source_version": "calc_version",
                "entitlement_requirement": None,
                "entitlement_status": "NOT_APPLICABLE",
                "license_status": "INTERNAL",
                "usage_policy": "INTERNAL_ONLY",
                "redistribution_status": "REVIEW_REQUIRED",
                "retention_policy": "DELETE_AFTER_DAYS",
                "retention_days": 365,
                "retention_enforced": False,
                "raw_immutable": False,
                "recomputable": True,
                "dependencies_json": "[\"UPSTOX_OPTION_CANDLES_3MIN\",\"UPSTOX_CONTRACT_SPECS\",\"UPSTOX_NIFTY_CANDLES_3MIN\"]",
                "notes": "Derived model output. Rebuildable from immutable raw observations plus the calculation version.",
                "active": True,
            },
            {
                "dataset_key": "STRIKENOVA_HISTORICAL_GEX",
                "domain": "INTELLIGENCE",
                "dataset_tier": "ANALYTICS",
                "table_name": "historical_gex",
                "pipeline": None,
                "completeness_data_type": None,
                "source": "STRIKENOVA",
                "source_reference": "Internal GEX engine",
                "source_version": "calc_version",
                "entitlement_requirement": None,
                "entitlement_status": "NOT_APPLICABLE",
                "license_status": "INTERNAL",
                "usage_policy": "INTERNAL_ONLY",
                "redistribution_status": "REVIEW_REQUIRED",
                "retention_policy": "DELETE_AFTER_DAYS",
                "retention_days": 90,
                "retention_enforced": False,
                "raw_immutable": False,
                "recomputable": True,
                "dependencies_json": "[\"UPSTOX_OPTION_CANDLES_3MIN\",\"STRIKENOVA_OPTION_GREEKS\",\"UPSTOX_NIFTY_CANDLES_3MIN\",\"UPSTOX_CONTRACT_SPECS\"]",
                "notes": "Derived analytics. Must remain traceable to immutable raw/model dependencies and the recorded calculation version.",
                "active": True,
            },
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_historical_ingestion_runs_status", table_name="historical_ingestion_runs")
    op.drop_index("ix_historical_ingestion_runs_job", table_name="historical_ingestion_runs")
    op.drop_table("historical_ingestion_runs")
    op.drop_index("ix_historical_dataset_governance_source", table_name="historical_dataset_governance")
    op.drop_index("ix_historical_dataset_governance_tier", table_name="historical_dataset_governance")
    op.drop_table("historical_dataset_governance")
