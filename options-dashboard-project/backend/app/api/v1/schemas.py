"""Day 43 — market-data chain domain schemas (versioned API contract).

Explicit domain request/response schemas for the scoped versioned chain
surface. These are API contracts, NOT persistence models: the chain
domain has no ORM rows of its own (it normalizes live broker payloads),
so the boundary here is between the broker adapter's internal dict and
the stable public contract. Missing market data stays ``None`` — never
a fabricated zero.
"""
from typing import Optional

from pydantic import BaseModel, ConfigDict


class ChainLegOut(BaseModel):
    """One option leg (call or put) at a strike — canonical fields only."""

    model_config = ConfigDict(extra="forbid")

    ltp: Optional[float] = None
    oi: Optional[int] = None
    chg_oi: Optional[int] = None
    volume: Optional[int] = None
    quote_timestamp: Optional[str] = None
    iv: Optional[float] = None
    delta: Optional[float] = None
    theta: Optional[float] = None
    gamma: Optional[float] = None
    vega: Optional[float] = None
    pop: Optional[float] = None


class ChainRowOut(BaseModel):
    """One strike of the option chain."""

    model_config = ConfigDict(extra="forbid")

    strike: float
    call: ChainLegOut
    put: ChainLegOut


class OptionChainOut(BaseModel):
    """Canonical option chain response (``GET /api/v1/chains/{symbol}``)."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    expiry_date: str
    underlying_spot_price: Optional[float] = None
    chain: list[ChainRowOut]


class ExpiriesOut(BaseModel):
    """Canonical expiry list (``GET /api/v1/chains/{symbol}/expiries``)."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    expiries: list[str]
