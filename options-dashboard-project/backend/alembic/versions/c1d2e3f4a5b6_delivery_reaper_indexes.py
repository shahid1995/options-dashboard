"""Delivery reaper indexes and the evidence dedupe key.

Adds the three indexes the worker/reaper contract needs:

    ix_delivery_status_lease           deliveries (status, lease_expires_at)
    ix_delivery_status_payload_expiry  deliveries (status, payload_expires_at)
    uq_evidence_provider_event         evidence   (source, evidence_source)

The reaper scans two disjoint status sets -- expired claims, and blocked
deliveries whose payload has expired -- so without the first two indexes every
pass is a sequential scan over the whole deliveries table.

The unique key gives one provider observation exactly one row. Without it a
duplicate webhook delivery inserts a second evidence row and two observers can
disagree about the same event. It is created only after a preflight, because
existing duplicates would fail the DDL with a driver error and leave the
migration half applied.

Revision ID: c1d2e3f4a5b6
Revises: a8b3d5e7c902
"""

from alembic import op
import sqlalchemy as sa


revision = "c1d2e3f4a5b6"
down_revision = "a8b3d5e7c902"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    n = bind.execute(
        sa.text(
            """
            SELECT COUNT(*) FROM (
                SELECT source, evidence_source
                FROM email_delivery_evidence
                GROUP BY source, evidence_source
                HAVING COUNT(*) > 1
            ) AS duplicate_events
            """
        )
    ).scalar()
    if n:
        raise RuntimeError(
            f"preflight duplicate provider events failed: {n} "
            "(source, evidence_source) pair(s) already have more than one row"
        )

    op.create_index(
        "ix_delivery_status_lease",
        "email_deliveries",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "ix_delivery_status_payload_expiry",
        "email_deliveries",
        ["status", "payload_expires_at"],
    )
    op.create_index(
        "uq_evidence_provider_event",
        "email_delivery_evidence",
        ["source", "evidence_source"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_evidence_provider_event", table_name="email_delivery_evidence")
    op.drop_index("ix_delivery_status_payload_expiry", table_name="email_deliveries")
    op.drop_index("ix_delivery_status_lease", table_name="email_deliveries")
