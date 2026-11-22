import os
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import create_engine, text, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Database path / URL resolution
# ---------------------------------------------------------------------------
#
# SQLite remains the default for local development and for the current
# production environment until the explicit PostgreSQL switchover phase.
# When DATABASE_URL points at PostgreSQL, normalize bare postgres URLs to the
# installed psycopg 3 SQLAlchemy dialect.
# ---------------------------------------------------------------------------

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DB_PATH = os.path.join(_BACKEND_DIR, "paper_journal.db")


def normalize_database_url(url: str) -> str:
    """Normalize database URLs to dialects supported by this application.

    Railway may provide either ``postgres://`` or ``postgresql://`` style
    URLs. The application uses psycopg 3, so bare PostgreSQL URLs are mapped
    to ``postgresql+psycopg://``. Explicit driver URLs are preserved.
    SQLite and other SQLAlchemy URLs are returned unchanged.
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _engine():
    if settings.DATABASE_URL:
        url = normalize_database_url(settings.DATABASE_URL)
    else:
        url = f"sqlite:///{_DEFAULT_DB_PATH}"

    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        eng = create_engine(url, connect_args=connect_args)

        # SQLite-only crash/concurrency tuning. Never register these hooks
        # against PostgreSQL or another SQLAlchemy dialect.
        @event.listens_for(eng, "connect")
        def _set_wal(dbapi_conn, _rec):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA synchronous=NORMAL")
    else:
        # PostgreSQL production/staging configuration.
        eng = create_engine(
            url,
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,
            pool_pre_ping=True,
        )

    return eng


# ---------------------------------------------------------------------------
# Production safety validation (ADR-014, fail-closed)
# ---------------------------------------------------------------------------


def validate_production_config() -> None:
    """Fail closed when production database configuration is unsafe.

    Called at module import time BEFORE any engine is constructed. When the
    production signal is active (``STRIKENOVA_ENV=production`` —
    provider-neutral — or the legacy Railway-era indicators), the application
    MUST NOT be able to start on SQLite:

    - missing ``DATABASE_URL``       -> RuntimeError (no silent fallback)
    - SQLite ``DATABASE_URL``        -> RuntimeError (scheme match is
      case-insensitive: ``sqlite:``, ``SQLITE:``, and mixed case all refused)
    - PostgreSQL/CockroachDB ``DATABASE_URL`` -> accepted

    Non-production environments are untouched: intentional local SQLite
    development/test behavior is preserved.

    Failure messages never embed the connection string (no credentials in
    logs or exception text).
    """
    import logging

    logger = logging.getLogger(__name__)

    if not settings.IS_PRODUCTION:
        return

    if not settings.DATABASE_URL:
        logger.error(
            "Production environment detected but DATABASE_URL is not set. "
            "Refusing to start: the application would silently fall back to "
            "local SQLite, which is unsuitable for production. Set "
            "DATABASE_URL to a PostgreSQL/CockroachDB connection string."
        )
        raise RuntimeError(
            "production database configuration is required: DATABASE_URL is "
            "not set while production mode is enabled. The application "
            "refuses to silently fall back to SQLite."
        )

    normalized = normalize_database_url(settings.DATABASE_URL)
    if normalized.lower().startswith("sqlite"):
        logger.error(
            "Production environment detected but DATABASE_URL points to "
            "SQLite (connection string masked). Refusing to start: "
            "production must use PostgreSQL/CockroachDB."
        )
        raise RuntimeError(
            "production database configuration is required: DATABASE_URL "
            "points to SQLite while production mode is enabled. Set "
            "DATABASE_URL to a PostgreSQL/CockroachDB connection string."
        )

    # Explicit allowlist of the only dialect families production supports
    # (PostgreSQL/CockroachDB after normalization). A malformed or unknown
    # scheme (e.g. ``unknown://``, ``postgres+nosuchdriver://``) must fail
    # through THIS error contract — not with a raw SQLAlchemy
    # ``NoSuchModuleError`` from engine construction. Credential text is
    # never included: only the scheme family is echoed.
    #
    # CockroachDB schemes are REQUIRED: CockroachDB Cloud is the mandated
    # production database, the SQLAlchemy psycopg dialect cannot parse
    # CockroachDB's server version string (startup aborts with
    # "Could not determine version from string 'CockroachDB CCL ...'"), and
    # the project's declared dependency sqlalchemy-cockroachdb provides the
    # working ``cockroachdb[+psycopg]://`` dialects. Staging's production
    # configuration uses ``cockroachdb+psycopg://``. Rejecting these schemes
    # would make it impossible to boot against the intended production
    # database while the plain-PostgreSQL schemes cannot actually run on it.
    allowed_prefixes = (
        "postgresql+psycopg://",  # normalize_database_url() target
        "postgresql://",          # accepted pre-normalization form
        "postgres://",            # legacy pre-normalization form
        "cockroachdb+psycopg://",  # sqlalchemy-cockroachdb (psycopg 3) — CRDB
        "cockroachdb://",          # sqlalchemy-cockroachdb dialect — CRDB
    )
    if not normalized.startswith(allowed_prefixes):
        scheme = normalized.split(":", 1)[0]
        logger.error(
            "Production environment detected but DATABASE_URL uses an "
            "unsupported scheme (connection string masked). Refusing to "
            "start: production must use PostgreSQL/CockroachDB."
        )
        raise RuntimeError(
            "production database configuration is required: DATABASE_URL "
            f"uses unsupported scheme '{scheme}' while production mode is "
            "enabled. Set DATABASE_URL to a PostgreSQL/CockroachDB "
            "connection string."
        )


# Validation MUST run before engine construction: a malformed SQLite URL
# could otherwise fail inside create_engine() with a dialect error before the
# required production-configuration error is raised.
validate_production_config()


engine = _engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


# ---------------------------------------------------------------------------
# Migration-state validation (Day 5)
# ---------------------------------------------------------------------------


def validate_migration_state() -> dict:
    """Validate Alembic migration state against the expected head.

    Returns a dict with:
    - "status": "current" | "behind" | "uninitialised" | "error"
    - "expected_head": the single expected Alembic head revision
    - "actual_revision": the revision stamped in the database (or None)
    - "alembic_heads": list of heads from the migration script directory
    - "error": error message if validation failed

    Never raises — always returns a result dict.
    Credentials are never included in the output.
    """
    import logging

    logger = logging.getLogger(__name__)
    result: dict = {
        "status": "error",
        "expected_head": None,
        "actual_revision": None,
        "alembic_heads": [],
        "error": None,
    }

    try:
        from alembic.config import Config as AlembicConfig
        from alembic.script import ScriptDirectory
        from alembic.runtime.migration import MigrationContext

        alembic_cfg = AlembicConfig("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", str(engine.url))
        script = ScriptDirectory.from_config(alembic_cfg)
        heads = script.get_heads()
        result["alembic_heads"] = list(heads)

        if len(heads) != 1:
            result["status"] = "error"
            result["error"] = (
                f"Expected exactly 1 Alembic head, got {len(heads)}: {heads}"
            )
            return result

        result["expected_head"] = heads[0]

        # Collect every revision ID in the migration graph.  This lets us
        # distinguish "behind" (revision is in the graph but not the head)
        # from "unknown" (revision is not in the graph at all).
        #
        # Uses Alembic's full-graph walk: a manual down_revision walk that
        # picks downs[0] at merge points silently drops sibling parents and
        # misclassifies real chain revisions as "unknown" (Day41 fix — the
        # graph has merge points since Day38).
        all_revisions: set = set()
        try:
            all_revisions = {r.revision for r in script.walk_revisions()}
        except Exception:
            # If graph walking fails, fall back to checking only against head
            all_revisions = set(heads)

        with engine.connect() as conn:
            mc = MigrationContext.configure(conn)
            current_rev = mc.get_current_revision()
            result["actual_revision"] = current_rev

        if current_rev is None:
            result["status"] = "uninitialised"
            result["error"] = "No alembic_version record found"
        elif current_rev == heads[0]:
            result["status"] = "current"
        elif current_rev in all_revisions:
            result["status"] = "behind"
            result["error"] = (
                f"Database revision {current_rev} != expected head {heads[0]}"
            )
        else:
            result["status"] = "unknown"
            result["error"] = (
                f"Database revision {current_rev} is not present in the "
                f"migration graph (expected one of: {sorted(all_revisions)})"
            )

    except Exception as e:
        result["status"] = "error"
        # Mask any potential credentials in the error message
        error_str = str(e)
        for sensitive in ["password", "secret", "token"]:
            if sensitive in error_str.lower():
                error_str = "Database connection error (details masked)"
                break
        result["error"] = error_str
        logger.warning("Migration state validation failed: %s", result["error"])

    return result



def get_database_path() -> str:
    """Return the configured database URL or local SQLite path."""
    if settings.DATABASE_URL:
        return normalize_database_url(settings.DATABASE_URL)
    return _DEFAULT_DB_PATH


def get_db():
    """FastAPI dependency: yields a database session, closed after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def resolve_migration_database_url(explicit_url: str | None = None) -> str:
    """Resolve the candidate database URL for an Alembic invocation.

    Precedence (PR #107 review contract):

    1. ``explicit_url`` — an operator-supplied ``sqlalchemy.url`` (the
       alembic.ini / CLI-configured value passed in by ``alembic/env.py``);
       the highest-priority, intentional operator override.
    2. ``STRIKENOVA_MIGRATION_DATABASE_URL`` — the dedicated migration
       identity.
    3. ``DATABASE_URL`` — the runtime URL.
    4. The local SQLite file when nothing is configured.

    This resolver only *proposes* a URL; it is not the policy boundary.
    :func:`assert_guarded_migration_configuration` is, and it fails closed
    (``45004``) when ``DATABASE_URL`` is PostgreSQL and
    ``STRIKENOVA_MIGRATION_DATABASE_URL`` is missing or resolves to the same
    database identity. The fallback in step 3 therefore cannot produce an
    unguarded PostgreSQL migration: it only serves SQLite, which keeps its
    historical single-process, single-identity behaviour.

    Blank/whitespace values are treated as unset at every level, and every
    resolved URL passes through :func:`normalize_database_url` exactly like
    the startup migration path (``cockroachdb+psycopg://`` and other
    explicit driver URLs are preserved). This resolver never changes which
    identity serves application traffic — the runtime engine keeps using
    ``DATABASE_URL`` (ADR-014's production guard semantics are untouched).
    """
    normalized_explicit = (explicit_url or "").strip()
    if normalized_explicit and not normalized_explicit.startswith("driver://"):
        return normalize_database_url(normalized_explicit)
    migration_url = getattr(settings, "STRIKENOVA_MIGRATION_DATABASE_URL", None)
    if migration_url and migration_url.strip():
        return normalize_database_url(migration_url.strip())
    runtime_url = settings.DATABASE_URL
    if runtime_url and runtime_url.strip():
        return normalize_database_url(runtime_url.strip())
    return f"sqlite:///{_DEFAULT_DB_PATH}"


