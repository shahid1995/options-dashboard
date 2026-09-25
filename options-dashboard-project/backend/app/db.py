import os

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
# Migration state validation (Day 5)
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


def _run_alembic_migrations() -> None:
    """Run Alembic migrations against the current engine.

    Alembic is the authoritative schema-management path. The current engine
    is passed through Config.attributes so programmatic startup and in-memory
    tests reuse the same connectable.
    """
    import logging
    from alembic.config import Config
    from alembic import command

    logger = logging.getLogger(__name__)
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", str(engine.url))
    alembic_cfg.attributes["connectable"] = engine
    command.upgrade(alembic_cfg, "head")
    logger.info("Alembic migrations applied successfully")


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
    _run_alembic_migrations()

    # Conservative one-time backfill for pre-existing, provably unambiguous
    # executions. Rows already present are never duplicated.
    from app.services.leg_exposure import backfill_all_exposures

    session = sessionmaker(bind=engine)()
    try:
        backfill_all_exposures(session)
    finally:
        session.close()

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
