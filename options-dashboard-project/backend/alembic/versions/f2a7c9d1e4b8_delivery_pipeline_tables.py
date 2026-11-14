"""Delivery pipeline: deliveries, generations, attempts, evidence.

Creates the four tables the delivery state machine needs. Cross-table
composite foreign keys (ownership, attempt->generation, evidence->attempt and
the active-generation pointer) are added in the following revision so that
every referenced table and unique target key already exists.

Revision ID: f2a7c9d1e4b8
Revises: b2c3d4e5f6a7
"""

from alembic import op
import sqlalchemy as sa


revision = "f2a7c9d1e4b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


STATUS_ENUM = (
    "('pending','sending','dispatched','awaiting_reconciliation',"
    "'review_required','delivered','failed','expired','cancelled')"
)
TERMINAL = "('delivered','failed','expired','cancelled')"
BLOCKED = "('awaiting_reconciliation','review_required')"
DISPOSITION_ENUM = (
    "('reserved','payload_released','dispatch_authorized',"
    "'execution_unknown','execution_completed','not_dispatched')"
)
OUTCOME_ENUM = "('accepted','rejected_retryable','rejected_permanent')"


def upgrade() -> None:
    op.create_table(
        "email_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        # Nullable only because the composite FK target cannot exist before the
        # delivery row does. No committed row may hold NULL: see
        # a8b3d5e7c902 preflight P5 and the creation transaction.
        sa.Column("active_generation_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claim_source", sa.String(length=64), nullable=True),
        sa.Column("reservation_id", sa.String(length=64), nullable=True),
        sa.Column("call_reserved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("payload_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"status IN {STATUS_ENUM}",
                           name="ck_delivery_status_enum"),
        sa.CheckConstraint(
            f"(status IN {TERMINAL}) = (finalized_at IS NOT NULL)",
            name="ck_delivery_terminal_finalized",
        ),
        sa.CheckConstraint(
            f"status NOT IN {BLOCKED} OR lease_expires_at IS NULL",
            name="ck_delivery_blocked_no_lease",
        ),
        sa.CheckConstraint(
            "(payload_encrypted IS NULL) = (payload_expires_at IS NULL)",
            name="ck_delivery_payload_pair",
        ),
        sa.UniqueConstraint("id", "user_id", "principal_id",
                            name="uq_delivery_identity"),
    )

    op.create_table(
        "email_delivery_generations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 1", name="ck_generation_positive"),
        sa.UniqueConstraint("delivery_id", "generation",
                            name="uq_generation_number"),
        # composite FK target for the active-generation pointer and for
        # attempts (delivery_id, generation_id)
        sa.UniqueConstraint("delivery_id", "id", name="uq_generation_delivery_id"),
        sa.ForeignKeyConstraint(["delivery_id"], ["email_deliveries.id"],
                                name="fk_generation_delivery"),
    )

    op.create_table(
        "email_delivery_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_index", sa.Integer(), nullable=False),
        sa.Column("reservation_id", sa.String(length=64), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claim_source", sa.String(length=64), nullable=True),
        sa.Column("payload_released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_marker_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_disposition", sa.String(length=32), nullable=False),
        sa.Column("provider_outcome", sa.String(length=32), nullable=True),
        sa.Column("original_call_reserved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("original_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"execution_disposition IN {DISPOSITION_ENUM}",
                           name="ck_attempt_disposition_enum"),
        sa.CheckConstraint(
            "execution_disposition <> 'execution_completed'"
            " OR provider_outcome IS NOT NULL",
            name="ck_attempt_completed_requires_outcome",
        ),
        sa.CheckConstraint(
            f"provider_outcome IS NULL OR provider_outcome IN {OUTCOME_ENUM}",
            name="ck_attempt_outcome_enum",
        ),
        sa.CheckConstraint("attempt_index >= 1", name="ck_attempt_index_positive"),
        # composite FK targets
        sa.UniqueConstraint("delivery_id", "id", name="uq_attempt_delivery_id"),
        sa.UniqueConstraint("delivery_id", "generation_id",
                            name="uq_attempt_generation"),
        sa.UniqueConstraint("delivery_id", "attempt_index",
                            name="uq_attempt_index"),
        sa.ForeignKeyConstraint(["delivery_id"], ["email_deliveries.id"],
                                name="fk_attempt_delivery"),
    )

    op.create_table(
        "email_delivery_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("observed_outcome", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("evidence_source", sa.String(length=64), nullable=False),
        sa.CheckConstraint(f"observed_outcome IN {OUTCOME_ENUM}",
                           name="ck_evidence_outcome_enum"),
        # No unique on (delivery_id, attempt_id): an attempt may accumulate
        # several observations (that is what H1 detects). The evidence->attempt
        # FK targets uq_attempt_delivery_id on the attempt side.
        sa.ForeignKeyConstraint(["delivery_id"], ["email_deliveries.id"],
                                name="fk_evidence_delivery"),
    )


def downgrade() -> None:
    op.drop_table("email_delivery_evidence")
    op.drop_table("email_delivery_attempts")
    op.drop_table("email_delivery_generations")
    op.drop_table("email_deliveries")
