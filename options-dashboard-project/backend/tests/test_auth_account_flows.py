"""StrikeNova account-security API flow tests.

Covers the /auth/account/* endpoints implemented task-by-task per
docs/superpowers/plans/2026-09-16-strikenova-auth-account-security-execution-plan.md:

- Task 1: account login / logout / logout-all / session + broker-OAuth boundary
- Task 3: registration, email verification, resend verification
- Task 4: forgot/reset password, change password/email, recent authentication
- Task 5: rate limiting, security events, secret-leak prevention

Architecture boundary (design spec §3/§9): these endpoints authenticate the
StrikeNova User and manage durable UserSessions. They NEVER invoke the broker
gateway. GET /auth/login remains the broker OAuth initiation route.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.identity import (
    User,
    UserSession,
    create_session_record,
    hash_password,
    hash_session_id,
    store_credentials,
)
from app.main import app
from app.config import settings
from app.routers.auth import SESSION_COOKIE_NAME
from app.services import token_store


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_auth_router.py — same in-memory DB pattern)
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
def _clear_auth_rate_limiter():
    """Clear rate limiter state before/after each test to prevent leakage."""
    from app.services.rate_limiter import rate_limiter

    rate_limiter._hits.clear()
    yield
    rate_limiter._hits.clear()


@pytest.fixture(autouse=True)
def _clear_email_sink():
    """The deterministic test sender keeps a process-global message list;
    clear it around every test so other files (e.g. the log-leak test in
    test_account_security.py) cannot leak messages into email assertions."""
    from app.services.email import clear_sent_messages

    clear_sent_messages()
    yield
    clear_sent_messages()


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


def _local_user(
    db,
    email="trader@example.com",
    password="Sup3rSecret!",
    status="active",
):
    """Create a local (email/password) StrikeNova user."""
    user = User(
        id=str(uuid4()),
        email=email,
        password_hash=hash_password(password),
        display_name=email.split("@")[0],
        status=status,
        identity_source="email",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


ACCOUNT = "/auth/account"


# ---------------------------------------------------------------------------
# Task 1 — POST /auth/account/login
# ---------------------------------------------------------------------------


class TestAccountLogin:
    def test_account_login_valid_credentials_creates_durable_session(
        self, client, db_session
    ):
        """POST /auth/account/login authenticates a StrikeNova User and
        creates exactly one durable, non-revoked UserSession."""
        user = _local_user(db_session)

        resp = client.post(
            f"{ACCOUNT}/login",
            json={"email": user.email, "password": "Sup3rSecret!"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["user"]["user_id"] == user.id
        assert body["user"]["email"] == user.email
        # Issue #61: the session is transported ONLY by the secure cookie —
        # the body never carries the session identifier.
        assert "session_id" not in body
        assert resp.cookies.get(SESSION_COOKIE_NAME), "login must set the canonical session cookie"

        sessions = (
            db_session.query(UserSession).filter(UserSession.user_id == user.id).all()
        )
        assert len(sessions) == 1
        assert sessions[0].revoked_at is None
        assert sessions[0].expires_at > sessions[0].created_at

    def test_account_login_sets_secure_session_cookie(self, client, db_session):
        """Login must set the HttpOnly Secure SameSite=None session cookie
        (existing secure cookie policy) — never a readable document.cookie."""
        user = _local_user(db_session)

        resp = client.post(
            f"{ACCOUNT}/login",
            json={"email": user.email, "password": "Sup3rSecret!"},
        )

        set_cookie = resp.headers.get("set-cookie", "")
        assert f"{SESSION_COOKIE_NAME}=" in set_cookie, f"Set-Cookie missing: {set_cookie}"
        assert "httponly" in set_cookie.lower()
        assert "secure" in set_cookie.lower()
        assert "samesite=none" in set_cookie.lower()

    def test_account_login_wrong_password_returns_generic_401(self, client, db_session):
        user = _local_user(db_session)
        resp = client.post(
            f"{ACCOUNT}/login", json={"email": user.email, "password": "wrong-password"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid email or password"

    def test_account_login_unknown_email_returns_identical_generic_401(
        self, client, db_session
    ):
        """Unknown email must fail with exactly the same public response as a
        wrong password (no account-existence oracle)."""
        resp_unknown = client.post(
            f"{ACCOUNT}/login",
            json={"email": "nobody@example.com", "password": "whatever123"},
        )
        assert resp_unknown.status_code == 401
        assert resp_unknown.json()["detail"] == "Invalid email or password"

    def test_account_login_normalizes_email(self, client, db_session):
        """Email matching is case/whitespace-insensitive at login."""
        user = _local_user(db_session, email="trader@example.com")
        resp = client.post(
            f"{ACCOUNT}/login",
            json={"email": "  TRADER@Example.COM  ", "password": "Sup3rSecret!"},
        )
        assert resp.status_code == 200

    def test_account_login_never_invokes_broker_gateway(self, client, db_session):
        """Account authentication is independent of broker authorization:
        the broker gateway must not be touched (design spec §3 boundary)."""
        _local_user(db_session)

        with patch(
            "app.brokers.gateway.gateway.create",
            side_effect=AssertionError("broker gateway must not be invoked by account login"),
        ) as gateway_create:
            resp = client.post(
                f"{ACCOUNT}/login",
                json={"email": "trader@example.com", "password": "Sup3rSecret!"},
            )

        assert resp.status_code == 200
        gateway_create.assert_not_called()

    def test_account_login_requires_no_broker_credentials(self, client, db_session):
        """Broker connectivity is NOT a prerequisite for account login: a user
        with no BrokerConnection / broker credentials can authenticate."""
        _local_user(db_session)  # no store_credentials() call
        resp = client.post(
            f"{ACCOUNT}/login",
            json={"email": "trader@example.com", "password": "Sup3rSecret!"},
        )
        assert resp.status_code == 200

    def test_account_login_missing_fields_422(self, client, db_session):
        resp = client.post(f"{ACCOUNT}/login", json={"email": "", "password": ""})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Task 1 — GET /auth/account/session
# ---------------------------------------------------------------------------


class TestAccountSession:
    def _login(self, client):
        """Login and return the session ID from the canonical secure cookie
        (Issue #61: the response body never carries the session)."""
        resp = client.post(
            f"{ACCOUNT}/login",
            json={"email": "trader@example.com", "password": "Sup3rSecret!"},
        )
        assert resp.status_code == 200
        return resp.cookies.get(SESSION_COOKIE_NAME)

    def test_session_endpoint_returns_authenticated_user(self, client, db_session):
        user = _local_user(db_session)
        sid = self._login(client)

        resp = client.get(
            f"{ACCOUNT}/session", headers={"X-Session-Id": sid}
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["authenticated"] is True
        assert data["user"]["user_id"] == user.id
        assert data["session"]["expires_at"] > data["session"]["created_at"]

    def test_session_rejects_invalid_session(self, client, db_session):
        _local_user(db_session)
        resp = client.get(f"{ACCOUNT}/session", headers={"X-Session-Id": "not-a-session"})
        assert resp.status_code == 401

    def test_session_rejects_revoked_session(self, client, db_session):
        """UserSession.revoked_at is the authority — a revoked session is
        rejected even if the client still presents its identifier."""
        _local_user(db_session)
        sid = self._login(client)

        logout = client.post(f"{ACCOUNT}/logout", headers={"X-Session-Id": sid})
        assert logout.status_code == 200

        resp = client.get(f"{ACCOUNT}/session", headers={"X-Session-Id": sid})
        assert resp.status_code == 401

    def test_session_rejects_expired_session(self, client, db_session):
        """UserSession.expires_at is the authority — expired sessions fail."""
        _local_user(db_session)
        sid = self._login(client)

        db_session.query(UserSession).filter(
            UserSession.session_hash == hash_session_id(sid)
        ).update({"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
        db_session.commit()

        resp = client.get(f"{ACCOUNT}/session", headers={"X-Session-Id": sid})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Task 1 — POST /auth/account/logout and /auth/account/logout-all
# ---------------------------------------------------------------------------


class TestAccountLogout:
    def test_logout_revokes_current_session_only(self, client, db_session):
        user = _local_user(db_session)
        r1 = client.post(
            f"{ACCOUNT}/login", json={"email": user.email, "password": "Sup3rSecret!"}
        ).cookies.get(SESSION_COOKIE_NAME)
        r2 = client.post(
            f"{ACCOUNT}/login", json={"email": user.email, "password": "Sup3rSecret!"}
        ).cookies.get(SESSION_COOKIE_NAME)

        resp = client.post(
            f"{ACCOUNT}/logout", headers={"X-Session-Id": r1}
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Logged-out session is rejected; the other session still works.
        assert (
            client.get(
                f"{ACCOUNT}/session", headers={"X-Session-Id": r1}
            ).status_code
            == 401
        )
        assert (
            client.get(
                f"{ACCOUNT}/session", headers={"X-Session-Id": r2}
            ).status_code
            == 200
        )

    def test_logout_is_idempotent(self, client, db_session):
        """Logout of an unknown/already-revoked session still returns ok."""
        resp = client.post(f"{ACCOUNT}/logout", headers={"X-Session-Id": "never-existed"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_logout_clears_session_cookie(self, client, db_session):
        _local_user(db_session)
        resp = client.post(f"{ACCOUNT}/logout")
        set_cookie = resp.headers.get("set-cookie", "")
        assert f"{SESSION_COOKIE_NAME}=" in set_cookie
        assert "max-age=0" in set_cookie.lower() or "expires=" in set_cookie.lower()

    def test_logout_all_revokes_every_session(self, client, db_session):
        user = _local_user(db_session)
        sessions = [
            client.post(
                f"{ACCOUNT}/login",
                json={"email": user.email, "password": "Sup3rSecret!"},
            ).cookies.get(SESSION_COOKIE_NAME)
            for _ in range(3)
        ]

        resp = client.post(
            f"{ACCOUNT}/logout-all", headers={"X-Session-Id": sessions[0]}
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        for sid in sessions:
            assert (
                client.get(f"{ACCOUNT}/session", headers={"X-Session-Id": sid}).status_code
                == 401
            )

    def test_logout_all_requires_authentication(self, client, db_session):
        resp = client.post(f"{ACCOUNT}/logout-all")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Task 1 — Broker OAuth regression: GET /auth/login stays the broker route
# ---------------------------------------------------------------------------


class TestBrokerOAuthBoundary:
    def test_broker_oauth_route_unchanged(self, client, db_session):
        """GET /auth/login?broker=UPSTOX must remain the broker OAuth
        initiation route: authenticated session + BYOB credentials → 307
        redirect carrying the user's own client_id."""
        user = _local_user(db_session)
        session_id = token_store.set_token("tok-broker-route")
        create_session_record(db_session, user.id, session_id)
        store_credentials(db_session, user.id, "UPSTOX", "user-api-key", "user-api-secret")

        resp = client.get(
            "/auth/login?broker=UPSTOX",
            headers={"X-Session-Id": session_id},
            follow_redirects=False,
        )

        assert resp.status_code == 307
        location = resp.headers["location"]
        assert "client_id=user-api-key" in location

    def test_broker_oauth_requires_authentication(self, client, db_session):
        """Day-3 security fix is preserved: anonymous /auth/login → 401."""
        resp = client.get("/auth/login?broker=UPSTOX", follow_redirects=False)
        assert resp.status_code == 401

    def test_account_login_is_not_the_broker_route(self, client, db_session):
        """POST /auth/account/login must exist separately from broker OAuth —
        it authenticates without any broker query parameter or redirect."""
        _local_user(db_session)
        resp = client.post(
            f"{ACCOUNT}/login",
            json={"email": "trader@example.com", "password": "Sup3rSecret!"},
        )
        assert resp.status_code == 200
        assert "location" not in resp.headers
        assert resp.cookies.get(SESSION_COOKIE_NAME)


# ---------------------------------------------------------------------------
# Task 3 — Registration, email verification, resend verification
# ---------------------------------------------------------------------------


class TestAccountRegistration:
    def test_register_creates_unverified_account_and_sends_verification_email(
        self, client, db_session
    ):
        """Valid registration creates an account in an unverified state,
        stores a hashed verification token, and delivers the verification
        email through the configured EmailSender — with no session issued."""
        resp = client.post(
            f"{ACCOUNT}/register",
            json={
                "email": "New.Trader@Example.COM",
                "password": "Sup3rSecret!",
                "display_name": "New Trader",
            },
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("ok") is True
        # No auto-login: no session id, no session cookie, no token material
        assert "session_id" not in body
        assert "token" not in body
        assert "verification" not in body or "token" not in str(body.get("verification", ""))
        assert "Set-Cookie" not in resp.headers or SESSION_COOKIE_NAME not in resp.headers.get(
            "Set-Cookie", ""
        )

        # Account exists, normalized, unverified
        user = (
            db_session.query(User).filter(User.email == "new.trader@example.com").one_or_none()
        )
        assert user is not None
        assert user.identity_source == "email"
        assert user.status == "pending_verification"
        assert user.password_hash and user.password_hash != "Sup3rSecret!"

        # Exactly one verification token, stored hashed only
        from app.identity import EmailVerificationToken

        tokens = (
            db_session.query(EmailVerificationToken)
            .filter(EmailVerificationToken.user_id == user.id)
            .all()
        )
        assert len(tokens) == 1
        assert tokens[0].used_at is None
        assert "Sup3rSecret" not in tokens[0].token_hash

        # Verification email was delivered
        from app.services.email import get_sent_messages

        sent = get_sent_messages()
        assert len(sent) == 1
        assert sent[0].to == "new.trader@example.com"
        assert "verify" in sent[0].subject.lower()
        # The link carries the token, but the stored record never does
        assert sent[0].raw_token
        assert sent[0].raw_token not in tokens[0].token_hash

    def test_register_normalizes_email(self, client, db_session):
        """Email is stored trimmed and lower-cased."""
        resp = client.post(
            f"{ACCOUNT}/register",
            json={"email": "  MiXeD@ExAmPlE.CoM  ", "password": "Sup3rSecret!"},
        )
        assert resp.status_code == 200
        user = db_session.query(User).filter(User.email == "mixed@example.com").one_or_none()
        assert user is not None

    def test_register_rejects_weak_password(self, client, db_session):
        resp = client.post(
            f"{ACCOUNT}/register",
            json={"email": "weak@example.com", "password": "short"},
        )
        assert resp.status_code == 422

    def test_register_rejects_invalid_email(self, client, db_session):
        resp = client.post(
            f"{ACCOUNT}/register",
            json={"email": "not-an-email", "password": "Sup3rSecret!"},
        )
        assert resp.status_code == 422

    def test_register_duplicate_local_account_is_enumeration_resistant(
        self, client, db_session
    ):
        """A duplicate registration must NOT reveal the account exists and
        must NOT create a second account or a second token."""
        _local_user(db_session, email="taken@example.com")
        from app.identity import EmailVerificationToken

        before = db_session.query(EmailVerificationToken).count()

        resp = client.post(
            f"{ACCOUNT}/register",
            json={"email": "taken@example.com", "password": "Sup3rSecret!"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True

        users = db_session.query(User).filter(User.email == "taken@example.com").count()
        assert users == 1
        after = db_session.query(EmailVerificationToken).count()
        assert after == before  # no new token for the duplicate attempt


class TestEmailVerification:
    def _register(self, client, email="verify@example.com"):
        from app.services.email import clear_sent_messages, get_sent_messages

        clear_sent_messages()
        resp = client.post(
            f"{ACCOUNT}/register",
            json={"email": email, "password": "Sup3rSecret!"},
        )
        assert resp.status_code == 200
        return get_sent_messages()[-1].raw_token

    def test_verify_email_with_valid_token_marks_account_verified(
        self, client, db_session
    ):
        raw_token = self._register(client)
        user = db_session.query(User).filter(User.email == "verify@example.com").one()

        resp = client.post(f"{ACCOUNT}/verify-email", json={"token": raw_token})
        assert resp.status_code == 200, resp.text
        assert resp.json().get("ok") is True
        db_session.expire_all()
        assert user.status == "active"

    def test_verify_email_does_not_create_a_session(self, client, db_session):
        raw_token = self._register(client)
        resp = client.post(f"{ACCOUNT}/verify-email", json={"token": raw_token})
        assert resp.status_code == 200
        assert "Set-Cookie" not in resp.headers
        assert not resp.json().get("session_id")

    def test_verify_email_invalid_token_fails_closed(self, client, db_session):
        resp = client.post(f"{ACCOUNT}/verify-email", json={"token": "junk-token"})
        assert resp.status_code == 400

    def test_verify_email_replayed_token_fails(self, client, db_session):
        raw_token = self._register(client)
        first = client.post(f"{ACCOUNT}/verify-email", json={"token": raw_token})
        assert first.status_code == 200
        second = client.post(f"{ACCOUNT}/verify-email", json={"token": raw_token})
        assert second.status_code == 400

    def test_verify_email_expired_token_fails(self, client, db_session, monkeypatch):
        raw_token = self._register(client)
        user = db_session.query(User).filter(User.email == "verify@example.com").one()
        from app.identity import EmailVerificationToken

        record = (
            db_session.query(EmailVerificationToken)
            .filter(EmailVerificationToken.user_id == user.id)
            .one()
        )
        record.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db_session.commit()

        resp = client.post(f"{ACCOUNT}/verify-email", json={"token": raw_token})
        assert resp.status_code == 400
        db_session.expire_all()
        assert user.status == "pending_verification"

    def test_resend_verification_invalidates_prior_token(
        self, client, db_session
    ):
        """Resend must invalidate the previous active token; the old token
        then fails and only the newest one verifies."""
        first_token = self._register(client)
        user = db_session.query(User).filter(User.email == "verify@example.com").one()

        resend = client.post(
            f"{ACCOUNT}/resend-verification", json={"email": "verify@example.com"}
        )
        assert resend.status_code == 200, resend.text
        assert resend.json().get("ok") is True

        from app.services.email import get_sent_messages

        assert len(get_sent_messages()) == 2
        second_token = get_sent_messages()[-1].raw_token
        assert second_token != first_token

        old = client.post(f"{ACCOUNT}/verify-email", json={"token": first_token})
        assert old.status_code == 400
        new = client.post(f"{ACCOUNT}/verify-email", json={"token": second_token})
        assert new.status_code == 200
        db_session.expire_all()
        assert user.status == "active"

    def test_resend_for_unknown_email_is_generic(self, client, db_session):
        """Enumeration resistance: unknown email gets the same public
        response as a known one."""
        known = client.post(
            f"{ACCOUNT}/resend-verification", json={"email": "verify@example.com"}
        )
        unknown = client.post(
            f"{ACCOUNT}/resend-verification", json={"email": "ghost@example.com"}
        )
        assert known.status_code == unknown.status_code == 200
        assert known.json() == unknown.json()

    def test_login_before_verification_is_rejected(self, client, db_session):
        """Unverified local accounts cannot log in until verified."""
        self._register(client, email="pending@example.com")
        resp = client.post(
            f"{ACCOUNT}/login",
            json={"email": "pending@example.com", "password": "Sup3rSecret!"},
        )
        assert resp.status_code == 403
        db_session.expire_all()
        user = (
            db_session.query(User).filter(User.email == "pending@example.com").one()
        )
        assert user.status == "pending_verification"

    def test_login_after_verification_succeeds(self, client, db_session):
        raw_token = self._register(client, email="verified-login@example.com")
        client.post(f"{ACCOUNT}/verify-email", json={"token": raw_token})

        resp = client.post(
            f"{ACCOUNT}/login",
            json={
                "email": "verified-login@example.com",
                "password": "Sup3rSecret!",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.cookies.get(SESSION_COOKIE_NAME)


# ---------------------------------------------------------------------------
# Task 4 — Password recovery, sensitive changes, recent authentication
# ---------------------------------------------------------------------------


def _verified_user(db, email="recovery@example.com", password="Sup3rSecret!"):
    """A verified local user ready to log in."""
    user = _local_user(db, email=email, password=password)
    return user


def auth(session_id):
    """Repo-standard session transport: X-Session-Id header (cookies are
    Secure in this app, so TestClient over http never replays them)."""
    return {"X-Session-Id": session_id} if session_id else {}


def _login(client, email="recovery@example.com", password="Sup3rSecret!"):
    """Login and return the session ID from the canonical secure cookie
    (Issue #61: the response body never carries the session)."""
    resp = client.post(f"{ACCOUNT}/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.cookies.get(SESSION_COOKIE_NAME)


class TestForgotPassword:
    def test_known_and_unknown_email_get_identical_responses(
        self, client, db_session
    ):
        """Enumeration protection: identical status + body for known and
        unknown addresses."""
        _verified_user(db_session)
        known = client.post(
            f"{ACCOUNT}/forgot-password", json={"email": "recovery@example.com"}
        )
        unknown = client.post(
            f"{ACCOUNT}/forgot-password", json={"email": "ghost@example.com"}
        )
        assert known.status_code == unknown.status_code == 200
        assert known.json() == unknown.json()
        # No token material, no URL in the public response
        assert "token" not in known.text.lower()

    def test_forgot_password_sends_reset_email_for_eligible_account(
        self, client, db_session
    ):
        from app.services.email import clear_sent_messages, get_sent_messages

        clear_sent_messages()
        _verified_user(db_session)
        client.post(f"{ACCOUNT}/forgot-password", json={"email": "recovery@example.com"})

        sent = get_sent_messages()
        assert len(sent) == 1
        assert sent[0].to == "recovery@example.com"
        assert "reset" in sent[0].subject.lower()
        # The reset link carries the raw token — but nothing is persisted
        assert sent[0].raw_token

    def test_new_request_invalidates_previous_reset_token(self, client, db_session):
        from app.services.email import clear_sent_messages, get_sent_messages
        from app.identity import PasswordResetToken

        _verified_user(db_session)
        clear_sent_messages()
        client.post(f"{ACCOUNT}/forgot-password", json={"email": "recovery@example.com"})
        first = get_sent_messages()[-1].raw_token

        client.post(f"{ACCOUNT}/forgot-password", json={"email": "recovery@example.com"})
        second = get_sent_messages()[-1].raw_token
        assert second != first

        user = db_session.query(User).filter(User.email == "recovery@example.com").one()
        active = (
            db_session.query(PasswordResetToken)
            .filter(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
            .count()
        )
        assert active == 1


class TestResetPassword:
    def _request_reset(self, client, email="recovery@example.com"):
        from app.services.email import clear_sent_messages, get_sent_messages

        clear_sent_messages()
        client.post(f"{ACCOUNT}/forgot-password", json={"email": email})
        return get_sent_messages()[-1].raw_token

    def test_valid_reset_updates_password_and_revokes_sessions(
        self, client, db_session
    ):
        user = _verified_user(db_session)
        # Two active sessions
        _login(client)
        _login(client)
        raw_token = self._request_reset(client)

        resp = client.post(
            f"{ACCOUNT}/reset-password",
            json={"token": raw_token, "new_password": "N3wPassword!"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("ok") is True
        # No new session is created by reset
        assert "session_id" not in body
        assert "Set-Cookie" not in resp.headers

        db_session.expire_all()
        updated = (
            db_session.query(User).filter(User.id == user.id).one()
        )
        from app.identity import verify_password

        assert verify_password("N3wPassword!", updated.password_hash)
        assert not verify_password("Sup3rSecret!", updated.password_hash)

        # All existing sessions revoked
        from app.identity import UserSession as US

        now = datetime.now(timezone.utc)
        active = (
            db_session.query(US)
            .filter(US.user_id == user.id, US.revoked_at.is_(None), US.expires_at > now)
            .count()
        )
        assert active == 0

    def test_user_must_log_in_normally_after_reset(self, client, db_session):
        _verified_user(db_session)
        raw_token = self._request_reset(client)
        client.post(
            f"{ACCOUNT}/reset-password",
            json={"token": raw_token, "new_password": "N3wPassword!"},
        )
        resp = client.post(
            f"{ACCOUNT}/login",
            json={"email": "recovery@example.com", "password": "N3wPassword!"},
        )
        assert resp.status_code == 200
        assert resp.cookies.get(SESSION_COOKIE_NAME)

    def test_invalid_reset_token_fails(self, client, db_session):
        _verified_user(db_session)
        resp = client.post(
            f"{ACCOUNT}/reset-password",
            json={"token": "junk", "new_password": "N3wPassword!"},
        )
        assert resp.status_code == 400

    def test_expired_reset_token_fails(self, client, db_session):
        _verified_user(db_session)
        raw_token = self._request_reset(client)
        from app.identity import PasswordResetToken

        user = db_session.query(User).filter(User.email == "recovery@example.com").one()
        record = (
            db_session.query(PasswordResetToken)
            .filter(PasswordResetToken.user_id == user.id)
            .one()
        )
        record.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db_session.commit()

        resp = client.post(
            f"{ACCOUNT}/reset-password",
            json={"token": raw_token, "new_password": "N3wPassword!"},
        )
        assert resp.status_code == 400

    def test_replayed_reset_token_fails(self, client, db_session):
        _verified_user(db_session)
        raw_token = self._request_reset(client)
        first = client.post(
            f"{ACCOUNT}/reset-password",
            json={"token": raw_token, "new_password": "N3wPassword!"},
        )
        assert first.status_code == 200
        second = client.post(
            f"{ACCOUNT}/reset-password",
            json={"token": raw_token, "new_password": "AnotherPass1!"},
        )
        assert second.status_code == 400

    def test_reset_sends_security_notification(self, client, db_session):
        from app.services.email import clear_sent_messages, get_sent_messages

        _verified_user(db_session)
        raw_token = self._request_reset(client)
        clear_sent_messages()
        client.post(
            f"{ACCOUNT}/reset-password",
            json={"token": raw_token, "new_password": "N3wPassword!"},
        )
        sent = get_sent_messages()
        assert len(sent) == 1
        assert sent[0].to == "recovery@example.com"
        # Notification is secret-free
        assert sent[0].raw_token is None


class TestRecentAuthentication:
    def test_login_records_recent_auth_server_side(self, client, db_session):
        from app.services.account_security import is_recently_authenticated

        user = _verified_user(db_session)
        _login(client)
        session_id = client.cookies.get(SESSION_COOKIE_NAME)
        assert session_id
        assert is_recently_authenticated(db_session, user.id, session_id) is True

    def test_no_recent_auth_without_login(self, client, db_session):
        from app.services.account_security import is_recently_authenticated

        user = _verified_user(db_session)
        assert is_recently_authenticated(db_session, user.id, "unknown-session") is False

    def test_change_password_requires_recent_auth(self, client, db_session, monkeypatch):
        user = _verified_user(db_session)
        session_id = _login(client)
        # Deterministic staleness: shrink the server-side freshness window to
        # zero so the login's recent-auth record is outside it (no sleeps).
        monkeypatch.setattr(settings, "RECENT_AUTH_TTL_MINUTES", 0)
        resp = client.post(
            f"{ACCOUNT}/change-password",
            headers=auth(session_id),
            json={"current_password": "Sup3rSecret!", "new_password": "N3wPassword!"},
        )
        assert resp.status_code == 403
        db_session.expire_all()
        from app.identity import verify_password

        refreshed = db_session.query(User).filter(User.id == user.id).one()
        assert verify_password("Sup3rSecret!", refreshed.password_hash)

    def test_change_password_validates_current_password(self, client, db_session):
        _verified_user(db_session)
        session_id = _login(client)
        resp = client.post(
            f"{ACCOUNT}/change-password",
            headers=auth(session_id),
            json={"current_password": "WrongPassword1!", "new_password": "N3wPassword!"},
        )
        assert resp.status_code == 401

    def test_change_password_succeeds_with_recent_auth_and_revokes_others(
        self, client, db_session
    ):
        user = _verified_user(db_session)
        # Two sessions: the first (older) and the current one. The login just
        # performed records a fresh server-side recent-auth event.
        session_id = _login(client)
        current_session = session_id

        resp = client.post(
            f"{ACCOUNT}/change-password",
            headers=auth(session_id),
            json={"current_password": "Sup3rSecret!", "new_password": "N3wPassword!"},
        )
        assert resp.status_code == 200, resp.text
        db_session.expire_all()
        from app.identity import verify_password

        refreshed = db_session.query(User).filter(User.id == user.id).one()
        assert verify_password("N3wPassword!", refreshed.password_hash)
        # Other sessions revoked; the current one survives (session policy)
        now = datetime.now(timezone.utc)
        others = (
            db_session.query(UserSession)
            .filter(
                UserSession.user_id == user.id,
                UserSession.session_hash != hash_session_id(current_session),
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
            )
            .count()
        )
        assert others == 0

    def test_change_email_requires_recent_auth(self, client, db_session, monkeypatch):
        user = _verified_user(db_session)
        session_id = _login(client)
        monkeypatch.setattr(settings, "RECENT_AUTH_TTL_MINUTES", 0)
        resp = client.post(
            f"{ACCOUNT}/change-email",
            headers=auth(session_id),
            json={"new_email": "newaddress@example.com"},
        )
        assert resp.status_code == 403

    def test_change_email_stores_pending_change_old_email_stays_authoritative(
        self, client, db_session
    ):
        from app.identity import PendingEmailChange
        from app.services.email import clear_sent_messages, get_sent_messages

        user = _verified_user(db_session)
        session_id = _login(client)
        clear_sent_messages()
        resp = client.post(
            f"{ACCOUNT}/change-email",
            headers=auth(session_id),
            json={"new_email": "newaddress@example.com"},
        )
        assert resp.status_code == 200, resp.text

        # Old email unchanged; pending record exists, hashed token stored
        db_session.expire_all()
        refreshed = db_session.query(User).filter(User.id == user.id).one()
        assert refreshed.email == "recovery@example.com"
        pending = (
            db_session.query(PendingEmailChange)
            .filter(PendingEmailChange.user_id == user.id)
            .one()
        )
        assert pending.new_email == "newaddress@example.com"
        assert pending.used_at is None
        # Confirmation email delivered to the NEW address
        sent = get_sent_messages()
        assert len(sent) == 1
        assert sent[0].to == "newaddress@example.com"
        assert sent[0].raw_token

    def test_change_email_verification_completes_change(self, client, db_session):
        from app.services.email import clear_sent_messages, get_sent_messages

        user = _verified_user(db_session)
        session_id = _login(client)
        clear_sent_messages()
        client.post(f"{ACCOUNT}/change-email", headers=auth(session_id), json={"new_email": "newaddress@example.com"})
        raw_token = get_sent_messages()[-1].raw_token

        resp = client.post(
            f"{ACCOUNT}/verify-email-change", json={"token": raw_token}
        )
        assert resp.status_code == 200, resp.text
        db_session.expire_all()
        refreshed = db_session.query(User).filter(User.id == user.id).one()
        assert refreshed.email == "newaddress@example.com"

    def test_change_email_verification_rejects_replay(self, client, db_session):
        from app.services.email import clear_sent_messages, get_sent_messages

        user = _verified_user(db_session)
        session_id = _login(client)
        clear_sent_messages()
        client.post(f"{ACCOUNT}/change-email", headers=auth(session_id), json={"new_email": "newaddress@example.com"})
        raw_token = get_sent_messages()[-1].raw_token

        first = client.post(f"{ACCOUNT}/verify-email-change", json={"token": raw_token})
        assert first.status_code == 200
        second = client.post(f"{ACCOUNT}/verify-email-change", json={"token": raw_token})
        assert second.status_code == 400
        db_session.expire_all()
        refreshed = db_session.query(User).filter(User.id == user.id).one()
        assert refreshed.email == "newaddress@example.com"  # changed once

    def test_change_email_requires_authenticated_session(self, client, db_session):
        _verified_user(db_session)
        resp = client.post(
            f"{ACCOUNT}/change-email",
            json={"new_email": "newaddress@example.com"},
        )
        assert resp.status_code == 401



# ---------------------------------------------------------------------------
# Task 5 — Abuse controls (rate limiting) + security audit events
# ---------------------------------------------------------------------------


class TestAccountAbuseControls:
    """Bounded rate limits on every account-security endpoint (plan Task 5).

    Deterministic time: the limiter reads time.time() from the
    app.services.rate_limiter module namespace, so tests advance a fake clock
    instead of sleeping.
    """

    def _fake_clock(self, monkeypatch):
        import app.services.rate_limiter as rl_mod

        state = {"now": 1_000_000.0}
        monkeypatch.setattr(rl_mod.time, "time", lambda: state["now"])
        return state

    def _post(self, client, path, n, json_body, headers=None):
        last = None
        for _ in range(n):
            last = client.post(path, json=json_body, headers=headers or {})
        return last

    def test_account_login_rate_limited(self, client, db_session, monkeypatch):
        self._fake_clock(monkeypatch)
        _verified_user(db_session)
        body = {"email": "recovery@example.com", "password": "Sup3rSecret!"}
        for _ in range(10):
            assert client.post(f"{ACCOUNT}/login", json=body).status_code == 200
        assert client.post(f"{ACCOUNT}/login", json=body).status_code == 429

    def test_account_login_rate_limit_resets_after_window(
        self, client, db_session, monkeypatch
    ):
        state = self._fake_clock(monkeypatch)
        _verified_user(db_session)
        body = {"email": "recovery@example.com", "password": "Sup3rSecret!"}
        for _ in range(10):
            client.post(f"{ACCOUNT}/login", json=body)
        assert client.post(f"{ACCOUNT}/login", json=body).status_code == 429
        state["now"] += 61  # window is 60s — deterministic, no sleeps
        assert client.post(f"{ACCOUNT}/login", json=body).status_code == 200

    def test_account_register_rate_limited(self, client, db_session, monkeypatch):
        self._fake_clock(monkeypatch)
        for i in range(5):
            resp = client.post(
                f"{ACCOUNT}/register",
                json={"email": f"new{i}@example.com", "password": "Val1dPass!"},
            )
            assert resp.status_code == 200, resp.text
        resp = client.post(
            f"{ACCOUNT}/register",
            json={"email": "overflow@example.com", "password": "Val1dPass!"},
        )
        assert resp.status_code == 429

    def test_resend_verification_rate_limited(self, client, db_session, monkeypatch):
        self._fake_clock(monkeypatch)
        _local_user(db_session, email="pending@example.com", status="pending_verification")
        for _ in range(3):
            assert client.post(
                f"{ACCOUNT}/resend-verification", json={"email": "pending@example.com"}
            ).status_code == 200
        assert client.post(
            f"{ACCOUNT}/resend-verification", json={"email": "pending@example.com"}
        ).status_code == 429

    def test_forgot_password_rate_limited(self, client, db_session, monkeypatch):
        self._fake_clock(monkeypatch)
        _verified_user(db_session)
        for _ in range(5):
            assert client.post(
                f"{ACCOUNT}/forgot-password", json={"email": "recovery@example.com"}
            ).status_code == 200
        assert client.post(
            f"{ACCOUNT}/forgot-password", json={"email": "recovery@example.com"}
        ).status_code == 429

    def test_reset_password_rate_limited(self, client, db_session, monkeypatch):
        self._fake_clock(monkeypatch)
        _verified_user(db_session)
        for _ in range(5):
            assert client.post(
                f"{ACCOUNT}/reset-password",
                json={"token": "junk", "new_password": "N3wPassword!"},
            ).status_code == 400
        assert client.post(
            f"{ACCOUNT}/reset-password",
            json={"token": "junk", "new_password": "N3wPassword!"},
        ).status_code == 429

    def test_change_password_rate_limited(self, client, db_session, monkeypatch):
        self._fake_clock(monkeypatch)
        _verified_user(db_session)
        session_id = _login(client)
        headers = auth(session_id)
        for _ in range(5):
            resp = client.post(
                f"{ACCOUNT}/change-password",
                headers=headers,
                json={"current_password": "wrong", "new_password": "N3wPassword!"},
            )
            assert resp.status_code == 401
        assert client.post(
            f"{ACCOUNT}/change-password",
            headers=headers,
            json={"current_password": "wrong", "new_password": "N3wPassword!"},
        ).status_code == 429

    def test_change_email_rate_limited(self, client, db_session, monkeypatch):
        self._fake_clock(monkeypatch)
        _verified_user(db_session)
        session_id = _login(client)
        headers = auth(session_id)
        for i in range(5):
            assert client.post(
                f"{ACCOUNT}/change-email",
                headers=headers,
                json={"new_email": f"move{i}@example.com"},
            ).status_code == 200
        assert client.post(
            f"{ACCOUNT}/change-email",
            headers=headers,
            json={"new_email": "overflow@example.com"},
        ).status_code == 429


class TestSecurityEventEmission:
    """Durable, secret-free SecurityEvent rows for the remaining flows."""

    def _events(self, db_session, event_type):
        from app.identity import SecurityEvent

        return (
            db_session.query(SecurityEvent)
            .filter(SecurityEvent.event_type == event_type)
            .all()
        )

    def test_login_success_and_failure_emit_events(self, client, db_session):
        _verified_user(db_session)
        # Failure first
        client.post(
            f"{ACCOUNT}/login",
            json={"email": "recovery@example.com", "password": "WrongPass1!"},
        )
        # Unknown email failure
        client.post(
            f"{ACCOUNT}/login",
            json={"email": "ghost@example.com", "password": "Whatever1!"},
        )
        # Success
        client.post(
            f"{ACCOUNT}/login",
            json={"email": "recovery@example.com", "password": "Sup3rSecret!"},
        )

        failures = self._events(db_session, "login_failed")
        successes = self._events(db_session, "login_succeeded")
        assert len(failures) == 2
        assert len(successes) == 1
        # No attempted password material in any event
        for ev in failures + successes:
            blob = str(ev.metadata_json)
            assert "WrongPass1!" not in blob and "Whatever1!" not in blob
            assert "Sup3rSecret!" not in blob

    def test_registration_emits_event(self, client, db_session):
        from app.services.email import clear_sent_messages

        clear_sent_messages()
        client.post(
            f"{ACCOUNT}/register",
            json={"email": "fresh@example.com", "password": "Val1dPass!"},
        )
        events = self._events(db_session, "registration_completed")
        assert len(events) == 1
        assert "Val1dPass!" not in str(events[0].metadata_json)

    def test_logout_and_logout_all_emit_events(self, client, db_session):
        _verified_user(db_session)
        sid1 = _login(client)
        sid2 = _login(client)

        client.post(f"{ACCOUNT}/logout", headers=auth(sid1))
        logouts = self._events(db_session, "logout")
        assert len(logouts) == 1

        client.post(f"{ACCOUNT}/logout-all", headers=auth(sid2))
        logouts_all = self._events(db_session, "logout_all")
        assert len(logouts_all) == 1
        # The all-revocation also records per-session revocation events
        revoked = self._events(db_session, "session_revoked")
        assert len(revoked) >= 1

    def test_change_password_revocation_emits_session_revoked_events(
        self, client, db_session
    ):
        _verified_user(db_session)
        _login(client)  # older session — will be revoked
        session_id = _login(client)  # current session — survives
        client.post(
            f"{ACCOUNT}/change-password",
            headers=auth(session_id),
            json={"current_password": "Sup3rSecret!", "new_password": "N3wPassword!"},
        )
        revoked = self._events(db_session, "session_revoked")
        assert len(revoked) == 1
        # Revoked-session audit rows carry the session HASH, not the raw id
        assert revoked[0].session_id != session_id

    def test_all_security_events_secret_free_after_full_journey(
        self, client, db_session
    ):
        from app.services.email import clear_sent_messages, get_sent_messages
        from app.identity import SecurityEvent

        _verified_user(db_session)
        session_id = _login(client)
        clear_sent_messages()
        client.post(
            f"{ACCOUNT}/change-email",
            headers=auth(session_id),
            json={"new_email": "moved@example.com"},
        )
        raw_change_token = get_sent_messages()[-1].raw_token
        client.post(f"{ACCOUNT}/forgot-password", json={"email": "recovery@example.com"})
        raw_reset = get_sent_messages()[-1].raw_token

        secrets = [raw_change_token, raw_reset, "Sup3rSecret!", "N3wPassword!"]
        for ev in db_session.query(SecurityEvent).all():
            blob = (
                str(ev.metadata_json)
                + str(ev.event_type)
                + str(ev.ip_hash or "")
                + str(ev.user_agent_hash or "")
            )
            for secret in secrets:
                assert secret not in blob, f"secret leaked in {ev.event_type}: {blob}"

def test_account_register_cannot_attach_password_to_existing_oauth_user(client, db_session):
    """Account registration must not silently link an unauthenticated OAuth identity."""
    from app.identity import User

    user = User(
        id=str(uuid4()),
        email="oauth-account@example.com",
        password_hash=None,
        display_name="OAuth Account",
        status="active",
        identity_source="google",
    )
    db_session.add(user)
    db_session.commit()

    resp = client.post(
        f"{ACCOUNT}/register",
        json={"email": user.email, "password": "AttackerPassword123"},
    )
    assert resp.status_code == 200

    db_session.refresh(user)
    assert user.password_hash is None

    login = client.post(
        f"{ACCOUNT}/login",
        json={"email": user.email, "password": "AttackerPassword123"},
    )
    assert login.status_code == 401


def test_account_session_token_is_platform_only(client, db_session):
    """Durable account session tokens must never be exposed as broker access tokens."""
    from app.routers.deps import _resolve_user
    from app.services.platform_session import is_platform_session_token

    user = _local_user(db_session)
    resp = client.post(
        f"{ACCOUNT}/login",
        json={"email": user.email, "password": "Sup3rSecret!"},
    )
    assert resp.status_code == 200
    session_id = resp.cookies.get(SESSION_COOKIE_NAME)
    assert session_id

    raw_session_token = token_store.get_token(session_id)
    assert raw_session_token.startswith("account:")
    assert is_platform_session_token(raw_session_token)

    resolved = _resolve_user(db_session, session_id)
    assert resolved.user_id == user.id
    assert resolved.access_token is None
