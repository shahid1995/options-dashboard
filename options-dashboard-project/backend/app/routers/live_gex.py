"""Live GEX API — Phase 8A.

Endpoints:
  GET /gex/live          — compute GEX from the current authorized user's option chain

All endpoints require session authentication (X-Session-Id header or
session_id cookie).  The broker token is used to fetch the option chain;
GEX is computed server-side from that chain data.

This endpoint does NOT persist snapshots (Phase 8B).  It does NOT use
a WebSocket (Phase 8C).  It computes GEX on demand from the current chain.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.brokers.adapters.upstox.mapper import UPSTOX_INSTRUMENT_KEYS as INSTRUMENT_KEYS
from app.brokers.domain.enums import BROKER_ID_UPSTOX
from app.brokers.domain.errors import BrokerError, BrokerErrorCode, PUBLIC_BROKER_ERROR_MESSAGE
from app.brokers.gateway import gateway
from app.routers.deps import get_session_id
from app.services import token_store
from app.services.platform_session import is_platform_session_token
from app.services.live_gex import LiveGexService

router = APIRouter()

# Shared service instance — stateless, safe for concurrent requests
_live_gex_service = LiveGexService()


# ---------------------------------------------------------------------------
# Auth helpers (consistent with chains.py / gex.py)
# ---------------------------------------------------------------------------

def _require_token(session_id: str | None) -> str:
    """Validate session and return the broker access token.

    Raises 401 if session is invalid/expired (not logged in).
    Raises 403 if session is valid but no broker token is available.
    """
    token = token_store.get_token(session_id) if session_id else None
    if token:
        return token

    # No broker token — check if session is valid at all
    if session_id and token_store.has_platform_session(session_id):
        raise HTTPException(
            status_code=403,
            detail="No broker token available. Connect your broker to view market data.",
        )
    raise HTTPException(status_code=401, detail="Not logged in. Visit /auth/login first.")


def _resolve_symbol(symbol: str) -> str:
    """Validate and normalize the symbol."""
    symbol = symbol.upper()
    if symbol not in INSTRUMENT_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown symbol '{symbol}'")
    return symbol


def _validate_expiry_date(expiry_date: str) -> str:
    """Validate expiry date format."""
    try:
        date.fromisoformat(expiry_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="expiry_date must be YYYY-MM-DD")
    return expiry_date


async def _fetch_chain(symbol: str, expiry_date: str, token: str, session_id: str | None = None) -> dict:
    """Fetch option chain from the customer's authorized broker.

    Uses the existing Upstox adapter — no second broker integration.
    Phase 9A: session_id is passed for scoped token invalidation.
    """
    adapter = gateway.create(BROKER_ID_UPSTOX, access_token=token)
    try:
        return await adapter.get_option_chain(symbol, expiry_date)
    except BrokerError as e:
        if e.code in BrokerErrorCode.SESSION_CODES:
            # Defense-in-depth: only clear real broker tokens.
            # Platform session tokens (email:..., google:...) must survive broker failures.
            existing = token_store.get_token(session_id)
            if not is_platform_session_token(existing):
                token_store.clear_token(session_id)
            raise HTTPException(
                status_code=401,
                detail="Upstox session expired. Please log in again.",
            ) from e
        raise HTTPException(
            status_code=502,
            detail=PUBLIC_BROKER_ERROR_MESSAGE,
        ) from e


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class StrikeGexOut(BaseModel):
    """GEX result for a single strike."""
    strike: float
    callGex: Optional[float] = None
    putGex: Optional[float] = None
    netGex: Optional[float] = None
    callOi: Optional[float] = None
    putOi: Optional[float] = None
    callGamma: Optional[float] = None
    putGamma: Optional[float] = None
    status: str = "unavailable"


class LiveGexResponse(BaseModel):
    """Complete live GEX calculation result."""
    symbol: Optional[str] = None
    spot: Optional[float] = None
    expiry: Optional[str] = None
    captured_at: str = ""
    methodology: str = "GEX_STANDARD_V1"
    sign_convention: str = "NAIVE_DEALER_CONVENTION"
    call_gex: Optional[float] = None
    put_gex: Optional[float] = None
    net_gex: Optional[float] = None
    availability_status: str = "unavailable"
    valid_strike_count: int = 0
    total_strike_count: int = 0
    chain_age_ms: Optional[float] = None
    methodology_metadata: dict = Field(default_factory=dict)
    strikes: list[StrikeGexOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/live", response_model=LiveGexResponse)
async def get_live_gex(
    symbol: str = Query("NIFTY", description="Underlying symbol"),
    expiry_date: str = Query(..., description="Expiry date YYYY-MM-DD"),
    session_id: str | None = Depends(get_session_id),
):
    """Compute GEX from the current authorized user's option chain.

    1. Authenticates using the existing session architecture.
    2. Fetches the option chain from the customer's authorized Upstox account.
    3. Computes GEX server-side using the Phase 7.1 formula.
    4. Returns the calculated GEX result.

    Does NOT persist the result (Phase 8B). Does NOT use WebSocket (Phase 8C).
    """
    symbol = _resolve_symbol(symbol)
    expiry_date = _validate_expiry_date(expiry_date)
    token = _require_token(session_id)

    # Phase 9B: rate limit check
    from app.services.rate_limiter import rate_limiter
    rate_limiter.check(session_id, "/gex/live")

    # Fetch chain from customer's broker
    chain = await _fetch_chain(symbol, expiry_date, token, session_id=session_id)

    # Calculate GEX server-side
    result = _live_gex_service.calculate(chain)

    # Convert to response model
    return LiveGexResponse(
        symbol=result.symbol,
        spot=result.spot,
        expiry=result.expiry,
        captured_at=result.captured_at,
        methodology=result.methodology,
        sign_convention=result.sign_convention,
        call_gex=result.call_gex,
        put_gex=result.put_gex,
        net_gex=result.net_gex,
        availability_status=result.availability_status,
        valid_strike_count=result.valid_strike_count,
        total_strike_count=result.total_strike_count,
        chain_age_ms=result.chain_age_ms,
        methodology_metadata=result.methodology_metadata,
        strikes=[
            StrikeGexOut(
                strike=s.strike,
                callGex=s.call_gex,
                putGex=s.put_gex,
                netGex=s.net_gex,
                callOi=s.call_oi,
                putOi=s.put_oi,
                callGamma=s.call_gamma,
                putGamma=s.put_gamma,
                status=s.status,
            )
            for s in result.strikes
        ],
    )
