"""Phase 10 identity foundation.

This module deliberately sits beside the existing auth/session implementation
while the application migrates from broker-coupled identity to a durable
StrikeNova account. It owns only identity metadata and session ownership;
broker tokens remain in the existing token store.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json as _json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, event
from sqlalchemy import false as sa_false
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.db import Base


SESSION_TTL = timedelta(hours=24)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256 with a random salt.

    Returns a string in the format ``iterations$salt$digest``.
    """
    iterations = 480_000
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a stored PBKDF2 hash.

    Returns True if the password matches, False otherwise.
    """
    try:
        parts = stored_hash.split("$")
        if len(parts) != 3:
            return False
        iterations, salt_hex, expected_hex = int(parts[0]), parts[1], parts[2]
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return _hmac.compare_digest(dk.hex(), expected_hex)
    except Exception:
        return False


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    identity_source: Mapped[str] = mapped_column(String(32), default="upstox")
    # LEGACY COMPATIBILITY METADATA (multi-broker refactor): the stamp of
    # the FIRST broker identity linked to this user. Never an ownership or
    # authorization gate — BrokerConnection is the authoritative broker
    # ownership ledger (one user -> many connections). Never overwritten
    # once populated (see ensure_broker_stamp).
    broker_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    broker_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    google_sub: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # Day 45 admin principal: EXPLICIT platform-admin flag, set only through
    # the durable DB column (Founder/ops bootstrap — no API, OAuth flow, or
    # broker linkage can grant it). Admin authority is never inferred from
    # tenant ownership (a user's own BrokerConnection/authorization is not
    # an admin credential). Default False for every existing user.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=sa_false(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # NO schema-level uniqueness on the legacy stamp columns: under the
        # multi-broker architecture the stamp is compatibility metadata
        # that may legitimately duplicate across users (stale stamps are
        # informational junk), and any uniqueness here would resurrect the
        # one-user-one-broker gate at the DB level (Test G). The historical
        # table-level constraint uq_users_broker_identity was removed by
        # migration e5f6a7b8c9d0 on PostgreSQL/CockroachDB. The
        # authoritative broker-identity uniqueness lives on
        # broker_connections (migration d9e0f1a2b3c4), NOT here.
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    session_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    broker_connection_id: Mapped[str | None] = mapped_column(
        ForeignKey("broker_connections.id"), nullable=True
    )


# ---------------------------------------------------------------------------
# Account-security lifecycle records (2026-09-16 design spec §5).
#
# Opaque one-time tokens are stored ONLY as SHA-256 digests; raw token
# material never reaches the database, logs, or API responses.
# SecurityEvent rows are append-only (see the before_update guard below):
# they carry safe metadata only — never passwords, tokens, reset URLs,
# OAuth codes or broker secrets.
# ---------------------------------------------------------------------------


class JSONText(TypeDecorator):
    """JSON-serialized Text storage (SQLite/PostgreSQL/CockroachDB portable)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return _json.dumps(value or {})

    def process_result_value(self, value, dialect):
        return _json.loads(value) if value else {}


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class PendingEmailChange(Base):
    """A requested (not yet verified) email change. The current
    ``users.email`` remains authoritative until the change token is consumed."""

    __tablename__ = "pending_email_changes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    new_email: Mapped[str] = mapped_column(String(320))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class SecurityEvent(Base):
    """Durable, append-only security audit record.

    ``user_id`` is nullable for anonymous events (e.g. failed login for an
    unknown email). It is deliberately NOT a ForeignKey: audit records must
    survive any later account deletion untouched. ``metadata_json`` carries
    only sanitized, secret-free metadata (see account_security.py).
    """

    __tablename__ = "security_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    metadata_json: Mapped[dict] = mapped_column(JSONText, default=dict)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"SecurityEvent(id={self.id!r}, event_type={self.event_type!r}, "
            f"occurred_at={self.occurred_at!r})"
        )

    def __str__(self) -> str:  # pragma: no cover - trivial
        return repr(self)


@event.listens_for(SecurityEvent, "before_update")
def _security_events_are_immutable(mapper, connection, target):
    """Fail closed: security-event rows are never updated in place."""
    raise RuntimeError(
        "security_events rows are append-only; update attempts are rejected"
    )


