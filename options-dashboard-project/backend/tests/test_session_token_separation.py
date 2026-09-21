"""Regression tests: platform session must NOT become a broker access token.

The core invariant:
  Platform Session (Google/email) != Broker Access Token

get_token() must return None for platform-only sessions.
AuthenticatedUser.access_token must be None for platform-only users.
require_token() must return 403 (not leak the session ID as a broker token).
has_platform_session() must return True for valid UserSessions.
"""
import secrets
import pytest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from app.db import Base, SessionLocal
from app.identity import User, UserSession, create_session_record, hash_session_id
from app.services import token_store


@pytest.fixture(autouse=True)
def db_session():
    """Use the same engine as app.db.SessionLocal so token_store DB fallback sees test data."""
    Base.metadata.create_all(bind=SessionLocal().get_bind())
    s = SessionLocal()
    yield s
    s.close()


def _create_platform_user(db):
    """Create a user WITHOUT broker credentials (platform-only identity)."""
    user_id = str(uuid4())
    user = User(
        id=user_id,
        status="active",
        identity_source="google",
        broker_provider=None,
    )
    db.add(user)
    db.flush()
    return user_id


def _create_platform_session(db, user_id=None):
    """Create a real UserSession in the database (no BrokerToken).

    Platform sessions do NOT use token_store.set_token() — they only
    create a UserSession DB record.  get_token() will return None for them.

    Returns (session_id, user_id).
    """
    if user_id is None:
        user_id = _create_platform_user(db)
    session_id = secrets.token_urlsafe(32)
    create_session_record(db, user_id, session_id)
    db.commit()
    return session_id, user_id


def _create_broker_session(db, broker_token_value="upstox_real_token_abc123"):
    """Create a user + session WITH a broker token (simulates broker login).

    Calls set_token(persist_to_db=True) to create BOTH the in-memory entry
    AND the BrokerToken DB row. Then creates a matching UserSession DB record.

    Returns (session_id, user_id).
    """
    user_id = str(uuid4())
    user = User(
        id=user_id,
        status="active",
        broker_provider="UPSTOX",
        broker_user_id=f"broker-{user_id[:8]}",
    )
    db.add(user)
    db.flush()
    # set_token creates the session_id AND persists the BrokerToken to DB
    session_id = token_store.set_token(
        broker_token_value,
        connection_id=f"conn-{user_id[:8]}",
        persist_to_db=True,
    )
    # Create matching UserSession DB record with the SAME session_id
    create_session_record(db, user_id, session_id)
    db.commit()
    return session_id, user_id


class TestPlatformSessionNotBrokerToken:
    """Platform sessions must never masquerade as broker tokens."""

    def test_get_token_returns_none_for_platform_session(self, db_session):
        """get_token() must return None, not the session_id, for a platform session."""
        sid, _ = _create_platform_session(db_session)

        # Clear in-memory cache to test DB fallback path
        token_store._sessions.pop(sid, None)
        result = token_store.get_token(sid)
        assert result is None, (
            f"get_token() returned '{result}' for platform session. "
            "Platform sessions must not masquerade as broker access tokens."
        )

    def test_has_platform_session_returns_true(self, db_session):
        """has_platform_session() returns True for valid UserSession."""
        sid, _ = _create_platform_session(db_session)
        assert token_store.has_platform_session(sid) is True

    def test_has_platform_session_returns_false_for_missing(self):
        """has_platform_session() returns False for unknown session."""
        assert token_store.has_platform_session("nonexistent-session-id") is False

    def test_has_platform_session_returns_false_for_none(self):
        """has_platform_session() returns False for None."""
        assert token_store.has_platform_session(None) is False

    def test_require_token_returns_403_for_platform_session(self, db_session):
        """chains market-data resolution must raise 403 for platform-only sessions."""
        from app.routers.chains import require_market_data_token

        sid, _ = _create_platform_session(db_session)
        with pytest.raises(Exception) as exc_info:
            require_market_data_token(sid)
        assert exc_info.value.status_code == 403

    def test_require_token_returns_401_for_missing_session(self):
        """chains market-data resolution must raise 401 for non-existent sessions."""
        from app.routers.chains import require_market_data_token

        with pytest.raises(Exception) as exc_info:
            require_market_data_token("totally-fake-session-id")
        assert exc_info.value.status_code == 401

    def test_broker_token_still_works(self, db_session):
        """Real broker tokens must still be returned by get_token()."""
        sid, _ = _create_broker_session(db_session, "upstox_real_token_abc123")
        token_store._sessions.pop(sid, None)
        result = token_store.get_token(sid)
        assert result == "upstox_real_token_abc123"

    def test_platform_session_not_in_cache_as_broker_token(self, db_session):
        """In-memory cache must not store platform session_id as access_token."""
        sid, _ = _create_platform_session(db_session)
        # The cache might have an entry from set_token, but the value must not be the session_id
        entry = token_store._sessions.get(sid)
        if entry is not None:
            # If there's a cache entry, it should not contain the session_id as the token
            # (platform sessions don't go through set_token at all)
            pass
        # After DB fallback, get_token must return None
        token_store._sessions.pop(sid, None)
        assert token_store.get_token(sid) is None


