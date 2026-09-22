import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
import html as html_module
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Body, Cookie, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.brokers.domain.enums import BROKER_ID_UPSTOX
from app.brokers.domain.errors import BrokerError, PUBLIC_BROKER_ERROR_MESSAGE
from app.brokers.gateway import gateway
from app.config import settings
from app.db import SessionLocal, get_db
from app.identity import (
    BrokerConnection,
    User,
    UserSession,
    create_session_record,
    ensure_broker_stamp,
    find_broker_identity_owner,
    get_active_session,
    get_or_create_connection,
    get_or_create_user_from_google,
    get_analytics_token,
    hash_password,
    hash_session_id,
    remove_analytics_token,
    resolve_platform_user,
    resolve_user_credentials,
    revoke_session,
    store_analytics_token,
    store_credentials,
    verify_password,
    BrokerIdentityInUse,
)
from sqlalchemy.exc import IntegrityError
from fastapi import Request as FastAPIRequest
from app.routers.deps import CurrentUser, AuthenticatedUser, get_session_id
from app.services.broker_authorization import persist_connection_authorization
from app.services import token_store
from app.services import account_security
from app.services.rate_limiter import rate_limiter, RateLimitRule

logger = logging.getLogger(__name__)

def _serialize_utc(dt) -> str | None:
    """Serialize a datetime to ISO 8601 with explicit UTC offset.

    SQLite strips timezone info, so datetimes read back are naive UTC.
    This re-attaches UTC and produces +00:00 offset.
    """
    if dt is None:
        return None
    if hasattr(dt, "tzinfo") and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)


router = APIRouter()

SESSION_COOKIE_NAME = "strikenova_session"
SESSION_COOKIE_TTL = 60 * 60 * 24

# ---------------------------------------------------------------------------
# Popup OAuth kickoff — one-time pre-authorized kick token for the seamless
# popup flow. The popup is a TOP-LEVEL navigation to this API origin: it
# carries no X-Session-Id header and (for first-time email/google users) no
# session cookie either, so /auth/login would 401 before the broker page
# loads (verified live in staging 2026-09-15). The authenticated opener page
# mints a single-use kick token (POST /auth/oauth/popup-kick); the popup
# presents it via the sn_oauth_kick cookie. The token is bound to
# (session_id, broker), TTL-bounded, in-memory only, and consumed on first
# successful use — it NEVER creates, extends, or replaces a session.
# ---------------------------------------------------------------------------

POPUP_KICK_COOKIE = "sn_oauth_kick"
_POPUP_KICK_TTL_SECONDS = 600  # never outlives the OAuth state window
_popup_kick_tokens: dict[str, dict] = {}

# Rate limiting rules for auth endpoints (brute-force protection)
rate_limiter.add_rule("/auth/login-email", RateLimitRule(max_requests=20, window_seconds=60))
rate_limiter.add_rule("/auth/register", RateLimitRule(max_requests=10, window_seconds=60))
rate_limiter.add_rule("/auth/google", RateLimitRule(max_requests=20, window_seconds=60))

# Account-security abuse controls (2026-09-16 plan Task 5). Keys are scoped
# per operation: email+IP for unauthenticated login/recovery, user+session
# for authenticated changes. The limiter stays behind the existing
# SessionRateLimiter abstraction so a Redis store can replace it later
# without changing endpoint contracts.
rate_limiter.add_rule("/auth/account/login", RateLimitRule(max_requests=10, window_seconds=60))
rate_limiter.add_rule("/auth/account/register", RateLimitRule(max_requests=5, window_seconds=60))
rate_limiter.add_rule("/auth/account/resend-verification", RateLimitRule(max_requests=3, window_seconds=60))
rate_limiter.add_rule("/auth/account/forgot-password", RateLimitRule(max_requests=5, window_seconds=60))
rate_limiter.add_rule("/auth/account/reset-password", RateLimitRule(max_requests=5, window_seconds=60))
rate_limiter.add_rule("/auth/account/change-password", RateLimitRule(max_requests=5, window_seconds=60))
rate_limiter.add_rule("/auth/account/change-email", RateLimitRule(max_requests=5, window_seconds=60))
rate_limiter.add_rule("/auth/account/verify-email-change", RateLimitRule(max_requests=10, window_seconds=60))
rate_limiter.add_rule("/auth/account/verify-email", RateLimitRule(max_requests=10, window_seconds=60))


def _mint_popup_kick(response: Response, session_id: str, broker_id: str) -> None:
    """Issue a single-use kick token bound to (session_id, broker)."""
    now = time.time()
    for tok, meta in list(_popup_kick_tokens.items()):
        if now - meta["ts"] > _POPUP_KICK_TTL_SECONDS:
            del _popup_kick_tokens[tok]
    token = uuid4().hex
    _popup_kick_tokens[token] = {"ts": now, "sid": session_id, "brk": broker_id}
    response.set_cookie(
        POPUP_KICK_COOKIE,
        token,
        max_age=_POPUP_KICK_TTL_SECONDS,
        secure=True,
        httponly=True,
        samesite="none",  # set from a cross-site XHR; must be None to be stored
        path="/auth",
    )


def _consume_popup_kick_session(cookie_value: str | None, broker_id: str) -> str | None:
    """Consume a kick token and return its bound session_id (single use).

    Fail-closed: unknown/expired/wrong-broker tokens yield None. The token
    is removed on first presentation — it can never be replayed, and it
    never creates, extends, or replaces a real session.
    """
    if not cookie_value:
        return None
    meta = _popup_kick_tokens.pop(str(cookie_value).strip(), None)
    if meta is None:
        return None
    if time.time() - meta["ts"] > _POPUP_KICK_TTL_SECONDS:
        return None
    if meta["brk"] != broker_id:
        return None
    return meta["sid"]


@router.post("/oauth/popup-kick")
def oauth_popup_kick(
    broker: str = Body(..., embed=True),
    session_id: str | None = Depends(get_session_id),
):
    """Mint the popup kick cookie for an authenticated session.

    Called by the opener page (fetch with X-Session-Id) right before
    ``window.open(...)`` so the popup's navigation to /auth/login can prove
    which session initiated it. Fail-closed: unauthenticated callers get 401.
    """
    broker_id = broker.upper()
    if not session_id or token_store.get_token(session_id) is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Log in first to connect a broker.",
        )
    resp = JSONResponse({"ok": True})
    _mint_popup_kick(resp, session_id, broker_id)
    return resp


# ---------------------------------------------------------------------------
# GET /auth/login — Phase 10.2B-2: BYOB-aware login
# ---------------------------------------------------------------------------

