"""Issue #78 — Phase 3 value provenance on research chain snapshots.

Adds one nullable JSON column, ``value_provenance``, to
``gap_option_chain_snapshots`` so every IV/Greek/quote value in the research
dataset is distinguishable as ``observed`` / ``reconstructed`` /
``unavailable`` / ``derived`` (Phase 3 hard contract: a Black-Scholes-derived
value must never be reported as observed).

Minimal and additive: existing Phase 1/2 rows keep NULL (documented as
"pre-Phase-3 provenance"; Phase 2 enriched rows are engine-reconstructed by
construction). No data is rewritten.

Revision ID: f4a9b8c2d1e7
Revises: e9f8a7b6c5d4
Create Date: 2026-09-20
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "f4a9b8c2d1e7"
down_revision = "e9f8a7b6c5d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "gap_option_chain_snapshots",
        sa.Column("value_provenance", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("gap_option_chain_snapshots", "value_provenance")
