"""Day 47 — durable background jobs (Issue: Day 47 master plan).

Additive-only schema for the smallest durable background-job subsystem:

  - ``background_jobs`` — one row per logical job with idempotency key,
    status, attempt count, lease owner/deadline, run-after scheduling
    (``available_at``), started/completed timestamps, last-error info and
    an explicit dead-letter reason. Claims are handed out by a single
    conditional UPDATE (lease/visibility-timeout pattern), so a crashed
    worker's job becomes claimable again once its lease expires, and two
    workers can never own the same attempt.

No existing table is altered. Alembic remains the sole schema authority.
Compatible with PostgreSQL, CockroachDB (plain column types, named
indexes) and SQLite (test/convenience runtime).

Revision ID: d47aa0000001
Revises: d46aa0000001
Create Date: 2026-09-27
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "d47aa0000001"
down_revision = "d46aa0000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOTE: no ``index=True`` on the columns below — Alembic would create
    # those indexes during ``create_table`` and the explicit ``create_index``
    # calls further down would collide (DuplicateTable). All indexes are
    # created explicitly with the exact names the ORM metadata declares.
    op.create_table(
        "background_jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("user_scope", sa.String(length=36), nullable=True),
        sa.Column(
            "status", sa.String(length=32), nullable=False,
            server_default="PENDING",
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "available_at", sa.DateTime(), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("dead_letter_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.func.now(),
        ),
        # Named table constraint declared INSIDE create_table: a separate
        # ``op.create_unique_constraint`` is an ALTER that SQLite (the
        # dev/test runtime) cannot execute, while CREATE TABLE ... UNIQUE
        # works identically on SQLite, PostgreSQL and CockroachDB.
        sa.UniqueConstraint(
            "idempotency_key", name="uq_background_jobs_idempotency"
        ),
    )
    op.create_index(
        "ix_background_jobs_status_available",
        "background_jobs",
        ["status", "available_at"],
    )
    op.create_index(
        "ix_background_jobs_job_type",
        "background_jobs",
        ["job_type"],
    )
    op.create_index(
        "ix_background_jobs_lease_owner",
        "background_jobs",
        ["lease_owner"],
    )
    op.create_index(
        "ix_background_jobs_user_scope",
        "background_jobs",
        ["user_scope"],
    )


def downgrade() -> None:
    op.drop_index("ix_background_jobs_user_scope", table_name="background_jobs")
    op.drop_index("ix_background_jobs_lease_owner", table_name="background_jobs")
    op.drop_index("ix_background_jobs_job_type", table_name="background_jobs")
    op.drop_index(
        "ix_background_jobs_status_available", table_name="background_jobs"
    )
    op.drop_table("background_jobs")
