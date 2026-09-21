"""Canonical read-only market-data credential resolution.

One authoritative path for every market-data consumer (the option-chain
router, the WebSocket live feed, and background GEX capture — which
already resolves through the same underlying ``identity.get_analytics_token``
semantics):

    StrikeNova platform session   (proves WHO the user is)
        → resolve_market_data_token(user_id, broker)
              1. the user's stored Upstox Analytics Token — the preferred
                 read-only market-data credential (encrypted at rest,
                 decrypted server-side only, requires an explicitly
                 authorized connection: ``connected`` + ``data_status
                 == "active"``);
              2. fallback: the user's default connected connection's
                 active OAuth BrokerAuthorization — used only where the
                 architecture intentionally supports OAuth as a
                 market-data source;
        → credential handed to the broker adapter (server-side only)

Security properties:
- Token material is decrypted ONLY in-process; never logged, never
  serialized into API responses or diagnostics.
- A platform session identifier is NEVER returned as a broker
  credential (the caller-side ``is_platform_session_token`` defense
  remains in place).
- Ownership is enforced by ``user_id`` at every query — one user can
  never resolve another user's credential.
- An explicitly pinned ``connection_id`` resolves ONLY that connection:
  no silent cross-connection fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.identity import BrokerConnection, get_analytics_token

ANALYTICS_SOURCE = "analytics_token"
OAUTH_SOURCE = "broker_oauth"
# Pre-architecture compatibility source: an in-memory session created by the
# legacy OAuth flow that carries a cached broker token and has no durable
# platform-session row. Kept so existing clients keep working; new logins
# always resolve through the durable platform-session path above.
LEGACY_SESSION_SOURCE = "legacy_session_token"


@dataclass(frozen=True)
class MarketDataCredential:
    """A resolved read-only market-data credential (server-side only).

    ``token`` is the secret material itself — it must never be logged or
    returned by any API endpoint. ``source`` records the credential's
    provenance so callers can distinguish Analytics-Token authorization
    from OAuth fallback (and from legacy compatibility sessions).
    """

    token: str
    source: str
    connection_id: str | None
    broker: str


def _analytics_connection_id(
    db: Session, user_id: str, broker: str
) -> str | None:
    """Pick which connection to consult for the Analytics Token.

    Selection only — ``identity.get_analytics_token`` remains the single
    authority for validity and decryption. Mirrors its default-first,
    oldest-first fallback ordering exactly (``created_at.asc()``), so the
    chosen connection is the one ``get_analytics_token`` itself would use.
    """
    row = (
        db.query(BrokerConnection.id)
        .filter(
            BrokerConnection.user_id == user_id,
            BrokerConnection.broker == broker,
            BrokerConnection.status == "connected",
            BrokerConnection.data_status == "active",
            BrokerConnection.broker_analytics_token_encrypted.isnot(None),
        )
        .order_by(
            BrokerConnection.is_default.desc(),
            BrokerConnection.created_at.asc(),
        )
        .first()
    )
    return row[0] if row else None


def resolve_market_data_token(
    db: Session,
    user_id: str,
    broker: str = "UPSTOX",
    *,
    connection_id: str | None = None,
    now: datetime | None = None,
) -> MarketDataCredential | None:
    """Resolve the user's read-only market-data credential for *broker*.

    1. Analytics Token (preferred): resolves the user's OWN authorized
       connection via ``identity.get_analytics_token`` — the single
       existing Analytics-Token authority shared with background GEX
       capture. Requires ``status == "connected"`` and
       ``data_status == "active"``.
    2. OAuth fallback: the user's default connected connection's active
       ``BrokerAuthorization`` (same ownership path as background GEX
       fallback — no session coupling).
    3. ``None`` when neither is available (the caller turns this into a
       structured "not connected" error).

    When ``connection_id`` is provided, ONLY that user-owned connection
    is consulted (Analytics Token first, then its active OAuth
    authorization) — never another connection.
    """
    from app.crypto import decrypt
    from app.services.broker_authorization import (
        resolve_broker_authorization,
        resolve_default_broker_authorization,
    )

    broker_upper = (broker or "").upper()

    def _analytics(conn_id: str | None) -> MarketDataCredential | None:
        if conn_id is None:
            return None
        token = get_analytics_token(db, user_id, broker_upper, connection_id=conn_id)
        if not token:
            return None
        return MarketDataCredential(
            token=token,
            source=ANALYTICS_SOURCE,
            connection_id=conn_id,
            broker=broker_upper,
        )

    # --- 1. Analytics Token (preferred read-only market-data credential) ---
    if connection_id is not None:
        credential = _analytics(connection_id)
    else:
        credential = _analytics(_analytics_connection_id(db, user_id, broker_upper))
    if credential is not None:
        return credential

    # --- 2. OAuth fallback (intentionally supported market-data source) ---
    if connection_id is not None:
        conn, authz = resolve_broker_authorization(db, user_id, broker_upper, now=now)
        if conn is not None and conn.id != connection_id:
            return None
    else:
        conn, authz = resolve_default_broker_authorization(db, user_id, now=now)
    if (
        conn is not None
        and authz is not None
        and authz.access_token_encrypted
        and conn.broker == broker_upper
    ):
        return MarketDataCredential(
            token=decrypt(authz.access_token_encrypted),
            source=OAUTH_SOURCE,
            connection_id=conn.id,
            broker=conn.broker,
        )

    return None
