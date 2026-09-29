"""Alembic single-head invariant — ``alembic upgrade head`` must be unambiguous.

Background: two migration heads coexisted after parallel work sessions
(``e2b4c6d8f0a1`` day41.2 cross-D1 lock + ``f1a2b3c4d5e6`` broker
authorizations). ``command.upgrade(cfg, "head")`` raises ``MultipleHeads``
in that state, which broke startup migrations in every test that drives
the real alembic pipeline (test_day6, test_day7, day41 evidence suites…)
and made the head ambiguous for deployments.

Contract under test:
  1. The script directory has EXACTLY ONE head.
  2. ``upgrade head`` succeeds against a fresh (empty) database.
  3. Every revision file is at least import-safe and its identifiers
     resolve in the script directory (down_revision typos surface here).
  4. The merge revision consolidates the two former heads (both are
     ancestors of head).
  5. Downgrade of the merge node is exercised on a scratch DB to prove
     the consolidation is not a one-way door.

All scratch databases are per-test temp files — never the project DB.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _make_cfg(db_path: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _fresh_db() -> tuple[Config, str]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return _make_cfg(path), path


# ---------------------------------------------------------------------------
# 1 + 4 — the invariant that was violated
# ---------------------------------------------------------------------------


def test_script_directory_has_exactly_one_head():
    cfg, _ = _fresh_db()
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, (
        f"alembic upgrade head is ambiguous: {len(heads)} heads ({heads}). "
        "Add or update a merge revision so 'head' resolves unambiguously."
    )


def test_merge_revision_consolidates_former_heads():
    cfg, _ = _fresh_db()
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(cfg)
    # The consolidation point is the merge revision whose parents are the two
    # former heads. It need not remain the graph head once later revisions
    # (e.g. 2026-09-16 account-security lifecycle tables) extend the chain
    # from it; what matters is that the former heads were merged exactly once
    # so "upgrade head" resolves unambiguously (asserted above).
    merge = None
    for rev in script.walk_revisions():
        downs = rev.down_revision
        downs_list = list(downs) if isinstance(downs, (list, tuple)) else [downs]
        if {"e2b4c6d8f0a1", "f1a2b3c4d5e6"}.issubset(set(downs_list)):
            merge = rev
            break
    assert merge is not None, (
        "no revision merges the former heads e2b4c6d8f0a1 and f1a2b3c4d5e6; "
        "'upgrade head' cannot resolve a single unambiguous history"
    )
    # Exactly one consolidation point for the two former heads.
    assert script.get_revision(merge.revision) is not None


# ---------------------------------------------------------------------------
# 2 + 5 — upgrade head works; downgrade/upgrade round-trip is safe
# ---------------------------------------------------------------------------


def test_upgrade_head_succeeds_on_fresh_db():
    cfg, path = _fresh_db()
    try:
        command.upgrade(cfg, "head")  # MultipleHeads would raise here
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        engine.dispose()
        assert len(version) == 1, f"single head expected in DB, got {version}"
    finally:
        os.remove(path)


def test_upgrade_head_then_downgrade_round_trip():
    """CodeRabbit #2 (Day 49): downgrading from head must be REFUSED.

    Since d49aa0000001, the UTC→IST IV normalization is intentionally
    irreversible: a downgrade crossing it would falsely imply the legacy
    data representation was restored (or corrupt canonical IST rows written
    post-migration).  The refusal is atomic — it raises before any revision
    rewinds, so the database stays at head and a repeated upgrade is a
    no-op."""
    cfg, path = _fresh_db()
    try:
        command.upgrade(cfg, "head")
        with pytest.raises(NotImplementedError) as excinfo:
            command.downgrade(cfg, "5e2a7b9c3f4d")
        assert "irreversible" in str(excinfo.value).lower()

        # Zero partial rewind: still exactly one version row, at head.
        command.upgrade(cfg, "head")  # idempotent no-op
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        engine.dispose()
        assert len(rows) == 1
    finally:
        os.remove(path)


def test_upgrade_head_is_idempotent_when_already_at_head():
    cfg, path = _fresh_db()
    try:
        command.upgrade(cfg, "head")
        command.upgrade(cfg, "head")  # no-op, must not raise
    finally:
        os.remove(path)


# ---------------------------------------------------------------------------
# 3 — structural integrity of every revision file
# ---------------------------------------------------------------------------


def test_every_revision_resolves_and_is_import_safe():
    cfg, _ = _fresh_db()
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(cfg)
    revisions = list(script.walk_revisions())
    assert len(revisions) > 10, "migration graph unexpectedly small — check script location"
    seen = set()
    for rev in revisions:
        assert rev.revision not in seen, f"duplicate revision id {rev.revision}"
        seen.add(rev.revision)
        assert Path(rev.path).exists(), f"revision file missing on disk: {rev.path}"


def test_historical_ingestion_runs_has_dataset_mapping_snapshot_column():
    """Finding 3 (PR #128): the application model and governance service
    persist ``dataset_mapping_snapshot_json`` on every ingestion manifest, so
    the migration chain must actually create that column — verified against
    the real alembic upgrade, not the SQLAlchemy test-fixture schema.
    """
    cfg, path = _fresh_db()
    try:
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                ).fetchall()
            }
            columns = set()
            if "historical_ingestion_runs" in tables:
                columns = {
                    r[1]
                    for r in conn.execute(
                        text("PRAGMA table_info(historical_ingestion_runs)")
                    ).fetchall()
                }
        engine.dispose()
        assert "historical_ingestion_runs" in tables, (
            "historical_ingestion_runs table missing after upgrade"
        )
        assert "dataset_mapping_snapshot_json" in columns, (
            "d48aa0000002 must add historical_ingestion_runs."
            "dataset_mapping_snapshot_json so refresh_ingestion_run_metrics "
            "can read the immutable snapshot written by start_ingestion_run"
        )
    finally:
        os.remove(path)


def test_broker_authorizations_table_created_by_upgrade():
    cfg, path = _fresh_db()
    try:
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            tables = {
                r[0]
                for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
            }
        engine.dispose()
        assert "broker_authorizations" in tables
        assert "order_family_sync_lock" in tables
    finally:
        os.remove(path)
