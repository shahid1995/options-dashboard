"""uq_users_broker_identity_active — retire the legacy stamp uniqueness gate

Revision ID: e5f6a7b8c9d0
Revises: d9e0f1a2b3c4
Create Date: 2026-09-14

Authorized by: StrikeNova Multi-Broker Identity Refactor (mission Phase 6,
minimum-migration path) + docs/architecture/UPSTOX_IDENTITY_LINKING_DESIGN.md
(§17.5: the legacy stamp is compatibility metadata).

Purpose
-------
The baseline schema enforced the legacy stamp as a full table-level unique
constraint on users (broker_provider, broker_user_id). Under the
multi-broker architecture that constraint is a residual one-user-one-broker
gate at the database level:

    BrokerConnection is the AUTHORITATIVE broker-ownership ledger; the
    users.broker_* stamp is compatibility metadata that must never
    prevent a legitimate additional connection (mission Phase 1 items
    7/8, Test G).

Concretely, with the constraint present, a user may legitimately adopt a
broker identity whose only trace is ANOTHER user's stale populated stamp
(no live ledger row exists). Populating the adopter's empty stamp then
duplicates the stale metadata pair and the flush raises IntegrityError —
blocking a connection the architecture explicitly allows. Stale stamps are
informational junk and may legitimately duplicate across users.

Therefore this migration REMOVES the stamp uniqueness on the production
engines (PostgreSQL / CockroachDB) instead of replacing it:

- The authoritative uniqueness backstop is NOT touched: the global
  broker-ownership index uq_broker_identity_global on broker_connections
  (migration d9e0f1a2b3c4, deployed to staging) remains exactly as-is,
  preserving both per-user sentinels ('pending', 'data-only') in its
  predicate (mission Phase 1 item 8).
- Corrupt-state detection (stamp vs ledger disagreement) stays in
  application code (find_broker_identity_owner) — fail closed, never
  reconciled.
- No users column is added, changed, or removed; the legacy columns stay
  (mission Phase 1 item 7).

Why not a partial unique index on populated stamps instead?  A partial
index has the identical key semantics as the old constraint among
non-NULL stamps, so it would reproduce the exact Test-G blocking failure
on every migrated database. The stamp simply cannot carry a uniqueness
guarantee in the multi-broker architecture.

SQLite note
-----------
SQLite cannot drop a constraint embedded in CREATE TABLE without a full
table rebuild (batch_alter_table recreate). Production never runs on
SQLite (staging/prod = CockroachDB/PostgreSQL; SQLite = test/dev create_all
databases, which no longer declare the constraint in ORM metadata), so the
residual constraint on migrated dev SQLite databases is accepted and
documented here rather than rebuilt at data-copy risk.

Pre-audit of existing data (Phase 6 requirement)
------------------------------------------------
Dropping a constraint never fails on existing data and never rewrites
rows. The removal admits a strict superset of the current row set — no
data migration is performed. broker_connections is not read or written by
this migration.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d9e0f1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEGACY_CONSTRAINT = "uq_users_broker_identity"


def upgrade() -> None:
    """Drop the legacy one-user-one-broker stamp constraint."""
    dialect = op.get_bind().dialect.name
    if dialect == "cockroachdb":
        # CockroachDB implements UNIQUE constraints as indexes and does not
        # support ``ALTER TABLE ... DROP CONSTRAINT`` for them
        # (cockroachdb/cockroach#42840: "cannot drop UNIQUE constraint
        # ... use DROP INDEX CASCADE instead"). Dropping the backing index
        # with CASCADE removes the constraint with it; nothing else depends
        # on this index (verified in the ADR-017 CockroachDB rehearsal).
        # CRDB index addressing uses ``table@index`` (``schema.index`` would
        # name a nonexistent schema).
        op.execute(f"DROP INDEX IF EXISTS users@{_LEGACY_CONSTRAINT} CASCADE")
    elif dialect == "postgresql":
        op.execute(
            f"ALTER TABLE users DROP CONSTRAINT IF EXISTS {_LEGACY_CONSTRAINT}"
        )
    else:
        # SQLite: the constraint is embedded in the CREATE TABLE DDL and
        # cannot be dropped without a full table rebuild. Production runs
        # PostgreSQL/CockroachDB; test/dev SQLite databases are built via
        # create_all, whose ORM metadata no longer declares the
        # constraint. See module docstring.
        pass


def downgrade() -> None:
    """Restore the exact historical schema (full table-level constraint)."""
    dialect = op.get_bind().dialect.name
    if dialect in ("postgresql", "cockroachdb"):
        op.execute(
            f"ALTER TABLE users ADD CONSTRAINT {_LEGACY_CONSTRAINT} "
            f"UNIQUE (broker_provider, broker_user_id)"
        )
    else:
        pass
