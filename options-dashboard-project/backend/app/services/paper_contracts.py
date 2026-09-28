"""Server-authoritative contract metadata for paper execution."""

from __future__ import annotations

from app.brokers import gateway
from app.brokers.domain.errors import BrokerError
from app.config import BROKER_ID_UPSTOX
from app.services.paper_execution import PaperExecutionError


async def resolve_authoritative_lot_sizes(
    access_token: str,
    symbol: str,
    legs,
) -> dict[tuple[str, float, str], int]:
    """Resolve one authoritative lot size per requested option contract.

    The source is the broker's current option-contract metadata. Client-provided
    lot sizes are never accepted as the accounting authority.
    """
    adapter = gateway.create(BROKER_ID_UPSTOX, access_token=access_token)
    try:
        contract_result = await adapter.get_option_contracts(symbol.upper())
    except BrokerError as exc:
        raise PaperExecutionError(
            "CONTRACT_DATA_MISSING",
            f"Could not load contract metadata for {symbol.upper()}: {exc}",
        ) from exc

    contracts = contract_result.get("contracts") or []
    by_key = {
        (
            str(contract["expiry"]),
            float(contract["strike"]),
            str(contract["option_type"]).lower(),
        ): int(contract["lot_size"])
        for contract in contracts
        if contract.get("lot_size") is not None
    }

    resolved: dict[tuple[str, float, str], int] = {}
    for leg in legs:
        key = (
            str(leg.expiration_date),
            float(leg.strike_price),
            str(leg.option_type).lower(),
        )
        lot_size = by_key.get(key)
        if lot_size is None or lot_size <= 0:
            raise PaperExecutionError(
                "CONTRACT_DATA_MISSING",
                f"Authoritative lot size is unavailable for "
                f"{symbol.upper()} {leg.strike_price:g} "
                f"{leg.option_type.upper()} ({leg.expiration_date}).",
            )
        resolved[key] = lot_size

    return resolved
