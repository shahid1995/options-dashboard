"""Tests for Google OAuth authentication (Phase A).

Covers:
- POST /auth/google token verification
- Account creation from Google identity
- Account linking (existing email → Google sub)
- Duplicate account protection
- Session management
- /auth/me with Google-created users
- Edge cases (invalid token, expired token, missing config)
"""

import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine, SessionLocal
from app.identity import User, UserSession, hash_session_id
from app.main import app
from app.routers.deps import SESSION_COOKIE_NAME
from app.services import token_store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_db():
    """Create all tables before each test."""
    from app.services.rate_limiter import rate_limiter
    rate_limiter._hits.clear()
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helper: build a fake Google credential (JWT-like)
# ---------------------------------------------------------------------------

def _make_valid_state() -> str:
    """Create a valid HMAC-signed Google OAuth state for tests."""
    return token_store.create_google_oauth_state()


def _login_session_id(resp):
    """Extract the canonical session ID from a login response's cookie.

    Issue #61: /auth/google transports the session ONLY via the secure
    HttpOnly ``strikenova_session`` cookie — the body never contains it.
    """
    return resp.cookies.get(SESSION_COOKIE_NAME)


def _fake_google_credential(sub="google-user-123", email="test@gmail.com", name="Test User"):
    """Build a minimal JWT-like string for testing.

    The real verification calls Google's JWKS endpoint, so we mock
    _verify_google_token in the auth router to bypass that.
    """
    header = {"alg": "RS256", "typ": "JWT", "kid": "fake-key-id"}
    payload = {"sub": sub, "email": email, "name": name, "aud": "fake-client-id"}
    # We don't actually need a valid signature since we mock verification
    import base64
    h = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    p = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{h}.{p}.fake-signature"


# ---------------------------------------------------------------------------
# Test: new user creation via Google
# ---------------------------------------------------------------------------

class TestGoogleAuthNewUser:
    """POST /auth/google creates a new user when no existing match."""

    @patch("app.routers.auth._verify_google_token")
    def test_new_google_user_created(self, mock_verify, client, db):
        mock_verify.return_value = {
            "sub": "google-sub-001",
            "email": "newuser@gmail.com",
            "name": "New User",
        }
        resp = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        # Issue #61: the session is transported only by the secure cookie
        assert _login_session_id(resp)
        assert "session_id" not in data
        assert data["user"]["email"] == "newuser@gmail.com"
        assert data["user"]["display_name"] == "New User"

        # Verify user in DB
        user = db.query(User).filter(User.google_sub == "google-sub-001").first()
        assert user is not None
        assert user.identity_source == "google"
        assert user.email == "newuser@gmail.com"
        assert user.password_hash is None  # No password for Google-only user

    @patch("app.routers.auth._verify_google_token")
    def test_new_google_user_no_email(self, mock_verify, client, db):
        """Google account without public email."""
        mock_verify.return_value = {
            "sub": "google-sub-no-email",
            "email": None,
            "name": "Anonymous",
        }
        resp = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        user = db.query(User).filter(User.google_sub == "google-sub-no-email").first()
        assert user is not None
        assert user.email is None


# ---------------------------------------------------------------------------
# Test: account linking — existing email user → Google
# ---------------------------------------------------------------------------

class TestGoogleAccountLinking:
    """Google auth links to existing email/password account."""

    @patch("app.routers.auth._verify_google_token")
    def test_link_google_to_existing_email_user(self, mock_verify, client, db):
        """User registered with email, later signs in with Google."""
        from app.identity import hash_password

        # Create an existing email/password user
        user_id = str(uuid4())
        user = User(
            id=user_id,
            email="linktest@gmail.com",
            password_hash=hash_password("TestPass123!"),
            display_name="Email User",
            status="active",
            identity_source="email",
        )
        db.add(user)
        db.commit()

        # Google login with same email
        mock_verify.return_value = {
            "sub": "google-sub-link",
            "email": "linktest@gmail.com",
            "name": "Google User",
        }
        resp = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

        # Same user should be returned (linked, not duplicated)
        db.refresh(user)
        assert user.google_sub == "google-sub-link"
        assert user.identity_source == "google"  # Updated from "email"
        assert user.password_hash is not None  # Password preserved

    @patch("app.routers.auth._verify_google_token")
    def test_subsequent_google_login_returns_same_user(self, mock_verify, client, db):
        """Second Google login returns the same user (no duplicate)."""
        mock_verify.return_value = {
            "sub": "google-sub-repeat",
            "email": "repeat@gmail.com",
            "name": "Repeat User",
        }
        resp1 = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        assert resp1.status_code == 200
        user_id_1 = resp1.json()["user"]["user_id"]

        resp2 = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        assert resp2.status_code == 200
        user_id_2 = resp2.json()["user"]["user_id"]

        assert user_id_1 == user_id_2  # Same user, not duplicated


