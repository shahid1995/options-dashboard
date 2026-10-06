"""Day 48 — immutable dataset-mapping snapshot for ingestion manifests.

Follow-up to d48aa0000001: the Day-48 application model and governance
service persist the dataset mapping captured at run creation in
``historical_ingestion_runs.dataset_mapping_snapshot_json``, but the original
Day-48 migration did not create that column. This additive ALTER adds it so
``refresh_ingestion_run_metrics`` interprets an already-created run from its
immutable snapshot instead of the mutable current catalog (Greptile
Finding 3, PR #128).

Existing rows receive the repository's established empty-JSON-object
representation (``server_default="{}"``), matching sibling snapshot columns
such as ``source_snapshot_json``. A legacy run with an empty snapshot fails
closed in the service layer rather than falling back to current catalog state.

Plain column types keep SQLite, PostgreSQL, and CockroachDB compatibility.
Alembic remains the sole schema authority.
"""

from alembic import op
import sqlalchemy as sa

revision = "d48aa0000002"
down_revision = "d48aa0000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "historical_ingestion_runs",
        sa.Column(
            "dataset_mapping_snapshot_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "historical_ingestion_runs",
        "dataset_mapping_snapshot_json",
    )
