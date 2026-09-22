"""Day 43 — versioned market-data chain domain (``/api/v1/chains``).

Same audited authorization and market-data credential resolution as the
compatibility ``/chains`` surface (single resolution path, Analytics
Token first, OAuth fallback, legacy session compatibility), exposed
under the canonical version prefix with EXPLICIT domain response
schemas. The raw broker-adapter dict never passes through unchecked.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.v1.schemas import ExpiriesOut, OptionChainOut
from app.routers.chains import (
    BROKER_ID_UPSTOX,
    INSTRUMENT_KEYS,
    call_upstox,
    gateway,
    require_market_data_token,
    resolve_symbol,
    validate_expiry_date,
)
from app.routers.deps import get_session_id

router = APIRouter(prefix="/chains")

# Re-exported for tests/observability: the versioned surface resolves
# credentials through the SAME canonical resolver as the compatibility
# surface (one authoritative resolution path).
from app.services.market_data_authorization import (  # noqa: E402,F401
    resolve_market_data_token,
)

chains_v1_router = router


@router.get("/{symbol}/expiries", response_model=ExpiriesOut)
async def list_expiries(
    symbol: str, session_id: str | None = Depends(get_session_id)
):
    symbol = resolve_symbol(symbol)
    credential, _user_id = require_market_data_token(session_id)
    adapter = gateway.create(BROKER_ID_UPSTOX, access_token=credential.token)
    payload = await call_upstox(
        adapter.get_option_contracts(symbol),
        source=credential.source,
        session_id=session_id,
    )
    return payload


@router.get("/{symbol}", response_model=OptionChainOut)
async def get_chain(
    symbol: str,
    expiry_date: str = Query(..., description="YYYY-MM-DD"),
    session_id: str | None = Depends(get_session_id),
):
    symbol = resolve_symbol(symbol)
    expiry_date = validate_expiry_date(expiry_date)
    credential, _user_id = require_market_data_token(session_id)
    adapter = gateway.create(BROKER_ID_UPSTOX, access_token=credential.token)
    payload = await call_upstox(
        adapter.get_option_chain(symbol, expiry_date),
        source=credential.source,
        session_id=session_id,
    )
    return payload
