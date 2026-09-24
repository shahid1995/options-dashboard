import asyncio
import logging
import time
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.brokers.adapters.upstox.mapper import (
    UPSTOX_INSTRUMENT_KEYS as INSTRUMENT_KEYS,  # compat re-export (adapter mapping)
)
from app.brokers.adapters.upstox.mapper import transform_chain  # compat re-export
from app.brokers.domain.enums import BROKER_ID_UPSTOX
from app.brokers.domain.errors import BrokerError, BrokerErrorCode
from app.brokers.gateway import gateway
from app.db import SessionLocal, get_db
from app.routers.deps import get_session_id
from app.services import token_store
from app.services.operations import record_broker_failure, record_market_data_stale
from app.services.market_data_authorization import (
    ANALYTICS_SOURCE,
    LEGACY_SESSION_SOURCE,
    OAUTH_SOURCE,
    MarketDataCredential,
    resolve_market_data_token,
)
from app.services.platform_session import is_platform_session_token

logger = logging.getLogger(__name__)

router = APIRouter()

# Index option chains available via Upstox (NSE + BSE). The instrument keys
# are the canonical mapping table living in the Upstox adapter
# (app/brokers/adapters/upstox/mapper.py); this re-export keeps the
# pre-existing import path working.

WS_PUSH_INTERVAL_SECONDS = 3
WS_LIVE_PUSH_INTERVAL_SECONDS = 1  # Push live ticks more frequently



def ws_session(websocket: WebSocket) -> tuple[str | None, str | None]:
    """Resolve the platform session from the canonical HttpOnly cookie only."""
    return websocket.cookies.get("strikenova_session"), None


_NOT_CONNECTED_DETAIL = (
    "Market data is not connected. Add your Upstox Analytics Token in "
    "Settings to view market data."
)


def _platform_user_id(session_id: str | None, db: Session | None = None) -> str | None:
    """Return the durable user_id for a valid platform session, else None.

    Uses the request-scoped DB session when provided (the request's unit
    of work — required under test fixtures that override ``get_db``);
    ad-hoc callers fall back to a short-lived ``SessionLocal``. Any
    lookup failure (no DB row, or the session store being unavailable)
    degrades to ``None`` so resolution continues on the legacy path —
    the same graceful behavior the pre-Analytics flow had.
    """
    from app.identity import get_active_session

    try:
        owns_db = db is None
        lookup_db = SessionLocal() if owns_db else db
        try:
            session = get_active_session(lookup_db, session_id)
            return session.user_id if session is not None else None
        finally:
            if owns_db:
                lookup_db.close()
    except Exception:
        return None


def _not_connected() -> HTTPException:
    # Day 43: the stable machine-readable token rides the exception as an
    # additive attribute — the versioned error envelope reads it; the
    # unversioned HTTP contract (status 403 + detail) is unchanged.
    exc = HTTPException(status_code=403, detail=_NOT_CONNECTED_DETAIL)
    exc.error_code = "MARKET_DATA_NOT_CONNECTED"
    return exc


def require_market_data_token(
    session_id: str | None, db: Session | None = None
) -> tuple[MarketDataCredential, str | None]:
    """Resolve the caller's read-only market-data credential.

    Priority (the Analytics Token is the authorized market-data
    credential; OAuth and legacy session tokens remain compatibility
    fallbacks):

      1. The valid platform session's stored Analytics Token.
      2. The valid platform session's default OAuth BrokerAuthorization.
      3. Legacy session-scoped broker token (pre-architecture rows and
         compatibility sessions).

    Raises 401 when the caller has no valid session at all, and 403 for
    a valid session with no active market-data authorization — the two
    states the UI distinguishes.

    Platform session tokens (email:..., google:...) are NEVER returned
    as broker credentials — they are rejected before any credential is
    handed to a broker adapter.
    """
    user_id = _platform_user_id(session_id, db=db)

    if user_id is not None:
        owns_db = db is None
        cred_db = SessionLocal() if owns_db else db
        try:
            credential = resolve_market_data_token(cred_db, user_id, BROKER_ID_UPSTOX.value)
        finally:
            if owns_db:
                cred_db.close()
        if credential is not None:
            return credential, user_id

    # Legacy compatibility: session-scoped broker tokens (in-memory cache
    # or pre-architecture DB rows). Platform session identifiers are
    # never accepted here.
    legacy_token = token_store.get_token(session_id)
    if legacy_token and not is_platform_session_token(legacy_token):
        return (
            MarketDataCredential(
                token=legacy_token,
                source=LEGACY_SESSION_SOURCE,
                connection_id=None,
                broker=BROKER_ID_UPSTOX.value,
            ),
            user_id,
        )

    if user_id is not None:
        # Valid platform session, but no market-data authorization.
        raise _not_connected()

    raise HTTPException(status_code=401, detail="Not logged in. Visit /auth/login first.")


