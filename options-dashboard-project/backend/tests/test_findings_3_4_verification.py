"""Findings 3 & 4 verification tests.

Finding 3: Verify get_analytics_token() correctly handles connection_id pinning.
Finding 4: Verify no duplicate API version authorities exist (app/api/ was refactored to routers/).
"""

import pytest
from uuid import uuid4
from datetime import datetime, timedelta, timezone

from app.db import Base
from app.identity import User, BrokerConnection, create_session_record, hash_password
from app.services import token_store
from app.crypto import encrypt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


# ---------------------------------------------------------------------------
# Finding 3 — connection_id pinning in get_analytics_token()
# ---------------------------------------------------------------------------

def _create_user(db, email="user@test.com"):
    user = User(
        id=str(uuid4()), email=email, password_hash=hash_password("pw"),
        display_name="Test", status="active", identity_source="email",
    )
    db.add(user)
    db.flush()
    return user


def _create_connection(db, user, broker="UPSTOX", account_id="acct", is_default=True):
    conn = BrokerConnection(
        id=str(uuid4()), user_id=user.id, broker=broker,
        broker_account_id=account_id, status="connected", is_default=is_default,
    )
    db.add(conn)
    db.flush()
    return conn


def test_pinned_connection_resolves_analytics_token(db_session):
    """Pinned connection_id + valid Analytics Token -> resolves pinned token."""
    user = _create_user(db_session)
    conn = _create_connection(db_session, user)
    conn.broker_analytics_token_encrypted = encrypt("pinned-token")
    conn.data_status = "active"
    conn.data_source = "analytics_token"
    db_session.commit()

    from app.identity import get_analytics_token
    result = get_analytics_token(db_session, user.id, "UPSTOX", connection_id=conn.id)
    assert result == "pinned-token"


def test_pinned_connection_expired_returns_none(db_session):
    """Pinned connection + expired data status -> returns None."""
    user = _create_user(db_session)
    conn = _create_connection(db_session, user)
    conn.broker_analytics_token_encrypted = encrypt("expired-token")
    conn.data_status = "inactive"
    db_session.commit()

    from app.identity import get_analytics_token
    result = get_analytics_token(db_session, user.id, "UPSTOX", connection_id=conn.id)
    assert result is None


def test_pinned_connection_other_user_fails_closed(db_session):
    """Pinned connection belonging to another user -> fails closed."""
    user_a = _create_user(db_session, email="a@test.com")
    user_b = _create_user(db_session, email="b@test.com")
    conn = _create_connection(db_session, user_b)  # belongs to B
    conn.broker_analytics_token_encrypted = encrypt("b-token")
    conn.data_status = "active"
    db_session.commit()

    from app.identity import get_analytics_token
    # A tries to pin B's connection
    result = get_analytics_token(db_session, user_a.id, "UPSTOX", connection_id=conn.id)
    assert result is None


def test_pinned_connection_wrong_broker_fails_closed(db_session):
    """Pinned connection for broker A while requesting broker B -> fails closed."""
    user = _create_user(db_session)
    conn = _create_connection(db_session, user, broker="FYERS")
    conn.broker_analytics_token_encrypted = encrypt("fyers-token")
    conn.data_status = "active"
    db_session.commit()

    from app.identity import get_analytics_token
    # Request UPSTOX but pin FYERS connection
    result = get_analytics_token(db_session, user.id, "UPSTOX", connection_id=conn.id)
    assert result is None


def test_no_connection_id_default_selection_unchanged(db_session):
    """No connection_id -> existing default-selection behavior remains."""
    user = _create_user(db_session)
    conn = _create_connection(db_session, user, is_default=True)
    conn.broker_analytics_token_encrypted = encrypt("default-token")
    conn.data_status = "active"
    conn.data_source = "analytics_token"
    db_session.commit()

    from app.identity import get_analytics_token
    # No connection_id -> resolves default connection
    result = get_analytics_token(db_session, user.id, "UPSTOX")
    assert result == "default-token"


def test_non_default_connection_can_be_explicitly_selected(db_session):
    """Non-default valid connection can be explicitly selected."""
    user = _create_user(db_session)
    default_conn = _create_connection(db_session, user, account_id="default", is_default=True)
    default_conn.broker_analytics_token_encrypted = encrypt("default-token")
    default_conn.data_status = "active"
    default_conn.data_source = "analytics_token"

    other_conn = _create_connection(db_session, user, account_id="other", is_default=False)
    other_conn.broker_analytics_token_encrypted = encrypt("other-token")
    other_conn.data_status = "active"
    other_conn.data_source = "analytics_token"
    db_session.commit()

    from app.identity import get_analytics_token
    # Explicitly pin the non-default connection
    result = get_analytics_token(db_session, user.id, "UPSTOX", connection_id=other_conn.id)
    assert result == "other-token"


# ---------------------------------------------------------------------------
# Finding 4 — no duplicate API version authorities
# ---------------------------------------------------------------------------

def test_no_app_api_directory():
    """app/api/ should not exist (refactored to routers/)."""
    import os
    api_dir = os.path.join(os.path.dirname(__file__), "..", "app", "api")
    assert not os.path.exists(api_dir), "app/api/ directory should not exist"


def test_no_duplicate_api_version_constants():
    """No duplicate API_VERSION or API_VERSION_PREFIX constants."""
    import os
    app_dir = os.path.join(os.path.dirname(__file__), "..", "app")
    found = []
    for root, dirs, files in os.walk(app_dir):
        for f in files:
            if f.endswith(".py") and "__pycache__" not in root:
                path = os.path.join(root, f)
                with open(path) as fp:
                    content = fp.read()
                if "API_VERSION" in content and "API_VERSION_PREFIX" in content:
                    found.append(path)
    assert len(found) == 0, f"Found duplicate API_VERSION constants in: {found}"


def test_routes_still_mount():
    """All API routes should still be mounted correctly."""
    from app.main import app
    routes = [route.path for route in app.routes if hasattr(route, 'path')]
    assert any("/chains" in r for r in routes), "chains routes should be mounted"
    assert any("/auth" in r for r in routes), "auth routes should be mounted"
    assert any("/gex" in r for r in routes), "gex routes should be mounted"