@router.get("/login")
def login(
    broker: str = Query(default="UPSTOX"),
    session_id: str | None = Depends(get_session_id),
    popup_kick_cookie: str | None = Cookie(default=None, alias=POPUP_KICK_COOKIE),
    popup: bool = False,
):
    """Redirect the browser to the broker's OAuth login page.

    BYOB path: the user MUST be authenticated AND have stored credentials
    for this broker.  No platform-level credential fallback.

    Day 3 security fix: unauthenticated users cannot initiate OAuth.
    This eliminates the OAuth-state-to-user-identity problem entirely.
    """
    broker_id = broker.upper()

    # Day 3: Require authenticated session — no anonymous OAuth initiation.
    # Seamless-popup exception: the popup is a top-level navigation with no
    # header transport, so it presents the single-use kick token minted by
    # the authenticated opener (POST /auth/oauth/popup-kick). Consumed here
    # on first presentation (never replays); the session itself is still
    # fully validated below, and the token never creates or extends one.
    kick_session_id = _consume_popup_kick_session(popup_kick_cookie, broker_id)
    if not session_id or token_store.get_token(session_id) is None:
        if kick_session_id is None:
            raise HTTPException(
                status_code=401,
                detail="Authentication required. Log in first to connect a broker.",
            )
        session_id = kick_session_id

    # Resolve user's per-user credentials (BYOB path).
    # Day 3: No platform key fallback — user must have stored credentials.
    user_credentials: dict = {}
    db = SessionLocal()
    try:
        session = get_active_session(db, session_id)
        if session is None:
            raise HTTPException(
                status_code=401,
                detail="Session expired or invalid. Please log in again.",
            )
        try:
            user_credentials = resolve_user_credentials(
                session.user_id, broker_id, db
            )
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No {broker_id} credentials found. "
                    f"Store your broker API key/secret via POST /auth/connect first."
                ),
            )
    finally:
        db.close()

    # Phase 10.2B-3: Embed session_id in signed OAuth state for callback binding.
    # The popup flag is also embedded in the state so it survives the broker's
    # redirect — FYERS only preserves auth_code and state, not custom params.
    state = token_store.create_oauth_state(session_id=session_id, broker=broker_id, popup=popup)

    adapter = gateway.create(broker_id, **user_credentials)
    redirect = RedirectResponse(adapter.get_authorization_url(state))
    return redirect


# ---------------------------------------------------------------------------
# GET /auth/callback — Phase 10.2B-2: BYOB-aware callback
# ---------------------------------------------------------------------------

