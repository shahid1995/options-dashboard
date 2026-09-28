"""Day 49 — normalize legacy IV observation timestamps to canonical IST.

Before Day 49, ``IVObservation.observed_at`` received UTC-aware datetimes while
the column itself is ``DateTime`` without timezone storage. Existing rows
therefore persist as naive UTC values. Day 49 standardizes persisted market
timestamps to naive IST.

This one-time data migration converts all pre-Day-49 IV rows from naive UTC to
naive IST. Future writes are already normalized by ``record_iv_observations``.
"""

from datetime import datetime, timedelta, timezone

from alembic import op
import sqlalchemy as sa

revision = "d49aa0000001"
down_revision = "d48aa0000001"
branch_labels = None
depends_on = None

IST = timezone(timedelta(hours=5, minutes=30))


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
    rows = bind.execute(sa.select(table.c.id, table.c.observed_at)).fetchall()
    for row in rows:
        if row.observed_at is None:
            continue
        bind.execute(
            table.update()
            .where(table.c.id == row.id)
            .values(observed_at=_legacy_utc_to_ist(row.observed_at))
        )


def downgrade() -> None:
    # Timestamp normalization is a one-way data correction. A downgrade of the
    # application must not reinterpret newly written canonical IST rows as UTC.
    pass