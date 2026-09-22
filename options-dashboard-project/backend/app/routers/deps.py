from __future__ import annotations

from dataclasses import dataclass

from fastapi import Cookie, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db


SESSION_COOKIE_NAME = "strikenova_session"


def _canonical_session_id(
    x_session_id: str | None,
    session_id_cookie: str | None,
) -> str | None:
    """Single canonical resolver for the browser session transport.

    Issue #61: the only browser session transport is the HttpOnly
    ``strikenova_session`` cookie. The ``X-Session-Id`` header remains as a
    server-side compatibility transport for legacy/test clients; it is NOT
    set by the browser application. The legacy ``session_id`` cookie name is
    deliberately NOT consulted — Issue #61 retired it (issue #61 BLOCKER 1:
    a client carrying only the canonical cookie must authenticate; the old
    name must never re-enable transport).
    """
    return session_id_cookie or x_session_id


def get_session_id(
    x_session_id: str | None = Header(default=None),
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> str | None:
    """Resolve the canonical browser session cookie, with header compatibility."""
    return _canonical_session_id(x_session_id, session_id)


@dataclass(frozen=True)
class AuthenticatedUser:
    """Canonical application identity resolved from a session.

    ``user_id`` is the durable ``users.id`` (UUID) — the application-level
    identity that every data query must use.  ``access_token`` is the broker
    token required for Upstox API calls, or ``None`` when the session is
    platform-only (Google/email) with no broker connection.
    """
    user_id: str
    access_token: str | None


def _extract_session_id(
    x_session_id: str | None,
    session_id_cookie: str | None,
) -> str:
    """Extract session ID from the canonical transport, raising 401 if absent."""
    sid = _canonical_session_id(x_session_id, session_id_cookie)
    if not sid:
        raise HTTPException(status_code=401, detail="Not logged in. Visit /auth/login first.")
    return sid


def _resolve_user(db: Session, sid: str) -> AuthenticatedUser:
    """Core resolution: session_id → (user_id, access_token|None).

    Two distinct lookups:
    - token_store.get_token(sid) → broker access token (None if no broker)
    - identity.get_active_session(db, sid) → platform session validity

    Raises 401/403 on any failure.  Pure logic, no DI.
    """
    from app.identity import get_active_session, User
    from app.services import token_store

    # Broker token: None for platform-only sessions, real token for broker sessions
    broker_token = token_store.get_token(sid)

    # Platform session validity: must exist and be active
    session = get_active_session(db, sid)
    if session is None:
        raise HTTPException(status_code=401, detail="Session is invalid or expired.")

    user = db.query(User).filter(User.id == session.user_id).one_or_none()
    if user is None or user.status != "active":
        raise HTTPException(status_code=403, detail="StrikeNova account is not active.")

    # access_token is None for platform-only sessions (no broker connected)
    return AuthenticatedUser(user_id=user.id, access_token=broker_token)


def get_current_user(
    x_session_id: str | None = Header(default=None),
    session_id_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> AuthenticatedUser:
    """Resolve session → user identity WITHOUT a shared DB session.

    Creates its own ``SessionLocal`` connection.  Use this when the endpoint
    does NOT need a ``db: Session = Depends(get_db)`` parameter (e.g.
    market-status, broker-profile that only need the access token).

    Phase 10.2A canonical dependency — uses ``user.id`` (UUID) as the
    application-level identity.  ``session_id`` is transport-only.
    """
    sid = _extract_session_id(x_session_id, session_id_cookie)
    from app.db import SessionLocal
    own_db = SessionLocal()
    try:
        return _resolve_user(own_db, sid)
    finally:
        own_db.close()


class CurrentUser:
    """FastAPI dependency class that shares the request's DB session.

    Usage in an endpoint::

        user: AuthenticatedUser = Depends(CurrentUser())

    This resolves ``db: Session = Depends(get_db)`` first, then queries the
    ``users`` / ``user_sessions`` tables on that same connection — avoiding a
    second connection and keeping tests on the same in-memory database.

    For endpoints that do NOT need a ``db`` parameter, use the simpler
    ``get_current_user`` function dependency instead.
    """

    def __call__(
        self,
        db: Session = Depends(get_db),
        x_session_id: str | None = Header(default=None),
        session_id_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ) -> AuthenticatedUser:
        sid = _extract_session_id(x_session_id, session_id_cookie)
        return _resolve_user(db, sid)


class AdminUser:
    """Day 45 — explicit admin authorization boundary (server-enforced).

    Resolves the SAME authenticated principal as ``CurrentUser`` and then
    additionally requires the durable ``users.is_admin`` flag BEFORE any
    admin work runs. Admin authority is never inferred from tenant
    ownership (BrokerConnection/authorization), broker linkage, or session
    transport — only the explicit flag grants the control plane. Raises
    401 (anonymous), 403 (authenticated non-admin / disabled account).

    Usage::

        user: AuthenticatedUser = Depends(AdminUser())
    """

    def __call__(
        self,
        db: Session = Depends(get_db),
        x_session_id: str | None = Header(default=None),
        session_id_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ) -> AuthenticatedUser:
        sid = _extract_session_id(x_session_id, session_id_cookie)
        user = _resolve_user(db, sid)
        from app.identity import User as UserModel

        row = db.query(UserModel).filter(UserModel.id == user.user_id).one_or_none()
        if row is None or not bool(row.is_admin):
            raise HTTPException(status_code=403, detail="Admin privileges required.")
        return user