def _migration_engine_url() -> str:
    """Resolve the URL used by Alembic migrations.

    Identity separation (ADR-017/018): on PostgreSQL every migration runs
    under the dedicated migration identity named by
    ``STRIKENOVA_MIGRATION_DATABASE_URL``. Single-identity deployment is no
    longer supported for PostgreSQL: a missing or identical migration
    identity is refused by :func:`assert_guarded_migration_configuration`
    with SQLSTATE ``45004`` rather than silently reusing the runtime URL.
    SQLite keeps the historical single-process path.

    Delegates to :func:`resolve_migration_database_url` so the startup path
    and the standalone Alembic CLI resolve URLs identically.
    """
    return resolve_migration_database_url()


# ---------------------------------------------------------------------------
# ADR-017 migration lease / ADR-018 identity separation
# ---------------------------------------------------------------------------


def _runtime_url_from_settings() -> str | None:
    value = (settings.DATABASE_URL or "").strip()
    return normalize_database_url(value) if value else None


def _is_postgres_url(url: str) -> bool:
    return url.split(":", 1)[0].startswith(("postgres", "cockroachdb"))


def _database_identity(url: str) -> tuple:
    """Return the identity attributes that must differ between roles.

    Passwords are deliberately excluded: they are not part of the identity
    comparison and must never appear in a log or exception.
    """
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    return (
        parsed.get_backend_name(),
        parsed.username,
        parsed.host,
        parsed.port,
        parsed.database,
    )