class AdminControl(Base):
    """Day 45 — admin-owned platform control (instrument/configuration/
    retention/feature-flag) with versioning.

    One row per (domain, key). Each admin write bumps ``version`` and
    records who changed it; ``history`` is the append-only change ledger
    (entries are sanitized before storage — no secret material is ever
    accepted). Domains are a closed set validated by the service layer.
    Ordinary users have no read or write path to these rows.
    """

    __tablename__ = "admin_controls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    domain: Mapped[str] = mapped_column(String(32), index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[dict] = mapped_column(JSONText, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # Append-only change ledger: [{version, value, by, at}]
    history: Mapped[list] = mapped_column(JSONText, default=list)

    __table_args__ = (
        UniqueConstraint("domain", "key", name="uq_admin_controls_domain_key"),
    )


class AdminAuditEvent(Base):
    """Day 45 — durable audit record for material admin actions.

    Append-only (same immutability posture as SecurityEvent). Stores actor
    (users.id), action, structured target, result, and time. NEVER stores
    credential material: values are sanitized through the same redaction
    rules as the secret-free SecurityEvent metadata (keys that look like
    tokens/secrets are dropped, string values shaped like credentials are
    replaced with a placeholder) — see app/services/admin_audit.py.
    """

    __tablename__ = "admin_audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[dict] = mapped_column(JSONText, default=dict)
    result: Mapped[str] = mapped_column(String(16), default="success", index=True)
    detail: Mapped[dict] = mapped_column(JSONText, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"AdminAuditEvent(id={self.id!r}, action={self.action!r}, "
            f"result={self.result!r}, occurred_at={self.occurred_at!r})"
        )

    def __str__(self) -> str:  # pragma: no cover - trivial
        return repr(self)


@event.listens_for(AdminAuditEvent, "before_update")
def _admin_audit_events_are_immutable(mapper, connection, target):
    """Fail closed: admin-audit rows are never updated in place."""
    raise RuntimeError(
        "admin_audit_events rows are append-only; update attempts are rejected"
    )


class NotificationEvent(Base):
    """Day 46 (Issue #92) — durable, backend-authoritative notification.

    One row per notification event: type, severity, source domain,
    human-readable summary, sanitized structured details, tenant/user
    scope (None = platform-operational), correlation/operation ID and a
    deduplication identity. Payloads pass the shared sanitizer BEFORE
    persistence, so no broker credential, Analytics Token, session ID,
    or cookie value can ever reach this table, a channel, or a reader.
    User-scoped rows are readable only within their scope (tenant
    isolation is enforced at the service/router boundary); platform-
    scoped rows are admin/operational-surface material only.
    """

    __tablename__ = "notification_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info", index=True)
    source: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONText, default=dict)
    user_scope: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    dedup_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"NotificationEvent(id={self.id!r}, event_type={self.event_type!r}, "
            f"severity={self.severity!r}, occurred_at={self.occurred_at!r})"
        )

    def __str__(self) -> str:  # pragma: no cover - trivial
        return repr(self)


class BrokerConnection(Base):
    """Persistent broker connection owned by a StrikeNova user. (AD-4)

    Stores the user's per-user broker credentials (encrypted) and
    connection metadata. Each row represents one broker account
    linked to one StrikeNova user. (AD-2, AD-5)

    Three independent capabilities (Phase 10.2B-6):
      1. Authentication — OAuth identity, profile, funds
      2. Market Data — option chain, quotes, Greeks, GEX, historical
      3. Trading — order placement, modification, cancellation

    Status lifecycle:
      pending  → connected → expired | disconnected
      pending:  credentials stored via POST /auth/connect, no OAuth yet
      connected: first OAuth completed, broker_account_id populated

    broker_account_id lifecycle:
      1. Initially "pending" when credentials are stored (POST /auth/connect)
         before the first OAuth completes.
      2. After first successful OAuth, updated to the broker's account ID
         (e.g. Upstox user_id, FYERS app_id).  Immutable thereafter.
      3. Required (NOT NULL) — "pending" is a sentinel for pre-OAuth rows.
      4. Part of unique constraint (user_id, broker, broker_account_id).
         "pending" allows one pre-OAuth row per (user, broker).

    is_default invariant:
      At most one connection per (user_id, broker) may have is_default=True.
      Enforced via partial unique index uq_one_default_per_user_broker.
    """

    __tablename__ = "broker_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    broker: Mapped[str] = mapped_column(String(32), index=True)
    broker_account_id: Mapped[str] = mapped_column(String(128))
    display_label: Mapped[str | None] = mapped_column(String(160), nullable=True)
    is_default: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(20), default="connected")
    capability_mode: Mapped[str] = mapped_column(String(20), default="trading")  # DEPRECATED: use data_status + trading_status

    # Phase 10.2B-6: Independent capability status
    data_status: Mapped[str] = mapped_column(String(20), default="inactive")  # "inactive" | "active" | "expired"
    data_source: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "analytics_token" | "oauth_token"
    trading_status: Mapped[str] = mapped_column(String(20), default="inactive")  # "inactive" | "active" | "expired"
    trading_static_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)  # per-user static IP for trading

    # Per-user broker credentials (encrypted — AD-2, AD-3)
    broker_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_api_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_analytics_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_redirect_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_static_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    # Provider-specific metadata
    app_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_metadata_json: Mapped[str] = mapped_column(Text, default="{}")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    connected_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    authorizations: Mapped[list["BrokerAuthorization"]] = relationship(
        back_populates="connection",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "broker", "broker_account_id", name="uq_broker_connection"),
        # Partial unique index: at most one default connection per (user, broker).
        # Enforced ONLY at the schema level via Alembic migration
        # (125e1807df8d).  NOT declared here because cross-dialect partial
        # indexes (PostgreSQL WHERE vs SQLite WHERE) cannot be expressed
        # portably in SQLAlchemy ORM metadata — create_all() would create
        # a plain unique index on (user_id, broker) in SQLite, blocking
        # legitimate multi-connection rows.
    )


