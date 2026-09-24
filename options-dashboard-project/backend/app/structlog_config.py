"""Day 46 — observability core (Issue #92): correlation IDs, structured
secret-safe logging, and the shared payload sanitizer.

Correlation context
-------------------
``correlation_id()`` / ``set_correlation_id()`` are backed by a
:class:`contextvars.ContextVar`, so each request/task carries its own
value and concurrent operations never share one. The ASGI middleware in
``app.middleware`` generates (or adopts) the ID at the request boundary
and keeps it stable for the request; background operations call
``new_correlation_id()`` to mint their own operation ID.

Structured access logs
----------------------
``ACCESS_LOGGER_NAME`` is the logger the request middleware writes one
JSON record to per request:

    {"ts", "level", "logger", "correlation_id", "route", "method",
     "status", "duration_ms", "user_id"?, "tenant"?}

``json_formatter`` produces that record and attaches it to the log
record as ``record.structured_json`` (tests assert on the parsed dict).

Secret safety
-------------
``SANITIZE`` never logs, persists, or returns credential material:

* keys that ARE credential names (``access_token``, ``api_key``,
  ``password``, ``authorization``…) are dropped entirely;
* long opaque strings carrying credential vocabulary are replaced with
  ``"<redacted>"``;
* session-identifier-shaped values are dropped from headers/cookies —
  request headers and cookies are NEVER copied into log records.

A correlation ID is never treated as authorization material anywhere:
``CurrentUser``/``AdminUser`` continue to resolve only the
``strikenova_session`` cookie (privileged surfaces) or the intentionally
supported legacy header for ordinary non-admin endpoints.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timezone

ACCESS_LOGGER_NAME = "strikenova.access"

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)


def new_correlation_id() -> str:
    """Mint a fresh correlation/operation ID (request or background op)."""
    return f"corr-{uuid.uuid4().hex}"


def set_correlation_id(value: str | None) -> None:
    """Bind the correlation ID for the current context."""
    _correlation_id.set(value)


def correlation_id() -> str | None:
    """The current context's correlation ID (None outside a request/op)."""
    return _correlation_id.get()


# --------------------------------------------------------------------------
# Sanitization (shared by logs, notification payloads, operational events)
# --------------------------------------------------------------------------

REDACTED = "<redacted>"

_SECRETISH_KEY = re.compile(
    r"(token|secret|credential|password|passwd|api[-_]?key|apikey|authorization|cookie|session)",
    re.IGNORECASE,
)
_SECRETISH_VALUE = re.compile(r"\b[A-Za-z0-9_\-]{28,}\b")


# Correlation IDs are minted tracers (corr-<hex>), not secrets — they are
# exempt from the generic opaque-blob value redaction so they survive in
# structured logs and event payloads (still validated at the boundary via
# is_safe_correlation_id). Match the adopted/created form exactly.
_CORRELATION_ID_SHAPE = re.compile(r"^corr-[0-9a-f]{32}$")

