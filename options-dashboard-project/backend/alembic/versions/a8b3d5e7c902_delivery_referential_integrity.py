"""Delivery referential integrity: composite ownership and pointer keys.

Adds the four cross-table foreign keys that the history queries and the
transaction writers rely on:

    fk_attempt_owner              attempts (delivery_id, user_id, principal_id)
                                     -> deliveries (id, user_id, principal_id)
    fk_attempt_generation         attempts (delivery_id, generation_id)
                                     -> generations (delivery_id, id)
    fk_evidence_attempt           evidence (delivery_id, attempt_id)
                                     -> attempts (delivery_id, id)
    fk_delivery_active_generation deliveries (id, active_generation_id)
                                     -> generations (delivery_id, id)

The last one replaces the CockroachDB partial unique index that earlier design
drafts proposed: "one active generation" is now carried by a single-valued,
referentially checked pointer column instead of by a dialect-specific index
predicate that the cockroachdb compiler silently drops.

Each foreign key has a preflight that refuses to run against data that would
violate it, so the upgrade fails loudly instead of partially applying.

Revision ID: a8b3d5e7c902
Revises: f2a7c9d1e4b8
"""

from alembic import op
import sqlalchemy as sa


revision = "a8b3d5e7c902"
down_revision = "f2a7c9d1e4b8"
branch_labels = None
depends_on = None


REQUIRED_TABLES = (
    "email_deliveries",
    "email_delivery_generations",
    "email_delivery_attempts",
    "email_delivery_evidence",
)


def _scalar(bind, sql: str):
    return bind.execute(sa.text(sql)).scalar()


def _fail(check: str, count: int) -> None:
    raise RuntimeError(f"preflight {check} failed: {count} row(s) violate the key")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table in REQUIRED_TABLES:
        if not inspector.has_table(table):
            raise RuntimeError(f"preflight: required table {table} is missing")

    # P1  every pointer must resolve inside its own delivery
    n = _scalar(
        bind,
        """
        SELECT COUNT(*)
        FROM email_deliveries d
        LEFT JOIN email_delivery_generations g
               ON g.id = d.active_generation_id
              AND g.delivery_id = d.id
        WHERE d.active_generation_id IS NOT NULL
          AND g.id IS NULL
        """,
    )
    if n:
        _fail("P1 active pointer resolves outside its delivery", n)

    # P2  no attempt may name a generation belonging to another delivery
    n = _scalar(
        bind,
        """
        SELECT COUNT(*)
        FROM email_delivery_attempts a
        JOIN email_delivery_generations g ON g.id = a.generation_id
        WHERE g.delivery_id <> a.delivery_id
        """,
    )
    if n:
        _fail("P2 attempt references a foreign generation", n)

    # P3  no attempt may carry ownership that disagrees with its delivery
    n = _scalar(
        bind,
        """
        SELECT COUNT(*)
        FROM email_delivery_attempts a
        JOIN email_deliveries d ON d.id = a.delivery_id
        WHERE a.user_id <> d.user_id
           OR a.principal_id <> d.principal_id
        """,
    )
    if n:
        _fail("P3 attempt ownership drifted from its delivery", n)

    # P4  every evidence row must name an attempt of the same delivery
    n = _scalar(
        bind,
        """
        SELECT COUNT(*)
        FROM email_delivery_evidence e
        LEFT JOIN email_delivery_attempts a
               ON a.id = e.attempt_id
              AND a.delivery_id = e.delivery_id
        WHERE a.id IS NULL
        """,
    )
    if n:
        _fail("P4 evidence has no attempt in its own delivery", n)

    # P5  no committed delivery may lack an active generation
    n = _scalar(
        bind,
        "SELECT COUNT(*) FROM email_deliveries WHERE active_generation_id IS NULL",
    )
    if n:
        _fail("P5 committed delivery has a NULL active pointer", n)

    # Ownership and generation keys first: the pointer key is added last so no
    # intermediate state references a table whose keys are still incomplete.
    with op.batch_alter_table("email_delivery_attempts") as batch:
        batch.create_foreign_key(
            "fk_attempt_owner",
            "email_deliveries",
            ["delivery_id", "user_id", "principal_id"],
            ["id", "user_id", "principal_id"],
        )
        batch.create_foreign_key(
            "fk_attempt_generation",
            "email_delivery_generations",
            ["delivery_id", "generation_id"],
            ["delivery_id", "id"],
        )

    with op.batch_alter_table("email_delivery_evidence") as batch:
        batch.create_foreign_key(
            "fk_evidence_attempt",
            "email_delivery_attempts",
            ["delivery_id", "attempt_id"],
            ["delivery_id", "id"],
        )

    with op.batch_alter_table("email_deliveries") as batch:
        batch.create_foreign_key(
            "fk_delivery_active_generation",
            "email_delivery_generations",
            ["id", "active_generation_id"],
            ["delivery_id", "id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("email_deliveries") as batch:
        batch.drop_constraint("fk_delivery_active_generation", type_="foreignkey")

    with op.batch_alter_table("email_delivery_evidence") as batch:
        batch.drop_constraint("fk_evidence_attempt", type_="foreignkey")

    with op.batch_alter_table("email_delivery_attempts") as batch:
        batch.drop_constraint("fk_attempt_generation", type_="foreignkey")
        batch.drop_constraint("fk_attempt_owner", type_="foreignkey")
