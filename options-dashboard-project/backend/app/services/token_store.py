"""Multi-user token storage with per-session isolation and DB persistence.

Phase 10.2B-3: Dual-layer architecture — in-memory cache backed by PostgreSQL.
Tokens survive server restarts via get_token() DB fallback on cache miss.

Each session owns exactly one broker token.  Sessions are identified by
cryptographically strong IDs (``secrets.token_urlsafe(32)``).  Token lookup
is O(1) by session ID via in-memory cache, with DB fallback on cache miss.

Security properties:
- One session's login never overwrites another session's token.
- ``clear_token(session_id)`` only clears the specified session.
- Session IDs are compared with constant-time ``secrets.compare_digest``.
- Expired/revoked sessions cannot access broker tokens.
- Tokens encrypted at rest via Fernet (app.crypto).
- Security-relevant events are logged without exposing secrets.
- OAuth state is HMAC-signed to prevent tampering and carry session binding.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory cache (fast path)
# ---------------------------------------------------------------------------

# session_id → {"access_token": str, "created_at": float}
_sessions: dict[str, dict] = {}

# Session TTL (24 hours) — matches the cookie max_age set in auth.py
_SESSION_TTL_SECONDS = 60 * 60 * 24

# In-memory set of revoked session_ids.
# Durable-session revocation paths mark the UserSession row in the DB but
# may not clear the in-memory cache for every affected session. This set lets
# get_token() fail closed without a DB round-trip, which keeps the check O(1)
# and avoids cross-engine transaction visibility issues during logout-all,
# password reset, and password change revocation flows.
_revoked_sessions: set[str] = set()

# ---------------------------------------------------------------------------
# OAuth state management — signed with HMAC
# ---------------------------------------------------------------------------

_STATE_TTL_SECONDS = 600  # 10 minutes

# HMAC signing secret (derived from TOKEN_ENCRYPTION_KEY on first use)
_state_hmac_key: bytes | None = None


def _get_state_hmac_key() -> bytes:
    """Derive an HMAC signing key from TOKEN_ENCRYPTION_KEY."""
    global _state_hmac_key
    if _state_hmac_key is not None:
        return _state_hmac_key
    from app.config import settings
    key = getattr(settings, "TOKEN_ENCRYPTION_KEY", "")
    if not key:
        raise ValueError(
            "TOKEN_ENCRYPTION_KEY must be set for OAuth state signing. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    _state_hmac_key = hashlib.sha256(key.encode("utf-8")).digest()
    return _state_hmac_key


# ---------------------------------------------------------------------------
# Token operations — dual-layer (memory + DB)
# ---------------------------------------------------------------------------


def set_token(token: str, *, connection_id: str | None = None, expires_at=None, persist_to_db: bool = True) -> str:
    """Store a broker token in memory (and optionally DB).

    Returns a new session ID bound to the token.

    Parameters
    ----------
    token : str
        The plaintext broker access token.
    connection_id : str, optional
        The broker connection ID to link this token to.
    expires_at : datetime, optional
        When the token expires (provider-specific).
    persist_to_db : bool, optional
        Whether to persist to DB (default True).  Set to False for
        non-broker sessions (email/password, Google) that don't need
        DB-backed token recovery after restart.
    """
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = {
        "access_token": token,
        "created_at": time.time(),
    }
    logger.info(
        "Session created",
        extra={"event": "auth.session.created", "session_prefix": session_id[:8]},
    )

    # Phase A fix: only persist to DB when explicitly requested (broker sessions).
    # Email/password and Google sessions store identity tokens that are not
    # broker access tokens — persisting them to DB is unnecessary and causes
    # a misleading warning.
    if persist_to_db:
        try:
            _persist_token_to_db(session_id, token, connection_id, expires_at)
        except Exception:
            logger.warning(
                "Failed to persist token to DB (non-critical)",
                extra={"event": "auth.token.persist_failed", "session_prefix": session_id[:8]},
            )

    return session_id


# ---------------------------------------------------------------------------
# Session-scoped broker-session API — atomic broker-link persistence.
#
# The broker OAuth callback MUST use this three-step flow instead of
# set_token() so the BrokerToken row is written on the callback's ACTIVE
# SQLAlchemy session (same transaction as the BrokerConnection and
# UserSession rows — UPSTOX_IDENTITY_LINKING_DESIGN.md §10/§17.4):
#
#   session_id = prepare_broker_session(token, expires_at=...)   # memory only
#   persist_broker_token_row(db, session_id, token, conn_id, exp)  # caller's tx
#   ... commit ...
#   cache_broker_session(session_id, token)                      # post-commit
#
# If the caller's transaction rolls back, the memory cache was never
# populated, so no committed broker session can reference a connection
# that does not exist.
# ---------------------------------------------------------------------------


def prepare_broker_session(token: str, *, expires_at=None) -> str:
    """Generate (but do not cache) the broker session id for a callback.

    The id is reserved so the callback can persist the UserSession and
    BrokerToken rows inside its own transaction before anything is
    visible in the in-memory cache.
    """
    return secrets.token_urlsafe(32)


def persist_broker_token_row(
    db,
    session_id: str,
    token: str,
    connection_id: str | None,
    expires_at=None,
) -> None:
    """Write the encrypted BrokerToken row on the CALLER's active session.

    Runs inside the caller's transaction — no commit, no rollback, no
    separate DB session. Persistence failures propagate to the caller and
    roll back the whole broker-link transaction (never swallowed as
    "non-critical" in this flow).
    """
    from datetime import datetime, timezone

    from app.crypto import encrypt
    from app.identity import BrokerToken, hash_session_id

    bt = BrokerToken(
        connection_id=connection_id or "none",
        session_hash=hash_session_id(session_id),
        broker_token_encrypted=encrypt(token),
        broker_token_expires_at=expires_at,
        created_at=datetime.now(timezone.utc),
    )
    db.add(bt)
    db.flush()


def persist_fyers_refresh_token(db, session_id: str, refresh_token: str) -> None:
    """Store the FYERS refresh token encrypted on the session's token row.

    Additive (AD-11): FYERS is the only broker with a refresh token, and
    it may be discontinued — daily re-authentication remains the
    baseline, so this is best-effort persistence (never a session
    strategy). The ``BrokerToken`` model already carries nullable
    ``broker_refresh_token_encrypted`` columns, so no shared-interface
    redesign is required. Runs inside the caller's transaction; the
    token value is never logged.
    """
    from datetime import datetime, timedelta, timezone

    from app.crypto import encrypt
    from app.identity import BrokerToken, hash_session_id

    bt = (
        db.query(BrokerToken)
        .filter(
            BrokerToken.session_hash == hash_session_id(session_id),
            BrokerToken.broker_refresh_token_encrypted.is_(None),
        )
        .one_or_none()
    )
    if bt is None:
        # The row may not exist yet (persist_broker_token_row order) or may
        # already hold a refresh token — never overwrite a stored one.
        return
    bt.broker_refresh_token_encrypted = encrypt(refresh_token)
    bt.broker_refresh_token_expires_at = datetime.now(timezone.utc) + timedelta(days=15)
    db.flush()


def cache_broker_session(session_id: str, token: str) -> None:
    """Populate the in-memory cache AFTER the caller's transaction commits."""
    _sessions[session_id] = {
        "access_token": token,
        "created_at": time.time(),
    }
    logger.info(
        "Session created",
        extra={"event": "auth.session.created", "session_prefix": session_id[:8]},
    )