class BrokerToken(Base):
    """Session-scoped broker token. (§5.2) — LEGACY, superseded.

    One row per (connection, session) pair. Tokens are encrypted at rest.

    DEPRECATED by the BrokerAuthorization architecture: the authoritative
    authorization source is now :class:`BrokerAuthorization`, which belongs
    to the BrokerConnection and survives session expiry. The (connection,
    session) row shape made every broker token hostage to one browser
    session. Callers must use ``app.services.broker_authorization``;
    existing rows are carried forward by migration (never destroyed).
    """

    __tablename__ = "broker_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[str] = mapped_column(
        ForeignKey("broker_connections.id", ondelete="CASCADE"), index=True
    )
    session_hash: Mapped[str] = mapped_column(String(64), index=True)
    broker_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    broker_refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("connection_id", "session_hash", name="uq_broker_token_per_session"),
    )


class BrokerAuthorization(Base):
    """Current API authorization for a BrokerConnection — NOT session-owned.

    One BrokerConnection has at most one active authorization at a time
    (the broker's OAuth token state is singular). Each successful OAuth
    callback inserts a NEW row and revokes the previous active one, so
    the history of authorizations is preserved and the lifecycle of the
    token material is independent of any StrikeNova browser session.

    All token values are encrypted at rest (app.crypto). Token material
    is NEVER exposed through repr(), str(), API responses, diagnostics,
    or logging — see public_status() and the masked column reprs.
    """

    __tablename__ = "broker_authorizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    connection_id: Mapped[str] = mapped_column(
        ForeignKey("broker_connections.id", ondelete="CASCADE"), index=True
    )

    # Encrypted token material (never logged, never serialized).
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Lifecycle: active | expired | revoked | superseded
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    # How this authorization was obtained: oauth_callback | migration | refresh
    method: Mapped[str] = mapped_column(String(32), default="oauth_callback")

    issued_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    connection: Mapped["BrokerConnection"] = relationship(back_populates="authorizations")

    # Column-level repr masking: the explicit __repr__ lists ONLY safe
    # metadata fields — token columns can never leak through repr()/str().

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"BrokerAuthorization(id={self.id!r}, "
            f"connection_id={self.connection_id!r}, status={self.status!r}, "
            f"method={self.method!r}, issued_at={self.issued_at!r})"
        )

    def __str__(self) -> str:  # pragma: no cover - trivial
        return repr(self)

    def access_token_plain(self) -> str | None:
        """Decrypt and return the access token (in-process use only)."""
        if self.access_token_encrypted is None:
            return None
        from app.crypto import decrypt

        return decrypt(self.access_token_encrypted)

    def refresh_token_plain(self) -> str | None:
        """Decrypt and return the refresh token (in-process use only)."""
        if self.refresh_token_encrypted is None:
            return None
        from app.crypto import decrypt

        return decrypt(self.refresh_token_encrypted)

    def public_status(self) -> dict:
        """Safe view for API responses/diagnostics — metadata only, no secrets."""
        return {
            "id": self.id,
            "connection_id": self.connection_id,
            "status": self.status,
            "method": self.method,
            "has_access_token": self.access_token_encrypted is not None,
            "has_refresh_token": self.refresh_token_encrypted is not None,
            "access_token_expires_at": self.access_token_expires_at,
            "refresh_token_expires_at": self.refresh_token_expires_at,
            "issued_at": self.issued_at,
            "last_refreshed_at": self.last_refreshed_at,
            "last_used_at": self.last_used_at,
        }


