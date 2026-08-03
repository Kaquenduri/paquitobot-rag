"""FastAPI exception handlers and the ``safe_message`` scrubber.

The contract: every error response and every error log line MUST pass through
``safe_message`` so that no plaintext credential, Fernet ciphertext, or DB URL
ever reaches the wire or the log stream.

PR 6 added :class:`app.middleware.correlation_id.CorrelationIdMiddleware`;
the exception handlers now reuse the bound ``correlation_id`` from the
contextvar instead of minting a fresh one, so the response body and
``X-Correlation-ID`` header always agree.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_correlation_id, get_logger
from app.schemas.errors import ErrorBody

# --- safe_message ---------------------------------------------------------

# Patterns are intentionally conservative; PR 2 will tighten them with the
# full RedactionFilter (dict-level + nested structure).
_FERNET_PATTERN = re.compile(r"gAAAA[A-Za-z0-9_\-=]+")
_BEARER_PATTERN = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-+/=]+")
_PG_URL_PATTERN = re.compile(r"postgres(?:ql)?(?:\+[A-Za-z]+)?://[^\s\"'<>]+")
_LONG_BLOB_PATTERN = re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")

_MASK = "***REDACTED***"


def safe_message(message: str) -> str:
    """Return ``message`` with every known credential pattern masked.

    Applied to BOTH the HTTP response body and the log emission so the two
    streams cannot diverge.
    """
    if not message:
        return message
    scrubbed = _FERNET_PATTERN.sub(_MASK, message)
    scrubbed = _BEARER_PATTERN.sub(_MASK, scrubbed)
    scrubbed = _PG_URL_PATTERN.sub(_MASK, scrubbed)
    scrubbed = _LONG_BLOB_PATTERN.sub(_MASK, scrubbed)
    return scrubbed


def _safe_dict(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Apply ``safe_message`` to every string value inside ``payload``."""
    if payload is None:
        return None
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            safe[key] = safe_message(value)
        elif isinstance(value, dict):
            safe[key] = _safe_dict(value)
        elif isinstance(value, list):
            safe[key] = [_safe_dict(v) if isinstance(v, dict) else v for v in value]
        else:
            safe[key] = value
    return safe


# --- Correlation ID -------------------------------------------------------


def new_correlation_id() -> str:
    """Generate a UUID v4 used for ``correlation_id`` and response header."""
    return str(uuid.uuid4())


def _bound_correlation_id() -> str:
    """Return the bound correlation_id, generating a fresh one if absent.

    PR 6's :class:`CorrelationIdMiddleware` binds the per-request id
    to the structlog ``contextvars`` before any handler runs, so error
    responses reuse the same id as the response header. This helper
    preserves the PR 1 fallback so an exception raised before the
    middleware ever ran still gets a valid id.
    """
    return get_correlation_id() or new_correlation_id()


# --- Handlers -------------------------------------------------------------


def _log_error(correlation_id: str, code: str, message: str, *, status: int) -> None:
    log = get_logger("app.errors")
    log.error(
        "request_error",
        correlation_id=correlation_id,
        code=code,
        status=status,
        message=message,
    )


def _error_response(
    *,
    status: int,
    code: str,
    message: str,
    correlation_id: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body = ErrorBody(
        code=code,
        message=safe_message(message),
        correlation_id=correlation_id,
        details=_safe_dict(details),
    )
    response = JSONResponse(status_code=status, content=body.model_dump(mode="json"))
    response.headers["X-Correlation-ID"] = correlation_id
    return response


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    correlation_id = _bound_correlation_id()
    # Allow callers to override the default ``http_<status>`` code by
    # passing a dict detail with explicit ``code`` / ``message`` keys.
    # Used by ``app.controllers.auth`` to emit ``canvas_token_invalid``
    # without bypassing the central redaction pipeline.
    if isinstance(exc.detail, dict):
        code = str(exc.detail.get("code") or f"http_{exc.status_code}")
        raw_message = exc.detail.get("message")
        message = str(raw_message) if raw_message is not None else code
    else:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        code = f"http_{exc.status_code}"
        message = detail or "Request failed"
    _log_error(correlation_id, code, message, status=exc.status_code)
    return _error_response(
        status=exc.status_code,
        code=code,
        message=message,
        correlation_id=correlation_id,
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    correlation_id = _bound_correlation_id()
    # ``errors()`` already returns dicts; scrub any string values inside them.
    details = _safe_dict({"errors": exc.errors()})
    message = "Request validation failed"
    _log_error(correlation_id, "validation_error", message, status=422)
    return _error_response(
        status=422,
        code="validation_error",
        message=message,
        correlation_id=correlation_id,
        details=details,
    )


async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Catch-all: NEVER expose the raw exception text or args verbatim."""
    correlation_id = _bound_correlation_id()
    safe = safe_message(str(exc)) or exc.__class__.__name__
    log = get_logger("app.errors")
    log.exception(
        "unhandled_exception",
        correlation_id=correlation_id,
        error_class=exc.__class__.__name__,
        message=safe,
    )
    return _error_response(
        status=500,
        code="internal_error",
        message="An unexpected error occurred",
        correlation_id=correlation_id,
        details={"error_class": exc.__class__.__name__},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach all error handlers to ``app`` in one call."""
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