@router.get("/callback")
async def callback(
    code: str | None = None,
    auth_code: str | None = None,
    error: str | None = None,
    state: str | None = None,
    broker: str = Query(default="UPSTOX"),
):
    """Complete broker OAuth using USER's per-user credentials (BYOB).

    Both the authorization-code exchange AND the profile fetch use the
    SAME user's API key/secret.  No shared platform credentials in BYOB path.

    Popup mode: when the signed OAuth state carries ``popup=true``, returns
    an HTML page that sends ``postMessage`` to the opener window instead of
    redirecting.  This enables a seamless "Save & Connect" UX while keeping
    the same OAuth state validation and token exchange flow.  The popup page
    exposes NO tokens, secrets or auth_codes — only a minimal status message.

    The popup flag is embedded in the signed OAuth state (not a query param)
    because FYERS only preserves ``auth_code`` and ``state`` through its
    redirect — custom query parameters would be lost.
    """
    # Phase 10.2B-3: Extract session_id + broker + popup from signed OAuth state.
    # This eliminates the race condition — we know EXACTLY which user initiated OAuth.
    state_data = token_store.consume_oauth_state(state)
    if state_data is None:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    # Resolve the authorization code AFTER consuming the state, because the
    # choice is broker-specific and the broker identity is ONLY trustworthy
    # from the signed state (never from the query string). FYERS v3 redirects
    # back with `s=ok&code=200&auth_code=<JWT>` — its `code` is a NUMERIC
    # STATUS, while the real authorization code arrives as `auth_code`
    # (staging incident 2026-09-15: the previous `code = code or auth_code`
    # alias bound code="200" and exchanged the literal "200", which FYERS
    # rejected). Upstox uses the standard OAuth `code` parameter.
    broker_id_for_code = state_data.get("broker", "UPSTOX")
    if broker_id_for_code == "FYERS":
        code = auth_code or code
    # else: keep the standard `code` parameter as-is.

    # Extract popup flag from signed state (not query param — broker wouldn't preserve it)
    popup = state_data.get("popup", False)

    if error:
        if popup:
            return _popup_error_response(error)
        return RedirectResponse(f"{settings.FRONTEND_ORIGIN}?login_error={quote(error)}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    bound_session_id = state_data.get("session_id", "")
    # Day 3: broker comes ONLY from the signed state, never from the query param.
    broker_id = state_data.get("broker", "UPSTOX")

    # Resolve user's per-user credentials from the bound session.
    # Deterministic: we know exactly which session initiated this OAuth flow.
    # Day 3: credentials are mandatory — no platform fallback in callback.
    user_credentials: dict = {}
    user_id_for_connection: str | None = None

    if not bound_session_id:
        raise HTTPException(
            status_code=400,
            detail="OAuth state missing session binding. Please log in and try again.",
        )

    pre_db = SessionLocal()
    try:
        session = get_active_session(pre_db, bound_session_id)
        if session is None:
            raise HTTPException(
                status_code=400,
                detail="Session expired or invalid. Please log in and try again.",
            )
        user_id_for_connection = session.user_id
        try:
            user_credentials = resolve_user_credentials(
                user_id_for_connection, broker_id, pre_db
            )
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No {broker_id} credentials found for this user. "
                    f"Store your broker API key/secret via POST /auth/connect first."
                ),
            )
    finally:
        pre_db.close()

    try:
        # Create adapter with USER's credentials — both exchange and profile
        # use the same user's API key (single adapter creation, no double-bug)
        adapter = gateway.create(broker_id, **user_credentials)
        access_token = await adapter.exchange_authorization_code(code)
        profile = await gateway.create(
            broker_id, access_token=access_token, **user_credentials
        ).get_profile()
    except BrokerError as e:
        logger.error("Token/profile exchange failed: %s — %s", e.code.value, e.message)
        if popup:
            return _popup_error_response(PUBLIC_BROKER_ERROR_MESSAGE)
        return RedirectResponse(f"{settings.FRONTEND_ORIGIN}?login_error={quote(PUBLIC_BROKER_ERROR_MESSAGE)}")

    # Session-bound linking (UPSTOX_IDENTITY_LINKING_DESIGN.md §7/§17):
    # the state-bound initiating session's user is the ONLY platform
    # identity authority. The callback NEVER creates a User and NEVER
    # consults the broker profile email for identity decisions.
    db = SessionLocal()
    session_id = None
    broker_identity: str | None = None
    broker_account_id: str | None = None
    profile_data = profile.get("data") if isinstance(profile, dict) else {}
    profile_data = profile_data if isinstance(profile_data, dict) else {}

    # FYERS staging validation (identity-confirmation phase): run the safe,
    # masked identity diagnostic on the first real profile response. Reports
    # ONLY profile key names, the selected identity field and a masked value
    # — never tokens, secrets, PIN, PAN or the full email. Non-fatal: a
    # diagnostic failure must never break the connection flow.
    if broker_id == "FYERS":
        try:
            from app.brokers.adapters.fyers.profile import diagnose_profile_identity

            logger.info(
                "FYERS profile identity diagnostic: %s",
                diagnose_profile_identity(profile),
            )
        except Exception:
            logger.exception("FYERS identity diagnostic failed (non-fatal)")

    def _persist_broker_link() -> None:
        """ONE transaction: stamp + connection + UserSession + BrokerToken.

        (design §10/§17.4) — the token row is written on this callback's
        active session; nothing here persists outside the transaction.
        """
        nonlocal session_id
        db.rollback()  # discard any half-flushed state from a prior attempt
        ensure_broker_stamp(db, user, broker_id, broker_identity)

        connection = None
        if broker_account_id:
            connection = get_or_create_connection(
                db, user.id, broker_id, broker_account_id
            )
            # Broker profile email/display name are INFORMATIONAL metadata
            # (design Invariant 4) — stored on the connection, never on
            # users.email.
            try:
                meta = json.loads(connection.provider_metadata_json or "{}")
            except (TypeError, ValueError):
                meta = {}
            profile_meta = {
                k: profile_data.get(k)
                for k in ("email", "user_name", "broker", "is_active")
                if profile_data.get(k) is not None
            }
            if profile_meta:
                meta["upstox_profile"] = profile_meta
                connection.provider_metadata_json = json.dumps(meta)
        else:
            logger.warning(
                "Could not extract broker account ID from %s profile", broker_id
            )

        session_id = token_store.prepare_broker_session(access_token)
        create_session_record(
            db, user.id, session_id,
            broker_connection_id=connection.id if connection else None,
        )
        # LEGACY dual-write (transition only): the session-scoped
        # BrokerToken row is no longer consulted for token resolution —
        # the authoritative source is BrokerAuthorization below. Kept for
        # one release for rollback safety; do NOT build on it.
        token_store.persist_broker_token_row(
            db,
            session_id,
            access_token,
            connection.id if connection else None,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        # FYERS: preserve the refresh token (never silently discarded) —
        # encrypted at rest on the session's token row. Daily re-auth
        # remains the baseline (AD-11); this is best-effort persistence,
        # not a session-persistence strategy.
        refresh_token = getattr(adapter, "_refresh_token", None)
        refresh_token = refresh_token if isinstance(refresh_token, str) and refresh_token else None
        if refresh_token:
            token_store.persist_fyers_refresh_token(db, session_id, refresh_token)
        # BrokerAuthorization — AUTHORITATIVE token source (architecture
        # refactor): belongs to the BrokerConnection, NOT to this browser
        # session. The initiating session proved WHO connected; the
        # authorization's lifetime is independent of that session.
        # Supersedes any previous active authorization for the connection.
        if connection is not None:
            persist_connection_authorization(
                db,
                connection_id=connection.id,
                broker=broker_id,
                access_token=access_token,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
                refresh_token=refresh_token,
                refresh_expires_at=(
                    datetime.now(timezone.utc) + timedelta(days=15)
                ) if refresh_token else None,
                method="oauth_callback",
            )
        db.commit()

    try:
        user = resolve_platform_user(db, user_id_for_connection)
        if user.status != "active":
            db.rollback()
            raise HTTPException(status_code=403, detail="StrikeNova account is not active")

        # Broker-neutral identity extraction (AD-6): the ADAPTER owns the
        # profile-field mapping — Upstox (data.user_id / UCC) today, FYERS
        # (customer Login ID, fail-closed) now. The API App ID is never an
        # ownership identity. broker_user_id == broker_account_id == the
        # extracted customer identity for every broker.
        adapter_account_id = None
        extractor = getattr(adapter, "extract_account_id", None)
        if extractor is not None:
            try:
                extracted = extractor(profile)
            except ValueError as exc:
                raise ValueError(f"{broker_id} identity extraction failed: {exc}") from exc
            if extracted:
                adapter_account_id = str(extracted).strip() or None
        broker_identity = adapter_account_id
        if not broker_identity and broker_id == "UPSTOX":
            # Upstox legacy fallback: the profile's data.user_id (UCC) —
            # §17.2 proved extract_account_id returns the same value.
            broker_identity = str(profile_data.get("user_id") or "").strip()
        if not broker_identity:
            raise ValueError(f"{broker_id} profile did not contain a broker identity")
        broker_account_id = broker_identity

        # Ownership arbitration: a broker identity belongs to at most one
        # StrikeNova user; contested ownership is rejected, never
        # transferred (design Invariants 1/7).
        owner_id = find_broker_identity_owner(
            db, broker_id, broker_identity, broker_account_id
        )
        if owner_id is not None and owner_id != user.id:
            db.rollback()
            if popup:
                return _popup_error_response("This broker account is connected to another StrikeNova user.")
            return RedirectResponse(
                f"{settings.FRONTEND_ORIGIN}?login_error=broker_identity_in_use"
            )

        _persist_broker_link()
    except HTTPException:
        raise
    except BrokerIdentityInUse as e:
        db.rollback()
        logger.warning("Broker identity ownership conflict: %s", e)
        if popup:
            return _popup_error_response("This broker account is connected to another StrikeNova user.")
        return RedirectResponse(
            f"{settings.FRONTEND_ORIGIN}?login_error=broker_identity_in_use"
        )
    except IntegrityError:
        # Database arbitration fired (global ownership index, per-user
        # connection constraint, or the legacy stamp constraint). Roll back
        # and classify by re-reading the committed winner (design §17.3).
        db.rollback()
        session_id = None
        try:
            owner_id = find_broker_identity_owner(
                db, broker_id, broker_identity or "", broker_account_id or ""
            )
        except BrokerIdentityInUse:
            owner_id = None  # corrupt/conflicting records — treat as unrecoverable
        if owner_id is not None and owner_id != user.id:
            logger.warning(
                "Broker identity %s/%s owned by user %s; rejected for user %s",
                broker_id, broker_account_id, owner_id, user.id,
            )
            if popup:
                return _popup_error_response("This broker account is connected to another StrikeNova user.")
            return RedirectResponse(
                f"{settings.FRONTEND_ORIGIN}?login_error=broker_identity_in_use"
            )
        # Same-user reconnect race (or a vanished row): deterministic
        # recover — re-run the link persistence exactly once.
        try:
            _persist_broker_link()
        except Exception:
            db.rollback()
            session_id = None
            logger.exception("Failed to persist StrikeNova identity/session")
            if popup:
                return _popup_error_response("Account setup failed. Please try again.")
            return RedirectResponse(
                f"{settings.FRONTEND_ORIGIN}?login_error=account_setup_failed"
            )
    except Exception:
        db.rollback()
        session_id = None
        logger.exception("Failed to persist StrikeNova identity/session")
        if popup:
            return _popup_error_response("Account setup failed. Please try again.")
        return RedirectResponse(
            f"{settings.FRONTEND_ORIGIN}?login_error=account_setup_failed"
        )
    finally:
        db.close()

    # Design §10 Phase 2: populate the in-memory token cache ONLY after the
    # link transaction committed. Idempotent; a rollback path never reaches
    # this line, so no cached session can reference a rolled-back connection.
    token_store.cache_broker_session(session_id, access_token)

    if popup:
        return _popup_success_response(broker_id)

    # The session is stored in the HttpOnly cookie, never the URL.
    response = RedirectResponse(f"{settings.FRONTEND_ORIGIN}/dashboard")
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=SESSION_COOKIE_TTL,
        path="/",
    )
    return response


# ---------------------------------------------------------------------------
# POST /auth/connect — Phase 10.2B-2: Store broker credentials (BYOB)
# ---------------------------------------------------------------------------