# Durable user IDs are hyphenated UUIDs (str(uuid4())) — safe, non-secret
# actor identifiers that MUST survive into the access log (F16). The
# hyphen groups make this shape distinct from opaque secrets: session IDs
# are single 43-char token_urlsafe blobs and never match. The exact UUID
# shape is required so invented long blobs can never borrow the exemption.
_USER_ID_SHAPE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def sanitize(value, depth: int = 0):
    """Recursively remove/redact credential-shaped material.

    Keys that ARE credential names are dropped entirely; long opaque
    credential-vocabulary strings are replaced by ``REDACTED``. Safe
    scalar configuration survives verbatim. Valid correlation IDs are
    exempt from opaque-blob redaction (they are non-secret tracers).
    """
    if depth > 6:
        return None
    if isinstance(value, dict):
        clean: dict = {}
        for k, v in value.items():
            if _SECRETISH_KEY.search(str(k)):
                continue
            clean[str(k)] = sanitize(v, depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [sanitize(v, depth + 1) for v in value[:20]]
    if isinstance(value, str):
        if _CORRELATION_ID_SHAPE.fullmatch(value):
            return value
        if _USER_ID_SHAPE.fullmatch(value):
            return value
        if _SECRETISH_KEY.search(value) and len(value) >= 24 and " " not in value:
            return REDACTED
        return _SECRETISH_VALUE.sub(REDACTED, value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _safe_user_facts(request_facts: dict | None = None) -> dict:
    """Best-effort, never-secret user facts for the access log.

    Facts recorded on the ASGI scope (``request.state``) take precedence —
    they are visible to the middleware task even for sync (threadpool)
    endpoints. The ContextVar fallback covers async paths and background
    operations.
    """
    if request_facts:
        return request_facts
    from app.routers.deps import current_request_user_facts

    return current_request_user_facts()


# --------------------------------------------------------------------------
# JSON log formatter
# --------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """Format records as one JSON object with the Day 46 contract fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": correlation_id(),
            "message": record.getMessage(),
        }
        extra = getattr(record, "structured_fields", None)
        if isinstance(extra, dict):
            payload.update(sanitize(extra))
        record.structured_json = payload  # tests read the parsed dict
        return json.dumps(payload, default=str)


def emit_structured(logger: logging.Logger, level: int, message: str, **fields) -> None:
    """Emit one structured record with sanitized Day 46 fields."""
    logger.log(level, message, extra={"structured_fields": fields})


def configure_logging() -> None:
    """Attach the JSON formatter to the access logger (idempotent)."""
    access_logger = logging.getLogger(ACCESS_LOGGER_NAME)
    if getattr(access_logger, "_day46_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    access_logger.addHandler(handler)
    access_logger.setLevel(logging.INFO)
    access_logger.propagate = True  # caplog/regression suites capture records
    access_logger._day46_configured = True


# --------------------------------------------------------------------------
# Header/cookie hygiene — what may NEVER reach a log record
# --------------------------------------------------------------------------

_NEVER_LOG_HEADERS = {"authorization", "cookie", "x-session-id", "set-cookie"}


def safe_header_summary(headers) -> dict:
    """Return header names only (never values) for the permitted subset."""
    safe: dict = {}
    for name in headers.keys():
        lowered = str(name).lower()
        if lowered in _NEVER_LOG_HEADERS:
            continue
        safe[lowered] = REDACTED
    return safe


def request_log_fields(
    *,
    method: str,
    route: str,
    status_code: int,
    duration_ms: float,
    correlation: str | None = None,
    user_facts: dict | None = None,
) -> dict:
    """Build the Day 46 request-log field set (sanitized, secret-free)."""
    fields = {
        "route": route,
        "method": method,
        "status": status_code,
        "duration_ms": round(duration_ms, 2),
        "correlation_id": correlation or correlation_id(),
    }
    try:
        fields.update(_safe_user_facts(user_facts))
    except Exception:  # never fail a request because logging facts failed
        pass
    return fields


_CONSERVATIVE_CORRELATION = re.compile(r"^[A-Za-z0-9._:@-]{8,128}$")


def is_safe_correlation_id(value: str | None) -> bool:
    """True when a client-supplied correlation ID may be adopted.

    Two gates: a conservative character set/length, and refusal of any
    value carrying credential vocabulary — credential-shaped strings can
    never ride into logs or operational events through this header.
    """
    if not value or not _CONSERVATIVE_CORRELATION.fullmatch(value):
        return False
    return not _SECRETISH_KEY.search(value)


__all__ = [
    "ACCESS_LOGGER_NAME",
    "JsonFormatter",
    "REDACTED",
    "configure_logging",
    "correlation_id",
    "emit_structured",
    "is_safe_correlation_id",
    "new_correlation_id",
    "request_log_fields",
    "safe_header_summary",
    "sanitize",
    "set_correlation_id",
]
