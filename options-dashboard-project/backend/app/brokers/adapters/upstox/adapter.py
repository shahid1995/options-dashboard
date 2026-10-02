"""Upstox adapter (Phase 6.5.0.2) — Adapter #1 behind the broker gateway.

Wraps the pre-existing raw Upstox HTTP client (``app.services.upstox`` —
base URLs, OAuth, tokens, instrument keys, response formats and
``UpstoxError`` all stay HERE / in the raw client) and exposes the
broker-neutral :class:`BrokerAdapter` contract to the application.

Boundary rules enforced here:

- ``UpstoxError`` never escapes this adapter: every failure is mapped to a
  canonical :class:`BrokerError`.
- Broker-specific field names never appear in method signatures consumed
  by the app (chain/contract methods return canonical structures; raw
  payload returns are consumed only by the broker integration services).
- Tokens are never logged, never repr'd, never returned in results.
- Order/trade/portfolio operations are PREPARED but NOT WIRED: they raise
  ``BrokerError(CAPABILITY_UNSUPPORTED)`` until a later phase wires live
  execution. No fake implementations.

The current single-user MVP passes the access token explicitly (it comes
from the app's session token store). The optional ``connection_context``
carries user/account scope for the future multi-connection model.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.brokers.adapters.upstox import mapper
from app.brokers.domain.capabilities import (
    BrokerCapabilities,
    BrokerCapability,
    CapabilityState,
)
from app.brokers.domain.enums import BROKER_ID_UPSTOX, BrokerId
from app.brokers.domain.errors import BrokerError, BrokerErrorCode
from app.brokers.domain.models import (
    BrokerConnectionContext,
    BrokerInstrumentMapping,
    BrokerOrderRequest,
    BrokerOrderResult,
    InstrumentIdentity,
)
from app.services import upstox
from app.services.upstox import UpstoxError

logger = logging.getLogger(__name__)

NOT_WIRED_MESSAGE = (
    "Upstox {operation} is prepared but NOT wired — live broker execution "
    "is not enabled in Phase 6.5.0.2."
)


class UpstoxAdapter:
    """Canonical broker adapter for Upstox (Adapter #1)."""

    broker_id: str = BROKER_ID_UPSTOX.value
    broker_name: str = "UPSTOX"

    # ---- construction -----------------------------------------------------

    def __init__(
        self,
        access_token: str | None = None,
        *,
        api_key: str | None = None,           # Phase 10.2B-2: user's API key
        api_secret: str | None = None,        # Phase 10.2B-2: user's API secret
        redirect_uri: str | None = None,      # Phase 10.2B-2: user's redirect URI
        connection_context: BrokerConnectionContext | None = None,
        login_url_builder=None,
        token_exchanger=None,
        profile_fetcher=None,
        funds_fetcher=None,
        margin_fetcher=None,
        chain_fetcher=None,
        contracts_fetcher=None,
        market_status_fetcher=None,
        quote_fetcher=None,
        now=None,
    ):
        self._access_token = access_token
        self._api_key = api_key
        self._api_secret = api_secret
        self._redirect_uri = redirect_uri
        self._connection_context = connection_context
        # Fetcher defaults resolve at CALL time via the module attribute so
        # runtime monkeypatching (tests, tooling) always intercepts.
        self._login_url_builder = login_url_builder
        self._token_exchanger = token_exchanger
        self._profile_fetcher = profile_fetcher
        self._funds_fetcher = funds_fetcher
        self._margin_fetcher = margin_fetcher
        self._chain_fetcher = chain_fetcher
        self._contracts_fetcher = contracts_fetcher
        self._market_status_fetcher = market_status_fetcher
        self._quote_fetcher = quote_fetcher
        self._now = now or (lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:  # never include the access token
        return f"UpstoxAdapter(broker={self.broker_name})"

    def get_connection_context(self) -> BrokerConnectionContext | None:
        return self._connection_context

    # ---- error boundary ---------------------------------------------------

    @staticmethod
    def _map_error(exc: UpstoxError) -> BrokerError:
        """Map an Upstox HTTP failure to the canonical taxonomy.

        The upstream message is preserved (it is broker diagnostics, not a
        credential); the canonical code is what the app branches on.
        """
        status = exc.status_code
        if status in (401, 403):
            return BrokerError(
                BrokerErrorCode.TOKEN_EXPIRED,
                "Upstox session expired or unauthorized.",
                status_code=status,
            )
        if status == 423:
            return BrokerError(
                BrokerErrorCode.MAINTENANCE,
                "Upstox is in its daily maintenance window.",
                status_code=status,
            )
        if status == 429:
            return BrokerError(
                BrokerErrorCode.RATE_LIMITED,
                "Upstox rate limit reached — try again shortly.",
                status_code=status,
            )
        if status == 502 and str(exc.message).startswith("Could not reach Upstox"):
            return BrokerError(
                BrokerErrorCode.NETWORK_ERROR,
                f"Could not reach Upstox: {exc.message}",
                status_code=status,
            )
        return BrokerError(
            BrokerErrorCode.UPSTREAM_ERROR,
            exc.message,
            status_code=status,
        )

    # ---- low-level fetch helpers (token + fetcher resolution) -------------

    def _require_token(self) -> str:
        if not self._access_token:
            raise BrokerError(
                BrokerErrorCode.AUTH_REQUIRED,
                "Broker login required — authenticate with Upstox first.",
            )
        return self._access_token

    async def _fetch_profile(self) -> dict:
        fetcher = self._profile_fetcher or upstox.get_broker_profile
        return await fetcher(self._require_token())

    async def _fetch_funds(self) -> dict:
        fetcher = self._funds_fetcher or upstox.get_funds_and_margin
        return await fetcher(self._require_token())

    async def _fetch_margin(self, payload: list[dict]) -> dict:
        fetcher = self._margin_fetcher or upstox.get_margin_details
        return await fetcher(self._require_token(), payload)

    async def _fetch_raw_chain(self, broker_key: str, expiry_date: str) -> dict:
        fetcher = self._chain_fetcher or upstox.get_option_chain
        return await fetcher(self._require_token(), broker_key, expiry_date)

    async def _fetch_contracts(self, broker_key: str) -> dict:
        fetcher = self._contracts_fetcher or upstox.get_option_contracts
        return await fetcher(self._require_token(), broker_key)

    async def _fetch_market_status(self, exchange: str) -> dict:
        fetcher = self._market_status_fetcher or upstox.get_market_status
        return await fetcher(self._require_token(), exchange=exchange)

    # ---- AUTHENTICATION ---------------------------------------------------

    def get_authorization_url(self, state: str) -> str:
        if self._login_url_builder:
            return self._login_url_builder(state)
        if self._api_key:
            return upstox.get_login_url(
                state,
                client_id=self._api_key,
                redirect_uri=self._redirect_uri,
            )
        return upstox.get_login_url(state)

    async def exchange_authorization_code(self, code: str) -> str:
        if self._token_exchanger:
            exchanger = self._token_exchanger
        else:
            exchanger = upstox.exchange_code_for_token
        try:
            if self._api_key and self._api_secret:
                return await exchanger(
                    code,
                    client_id=self._api_key,
                    client_secret=self._api_secret,
                    redirect_uri=self._redirect_uri,
                )
            return await exchanger(code)
        except UpstoxError as exc:
            raise self._map_error(exc) from exc

    def disconnect(self) -> None:
        """Release this adapter's token reference.

        Session/token revocation is owned by the app's auth layer (the
        token store) — the adapter only forgets its own copy.
        """
        self._access_token = None

    # ---- BROKER-SPECIFIC PROFILE EXTRACTION (AD-6) ----------------------

    @staticmethod
    def extract_account_id(profile: dict) -> str | None:
        """Extract Upstox-specific account ID from profile.

        Broker-specific logic lives in the adapter layer, never in
        identity.py (AD-6).
        """
        from app.brokers.adapters.upstox.profile import extract_account_id
        return extract_account_id(profile)

    # ---- ACCOUNT ----------------------------------------------------------

    async def get_profile(self) -> dict:
        try:
            return await self._fetch_profile()
        except UpstoxError as exc:
            raise self._map_error(exc) from exc

    async def get_funds(self) -> dict:
        try:
            return await self._fetch_funds()
        except UpstoxError as exc:
            raise self._map_error(exc) from exc

    async def get_margin(self, instruments: list[dict]) -> dict:
        try:
            return await self._fetch_margin(instruments)
        except UpstoxError as exc:
            raise self._map_error(exc) from exc

    # ---- CAPABILITIES -----------------------------------------------------

    def get_capabilities(self, profile: dict | None = None) -> BrokerCapabilities:
        """Canonical capability matrix (see ``upstox_capability_matrix``).

        ``profile`` (the normalized SAFE profile) is optional; when given,
        account-level signals (inactive account, missing NFO segment) are
        applied via the domain capability model.
        """
        items = [BrokerCapability(name, state, wired, detail) for name, state, wired, detail in upstox_capability_matrix()]
        capabilities = BrokerCapabilities(items)
        return capabilities.with_session_state(
            session_active=self._access_token is not None, profile=profile
        )

    # ---- INSTRUMENTS ------------------------------------------------------

    def resolve_instrument(self, symbol: str) -> InstrumentIdentity:
        return mapper.resolve_instrument_identity(symbol)

    def search_instruments(self, query: str) -> list[BrokerInstrumentMapping]:
        """Search the known instrument master (canonical identities + keys).

        Pure local lookup; a prefix/substring match on symbol or underlying.
        """
        q = (query or "").strip().upper()
        if not q:
            return []
        results = []
        for symbol, info in mapper.UPSTOX_INSTRUMENTS.items():
            if q in symbol or q in info["underlying"].upper():
                identity = mapper.resolve_instrument_identity(symbol)
                results.append(
                    BrokerInstrumentMapping(
                        broker=self.broker_name,
                        broker_instrument_id=info["broker_instrument_id"],
                        identity=identity,
                    )
                )
        return results

    async def get_option_contracts(self, symbol: str) -> dict:
        """Canonical expiry discovery plus live execution-contract metadata."""
        identity = self.resolve_instrument(symbol)
        key = mapper.broker_key_for(identity.symbol)
        try:
            raw = await self._fetch_contracts(key)
        except UpstoxError as exc:
            raise self._map_error(exc) from exc
        return {
            "symbol": identity.symbol,
            "expiries": mapper.contracts_from_payload(raw),
            "contracts": mapper.execution_contracts_from_payload(raw),
        }

    async def resolve_instrument_keys(self, instruments: list[dict]) -> list[dict]:
        """Resolve broker instrument keys for margin instruments.

        Fetches each required expiry's chain ONCE and reads the contract's
        Upstox ``instrument_key`` from the raw payload — never constructed
        from strike text. Legs whose strike/side is missing get
        ``instrument_key: None`` (callers then fail safely instead of
        submitting an invalid broker request).
        """
        by_expiry: dict[str, list[dict]] = {}
        for inst in instruments:
            by_expiry.setdefault(inst["expiry"], []).append(inst)

        resolved: list[dict] = []
        for expiry, insts in by_expiry.items():
            symbol = insts[0]["symbol"]
            key = mapper.broker_key_for(symbol)
            try:
                raw = await self._fetch_raw_chain(key, expiry)
            except UpstoxError as exc:
                raise self._map_error(exc) from exc
            by_strike = {}
            for item in raw.get("data", []):
                strike = item.get("strike_price")
                if strike is not None:
                    by_strike[strike] = item
            for inst in insts:
                item = by_strike.get(inst["strike"])
                side = None
                if item is not None:
                    side = item.get(
                        "call_options" if inst["option_type"] == "call" else "put_options"
                    ) or {}
                resolved.append({**inst, "instrument_key": (side or {}).get("instrument_key")})
        return resolved

    # ---- MARKET DATA ------------------------------------------------------

    async def get_market_status(self, exchange: str) -> dict:
        try:
            return await self._fetch_market_status(exchange)
        except UpstoxError as exc:
            raise self._map_error(exc) from exc

    async def get_option_chain(self, symbol: str, expiry_date: str) -> dict:
        """Canonical option chain (see ``mapper.transform_chain``)."""
        identity = self.resolve_instrument(symbol)
        key = mapper.broker_key_for(identity.symbol)
        try:
            raw = await self._fetch_raw_chain(key, expiry_date)
        except UpstoxError as exc:
            raise self._map_error(exc) from exc
        return mapper.transform_chain(identity.symbol, expiry_date, raw)

    # ---- MARKET QUOTES (wired Day 10) -------------------------------------

    @staticmethod
    def _quote_broker_key(instrument: InstrumentIdentity) -> str:
        """Resolve the Upstox instrument key needed for a market quote.

        Only index/underlying identities have a static broker-key mapping.
        A concrete option/future contract's key is only discoverable from
        chain/contract data, so quoting it directly from its canonical
        identity is impossible here — fail with a canonical error rather
        than inventing a key.
        """
        if instrument.is_concrete_contract:
            raise BrokerError(
                BrokerErrorCode.INVALID_INSTRUMENT,
                "Quote requires a chain-resolved broker instrument key; "
                "concrete contract identities cannot be quoted directly.",
            )
        return mapper.broker_key_for(instrument.symbol)

    @staticmethod
    def _extract_quote_payload(raw: dict, broker_key: str) -> dict:
        """Locate one instrument's quote payload inside the response body.

        The Upstox ``data`` map is normally keyed by the requested
        instrument key, but the documented examples show symbol-style keys
        (e.g. ``NSE_EQ:NHPC``), so we fall back to matching the payload's
        ``instrument_token`` and finally to the single-entry case.
        """
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, dict) or not data:
            raise BrokerError(
                BrokerErrorCode.INVALID_MARKET_DATA,
                "Upstox quote response contained no data payload.",
            )
        if broker_key in data:
            return data[broker_key]
        for key, payload in data.items():
            if isinstance(payload, dict) and payload.get("instrument_token") == broker_key:
                return payload
        if len(data) == 1:
            return next(iter(data.values()))
        raise BrokerError(
            BrokerErrorCode.INVALID_MARKET_DATA,
            f"Upstox quote unavailable for {broker_key}.",
        )

    async def get_quote(self, instrument: InstrumentIdentity):
        """Single canonical market quote (Day 10, wired).

        Returns a :class:`QuoteObservation` — the Day-9 canonical market-data
        contract — never a raw Upstox payload.
        """
        broker_key = self._quote_broker_key(instrument)
        fetcher = self._quote_fetcher or upstox.get_market_quotes
        try:
            raw = await fetcher(self._require_token(), [broker_key])
        except UpstoxError as exc:
            raise self._map_error(exc) from exc
        payload = self._extract_quote_payload(raw, broker_key)
        normalized = mapper.instrument_identity_to_normalized(instrument)
        return mapper.upstox_quote_to_observation(
            payload, normalized, received_at=self._now()
        )

    async def get_quotes(self, instruments: list[InstrumentIdentity]):
        """Batch canonical market quotes (Day 10, wired).

        Returns one :class:`QuoteObservation` per requested instrument, in
        request order — never raw Upstox payloads.
        """
        keys = [self._quote_broker_key(inst) for inst in instruments]
        fetcher = self._quote_fetcher or upstox.get_market_quotes
        try:
            raw = await fetcher(self._require_token(), keys)
        except UpstoxError as exc:
            raise self._map_error(exc) from exc
        observations = []
        for instrument in instruments:
            broker_key = self._quote_broker_key(instrument)
            payload = self._extract_quote_payload(raw, broker_key)
            normalized = mapper.instrument_identity_to_normalized(instrument)
            observations.append(
                mapper.upstox_quote_to_observation(
                    payload, normalized, received_at=self._now()
                )
            )
        return observations

    # ---- ORDERS (prepared, NOT wired) -------------------------------------

    def _not_wired(self, operation: str) -> BrokerError:
        return BrokerError(
            BrokerErrorCode.CAPABILITY_UNSUPPORTED,
            NOT_WIRED_MESSAGE.format(operation=operation),
        )

    def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        raise self._not_wired("order placement")

    def place_orders(self, requests: list[BrokerOrderRequest]) -> list[BrokerOrderResult]:
        raise self._not_wired("multi-order placement")

    def modify_order(self, broker_order_id: str, request: BrokerOrderRequest) -> BrokerOrderResult:
        raise self._not_wired("order modification")

    def cancel_order(self, broker_order_id: str) -> BrokerOrderResult:
        raise self._not_wired("order cancellation")

    def cancel_orders(self, broker_order_ids: list[str]) -> list[BrokerOrderResult]:
        raise self._not_wired("multi-order cancellation")

    def get_order(self, broker_order_id: str) -> BrokerOrderResult:
        raise self._not_wired("order details")

    def get_orders(self) -> list[BrokerOrderResult]:
        raise self._not_wired("order book")

    def get_order_history(self, broker_order_id: str) -> list[BrokerOrderResult]:
        raise self._not_wired("order history")

    # ---- TRADES (NOT WIRED) -----------------------------------------------

    def get_trades(self) -> list[dict]:
        raise self._not_wired("trade history")

    def get_order_trades(self, broker_order_id: str) -> list[dict]:
        raise self._not_wired("order trades")

    def get_trade_history(self) -> list[dict]:
        raise self._not_wired("trade history")

    # ---- PORTFOLIO (NOT WIRED) --------------------------------------------

    def get_positions(self) -> list[dict]:
        raise self._not_wired("position fetch")

    def get_holdings(self) -> list[dict]:
        raise self._not_wired("holdings fetch")


def upstox_capability_matrix() -> list[tuple[str, CapabilityState, bool, str | None]]:
    """Static capability matrix for the Upstox API.

    ``(name, state, wired, detail)`` — ``wired`` is the PLATFORM dimension:
    read-only data capabilities are wired into the current app; order /
    trade / portfolio / streaming capabilities are SUPPORTED by the Upstox
    API but NOT wired in this phase (prepared only).
    """
    return [
        # ---- read-only data (wired) ----
        ("profile", CapabilityState.SUPPORTED, True, "GET /v2/user/profile"),
        ("funds", CapabilityState.SUPPORTED, True, "GET /v3/user/get-funds-and-margin"),
        ("margin", CapabilityState.SUPPORTED, True, "POST /v2/charges/margin"),
        ("market_status", CapabilityState.SUPPORTED, True, "GET /v2/market/status/{exchange}"),
        ("option_chain", CapabilityState.SUPPORTED, True, "GET /v2/option/chain"),
        ("option_contracts", CapabilityState.SUPPORTED, True, "GET /v2/option/contract"),
        # ---- supported by API but NOT wired this phase ----
        ("quotes", CapabilityState.SUPPORTED, True, "GET /v2/market-quote/quotes — wired Day 10 (canonical QuoteObservation)"),
        ("websocket_market_data", CapabilityState.SUPPORTED, False, "Upstox market feed socket — platform uses HTTP polling"),
        ("positions", CapabilityState.SUPPORTED, False, "GET /v2/positions — not wired"),
        ("holdings", CapabilityState.SUPPORTED, False, "GET /v2/portfolio/short-term-holdings — not wired"),
        ("trades", CapabilityState.SUPPORTED, False, "GET /v2/trades — not wired"),
        ("orders", CapabilityState.SUPPORTED, False, "V3 order family — prepared, NOT wired (no live execution)"),
        ("market_orders", CapabilityState.SUPPORTED, False, "V3 place order MARKET"),
        ("limit_orders", CapabilityState.SUPPORTED, False, "V3 place order LIMIT"),
        ("stop_loss", CapabilityState.SUPPORTED, False, "V3 SL"),
        ("stop_loss_market", CapabilityState.SUPPORTED, False, "V3 SL-M"),
        ("modify_order", CapabilityState.SUPPORTED, False, "V3 modify order"),
        ("cancel_order", CapabilityState.SUPPORTED, False, "V3 cancel order"),
        ("multi_order", CapabilityState.SUPPORTED, False, "V3 place multi order"),
        ("order_tags", CapabilityState.SUPPORTED, False, "V3 tag"),
        (
            "native_slicing",
            CapabilityState.SUPPORTED,
            False,
            "V3 broker-native slicing — represented via execution_policy + broker_order_ids",
        ),
        ("market_protection", CapabilityState.SUPPORTED, False, "V3 market_protection"),
        ("after_market_orders", CapabilityState.SUPPORTED, False, "V3 is_amo"),
    ]
