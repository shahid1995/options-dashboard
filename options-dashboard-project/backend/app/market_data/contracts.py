"""Canonical Market-Data Contracts (Day 9).

These dataclasses define the **stable domain boundary** between external
data sources (broker adapters, datasets, imports) and every downstream
subsystem (Data Quality → Quant → Intelligence → Opportunity → Strategy).

Design rules
------------
1. **No broker-specific field names.**  Upstox ``instrument_key``,
   ``transaction_type``, ``call_options`` / ``put_options`` etc. exist
   ONLY inside ``app.brokers.adapters.<broker>``.  Adapters map them
   to/from these contracts at the boundary.

2. **Missing values stay ``None``.**  We never fabricate 0 for a field
   whose value is unknown — ``None`` means *not available*, 0 means
   *observed as zero*.

3. **Two timestamps, not one.**  ``market_timestamp`` is the
   exchange/event time; ``received_timestamp`` is when the application
   received the observation.  They must never be conflated.

4. **OI is contracts, not lots** unless the source explicitly defines
   another unit.  The contract documents this semantic; conversion is
   the caller's responsibility.

5. **Broker Greeks ≠ Model Greeks.**  The source field on
   :class:`GreeksObservation` distinguishes broker-provided values from
   StrikeNova model-calculated values.  They may differ and must never
   silently overwrite each other.

6. **IV is a canonical decimal fraction.**  0.1824 means 18.24%.  Never
   store the raw broker percent.

7. **Contract versioning.**  Every observation carries
   :attr:`MarketObservation.contract_version` so future broker/API
   changes can be handled without silently changing historical semantics.

8. **Provenance is mandatory.**  Every observation must be traceable to
   its source, collection mode and normalization pipeline version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ===========================================================================
# Enums
# ===========================================================================


class Side(str, Enum):
    """Canonical CE/PE representation for option contracts."""

    CALL = "CALL"
    PUT = "PUT"


class DataMode(str, Enum):
    """Source / data-mode semantics.

    ``BROKER_LIVE`` means real-time streaming from a connected broker.
    ``BROKER_SNAPSHOT`` means a point-in-time fetch (e.g. ``get_quote``).
    ``HISTORICAL`` means admin-curated historical datasets.
    ``IMPORTED`` means externally supplied data loaded into the platform.
    ``REPLAY`` means re-feeding previously captured observations.
    ``TEST`` means synthetic / test-only data.

    Delayed data MUST NOT be represented as ``BROKER_LIVE``.
    """

    BROKER_LIVE = "BROKER_LIVE"
    BROKER_SNAPSHOT = "BROKER_SNAPSHOT"
    HISTORICAL = "HISTORICAL"
    IMPORTED = "IMPORTED"
    REPLAY = "REPLAY"
    TEST = "TEST"


class QualityState(str, Enum):
    """Observation quality classification per Blueprint §8."""

    EXCELLENT = "EXCELLENT"
    GOOD = "GOOD"
    DEGRADED = "DEGRADED"
    INSUFFICIENT = "INSUFFICIENT"


class ContractVersion(str, Enum):
    """Explicit versioning for the canonical market-data contract.

    When the contract changes in a backward-incompatible way the version
    is bumped.  Observations always carry the contract version they
    conform to so downstream consumers can validate compatibility.
    """

    v1_0_0 = "1.0.0"


# ===========================================================================
# Instrument Identity
# ===========================================================================


@dataclass(frozen=True)
class NormalizedInstrument:
    """Canonical, broker-neutral instrument identity.

    This is the platform's universal instrument identity — **not** a broker
    key.  Broker keys (Upstox ``instrument_key``, Zerodha
    ``instrument_token``, …) live in
    :class:`app.brokers.domain.models.BrokerInstrumentMapping` and are
    resolved at the adapter boundary.

    For an underlying/index identity (no concrete contract), ``expiry``,
    ``strike`` and ``option_type`` stay ``None``.  ``lot_size`` and
    ``tick_size`` are populated only when known — never fabricated.
    """

    exchange: str
    segment: str
    underlying: str
    symbol: str
    instrument_type: str = "INDEX"
    expiry: str | None = None       # YYYY-MM-DD
    strike: float | None = None
    option_type: Side | None = None  # CALL | PUT
    lot_size: int | None = None
    tick_size: float | None = None

    @property
    def is_concrete_contract(self) -> bool:
        """True when the identity represents a tradeable option/future
        contract (has expiry + strike + option type)."""
        return self.expiry is not None and self.strike is not None and self.option_type is not None


# ===========================================================================
# Price Fields
# ===========================================================================


@dataclass(frozen=True)
class PriceQuote:
    """Canonical price fields for a single instrument at a point in time.

    **Units / semantics**

    * ``ltp`` — Last Traded Price (exchange-reported).
    * ``open / high / low / close`` — OHLC for the current session or
      candle window, depending on the data mode.
    * ``bid / ask`` — Best bid / ask if the source provides a book.
    * ``bid_quantity / ask_quantity`` — Size at the best bid / ask level.
    * ``volume`` — Cumulative traded volume for the session/candle.
    * ``source`` — Who supplied these price values (e.g. ``"BROKER"``,
      ``"STRIKENOVA_DATASET"``).

    Missing fields stay ``None`` — never fabricated to 0.
    """

    ltp: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_quantity: int | None = None
    ask_quantity: int | None = None
    volume: float | None = None
    oi: float | None = None  # Open Interest (contracts, not lots)
    source: str | None = None
    # Observed option analytics (Issue #80 prospective research capture).
    # Populated ONLY from broker-reported values (never computed here);
    # unit conventions are documented per adapter mapper:
    #   iv    — canonical decimal fraction (0.18 = 18%), converted from the
    #           broker's percentage convention at the mapper boundary.
    #   delta — dimensionless, pass-through.
    #   gamma — dimensionless (per point), pass-through.
    # vega/theta are deliberately NOT mapped: their broker unit conventions
    # (per-day vs annualized, per-point vs per-vol-point) are not verified,
    # and silently converting units would fabricate semantics. Raw broker
    # values remain available in the adapter payload for audited extraction.
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    # Exchange/event time of this quote as reported by the source (Issue #80
    # cutoff integrity).  None when the payload carries no event timestamp —
    # NEVER synthesized from receive time; research capture refuses
    # observations that cannot prove their observation time.
    event_timestamp: datetime | None = None


# ===========================================================================
# Market Observation
# ===========================================================================


@dataclass(frozen=True)
class Provenance:
    """Observation provenance metadata.

    Every canonical observation must carry enough metadata to answer:
    *Where did this data come from, when was it received, and which
    normalization/version produced it?*
    """

    source: str
    collection_mode: str  # DataMode value or custom string
    received_at: datetime
    normalization_version: str
    contract_version: str
    transformation_id: str | None = None


@dataclass(frozen=True)
class MarketObservation:
    """A single normalized market observation.

    This is the **core contract** that every downstream consumer (Data
    Quality, Quant, Intelligence, Opportunity) receives.  It contains:

    * :attr:`instrument` — what was observed (identity only, no price)
    * :attr:`market_timestamp` — exchange/event time
    * :attr:`received_timestamp` — application receive time
    * :attr:`source` — data provider label
    * :attr:`data_mode` — live / snapshot / historical / …
    * :attr:`sequence_id` — optional sequence for streaming sources
    * :attr:`quality` — data quality classification
    * :attr:`provenance` — detailed traceability metadata
    * :attr:`contract_version` — schema version of this observation

    **Broker payloads do NOT leak into this contract.**  Adapters
    normalize broker-specific fields *before* constructing a
    ``MarketObservation``.
    """

    instrument: NormalizedInstrument
    market_timestamp: datetime
    received_timestamp: datetime
    source: str
    data_mode: DataMode
    sequence_id: int | None = None
    quality: QualityState | None = None
    provenance: Provenance | None = None
    contract_version: ContractVersion = ContractVersion.v1_0_0


# ===========================================================================
# Option Chain
# ===========================================================================


@dataclass(frozen=True)
class OptionChainRow:
    """One strike row in a canonical option chain.

    ``call`` and ``put`` are each independently optional — a chain row
    can have only one side when the source provides partial data.

    **OI semantics:** Open interest is expressed in *contracts*, not lots,
    unless the source explicitly defines another unit.  The contract
    documents this; conversion is the caller's responsibility.
    """

    strike: float
    call: PriceQuote | None = None
    put: PriceQuote | None = None


@dataclass(frozen=True)
class OptionChainObservation:
    """Canonical option-chain observation for one expiry.

    Combines the chain rows with observation-level metadata (timestamps,
    source, data mode, quality).  The chain is already sorted by strike
    ascending.
    """

    symbol: str
    expiry_date: str
    underlying_spot_price: float | None
    chain: list[OptionChainRow] = field(default_factory=list)
    market_timestamp: datetime | None = None
    received_timestamp: datetime | None = None
    source: str | None = None
    data_mode: DataMode | None = None
    quality: QualityState | None = None
    contract_version: ContractVersion = ContractVersion.v1_0_0


# ===========================================================================
# Single-instrument Quote Observation
# ===========================================================================


@dataclass(frozen=True)
class QuoteObservation:
    """A single-instrument normalized quote observation (Day 10).

    Composes the Day-9 canonical vocabulary for the REST-quote read path:

    * :attr:`instrument` — canonical broker-neutral identity
    * :attr:`quote` — canonical price fields (LTP/OHLC/bid/ask/volume/OI)
    * :attr:`market_timestamp` — exchange/event time (None when the source
      provides no event timestamp; never silently replaced by receive time)
    * :attr:`received_timestamp` — when the application received the quote
    * :attr:`source` — data provider label (e.g. ``"UPSTOX"``)
    * :attr:`data_mode` — snapshot vs live semantics
    * :attr:`provenance` — traceability metadata
    * :attr:`contract_version` — schema version of this observation

    Broker payloads (envelopes, ``depth``, ``last_price``, instrument
    tokens) never leak into this contract — adapters map them away before
    constructing a ``QuoteObservation``.
    """

    instrument: NormalizedInstrument
    quote: PriceQuote
    market_timestamp: datetime | None = None
    received_timestamp: datetime | None = None
    source: str | None = None
    data_mode: DataMode | None = None
    quality: QualityState | None = None
    provenance: Provenance | None = None
    contract_version: ContractVersion = ContractVersion.v1_0_0


# ===========================================================================
# Greeks / IV Boundary
# ===========================================================================


@dataclass(frozen=True)
class GreeksObservation:
    """Greeks and IV for a single option at a point in time.

    **Broker vs Model separation:**

    * ``source = "BROKER"`` — values provided by the broker adapter
      (e.g. Upstox ``option_greeks``).
    * ``source = "MODEL"`` — values calculated by the StrikeNova
      quantitative engine (e.g. Black-Scholes).

    Both may exist for the same instrument at the same timestamp.  They
    are stored as **separate observations** and must never silently
    overwrite each other.

    **IV** is a canonical decimal fraction (0.1824 = 18.24%).  Never
    store the raw broker percent.

    **Per-unit Greek convention:**
    * ``delta`` — per unit (share/lot)
    * ``gamma`` — per unit per unit
    * ``theta`` — annualized per unit
    * ``vega`` — per 1.00 vol move per unit

    Missing Greek fields stay ``None`` — brokers may provide partial data.
    """

    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    source: str  # "BROKER" | "MODEL"
    calc_model: str | None = None     # e.g. "BLACK_SCHOLES_EUROPEAN"
    calc_version: str | None = None   # e.g. "1.0.0"