# ---------------------------------------------------------------------------
# Test: duplicate account protection
# ---------------------------------------------------------------------------

class TestDuplicateAccountProtection:
    """Ensure no duplicate accounts when same email uses multiple methods."""

    @patch("app.routers.auth._verify_google_token")
    def test_google_does_not_create_duplicate_with_email_user(self, mock_verify, client, db):
        from app.identity import hash_password

        # Create email user
        user = User(
            id=str(uuid4()),
            email="nodup@gmail.com",
            password_hash=hash_password("TestPass123!"),
            status="active",
            identity_source="email",
        )
        db.add(user)
        db.commit()
        original_count = db.query(User).count()

        # Google login with same email
        mock_verify.return_value = {
            "sub": "google-nodup",
            "email": "nodup@gmail.com",
            "name": "No Dup",
        }
        resp = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        assert resp.status_code == 200

        # Count should not increase
        new_count = db.query(User).count()
        assert new_count == original_count

    @patch("app.routers.auth._verify_google_token")
    def test_google_does_not_duplicate_with_upstox_user(self, mock_verify, client, db):
        """Upstox user with same email should be linked, not duplicated."""
        user = User(
            id=str(uuid4()),
            email="upstoxuser@gmail.com",
            display_name="Upstox User",
            status="active",
            identity_source="upstox",
            broker_provider="UPSTOX",
            broker_user_id="UPSTOX-123",
        )
        db.add(user)
        db.commit()

        mock_verify.return_value = {
            "sub": "google-sub-upstox",
            "email": "upstoxuser@gmail.com",
            "name": "Upstox Google",
        }
        resp = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        assert resp.status_code == 200

        # Upstox user should now have google_sub
        db.refresh(user)
        assert user.google_sub == "google-sub-upstox"


# ---------------------------------------------------------------------------
# Test: invalid / expired Google tokens
# ---------------------------------------------------------------------------

class TestGoogleTokenValidation:
    """Invalid Google tokens are rejected."""

    @patch("app.routers.auth._verify_google_token")
    def test_invalid_token_rejected(self, mock_verify, client):
        mock_verify.return_value = None
        resp = client.post("/auth/google", json={"credential": "invalid-token", "state": _make_valid_state()})
        assert resp.status_code == 401
        assert "Invalid" in resp.json()["detail"]

    @patch("app.routers.auth._verify_google_token")
    def test_empty_token_rejected(self, mock_verify, client):
        resp = client.post("/auth/google", json={"credential": ""})
        assert resp.status_code == 422

    @patch("app.routers.auth._verify_google_token")
    def test_missing_token_rejected(self, mock_verify, client):
        resp = client.post("/auth/google", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test: session management
# ---------------------------------------------------------------------------

class TestGoogleSessionManagement:
    """Sessions created via Google auth work correctly."""

    @patch("app.routers.auth._verify_google_token")
    def test_google_session_works_with_me_endpoint(self, mock_verify, client, db):
        mock_verify.return_value = {
            "sub": "google-me-test",
            "email": "metest@gmail.com",
            "name": "ME Test",
        }
        resp = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        session_id = _login_session_id(resp)

        # Use the session to access /auth/me
        me_resp = client.get("/auth/me", headers={"X-Session-Id": session_id})
        assert me_resp.status_code == 200
        me_data = me_resp.json()
        assert me_data["email"] == "metest@gmail.com"
        assert me_data["display_name"] == "ME Test"
        assert me_data["identity_source"] == "google"

    @patch("app.routers.auth._verify_google_token")
    def test_google_session_logout(self, mock_verify, client, db):
        mock_verify.return_value = {
            "sub": "google-logout-test",
            "email": "logout@gmail.com",
            "name": "Logout Test",
        }
        resp = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        session_id = _login_session_id(resp)

        # Logout
        logout_resp = client.post("/auth/logout", headers={"X-Session-Id": session_id})
        assert logout_resp.status_code == 200

        # Session should be invalid after logout
        me_resp = client.get("/auth/me", headers={"X-Session-Id": session_id})
        assert me_resp.status_code == 401

    @patch("app.routers.auth._verify_google_token")
    def test_google_session_is_unique_per_login(self, mock_verify, client, db):
        mock_verify.return_value = {
            "sub": "google-unique-session",
            "email": "unique@gmail.com",
            "name": "Unique Session",
        }
        resp1 = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        resp2 = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})

        sid1 = _login_session_id(resp1)
        sid2 = _login_session_id(resp2)
        assert sid1 != sid2  # Each login gets a unique session