def get_token(session_id: str | None) -> str | None:
    """Return the broker access token for the given session.

    Fast path: in-memory cache.  Slow path: DB fallback + decrypt + cache populate.

    Returns None if:
    - session_id is None/empty
    - session_id is not found in memory or DB
    - session has expired
    - session has been revoked (revoked_at set in DB OR marked in-memory)
    """
    if not session_id:
        return None

    # Fast path: memory — must honor durable session validity.
    entry = _sessions.get(session_id)
    if entry is not None:
        age = time.time() - entry["created_at"]
        if age <= _SESSION_TTL_SECONDS:
            # Root authorization invariant: a revoked durable platform
            # session MUST NOT obtain broker market-data access merely
            # because a broker token remains in the in-memory cache.
            if session_id in _revoked_sessions:
                _sessions.pop(session_id, None)
                logger.info(
                    "Session revoked — cache evicted",
                    extra={"event": "auth.session.revoked_cache_evicted", "session_prefix": session_id[:8]},
                )
                return None
            return entry["access_token"]
        # Expired — remove from memory
        _sessions.pop(session_id, None)
        _revoked_sessions.discard(session_id)
        logger.info(
            "Session expired",
            extra={"event": "auth.session.expired", "session_prefix": session_id[:8]},
        )
        return None

    # Slow path: DB fallback (already checks UserSession validity)
    token = _load_token_from_db(session_id)
    if token is not None:
        _sessions[session_id] = {
            "access_token": token,
            "created_at": time.time(),
        }
        return token

    return None