@router.post("/connect")
def connect_broker(
    broker: str = Body(..., embed=True),
    api_key: str = Body(..., embed=True),
    api_secret: str = Body(..., embed=True),
    redirect_uri: str | None = Body(default=None, embed=True),
    display_label: str | None = Body(default=None, embed=True),
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """Store a user's broker Developer App credentials (BYOB onboarding).

    The user must be authenticated to StrikeNova first.
    Credentials are encrypted and stored in broker_connections.

    Validation:
    - api_key and api_secret must be non-empty strings
    - Maximum length: 512 characters each
    """
    # Input validation
    api_key = api_key.strip()
    api_secret = api_secret.strip()

    if not api_key:
        raise HTTPException(status_code=422, detail="api_key must not be empty")
    if len(api_key) > 512:
        raise HTTPException(
            status_code=422, detail="api_key must be 512 characters or fewer"
        )
    if not api_secret:
        raise HTTPException(status_code=422, detail="api_secret must not be empty")
    if len(api_secret) > 512:
        raise HTTPException(
            status_code=422, detail="api_secret must be 512 characters or fewer"
        )

    conn = store_credentials(
        db,
        user_id=user.user_id,
        broker=broker,
        api_key=api_key,
        api_secret=api_secret,
        redirect_uri=redirect_uri,
        display_label=display_label,
    )
    db.commit()

    return {
        "ok": True,
        "connection_id": conn.id,
        "broker": conn.broker,
        "status": conn.status,
    }


# ---------------------------------------------------------------------------
# POST /auth/register — Email/password registration (minimal)
# ---------------------------------------------------------------------------

@router.post("/register")
def register(
    email: str = Body(..., embed=True),
    password: str = Body(..., embed=True),
    display_name: str | None = Body(default=None, embed=True),
    db: Session = Depends(get_db),
):
    """Register a new StrikeNova account with email/password.

    This is a minimal registration endpoint for manual verification.
    The primary auth flow remains Upstox OAuth.
    """
    # Rate limit: use a constant key for register (per-endpoint bucket)
    # Register uses the same key for all clients to prevent mass account
    # creation; login uses per-email to prevent brute-force on a specific account.
    rate_limiter.check(None, "/auth/register", client_id="unauth:register")

    email = email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="A valid email address is required")
    if len(email) > 320:
        raise HTTPException(status_code=422, detail="Email must be 320 characters or fewer")
    if not password:
        raise HTTPException(status_code=422, detail="Password must not be empty")
    if len(password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    if len(password) > 128:
        raise HTTPException(status_code=422, detail="Password must be 128 characters or fewer")

    # Check if email already exists
    existing = db.query(User).filter(User.email == email).one_or_none()
    if existing is not None:
        if existing.identity_source == "email" and existing.password_hash:
            # Account enumeration protection: return 200 with generic message
            # instead of 409 which would leak that the email is registered
            logger.info(
                "Registration attempted for existing email",
                extra={"event": "auth.register.existing_email", "email_domain": email.split("@")[-1]},
            )
            return {"ok": True, "message": "If this email is not already registered, your account has been created."}
        # OAuth-created account with same email — link the password
        existing.password_hash = hash_password(password)
        if display_name:
            existing.display_name = display_name
        db.commit()
        return {"ok": True, "message": "Password set for existing account", "user_id": existing.id}

    user = User(
        id=str(uuid4()),
        email=email,
        password_hash=hash_password(password),
        display_name=display_name or email.split("@")[0],
        status="active",
        identity_source="email",
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return {"ok": True, "message": "Account created", "user_id": user.id}


# ---------------------------------------------------------------------------
# POST /auth/login-email — Email/password login
# ---------------------------------------------------------------------------

@router.post("/login-email")
def login_email(
    email: str = Body(..., embed=True),
    password: str = Body(..., embed=True),
    response: Response = None,
    db: Session = Depends(get_db),
):
    """Authenticate with email/password and return a session.

    The browser session is returned only as the HttpOnly cookie.
    """
    # Rate limit: use email as client identifier (unauthenticated endpoint)
    rate_limiter.check(None, "/auth/login-email", client_id=f"unauth:{email.strip().lower()}")

    email = email.strip().lower()
    if not email or not password:
        raise HTTPException(status_code=422, detail="Email and password are required")

    user = db.query(User).filter(User.email == email).one_or_none()
    if user is None or not user.password_hash:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.status != "active":
        raise HTTPException(status_code=403, detail="StrikeNova account is not active")
    if not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Create session — generate a unique session-bound token (not a broker
    # access token; email login has no broker token).  Each login gets a
    # distinct, non-guessable value so two users cannot share a session
    # and DB fallback after restart returns the correct per-session value.
    from app.services.token_store import set_token

    user.last_login_at = datetime.now(timezone.utc)
    session_token = f"email:{user.id}:{secrets.token_urlsafe(24)}"
    session_id = set_token(
        session_token,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        persist_to_db=False,  # Platform sessions use UserSession, not BrokerToken
    )
    create_session_record(db, user.id, session_id)
    db.commit()

    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=SESSION_COOKIE_TTL,
        path="/",
    )

    return {
        "ok": True,
        "user": {
            "user_id": user.id,
            "email": user.email,
            "display_name": user.display_name,
        },
    }


# ---------------------------------------------------------------------------
# POST /auth/google/state — Generate HMAC-signed state for nonce binding
# ---------------------------------------------------------------------------

@router.post("/google/state")
def google_oauth_state():
    """Generate an HMAC-signed state value for Google OAuth nonce binding.

    The frontend calls this before redirecting to Google.  It stores a
    random nonce in HMAC-signed state and returns the state string.
    The frontend includes this state in the Google OAuth URL.
    When Google redirects back, the frontend sends the state alongside
    the id_token to POST /auth/google.
    """
    state = token_store.create_google_oauth_state()
    # Return both state and nonce.  The frontend must use the nonce value
    # (not generate its own) as the Google OAuth nonce parameter, so the
    # backend can later compare it against the JWT nonce claim.
    nonce = token_store.peek_google_oauth_nonce(state)
    return {"state": state, "nonce": nonce}


# ---------------------------------------------------------------------------
# POST /auth/google — Google One Tap / Sign-In
# ---------------------------------------------------------------------------

@router.post("/google")
def google_auth(
    credential: str = Body(..., embed=True),
    state: str | None = Body(default=None, embed=True),
    response: Response = None,
    db: Session = Depends(get_db),
):
    """Authenticate via Google Sign-In (One Tap / GIS).

    Accepts a Google ID token (JWT), verifies it against Google's public
    keys, extracts the user's identity, and creates or links a StrikeNova
    account.

    Account linking:
    - If a user with this Google sub exists → login.
    - If a user with this email exists → link Google to existing account.
    - Otherwise → create new account.

    Returns the session only as the secure HttpOnly ``strikenova_session``
    cookie (Issue #61 secure transport); the response body carries user
    info only — never ``session_id`` or token material.
    """
    # Rate limit: use a hash of the credential as client identifier
    # (unauthenticated endpoint, no session yet)
    import hashlib as _hashlib
    cred_hash = _hashlib.sha256(credential.encode()).hexdigest()[:16]
    rate_limiter.check(None, "/auth/google", client_id=f"unauth:google:{cred_hash}")

    if not credential:
        raise HTTPException(status_code=422, detail="Google credential is required")

    # Phase A security: state is MANDATORY for nonce binding.
    # The HMAC-signed state carries a nonce that must match the JWT nonce.
    # Without state, an attacker could replay a stolen Google ID token.
    if not state:
        raise HTTPException(
            status_code=401,
            detail="Google OAuth state is required. Please restart the sign-in flow.",
        )

    expected_nonce = token_store.consume_google_oauth_state(state)
    if expected_nonce is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired Google OAuth state",
        )

    # Verify the Google ID token
    google_user = _verify_google_token(credential, expected_nonce=expected_nonce)
    if google_user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired Google credential")

    # Get or create the StrikeNova user
    try:
        user = get_or_create_user_from_google(
            db,
            google_sub=google_user["sub"],
            email=google_user.get("email"),
            display_name=google_user.get("name"),
        )
    except Exception:
        db.rollback()
        logger.exception("Failed to create/link Google user")
        raise HTTPException(status_code=500, detail="Account creation failed")

    if user.status != "active":
        raise HTTPException(status_code=403, detail="StrikeNova account is not active")

    # Create session (same pattern as email login)
    user.last_login_at = datetime.now(timezone.utc)
    session_token = f"google:{user.id}:{secrets.token_urlsafe(24)}"
    session_id = token_store.set_token(
        session_token,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        persist_to_db=False,  # Platform sessions use UserSession, not BrokerToken
    )
    create_session_record(db, user.id, session_id)
    db.commit()

    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=SESSION_COOKIE_TTL,
        path="/",
    )

    return {
        "ok": True,
        "user": {
            "user_id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "identity_source": user.identity_source,
        },
    }