def assert_guarded_migration_configuration(
    runtime_url: str | None = None,
    migration_url: str | None = None,
) -> str:
    """Fail closed on an unsafe PostgreSQL migration configuration (ADR §8.1).

    On PostgreSQL the migration identity must be explicit
    (``STRIKENOVA_MIGRATION_DATABASE_URL``) and must not resolve to the same
    database identity as the runtime ``DATABASE_URL``. SQLite keeps its
    single-process, single-identity behaviour.
    """
    runtime_url = runtime_url or _runtime_url_from_settings()
    migration_url = migration_url or _migration_engine_url()

    if not runtime_url or not _is_postgres_url(runtime_url):
        return migration_url

    explicit = (getattr(settings, "STRIKENOVA_MIGRATION_DATABASE_URL", None) or "").strip()
    if not explicit:
        from app._migration_lease import LeaseError, SQLSTATE_CONFIG

        raise LeaseError(
            SQLSTATE_CONFIG,
            "STRIKENOVA_MIGRATION_DATABASE_URL is required when DATABASE_URL is "
            "PostgreSQL: the runtime identity must never execute migrations",
        )

    explicit_url = normalize_database_url(explicit)
    if _database_identity(explicit_url) == _database_identity(runtime_url):
        from app._migration_lease import LeaseError, SQLSTATE_CONFIG

        raise LeaseError(
            SQLSTATE_CONFIG,
            "the migration and runtime identities resolve to the same database "
            "identity; refusing to run migrations as the runtime role",
        )
    return migration_url