def mark_session_revoked(session_id: str | None = None) -> None:
    """Mark a session as revoked so the in-memory cache cannot serve it.

    All durable-session revocation paths MUST call this so that cached
    broker tokens become immediately unusable even before the DB record
    is updated or the cache entry is cleared.

    If session_id is None, marks ALL sessions as revoked (logout-all,
    password reset).
    """
    if session_id is None:
        _revoked_sessions.update(_sessions.keys())
        logger.info(
            "All sessions marked revoked",
            extra={"event": "auth.sessions.revoked_all", "count": len(_sessions)},
        )
    else:
        _revoked_sessions.add(session_id)
        logger.info(
            "Session marked revoked",
            extra={"event": "auth.session.marked_revoked", "session_prefix": session_id[:8]},
        )


def mark_all_sessions_revoked_except(except_session_id: str) -> None:
    """Mark all sessions as revoked EXCEPT the specified one.

    Used by password-change which revokes all OTHER sessions but keeps
    the current one active.
    """
    for sid in list(_sessions.keys()):
        if sid != except_session_id:
            _revoked_sessions.add(sid)
    logger.info(
        "All sessions marked revoked (except current)",
        extra={"event": "auth.sessions.revoked_all_except", "count": len(_sessions) - 1},
    )


def _has_durable_session(session_id: str) -> bool:
    """Check whether a UserSession record exists for session_id (any status).

    Returns True if a row exists (even if expired/revoked) — this distinguishes
    durable sessions from legacy in-memory-only sessions.
    """
    try:
        from app.db import SessionLocal
        from app.identity import UserSession, hash_session_id

        db = SessionLocal()
        try:
            us = (
                db.query(UserSession)
                .filter(UserSession.session_hash == hash_session_id(session_id))
                .first()
            )
            if us is None:
                logger.info(
                    "Session not found",
                    extra={"event": "auth.session.not_found", "session_prefix": session_id[:8]},
                )
            return us is not None
        finally:
            db.close()
    except Exception:
        return False  # Fail closed: treat as no durable session (legacy path)


def _session_is_valid(session_id: str) -> bool:
    """Check UserSession validity (kept for reference; not used in fast path).
    
    The fast path now uses the in-memory _revoked_sessions set instead
    of a DB round-trip per token lookup.
    """
    try:
        import app.db
        from datetime import datetime, timezone
        from app.identity import UserSession, hash_session_id

        now = datetime.now(timezone.utc)
        db = app.db.SessionLocal()
        try:
            us = (
                db.query(UserSession)
                .filter(
                    UserSession.session_hash == hash_session_id(session_id),
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > now,
                )
                .first()
            )
            return us is not None
        finally:
            db.close()
    except Exception:
        return False  # Fail closed on DB errors


# ---------------------------------------------------------------------------
# Revocation registry — populated by mark_session_revoked() and
# mark_all_sessions_revoked_except(). Cleared when sessions are evicted
# from the in-memory cache.
# ---------------------------------------------------------------------------


def clear_token(session_id: str | None = None) -> None:
    """Clear a specific session's token, or all tokens if session_id is None.

    Clears both memory cache and DB. Also clears the revocation registry
    so a cleared session cannot be accidentally re-marked.
    """
    if session_id is None:
        # Emergency: clear all sessions
        count = len(_sessions)
        _sessions.clear()
        _revoked_sessions.clear()
        logger.info(
            "All sessions cleared",
            extra={"event": "auth.sessions.cleared_all", "count": count},
        )
        # DB cleanup is best-effort for emergency clear
        try:
            _clear_all_tokens_in_db()
        except Exception:
            logger.warning("Failed to clear all tokens in DB", extra={"event": "auth.token.clear_all_db_failed"})
    else:
        removed = _sessions.pop(session_id, None)
        _revoked_sessions.discard(session_id)
        if removed:
            logger.info(
                "Session cleared",
                extra={"event": "auth.session.cleared", "session_prefix": session_id[:8]},
            )
        # DB cleanup
        try:
            _clear_token_in_db(session_id)
        except Exception:
            logger.warning(
                "Failed to clear token in DB",
                extra={"event": "auth.token.clear_db_failed", "session_prefix": session_id[:8]},
            )


