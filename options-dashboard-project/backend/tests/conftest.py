import os
import sys

os.environ.setdefault("UPSTOX_API_KEY", "test-api-key")
os.environ.setdefault("UPSTOX_API_SECRET", "test-api-secret")
os.environ.setdefault("UPSTOX_REDIRECT_URI", "http://localhost:8000/auth/callback")
os.environ.setdefault("FRONTEND_URL", "http://localhost:3000")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "test-encryption-key-for-dev-only")

import pytest

from app.services import token_store


# ---------------------------------------------------------------------------
# Production database protection (Phase 7.8C)
# ---------------------------------------------------------------------------
#
# During test execution, override the production SQLAlchemy engine and
# SessionLocal with in-memory equivalents so that no test (or fixture,
# or init_db() triggered by TestClient startup) can accidentally write
# to backend/paper_journal.db.
#
# The production module-level `engine` and `SessionLocal` are replaced
# once at import time.  Tests that explicitly create their own engine
# (StaticPool / sqlite://) are unaffected.
# ---------------------------------------------------------------------------

if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
    from sqlalchemy import create_engine as _create_engine
    from sqlalchemy.orm import sessionmaker as _sessionmaker
    from sqlalchemy.pool import StaticPool
    import app.db as _db_module

    _test_engine = _create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _db_module.engine = _test_engine
    _db_module.SessionLocal = _sessionmaker(
        bind=_test_engine, autocommit=False, autoflush=False
    )


@pytest.fixture(autouse=True)
def reset_token_store():
    token_store._revoked_sessions.clear()
    token_store.clear_token()
    yield
    token_store._revoked_sessions.clear()
    token_store.clear_token()


# ---------------------------------------------------------------------------
# Hermetic init_db() support (Issue #59)
# ---------------------------------------------------------------------------
#
# init_db() runs ``alembic upgrade head`` against app.db.engine.  When a test
# calls init_db() directly, Alembic must see a database it fully controls:
# the conftest-swapped shared engine is populated by many suites through
# ``Base.metadata.create_all(...)`` which creates application tables but no
# ``alembic_version`` row.  Alembic then replays the baseline migration on top
# of that schema and dies with "table bulk_exit_records already exists".
#
# Tests that exercise init_db() must therefore point app.db.engine at a fresh,
# disposable database for the duration of the test.  This fixture follows the
# existing pattern in test_db_migration.py (file-based SQLite so Alembic's
# connectable and the test engine address the same database) and is shared so
# every suite exercises the identical startup path.
# ---------------------------------------------------------------------------


@pytest.fixture
def hermetic_init_db(monkeypatch, tmp_path):
    """Redirect app.db.engine/SessionLocal to a fresh SQLite DB for init_db().

    Yields the ``init_db`` callable.  Each call runs the real Alembic startup
    path against an isolated file-based SQLite database that no other test
    can observe or pollute, and that is removed with the test's tmp_path.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    db_path = tmp_path / "hermetic_init_db.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    monkeypatch.setattr("app.db.engine", engine)
    monkeypatch.setattr("app.db.SessionLocal", sessionmaker(bind=engine))

    from app.db import init_db

    yield init_db
    engine.dispose()

