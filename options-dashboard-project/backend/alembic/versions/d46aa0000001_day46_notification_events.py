"""Day 46 — notification events (Issue #92).

Additive-only schema for the backend-authoritative notification
abstraction:

  - ``notification_events`` — durable notification events with type,
    severity, source, sanitized structured details, tenant/user scope
    (nullable for platform-operational events), correlation ID, dedup
    key and occurred-at. Indexed for tenant-isolated reads and the
    bounded dedup-window lookup.

No existing table is altered. Alembic remains the sole schema authority.

Revision ID: d46aa0000001
Revises: d45aa0000001
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "d46aa0000001"
down_revision = "d45aa0000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event_type", sa.String(length=64), nullable=False, index=True),
        sa.Column("severity", sa.String(length=16), nullable=False, index=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("user_scope", sa.String(length=36), nullable=True, index=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True, index=True),
        sa.Column("dedup_key", sa.String(length=128), nullable=True, index=True),
        sa.Column(
            "occurred_at", sa.DateTime(), nullable=False, index=True,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_notification_events_scope_dedup",
        "notification_events",
        ["user_scope", "dedup_key", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_events_scope_dedup", table_name="notification_events"
    )
    op.drop_table("notification_events")
