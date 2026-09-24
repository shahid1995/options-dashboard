"""Day 46 — request observability middleware (Issue #92; F15/F16 remediation).

Pure-ASGI middleware (NOT ``BaseHTTPMiddleware``) owning the correlation
boundary. Pure ASGI is required for two correctness properties:

1. **F15 — header on every response.** The correlation header is set
   directly in the ``http.response.start`` message, so it is present on
   normal responses, handled errors AND unhandled-exception 500s —
   without re-raising or wrapping application internals.
2. **F16 — reliable actor identity.** The downstream request runs in
   THIS task (no task hop through ``call_next``), so ``ContextVar``
   mutations made by authentication dependencies while resolving the
   user are visible here when the response completes.

Behavior:

1. adopt the incoming ``X-Correlation-Id`` when it passes
   :func:`app.structlog_config.is_safe_correlation_id` (conservative
   charset + credential-vocabulary refusal), else mint a fresh ID;
2. bind it via :mod:`app.structlog_config` for the request/task;
3. emit ONE structured, sanitized JSON access-log record per request
   (route/method/status/duration/correlation ID/safe user facts) —
   including on unhandled exceptions;
4. set the ``X-Correlation-Id`` response header unconditionally.

The middleware NEVER logs or echoes header/cookie VALUES. It does not
participate in authorization — ``CurrentUser``/``AdminUser`` are
untouched, so a correlation ID can never authenticate a request.
"""

from __future__ import annotations

import logging
import time

from app.structlog_config import (
    ACCESS_LOGGER_NAME,
    correlation_id,
    new_correlation_id,
    request_log_fields,
    set_correlation_id,
)

CORRELATION_HEADER = "X-Correlation-Id"


def _adoptable(value: str | None) -> bool:
    from app.structlog_config import is_safe_correlation_id

    return is_safe_correlation_id(value)


_access_logger = None


def _logger():
    global _access_logger
    if _access_logger is None:
        _access_logger = logging.getLogger(ACCESS_LOGGER_NAME)
    return _access_logger