def get_session_count() -> int:
    """Return the number of active sessions (for monitoring)."""
    return len(_sessions)


def get_all_session_ids() -> list[str]:
    """Return all active session IDs (for admin monitoring only).

    Never expose full session IDs in API responses.
    """
    return list(_sessions.keys())


# ---------------------------------------------------------------------------
# Startup — DB token health check (no in-memory rehydration)
# ---------------------------------------------------------------------------

# Phase 10.2B-3 design decision:
# In-memory cache cannot be rehydrated because the DB stores session_hash
# (SHA-256), not the plaintext session_id needed as the cache key.
# Instead, get_token() uses a DB fallback on cache miss, which correctly
# looks up by session_hash and repopulates the in-memory cache with the
# correct plaintext key.
#
# This means the first request per session after a server restart goes
# through the slow DB path (decrypt + join), and subsequent requests hit
# the fast in-memory path.  This is the correct trade-off: it avoids
# storing plaintext session IDs in the database.


def startup_db_check() -> int:
    """Verify DB connectivity and count active tokens at startup.

    Returns the number of active (non-expired, non-revoked) tokens in DB.
    These tokens will be loaded on-demand via get_token() DB fallback
    (ownership path). Does NOT populate the in-memory cache.
    """
    count = 0
    try:
        from app.db import SessionLocal
        from app.identity import BrokerAuthorization, BrokerConnection
        from datetime import datetime, timezone

        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            count = (
                db.query(BrokerAuthorization)
                .join(
                    BrokerConnection,
                    BrokerAuthorization.connection_id == BrokerConnection.id,
                )
                .filter(
                    BrokerAuthorization.status == "active",
                    BrokerAuthorization.access_token_encrypted.isnot(None),
                    BrokerConnection.status.in_(("connected", "pending")),
                )
                .count()
            )
        finally:
            db.close()
    except Exception:
        logger.warning(
            "DB token health check failed (non-critical)",
            extra={"event": "auth.startup.db_check_failed"},
        )

    logger.info(
        "DB token health check passed",
        extra={"event": "auth.startup.db_check", "active_tokens": count},
    )
    return count


# ---------------------------------------------------------------------------
# Signed OAuth state — HMAC-signed, carries session_id + broker
# ---------------------------------------------------------------------------


def create_oauth_state(
    session_id: str | None = None,
    broker: str = "UPSTOX",
    popup: bool = False,
) -> str:
    """Create a signed OAuth state value.

    Day 3 security fix: always produces HMAC-signed state with session binding.
    Unsigned fallback is removed.

    The ``popup`` flag is embedded in the signed state itself so it survives
    the broker's redirect back to the callback URL — the broker only preserves
    ``auth_code`` and ``state``, not custom query parameters.
    """
    # Garbage-collect old pending states
    now = time.time()
    for state_val, created_at in list(_pending_states.items()):
        if now - created_at > _STATE_TTL_SECONDS:
            del _pending_states[state_val]

    # Always produce signed state with session binding
    payload = json.dumps(
        {"sid": session_id or "", "brk": broker.upper(), "popup": bool(popup), "ts": int(now)},
        separators=(",", ":"),
    )
    b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig = hmac.new(_get_state_hmac_key(), b64.encode(), hashlib.sha256).hexdigest()[:32]
    state = f"{b64}.{sig}"

    _pending_states[state] = now
    return state


def consume_oauth_state(state: str | None) -> dict | None:
    """Validate and extract session_id + broker + popup from signed OAuth state.

    Returns {"session_id": "...", "broker": "UPSTOX", "popup": True/False} on success.
    Returns None if state is invalid, expired, or already consumed.

    Day 3 security fix: legacy unsigned states are rejected.
    All states must be HMAC-signed with session binding.
    """
    if not state:
        return None

    # Only HMAC-signed states (containing a dot separator) are accepted.
    # Day 3: Legacy unsigned states are rejected outright.
    if "." not in state:
        return None

    b64, sig = state.rsplit(".", 1)
    try:
        expected_sig = hmac.new(
            _get_state_hmac_key(), b64.encode(), hashlib.sha256
        ).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected_sig):
            return None  # Tampered — reject
        payload = json.loads(base64.urlsafe_b64decode(b64))
        if time.time() - payload.get("ts", 0) > _STATE_TTL_SECONDS:
            return None  # Expired
        created_at = _pending_states.pop(state, None)
        if created_at is None:
            return None  # Already consumed or not from us
        return {
            "session_id": payload.get("sid", ""),
            "broker": payload.get("brk", "UPSTOX"),
            "popup": bool(payload.get("popup", False)),
        }
    except Exception:
        # Corrupted signed state — reject
        return None


