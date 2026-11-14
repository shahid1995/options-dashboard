"""A fresh SQLite application connection actually enforces foreign keys.

SQLite defaults ``foreign_keys`` to OFF, which makes every foreign key in the
schema inert. ``app.db._engine()`` turns enforcement on from its single connect
listener; this module proves that the pragma a connection actually carries is
ON, and that a violating write fails instead of being silently accepted.

The point is the *connection state*, not the schema: a migration or a test that
pragmas its own connection would pass even if the application listener were
missing, which is exactly the gap this covers.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app import db


def _scratch_engine(monkeypatch, tmp_path):
    """An engine built by the application factory against a throwaway file."""
    monkeypatch.setattr(db.settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'fk.db'}")
    return db._engine()


@pytest.fixture()
def engine(monkeypatch, tmp_path):
    eng = _scratch_engine(monkeypatch, tmp_path)
    try:
        yield eng
    finally:
        eng.dispose()


def test_fresh_connection_has_foreign_keys_enabled(engine):
    """The pragma the listener sets is the value the connection reports."""
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_fresh_connection_uses_wal(engine):
    """The WAL pragma still applies now that the listener is registered once."""
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
        # synchronous=NORMAL is 1
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 1


def test_fresh_connection_rejects_a_foreign_key_violation(engine):
    """Enforcement is real: an orphan child row cannot be written."""
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE fk_parent (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql(
            "CREATE TABLE fk_child ("
            "id INTEGER PRIMARY KEY, "
            "parent_id INTEGER NOT NULL REFERENCES fk_parent(id))"
        )

    with engine.connect() as conn:
        with pytest.raises(IntegrityError):
            conn.exec_driver_sql("INSERT INTO fk_child (parent_id) VALUES (999)")
            conn.commit()

    # The rejection is not a stuck transaction: a valid child still writes.
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO fk_parent (id) VALUES (1)")
        conn.exec_driver_sql("INSERT INTO fk_child (parent_id) VALUES (1)")
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM fk_child").scalar() == 1


def test_every_pooled_connection_carries_the_pragma(engine):
    """The pragma comes from the listener, so it is not first-connection-only."""
    seen = []
    for _ in range(3):
        with engine.connect() as conn:
            seen.append(conn.exec_driver_sql("PRAGMA foreign_keys").scalar())
    assert seen == [1, 1, 1]
