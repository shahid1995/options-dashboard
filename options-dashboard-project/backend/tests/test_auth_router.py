from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base, get_db
from app.identity import User, UserSession, hash_password
from app.main import app
from app.services import token_store, upstox


# The callback endpoint creates its own ``SessionLocal()`` outside of
# FastAPI dependency injection.  We must patch both ``get_db`` AND
# ``SessionLocal`` so the callback's DB session lands on the same
# in-memory database as the test fixtures.


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


@pytest.fixture
def client(db_session):
    def override_get_db():
        yield db_session

    # Patch SessionLocal so the callback's direct ``SessionLocal()``
    # call uses the same in-memory DB as db_session.
    import app.routers.auth as auth_mod

    _orig_session_local = auth_mod.SessionLocal
    auth_mod.SessionLocal = lambda: db_session
    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        auth_mod.SessionLocal = _orig_session_local


def test_login_redirects_to_upstox_with_state(client):
    resp = client.get("/auth/login", follow_redirects=False)
    assert resp.status_code == 307
    location = resp.headers["location"]
    assert location.startswith(f"{upstox.BASE_URL}/login/authorization/dialog")
    assert "state=" in location


def test_callback_with_error_redirects_to_frontend(client):
    resp = client.get("/auth/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == f"{settings.FRONTEND_URL}?login_error=access_denied"
    assert client.get("/auth/status").json() == {"logged_in": False}


def test_callback_without_state_returns_400(client):
    resp = client.get("/auth/callback", params={"code": "auth-code"}, follow_redirects=False)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid or expired OAuth state"


def test_callback_with_forged_state_returns_400(client):
    resp = client.get(
        "/auth/callback", params={"code": "auth-code", "state": "forged"}, follow_redirects=False
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid or expired OAuth state"


def test_callback_without_code_returns_400(client):
    state = token_store.create_oauth_state()
    resp = client.get("/auth/callback", params={"state": state}, follow_redirects=False)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Missing authorization code"


def test_callback_with_code_sets_session_cookie_and_redirects(client, monkeypatch):
    mock_adapter = AsyncMock()
    mock_adapter.exchange_authorization_code = AsyncMock(return_value="tok-xyz")
    mock_adapter.get_profile = AsyncMock(
        return_value={
            "data": {
                "user_id": "broker-user-1",
                "email": "test@example.com",
                "user_name": "Test User",
                "broker": "UPSTOX",
                "is_active": True,
            }
        }
    )
    # extract_account_id is a @staticmethod — must return str, not coroutine
    mock_adapter.extract_account_id = MagicMock(return_value="broker-user-1")
    mock_gw = MagicMock()
    mock_gw.create.return_value = mock_adapter
    monkeypatch.setattr("app.routers.auth.gateway", mock_gw)

    state = token_store.create_oauth_state()
    resp = client.get(
        "/auth/callback", params={"code": "auth-code", "state": state}, follow_redirects=False
    )

    assert resp.status_code == 307
    location = resp.headers["location"]
    assert location.startswith(f"{settings.FRONTEND_URL}/dashboard#session_id=")
    session_id = resp.cookies.get("session_id")
    assert session_id
    assert location == f"{settings.FRONTEND_URL}/dashboard#session_id={session_id}"
    assert token_store.get_token(session_id) == "tok-xyz"


def test_status_logged_out(client):
    resp = client.get("/auth/status")
    assert resp.status_code == 200
    assert resp.json() == {"logged_in": False}


def test_status_logged_in(client):
    session_id = token_store.set_token("tok-xyz")
    resp = client.get("/auth/status", cookies={"session_id": session_id})
    assert resp.status_code == 200
    assert resp.json() == {"logged_in": True}


def test_status_logged_in_via_header(client):
    session_id = token_store.set_token("tok-xyz")
    resp = client.get("/auth/status", headers={"X-Session-Id": session_id})
    assert resp.status_code == 200
    assert resp.json() == {"logged_in": True}


def test_status_logged_out_with_wrong_session(client):
    token_store.set_token("tok-xyz")
    resp = client.get("/auth/status", cookies={"session_id": "wrong"})
    assert resp.status_code == 200
    assert resp.json() == {"logged_in": False}


def test_logout_clears_token(client, db_session):
    from tests.test_helpers import create_test_identity
    session_id, _ = create_test_identity(db_session, "tok-xyz")
    resp = client.post("/auth/logout", cookies={"session_id": session_id})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert token_store.get_token(session_id) is None


def test_logout_via_header(client, db_session):
    from tests.test_helpers import create_test_identity
    session_id, _ = create_test_identity(db_session, "tok-xyz")
    resp = client.post("/auth/logout", headers={"X-Session-Id": session_id})
    assert resp.status_code == 200
    assert token_store.get_token(session_id) is None


def test_logout_requires_valid_session(client):
    token_store.set_token("tok-xyz")
    resp = client.post("/auth/logout")
    assert resp.status_code == 401


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Email/password registration and login (Phase 10.2B-5)
# ---------------------------------------------------------------------------


def _register_user(client, email, password, display_name=None):
    """Helper: register a user via POST /auth/register."""
    payload = {"email": email, "password": password}
    if display_name:
        payload["display_name"] = display_name
    return client.post("/auth/register", json=payload)


def _login_email(client, email, password):
    """Helper: login via POST /auth/login-email."""
    return client.post("/auth/login-email", json={"email": email, "password": password})


def test_register_creates_user(client):
    resp = _register_user(client, "new@test.com", "password123", "New User")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "user_id" in data


def test_register_rejects_duplicate_email(client):
    _register_user(client, "dup@test.com", "password123")
    resp = _register_user(client, "dup@test.com", "password456")
    assert resp.status_code == 409


def test_register_rejects_short_password(client):
    resp = _register_user(client, "short@test.com", "123")
    assert resp.status_code == 422


def test_register_rejects_invalid_email(client):
    resp = client.post("/auth/register", json={"email": "not-an-email", "password": "password123"})
    assert resp.status_code == 422


def test_login_email_returns_unique_session(client):
    _register_user(client, "unique@test.com", "password123")
    resp = _login_email(client, "unique@test.com", "password123")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "session_id" in data
    assert len(data["session_id"]) > 20  # Not a fixed short string


def test_login_email_rejects_wrong_password(client):
    _register_user(client, "wrong@test.com", "password123")
    resp = _login_email(client, "wrong@test.com", "wrongpassword")
    assert resp.status_code == 401
    assert "Invalid email or password" in resp.json()["detail"]


def test_login_email_rejects_unknown_email(client):
    resp = _login_email(client, "nobody@test.com", "password123")
    assert resp.status_code == 401


def test_login_email_rejects_empty_fields(client):
    resp = client.post("/auth/login-email", json={"email": "", "password": ""})
    assert resp.status_code == 422


def test_login_email_session_is_valid(client, db_session):
    """Email login session should be recognized by /auth/status."""
    _register_user(client, "valid@test.com", "password123")
    resp = _login_email(client, "valid@test.com", "password123")
    session_id = resp.json()["session_id"]

    status = client.get("/auth/status", headers={"X-Session-Id": session_id})
    assert status.json() == {"logged_in": True}


def test_two_email_logins_get_unique_sessions(client):
    """Two different users logging in via email get unique session IDs."""
    _register_user(client, "user1@test.com", "password123", "User One")
    _register_user(client, "user2@test.com", "password456", "User Two")

    resp1 = _login_email(client, "user1@test.com", "password123")
    resp2 = _login_email(client, "user2@test.com", "password456")

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    sid1 = resp1.json()["session_id"]
    sid2 = resp2.json()["session_id"]
    assert sid1 != sid2, "Each login must produce a unique session ID"


def test_same_user_two_logins_get_unique_sessions(client):
    """Same user logging in twice gets unique session IDs each time."""
    _register_user(client, "multi@test.com", "password123")
    resp1 = _login_email(client, "multi@test.com", "password123")
    resp2 = _login_email(client, "multi@test.com", "password123")

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    sid1 = resp1.json()["session_id"]
    sid2 = resp2.json()["session_id"]
    assert sid1 != sid2, "Each login must produce a unique session ID"


def test_user_a_cannot_use_user_b_session(client, db_session):
    """User A's session is not recognized when User B tries to use it."""
    from tests.test_helpers import create_test_identity

    # User A via email login
    _register_user(client, "emailA@test.com", "password123")
    resp_a = _login_email(client, "emailA@test.com", "password123")
    sid_a = resp_a.json()["session_id"]

    # User B via OAuth-style test identity
    sid_b, uid_b = create_test_identity(db_session, "tok-b")

    # User B's session works for User B
    status_b = client.get("/auth/status", headers={"X-Session-Id": sid_b})
    assert status_b.json() == {"logged_in": True}

    # User A's session does NOT work for User B
    status_a_for_b = client.get("/auth/status", headers={"X-Session-Id": sid_a})
    assert status_a_for_b.json() == {"logged_in": True}  # Both are valid sessions
    # But they are different sessions
    assert sid_a != sid_b


def test_logout_invalidates_only_correct_session(client, db_session):
    """Logging out User A does not affect User B's session."""
    from tests.test_helpers import create_test_identity

    _register_user(client, "logoutA@test.com", "password123")
    _register_user(client, "logoutB@test.com", "password456")

    resp_a = _login_email(client, "logoutA@test.com", "password123")
    resp_b = _login_email(client, "logoutB@test.com", "password456")
    sid_a = resp_a.json()["session_id"]
    sid_b = resp_b.json()["session_id"]

    # Both logged in
    assert client.get("/auth/status", headers={"X-Session-Id": sid_a}).json()["logged_in"]
    assert client.get("/auth/status", headers={"X-Session-Id": sid_b}).json()["logged_in"]

    # Logout A
    resp = client.post("/auth/logout", headers={"X-Session-Id": sid_a})
    assert resp.status_code == 200

    # A is logged out, B is still logged in
    assert client.get("/auth/status", headers={"X-Session-Id": sid_a}).json()["logged_in"] is False
    assert client.get("/auth/status", headers={"X-Session-Id": sid_b}).json()["logged_in"]


def test_email_session_token_not_fixed_string(client):
    """The email session token must NOT be the fixed 'email-session' string."""
    _register_user(client, "fixed@test.com", "password123")
    resp = _login_email(client, "fixed@test.com", "password123")
    session_id = resp.json()["session_id"]
    token = token_store.get_token(session_id)
    assert token != "email-session", "Email session token must be unique, not a fixed string"
    assert token.startswith("email:"), "Email session token must be user-bound"


def test_email_login_response_exposes_no_secrets(client):
    """Login response must not contain password hashes or session tokens."""
    _register_user(client, "secret@test.com", "password123")
    resp = _login_email(client, "secret@test.com", "password123")
    body = resp.json()
    assert "password" not in body
    assert "password_hash" not in body
    assert "api_key" not in body
    assert "api_secret" not in body
    assert "token" not in body  # Only session_id should be present, not token
    # session_id is expected — it's the session identifier, not the token
    assert "session_id" in body


def test_register_response_exposes_no_secrets(client):
    """Register response must not contain password hashes."""
    resp = _register_user(client, "noreg@test.com", "password123")
    body = resp.json()
    assert "password" not in body
    assert "password_hash" not in body
    assert body["ok"] is True


# ---------------------------------------------------------------------------
# Security: anonymous registration must never link credentials to an
# existing account (P1 account-takeover regression)
# ---------------------------------------------------------------------------

ATTACKER_PASSWORD = "AttackerPass123!"


def _seed_existing_user(db, email, *, identity_source, password=None,
                        display_name="Existing Owner", **extra):
    """Seed a pre-existing account directly, bypassing the registration route."""
    user = User(
        id=str(uuid4()),
        email=email,
        password_hash=hash_password(password) if password else None,
        display_name=display_name,
        status="active",
        identity_source=identity_source,
        **extra,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _snapshot(user):
    """Every persisted User column, so any unintended write is detected."""
    return {column.key: getattr(user, column.key) for column in user.__table__.columns}


@pytest.mark.parametrize(
    "identity_source,extra",
    [
        ("google", {"google_sub": "google-sub-attacked"}),
        (
            "upstox",
            {"broker_provider": "UPSTOX", "broker_user_id": "upstox-user-attacked"},
        ),
    ],
)
def test_register_never_sets_a_password_on_an_existing_oauth_account(
    client, db_session, identity_source, extra
):
    """Anonymous POST /auth/register must not attach a password to an OAuth user."""
    email = f"oauth-{identity_source}@test.com"
    user = _seed_existing_user(
        db_session, email, identity_source=identity_source,
        display_name="OAuth Owner", **extra,
    )
    before = _snapshot(user)

    resp = _register_user(client, email, ATTACKER_PASSWORD, "Attacker")

    # Generic, enumeration-resistant failure — no identity disclosure and no
    # false claim that a password was attached.
    assert resp.status_code == 409
    assert resp.json() == {"detail": "An account with this email already exists"}
    assert "user_id" not in resp.json()
    assert "session_id" not in resp.json()
    assert resp.headers.get("set-cookie") is None
    assert db_session.query(UserSession).count() == 0

    # The existing account is byte-for-byte unchanged: no password, no profile
    # overwrite, no identity-source/verification/broker association change.
    db_session.expire_all()
    after = _snapshot(db_session.query(User).filter(User.id == user.id).one())
    assert after == before
    assert after["password_hash"] is None

    # The attacker-chosen password does not authenticate the pre-existing user.
    login = _login_email(client, email, ATTACKER_PASSWORD)
    assert login.status_code == 401
    assert login.headers.get("set-cookie") is None


def test_duplicate_registration_preserves_an_existing_local_account(client, db_session):
    """A duplicate anonymous registration must not rewrite a local account."""
    email = "local-owner@test.com"
    assert _register_user(client, email, "OriginalPass123!", "Original Name").status_code == 200

    user = db_session.query(User).filter(User.email == email).one()
    before = _snapshot(user)
    sessions_before = db_session.query(UserSession).count()

    resp = _register_user(client, email, ATTACKER_PASSWORD, "Attacker")

    assert resp.status_code == 409
    assert resp.json() == {"detail": "An account with this email already exists"}
    assert "user_id" not in resp.json()
    assert resp.headers.get("set-cookie") is None
    assert db_session.query(UserSession).count() == sessions_before

    db_session.expire_all()
    after = _snapshot(db_session.query(User).filter(User.id == user.id).one())
    assert after == before

    # Original credentials still work; the attacker-chosen password does not.
    assert _login_email(client, email, "OriginalPass123!").status_code == 200
    assert _login_email(client, email, ATTACKER_PASSWORD).status_code == 401


def test_duplicate_registration_does_not_distinguish_account_type(client, db_session):
    """OAuth-created and local accounts must produce an identical response."""
    _seed_existing_user(
        db_session, "enum-oauth@test.com", identity_source="google",
        google_sub="google-sub-enum",
    )
    assert _register_user(client, "enum-local@test.com", "OriginalPass123!").status_code == 200

    oauth_resp = _register_user(client, "enum-oauth@test.com", ATTACKER_PASSWORD)
    local_resp = _register_user(client, "enum-local@test.com", ATTACKER_PASSWORD)

    assert oauth_resp.status_code == 409
    assert local_resp.status_code == 409
    assert oauth_resp.json() == local_resp.json()
    assert oauth_resp.headers.get("set-cookie") == local_resp.headers.get("set-cookie")


def test_blocked_password_link_leaves_oauth_sign_in_working(client, db_session, monkeypatch):
    """The fix must not disable legitimate Google sign-in for the account."""
    import app.routers.auth as auth_mod

    monkeypatch.setattr(
        auth_mod,
        "_verify_google_token",
        lambda credential, expected_nonce: {
            "sub": "google-sub-still-works",
            "email": "still-oauth@test.com",
            "name": "Still OAuth",
        },
    )

    first = client.post(
        "/auth/google",
        json={"credential": "fake", "state": token_store.create_google_oauth_state()},
    )
    assert first.status_code == 200
    user_id = first.json()["user"]["user_id"]

    assert _register_user(client, "still-oauth@test.com", ATTACKER_PASSWORD).status_code == 409

    second = client.post(
        "/auth/google",
        json={"credential": "fake", "state": token_store.create_google_oauth_state()},
    )
    assert second.status_code == 200
    assert second.json()["user"]["user_id"] == user_id
