"""The delivery indexes from revision c1d2e3f4a5b6 exist at head, and only there.

Runs against whichever engine the shared fixture provides: a per-test SQLite
file, or a per-test PostgreSQL database when ``RC_TEST_DATABASE_URL`` is set.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tests.test_email_delivery_retry import engine  # re-exported fixture

BACKEND = Path(__file__).resolve().parents[1]

INDEXES = {
    "ix_delivery_status_lease": ("email_deliveries", ["status", "lease_expires_at"], False),
    "ix_delivery_status_payload_expiry": (
        "email_deliveries", ["status", "payload_expires_at"], False,
    ),
    "uq_evidence_provider_event": (
        "email_delivery_evidence", ["source", "evidence_source"], True,
    ),
}

HEAD = "c1d2e3f4a5b6"
BEFORE = "a8b3d5e7c902"


def _migrate(eng, revision: str, direction: str) -> None:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", "sqlite://")  # unused: connectable wins
    cfg.attributes["connectable"] = eng
    (command.upgrade if direction == "up" else command.downgrade)(cfg, revision)


def _version(eng) -> str:
    with eng.connect() as conn:
        return conn.execute(text("select version_num from alembic_version")).scalar_one()


def _indexes(eng) -> dict:
    """name -> (table, columns, unique) for the three indexes, if present."""
    found: dict = {}
    names = list(INDEXES)

    if eng.dialect.name == "sqlite":
        with eng.connect() as conn:
            for name, (table, _cols, _uniq) in INDEXES.items():
                listed = [
                    row for row in conn.exec_driver_sql(f"pragma index_list('{table}')").fetchall()
                    if row[1] == name
                ]
                if not listed:
                    continue
                cols = [r[2] for r in conn.exec_driver_sql(f"pragma index_info('{name}')")]
                found[name] = (table, cols, bool(listed[0][2]))
        return found

    with eng.connect() as conn:
        rows = conn.execute(
            text(
                """
                select i.relname, t.relname, ix.indisunique,
                       (select array_agg(a.attname order by k.ord)
                          from unnest(ix.indkey) with ordinality as k(attnum, ord)
                          join pg_attribute a
                            on a.attrelid = t.oid and a.attnum = k.attnum)
                from pg_index ix
                join pg_class i on i.oid = ix.indexrelid
                join pg_class t on t.oid = ix.indrelid
                where i.relname in (:n1, :n2, :n3)
                """
            ),
            dict(zip(("n1", "n2", "n3"), names)),
        ).all()
    for name, table, unique, cols in rows:
        found[name] = (table, list(cols), bool(unique))
    return found


def test_delivery_indexes_exist_at_head_and_are_dropped_on_downgrade(engine):
    assert _version(engine) == HEAD

    expected = {
        name: (table, cols, unique)
        for name, (table, cols, unique) in INDEXES.items()
    }
    assert _indexes(engine) == expected, f"missing or wrong at head: {_indexes(engine)}"

    _migrate(engine, BEFORE, "down")
    assert _version(engine) == BEFORE
    assert _indexes(engine) == {}, f"indexes survived downgrade: {_indexes(engine)}"

    _migrate(engine, "head", "up")
    assert _version(engine) == HEAD
    assert _indexes(engine) == expected