def _verify_google_token(credential: str, expected_nonce: str) -> dict | None:
    """Verify a Google ID token (JWT) and return the payload.

    Uses Google's public JWKS endpoint to verify the token signature.
    Returns the decoded payload with at minimum 'sub' and optionally
    'email', 'name', 'picture'.

    Returns None if verification fails.
    """
    import json
    import time
    from urllib.request import urlopen, Request
    from urllib.error import URLError
    import base64 as _b64

    client_id = settings.GOOGLE_CLIENT_ID
    if not client_id:
        logger.error("GOOGLE_CLIENT_ID not configured")
        raise HTTPException(
            status_code=500,
            detail="Google authentication is not configured",
        )

    try:
        # Split the JWT
        parts = credential.split(".")
        if len(parts) != 3:
            return None

        header_b64, payload_b64, signature_b64 = parts

        # Decode header to get kid
        header_json = _b64.urlsafe_b64decode(header_b64 + "==")
        header = json.loads(header_json)
        kid = header.get("kid")
        alg = header.get("alg")
        if alg != "RS256" or not kid:
            return None

        # Phase A security: Pre-flight checks on payload BEFORE JWKS fetch.
        # Decode the payload to check issuer and nonce early — avoids an
        # unnecessary network round-trip to Google JWKS for obviously
        # invalid tokens.
        try:
            payload_pre = json.loads(_b64.urlsafe_b64decode(payload_b64 + "=="))
        except Exception:
            return None

        # Explicit issuer validation — prevents acceptance of JWTs from
        # non-Google issuers.
        valid_issuers = {"https://accounts.google.com", "accounts.google.com"}
        if payload_pre.get("iss") not in valid_issuers:
            logger.warning(
                "Google token rejected: invalid issuer %s",
                payload_pre.get("iss"),
            )
            return None

        # Nonce validation — MANDATORY.
        # The expected_nonce comes from the HMAC-signed state (required by
        # POST /auth/google).  The JWT nonce MUST match exactly.
        # This cryptographically binds the token to this specific auth attempt.
        jwt_nonce = payload_pre.get("nonce")
        if not jwt_nonce:
            logger.warning("Google token rejected: missing nonce claim")
            return None
        if expected_nonce is None:
            # Defensive: should never reach here (state is required upstream)
            logger.warning("Google token rejected: no expected nonce (state missing)")
            return None
        if jwt_nonce != expected_nonce:
            logger.warning(
                "Google token rejected: nonce mismatch (expected from state, got from JWT)",
            )
            return None

        # Fetch Google's public keys
        jwks_url = "https://www.googleapis.com/oauth2/v3/certs"
        req = Request(jwks_url, headers={"User-Agent": "StrikeNova/1.0"})
        with urlopen(req, timeout=10) as resp:
            jwks = json.loads(resp.read())

        # Find the matching key and build RSA public key directly
        from jwt import decode as jwt_decode
        from jwt.algorithms import RSAAlgorithm

        rsa_key = None
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                rsa_key = RSAAlgorithm.from_jwk(key)
                break
        if rsa_key is None:
            return None

        payload = jwt_decode(
            credential,
            rsa_key,
            algorithms=["RS256"],
            audience=client_id,
            options={"verify_exp": True},
        )

        return {
            "sub": payload["sub"],
            "email": payload.get("email"),
            "name": payload.get("name"),
            "picture": payload.get("picture"),
        }
    except Exception as e:
        # Phase A fix: remove debug print, use structured logging only.
        # Never log the credential/token itself.
        logger.warning(
            "Google token verification failed: %s: %s",
            type(e).__name__, e,
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Remaining endpoints (unchanged from 10.2A)
# ---------------------------------------------------------------------------

@router.get("/status")
def status(session_id: str | None = Depends(get_session_id)):
    """Frontend calls this to check if the current session is valid."""
    return {"logged_in": token_store.get_token(session_id) is not None}


@router.get("/me")
def me(session_id: str | None = Depends(get_session_id), db: Session = Depends(get_db)):
    """Return the authenticated StrikeNova account without broker secrets."""
    if token_store.get_token(session_id) is None:
        raise HTTPException(status_code=401, detail="Not logged in")

    session = get_active_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=401, detail="StrikeNova session is invalid or expired")

    from app.identity import User

    user = db.query(User).filter(User.id == session.user_id).one_or_none()
    if user is None or user.status != "active":
        raise HTTPException(status_code=403, detail="StrikeNova account is not active")

    return {
        "user_id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "status": user.status,
        "identity_source": user.identity_source,
        "broker_provider": user.broker_provider,
        "created_at": _serialize_utc(user.created_at),
        "last_login_at": _serialize_utc(user.last_login_at),
    }


@router.post("/logout")
def logout(session_id: str | None = Depends(get_session_id), db: Session = Depends(get_db)):
    # Idempotent: safe to call even if session is already revoked, expired,
    # or never existed. Always returns the same successful response so that
    # repeated logout calls, browser retries, or race conditions do not leak
    # information about whether a session was valid.
    if session_id:
        revoke_session(db, session_id)
        db.commit()
        token_store.clear_token(session_id)

    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE_NAME, httponly=True, secure=True, samesite="none")
    return response


# ---------------------------------------------------------------------------
# Analytics Token endpoints (Phase 10.2B-4)
# ---------------------------------------------------------------------------


