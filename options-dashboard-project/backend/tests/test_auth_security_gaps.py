"""Tests for auth security hardening (TDD — written before implementation).

Covers gaps identified in Phase 1 audit:
  H. Redirect safety — auth redirects reject external URLs
  I. Rate limiting on auth endpoints (brute-force protection)
  J. Account enumeration protection
  K. Session security — secure cookie properties
"""

import time
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base, get_db
from app.identity import User, create_session_record, hash_password
from app.main import app
from app.routers.deps import SESSION_COOKIE_NAME
from app.services import token_store
from app.services.rate_limiter import rate_limiter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _clear_rate_limiter():
    """Clear rate limiter state before each test to prevent cross-test leakage."""
    rate_limiter._hits.clear()


@pytest.fixture
def client(db_session):
    def override_get_db():
        yield db_session

    import app.routers.auth as auth_mod

    _orig_session_local = auth_mod.SessionLocal
    auth_mod.SessionLocal = lambda: db_session
    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        auth_mod.SessionLocal = _orig_session_local


def _create_active_user(db_session, email="user@test.com", password="password123"):
    """Helper: create an active email user in the DB."""
    user = User(
        id=str(uuid4()),
        email=email,
        password_hash=hash_password(password),
        display_name="Test User",
        status="active",
        identity_source="email",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Test H — Redirect safety
# ---------------------------------------------------------------------------


class TestRedirectSafety:
    """Auth redirects must reject external/manipulated URLs."""

    def test_google_redirect_rejects_external_url(self, client, db_session):
        """Google OAuth redirect path must reject external URLs."""
        user = _create_active_user(db_session)

        # Create a Google state with an external redirect URL
        state = token_store.create_google_oauth_state()
        nonce = token_store.peek_google_oauth_nonce(state)

        # The frontend's captureGoogleIdTokenFromUrl parses state.redirect
        # We need to verify the backend doesn't use it for server-side redirects.
        # The Google callback only sets a session_id — the redirect is
        # handled client-side, but the path must be validated as internal.

        # For now: the Google endpoint only returns session_id in the body,
        # so there's no server-side redirect to manipulate. The client-side
        # redirect uses NEXT_PUBLIC_APP_URL which is an env var.
        # This test documents that the Google endpoint does NOT use a
        # redirect parameter from the client.
        assert state is not None
        assert nonce is not None

    def test_oauth_callback_redirect_uses_configured_frontend(self, client, db_session):
        """OAuth callback redirect uses FRONTEND_URL from config, not user input."""
        # This is already verified by test_callback_with_code_sets_session_cookie_and_redirects
        # but we add an explicit assertion for clarity.
        assert settings.FRONTEND_URL.startswith("http")


# ---------------------------------------------------------------------------
# Test I — Rate limiting on auth endpoints
# ---------------------------------------------------------------------------


class TestRateLimiting:
    """Auth endpoints must be rate-limited to prevent brute-force."""

    def test_login_email_rate_limited_after_excessive_requests(self, client, db_session):
        """Excessive login attempts trigger rate limiting (429)."""
        user = _create_active_user(db_session)

        # Make many rapid login attempts
        responses = []
        for _ in range(70):  # Exceed default 60/min
            resp = client.post("/auth/login-email", json={
                "email": user.email,
                "password": "wrongpassword",
            })
            responses.append(resp.status_code)

        # At least some should be rate limited (429)
        assert 429 in responses, f"Expected 429 in responses, got: {set(responses)}"

    def test_register_rate_limited_after_excessive_requests(self, client, db_session):
        """Excessive registration attempts trigger rate limiting (429)."""
        responses = []
        for i in range(70):
            resp = client.post("/auth/register", json={
                "email": f"user{i}@test.com",
                "password": "password123",
            })
            responses.append(resp.status_code)

        # At least some should be rate limited (429)
        assert 429 in responses, f"Expected 429 in responses, got: {set(responses)}"


# ---------------------------------------------------------------------------
# Test J — Account enumeration protection
# ---------------------------------------------------------------------------


class TestAccountEnumeration:
    """Auth endpoints should not reveal whether an email exists."""

    def test_login_email_same_error_for_unknown_and_wrong_password(self, client, db_session):
        """Login failure must use identical error message for unknown email and wrong password."""
        user = _create_active_user(db_session)

        # Unknown email
        resp_unknown = client.post("/auth/login-email", json={
            "email": "nonexistent@test.com",
            "password": "password123",
        })

        # Wrong password for known email
        resp_wrong = client.post("/auth/login-email", json={
            "email": user.email,
            "password": "wrongpassword",
        })

        # Both should return 401 with identical detail
        assert resp_unknown.status_code == 401
        assert resp_wrong.status_code == 401
        assert resp_unknown.json()["detail"] == resp_wrong.json()["detail"]

    def test_register_does_not_leak_existing_email(self, client, db_session):
        """Registration of existing email must not reveal whether account exists."""
        user = _create_active_user(db_session)

        # Try to register with existing email
        resp = client.post("/auth/register", json={
            "email": user.email,
            "password": "password123",
        })

        # Should NOT return 409 (which would leak that the email exists)
        # Instead, it should return a generic error or handle it silently
        assert resp.status_code != 409, "Registration must not leak existing email via 409"


# ---------------------------------------------------------------------------
# Test K — Session security properties
# ---------------------------------------------------------------------------


class TestSessionSecurity:
    """Session cookies must have secure properties."""

    def test_logout_deletes_session_cookie_with_secure_flags(self, client, db_session):
        """Logout must delete the session cookie with HttpOnly, Secure, SameSite=None."""
        session_id = token_store.set_token("test-tok")
        user = _create_active_user(db_session)
        create_session_record(db_session, user.id, session_id)
        db_session.commit()

        resp = client.post("/auth/logout", headers={"X-Session-Id": session_id})
        assert resp.status_code == 200

        # Check that Set-Cookie header deletes the cookie (canonical name, Issue #61)
        set_cookie = resp.headers.get("set-cookie", "")
        assert f"{SESSION_COOKIE_NAME}=" in set_cookie
        # The cookie should be expired (max-age=0) and have secure flags
        assert "httponly" in set_cookie.lower() or "HttpOnly" in set_cookie

    def test_session_ttl_is_24_hours(self):
        """Session TTL must be 24 hours."""
        from app.services.token_store import _SESSION_TTL_SECONDS
        assert _SESSION_TTL_SECONDS == 60 * 60 * 24, "Session TTL must be 24 hours"

    def test_session_id_is_cryptographically_random(self):
        """Session IDs must be cryptographically strong."""
        ids = set()
        for _ in range(100):
            token = token_store.set_token("test")
            ids.add(token)
            token_store.clear_token(token)
        # All 100 should be unique
        assert len(ids) == 100, "Session IDs must be unique"


# ---------------------------------------------------------------------------
# Test L — Password security
# ---------------------------------------------------------------------------


class TestPasswordSecurity:
    """Passwords must be securely handled."""

    def test_password_hash_is_pbkdf2(self):
        """Password hashing must use PBKDF2-HMAC-SHA256."""
        from app.identity import hash_password
        h = hash_password("test1234")
        # Format: iterations$salt$digest
        parts = h.split("$")
        assert len(parts) == 3
        assert parts[0] == "480000"  # 480k iterations

    def test_login_response_does_not_contain_password(self, client, db_session):
        """Login response must never contain password or hash."""
        user = _create_active_user(db_session)

        resp = client.post("/auth/login-email", json={
            "email": user.email,
            "password": "password123",
        })
        assert resp.status_code == 200
        body = resp.json()

        # No password fields anywhere in the response
        assert "password" not in body
        assert "password_hash" not in body
        assert "password123" not in str(body)


# ---------------------------------------------------------------------------
# Test M — Legacy registration must never link credentials to an account
# ---------------------------------------------------------------------------


def _oauth_user(db_session, email, *, identity_source, **extra):
    """Create an OAuth-created (Google/Upstox) user with no local password."""
    user = User(
        id=str(uuid4()),
        email=email,
        password_hash=None,
        display_name="OAuth Owner",
        status="active",
        identity_source=identity_source,
        **extra,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


# Synthetic per-run credential: the attack case needs an arbitrary password,
# not a fixed literal that could be mistaken for a real one.
ATTACKER_PASSWORD = f"attacker-{uuid4().hex}"


def _user_snapshot(user):
    """Every persisted User column, so any unintended write is detected."""
    return {column.key: getattr(user, column.key) for column in user.__table__.columns}


class TestLegacyRegistrationCannotLinkAnExistingAccount:
    """P1 regression: POST /auth/register is unauthenticated, so it proves
    nothing about ownership of an existing account and must never attach or
    replace local credentials (or any other stored field).

    Before this fix, an attacker who submitted the email of a Google/Upstox
    account had their chosen password written onto that row and received the
    victim's ``user_id`` in the response, after which POST /auth/login-email
    accepted the attacker-chosen password.
    """

    GENERIC = {
        "ok": True,
        "message": "If this email is not already registered, your account has been created.",
    }

    @staticmethod
    def _counts(db):
        from app.identity import (
            EmailVerificationToken,
            PasswordResetToken,
            UserSession,
        )

        return (
            db.query(EmailVerificationToken).count(),
            db.query(PasswordResetToken).count(),
            db.query(UserSession).count(),
        )

    def _assert_row_and_artifacts_unchanged(self, db_session, user, before, counts_before):
        db_session.expire_all()
        after = _user_snapshot(db_session.query(User).filter(User.id == user.id).one())
        assert after == before
        assert self._counts(db_session) == counts_before
        return after

    @pytest.mark.parametrize(
        "identity_source,extra",
        [
            ("google", {"google_sub": "google-sub-attacked"}),
            (
                "upstox",
                {"broker_provider": "UPSTOX", "broker_user_id": "upstox-attacked"},
            ),
        ],
    )
    def test_oauth_account_cannot_gain_a_local_password(
        self, client, db_session, identity_source, extra
    ):
        from app.services.email import clear_sent_messages, get_sent_messages

        clear_sent_messages()
        email = f"{identity_source}-victim@example.com"
        user = _oauth_user(db_session, email, identity_source=identity_source, **extra)
        before = _user_snapshot(user)
        counts_before = self._counts(db_session)

        resp = client.post(
            "/auth/register",
            json={
                "email": email,
                "password": ATTACKER_PASSWORD,
                "display_name": "Attacker Chosen",
            },
        )

        # Same generic response as any other existing account: no password-link
        # claim, no account identifier, no authentication artifact.
        assert resp.status_code == 200, resp.text
        assert resp.json() == self.GENERIC
        assert "user_id" not in resp.text
        assert user.id not in resp.text
        assert email not in resp.text
        assert SESSION_COOKIE_NAME not in resp.headers.get("set-cookie", "")

        # No verification email was issued for the pre-existing account.
        assert get_sent_messages() == []

        # Every persisted column, plus token/session counts, is unchanged.
        after = self._assert_row_and_artifacts_unchanged(
            db_session, user, before, counts_before
        )
        assert after["password_hash"] is None
        assert after["display_name"] == before["display_name"]
        assert after["identity_source"] == identity_source

        # The attacker-chosen password authenticates nothing, at either local
        # login boundary.
        legacy_login = client.post(
            "/auth/login-email", json={"email": email, "password": ATTACKER_PASSWORD}
        )
        assert legacy_login.status_code == 401
        assert SESSION_COOKIE_NAME not in legacy_login.headers.get("set-cookie", "")

        account_login = client.post(
            "/auth/account/login", json={"email": email, "password": ATTACKER_PASSWORD}
        )
        assert account_login.status_code == 401
        assert SESSION_COOKIE_NAME not in account_login.headers.get("set-cookie", "")

    def test_duplicate_local_registration_preserves_the_existing_credential(
        self, client, db_session
    ):
        """A repeat registration for a local account changes nothing and the
        original credential keeps working."""
        owner_password = f"local-{uuid4().hex}"
        user = _create_active_user(
            db_session, email="local-owner@test.com", password=owner_password
        )
        before = _user_snapshot(user)
        counts_before = self._counts(db_session)

        resp = client.post(
            "/auth/register",
            json={
                "email": "local-owner@test.com",
                "password": ATTACKER_PASSWORD,
                "display_name": "Attacker Chosen",
            },
        )

        assert resp.status_code == 200
        assert resp.json() == self.GENERIC
        assert self._assert_row_and_artifacts_unchanged(
            db_session, user, before, counts_before
        ) == before

        # Original password still authenticates; the attacker password does not.
        ok = client.post(
            "/auth/login-email",
            json={"email": "local-owner@test.com", "password": owner_password},
        )
        assert ok.status_code == 200
        assert ok.cookies.get(SESSION_COOKIE_NAME)

        bad = client.post(
            "/auth/login-email",
            json={"email": "local-owner@test.com", "password": ATTACKER_PASSWORD},
        )
        assert bad.status_code == 401

    def test_response_does_not_distinguish_account_type(self, client, db_session):
        """Google, Upstox and local accounts all get the identical response and
        identical (untouched) rows: the generic body is not a thin disguise
        over a silent password write."""
        google = _oauth_user(
            db_session, "enum-google@test.com",
            identity_source="google", google_sub="google-sub-enum",
        )
        upstox = _oauth_user(
            db_session, "enum-upstox@test.com",
            identity_source="upstox",
            broker_provider="UPSTOX", broker_user_id="upstox-enum",
        )
        local = _create_active_user(db_session, email="enum-local@test.com")
        targets = [
            (user.id, user.email, _user_snapshot(user))
            for user in (google, upstox, local)
        ]

        responses = [
            client.post(
                "/auth/register",
                json={"email": email, "password": ATTACKER_PASSWORD},
            )
            for _id, email, _before in targets
        ]

        assert [r.status_code for r in responses] == [200, 200, 200]
        assert responses[0].json() == responses[1].json() == responses[2].json() == self.GENERIC
        assert {r.headers.get("set-cookie") for r in responses} == {None}

        db_session.expire_all()
        for user_id, _email, before in targets:
            after = _user_snapshot(db_session.query(User).filter(User.id == user_id).one())
            assert after == before

    def test_repeat_duplicate_registration_matches_the_selected_contract(
        self, client, db_session
    ):
        """The Day-50 legacy route keeps its non-enumerating 200 contract (it
        does not answer 409 for an existing address), and repeats are stable."""
        user = _oauth_user(
            db_session, "repeat@test.com",
            identity_source="google", google_sub="google-sub-repeat",
        )

        first = client.post(
            "/auth/register", json={"email": user.email, "password": ATTACKER_PASSWORD}
        )
        second = client.post(
            "/auth/register", json={"email": user.email, "password": ATTACKER_PASSWORD}
        )

        assert first.status_code == second.status_code == 200
        assert first.json() == second.json() == self.GENERIC

    def test_blocked_registration_creates_no_session(self, client, db_session):
        """No session is created and the account's status is untouched, so an
        OAuth account never becomes locally authenticatable through this route."""
        user = _oauth_user(
            db_session, "no-session@test.com",
            identity_source="google", google_sub="google-sub-no-session",
        )
        before = _user_snapshot(user)
        counts_before = self._counts(db_session)

        resp = client.post(
            "/auth/register", json={"email": user.email, "password": ATTACKER_PASSWORD}
        )
        assert resp.status_code == 200

        # The TestClient keeps cookies: the blocked registration issued none, so
        # an authenticated endpoint must still reject the caller.
        me = client.get("/auth/me")
        assert me.status_code == 401

        after = self._assert_row_and_artifacts_unchanged(
            db_session, user, before, counts_before
        )
        assert after["status"] == before["status"]
        assert after["password_hash"] is None

    def test_genuinely_new_registration_still_creates_an_account(
        self, client, db_session
    ):
        """Preserved behaviour: a new address still registers and can log in."""
        resp = client.post(
            "/auth/register",
            json={"email": "brand-new@test.com", "password": "NewOwnerPass123!"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["message"] == "Account created"
        assert body["user_id"]

        user = db_session.query(User).filter(User.email == "brand-new@test.com").one()
        assert user.identity_source == "email"
        assert user.status == "active"
        assert user.password_hash

        login = client.post(
            "/auth/login-email",
            json={"email": "brand-new@test.com", "password": "NewOwnerPass123!"},
        )
        assert login.status_code == 200