def _migration_engine():
    """Build (and return) the engine for the dedicated migration identity."""
    url = assert_guarded_migration_configuration()
    return create_engine(
        url,
        poolclass=None,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=2,
        pool_timeout=30,
    )


def _quote_role(role: str) -> str:
    return '"' + role.replace('"', '""') + '"'


def _set_owner_role(conn) -> None:
    """ADR §8.3 Topology B: explicit owner-role window around Alembic DDL."""
    owner_role = (getattr(settings, "STRIKENOVA_OWNER_ROLE", "") or "").strip()
    if not owner_role:
        return
    conn.exec_driver_sql(f"SET ROLE {_quote_role(owner_role)}")
    current = conn.exec_driver_sql("SELECT current_user").scalar()
    if current != owner_role:
        from app._migration_lease import LeaseError, SQLSTATE_CONFIG

        raise LeaseError(
            SQLSTATE_CONFIG,
            f"SET ROLE {owner_role!r} did not take effect (current_user={current!r})",
        )


def _reset_owner_role(conn) -> None:
    owner_role = (getattr(settings, "STRIKENOVA_OWNER_ROLE", "") or "").strip()
    if not owner_role:
        return
    conn.exec_driver_sql("RESET ROLE")
    current = conn.exec_driver_sql("SELECT current_user").scalar()
    migrator_role = settings.STRIKENOVA_MIGRATOR_ROLE
    if current != migrator_role:
        from app._migration_lease import LeaseError, SQLSTATE_CONFIG

        raise LeaseError(
            SQLSTATE_CONFIG,
            f"RESET ROLE did not restore the migration identity "
            f"(current_user={current!r}, expected {migrator_role!r})",
        )


#: ADR §8.3 Topology A: the ordinary privileges the migration identity must
#: NOT keep on the application objects the startup backfill touches, once the
#: Alembic chain has created them. The migrator owns those objects, and
#: PostgreSQL 16 lets an owner revoke its own ordinary privileges — the
#: revoke is effective and a later INSERT/UPDATE/DELETE fails ``42501`` even
#: for the owner. Ownership still permits ``ALTER``/``DROP``: those are
#: inherent ownership powers, not ordinary privileges, and no revoke can
#: remove them (ADR §8.4 and D-2 therefore govern who owns the tables).
_TOPOLOGY_A_RESTRICTED_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("positions", ("INSERT", "UPDATE", "DELETE", "TRUNCATE")),
    ("paper_orders", ("INSERT", "UPDATE", "DELETE", "TRUNCATE")),
    ("strategy_leg_exposures", ("UPDATE", "DELETE", "TRUNCATE")),
)

#: ADR §8.3 Topology A: tables whose ``id`` sequence is restricted to
#: ``USAGE`` only (``REVOKE ALL`` then ``GRANT USAGE``), so ``nextval`` works
#: for ``INSERT … RETURNING id`` while ``SELECT`` on the sequence does not.
_TOPOLOGY_A_RESTRICTED_SEQUENCES: tuple[str, ...] = ("strategy_leg_exposures",)