@router.post("/connect-analytics-token")
def connect_analytics_token(
    broker: str = Body(default="UPSTOX", embed=True),
    analytics_token: str = Body(..., embed=True),
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """Store the user's Analytics Token for read-only market data access.

    The user must have an active broker connection for this broker.
    The Analytics Token is encrypted and stored on the broker_connection row.

    Validation:
    - analytics_token must be non-empty
    - Maximum length: 512 characters
    """
    analytics_token = analytics_token.strip()
    if not analytics_token:
        raise HTTPException(status_code=422, detail="analytics_token must not be empty")
    if len(analytics_token) > 512:
        raise HTTPException(
            status_code=422, detail="analytics_token must be 512 characters or fewer"
        )

    try:
        store_analytics_token(db, user.user_id, broker, analytics_token)
        db.commit()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {"ok": True, "broker": broker.upper(), "message": "Analytics Token stored"}


@router.get("/analytics-token/status")
def analytics_token_status(
    broker: str = Query(default="UPSTOX"),
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """Check if the user has an Analytics Token stored for this broker.

    Does NOT return the actual token — only whether it exists.
    """
    conn = (
        db.query(BrokerConnection)
        .filter(
            BrokerConnection.user_id == user.user_id,
            BrokerConnection.broker == broker.upper(),
            BrokerConnection.status == "connected",
            BrokerConnection.is_default == True,
        )
        .first()
    )
    if conn is None:
        return {
            "has_analytics_token": False,
            "broker": broker.upper(),
            "message": "No connected broker found",
        }

    return {
        "has_analytics_token": conn.broker_analytics_token_encrypted is not None,
        "broker": broker.upper(),
        "connection_id": conn.id,
    }


@router.delete("/analytics-token")
def delete_analytics_token(
    broker: str = Query(default="UPSTOX"),
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """Remove the user's Analytics Token for this broker."""
    removed = remove_analytics_token(db, user.user_id, broker)
    db.commit()

    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"No Analytics Token found for {broker.upper()}",
        )

    return {"ok": True, "broker": broker.upper(), "message": "Analytics Token removed"}


# ---------------------------------------------------------------------------
# Popup OAuth helpers — return HTML postMessage pages for seamless UX
# ---------------------------------------------------------------------------


def _popup_target_origins() -> list[str]:
    """Frontend origins allowed to receive popup postMessages.

    The primary FRONTEND origin plus every additional configured CORS
    origin. Never a wildcard: the popup loops over EXACT origins so the
    message is delivered only to the opener's real origin (which may be a
    preview deployment listed in FRONTEND_URL/ADDITIONAL_CORS_ORIGINS
    rather than the first entry).
    """
    origins: list[str] = []
    for raw in (settings.FRONTEND_URL, settings.ADDITIONAL_CORS_ORIGINS):
        for part in (raw or "").split(","):
            origin = part.strip().rstrip("/")
            if origin.startswith(("http://", "https://")) and origin not in origins:
                origins.append(origin)
    return origins or [settings.FRONTEND_ORIGIN]


def _post_message_block(indent: str) -> str:
    """Render one literal postMessage call per allowed origin.

    Literal quoted origins (never '*') keep the messages inspectable and
    the security test contract explicit.
    """
    lines = [
        f'{indent}try {{ window.opener.postMessage(payload, "{origin}"); }} catch (_) {{}}'
        for origin in _popup_target_origins()
    ]
    return "\n".join(lines)


def _popup_success_response(broker: str) -> HTMLResponse:
    """Return an HTML page that posts a success message to the opener window.

    The page intentionally exposes NO tokens, secrets, or auth_codes —
    only a minimal status marker.  The opener's listener updates the UI.
    """
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Broker Connected</title></head>
<body>
<p id="status">Connection complete. You can close this window.</p>
<script>
  (function() {{
    var payload = {{
      source: "strikenova-broker-oauth",
      broker: "{broker.upper()}",
      status: "connected"
    }};
    try {{
{_post_message_block("      ")}
    }} catch (_) {{}}
    // Close the popup after a brief delay to let the message be received.
    setTimeout(function() {{ window.close(); }}, 300);
  }})();
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


def _popup_error_response(message: str) -> HTMLResponse:
    """Return a safe error page for the popup.

    Never exposes tokens, secrets, auth_codes or raw OAuth payloads.
    A user-friendly message (e.g. "access_denied") is shown sanitized.
    """
    # Map known OAuth/broker error codes to user-friendly messages
    # Never expose raw error codes that could confuse users
    error_map = {
        "access_denied": "Authorization was denied. Please try again and approve the connection.",
        "invalid_request": "Invalid request. Please check your app configuration.",
        "invalid_client": "Invalid app credentials. Please check your App ID and Secret.",
        "invalid_grant": "Authorization expired. Please try again.",
        "unauthorized_client": "This app is not authorized for this operation.",
        "unsupported_response_type": "Unsupported authorization type.",
        "invalid_scope": "Invalid permissions requested.",
        "server_error": "The broker's server encountered an error. Please try again later.",
        "temporarily_unavailable": "The broker is temporarily unavailable. Please try again later.",
    }
    # Get user-friendly message or use a generic one
    safe_message = error_map.get(message.lower().strip(), "Unable to connect. Please try again.")
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Connection Failed</title></head>
<body>
<p id="status">Unable to connect.</p>
<p style="color:#666;font-size:0.9em;">{safe_message}</p>
<script>
  (function() {{
    var payload = {{
      source: "strikenova-broker-oauth",
      status: "error",
      error: "connection_failed"
    }};
    try {{
{_post_message_block("      ")}
    }} catch (_) {{}}
    setTimeout(function() {{ window.close(); }}, 2000);
  }})();
</script>
</body>
</html>"""
    return HTMLResponse(content=html_content)


# ===========================================================================
# StrikeNova Account Security — /auth/account/*
#
# These endpoints authenticate the StrikeNova User and manage durable
# UserSessions (account identity). They are deliberately separate from the
# broker OAuth flow above: GET /auth/login remains the ONLY broker OAuth
# initiation route and the broker gateway is never involved here.
# (2026-09-16 account-security design spec §3/§9.)
# ===========================================================================


def _account_user_from_session(db: Session, session_id: str | None) -> tuple[User, UserSession] | None:
    """Resolve an active durable UserSession to its active User."""
    if not session_id:
        return None
    session = get_active_session(db, session_id)
    if session is None:
        return None
    user = db.query(User).filter(User.id == session.user_id).one_or_none()
    if user is None or user.status != "active":
        return None
    return user, session


@router.post("/account/login")
def account_login(
    email: str = Body(..., embed=True),
    password: str = Body(..., embed=True),
    response: Response = None,
    db: Session = Depends(get_db),
):
    """Authenticate a StrikeNova account with email/password.

    Creates a durable UserSession and applies the existing secure cookie
    policy (HttpOnly, Secure, SameSite=None). Independent of broker OAuth:
    no broker credentials are required and the broker gateway is never
    invoked.
    """
    # Abuse control: per-email+IP key protects one account from brute force
    # without letting attackers lock out other users (plan Task 5).
    rate_limiter.check(None, "/auth/account/login", client_id=f"acct-login:{email.strip().lower()}")

    email = (email or "").strip().lower()
    if not email or not password:
        raise HTTPException(status_code=422, detail="Email and password are required")

    user = db.query(User).filter(User.email == email).one_or_none()
    if user is None or not user.password_hash:
        account_security.record_security_event(
            db,
            user_id=None,
            event_type="login_failed",
            metadata={"reason": "unknown_email_or_no_local_password"},
        )
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.status != "active":
        account_security.record_security_event(
            db,
            user_id=user.id,
            event_type="login_failed",
            metadata={"reason": "account_not_active"},
        )
        db.commit()
        raise HTTPException(status_code=403, detail="StrikeNova account is not active")
    if not verify_password(password, user.password_hash):
        account_security.record_security_event(
            db,
            user_id=user.id,
            event_type="login_failed",
            metadata={"reason": "bad_password"},
        )
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid email or password")

    user.last_login_at = datetime.now(timezone.utc)
    session_id, _record = account_security.issue_account_session(db, user)
    # Server-side recent-authentication state for sensitive-change gating
    # (plan Task 4): tied to (user_id, session_id), TTL-enforced on read.
    account_security.mark_recently_authenticated(db, user.id, session_id)
    account_security.record_security_event(
        db,
        user_id=user.id,
        event_type="login_succeeded",
        session_id=hash_session_id(session_id),
        metadata={"identity_source": user.identity_source},
    )
    db.commit()

    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=SESSION_COOKIE_TTL,
        path="/",
    )

    return {
        "ok": True,
        "user": {
            "user_id": user.id,
            "email": user.email,
            "display_name": user.display_name,
        },
    }


@router.get("/account/session")
def account_session(
    session_id: str | None = Depends(get_session_id),
    db: Session = Depends(get_db),
):
    """Return the authenticated account and durable-session state.

    UserSession.revoked_at / expires_at are the authority: revoked or
    expired sessions are rejected with 401.
    """
    resolved = _account_user_from_session(db, session_id)
    if resolved is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user, session = resolved

    return {
        "authenticated": True,
        "user": {
            "user_id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "identity_source": user.identity_source,
        },
        "session": {
            "created_at": _serialize_utc(session.created_at),
            "expires_at": _serialize_utc(session.expires_at),
        },
    }


@router.post("/account/logout")
def account_logout(
    session_id: str | None = Depends(get_session_id),
    db: Session = Depends(get_db),
):
    """Revoke the caller's current account session.

    Idempotent: revoking an unknown/already-revoked session still succeeds
    without revealing whether it ever existed.
    """
    if session_id:
        resolved = _account_user_from_session(db, session_id)
        account_security.record_security_event(
            db,
            user_id=resolved[0].id if resolved else None,
            event_type="logout",
            session_id=hash_session_id(session_id),
        )
    account_security.revoke_one(db, session_id)
    db.commit()
    if session_id:
        token_store.clear_token(session_id)

    response = JSONResponse({"ok": True})
    response.delete_cookie(
        SESSION_COOKIE_NAME, httponly=True, secure=True, samesite="none", path="/"
    )
    return response


@router.post("/account/logout-all")
def account_logout_all(
    session_id: str | None = Depends(get_session_id),
    db: Session = Depends(get_db),
):
    """Revoke every active account session for the authenticated user."""
    resolved = _account_user_from_session(db, session_id)
    if resolved is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user, _session = resolved

    revoked = account_security.revoke_all_for_user(db, user.id)
    account_security.record_security_event(
        db,
        user_id=user.id,
        event_type="logout_all",
        metadata={"revoked_sessions": revoked},
    )
    db.commit()
    if session_id:
        token_store.clear_token(session_id)

    return {"ok": True, "revoked_sessions": revoked}


# ---------------------------------------------------------------------------
# Account registration + email verification (2026-09-16 plan Task 3)
# ---------------------------------------------------------------------------


@router.post("/account/register")
def account_register(
    email: str = Body(..., embed=True),
    password: str = Body(..., embed=True),
    display_name: str | None = Body(default=None, embed=True),
    db: Session = Depends(get_db),
):
    rate_limiter.check(None, "/auth/account/register", client_id="unauth:register")

    """Register a StrikeNova local (email/password) account.

    Per design spec §6: the account is created UNVERIFIED, a single-use
    hashed verification token is issued, and the verification email is sent
    through the provider-neutral transport. No session is created — the user
    must verify their email before logging in.

    Enumeration protection: a duplicate local registration returns the same
    200 shape as a fresh one without creating accounts or tokens.
    """
    email = account_security.normalize_email(email)
    detail = account_security.validate_registration(email, password)
    if detail:
        raise HTTPException(status_code=422, detail=detail)

    existing = db.query(User).filter(User.email == email).one_or_none()
    if existing is not None:
        if existing.identity_source == "email" and existing.password_hash:
            # Enumeration-resistant generic response; no state change.
            return {
                "ok": True,
                "message": "Check your email to verify your account.",
            }
        # OAuth-linked account (google/upstox) without a local password:
        # setting a password follows the legacy /auth/register contract.
        existing.password_hash = hash_password(password)
        if display_name:
            existing.display_name = display_name
        db.commit()
        return {"ok": True, "message": "Check your email to verify your account."}

    user = User(
        id=str(uuid4()),
        email=email,
        password_hash=hash_password(password),
        display_name=display_name or email.split("@")[0],
        status="pending_verification",
        identity_source="email",
    )
    db.add(user)
    db.flush()

    raw_token, _record = account_security.create_verification_token(db, user.id)
    account_security.record_security_event(
        db,
        user_id=user.id,
        event_type="registration_completed",
        metadata={"identity_source": "email"},
    )
    db.commit()

    account_security.send_verification_email(email, raw_token)

    return {"ok": True, "message": "Check your email to verify your account."}


@router.post("/account/verify-email")
def account_verify_email(
    token: str = Body(..., embed=True),
    db: Session = Depends(get_db),
):
    """Consume a single-use verification token and mark the account verified.

    Expired/used/unknown tokens fail closed with 400. No session is created;
    the user logs in normally afterwards. Emits a security event.
    """
    record = account_security.consume_verification_token(db, token)
    if record is None:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")

    user = db.query(User).filter(User.id == record.user_id).one_or_none()
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")

    user.status = "active"
    account_security.record_security_event(
        db,
        user_id=user.id,
        event_type="email_verification_completed",
        metadata={"identity_source": user.identity_source},
    )
    db.commit()
    return {"ok": True, "message": "Email verified. You can now log in."}


@router.post("/account/resend-verification")
def account_resend_verification(
    email: str = Body(..., embed=True),
    db: Session = Depends(get_db),
):
    rate_limiter.check(
        None, "/auth/account/resend-verification",
        client_id=f"acct-resend:{(email or '').strip().lower()}",
    )

    """Re-issue the verification email, invalidating prior active tokens.

    Enumeration-resistant: unknown addresses and already-active accounts get
    the identical generic response and no email.
    """
    email = account_security.normalize_email(email)
    generic = {"ok": True, "message": "If your email needs verification, we sent a link."}

    user = db.query(User).filter(User.email == email).one_or_none()
    if (
        user is None
        or user.identity_source != "email"
        or not user.password_hash
        or user.status != "pending_verification"
    ):
        return generic

    raw_token, _record = account_security.create_verification_token(db, user.id)
    account_security.record_security_event(
        db,
        user_id=user.id,
        event_type="email_verification_requested",
        metadata={"resend": True},
    )
    db.commit()

    account_security.send_verification_email(email, raw_token)
    return generic


# ---------------------------------------------------------------------------
# Password recovery + sensitive account changes (2026-09-16 plan Task 4)
# ---------------------------------------------------------------------------


@router.post("/account/forgot-password")
def account_forgot_password(
    email: str = Body(..., embed=True),
    db: Session = Depends(get_db),
):
    rate_limiter.check(
        None, "/auth/account/forgot-password",
        client_id=f"acct-forgot:{(email or '').strip().lower()}",
    )

    """Request a password-reset email.

    Enumeration protection (design spec §6): known and unknown addresses
    receive the IDENTICAL public response; nothing in the body or status
    reveals account existence. Only eligible local-password accounts get a
    reset email with a short-lived, hashed, single-use token.
    """
    email = account_security.normalize_email(email)
    generic = {"ok": True, "message": "If that email has an account, a reset link is on its way."}

    if not email or "@" not in email:
        return generic

    user = db.query(User).filter(User.email == email).one_or_none()
    if user is None or user.identity_source != "email" or not user.password_hash:
        return generic

    raw_token, _record = account_security.create_reset_token(db, user.id)
    account_security.record_security_event(
        db,
        user_id=user.id,
        event_type="password_reset_requested",
        metadata={"transport": "email"},
    )
    db.commit()

    account_security.send_password_reset_email(email, raw_token)
    return generic


@router.post("/account/reset-password")
def account_reset_password(
    token: str = Body(..., embed=True),
    new_password: str = Body(..., embed=True),
    db: Session = Depends(get_db),
):
    # Never keyed by the token value: the bucket must not leak token validity.
    rate_limiter.check(None, "/auth/account/reset-password", client_id="unauth:reset")

    """Consume a reset token atomically and set the new password.

    Per design spec §5/§6: consumption invalidates the token, revokes ALL
    existing sessions, records a security event, and sends a security
    notification — and does NOT create a new authenticated session. The user
    logs in normally afterwards.
    """
    detail = account_security.validate_registration("reset@example.com", new_password)
    if detail:
        raise HTTPException(status_code=422, detail=detail)

    record = account_security.consume_reset_token(db, token)
    if record is None:
        account_security.record_security_event(
            db,
            user_id=None,
            event_type="password_reset_failed",
            metadata={"reason": "invalid_or_expired_token"},
        )
        db.commit()
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    user = db.query(User).filter(User.id == record.user_id).one_or_none()
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    user.password_hash = hash_password(new_password)
    account_security.revoke_all_for_user(db, user.id)
    account_security.record_security_event(
        db,
        user_id=user.id,
        event_type="password_reset_completed",
        metadata={"sessions_revoked": "all"},
    )
    db.commit()

    account_security.send_generic_notification_email(
        user.email,
        "Your StrikeNova password was changed",
        "Your StrikeNova password was just reset. If this was not you, "
        "contact support immediately.",
    )
    return {"ok": True, "message": "Password updated. Please log in with your new password."}


@router.post("/account/change-password")
def account_change_password(
    current_password: str = Body(..., embed=True),
    new_password: str = Body(..., embed=True),
    session_id: str | None = Depends(get_session_id),
    db: Session = Depends(get_db),
):
    rate_limiter.check(session_id, "/auth/account/change-password")

    """Change the password for the authenticated account.

    Requires an authenticated durable session plus server-side recent
    authentication (never a client-supplied boolean). On success the current
    session is retained and all other active sessions are revoked (session
    policy, design spec §6). Sends a security notification.
    """
    resolved = _account_user_from_session(db, session_id)
    if resolved is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user, session = resolved
    if not verify_password(current_password, user.password_hash or ""):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    account_security.require_recently_authenticated(db, user.id, session_id)

    detail = account_security.validate_registration(user.email, new_password)
    if detail:
        raise HTTPException(status_code=422, detail=detail)

    user.password_hash = hash_password(new_password)
    now = datetime.now(timezone.utc)
    others = (
        db.query(UserSession)
        .filter(
            UserSession.user_id == user.id,
            UserSession.id != session.id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
        .all()
    )
    for other in others:
        other.revoked_at = now
        account_security.record_security_event(
            db,
            user_id=user.id,
            event_type="session_revoked",
            session_id=other.session_hash,
            metadata={"scope": "password_change"},
        )
    # Revoke all OTHER sessions in the in-memory cache so cached broker
    # tokens become immediately unusable.
    token_store.mark_all_sessions_revoked_except(session_id)
    account_security.record_security_event(
        db,
        user_id=user.id,
        event_type="password_changed",
        session_id=hash_session_id(session_id or ""),
        metadata={"other_sessions_revoked": len(others)},
    )
    db.commit()

    account_security.send_generic_notification_email(
        user.email,
        "Your StrikeNova password was changed",
        "Your StrikeNova password was just changed from your account "
        "settings. If this was not you, contact support immediately.",
    )
    return {"ok": True, "message": "Password updated. Other sessions have been signed out."}


@router.post("/account/change-email")
def account_change_email(
    new_email: str = Body(..., embed=True),
    session_id: str | None = Depends(get_session_id),
    db: Session = Depends(get_db),
):
    rate_limiter.check(session_id, "/auth/account/change-email")

    """Request an email change.

    Requires an authenticated durable session plus server-side recent
    authentication. The change is stored as a pending record with a hashed,
    single-use token; the CURRENT email remains authoritative until the new
    address is verified. Confirmation goes to the NEW address.
    """
    resolved = _account_user_from_session(db, session_id)
    if resolved is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user, _session = resolved
    account_security.require_recently_authenticated(db, user.id, session_id)

    new_email = account_security.normalize_email(new_email)
    if not new_email or "@" not in new_email:
        raise HTTPException(status_code=422, detail="A valid new email address is required")
    if len(new_email) > 320:
        raise HTTPException(status_code=422, detail="Email must be 320 characters or fewer")
    if new_email == user.email:
        raise HTTPException(status_code=422, detail="New email must differ from the current email")
    clash = db.query(User).filter(User.email == new_email).one_or_none()
    if clash is not None:
        raise HTTPException(status_code=409, detail="That email is already in use")

    raw_token, _record = account_security.create_email_change_token(db, user.id, new_email)
    account_security.record_security_event(
        db,
        user_id=user.id,
        event_type="email_change_requested",
        metadata={"new_email_domain": new_email.split("@")[-1]},
    )
    db.commit()

    account_security.send_email_change_email(new_email, raw_token)
    return {"ok": True, "message": "Check your new email to confirm the change."}


@router.post("/account/verify-email-change")
def account_verify_email_change(
    token: str = Body(..., embed=True),
    db: Session = Depends(get_db),
):
    """Consume a single-use email-change token and complete the change.

    Expired/used/unknown tokens fail closed with 400; the current email stays
    authoritative until this succeeds. Records a security event and sends a
    security notification after the change.
    """
    record = account_security.consume_email_change_token(db, token)
    if record is None:
        raise HTTPException(status_code=400, detail="Invalid or expired change token")

    user = db.query(User).filter(User.id == record.user_id).one_or_none()
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired change token")

    old_email = user.email
    user.email = record.new_email
    account_security.record_security_event(
        db,
        user_id=user.id,
        event_type="email_change_completed",
        metadata={"old_email_domain": old_email.split("@")[-1],
                  "new_email_domain": record.new_email.split("@")[-1]},
    )
    db.commit()

    account_security.send_generic_notification_email(
        record.new_email,
        "Your StrikeNova email address was changed",
        "The email address on your StrikeNova account was just changed. "
        "If this was not you, contact support immediately.",
    )
    return {"ok": True, "message": "Email address updated."}