def resolve_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if symbol not in INSTRUMENT_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown symbol '{symbol}'")
    return symbol


def validate_expiry_date(expiry_date: str) -> str:
    try:
        date.fromisoformat(expiry_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="expiry_date must be YYYY-MM-DD")
    return expiry_date


async def call_upstox(
    coro,
    *,
    source: str | None = None,
    session_id: str | None = None,
    db: Session | None = None,
    user_scope: str | None = None,
):
    """Awaits a broker-gateway call, translating broker failures into HTTP.

    The coroutine comes from a broker ADAPTER, so failures arrive as
    canonical BrokerError — never a provider exception.

    Analytics-Token credentials are NOT invalidated on an upstream auth
    failure: a rejected Analytics Token means the stored credential is
    invalid/expired — the user must refresh it in Settings (the stored
    token is never cleared server-side), not log in again. Legacy
    session-scoped tokens keep the original behavior: a session-code
    failure clears the stored token (broker tokens expire daily).
    """
    try:
        return await coro
    except BrokerError as e:
        # Day 46 (F13): the REAL broker failure boundary emits the
        # operational alert — observational only, the translated HTTP
        # error below is unchanged. The request-scoped DB session (DI)
        # is used when provided; ad-hoc callers fall back to a short-
        # lived SessionLocal. Any recording failure is swallowed so the
        # original business error still surfaces.
        try:
            scope = user_scope if user_scope is not None else _platform_user_id(session_id)
            if scope is not None:
                owns_db = db is None
                alert_db = SessionLocal() if owns_db else db
                try:
                    record_broker_failure(
                        alert_db,
                        user_scope=scope,
                        broker=BROKER_ID_UPSTOX.value,
                        reason=f"{getattr(e.code, 'value', e.code)}: {e.message}",
                    )
                    alert_db.commit()
                finally:
                    if owns_db:
                        alert_db.close()
        except Exception:  # alert recording must never change the outcome
            pass
        if e.code in BrokerErrorCode.SESSION_CODES:
            if source == ANALYTICS_SOURCE:
                raise HTTPException(
                    status_code=401,
                    detail="Upstox market-data authorization was rejected. Your Analytics Token is invalid or expired — update it in Settings.",
                ) from e
            if source == LEGACY_SESSION_SOURCE and session_id:
                # Defense-in-depth: only clear REAL broker tokens.
                # Platform session tokens must survive broker failures.
                existing_token = token_store.get_token(session_id)
                if not is_platform_session_token(existing_token):
                    token_store.clear_token(session_id)
            raise HTTPException(status_code=401, detail="Upstox session expired. Please log in again.") from e
        raise HTTPException(status_code=502, detail=f"Upstox API error ({e.status_code}): {e.message}") from e


@router.get("/{symbol}/expiries")
async def list_expiries(
    symbol: str,
    session_id: str | None = Depends(get_session_id),
    db: Session = Depends(get_db),
):
    symbol = resolve_symbol(symbol)
    credential, user_id = require_market_data_token(session_id, db=db)
    adapter = gateway.create(BROKER_ID_UPSTOX, access_token=credential.token)
    return await call_upstox(
        adapter.get_option_contracts(symbol),
        source=credential.source,
        session_id=session_id,
        db=db,
        user_scope=user_id,
    )


@router.get("/{symbol}")
async def get_chain(
    symbol: str,
    expiry_date: str = Query(..., description="YYYY-MM-DD"),
    session_id: str | None = Depends(get_session_id),
    db: Session = Depends(get_db),
):
    symbol = resolve_symbol(symbol)
    expiry_date = validate_expiry_date(expiry_date)
    credential, user_id = require_market_data_token(session_id, db=db)
    adapter = gateway.create(BROKER_ID_UPSTOX, access_token=credential.token)
    return await call_upstox(
        adapter.get_option_chain(symbol, expiry_date),
        source=credential.source,
        session_id=session_id,
        db=db,
        user_scope=user_id,
    )


