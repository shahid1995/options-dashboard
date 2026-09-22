"""Day 43 — canonical error envelope for the versioned API surface.

One machine-readable error shape for every failure class on ``/api/v1``:

    {"error": {"code": <stable token>, "message": <human diagnostic>,
               "status": <http status>, "details": [...] (optional)}}

The envelope applies ONLY to the versioned surface (``/api/v1/...``).
Requests outside ``/api/v1`` are delegated to FastAPI's NATIVE handlers
(``http_exception_handler`` / ``request_validation_exception_handler``),
and unhandled exceptions on unversioned routes are RE-RAISED so
Starlette's ServerErrorMiddleware handles them exactly as before Day 43
— the unversioned error contract is untouched.

Sensitive internals (exception text, stack traces, upstream secrets) are
never included on the versioned surface; the full exception is logged
server-side only.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# Stable machine-readable error codes (the versioned API contract).
VALIDATION_ERROR = "VALIDATION_ERROR"
UNAUTHENTICATED = "UNAUTHENTICATED"
FORBIDDEN = "FORBIDDEN"
NOT_FOUND = "NOT_FOUND"
METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
UPSTREAM_ERROR = "UPSTREAM_ERROR"
INTERNAL_ERROR = "INTERNAL_ERROR"

_STATUS_TO_CODE = {
    400: VALIDATION_ERROR,
    401: UNAUTHENTICATED,
    403: FORBIDDEN,
    404: NOT_FOUND,
    405: METHOD_NOT_ALLOWED,
    422: VALIDATION_ERROR,
    502: UPSTREAM_ERROR,
    503: UPSTREAM_ERROR,
    504: UPSTREAM_ERROR,
}


def error_envelope(
    *,
    status_code: int,
    code: str,
    message: str,
    details: list | None = None,
) -> dict:
    envelope: dict = {
        "error": {"code": code, "message": message, "status": status_code}
    }
    if details:
        envelope["error"]["details"] = details[:10]
    return envelope


def install_v1_error_handlers(app: FastAPI) -> None:
    """Register the canonical handlers, scoped to the versioned surface."""

    def _is_v1(request: Request) -> bool:
        from app.api.v1 import API_VERSION_PREFIX

        return request.url.path.startswith(API_VERSION_PREFIX + "/") or (
            request.url.path == API_VERSION_PREFIX
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        if not _is_v1(request):
            # Native FastAPI handling for unversioned routes — delegated,
            # not reconstructed (remediation contract #1).
            return await http_exception_handler(request, exc)
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        code = getattr(exc, "error_code", None) or _STATUS_TO_CODE.get(
            exc.status_code,
            INTERNAL_ERROR if exc.status_code >= 500 else FORBIDDEN,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(
                status_code=exc.status_code, code=code, message=detail,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        if not _is_v1(request):
            # Native FastAPI validation contract for unversioned routes —
            # delegated, not replaced (remediation contract #2).
            return await request_validation_exception_handler(request, exc)
        details = [
            {
                "loc": [str(x) for x in err.get("loc", [])],
                "message": err.get("msg", ""),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_envelope(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                code=VALIDATION_ERROR,
                message="Request validation failed.",
                details=details,
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception):
        if not _is_v1(request):
            # Preserve the application's NATIVE unhandled-exception path
            # (Starlette ServerErrorMiddleware → plain-text 500) for
            # unversioned routes (remediation contract #3): re-raise so
            # the normal server handling takes over.
            raise exc
        logger.exception(
            "Unhandled API error on %s %s", request.method, request.url.path
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_envelope(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                code=INTERNAL_ERROR,
                message="An internal error occurred.",
            ),
        )
