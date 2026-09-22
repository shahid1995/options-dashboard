"""Day 43 — canonical error envelope for the versioned API surface.

One machine-readable error shape for every failure class on ``/api/v1``:

    {"error": {"code": <stable token>, "message": <human diagnostic>,
               "status": <http status>, "details": [...] (optional)}}

The envelope applies ONLY to the versioned surface (``/api/v1/...``);
unversioned routes keep FastAPI's native error shape so existing
consumers are unaffected (backward compatibility).

Sensitive internals (exception text, stack traces, upstream secrets) are
never included; the full exception is logged server-side only.
"""
from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Request, status
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


def _json_safe_errors(errors: list) -> list:
    """Make FastAPI validation errors JSON-serializable for the native
    (unversioned) response shape — pydantic v2 puts non-serializable
    objects (e.g. ValueError) into ``ctx``."""
    safe: list = []
    for err in errors:
        if isinstance(err, dict):
            clean = {}
            for key, value in err.items():
                try:
                    json.dumps(value)
                    clean[key] = value
                except (TypeError, ValueError):
                    clean[key] = str(value)
            safe.append(clean)
        else:
            safe.append(str(err))
    return safe


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
            # Native shape for unversioned routes (backward compatibility).
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=getattr(exc, "headers", None),
            )
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
            # Native FastAPI shape for unversioned routes (compatibility).
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"detail": _json_safe_errors(exc.errors())},
            )
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
        logger.exception(
            "Unhandled API error on %s %s", request.method, request.url.path
        )
        if not _is_v1(request):
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"detail": "Internal Server Error"},
            )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_envelope(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                code=INTERNAL_ERROR,
                message="An internal error occurred.",
            ),
        )