def apply_migrator_privilege_restrictions(conn) -> int:
    """Apply the ADR §8.3 Topology A restrictions to the migration identity.

    Called inside the protected DDL transaction, after Alembic has created (or
    re-created) the application objects and before the protected backfill, so
    the restriction is committed together with the schema and is re-applied by
    every later ``upgrade head`` instead of silently disappearing. Topology B
    does not use this path: its objects are owned by the separate owner role
    and its privileges come from the §8.3 grants.

    Returns the number of objects whose privileges were restricted. Only
    ordinary table/sequence privileges are revoked (see
    ``_TOPOLOGY_A_RESTRICTED_TABLES``): the migrator keeps its inherent
    ownership powers to ``ALTER``/``DROP`` those objects, and it could grant
    the privileges back to itself — these revokes bound accident, not intent.
    """
    owner_role = (getattr(settings, "STRIKENOVA_OWNER_ROLE", "") or "").strip()
    if owner_role:
        return 0
    migrator_role = (settings.STRIKENOVA_MIGRATOR_ROLE or "").strip()
    if not migrator_role:
        return 0
    migrator = _quote_role(migrator_role)
    restricted = 0
    for table, privileges in _TOPOLOGY_A_RESTRICTED_TABLES:
        relation = f"public.{_quote_role(table)}"
        if conn.exec_driver_sql(f"SELECT to_regclass('{relation}')").scalar() is None:
            continue
        conn.exec_driver_sql(
            f"REVOKE {', '.join(privileges)} ON TABLE {relation} FROM {migrator}"
        )
        restricted += 1
    for table in _TOPOLOGY_A_RESTRICTED_SEQUENCES:
        sequence = conn.exec_driver_sql(
            f"SELECT pg_get_serial_sequence('public.{table}', 'id')"
        ).scalar()
        if not sequence:
            continue
        conn.exec_driver_sql(f"REVOKE ALL ON SEQUENCE {sequence} FROM {migrator}")
        conn.exec_driver_sql(f"GRANT USAGE ON SEQUENCE {sequence} TO {migrator}")
        restricted += 1
    return restricted


def _run_guarded_migrations(
    alembic_cfg,
    command,
    logger,
    post_migration_callback: Callable[["MigrationWork"], None] | None = None,
) -> None:
    """Run Alembic DDL inside the ADR-017 lease transaction (ADR §7.1, §13).

    The lease row lock, the advisory guard and the DDL share one physical
    connection and one transaction. ``enter`` is the first statement of that
    transaction and again the last statement before ``COMMIT``.
    """
    from app._migration_lease import MigrationLease, preflight

    migration_engine = _migration_engine()
    lease = MigrationLease(migration_engine)
    preflight(migration_engine)
    lease.acquire()
    primary: BaseException | None = None
    try:
        with migration_engine.connect() as conn:
            # Session-level bounded lock wait, before the transaction begins.
            conn.exec_driver_sql(
                f"SET lock_timeout = '{settings.MIGRATION_LEASE_LOCK_TIMEOUT_MS}ms'"
            )
            conn.commit()
            with conn.begin():
                lease.enter(conn)  # FIRST statement of the protected transaction
                alembic_cfg.attributes["connection"] = conn
                alembic_cfg.attributes["lease_guard"] = lease
                _set_owner_role(conn)
                command.upgrade(alembic_cfg, "head")
                # Only reached on success: an exception rolls the whole
                # transaction back, so the elevation cannot outlive it.
                _reset_owner_role(conn)
                # ADR §8.3 Topology A: bound the migration identity's ordinary
                # privileges before the backfill runs, in the same transaction
                # as the DDL and on every upgrade (never just the first one).
                restricted = apply_migrator_privilege_restrictions(conn)
                if restricted:
                    logger.info(
                        "ADR §8.3 Topology A: restricted %d application object(s) "
                        "for the migration identity",
                        restricted,
                    )
                lease.enter(conn)  # refresh immediately before COMMIT
        logger.info("Alembic migrations applied successfully under the lease")

        if post_migration_callback is not None:
            post_migration_callback(MigrationWork(engine=migration_engine, lease=lease))
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            lease.release()
        except Exception:  # noqa: BLE001 - cleanup must never mask the cause
            logger.exception(
                "migration lease cleanup failed (fingerprint=%s)", lease.fingerprint
            )
            if primary is None:
                raise
        finally:
            migration_engine.dispose()