class CorrelationIdMiddleware:
    """Pure-ASGI correlation/observability middleware (F15/F16)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        supplied = None
        for name, value in scope.get("headers") or []:
            if name.decode("latin-1").lower() == CORRELATION_HEADER.lower():
                supplied = value.decode("latin-1")
                break

        correlation = supplied if _adoptable(supplied) else new_correlation_id()
        set_correlation_id(correlation)

        # F16: share the ASGI scope with request.state so authentication
        # dependencies can record safe actor facts on it. Sync endpoints
        # run in a threadpool where ContextVar mutations do NOT propagate
        # back to this task — scope state does.
        if "state" not in scope:
            scope["state"] = {}

        start = time.perf_counter()
        state = {"status": None}
        default_send = send

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                headers = message.setdefault("headers", [])
                marker = CORRELATION_HEADER.lower().encode("latin-1")
                # Exactly one correlation header per response.
                headers[:] = [h for h in headers if h[0].lower() != marker]
                headers.append((marker, correlation.encode("latin-1")))
            await default_send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Unhandled application exception: emit the access log here
            # (context still bound) and re-raise. The OUTER
            # _CorrelationServerError layer produces the 500 response with
            # the same correlation ID and clears the context afterwards.
            _emit_log(scope, state, start, correlation, failed=True)
            raise

        _emit_log(scope, state, start, correlation, failed=False)
        set_correlation_id(None)


def _emit_log(scope, state, start, correlation, *, failed: bool) -> None:
    try:
        import logging as _logging

        from app.structlog_config import emit_structured, request_log_fields

        duration_ms = (time.perf_counter() - start) * 1000.0
        path = scope.get("path", "")
        method = scope.get("method", "")
        status_code = state.get("status") or (500 if failed else 0)
        emit_structured(
            _logger(),
            _logging.ERROR if failed else _logging.INFO,
            f"{method} {path} {status_code}",
            **request_log_fields(
                method=method,
                route=path,
                status_code=status_code,
                duration_ms=duration_ms,
                correlation=correlation,
                user_facts=(scope.get("state") or {}).get("user_facts"),
            ),
        )
    except Exception:  # logging must never break the request
        pass


def install_correlation_middleware(app) -> None:
    """Install the correlation middleware plus the unhandled-500 responder.

    Stack order (outermost first): ``_CorrelationServerError`` ABOVE
    ``CorrelationIdMiddleware`` — the server-error responder sees the
    correlation context still bound, strips any outgoing correlation
    headers, and stamps EXACTLY ONE with the request's ID on the
    server-generated 500. Normal responses are stamped only by
    ``CorrelationIdMiddleware.send_wrapper``. Clearing the correlation
    context happens exclusively in the outermost layer, after the final
    response is fully sent.

    Error BODY delegation (Day 43 contract): the responder renders the
    error using the app's registered ``Exception`` handler, so unhandled
    exceptions on ``/api/v1`` produce the canonical INTERNAL_ERROR
    envelope and unversioned routes keep Starlette's native 500 — while
    the correlation header is stamped on both. Delegating is REQUIRED:
    a ``ServerErrorMiddleware`` subclass added via ``add_middleware``
    shadows FastAPI's own outer server-error layer and would otherwise
    erase the versioned error contract.
    """

    from starlette._utils import is_async_callable

    from starlette.concurrency import run_in_threadpool
    from starlette.middleware.errors import ServerErrorMiddleware
    from starlette.requests import Request

    v1_error_handler = app.exception_handlers.get(Exception)

    def _is_v1(scope) -> bool:
        from app.api.v1 import API_VERSION_PREFIX

        path = scope.get("path", "")
        return path == API_VERSION_PREFIX or path.startswith(API_VERSION_PREFIX + "/")

    class _CorrelationServerError(ServerErrorMiddleware):
        """500 responder that echoes the request's correlation ID (F15)
        and delegates the error body to the app's ``Exception`` handler
        (Day 43 envelope on ``/api/v1``, native 500 elsewhere).
        """

        def __init__(self, asgi_app, *, debug: bool = False):
            super().__init__(asgi_app, handler=None, debug=debug)

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            response_started = False

            async def stamp(message):
                nonlocal response_started
                if message["type"] == "http.response.start":
                    response_started = True
                    # Read the correlation ID LAZILY: at this point the
                    # inner CorrelationIdMiddleware has already bound it
                    # (same async task, pure ASGI — no task hop), so the
                    # 500 generated for an unhandled exception carries
                    # the request's ID. An eager read would see None (or
                    # a stale prior value) because the outer layer runs
                    # BEFORE the inner one.
                    correlation = correlation_id()
                    if correlation:
                        headers = message.setdefault("headers", [])
                        marker = CORRELATION_HEADER.lower().encode("latin-1")
                        # Exactly one correlation header: replace any
                        # existing values rather than appending a dupe.
                        headers[:] = [h for h in headers if h[0].lower() != marker]
                        headers.append((marker, correlation.encode("latin-1")))
                await send(message)

            try:
                await self.app(scope, receive, stamp)
            except Exception as exc:
                if response_started:
                    # A response already went out; a 500 can no longer
                    # be sent — re-raise exactly like ServerErrorMiddleware.
                    raise
                request = Request(scope)
                if v1_error_handler is not None and _is_v1(scope):
                    if is_async_callable(v1_error_handler):
                        response = await v1_error_handler(request, exc)
                    else:
                        response = await run_in_threadpool(v1_error_handler, request, exc)
                elif self.debug:
                    response = self.debug_response(request, exc)
                else:
                    response = self.error_response(request, exc)
                await response(scope, receive, stamp)
                # Keep ServerErrorMiddleware's contract: always re-raise
                # so servers/test clients observe the original failure.
                raise
            finally:
                set_correlation_id(None)

    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(_CorrelationServerError, debug=app.debug)


__all__ = [
    "CORRELATION_HEADER",
    "CorrelationIdMiddleware",
    "install_correlation_middleware",
]
