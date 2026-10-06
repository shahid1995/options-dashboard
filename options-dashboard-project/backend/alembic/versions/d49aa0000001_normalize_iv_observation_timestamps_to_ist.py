"""Day 49 — normalize legacy IV observation timestamps to canonical IST.

Before Day 49, ``IVObservation.observed_at`` received UTC-aware datetimes while
the column itself is ``DateTime`` without timezone storage. Existing rows
therefore persist as naive UTC values. Day 49 standardizes persisted market
timestamps to naive IST.

This one-time data migration converts all pre-Day-49 IV rows from naive UTC to
naive IST. Future writes are already normalized by ``record_iv_observations``.

Scale (CodeRabbit #1): the upgrade reads its input in bounded batches
(``IV_MIGRATION_BATCH_SIZE`` rows per server-side paged batch — the full table
is never materialized) and writes each batch with one ``executemany``
statement, so memory stays bounded and the statement count is proportional to
table size rather than one statement per row.  The per-row transformation,
NULL handling, and dialect-agnostic semantics are unchanged (one portable
batched path for SQLite, PostgreSQL, and CockroachDB alike).

Reversibility (CodeRabbit #2): the normalization is intentionally
irreversible.  Rows written after this migration already carry canonical
naive-IST values, so a blind reverse conversion would corrupt them; the
downgrade therefore refuses explicitly instead of silently doing nothing.
"""

from datetime import datetime, timedelta, timezone

from alembic import op
import sqlalchemy as sa

revision = "d49aa0000001"
# Isolated Day-49-only reconstruction: the merged Day-48 base added
# d48aa0000002 after this migration was originally written, so d49 chains
# onto the merged Day-48 head to keep a single linear history (Alembic
# single-head invariant).
down_revision = "d48aa0000002"
branch_labels = None
depends_on = None

IST = timezone(timedelta(hours=5, minutes=30))

#: Bounded batch window for reading (and rewriting) the IV history.  Each
#: batch is selected server-side with ``WHERE id > :last ORDER BY id LIMIT
#: :batch`` so the full result set is never materialized in memory
#: (CodeRabbit #1).
IV_MIGRATION_BATCH_SIZE = 500


def _paged_batches(bind, table):
    """Yield server-side paged batches of rows for non-NULL observations.

    Keyset pagination on ``id``: each round trip fetches at most
    ``IV_MIGRATION_BATCH_SIZE`` rows, so the full history is never held in
    memory regardless of table size.
    """
    last_id = -1
    while True:
        rows = bind.execute(
            sa.select(table.c.id, table.c.observed_at)
            .where(
                table.c.id > last_id,
                table.c.observed_at.isnot(None),
            )
            .order_by(table.c.id)
            .limit(IV_MIGRATION_BATCH_SIZE)
        ).fetchall()
        if not rows:
            return
        last_id = rows[-1].id
        yield rows


def _upgrade_batched(bind, table) -> None:
    """Batched update used for every dialect.

    Each paged batch is rewritten with one ``executemany`` call — bounded
    memory, statement count proportional to table size (not one statement
    per row), and the exact same Python-side IST conversion as before.
    """
    for rows in _paged_batches(bind, table):
        payload = [
            {
                "b_id": row.id,
                "b_observed_at": _legacy_utc_to_ist(row.observed_at),
            }
            for row in rows
        ]
        bind.execute(
            table.update()
            .where(table.c.id == sa.bindparam("b_id"))
            .values(observed_at=sa.bindparam("b_observed_at")),
            payload,
        )


def _legacy_utc_to_ist(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(IST).replace(tzinfo=None)
    return value + timedelta(hours=5, minutes=30)


def upgrade() -> None:
    bind = op.get_bind()
    table = sa.table(
        "iv_observations",
        sa.column("id", sa.Integer),
        sa.column("observed_at", sa.DateTime),
    )
    _upgrade_batched(bind, table)


def downgrade() -> None:
    # Timestamp normalization is a one-way data correction (CodeRabbit #2):
    # a downgrade of the application must not reinterpret newly written
    # canonical IST rows as UTC, and it cannot distinguish them from
    # pre-migration rows — so it refuses explicitly instead of silently
    # implying the legacy representation was restored.
    raise NotImplementedError(
        "d49aa0000001 downgrade is intentionally unsupported: the legacy "
        "UTC to canonical naive-IST normalization of iv_observations."
        "observed_at is irreversible. Rows written after this migration "
        "already carry canonical naive-IST timestamps, so a reverse "
        "conversion cannot distinguish them from pre-migration rows and "
        "would corrupt the canonical timestamp contract. Restore from a "
        "pre-migration backup if the legacy representation is required."
    )