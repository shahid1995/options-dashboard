"""A fresh SQLite application connection actually enforces foreign keys.

SQLite defaults ``foreign_keys`` to OFF, which makes every foreign key in the
schema inert. ``app.db._engine()`` turns enforcement on from its single connect
listener; this module proves that the pragma a connection actually carries is
ON, and that a violating write fails instead of being silently accepted.

The point is the *connection state*, not the schema: a migration or a test that
pragmas its own connection would pass even if the application listener were
missing, which is exactly the gap this covers.
"""

import logging
import sqlite3

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


# ---------------------------------------------------------------------------
# Legacy-database guard
# ---------------------------------------------------------------------------


def _legacy_db(path, *, orphan: bool) -> str:
    """A pre-existing SQLite file, written without any enforcement."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            "CREATE TABLE legacy_parent (id INTEGER PRIMARY KEY);"
            "CREATE TABLE legacy_child ("
            "id INTEGER PRIMARY KEY, "
            "parent_id INTEGER NOT NULL REFERENCES legacy_parent(id));"
        )
        if orphan:
            conn.execute("INSERT INTO legacy_child (id, parent_id) VALUES (1, 999)")
        conn.commit()
    finally:
        conn.close()
    return str(path)


def test_legacy_database_with_violations_is_not_enforced(monkeypatch, tmp_path, caplog):
    """A dirty legacy file keeps working: enforcement is skipped, not applied."""
    path = _legacy_db(tmp_path / "legacy_dirty.db", orphan=True)
    monkeypatch.setattr(db.settings, "DATABASE_URL", f"sqlite:///{path}")

    with caplog.at_level(logging.ERROR, logger="app.db"):
        eng = db._engine()
    try:
        with eng.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0
        # The write that used to succeed must still succeed.
        with eng.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO legacy_child (id, parent_id) VALUES (2, 998)"
            )
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM legacy_child").scalar() == 2
    finally:
        eng.dispose()

    assert db._SQLITE_FK_STATE[path] == "violations"
    assert any("foreign-key enforcement is OFF" in r.message for r in caplog.records)


def test_legacy_database_without_violations_is_enforced(monkeypatch, tmp_path):
    """A clean pre-existing file is enforced like any other."""
    path = _legacy_db(tmp_path / "legacy_clean.db", orphan=False)
    monkeypatch.setattr(db.settings, "DATABASE_URL", f"sqlite:///{path}")

    eng = db._engine()
    try:
        with eng.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        with eng.connect() as conn:
            with pytest.raises(IntegrityError):
                conn.exec_driver_sql(
                    "INSERT INTO legacy_child (id, parent_id) VALUES (3, 997)"
                )
                conn.commit()
    finally:
        eng.dispose()

    assert db._SQLITE_FK_STATE[path] == "enforced"


def test_preflight_reports_violation_counts(monkeypatch, tmp_path):
    """The guard reports what it found, so the error is actionable."""
    path = _legacy_db(tmp_path / "legacy_report.db", orphan=True)

    safe, count, tables = db._sqlite_fk_preflight(path)

    assert safe is False
    assert count == 1
    assert tables == ["legacy_child"]


def test_preflight_treats_a_missing_file_as_clean(tmp_path):
    """A database that does not exist yet cannot contain violations."""
    safe, count, tables = db._sqlite_fk_preflight(str(tmp_path / "absent.db"))

    assert (safe, count, tables) == (True, 0, [])
    assert not (tmp_path / "absent.db").exists()