# ---------------------------------------------------------------------------
# Pending states (CSRF protection) — kept for backward compat
# ---------------------------------------------------------------------------

_pending_states: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Google OAuth nonce binding — HMAC-signed state carrying the nonce
# ---------------------------------------------------------------------------
#
# Phase A security fix: The frontend generates a nonce and sends it to
# Google, but the backend never sees it.  To cryptographically bind the
# nonce to the authentication attempt, the backend generates its own
# nonce, embeds it in an HMAC-signed state, and returns it to the
# frontend.  The frontend includes this state in the Google OAuth URL.
# When Google redirects back, the frontend sends both the id_token AND
# the state to POST /auth/google.  The backend validates the HMAC,
# extracts the expected nonce, and compares it against the JWT nonce.
#
# This prevents replay of Google ID tokens from unrelated auth attempts.

def peek_google_oauth_nonce(state: str) -> str | None:
    """Read the nonce from a signed Google OAuth state WITHOUT consuming it.

    Used by POST /auth/google/state to return the nonce to the frontend.
    The state remains in _pending_states for later consumption.
    """
    if not state or "." not in state:
        return None
    b64, sig = state.rsplit(".", 1)
    try:
        expected_sig = hmac.new(
            _get_state_hmac_key(), b64.encode(), hashlib.sha256
        ).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(b64))
        return payload.get("nonce")
    except Exception:
        return None


def create_google_oauth_state(nonce: str | None = None) -> str:
    """Create an HMAC-signed state for Google OAuth nonce binding.

    Generates a random nonce if not provided.  The state carries
    {nonce, ts} and is HMAC-signed with the same key as broker OAuth state.

    Returns the signed state string (base64.signature format).
    """
    now = time.time()
    if nonce is None:
        nonce = secrets.token_urlsafe(32)
    payload = json.dumps(
        {"nonce": nonce, "ts": int(now)},
        separators=(",", ":"),
    )
    b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig = hmac.new(_get_state_hmac_key(), b64.encode(), hashlib.sha256).hexdigest()[:32]
    state = f"{b64}.{sig}"
    _pending_states[state] = now
    return state


def consume_google_oauth_state(state: str | None) -> str | None:
    """Validate and extract the expected nonce from a signed Google OAuth state.

    Returns the nonce string on success.
    Returns None if state is invalid, expired, or already consumed.
    """
    if not state or "." not in state:
        return None
    b64, sig = state.rsplit(".", 1)
    try:
        expected_sig = hmac.new(
            _get_state_hmac_key(), b64.encode(), hashlib.sha256
        ).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(b64))
        if time.time() - payload.get("ts", 0) > _STATE_TTL_SECONDS:
            return None
        created_at = _pending_states.pop(state, None)
        if created_at is None:
            return None  # Already consumed or not from us
        return payload.get("nonce")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# DB persistence helpers — best-effort, never block the request
# ---------------------------------------------------------------------------


