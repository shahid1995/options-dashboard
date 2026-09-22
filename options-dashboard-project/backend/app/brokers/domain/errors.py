"""Canonical broker error taxonomy (Phase 6.5.0.2).

Application/domain code catches :class:`BrokerError` — a stable,
broker-neutral error with a :class:`BrokerErrorCode`. Provider-specific
exceptions (e.g. UpstoxError) are mapped to this taxonomy INSIDE each
adapter and never escape the adapter boundary.

The canonical code is the stable contract. ``status_code`` and
``metadata`` carry safe upstream diagnostic detail (the broker's HTTP
status, a structured error body without credentials) so useful information
is never destroyed, while secrets are never included.
"""

from __future__ import annotations

from enum import Enum


class BrokerErrorCode(str, Enum):
    """Stable application-level broker error taxonomy.

    ``UPSTREAM_ERROR`` is the honest fallback for a provider failure that
    does not map to a specific taxonomy entry — never guessed into a
    more specific code.
    """

    AUTH_REQUIRED = "AUTH_REQUIRED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK_ERROR = "NETWORK_ERROR"
    MAINTENANCE = "MAINTENANCE"
    INVALID_INSTRUMENT = "INVALID_INSTRUMENT"
    INVALID_QUANTITY = "INVALID_QUANTITY"
    INVALID_PRICE = "INVALID_PRICE"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    ORDER_ALREADY_FINAL = "ORDER_ALREADY_FINAL"
    ACCOUNT_RESTRICTED = "ACCOUNT_RESTRICTED"
    SEGMENT_DISABLED = "SEGMENT_DISABLED"
    STATIC_IP_REQUIRED = "STATIC_IP_REQUIRED"
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
    BROKER_UNKNOWN = "BROKER_UNKNOWN"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    # A broker payload was received but is malformed / unusable for
    # canonical market-data normalization (e.g. a quote with no price, an
    # option chain row without a strike). Distinct from UPSTREAM_ERROR,
    # which means the broker/API call itself failed.
    INVALID_MARKET_DATA = "INVALID_MARKET_DATA"
    # A market-data request had no usable source: no session-bound adapter
    # was supplied, the source provider produced none, or no registered
    # source can satisfy the request. Distinct from BROKER_UNKNOWN (the
    # broker id itself is unrecognized).
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"

    # Codes that mean the caller's broker session is unusable and should be
    # re-authenticated. Routers use this to clear the stored session token.
    SESSION_CODES = frozenset({AUTH_REQUIRED, TOKEN_EXPIRED})


class BrokerError(Exception):
    """A structured, broker-neutral broker failure.

    Attributes:
        code: stable :class:`BrokerErrorCode`.
        message: human-readable, safe message (never a stack trace, never a
            credential, never an internal error string).
        status_code: the upstream HTTP status when one exists (safe metadata).
        metadata: extra safe structured detail (e.g. broker error-body keys);
            must never contain credentials.
    """

    def __init__(
        self,
        code: BrokerErrorCode | str,
        message: str,
        status_code: int | None = None,
        metadata: dict | None = None,
    ):
        self.code = BrokerErrorCode(code)
        self.message = message
        self.status_code = status_code
        self.metadata = dict(metadata or {})
        super().__init__(f"{self.code.value}: {message}")

    def __repr__(self) -> str:  # never include credentials/metadata in repr
        return f"BrokerError(code={self.code.value!r}, message={self.message!r})"


def is_session_code(code: BrokerErrorCode | str) -> bool:
    """True when the code means the broker session must be re-established."""
    return BrokerErrorCode(code) in BrokerErrorCode.SESSION_CODES


# ---------------------------------------------------------------------------
# Public error boundary — sanitize broker error messages for API responses.
#
# BrokerError.message can contain raw upstream data (e.g. portions of
# Upstox response bodies, network exception details with URLs). The public
# API must NEVER expose these details to clients.
# ---------------------------------------------------------------------------

# Stable generic public message for broker/upstream failures.
# Used by all router error handlers to sanitize the public error detail.
PUBLIC_BROKER_ERROR_MESSAGE = "Upstream market-data provider request failed."


def sanitize_broker_error_message(exc: BrokerError) -> str:
    """Return a safe, stable public message for a BrokerError.

    The detailed message is preserved in server-side logs; the public
    response gets a generic message that reveals nothing about the upstream
    provider, internal URLs, or raw response bodies.
    """
    return PUBLIC_BROKER_ERROR_MESSAGE