def _run_alembic_migrations(
    post_migration_callback: Callable[["MigrationWork"], None] | None = None,
) -> None:
    """Run Alembic migrations against the migration identity.

    On SQLite the historical single-process path is preserved. On PostgreSQL
    the ADR-017 lease guards the DDL transaction and the post-migration
    backfill (``post_migration_callback``), and the runtime identity never
    executes DDL.
    """
    import logging
    from alembic.config import Config
    from alembic import command

    logger = logging.getLogger(__name__)
    alembic_cfg = Config("alembic.ini")
    migration_url = assert_guarded_migration_configuration()
    alembic_cfg.set_main_option("sqlalchemy.url", migration_url)

    if migration_url.startswith("sqlite"):
        # Single-process by construction: no distributed lease is needed.
        logger.info("SQLite target: migration lease skipped (single-process)")
        alembic_cfg.attributes["connectable"] = engine
        command.upgrade(alembic_cfg, "head")
        logger.info("Alembic migrations applied successfully")
        if post_migration_callback is not None:
            post_migration_callback(MigrationWork(engine=engine, lease=None))
        return

    if migration_url.split(":", 1)[0].startswith("cockroachdb"):
        from app._migration_lease import LeaseError, SQLSTATE_CONFIG

        raise LeaseError(
            SQLSTATE_CONFIG,
            "CockroachDB is retired (ADR-019); the ADR-017 lease is "
            "PostgreSQL-only and no unleased CockroachDB migration path exists",
        )

    _run_guarded_migrations(alembic_cfg, command, logger, post_migration_callback)


@dataclass
class MigrationWork:
    """Context handed to post-migration work (the startup exposure backfill)."""

    engine: object
    lease: object | None


