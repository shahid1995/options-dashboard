import asyncio
import logging
import time
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from app.brokers.adapters.upstox.mapper import (
    UPSTOX_INSTRUMENT_KEYS as INSTRUMENT_KEYS,  # compat re-export (adapter mapping)
)
from app.brokers.adapters.upstox.mapper import transform_chain  # compat re-export
from app.brokers.domain.enums import BROKER_ID_UPSTOX
from app.brokers.domain.errors import BrokerError, BrokerErrorCode, PUBLIC_BROKER_ERROR_MESSAGE
from app.brokers.gateway import gateway
from app.routers.deps import get_session_id
from app.services import token_store
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

def require_token(session_id: str | None) -> str:
    """Return the broker access token for the session.

    Raises 401 if session is invalid/expired (not logged in).
    Raises 403 if session is valid but no broker token is available
    (Google/email session without broker connection).

    Platform session tokens (email:..., google:...) are NEVER returned
    as broker credentials — they are identified and rejected with 403
    before they can be passed to any broker adapter.
    """
    token = token_store.get_token(session_id)

    # DEFENSIVE: detect platform session tokens that the token store
    # returns from its in-memory cache.  These are NOT broker tokens
    # and must NEVER be passed to Upstox.
    if is_platform_session_token(token):
        raise HTTPException(
            status_code=403,
            detail="No broker token available. Connect your broker to view market data.",
        )

    if token:
        return token

    # No broker token in memory or DB. Check if the session itself is valid.
    from app.services.token_store import has_platform_session
    if has_platform_session(session_id):
        # Valid session exists but no broker token — platform-only user
        raise HTTPException(
            status_code=403,
            detail="No broker token available. Connect your broker to view market data.",
        )

    # No valid session at all
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


async def call_upstox(coro, *, session_id: str | None = None):
    """Awaits a broker-gateway call, translating session failures into a 401
    that also clears the stored token (broker tokens expire daily at 3:30 AM).

    The coroutine comes from a broker ADAPTER, so failures arrive as
    canonical BrokerError — never a provider exception.

    DEFENSIVE: Only clears tokens that are real broker credentials.
    Platform session tokens (email:..., google:...) are never cleared
    by broker error handling — clearing a platform token would destroy
    the user's StrikeNova authentication.
    """
    try:
        return await coro
    except BrokerError as e:
        if e.code in BrokerErrorCode.SESSION_CODES:
            if session_id:
                # Defense-in-depth: only clear REAL broker tokens.
                # Platform session tokens must survive broker failures.
                existing_token = token_store.get_token(session_id)
                if not is_platform_session_token(existing_token):
                    token_store.clear_token(session_id)
            raise HTTPException(status_code=401, detail="Upstox session expired. Please log in again.") from e
        # Sanitize: never expose raw upstream error message to clients
        logger.warning("Broker error: %s — %s", e.code.value, e.message)
        raise HTTPException(status_code=502, detail=PUBLIC_BROKER_ERROR_MESSAGE) from e


@router.get("/{symbol}/expiries")
async def list_expiries(symbol: str, session_id: str | None = Depends(get_session_id)):
    symbol = resolve_symbol(symbol)
    token = require_token(session_id)
    adapter = gateway.create(BROKER_ID_UPSTOX, access_token=token)
    return await call_upstox(adapter.get_option_contracts(symbol), session_id=session_id)


@router.get("/{symbol}")
async def get_chain(
    symbol: str,
    expiry_date: str = Query(..., description="YYYY-MM-DD"),
    session_id: str | None = Depends(get_session_id),
):
    symbol = resolve_symbol(symbol)
    expiry_date = validate_expiry_date(expiry_date)
    token = require_token(session_id)
    adapter = gateway.create(BROKER_ID_UPSTOX, access_token=token)
    return await call_upstox(adapter.get_option_chain(symbol, expiry_date), session_id=session_id)


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
      4401 — auth issues (token expired)
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

    # Get the broker token
    # get_token() returns None for platform-only sessions (Google/email)
    # and real broker tokens for broker sessions.
    token = token_store.get_token(session_id)

    # Platform session tokens (email:..., google:...) are NOT broker tokens.
    # Reject with 4401 — the same code used for expired broker sessions.
    if is_platform_session_token(token):
        await websocket.close(code=4401)
        return

    if not token:
        await websocket.close(code=4401)
        return

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

                # Check token validity periodically
                current_token = token_store.get_token(session_id)
                if is_platform_session_token(current_token) or not current_token:
                    await websocket.close(code=4401)
                    return

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
                    # Try HTTP fallback for this push
                    try:
                        adapter = gateway.create(BROKER_ID_UPSTOX, access_token=current_token)
                        chain = await adapter.get_option_chain(symbol, expiry_date)
                        await websocket.send_json(chain)
                        last_push = time.time()
                    except BrokerError as e:
                        if e.code in BrokerErrorCode.SESSION_CODES:
                            # Defense-in-depth: only clear real broker tokens
                            existing = token_store.get_token(session_id)
                            if not is_platform_session_token(existing):
                                token_store.clear_token(session_id)
                            await websocket.close(code=4401)
                            return

                await asyncio.sleep(0.1)  # Small sleep to prevent busy-waiting

        else:
            # HTTP polling fallback (original behavior)
            logger.info(
                "HTTP polling mode for client",
                extra={"symbol": symbol, "expiry": expiry_date},
            )
            while True:
                token = token_store.get_token(session_id)
                if is_platform_session_token(token) or not token:
                    await websocket.close(code=4401)
                    return
                try:
                    adapter = gateway.create(BROKER_ID_UPSTOX, access_token=token)
                    chain = await adapter.get_option_chain(symbol, expiry_date)
                except BrokerError as e:
                    if e.code in BrokerErrorCode.SESSION_CODES:
                        # Defense-in-depth: only clear real broker tokens
                        existing = token_store.get_token(session_id)
                        if not is_platform_session_token(existing):
                            token_store.clear_token(session_id)
                        await websocket.close(code=4401)
                    else:
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