def hash_session_id(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


class BrokerIdentityInUse(Exception):
    """A broker identity is already owned by a different StrikeNova user.

    Ownership is never transferred (UPSTOX_IDENTITY_LINKING_DESIGN.md
    Invariant 7 / §17.5); callers must reject the connection attempt.
    """


def resolve_platform_user(db: Session, user_id: str) -> User:
    """Resolve the authenticated platform user for a broker-link flow.

    Session-bound linking (design §7 / §17): the initiating session's
    user_id is the ONLY platform-identity authority. This function NEVER
    creates a User and NEVER consults broker profile data (email, display
    name, or otherwise) for identity decisions.
    """
    user = db.query(User).filter(User.id == user_id).one_or_none()
    if user is None:
        raise ValueError(f"No StrikeNova user for session user_id={user_id}")
    return user


def find_broker_identity_owner(
    db: Session, provider: str, broker_user_id: str, broker_account_id: str
) -> str | None:
    """Return the user_id owning the broker identity, if anyone does.

    ``BrokerConnection`` is the AUTHORITATIVE broker-ownership ledger
    (multi-broker architecture): a live ``(broker, broker_account_id)``
    row with a non-sentinel account id determines current ownership.

    The legacy ``users.broker_*`` stamp is compatibility metadata only.
    It never confers ownership by itself (a stale stamp without a
    matching live connection means "not owned"), and it never blocks a
    legitimate additional broker/account connection for the stamped
    user. The stamp IS still consulted for corruption detection: when a
    different user's stamp collides with the ledger's live owner, the
    disagreement raises :class:`BrokerIdentityInUse` (corrupt state is
    never silently resolved — fail closed).
    """
    owner: str | None = None
    conn = (
        db.query(BrokerConnection)
        .filter(
            BrokerConnection.broker == provider,
            BrokerConnection.broker_account_id == broker_account_id,
        )
        .one_or_none()
    )
    if conn is not None:
        owner = conn.user_id
    stamped = (
        db.query(User)
        .filter(User.broker_provider == provider, User.broker_user_id == broker_user_id)
        .one_or_none()
    )
    if stamped is not None:
        if owner is not None and stamped.id != owner:
            raise BrokerIdentityInUse(
                f"broker identity {provider}/{broker_user_id} has conflicting "
                f"ownership records: connection owner {owner} vs stamped user {stamped.id}"
            )
        # A stamp agreeing with the ledger is consistent but adds no
        # authority; a stamp WITHOUT a live ledger row does not create
        # ownership (legacy metadata is not the ownership ledger).
    return owner


def ensure_broker_stamp(
    db: Session, user: User, provider: str, broker_user_id: str
) -> None:
    """Maintain the legacy ``users.broker_*`` compatibility stamp.

    Multi-broker semantics (BrokerConnection is the authoritative
    ownership ledger; the stamp is compatibility metadata only):

    Allowed:  NULL → (provider, broker_user_id); identical → no-op;
              a DIFFERENT existing stamp → no-op (never overwrite
              existing legacy metadata with a second broker identity,
              never block the legitimate additional connection).
    Forbidden: nothing — this function can no longer reject a
              connection. Ownership authorization lives entirely in
              the ledger check (``find_broker_identity_owner`` /
              the global uniqueness index); corrupt-state detection
              lives in :func:`find_broker_identity_owner`.
    """
    if user.broker_provider is None and user.broker_user_id is None:
        user.broker_provider = provider
        user.broker_user_id = broker_user_id
        db.flush()
        return
    # An existing stamp — identical or different — is left untouched.
    # The legacy (broker_provider, broker_user_id) unique constraint on
    # users is per-user metadata, not an authorization gate.


def get_or_create_user_from_upstox(db: Session, profile: dict) -> User:
    """DEPRECATED legacy helper — lookup-only since session-bound linking.

    The historical create-path here (INSERT a platform User from the
    Upstox profile, including its email) caused duplicate-platform-user
    forks and ``users.email`` UniqueViolations; it is retired by the
    authorized identity-linking design (§7/§17.1: no anonymous Upstox
    sign-in, no broker-coupled user creation). The lookup-only remnant
    exists solely for the legacy Upstox-only migration workstream and
    tests. The live OAuth callback must NOT call this function; it uses
    :func:`resolve_platform_user` with the bound session's user_id.
    """
    data = profile.get("data") if isinstance(profile, dict) else None
    data = data if isinstance(data, dict) else {}

    broker_user_id = str(data.get("user_id") or "").strip()
    if not broker_user_id:
        raise ValueError("Upstox profile did not contain a broker user_id")

    provider = str(data.get("broker") or "UPSTOX").strip().upper()
    display_name = str(data.get("user_name") or "").strip() or None
    broker_active = bool(data.get("is_active", True))

    user = (
        db.query(User)
        .filter(User.broker_provider == provider, User.broker_user_id == broker_user_id)
        .one_or_none()
    )
    if user is None:
        raise LookupError(
            f"No StrikeNova user for broker identity {provider}/{broker_user_id}; "
            "broker OAuth no longer creates platform users"
        )

    if display_name:
        user.display_name = display_name or user.display_name
    # Do not let broker activity silently undo a future StrikeNova admin
    # suspension/disable action. Only an active account may be refreshed
    # by broker activity; disabled/suspended are platform-owned states.
    if user.status == "active" and not broker_active:
        user.status = "suspended"
    user.last_login_at = _utcnow()

    db.flush()
    return user


def create_session_record(
    db: Session,
    user_id: str,
    session_id: str,
    broker_connection_id: str | None = None,
) -> UserSession:
    """Create a durable session record linking session → user.

    broker_connection_id links the session to the specific broker
    connection used for authentication (nullable for backward compat).
    """
    now = _utcnow()
    record = UserSession(
        user_id=user_id,
        session_hash=hash_session_id(session_id),
        broker_connection_id=broker_connection_id,
        created_at=now,
        expires_at=now + SESSION_TTL,
    )
    db.add(record)
    db.flush()
    db.refresh(record)
    return record


def revoke_session(db: Session, session_id: str) -> bool:
    record = (
        db.query(UserSession)
        .filter(UserSession.session_hash == hash_session_id(session_id), UserSession.revoked_at.is_(None))
        .one_or_none()
    )
    if record is None:
        return False
    record.revoked_at = _utcnow()
    db.flush()
    return True


def get_active_session(db: Session, session_id: str | None) -> UserSession | None:
    if not session_id:
        return None
    now = _utcnow()
    return (
        db.query(UserSession)
        .filter(
            UserSession.session_hash == hash_session_id(session_id),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
        .one_or_none()
    )


# ---------------------------------------------------------------------------
# Platform session / broker token resolution
# ---------------------------------------------------------------------------


def resolve_platform_session(session_id: str | None) -> str | None:
    """Resolve session_id → user_id for a valid platform session.

    Returns the user_id if the session exists, is not expired, and is not
    revoked.  Returns None otherwise.

    This is the canonical platform-identity resolver — it NEVER returns a
    broker token.  Use resolve_broker_token_by_session_hash() for broker
    authorization.
    """
    if not session_id:
        return None
    try:
        from app.db import SessionLocal

        now = _utcnow()
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
            return us.user_id if us is not None else None
        finally:
            db.close()
    except Exception:
        return None


def resolve_broker_token_by_session_hash(session_hash: str | None) -> str | None:
    """Resolve session_hash → decrypted broker access token.

    Queries BrokerToken joined with UserSession by session_hash.
    Returns the decrypted broker token if:
      - BrokerToken exists with non-null encrypted token
      - UserSession is not expired and not revoked
    Returns None otherwise.

    This avoids the double-hashing bug of passing session_hash to
    get_token() which expects plaintext session_id.
    """
    if not session_hash:
        return None
    try:
        from app.db import SessionLocal
        from app.crypto import decrypt

        now = _utcnow()
        db = SessionLocal()
        try:
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
        finally:
            db.close()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Phase 10.2B-2 — BYOB Credential Management
# ---------------------------------------------------------------------------


def resolve_user_credentials(
    user_id: str, broker: str, db: Session
) -> dict:
    """Resolve a user's encrypted broker credentials from broker_connections.

    Selects the default connection (or most recent) for the given
    (user_id, broker) where credentials are available.

    Returns a dict suitable for passing to the adapter constructor:
      {"api_key": "...", "api_secret": "...", "redirect_uri": "..."}

    Raises ValueError if no credential-bearing connection exists.

    Security: this function NEVER returns platform-level credentials.
    It only returns per-user encrypted values from broker_connections.
    """
    from app.crypto import decrypt

    conn = (
        db.query(BrokerConnection)
        .filter(
            BrokerConnection.user_id == user_id,
            BrokerConnection.broker == broker.upper(),
            BrokerConnection.broker_api_key_encrypted.isnot(None),
        )
        .order_by(
            BrokerConnection.is_default.desc(),
            BrokerConnection.created_at.desc(),
        )
        .first()
    )
    if conn is None:
        raise ValueError(
            f"No {broker} credentials found for user {user_id}. "
            f"Use POST /auth/connect to store your broker credentials first."
        )

    credentials = {}
    api_key = decrypt(conn.broker_api_key_encrypted)
    if not api_key:
        raise ValueError(
            f"Stored API key for {broker} is empty after decryption"
        )
    credentials["api_key"] = api_key

    if conn.broker_api_secret_encrypted:
        api_secret = decrypt(conn.broker_api_secret_encrypted)
        if api_secret:
            credentials["api_secret"] = api_secret

    if conn.broker_redirect_uri:
        credentials["redirect_uri"] = conn.broker_redirect_uri

    return credentials


def store_credentials(
    db: Session,
    user_id: str,
    broker: str,
    api_key: str,
    api_secret: str,
    *,
    redirect_uri: str | None = None,
    display_label: str | None = None,
) -> BrokerConnection:
    """Encrypt and store a user's broker Developer App credentials.

    Creates or updates a BrokerConnection row.  New connections are created
    with broker_account_id="pending" and status="pending" — the real
    broker_account_id is populated after the first successful OAuth.

    Returns the BrokerConnection row.
    """
    from app.crypto import encrypt

    broker_upper = broker.upper()

    # Check for existing pending OR data-only connection for this (user, broker).
    # A data-only connection (created by store_analytics_token) can be upgraded
    # to hold broker credentials without creating a duplicate row.
    conn = (
        db.query(BrokerConnection)
        .filter(
            BrokerConnection.user_id == user_id,
            BrokerConnection.broker == broker_upper,
            BrokerConnection.broker_account_id.in_(["pending", "data-only"]),
        )
        .first()
    )

    if conn is None:
        conn = BrokerConnection(
            id=str(uuid4()),
            user_id=user_id,
            broker=broker_upper,
            broker_account_id="pending",
            status="pending",
            display_label=display_label,
            connected_at=_utcnow(),
        )
        # is_default defaults to True via ORM metadata — this is correct for
        # the first connection per (user, broker).  The partial unique index
        # uq_one_default_per_user_broker enforces at most one default per
        # (user, broker) at the schema level.
        db.add(conn)

    conn.broker_api_key_encrypted = encrypt(api_key)
    conn.broker_api_secret_encrypted = encrypt(api_secret)
    if redirect_uri:
        conn.broker_redirect_uri = redirect_uri
    if display_label:
        conn.display_label = display_label

    conn.updated_at = _utcnow()
    db.flush()
    return conn


def get_or_create_connection(
    db: Session,
    user_id: str,
    broker: str,
    broker_account_id: str,
    *,
    status: str = "connected",
) -> BrokerConnection:
    """Create or update a BrokerConnection after successful OAuth.

    Called after OAuth callback to:
      1. Replace broker_account_id="pending" with the real account ID
      2. Set status="connected"
      3. Update connected_at timestamp

    If a connection with the real broker_account_id already exists,
    it is updated (re-login scenario).

    broker_account_id must be pre-extracted by the adapter layer (AD-6).

    Concurrency (design §11/§17.3): concurrent callbacks for the same
    identity can both miss the SELECT. When that happens the INSERT/UPDATE
    flush raises ``IntegrityError`` (global partial ownership index or the
    per-user connection constraint) and this helper propagates it —
    flush-failure deactivates the SQLAlchemy session, so swallowing the
    error and re-reading in-session would raise ``PendingRollbackError``
    instead (proven live on PostgreSQL), which the API layer cannot
    classify. The API-layer caller (``routers/auth.py``) owns the full
    recover protocol on ``IntegrityError``: ROLLBACK → re-read the
    committed owner in a fresh transaction → classify (other-user winner
    → ``broker_identity_in_use``; same-user winner → deterministic
    idempotent retry of the whole link transaction). The database's
    global ownership index remains the arbiter of cross-user races.
    """
    broker_upper = broker.upper()

    def _apply(conn: BrokerConnection) -> BrokerConnection:
        conn.broker_account_id = broker_account_id
        conn.status = status
        conn.disconnected_at = None
        conn.connected_at = _utcnow()
        conn.updated_at = _utcnow()
        return conn

    # First: check if a pending row exists for this (user, broker)
    pending_conn = (
        db.query(BrokerConnection)
        .filter(
            BrokerConnection.user_id == user_id,
            BrokerConnection.broker == broker_upper,
            BrokerConnection.broker_account_id == "pending",
        )
        .first()
    )

    # Second: check if a connected row with this account ID exists
    existing_conn = (
        db.query(BrokerConnection)
        .filter(
            BrokerConnection.user_id == user_id,
            BrokerConnection.broker == broker_upper,
            BrokerConnection.broker_account_id == broker_account_id,
        )
        .first()
    )

    if existing_conn is not None:
        # Re-login to existing connection — an UPDATE of our own row
        # cannot lose an ownership race.
        return _apply(existing_conn)

    if pending_conn is not None:
        # Transition from pending → connected. A cross-user winner that
        # committed the identity between our SELECT and this flush makes
        # the UPDATE enter the global ownership index → IntegrityError
        # propagates for caller classification (docstring above).
        conn = _apply(pending_conn)
        db.flush()
        return conn

    # New connection (e.g. first OAuth without prior credential storage).
    # A cross-user or same-user concurrent winner that committed between
    # our SELECT and this flush makes the INSERT hit the global ownership
    # index (or the per-user constraint) → IntegrityError propagates for
    # caller classification (docstring above).
    conn = BrokerConnection(
        id=str(uuid4()),
        user_id=user_id,
        broker=broker_upper,
        broker_account_id=broker_account_id,
        connected_at=_utcnow(),
    )
    db.add(conn)
    db.flush()
    return conn


# ---------------------------------------------------------------------------
# Google OAuth identity (Phase A)
# ---------------------------------------------------------------------------


def get_or_create_user_from_google(
    db: Session,
    google_sub: str,
    email: str | None,
    display_name: str | None,
) -> User:
    """Map a Google-authenticated identity to a durable StrikeNova user.

    Account linking rules:
    1. If a user with this google_sub exists → update and return.
    2. If a user with this email exists (email/password or Upstox) → link Google.
    3. Otherwise → create a new user.

    This prevents duplicate accounts when the same person uses multiple
    sign-in methods.
    """
    email = (email or "").strip().lower() or None
    display_name = (display_name or "").strip() or None

    # 1. Existing Google user
    existing = (
        db.query(User)
        .filter(User.google_sub == google_sub)
        .one_or_none()
    )
    if existing is not None:
        existing.email = email or existing.email
        existing.display_name = display_name or existing.display_name
        existing.last_login_at = _utcnow()
        db.flush()
        return existing

    # 2. Existing user with same email — link Google to existing account
    if email:
        existing = (
            db.query(User)
            .filter(User.email == email)
            .one_or_none()
        )
        if existing is not None:
            existing.google_sub = google_sub
            existing.display_name = display_name or existing.display_name
            existing.last_login_at = _utcnow()
            # Update identity_source to reflect multi-provider
            if existing.identity_source == "email":
                existing.identity_source = "google"
            db.flush()
            return existing

    # 3. New user
    user = User(
        id=str(uuid4()),
        email=email,
        google_sub=google_sub,
        display_name=display_name,
        status="active",
        identity_source="google",
        last_login_at=_utcnow(),
    )
    db.add(user)
    db.flush()
    db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Analytics Token management (Phase 10.2B-4)
# ---------------------------------------------------------------------------


def store_analytics_token(
    db: Session,
    user_id: str,
    broker: str,
    analytics_token: str,
) -> BrokerConnection:
    """Store an encrypted Analytics Token on the user's default connection.

    Phase 10.2B-6: Supports both data-only and full OAuth connections.
    If no connected connection exists, creates a data-only connection
    with broker_account_id='data-only' and status='connected'.

    The Analytics Token is encrypted at rest via Fernet (app.crypto).
    Only one Analytics Token per (user, broker) — overwrites existing.

    Returns the BrokerConnection row.
    """
    from app.crypto import encrypt

    broker_upper = broker.upper()

    # Try to find an existing connected default connection
    conn = (
        db.query(BrokerConnection)
        .filter(
            BrokerConnection.user_id == user_id,
            BrokerConnection.broker == broker_upper,
            BrokerConnection.status == "connected",
            BrokerConnection.is_default == True,
        )
        .first()
    )

    # If no connected connection exists, create a data-only connection
    # This allows users to connect market data without completing OAuth
    if conn is None:
        conn = BrokerConnection(
            id=str(uuid4()),
            user_id=user_id,
            broker=broker_upper,
            broker_account_id="data-only",
            status="connected",
            data_status="active",
            data_source="analytics_token",
            display_label=f"{broker_upper} (Data Only)",
            connected_at=_utcnow(),
        )
        db.add(conn)
        db.flush()

    conn.broker_analytics_token_encrypted = encrypt(analytics_token)
    conn.data_status = "active"
    conn.data_source = "analytics_token"
    conn.updated_at = _utcnow()
    db.flush()
    return conn


def get_analytics_token(
    db: Session,
    user_id: str,
    broker: str,
    *,
    connection_id: str | None = None,
) -> str | None:
    """Retrieve and decrypt the Analytics Token for a user's broker connection.

    Phase 10.2B-6: Works with both data-only and full OAuth connections.
    Requires data_status == 'active' for explicit data authorization.

    Resolution:
      1. If connection_id is provided, resolve exactly that connection
         (verifies user ownership and data authorization).
      2. Otherwise, prefer is_default=True connection.
      3. Fallback: first connected connection with active data.

    Returns None if no Analytics Token is stored or data is inactive.
    """
    from app.crypto import decrypt

    broker_upper = broker.upper()

    # Path 1: Exact connection_id — deterministic, user-scoped
    if connection_id is not None:
        conn = (
            db.query(BrokerConnection)
            .filter(
                BrokerConnection.id == connection_id,
                BrokerConnection.user_id == user_id,
                BrokerConnection.broker == broker_upper,
                BrokerConnection.status == "connected",
                BrokerConnection.data_status == "active",
                BrokerConnection.broker_analytics_token_encrypted.isnot(None),
            )
            .first()
        )
        if conn is None:
            return None
        return decrypt(conn.broker_analytics_token_encrypted)

    # Path 2: No connection_id — prefer default, fallback to first active
    conn = (
        db.query(BrokerConnection)
        .filter(
            BrokerConnection.user_id == user_id,
            BrokerConnection.broker == broker_upper,
            BrokerConnection.status == "connected",
            BrokerConnection.data_status == "active",
            BrokerConnection.broker_analytics_token_encrypted.isnot(None),
            BrokerConnection.is_default == True,
        )
        .first()
    )
    if conn is None:
        # Fallback: any connected connection with active data
        conn = (
            db.query(BrokerConnection)
            .filter(
                BrokerConnection.user_id == user_id,
                BrokerConnection.broker == broker_upper,
                BrokerConnection.status == "connected",
                BrokerConnection.data_status == "active",
                BrokerConnection.broker_analytics_token_encrypted.isnot(None),
            )
            .order_by(BrokerConnection.is_default.desc(), BrokerConnection.created_at.asc())
            .first()
        )
    if conn is None:
        return None
    return decrypt(conn.broker_analytics_token_encrypted)


def remove_analytics_token(
    db: Session,
    user_id: str,
    broker: str,
) -> bool:
    """Remove the Analytics Token from a user's broker connection.

    Phase 10.2B-6: If this was a data-only connection (broker_account_id='data-only'),
    also update data_status to 'inactive'.

    Returns True if a token was removed, False if none existed.
    """
    broker_upper = broker.upper()
    conn = (
        db.query(BrokerConnection)
        .filter(
            BrokerConnection.user_id == user_id,
            BrokerConnection.broker == broker_upper,
            BrokerConnection.status == "connected",
            BrokerConnection.is_default == True,
        )
        .first()
    )
    if conn is None or conn.broker_analytics_token_encrypted is None:
        return False
    conn.broker_analytics_token_encrypted = None
    conn.data_status = "inactive"
    conn.data_source = None
    conn.updated_at = _utcnow()
    db.flush()
    return True
