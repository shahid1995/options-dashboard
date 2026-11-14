import os

from sqlalchemy import create_engine, text, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Database path resolution
# ---------------------------------------------------------------------------
#
# Phase 7.20 audit discovered that the previous relative path
# ("sqlite:///./paper_journal.db") resolved differently depending on
# the process working directory, causing data loss across server
# restarts and CLI invocations.
#
# FIX: When DATABASE_URL is not set, resolve relative to this file's
# parent directory (backend/), so the database always lives at
#   backend/paper_journal.db
# regardless of where Python/Uvicorn/CLI is launched from.
#
# When DATABASE_URL IS set (e.g. Railway PostgreSQL), use it as-is.
# ---------------------------------------------------------------------------

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DB_PATH = os.path.join(_BACKEND_DIR, "paper_journal.db")


def _engine():
    if settings.DATABASE_URL:
        url = settings.DATABASE_URL
    else:
        # Absolute file path wrapped in sqlite:/// URI
        url = f"sqlite:///{_DEFAULT_DB_PATH}"
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        eng = create_engine(url, connect_args=connect_args)

        # Exactly one connect listener. The previous code registered the same
        # WAL pragma pair twice (once inside this branch, once again after it),
        # which ran both statements twice on every new connection.
        #
        # foreign_keys=ON is what makes the schema's foreign keys actually
        # apply: SQLite defaults to OFF, so without this every FK in the
        # database is inert. Migrations are the one exception -- Alembic's
        # batch rebuilds must drop and recreate tables, which SQLite refuses
        # while enforcement is on. alembic/env.py therefore turns enforcement
        # off on the raw DBAPI connection for the duration of a migration and
        # restores it in a finally block (a transaction-scoped pragma cannot
        # work: the check on DROP TABLE is immediate, not deferrable), so this
        # listener can leave enforcement on for the life of the application.
        @event.listens_for(eng, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _rec):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    else:
        # Phase 9E: Production PostgreSQL configuration
        eng = create_engine(
            url,
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,
            pool_pre_ping=True,
        )

    return eng


engine = _engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_database_path() -> str:
    """Return the absolute filesystem path of the active database."""
    if settings.DATABASE_URL:
        return settings.DATABASE_URL
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

    Called from init_db() to apply versioned schema migrations at startup.
    This is the PRIMARY schema management path — Alembic owns the
    authoritative schema definition.

    Uses Alembic's ``Config.attributes`` to pass the current engine to
    env.py. This avoids module-global state and ensures Alembic reuses
    the same connection — critical for in-memory SQLite tests.
    CLI-driven ``alembic upgrade head`` (without Config.attributes) falls
    back to creating its own engine from the URL.
    """
    import logging
    from alembic.config import Config
    from alembic import command

    logger = logging.getLogger(__name__)
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", str(engine.url))
    # Alembic's official mechanism for sharing a connection.
    # env.py reads config.attributes['connectable'] when present.
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
#   3. Composite indexes         — SQLite-only pipeline query indexes
#
# Removed in Phase 10.1B:
#   - Base.metadata.create_all() — no longer in production startup
#   - ensure_column()            — all 15 legacy columns are in baseline
#   - _existing_columns()        — no longer needed
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

    # Step 1: Alembic migrations (sole schema management mechanism).
    # This MUST succeed — if it fails, the application should not start.
    _run_alembic_migrations()

    # Step 2: Idempotent data backfill — strategy-leg attribution.
    # Conservative one-time backfill for pre-existing, provably unambiguous
    # executions. Rows already present are never duplicated.
    from app.services.leg_exposure import backfill_all_exposures

    session = sessionmaker(bind=engine)()
    try:
        backfill_all_exposures(session)
    finally:
        session.close()

    # Step 3: Composite indexes for pipeline infrastructure queries.
    # These are NOT in the Alembic baseline (composite + covering indexes)
    # and use CREATE INDEX IF NOT EXISTS for idempotent execution.
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

# Tables that should exist in the historical-data schema.
_HISTORICAL_TABLES = [
    "nifty_candles",
    "contract_specs",
    "option_candles",
    "option_greeks",
]


def check_database_health() -> dict:
    """Return a diagnostic snapshot of the production database.

    Designed to be called before any backfill to confirm the database
    is accessible, correctly located, and has the expected schema.

    Returns
    -------
    dict
        A machine-readable health report with path, size, row counts,
        and accessibility status.
    """
    from sqlalchemy import inspect as sa_inspect, func, select

    db_path = get_database_path()
    report: dict = {
        "database_path": db_path,
        "file_exists": False,
        "file_size_bytes": 0,
        "accessible": False,
        "tables_present": [],
        "tables_missing": [],
        "row_counts": {},
        "oldest_record": None,
        "newest_record": None,
    }

    # File check
    if os.path.isfile(db_path):
        report["file_exists"] = True
        report["file_size_bytes"] = os.path.getsize(db_path)

    # Schema check
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

    # Row counts and date range
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

        # Date range from nifty_candles (if any data)
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