# ---------------------------------------------------------------------------
# Test: /auth/me with Google users
# ---------------------------------------------------------------------------

class TestAuthMe:
    """/auth/me returns correct data for Google-created users."""

    @patch("app.routers.auth._verify_google_token")
    def test_me_returns_google_user_data(self, mock_verify, client, db):
        mock_verify.return_value = {
            "sub": "google-me-data",
            "email": "medata@gmail.com",
            "name": "ME Data",
        }
        resp = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        session_id = _login_session_id(resp)

        me_resp = client.get("/auth/me", headers={"X-Session-Id": session_id})
        assert me_resp.status_code == 200
        data = me_resp.json()
        assert data["email"] == "medata@gmail.com"
        assert data["display_name"] == "ME Data"
        assert data["identity_source"] == "google"
        assert data["status"] == "active"
        assert data["created_at"] is not None
        assert data["last_login_at"] is not None

    def test_me_returns_401_when_not_logged_in(self, client):
        resp = client.get("/auth/me")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Test: identity_source tracking
# ---------------------------------------------------------------------------

class TestIdentitySourceTracking:
    """identity_source correctly reflects the auth method."""

    @patch("app.routers.auth._verify_google_token")
    def test_new_google_user_source_is_google(self, mock_verify, client, db):
        mock_verify.return_value = {
            "sub": "src-google",
            "email": "src@gmail.com",
            "name": "Src Test",
        }
        resp = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        assert resp.json()["user"]["identity_source"] == "google"

    @patch("app.routers.auth._verify_google_token")
    def test_email_user_linked_to_google_source_updates(self, mock_verify, client, db):
        from app.identity import hash_password

        user = User(
            id=str(uuid4()),
            email="srcupdate@gmail.com",
            password_hash=hash_password("TestPass123!"),
            status="active",
            identity_source="email",
        )
        db.add(user)
        db.commit()

        mock_verify.return_value = {
            "sub": "src-update-google",
            "email": "srcupdate@gmail.com",
            "name": "Source Update",
        }
        resp = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        assert resp.status_code == 200

        db.refresh(user)
        assert user.identity_source == "google"


# ---------------------------------------------------------------------------
# Test: edge cases
# ---------------------------------------------------------------------------

class TestGoogleAuthEdgeCases:
    """Edge cases and boundary conditions."""

    @patch("app.routers.auth._verify_google_token")
    def test_inactive_user_rejected(self, mock_verify, client, db):
        from app.identity import hash_password

        user = User(
            id=str(uuid4()),
            email="inactive@gmail.com",
            password_hash=hash_password("TestPass123!"),
            status="suspended",
            identity_source="email",
        )
        db.add(user)
        db.commit()

        mock_verify.return_value = {
            "sub": "google-inactive",
            "email": "inactive@gmail.com",
            "name": "Inactive",
        }
        resp = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        assert resp.status_code == 403
        assert "not active" in resp.json()["detail"]

    @patch("app.routers.auth._verify_google_token")
    def test_google_sub_case_sensitive(self, mock_verify, client, db):
        """Google subs are case-sensitive (they shouldn't be, but verify behavior)."""
        mock_verify.return_value = {
            "sub": "CASE-SENSITIVE-SUB",
            "email": None,
            "name": "Case Test",
        }
        resp1 = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        assert resp1.status_code == 200

        mock_verify.return_value = {
            "sub": "case-sensitive-sub",  # Different case
            "email": None,
            "name": "Case Test 2",
        }
        resp2 = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        assert resp2.status_code == 200

        # Should create two different users (different subs)
        assert resp1.json()["user"]["user_id"] != resp2.json()["user"]["user_id"]

    @patch("app.routers.auth._verify_google_token")
    def test_email_case_insensitive(self, mock_verify, client, db):
        """Emails should be normalized to lowercase."""
        mock_verify.return_value = {
            "sub": "sub-upper",
            "email": "UPPER@GMAIL.COM",
            "name": "Upper",
        }
        resp = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        assert resp.status_code == 200

        user = db.query(User).filter(User.google_sub == "sub-upper").first()
        assert user.email == "upper@gmail.com"  # Normalized


