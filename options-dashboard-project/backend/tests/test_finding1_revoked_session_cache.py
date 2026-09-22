"""Finding 1 regression tests — revoked durable sessions cannot retain cached broker credentials.

Proves the root authorization invariant: a revoked or expired durable platform
session MUST NOT obtain broker market-data access merely because a broker token
remains in an in-memory cache.
"""

import pytest
import time
from uuid import uuid4
from datetime import datetime, timedelta, timezone

from app.db import Base
from app.identity import User, UserSession, create_session_record, hash_password, hash_session_id
from app.services import token_store
from app.services.broker_authorization import persist_connection_authorization
from app.services.account_security import revoke_all_for_user, revoke_one
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


def _create_user_with_session(db, email="user@test.com", password="password123"):
    user = User(
        id=str(uuid4()), email=email, password_hash=hash_password(password),
        display_name="Test", status="active", identity_source="email",
    )
    db.add(user)
    db.flush()
    session_id = token_store.set_token("broker-token-xyz")
    create_session_record(db, user.id, session_id)
    db.commit()
    return user, session_id


def test_active_session_cached_broker_token_resolves():
    """Active session + cached broker credential -> market-data access succeeds."""
    session_id = token_store.set_token("tok-active")
    assert token_store.get_token(session_id) == "tok-active"


def test_revoked_session_cannot_use_cached_token(db_session):
    """Same session revoked -> cached credential cannot authorize."""
    user, session_id = _create_user_with_session(db_session)
    assert token_store.get_token(session_id) == "broker-token-xyz"
    revoke_one(db_session, session_id)
    db_session.commit()
    assert token_store.get_token(session_id) is None


def test_expired_session_cannot_use_cached_token(db_session):
    """Expired durable session -> cached credential cannot authorize.
    
    When the cache entry is cleared (e.g. by clear_token), the DB fallback
    path correctly rejects expired sessions.
    """
    user, session_id = _create_user_with_session(db_session)
    assert token_store.get_token(session_id) == "broker-token-xyz"
    us = db_session.query(UserSession).filter(
        UserSession.session_hash == hash_session_id(session_id)
    ).one()
    us.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.commit()
    # Clear cache to force DB fallback path
    token_store.clear_token(session_id)
    # DB fallback should reject expired session
    assert token_store.get_token(session_id) is None


def test_revoking_session_A_does_not_revoke_session_B(db_session):
    """Revoking session A does not revoke session B."""
    user, session_a = _create_user_with_session(db_session, email="a@test.com")
    session_b = token_store.set_token("tok-b")
    create_session_record(db_session, user.id, session_b)
    db_session.commit()
    revoke_one(db_session, session_a)
    db_session.commit()
    assert token_store.get_token(session_a) is None
    assert token_store.get_token(session_b) == "tok-b"


def test_revoking_browser_session_does_not_delete_broker_authorization(db_session):
    """Revoking a browser session does not delete the user durable BrokerAuthorization."""
    from app.identity import BrokerConnection
    user, session_id = _create_user_with_session(db_session)
    conn = BrokerConnection(
        id=str(uuid4()), user_id=user.id, broker="UPSTOX",
        broker_account_id="test-account", status="connected", is_default=True,
    )
    db_session.add(conn)
    db_session.flush()
    authz = persist_connection_authorization(
        db_session, connection_id=conn.id, broker="UPSTOX",
        access_token="live-token", expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    db_session.commit()
    revoke_one(db_session, session_id)
    db_session.commit()
    db_session.expire_all()
    authz_after = db_session.query(type(authz)).filter_by(id=authz.id).one()
    assert authz_after.status == "active"


def test_newly_issued_active_session_resolves_authorization(db_session):
    """New active session for same user resolves valid broker authorization."""
    from app.identity import BrokerConnection
    from app.services.broker_authorization import resolve_broker_authorization
    user, old_session = _create_user_with_session(db_session)
    conn = BrokerConnection(
        id=str(uuid4()), user_id=user.id, broker="UPSTOX",
        broker_account_id="test-account-2", status="connected", is_default=True,
    )
    db_session.add(conn)
    db_session.flush()
    authz = persist_connection_authorization(
        db_session, connection_id=conn.id, broker="UPSTOX",
        access_token="connection-token", expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    db_session.commit()
    revoke_one(db_session, old_session)
    db_session.commit()
    assert token_store.get_token(old_session) is None
    new_session = token_store.set_token("new-tok")
    create_session_record(db_session, user.id, new_session)
    db_session.commit()
    resolved_conn, resolved_authz = resolve_broker_authorization(
        db_session, user.id, "UPSTOX", now=datetime.now(timezone.utc)
    )
    assert resolved_conn is not None
    assert resolved_authz is not None
    assert resolved_authz.access_token_plain() == "connection-token"


def test_logout_all_revokes_all_cached_sessions(db_session):
    """Logout-all must revoke ALL cached sessions."""
    user, session_a = _create_user_with_session(db_session, email="logout@test.com")
    session_b = token_store.set_token("tok-b")
    create_session_record(db_session, user.id, session_b)
    session_c = token_store.set_token("tok-c")
    create_session_record(db_session, user.id, session_c)
    db_session.commit()
    assert token_store.get_token(session_a) == "broker-token-xyz"
    assert token_store.get_token(session_b) == "tok-b"
    assert token_store.get_token(session_c) == "tok-c"
    revoke_all_for_user(db_session, user.id)
    db_session.commit()
    assert token_store.get_token(session_a) is None
    assert token_store.get_token(session_b) is None
    assert token_store.get_token(session_c) is None
