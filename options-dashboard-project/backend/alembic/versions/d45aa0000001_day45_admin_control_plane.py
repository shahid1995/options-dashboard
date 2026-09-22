"""Day 45 — admin control plane foundation (Issue #90).

Additive-only schema for the admin authorization boundary and control
plane:

  - ``users.is_admin`` — explicit durable admin flag (Boolean, NOT NULL,
    server_default false, indexed). Admin authority is never inferred from
    tenant ownership or broker linkage; the flag is set only through an
    ops/Founder bootstrap on the database (no API grants it).
  - ``admin_controls`` — admin-owned instrument/configuration/retention/
    feature-flag controls with version + append-only history ledger.
  - ``admin_audit_events`` — append-only audit records for material admin
    actions (actor/action/target/result/time; sanitized, secret-free).

No existing table is altered except the additive ``users.is_admin`` column;
existing rows default to non-admin. Alembic remains the sole schema
authority.

Revision ID: d45aa0000001
Revises: f4a9b8c2d1e7
Create Date: 2026-09-22
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "d45aa0000001"
down_revision = "f4a9b8c2d1e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "ix_users_is_admin", "users", ["is_admin"]
    )
    op.create_table(
        "admin_controls",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("domain", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by", sa.String(length=36), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("history", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("domain", "key", name="uq_admin_controls_domain_key"),
    )
    op.create_index("ix_admin_controls_domain", "admin_controls", ["domain"])
    op.create_index("ix_admin_controls_key", "admin_controls", ["key"])
    op.create_table(
        "admin_audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False, server_default="success"),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_audit_events_actor", "admin_audit_events", ["actor_user_id"])
    op.create_index("ix_admin_audit_events_action", "admin_audit_events", ["action"])
    op.create_index("ix_admin_audit_events_result", "admin_audit_events", ["result"])
    op.create_index("ix_admin_audit_events_occurred_at", "admin_audit_events", ["occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_admin_audit_events_occurred_at", table_name="admin_audit_events")
    op.drop_index("ix_admin_audit_events_result", table_name="admin_audit_events")
    op.drop_index("ix_admin_audit_events_action", table_name="admin_audit_events")
    op.drop_index("ix_admin_audit_events_actor", table_name="admin_audit_events")
    op.drop_table("admin_audit_events")
    op.drop_index("ix_admin_controls_key", table_name="admin_controls")
    op.drop_index("ix_admin_controls_domain", table_name="admin_controls")
    op.drop_table("admin_controls")
    op.drop_index("ix_users_is_admin", table_name="users")
    op.drop_column("users", "is_admin")
