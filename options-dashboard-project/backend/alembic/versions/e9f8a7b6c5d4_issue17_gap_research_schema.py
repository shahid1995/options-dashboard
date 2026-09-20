"""Issue #17 — overnight gap intelligence research schema (Phase 1, research-only).

Revision ID: e9f8a7b6c5d4
Revises: c1d2e3f4a5b6
Create Date: 2026-09-20

Implements (research scope):
  docs/STRIKENOVA_OVERNIGHT_GAP_RESEARCH.md §19 (historical dataset design)

Schema (exactly the app.models research models; nothing more):
  - ``gap_prediction_sessions``     one row per research session (predictor
    side + later-attached realized target columns)
  - ``gap_underlying_snapshots``    immutable spot/futures/VIX + raw chain JSON
  - ``gap_option_chain_snapshots``  immutable strike-level CE/PE rows
  - ``gap_features``                derived features (versioned JSON)
  - ``gap_predictions``             model outputs (immutable once stored)
  - ``gap_backtest_results``        aggregate evaluation by model/period/regime

Research-only: no production table is altered. Raw snapshots are append-only;
missing data stays NULL (never silently zero) with explicit completeness flags.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "e9f8a7b6c5d4"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gap_prediction_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=16), nullable=False, server_default="NIFTY"),
        sa.Column("session_date", sa.String(length=10), nullable=False),
        sa.Column("cutoff_timestamp", sa.DateTime(), nullable=False),
        sa.Column("prior_close", sa.Float(), nullable=False),
        sa.Column("next_session_date", sa.String(length=10), nullable=True),
        sa.Column("next_open_timestamp", sa.DateTime(), nullable=True),
        sa.Column("next_open", sa.Float(), nullable=True),
        sa.Column("gap_points", sa.Float(), nullable=True),
        sa.Column("gap_pct", sa.Float(), nullable=True),
        sa.Column("gap_class", sa.String(length=12), nullable=True),
        sa.Column("completeness", sa.String(length=16), nullable=False, server_default="UNKNOWN"),
        sa.Column("completeness_detail", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("symbol", "session_date", name="uq_gap_sessions_symbol_session"),
    )
    op.create_index("ix_gap_prediction_sessions_session_date", "gap_prediction_sessions", ["session_date"])

    op.create_table(
        "gap_underlying_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("session_date", sa.String(length=10), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("spot_ltp", sa.Float(), nullable=True),
        sa.Column("spot_open", sa.Float(), nullable=True),
        sa.Column("spot_high", sa.Float(), nullable=True),
        sa.Column("spot_low", sa.Float(), nullable=True),
        sa.Column("spot_close", sa.Float(), nullable=True),
        sa.Column("futures_ltp", sa.Float(), nullable=True),
        sa.Column("futures_oi", sa.Float(), nullable=True),
        sa.Column("futures_volume", sa.Float(), nullable=True),
        sa.Column("futures_basis", sa.Float(), nullable=True),
        sa.Column("india_vix", sa.Float(), nullable=True),
        sa.Column("option_chain", sa.Text(), nullable=False, server_default="[]"),
        sa.UniqueConstraint("symbol", "session_date", name="uq_gap_underlying_symbol_session"),
    )
    op.create_index("ix_gap_underlying_snapshots_session_date", "gap_underlying_snapshots", ["session_date"])

    op.create_table(
        "gap_option_chain_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("session_date", sa.String(length=10), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("expiry", sa.String(length=10), nullable=False),
        sa.Column("strike", sa.Float(), nullable=False),
        sa.Column("option_type", sa.String(length=8), nullable=False),
        sa.Column("ltp", sa.Float(), nullable=True),
        sa.Column("bid", sa.Float(), nullable=True),
        sa.Column("ask", sa.Float(), nullable=True),
        sa.Column("bid_qty", sa.Float(), nullable=True),
        sa.Column("ask_qty", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("open_interest", sa.Float(), nullable=True),
        sa.Column("change_in_oi", sa.Float(), nullable=True),
        sa.Column("iv", sa.Float(), nullable=True),
        sa.Column("delta", sa.Float(), nullable=True),
        sa.Column("gamma", sa.Float(), nullable=True),
        sa.Column("vega", sa.Float(), nullable=True),
        sa.Column("theta", sa.Float(), nullable=True),
        sa.UniqueConstraint(
            "symbol", "session_date", "expiry", "strike", "option_type",
            name="uq_gap_chain_symbol_session_expiry_strike_type",
        ),
    )
    op.create_index("ix_gap_option_chain_snapshots_session_date", "gap_option_chain_snapshots", ["session_date"])

    op.create_table(
        "gap_features",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_date", sa.String(length=10), nullable=False),
        sa.Column("feature_version", sa.String(length=16), nullable=False, server_default="v1"),
        sa.Column("features", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("completeness", sa.String(length=16), nullable=False, server_default="UNKNOWN"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("session_date", "feature_version", name="uq_gap_features_session_version"),
    )
    op.create_index("ix_gap_features_session_date", "gap_features", ["session_date"])

    op.create_table(
        "gap_predictions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_date", sa.String(length=10), nullable=False),
        sa.Column("model_name", sa.String(length=32), nullable=False),
        sa.Column("model_version", sa.String(length=16), nullable=False, server_default="v1"),
        sa.Column("direction_score", sa.Float(), nullable=True),
        sa.Column("agreement_score", sa.Float(), nullable=True),
        sa.Column("dispersion", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="PREDICTED"),
        sa.Column("component_scores", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("probabilities", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "session_date", "model_name", "model_version",
            name="uq_gap_predictions_session_model",
        ),
    )
    op.create_index("ix_gap_predictions_session_date", "gap_predictions", ["session_date"])

    op.create_table(
        "gap_backtest_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_name", sa.String(length=32), nullable=False),
        sa.Column("model_version", sa.String(length=16), nullable=False, server_default="v1"),
        sa.Column("period_start", sa.String(length=10), nullable=False),
        sa.Column("period_end", sa.String(length=10), nullable=False),
        sa.Column("regime", sa.String(length=32), nullable=False, server_default="ALL"),
        sa.Column("metrics", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "model_name", "model_version", "period_start", "period_end", "regime",
            name="uq_gap_backtest_model_period_regime",
        ),
    )
    op.create_index("ix_gap_backtest_results_model_name", "gap_backtest_results", ["model_name"])


def downgrade() -> None:
    op.drop_index("ix_gap_backtest_results_model_name", table_name="gap_backtest_results")
    op.drop_table("gap_backtest_results")
    op.drop_index("ix_gap_predictions_session_date", table_name="gap_predictions")
    op.drop_table("gap_predictions")
    op.drop_index("ix_gap_features_session_date", table_name="gap_features")
    op.drop_table("gap_features")
    op.drop_index("ix_gap_option_chain_snapshots_session_date", table_name="gap_option_chain_snapshots")
    op.drop_table("gap_option_chain_snapshots")
    op.drop_index("ix_gap_underlying_snapshots_session_date", table_name="gap_underlying_snapshots")
    op.drop_table("gap_underlying_snapshots")
    op.drop_index("ix_gap_prediction_sessions_session_date", table_name="gap_prediction_sessions")
    op.drop_table("gap_prediction_sessions")
