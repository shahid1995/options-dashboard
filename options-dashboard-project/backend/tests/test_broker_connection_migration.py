"""Verify Alembic migration: upgrade / downgrade / re-upgrade cycle.

This test exercises the full migration lifecycle against a temporary
SQLite database file:
  1. Upgrade to head — verify tables, indexes, server_defaults
  2. Downgrade to base — verify broker tables removed
  3. Re-upgrade to head — verify everything restored
"""

import pathlib
import os
import tempfile

import pytest
from sqlalchemy import create_engine, inspect
from alembic.config import Config
from alembic import command


@pytest.fixture()
def alembic_cfg(tmp_path):
    """Create an Alembic Config pointing at a temp SQLite database."""
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    cfg = Config(str(pathlib.Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


class TestBrokerConnectionMigration:
    """Full upgrade/downgrade/re-upgrade cycle for the broker connection migration."""

    def test_upgrade_downgrade_reupgrade(self, alembic_cfg):
        """Upgrade, downgrade, re-upgrade must all succeed cleanly."""
        # --- 1. Upgrade to head ---
        command.upgrade(alembic_cfg, "head")

        db_url = alembic_cfg.get_main_option("sqlalchemy.url")
        engine = create_engine(db_url)
        insp = inspect(engine)
        tables = set(insp.get_table_names())

        assert "broker_connections" in tables
        assert "broker_tokens" in tables
        assert "user_sessions" in tables

        # Partial unique index
        indexes = insp.get_indexes("broker_connections")
        index_names = [i["name"] for i in indexes]
        assert "uq_one_default_per_user_broker" in index_names

        # Server defaults — SQLite inspector wraps values in extra quotes
        bc_cols = {c["name"]: c for c in insp.get_columns("broker_connections")}
        # Compare against the raw value from inspector (may have surrounding quotes)
        assert "1" in str(bc_cols["is_default"].get("default", ""))
        assert "connected" in str(bc_cols["status"].get("default", ""))
        assert "trading" in str(bc_cols["capability_mode"].get("default", ""))
        assert "{}" in str(bc_cols["provider_metadata_json"].get("default", ""))

        # user_sessions.broker_connection_id
        us_cols = [c["name"] for c in insp.get_columns("user_sessions")]
        assert "broker_connection_id" in us_cols

        engine.dispose()
        print("PASS: upgrade — tables, indexes, server_defaults, FK verified")

        # --- 2. Downgrade to base ---
        # CodeRabbit #2 (Day 49): the head revision d49aa0000001 refuses
        # downgrade (intentionally irreversible UTC→IST data normalization).
        # The refusal is atomic — raised before any revision rewinds — so the
        # database stays at head with the schema fully intact.
        with pytest.raises(NotImplementedError) as excinfo:
            command.downgrade(alembic_cfg, "base")
        assert "irreversible" in str(excinfo.value).lower()

        engine2 = create_engine(db_url)
        insp2 = inspect(engine2)
        tables2 = set(insp2.get_table_names())

        # Refused downgrade must not have dropped anything.
        assert "broker_connections" in tables2
        assert "broker_tokens" in tables2

        engine2.dispose()
        print("PASS: downgrade refused — schema intact")

        # --- 3. Re-upgrade to head (idempotent after refusal) ---
        command.upgrade(alembic_cfg, "head")

        engine3 = create_engine(db_url)
        insp3 = inspect(engine3)
        tables3 = set(insp3.get_table_names())

        assert "broker_connections" in tables3
        assert "broker_tokens" in tables3
        assert "user_sessions" in tables3

        indexes3 = insp3.get_indexes("broker_connections")
        index_names3 = [i["name"] for i in indexes3]
        assert "uq_one_default_per_user_broker" in index_names3

        us_cols3 = [c["name"] for c in insp3.get_columns("user_sessions")]
        assert "broker_connection_id" in us_cols3

        engine3.dispose()
        print("PASS: re-upgrade — everything restored")