def run_startup_backfill(work: MigrationWork) -> None:
    """Run the exposure backfill as the migration identity (ADR §7.2–§7.3).

    SQLite keeps one session and one implicit transaction. PostgreSQL
    enumerates users in a short committed transaction, then processes one
    user per protected transaction whose FIRST statement is ``enter`` and
    whose last statement before ``COMMIT`` is a second ``enter``.
    """
    import logging

    from sqlalchemy.orm import Session

    from app.services.leg_exposure import backfill_all_exposures, backfill_exposures

    logger = logging.getLogger(__name__)
    if work.lease is None:
        session = sessionmaker(bind=work.engine)()
        try:
            backfill_all_exposures(session)
        finally:
            session.close()
        return

    with work.engine.connect() as conn:
        user_ids = [
            row[0]
            for row in conn.execute(
                text("SELECT DISTINCT user_id FROM positions ORDER BY user_id")
            )
        ]
        # Enumeration is its own short transaction (ADR §7.2).
        conn.commit()

    logger.info("startup backfill: %d user(s) to process", len(user_ids))
    statement_timeout_ms = settings.MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS
    for user_id in user_ids:
        with work.engine.connect() as conn:
            with conn.begin():
                work.lease.enter(conn)  # FIRST statement of the user transaction
                conn.exec_driver_sql(
                    f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"
                )
                session = Session(
                    bind=conn,
                    autoflush=False,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                try:
                    backfill_exposures(session, user_id)
                    session.commit()  # release the savepoint, not the outer tx
                except BaseException:
                    session.rollback()
                    raise
                finally:
                    session.close()
                work.lease.enter(conn)  # MANDATORY pre-commit refresh


# ---------------------------------------------------------------------------
# DATABASE SCHEMA ARCHITECTURE (Phase 10.1B — final)
# ---------------------------------------------------------------------------
#
# Alembic is the SOLE authoritative schema management mechanism.
#
# Startup sequence:
#   1. Alembic upgrade head       — versioned, authoritative schema DDL
#   2. Data backfill (idempotent) — strategy-leg attribution backfill
#   3. Composite indexes           — SQLite-only pipeline query indexes
#
# CLI tools (candle_backfill, run_backfill, run_daily, etc.) may still
# call Base.metadata.create_all() for their own database setup — those
# are separate from the web application startup path.
#
# greeks_checkpoint remains CLI-owned raw SQL, intentionally outside
# Base.metadata and the Alembic baseline.
# ---------------------------------------------------------------------------


def init_db():
    """Initialize database on application startup.

    Alembic owns the authoritative schema. This function:
      1. Runs ``alembic upgrade head`` (schema DDL)
      2. Runs idempotent data backfills
      3. Creates composite indexes (SQLite only)

    Called ONCE from the FastAPI lifespan handler. Never called during
    request processing.
    """
    import logging
    from app import models  # noqa: F401  (registers tables on Base.metadata)

    logger = logging.getLogger(__name__)

    # Conservative one-time backfill for pre-existing, provably unambiguous
    # executions. Rows already present are never duplicated. On PostgreSQL it
    # runs as the migration identity inside the ADR-017 lease, one protected
    # transaction per user (ADR §7.2–§7.3); on SQLite it runs as before.
    _run_alembic_migrations(post_migration_callback=run_startup_backfill)

    # These indexes are intentionally SQLite-only and are not part of the
    # cross-dialect Alembic schema.
    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            for stmt in [
                "CREATE INDEX IF NOT EXISTS ix_ingestion_log_operation_status ON ingestion_log (operation, status)",
                "CREATE INDEX IF NOT EXISTS ix_ingestion_log_completed_at ON ingestion_log (completed_at)",
                "CREATE INDEX IF NOT EXISTS ix_data_completeness_status ON data_completeness (status)",
                "CREATE INDEX IF NOT EXISTS ix_ingestion_checkpoint_status ON ingestion_checkpoint (pipeline, status)",
            ]:
                conn.execute(text(stmt))


# ---------------------------------------------------------------------------
# Database health check (Phase 7.21)
# ---------------------------------------------------------------------------

_HISTORICAL_TABLES = [
    "nifty_candles",
    "contract_specs",
    "option_candles",
    "option_greeks",
]


def check_database_health() -> dict:
    """Return a diagnostic snapshot of the active database.

    Day 4: report includes the active dialect name and conditionally
    includes file-specific information only for SQLite databases.
    PostgreSQL reports omit file_exists/file_size_bytes since they are
    meaningless for client-server databases.
    """
    from sqlalchemy import inspect as sa_inspect, func, select

    dialect_name = engine.dialect.name
    db_path = get_database_path()
    report: dict = {
        "database_path": db_path,
        "dialect": dialect_name,
        "accessible": False,
        "tables_present": [],
        "tables_missing": [],
        "row_counts": {},
        "oldest_record": None,
        "newest_record": None,
    }

    # File-specific fields are only meaningful for SQLite.
    if dialect_name == "sqlite":
        report["file_exists"] = False
        report["file_size_bytes"] = 0
        if os.path.isfile(db_path):
            report["file_exists"] = True
            report["file_size_bytes"] = os.path.getsize(db_path)

    try:
        insp = sa_inspect(engine)
        existing_tables = set(insp.get_table_names())
        report["tables_present"] = sorted(
            t for t in _HISTORICAL_TABLES if t in existing_tables
        )
        report["tables_missing"] = sorted(
            t for t in _HISTORICAL_TABLES if t not in existing_tables
        )
    except Exception as e:
        report["schema_error"] = str(e)
        return report

    db = SessionLocal()
    try:
        from app.models import NiftyCandle, ContractSpec, OptionCandle, OptionGreeks

        for label, model in [
            ("nifty_candles", NiftyCandle),
            ("contract_specs", ContractSpec),
            ("option_candles", OptionCandle),
            ("option_greeks", OptionGreeks),
        ]:
            count = db.scalar(select(func.count(model.id))) or 0
            report["row_counts"][label] = count

        nifty_oldest = db.scalar(
            select(NiftyCandle.open_time).order_by(NiftyCandle.open_time.asc()).limit(1)
        )
        nifty_newest = db.scalar(
            select(NiftyCandle.open_time).order_by(NiftyCandle.open_time.desc()).limit(1)
        )
        if nifty_oldest:
            report["oldest_record"] = str(nifty_oldest)
        if nifty_newest:
            report["newest_record"] = str(nifty_newest)

        report["accessible"] = True
    except Exception as e:
        report["access_error"] = str(e)
    finally:
        db.close()

    return report