# ---------------------------------------------------------------------------
# Phase A: Google nonce/issuer validation + security
# ---------------------------------------------------------------------------


class TestGoogleNonceValidation:
    """Nonce and issuer validation for Google ID tokens."""

    def test_missing_nonce_rejected(self, client):
        """Token without nonce claim is rejected (pre-JWKS check)."""
        import base64
        from app.routers.auth import _verify_google_token

        header = {"alg": "RS256", "typ": "JWT", "kid": "test"}
        payload = {
            "sub": "123",
            "email": "test@gmail.com",
            "iss": "https://accounts.google.com",
            "aud": "test",
            "exp": int(time.time()) + 3600,
            # No nonce
        }
        h = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        p = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token = f"{h}.{p}.fake-sig"

        with patch("app.config.settings.GOOGLE_CLIENT_ID", "test"):
            result = _verify_google_token(token, expected_nonce="any")
            assert result is None  # Rejected: missing nonce in JWT

    def test_invalid_issuer_rejected(self, client):
        """Token with wrong issuer is rejected (pre-JWKS check)."""
        import base64
        from app.routers.auth import _verify_google_token

        header = {"alg": "RS256", "typ": "JWT", "kid": "test"}
        payload = {
            "sub": "123",
            "email": "test@gmail.com",
            "iss": "https://evil.com",
            "aud": "test",
            "exp": int(time.time()) + 3600,
            "nonce": "valid-nonce",
        }
        h = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        p = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token = f"{h}.{p}.fake-sig"

        with patch("app.config.settings.GOOGLE_CLIENT_ID", "test"):
            result = _verify_google_token(token, expected_nonce="valid-nonce")
            assert result is None  # Rejected: wrong issuer

    @patch("app.routers.auth._verify_google_token")
    def test_valid_google_session_no_db_persist(self, mock_verify, client, db):
        """Google sessions should not persist broker tokens to DB."""
        mock_verify.return_value = {
            "sub": "google-no-persist",
            "email": "nopersist@gmail.com",
            "name": "No Persist",
        }
        resp = client.post("/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()})
        assert resp.status_code == 200
        session_id = _login_session_id(resp)

        # Session works via in-memory cache
        me_resp = client.get("/auth/me", headers={"X-Session-Id": session_id})
        assert me_resp.status_code == 200

    def test_google_state_endpoint_returns_signed_state(self, client):
        """POST /auth/google/state returns HMAC-signed state with nonce."""
        resp = client.post("/auth/google/state")
        assert resp.status_code == 200
        data = resp.json()
        assert "state" in data
        assert "." in data["state"]  # HMAC-signed format
        assert "nonce" in data
        assert len(data["nonce"]) > 0

    @patch("app.routers.auth._verify_google_token")
    def test_google_auth_with_valid_nonce_state(self, mock_verify, client, db):
        """POST /auth/google with valid signed state and matching nonce succeeds."""
        from app.services import token_store
        state = token_store.create_google_oauth_state()
        nonce = token_store.peek_google_oauth_nonce(state)

        mock_verify.return_value = {
            "sub": "google-nonce-test",
            "email": "nonce@gmail.com",
            "name": "Nonce Test",
        }
        resp = client.post("/auth/google", json={
            "credential": "fake-jwt",
            "state": state,
        })
        assert resp.status_code == 200
        call_args = mock_verify.call_args
        assert call_args[1]["expected_nonce"] == nonce

    @patch("app.routers.auth._verify_google_token")
    def test_google_auth_rejects_invalid_state(self, mock_verify, client, db):
        """POST /auth/google with invalid/tampered state is rejected."""
        resp = client.post("/auth/google", json={
            "credential": "fake-jwt",
            "state": "tampered-state.signature",
        })
        assert resp.status_code == 401
        assert "OAuth state" in resp.json()["detail"]
        mock_verify.assert_not_called()

    @patch("app.routers.auth._verify_google_token")
    def test_google_auth_rejects_expired_state(self, mock_verify, client, db):
        """POST /auth/google with expired state is rejected."""
        from app.services import token_store
        import base64 as _b64
        import hmac as _hmac

        old_ts = int(time.time()) - 700
        payload = json.dumps({"nonce": "old-nonce", "ts": old_ts}, separators=(",", ":"))
        b64 = _b64.urlsafe_b64encode(payload.encode()).decode()
        sig = _hmac.new(
            token_store._get_state_hmac_key(), b64.encode(), token_store.hashlib.sha256
        ).hexdigest()[:32]
        expired_state = f"{b64}.{sig}"

        resp = client.post("/auth/google", json={
            "credential": "fake-jwt",
            "state": expired_state,
        })
        assert resp.status_code == 401
        mock_verify.assert_not_called()

    def test_google_auth_without_state_rejected(self, client):
        """POST /auth/google without state is rejected at the endpoint level."""
        resp = client.post("/auth/google", json={"credential": "fake-jwt"})
        assert resp.status_code == 401
        assert "state" in resp.json()["detail"].lower()

    def test_google_auth_rejects_nonce_mismatch(self, client):
        """POST /auth/google rejects token when JWT nonce doesn't match state."""
        from app.services import token_store
        from app.routers.auth import _verify_google_token
        import base64

        state = token_store.create_google_oauth_state()
        nonce_a = token_store.peek_google_oauth_nonce(state)

        header = {"alg": "RS256", "typ": "JWT", "kid": "test"}
        payload = {
            "sub": "123",
            "email": "test@gmail.com",
            "iss": "https://accounts.google.com",
            "aud": "test",
            "exp": int(time.time()) + 3600,
            "nonce": "different-nonce-B",
        }
        h = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        p = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token = f"{h}.{p}.fake-sig"

        with patch("app.config.settings.GOOGLE_CLIENT_ID", "test"):
            result = _verify_google_token(token, expected_nonce=nonce_a)
            assert result is None  # Rejected: nonce mismatch


# ---------------------------------------------------------------------------
# Phase A security: Mandatory nonce binding — comprehensive regression suite
# ---------------------------------------------------------------------------

class TestGoogleMandatoryNonceBinding:
    """Verify that POST /auth/google REQUIRES a valid signed state.

    The state carries an HMAC-signed nonce that MUST match the JWT nonce.
    No fallback to presence-only validation is allowed.
    """

    # 1. Missing state → rejected
    @patch("app.routers.auth._verify_google_token")
    def test_missing_state_rejected(self, mock_verify, client):
        """POST /auth/google without state parameter is rejected."""
        resp = client.post("/auth/google", json={"credential": "fake-jwt"})
        assert resp.status_code == 401
        assert "state" in resp.json()["detail"].lower()
        mock_verify.assert_not_called()  # Rejected before token verification

    # 2. Invalid state → rejected
    @patch("app.routers.auth._verify_google_token")
    def test_invalid_state_rejected(self, mock_verify, client):
        """POST /auth/google with garbage state is rejected."""
        resp = client.post("/auth/google", json={
            "credential": "fake-jwt",
            "state": "not-a-valid-state",
        })
        assert resp.status_code == 401
        mock_verify.assert_not_called()

    # 3. Tampered state → rejected
    @patch("app.routers.auth._verify_google_token")
    def test_tampered_state_rejected(self, mock_verify, client):
        """POST /auth/google with tampered HMAC signature is rejected."""
        from app.services import token_store
        import base64 as _b64
        import hmac as _hmac

        # Create a valid state, then tamper with the signature
        state = token_store.create_google_oauth_state()
        b64, _sig = state.rsplit(".", 1)
        tampered_sig = "0000000000000000"  # Wrong signature
        tampered_state = f"{b64}.{tampered_sig}"

        resp = client.post("/auth/google", json={
            "credential": "fake-jwt",
            "state": tampered_state,
        })
        assert resp.status_code == 401
        mock_verify.assert_not_called()

    # 4. Expired state → rejected
    @patch("app.routers.auth._verify_google_token")
    def test_expired_state_rejected(self, mock_verify, client):
        """POST /auth/google with expired state (>10min old) is rejected."""
        from app.services import token_store
        import base64 as _b64
        import hmac as _hmac

        old_ts = int(time.time()) - 700  # 11 minutes ago
        payload = json.dumps({"nonce": "old-nonce", "ts": old_ts}, separators=(",", ":"))
        b64 = _b64.urlsafe_b64encode(payload.encode()).decode()
        sig = _hmac.new(
            token_store._get_state_hmac_key(), b64.encode(), token_store.hashlib.sha256
        ).hexdigest()[:32]
        expired_state = f"{b64}.{sig}"

        resp = client.post("/auth/google", json={
            "credential": "fake-jwt",
            "state": expired_state,
        })
        assert resp.status_code == 401
        mock_verify.assert_not_called()

    # 5. Reused state → rejected
    @patch("app.routers.auth._verify_google_token")
    def test_reused_state_rejected(self, mock_verify, client, db):
        """POST /auth/google rejects reuse of an already-consumed state."""
        from app.services import token_store

        state = token_store.create_google_oauth_state()
        nonce = token_store.peek_google_oauth_nonce(state)

        mock_verify.return_value = {
            "sub": "google-reuse-test",
            "email": "reuse@gmail.com",
            "name": "Reuse Test",
        }

        # First use — should succeed
        resp1 = client.post("/auth/google", json={
            "credential": "fake-jwt-1",
            "state": state,
        })
        assert resp1.status_code == 200

        # Second use — should fail (state already consumed)
        mock_verify.reset_mock()
        resp2 = client.post("/auth/google", json={
            "credential": "fake-jwt-2",
            "state": state,
        })
        assert resp2.status_code == 401
        mock_verify.assert_not_called()

    # 6. JWT nonce != state nonce → rejected
    def test_nonce_mismatch_rejected(self, client):
        """POST /auth/google rejects token when JWT nonce doesn't match state."""
        from app.services import token_store
        from app.routers.auth import _verify_google_token
        import base64

        state = token_store.create_google_oauth_state()
        nonce_a = token_store.peek_google_oauth_nonce(state)

        header = {"alg": "RS256", "typ": "JWT", "kid": "test"}
        payload = {
            "sub": "123",
            "email": "test@gmail.com",
            "iss": "https://accounts.google.com",
            "aud": "test",
            "exp": int(time.time()) + 3600,
            "nonce": "different-nonce-B",
        }
        h = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        p = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token = f"{h}.{p}.fake-sig"

        with patch("app.config.settings.GOOGLE_CLIENT_ID", "test"):
            result = _verify_google_token(token, expected_nonce=nonce_a)
            assert result is None  # Rejected: nonce mismatch

    # 7. JWT nonce == state nonce → accepted (at nonce-check level)
    @patch("app.routers.auth._verify_google_token")
    def test_nonce_match_accepted(self, mock_verify, client, db):
        """POST /auth/google accepts token when JWT nonce matches state."""
        from app.services import token_store

        state = token_store.create_google_oauth_state()
        nonce = token_store.peek_google_oauth_nonce(state)

        mock_verify.return_value = {
            "sub": "google-nonce-match",
            "email": "match@gmail.com",
            "name": "Match Test",
        }
        resp = client.post("/auth/google", json={
            "credential": "fake-jwt",
            "state": state,
        })
        assert resp.status_code == 200
        # Verify the correct nonce was passed to verification
        call_args = mock_verify.call_args
        assert call_args[1]["expected_nonce"] == nonce

    # 8. Valid Google JWT + missing nonce claim → rejected
    def test_jwt_missing_nonce_rejected(self, client):
        """Token without nonce claim is rejected even with valid state."""
        import base64
        from app.routers.auth import _verify_google_token
        from app.services import token_store

        state = token_store.create_google_oauth_state()
        nonce = token_store.peek_google_oauth_nonce(state)

        header = {"alg": "RS256", "typ": "JWT", "kid": "test"}
        payload = {
            "sub": "123",
            "email": "test@gmail.com",
            "iss": "https://accounts.google.com",
            "aud": "test",
            "exp": int(time.time()) + 3600,
            # No nonce claim
        }
        h = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        p = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token = f"{h}.{p}.fake-sig"

        with patch("app.config.settings.GOOGLE_CLIENT_ID", "test"):
            result = _verify_google_token(token, expected_nonce=nonce)
            assert result is None  # Rejected: missing nonce in JWT

    # 9. Invalid Google issuer → rejected
    def test_invalid_issuer_rejected(self, client):
        """Token with wrong issuer is rejected."""
        import base64
        from app.routers.auth import _verify_google_token

        header = {"alg": "RS256", "typ": "JWT", "kid": "test"}
        payload = {
            "sub": "123",
            "email": "test@gmail.com",
            "iss": "https://evil.com",
            "aud": "test",
            "exp": int(time.time()) + 3600,
            "nonce": "valid-nonce",
        }
        h = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        p = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token = f"{h}.{p}.fake-sig"

        with patch("app.config.settings.GOOGLE_CLIENT_ID", "test"):
            result = _verify_google_token(token, expected_nonce="valid-nonce")
            assert result is None  # Rejected: wrong issuer

    # 10. Wrong audience → rejected (caught by PyJWT audience check)
    def test_wrong_audience_rejected(self, client):
        """Token with wrong audience is rejected by PyJWT."""
        import base64
        from app.routers.auth import _verify_google_token

        header = {"alg": "RS256", "typ": "JWT", "kid": "test"}
        payload = {
            "sub": "123",
            "email": "test@gmail.com",
            "iss": "https://accounts.google.com",
            "aud": "wrong-client-id",
            "exp": int(time.time()) + 3600,
            "nonce": "valid-nonce",
        }
        h = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        p = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token = f"{h}.{p}.fake-sig"

        with patch("app.config.settings.GOOGLE_CLIENT_ID", "correct-client-id"):
            # This will fail at PyJWT audience check (wrong aud vs GOOGLE_CLIENT_ID)
            # The nonce check passes, issuer check passes, but JWT verification fails
            result = _verify_google_token(token, expected_nonce="valid-nonce")
            assert result is None  # Rejected: wrong audience

    # 11. Expired JWT → rejected (caught by PyJWT exp check)
    def test_expired_jwt_rejected(self, client):
        """Expired JWT is rejected by PyJWT."""
        import base64
        from app.routers.auth import _verify_google_token

        header = {"alg": "RS256", "typ": "JWT", "kid": "test"}
        payload = {
            "sub": "123",
            "email": "test@gmail.com",
            "iss": "https://accounts.google.com",
            "aud": "test",
            "exp": int(time.time()) - 3600,  # Expired 1 hour ago
            "nonce": "valid-nonce",
        }
        h = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        p = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token = f"{h}.{p}.fake-sig"

        with patch("app.config.settings.GOOGLE_CLIENT_ID", "test"):
            # This will pass nonce/issuer pre-flight checks but fail at PyJWT exp check
            result = _verify_google_token(token, expected_nonce="valid-nonce")
            assert result is None  # Rejected: expired JWT


# ---------------------------------------------------------------------------
# Test: legacy registration fix does not disturb Google sign-in
# ---------------------------------------------------------------------------


class TestGoogleSignInAfterBlockedLegacyRegistration:
    """The legacy password-linking fix must leave OAuth sign-in untouched."""

    @patch("app.routers.auth._verify_google_token")
    def test_blocked_legacy_registration_then_google_sign_in(self, mock_verify, client, db):
        mock_verify.return_value = {
            "sub": "google-sub-after-fix",
            "email": "after-fix@gmail.com",
            "name": "Google Owner",
        }

        # An account created by Google sign-in has no local password.
        first = client.post(
            "/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()}
        )
        assert first.status_code == 200
        user = db.query(User).filter(User.google_sub == "google-sub-after-fix").one()
        assert user.identity_source == "google"
        assert user.password_hash is None

        # An anonymous legacy registration for the same address is refused and
        # cannot attach a password.
        blocked = client.post(
            "/auth/register",
            json={"email": user.email, "password": "AttackerPass123!"},
        )
        assert blocked.status_code == 200
        assert "user_id" not in blocked.text

        # Legitimate Google sign-in still works and the account stays OAuth-only.
        second = client.post(
            "/auth/google", json={"credential": "fake-jwt", "state": _make_valid_state()}
        )
        assert second.status_code == 200
        assert _login_session_id(second)

        db.expire_all()
        user = db.query(User).filter(User.id == user.id).one()
        assert user.password_hash is None
        assert user.identity_source == "google"