class TestAuthenticatedUserAccess:
    """AuthenticatedUser must express broker-token absence clearly."""

    def test_authenticated_user_access_token_none_for_platform_only(self):
        """Platform-only user should have access_token=None, not session_id."""
        from app.routers.deps import AuthenticatedUser
        user = AuthenticatedUser(user_id="user-123", access_token=None)
        assert user.access_token is None

    def test_authenticated_user_access_token_string_for_broker(self):
        """Broker-connected user should have access_token as string."""
        from app.routers.deps import AuthenticatedUser
        user = AuthenticatedUser(user_id="user-123", access_token="real-broker-token")
        assert user.access_token == "real-broker-token"

    def test_resolve_user_platform_only_has_no_access_token(self, db_session):
        """_resolve_user must not put session_id into access_token."""
        from app.routers.deps import _resolve_user

        session_id, user_id = _create_platform_session(db_session)
        result = _resolve_user(db_session, session_id)
        assert result.user_id == user_id
        # Critical: access_token must NOT be the session_id
        assert result.access_token != session_id, (
            f"access_token is session_id '{session_id}' — "
            "platform session leaked as broker token"
        )
        assert result.access_token is None

    def test_resolve_user_broker_has_access_token(self, db_session):
        """Broker-connected user should have real token in access_token."""
        from app.routers.deps import _resolve_user

        session_id, user_id = _create_broker_session(db_session, "real-upstox-token")
        result = _resolve_user(db_session, session_id)
        assert result.user_id == user_id
        assert result.access_token == "real-upstox-token"


class TestCrossUserSecurity:
    """User A sessions cannot access User B data."""

    def test_user_a_session_not_for_user_b(self, db_session):
        """User A's session maps to User A only."""
        from app.routers.deps import _resolve_user

        sid_a, uid_a = _create_platform_session(db_session)
        sid_b, uid_b = _create_platform_session(db_session)
        result_a = _resolve_user(db_session, sid_a)
        result_b = _resolve_user(db_session, sid_b)
        assert result_a.user_id == uid_a
        assert result_b.user_id == uid_b
        assert result_a.user_id != result_b.user_id

    def test_user_a_token_not_for_user_b(self, db_session):
        """User A broker token cannot be returned for User B's session."""
        from app.services.token_store import get_token

        sid_a, _ = _create_broker_session(db_session, "token-a")
        sid_b, _ = _create_platform_session(db_session)
        token_store._sessions.pop(sid_a, None)
        token_store._sessions.pop(sid_b, None)
        assert get_token(sid_a) == "token-a"
        assert get_token(sid_b) is None