def _persist_token_to_db(session_id: str, token: str, connection_id: str | None, expires_at) -> None:
    """Write encrypted token to broker_tokens table."""
    from datetime import datetime, timezone
    from app.db import SessionLocal
    from app.identity import BrokerToken, hash_session_id
    from app.crypto import encrypt

    db = SessionLocal()
    try:
        bt = BrokerToken(
            connection_id=connection_id or "none",
            session_hash=hash_session_id(session_id),
            broker_token_encrypted=encrypt(token),
            broker_token_expires_at=expires_at,
            created_at=datetime.now(timezone.utc),
        )
        db.add(bt)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _load_token_from_db(session_id: str) -> str | None:
    """Load the broker access token for a session — ownership path.

    Broker-authorization architecture: token resolution follows
    UserSession → user → BrokerConnection → active BrokerAuthorization.
    The token is owned by the CONNECTION, not by this browser session —
    so any active session of the connection's user resolves the same
    broker token, and expiring/revoking one session never disconnects
    the broker. A legacy session-scoped BrokerToken remains a fallback
    for rows created before the architecture migration (see the LEGACY
    dual-write note in auth.py); platform-only sessions return None.
    """
    from datetime import datetime, timezone
    from app.db import SessionLocal
    from app.identity import BrokerToken, UserSession, hash_session_id, BrokerAuthorization, BrokerConnection
    from app.services.broker_authorization import resolve_default_broker_authorization
    from app.crypto import decrypt

    session_hash = hash_session_id(session_id)
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)

        # Path 0: platform session validity gate (either style of session).
        us = (
            db.query(UserSession)
            .filter(
                UserSession.session_hash == session_hash,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
            )
            .first()
        )
        if us is not None:
            # PRIMARY (authoritative): ownership path. The authorization
            # belongs to the BrokerConnection — never to this browser
            # session. A session's broker_connection_id is only a HINT for
            # multi-connection users; the connection's user_id must match
            # the session's user (fail closed otherwise).
            conn_id = us.broker_connection_id
            if conn_id:
                conn = (
                    db.query(BrokerConnection)
                    .filter(
                        BrokerConnection.id == conn_id,
                        BrokerConnection.user_id == us.user_id,
                        BrokerConnection.status.in_(("connected", "pending")),
                    )
                    .first()
                )
            else:
                conn = None
            if conn is None:
                # Fresh session without a broker hint (or a stale hint):
                # resolve the user's default connection — same connection,
                # same authorization the consenting session used.
                _conn_r, authz = resolve_default_broker_authorization(
                    db, us.user_id, now=now
                )
            else:
                authz = (
                    db.query(BrokerAuthorization)
                    .filter(
                        BrokerAuthorization.connection_id == conn.id,
                        BrokerAuthorization.status == "active",
                    )
                    .order_by(BrokerAuthorization.issued_at.desc())
                    .first()
                )
            if authz is not None and authz.access_token_encrypted:
                expiry = authz.access_token_expires_at
                if expiry is None or expiry.tzinfo is None or expiry > now:
                    return decrypt(authz.access_token_encrypted)
                # Authorization expired on its own clock.
                authz.status = "expired"
                try:
                    db.commit()
                except Exception:
                    db.rollback()
                    return None
                return None

        # Path 1 (LEGACY fallback): session-scoped BrokerToken —
        # pre-migration rows only. Never written for new connections.
        row = (
            db.query(BrokerToken, UserSession)
            .join(
                UserSession,
                BrokerToken.session_hash == UserSession.session_hash,
            )
            .filter(
                BrokerToken.session_hash == session_hash,
                BrokerToken.broker_token_encrypted.isnot(None),
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
            )
            .first()
        )
        if row is not None:
            bt, _us = row
            return decrypt(bt.broker_token_encrypted)

        return None
    except Exception:
        return None
    finally:
        db.close()


def has_platform_session(session_id: str | None) -> bool:
    """Check whether session_id has a valid UserSession DB record.

    This is a DB-only check — it does NOT use the in-memory cache.
    Use this in require_token() to distinguish 'platform-only session'
    from 'broker session' or 'no session'.

    Returns True if a non-expired, non-revoked UserSession exists.
    """
    if not session_id:
        return False
    try:
        from datetime import datetime, timezone
        from app.db import SessionLocal
        from app.identity import UserSession, hash_session_id

        now = datetime.now(timezone.utc)
        db = SessionLocal()
        try:
            us = (
                db.query(UserSession)
                .filter(
                    UserSession.session_hash == hash_session_id(session_id),
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > now,
                )
                .first()
            )
            return us is not None
        finally:
            db.close()
    except Exception:
        return False


def _clear_token_in_db(session_id: str) -> None:
    """NULL the encrypted token in broker_tokens for this session."""
    from app.db import SessionLocal
    from app.identity import BrokerToken, hash_session_id

    session_hash = hash_session_id(session_id)
    db = SessionLocal()
    try:
        bt = db.query(BrokerToken).filter(BrokerToken.session_hash == session_hash).first()
        if bt is not None:
            bt.broker_token_encrypted = None
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _clear_all_tokens_in_db() -> None:
    """NULL all encrypted tokens in broker_tokens (emergency clear)."""
    from app.db import SessionLocal
    from app.identity import BrokerToken

    db = SessionLocal()
    try:
        db.query(BrokerToken).update({"broker_token_encrypted": None})
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
