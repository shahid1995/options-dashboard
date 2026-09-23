"""Day 46 — request observability middleware (Issue #92).

One ASGI middleware owns the correlation boundary:

1. adopt the incoming ``X-Correlation-Id`` when present (stability for
   distributed traces), otherwise mint a fresh ID;
2. bind it to the request's context via :mod:`app.structlog_config`;
3. echo it on the response so clients/tests can correlate;
4. emit ONE structured, sanitized JSON access-log record per request
   (route/method/status/duration/correlation ID/safe user facts).

The middleware NEVER logs or echoes header/cookie VALUES — only the
correlation ID (an opaque, non-secret tracer) crosses the boundary. It
does not participate in authorization: ``CurrentUser`` and ``AdminUser``
are untouched, so a correlation ID can never authenticate a request.
"""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.structlog_config import (
    ACCESS_LOGGER_NAME,
    correlation_id,
    new_correlation_id,
    request_log_fields,
    set_correlation_id,
)

CORRELATION_HEADER = "X-Correlation-Id"

# Day 46 security: an adopted correlation ID must be a sane tracer AND
# free of credential vocabulary (structlog_config.is_safe_correlation_id).
# Anything else is refused and a fresh ID is minted, so credential-shaped
# material can never ride into logs or operational events via this header.


def _adoptable(value: str | None) -> bool:
    from app.structlog_config import is_safe_correlation_id

    return is_safe_correlation_id(value)


_access_logger = None


def _logger():
    global _access_logger
    if _access_logger is None:
        import logging

        _access_logger = logging.getLogger(ACCESS_LOGGER_NAME)
    return _access_logger


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        supplied = request.headers.get(CORRELATION_HEADER)
        correlation = supplied if _adoptable(supplied) else new_correlation_id()
        set_correlation_id(correlation)

        start = time.perf_counter()
        response = None
        try:
            response = await call_next(request)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            route = request.url.path
            status_code = response.status_code if response is not None else 500
            try:
                from app.structlog_config import emit_structured

                emit_structured(
                    _logger(),
                    logging.INFO if response is not None else logging.ERROR,
                    f"{method_of(request)} {route} {status_code}",
                    **request_log_fields(
                        method=method_of(request),
                        route=route,
                        status_code=status_code,
                        duration_ms=duration_ms,
                        correlation=correlation,
                    ),
                )
            except Exception:  # logging must never break the request
                pass
            set_correlation_id(None)

        if response is not None:
            response.headers[CORRELATION_HEADER] = correlation
        return response


def method_of(request: Request) -> str:
    return request.method


__all__ = ["CORRELATION_HEADER", "CorrelationIdMiddleware"]
