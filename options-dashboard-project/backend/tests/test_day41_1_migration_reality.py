"""Day41.1 §5 — Migration-reality verification (real PostgreSQL ONLY).

The audit requirement: migration correctness is proven by running the REAL
Alembic chain against a disposable PostgreSQL database and inspecting the
resulting schema from the database catalog — NEVER by
``Base.metadata.create_all`` (which fabricates schema from the ORM and can
mask ORM↔migration drift such as the missing ``duplicate_of`` column).

Verified here, from the ACTUAL PostgreSQL schema:
- every required column: duplicate_of, observed_count, raw_observation_id,
  fill_eq_key, all alias columns, all lineage columns
- every index and PK on the five Day41 tables
- ORM ↔ actual-PostgreSQL agreement for columns/indexes/PKs (the full matrix)
- alembic_version is a single row at the Day41 head (b3e5f8a1c7d2)
- the fill-ledger functions WORK against the migrated schema (the missing
  duplicate_of column would break Lane-C duplicate classification INSERTs)
- upgrade AND downgrade of the Day41 chain both succeed on PostgreSQL

Skipped unless TEST_DATABASE_URL points at PostgreSQL.  The database must
already exist and be EMPTY (the harness creates a disposable one); tests run
the full Alembic chain from base via the alembic command API.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

DB_URL = os.getenv("TEST_DATABASE_URL", "")
if not DB_URL or not DB_URL.startswith(("postgresql+psycopg://", "postgresql://")):
    pytest.skip(
        "TEST_DATABASE_URL must point at an EMPTY disposable PostgreSQL "
        "database for migration-reality verification",
        allow_module_level=True,
    )

ENGINE = create_engine(DB_URL, pool_pre_ping=True)
TestSession = sessionmaker(bind=ENGINE, expire_on_commit=False)

# Tracks the ACTUAL current chain head.  Day41.2 (cross-D1 family lock +
# S2 evidence) extended the chain: b3e5f8a1c7d2 (Day41.1) is now an ancestor.
DAY41_HEAD = "e2b4c6d8f0a1"
PRE_DAY41_BASE = "f7aa24156f6d"  # revision the Day41 chain extends

DAY41_TABLES = (
    "broker_raw_observation",
    "broker_fill_ledger_observation",
    "broker_fill_ledger_fill",
    "broker_fill_identity_alias",
    "broker_fill_identity_lineage",
)

# Audit-required field list (§5) — every one MUST exist in the real schema.
REQUIRED_COLUMNS: dict[str, set[str]] = {
    "broker_fill_ledger_observation": {
        "observation_id", "raw_observation_id", "tenant_id", "broker",
        "provider_order_id", "observation_class", "d1", "content_fingerprint",
        "provider_trade_id", "fill_eq_key", "fill_quantity", "fill_price",
        "cumulative_after", "provider_status", "source_mode", "received_at",
        "raw_payload_excerpt", "reconciliation_state", "duplicate_of",
        "observed_count", "created_at",
    },
    "broker_fill_ledger_fill": {
        "tenant_id", "provider_order_id", "fill_eq_key",
        "fill_identity_type", "reconciliation_state", "canonical_id",
        "fill_quantity", "fill_price", "cumulative_after", "observed_count",
        "frozen_reason", "created_at", "updated_at",
    },
    "broker_fill_identity_alias": {
        "tenant_id", "provider_order_id", "from_eq_key", "to_eq_key",
        "alias_state", "created_at", "last_transition_at",
    },
    "broker_fill_identity_lineage": {
        "lineage_id", "tenant_id", "provider_order_id", "observation_id",
        "from_eq_key", "to_eq_key", "upgrade_trigger", "outcome",
        "trade_ids", "evidence_ref", "observed_at", "recorded_at",
    },
    "broker_raw_observation": {
        "raw_observation_id", "tenant_id", "broker", "received_at",
        "source_mode", "raw_payload", "delivery_evidence",
        "provider_order_id", "provider_trade_id", "d1",
        "content_fingerprint", "ingestion_status", "processing_status",
        "attempt_count", "last_error", "created_at",
        "processing_completed_at", "lease_expires_at",
    },
}


def _alembic_cfg():
    from alembic import command  # noqa: F401 — import verifies availability
    from alembic.config import Config

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))
    cfg.set_main_option("sqlalchemy.url", DB_URL)
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="module")
def migrated_pg():
    """Run the REAL Alembic chain base→head on the disposable database."""
    from alembic import command

    cfg = _alembic_cfg()
    command.upgrade(cfg, "head")
    yield
    # Downgrade-consistency proof: the Day41 chain must reverse cleanly.
    command.downgrade(cfg, PRE_DAY41_BASE)


# ---------------------------------------------------------------------------
# Actual-PostgreSQL schema verification (from the catalog, not the ORM)
# ---------------------------------------------------------------------------

def test_alembic_head_is_day41_single_row(migrated_pg) -> None:
    with ENGINE.connect() as conn:
        rows = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).fetchall()
    assert len(rows) == 1, f"expected a single alembic_version row, got {rows}"
    assert rows[0][0] == DAY41_HEAD


def test_day41_tables_exist_in_actual_pg_schema(migrated_pg) -> None:
    insp = inspect(ENGINE)
    present = set(insp.get_table_names())
    missing = [t for t in DAY41_TABLES if t not in present]
    assert missing == [], f"Day41 tables missing from real schema: {missing}"


@pytest.mark.parametrize("table", sorted(DAY41_TABLES))
def test_audit_required_columns_in_real_pg_schema(migrated_pg, table) -> None:
    insp = inspect(ENGINE)
    actual = {c["name"] for c in insp.get_columns(table)}
    missing = sorted(REQUIRED_COLUMNS[table] - actual)
    assert missing == [], (
        f"{table}: columns missing from REAL PostgreSQL schema: {missing} "
        f"(this is exactly the class of defect the create_all-based tests hid)"
    )


def test_duplicate_of_column_is_real_and_usable(migrated_pg) -> None:
    """The audit's headline finding: duplicate_of must exist in the actual
    migrated schema — proven here by inserting through it."""
    with ENGINE.begin() as conn:
        conn.execute(text(
            "INSERT INTO broker_fill_ledger_observation "
            "(observation_id, tenant_id, broker, provider_order_id, "
            " observation_class, d1, content_fingerprint, source_mode, "
            " received_at, reconciliation_state, duplicate_of, observed_count, "
            " created_at) "
            "VALUES ('obs-dup-test', 't-mig', 'UPSTOX', 'O-MIG', 'ECONOMIC_FILL', "
            " 'D-MIG', 'FP-MIG', 'STREAM', NOW(), 'DUPLICATE', 'obs-prior', 1, NOW())"
        ))
    with ENGINE.connect() as conn:
        val = conn.execute(text(
            "SELECT duplicate_of FROM broker_fill_ledger_observation "
            "WHERE observation_id = 'obs-dup-test'"
        )).scalar_one()
    with ENGINE.begin() as conn:
        conn.execute(text(
            "DELETE FROM broker_fill_ledger_observation "
            "WHERE observation_id = 'obs-dup-test'"
        ))
    assert val == "obs-prior"


def test_orm_matches_actual_postgres_schema_full_matrix(migrated_pg) -> None:
    """Full ORM ↔ REAL-PostgreSQL matrix (audit §5.3): columns, indexes and
    PKs of the five Day41 tables agree with Base.metadata.  This is the
    authoritative reality check — create_all is never consulted."""
    from app.db import Base
    import app.models  # noqa: F401
    import app.broker_sync.models  # noqa: F401
    import app.broker_sync.raw_ingress  # noqa: F401
    import app.broker_sync.fill_ledger  # noqa: F401

    insp = inspect(ENGINE)
    problems: list[str] = []
    for table in DAY41_TABLES:
        md = Base.metadata.tables[table]
        actual_cols = {c["name"] for c in insp.get_columns(table)}
        orm_cols = set(md.columns.keys())
        for c in sorted(orm_cols - actual_cols):
            problems.append(f"{table}: ORM column {c} MISSING in PG")
        for c in sorted(actual_cols - orm_cols):
            problems.append(f"{table}: PG column {c} not in ORM")

        actual_idx = {ix["name"]: tuple(ix["column_names"]) for ix in insp.get_indexes(table)}
        for ix in md.indexes:
            if ix.name not in actual_idx:
                problems.append(f"{table}: index {ix.name} MISSING in PG")
            elif tuple(actual_idx[ix.name]) != tuple(ix.columns.keys()):
                problems.append(
                    f"{table}: index {ix.name} columns {actual_idx[ix.name]} != ORM {tuple(ix.columns.keys())}"
                )

        actual_pk = tuple(insp.get_pk_constraint(table)["constrained_columns"])
        orm_pk = tuple(md.primary_key.columns.keys())
        if set(actual_pk) != set(orm_pk):
            problems.append(f"{table}: PK {actual_pk} != ORM {orm_pk}")

    assert problems == [], "ORM↔PG schema drift:\n  " + "\n  ".join(problems)


# ---------------------------------------------------------------------------
# Exercise the fill-ledger functions against the MIGRATED schema
# ---------------------------------------------------------------------------

def test_fill_ledger_functions_work_on_migrated_schema(migrated_pg) -> None:
    """End-to-end against the real migrated schema: Lane B apply → duplicate
    replay → Lane C ambiguity → alias upgrade.  The original duplicate_of
    omission would break the Lane-C DUPLICATE path (NULL column absent ⇒
    ProgrammingError on INSERT)."""
    from datetime import datetime, timezone

    from app.broker_sync.fill_ledger import (
        AliasState,
        ObservationClass,
        ReconciliationState,
        BrokerFillIdentityAlias,
        BrokerFillLedgerFill,
        BrokerFillLedgerObservation,
        apply_lane_b_fill,
        composite_eq_key,
        evaluate_lane_c_equivalence,
        record_fill_observation,
        trade_eq_key,
        upgrade_composite_to_trade,
    )

    ts = datetime(2026, 9, 11, 9, 0, 0, tzinfo=timezone.utc)
    s = TestSession()

    # Lane B: first applier, then duplicate replay.
    row1, out1, obs1 = apply_lane_b_fill(
        s, tenant_id="t-mig", broker="UPSTOX", provider_order_id="O-MIG-B",
        provider_trade_id="T-MIG", d1="D-MIG-B", content_fingerprint="FP-MIG-B",
        source_mode="STREAM", received_at=ts, fill_quantity=5,
    )
    s.commit()
    assert out1 == "APPLIED"
    _, out2, dup_obs = apply_lane_b_fill(
        s, tenant_id="t-mig", broker="UPSTOX", provider_order_id="O-MIG-B",
        provider_trade_id="T-MIG", d1="D-MIG-B", content_fingerprint="FP-MIG-B",
        source_mode="RECOVERY", received_at=ts, fill_quantity=5,
    )
    s.commit()
    assert out2 == "DUPLICATE_FILL"
    assert dup_obs.duplicate_of == obs1.observation_id  # exercises duplicate_of
    assert dup_obs.reconciliation_state == ReconciliationState.DUPLICATE.value

    # Lane C: no-ID fill → AMBIGUOUS composite.
    obs = record_fill_observation(
        s, tenant_id="t-mig", broker="UPSTOX", provider_order_id="O-MIG-C",
        observation_class=ObservationClass.ECONOMIC_FILL, d1="D-MIG-C",
        content_fingerprint="FP-MIG-C", source_mode="STREAM", received_at=ts,
        fill_quantity=5,
    )
    s.commit()
    assert evaluate_lane_c_equivalence(s, obs) == ReconciliationState.AMBIGUOUS.value
    s.commit()

    # Alias upgrade on the migrated schema.
    created = upgrade_composite_to_trade(
        s, tenant_id="t-mig", provider_order_id="O-MIG-C",
        composite_key=composite_eq_key("t-mig", "O-MIG-C", "D-MIG-C"),
        trade_ids=["T-MIG-C"], trigger="TRADE_HISTORY",
        observation_ids=[obs.observation_id],
    )
    s.commit()
    s.close()

    check = TestSession()
    try:
        assert created[0].fill_eq_key == trade_eq_key("T-MIG-C")
        alias = check.execute(
            select_alias("t-mig", "O-MIG-C")
        ).scalar_one()
        assert alias.alias_state == AliasState.AUTHORITATIVE.value
        composite = check.execute(
            select_composite("t-mig", "O-MIG-C")
        ).scalar_one()
        assert composite.reconciliation_state == ReconciliationState.SUPERSEDED.value
        n_obs = len(check.query(BrokerFillLedgerObservation).all())
        assert n_obs >= 3  # Lane-B RECONCILED + Lane-B DUPLICATE + Lane-C AMBIGUOUS
    finally:
        check.close()


def select_alias(tenant, order):
    from sqlalchemy import select

    from app.broker_sync.fill_ledger import BrokerFillIdentityAlias
    return select(BrokerFillIdentityAlias).where(
        BrokerFillIdentityAlias.tenant_id == tenant,
        BrokerFillIdentityAlias.provider_order_id == order,
    )


def select_composite(tenant, order):
    from sqlalchemy import select

    from app.broker_sync.fill_ledger import BrokerFillLedgerFill
    return select(BrokerFillLedgerFill).where(
        BrokerFillLedgerFill.tenant_id == tenant,
        BrokerFillLedgerFill.provider_order_id == order,
        BrokerFillLedgerFill.fill_identity_type == "COMPOSITE",
    )