@router.websocket("/ws/{symbol}")
async def chain_ws(websocket: WebSocket, symbol: str, expiry_date: str = Query(...)):
    """Pushes the canonical option chain to the client.

    **Phase 8C**: Uses the Upstox V3 WebSocket market-data feed as the
    primary data source, falling back to HTTP polling if the WebSocket
    connection fails.

    The frontend receives the same canonical chain format regardless of
    the data source — it does not need to know whether the source is
    HTTP polling or WebSocket.

    Close codes:
      4401 — auth issues (no valid platform session, no active
             market-data authorization, or a rejected credential —
             the client's HTTP fallback distinguishes 401 vs 403)
      4404 — unknown symbol
      4422 — malformed expiry date
      4502 — broker/API error
    """
    session_id, subprotocol = ws_session(websocket)
    await websocket.accept(subprotocol=subprotocol)

    symbol = symbol.upper()
    if symbol not in INSTRUMENT_KEYS:
        await websocket.close(code=4404)
        return

    try:
        date.fromisoformat(expiry_date)
    except ValueError:
        await websocket.close(code=4422)
        return

    # Resolve the read-only market-data credential from the platform
    # session: Analytics Token preferred, OAuth fallback. 4401 closes so
    # the client falls back to HTTP polling, which distinguishes 401 (no
    # platform session) from 403 (market data not connected).
    try:
        credential, _user_id = require_market_data_token(session_id)
    except HTTPException:
        await websocket.close(code=4401)
        return
    token = credential.token

    # Phase 8C: Try Upstox V3 WebSocket feed first
    feed = None
    use_websocket_feed = True

    try:
        from app.services.upstox_market_feed import UpstoxMarketFeed
        feed = UpstoxMarketFeed(access_token=token)

        # Get option contracts to discover instrument keys
        adapter = gateway.create(BROKER_ID_UPSTOX, access_token=token)
        contracts = await adapter.get_option_contracts(symbol)
        expiries = contracts.get("expiries", [])

        if expiry_date not in expiries:
            # Expiry not available — fall back to HTTP
            logger.warning(
                "Expiry not found in contracts, falling back to HTTP",
                extra={"symbol": symbol, "expiry": expiry_date},
            )
            use_websocket_feed = False
        else:
            # Get the chain to discover instrument keys
            chain_data = await adapter.get_option_chain(symbol, expiry_date)

            # Build contract_specs mapping from the chain
            contract_specs = {}
            underlying_spot = chain_data.get("underlying_spot_price")
            for row in chain_data.get("chain", []):
                strike = row.get("strike")
                call = row.get("call", {})
                put = row.get("put", {})

                # Extract instrument keys from the chain response
                # The Upstox chain response includes instrument_key in market_data
                # but our transform_chain() strips it. We need to get raw data.
                # For now, we'll use the HTTP chain as the initial snapshot
                # and let the WebSocket feed update it incrementally.

            # Connect to the WebSocket feed
            # Use a background task to keep the feed running
            await feed.connect(
                symbol=symbol,
                expiry_date=expiry_date,
                instrument_keys=[],  # Will be populated by subscribe
                contract_specs=contract_specs,
            )

    except Exception as e:
        logger.warning(
            "WebSocket feed initialization failed, falling back to HTTP",
            extra={"symbol": symbol, "error": str(e)},
        )
        use_websocket_feed = False
        if feed:
            try:
                await feed.disconnect()
            except Exception:
                pass
            feed = None

    try:
        if use_websocket_feed and feed and feed.state.value not in ("disconnected", "auth_failed"):
            # WebSocket feed mode: push live ticks as they arrive
            logger.info(
                "WebSocket feed active for client",
                extra={"symbol": symbol, "expiry": expiry_date},
            )

            last_push = 0.0
            while True:
                # Check if client is still connected
                try:
                    # Send a ping to check connection
                    await asyncio.wait_for(websocket.send_text(""), timeout=0.1)
                except Exception:
                    break

                # Legacy session credentials: keep the original mid-stream
                # validity check. Durable credentials (Analytics Token /
                # OAuth authorization) are validated at connect time; their
                # failures surface through the adapter calls below.
                if credential.source == LEGACY_SESSION_SOURCE:
                    current_token = token_store.get_token(session_id)
                    if is_platform_session_token(current_token) or not current_token:
                        await websocket.close(code=4401)
                        return
                    token = current_token

                # Push chain data at configured interval
                now = time.time()
                if now - last_push >= WS_LIVE_PUSH_INTERVAL_SECONDS:
                    try:
                        chain = feed.get_option_chain(symbol, expiry_date)
                        if chain.get("chain"):  # Only push if we have data
                            await websocket.send_json(chain)
                            last_push = now
                    except Exception as e:
                        logger.debug(
                            "Error getting chain from feed",
                            extra={"error": str(e)},
                        )

                # If feed is stale, try to recover
                if feed.is_stale() and feed.state.value == "live":
                    logger.warning(
                        "Feed data stale, attempting recovery",
                        extra={"symbol": symbol},
                    )
                    # Day 46 (F13): real staleness boundary emits the
                    # market_data.stale alert (observational only).
                    try:
                        db = SessionLocal()
                        try:
                            # Platform user resolution may legitimately fail
                            # (legacy session-scoped WS tokens predate the
                            # durable identity); such staleness events are
                            # PLATFORM-scoped (user_scope None) — never an
                            # empty-string user scope, which would strand the
                            # event outside every tenant's visibility.
                            record_market_data_stale(
                                db,
                                user_scope=_platform_user_id(session_id),
                                symbol=symbol,
                                age_seconds=time.time() - feed._last_tick_time,
                            )
                            db.commit()
                        finally:
                            db.close()
                    except Exception:
                        pass
                    # Try HTTP fallback for this push
                    try:
                        adapter = gateway.create(BROKER_ID_UPSTOX, access_token=token)
                        chain = await adapter.get_option_chain(symbol, expiry_date)
                        await websocket.send_json(chain)
                        last_push = time.time()
                    except BrokerError as e:
                        if e.code in BrokerErrorCode.SESSION_CODES:
                            # Defense-in-depth: only clear real broker tokens.
                            if credential.source == LEGACY_SESSION_SOURCE:
                                existing = token_store.get_token(session_id)
                                if not is_platform_session_token(existing):
                                    token_store.clear_token(session_id)
                            await websocket.close(code=4401)
                            return
                        # Transient upstream failure during recovery: keep
                        # the session alive (original behavior).

                await asyncio.sleep(0.1)  # Small sleep to prevent busy-waiting

        else:
            # HTTP polling fallback (original behavior)
            logger.info(
                "HTTP polling mode for client",
                extra={"symbol": symbol, "expiry": expiry_date},
            )
            while True:
                if credential.source == LEGACY_SESSION_SOURCE:
                    token = token_store.get_token(session_id)
                    if is_platform_session_token(token) or not token:
                        await websocket.close(code=4401)
                        return
                try:
                    adapter = gateway.create(BROKER_ID_UPSTOX, access_token=token)
                    chain = await adapter.get_option_chain(symbol, expiry_date)
                except BrokerError as e:
                    if e.code in BrokerErrorCode.SESSION_CODES:
                        if credential.source == ANALYTICS_SOURCE:
                            # Invalid/expired Analytics Token: the stored
                            # credential must be refreshed in Settings —
                            # never destroy unrelated session state.
                            await websocket.close(code=4401)
                        elif credential.source == LEGACY_SESSION_SOURCE:
                            # Defense-in-depth: only clear real broker tokens.
                            existing = token_store.get_token(session_id)
                            if not is_platform_session_token(existing):
                                token_store.clear_token(session_id)
                            await websocket.close(code=4401)
                        else:
                            await websocket.close(code=4401)
                        return
                    await websocket.close(code=4502)
                    return
                await websocket.send_json(chain)
                await asyncio.sleep(WS_PUSH_INTERVAL_SECONDS)

    except WebSocketDisconnect:
        pass
    finally:
        # Clean up the WebSocket feed
        if feed:
            try:
                await feed.disconnect()
            except Exception:
                pass
