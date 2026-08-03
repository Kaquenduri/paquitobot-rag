"""``CorrelationIdMiddleware`` — UUID v4 per request (PR 6 task 6.6).

The middleware generates a UUID v4 per request (or accepts a
client-supplied ``X-Correlation-ID`` if it parses as a UUID v4) and:

1. Binds it to the ``correlation_id`` ``ContextVar`` used by the
   structlog processor (see :mod:`app.core.logging`) so every log
   line carries the id.
2. Echoes the id in the ``X-Correlation-ID`` response header so
   clients can grep their access logs against ours.
3. Clears the binding on the way out so the next request starts
   fresh.

The middleware is intentionally simple: no header rewriting, no
tracing propagation, no per-request context other than the
correlation id. PR 6 task 6.6 only requires the header + log
emission contract.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import bind_correlation_id, clear_correlation_id, get_logger

CORRELATION_ID_HEADER = "X-Correlation-ID"

logger = get_logger("app.middleware.correlation_id")


def _is_valid_uuid4(value: str) -> bool:
    """Return ``True`` when ``value`` parses as a UUID v4 string."""
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 4


def new_correlation_id() -> str:
    """Generate a fresh UUID v4 string for a request."""
    return str(uuid.uuid4())


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Generate or accept a per-request correlation id.

    The middleware honours an incoming ``X-Correlation-ID`` header
    only when it is a valid UUID v4; anything else is discarded so a
    malicious or malformed header cannot poison the log binding.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = request.headers.get(CORRELATION_ID_HEADER)
        correlation_id = incoming if _is_valid_uuid4(incoming) else new_correlation_id()

        bind_correlation_id(correlation_id)
        try:
            response = await call_next(request)
        except Exception:
            clear_correlation_id()
            raise

        response.headers[CORRELATION_ID_HEADER] = correlation_id
        clear_correlation_id()
        return response


__all__ = [
    "CORRELATION_ID_HEADER",
    "CorrelationIdMiddleware",
    "new_correlation_id",
]


# ---------------------------------------------------------------------------
# Self-test (executable via ``python -m app.middleware.correlation_id``)
# ---------------------------------------------------------------------------


def _selftest() -> None:
    """Validate the helpers without spinning up the full middleware."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.logging import (
        clear_correlation_id,
        get_correlation_id,
    )

    # 1. UUID v4 validation.
    assert _is_valid_uuid4("00000000-0000-4000-8000-000000000000") is True
    assert (
        _is_valid_uuid4("00000000-0000-1000-8000-000000000000") is False
    )  # v1
    assert (
        _is_valid_uuid4("00000000-0000-3000-8000-000000000000") is False
    )  # v3
    assert _is_valid_uuid4("not-a-uuid") is False
    assert _is_valid_uuid4("") is False
    assert _is_valid_uuid4(None) is False  # type: ignore[arg-type]

    # 2. new_correlation_id always returns a v4 UUID string.
    cid = new_correlation_id()
    assert uuid.UUID(cid).version == 4

    # 3. End-to-end through FastAPI's TestClient: the middleware
    #    generates an id, exposes it on the response, and clears the
    #    ContextVar afterwards.
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/echo")
    def echo() -> dict:
        # Capture the binding at request time so we know it was set.
        return {"cid": get_correlation_id()}

    with TestClient(app) as client:
        # First request: middleware generates the id.
        first = client.get("/echo")
        assert first.status_code == 200
        first_cid = first.headers.get(CORRELATION_ID_HEADER)
        assert first_cid and uuid.UUID(first_cid).version == 4
        assert first.json()["cid"] == first_cid

        # Second request: middleware generates a fresh id (not
        # leaking the first).
        second = client.get("/echo")
        assert second.headers.get(CORRELATION_ID_HEADER) != first_cid

        # Third request: client-supplied UUID v4 is honoured.
        supplied = "12345678-1234-4678-9abc-def012345678"
        third = client.get(
            "/echo", headers={CORRELATION_ID_HEADER: supplied}
        )
        assert third.headers.get(CORRELATION_ID_HEADER) == supplied

        # Fourth request: malformed client value is discarded and
        # replaced with a fresh UUID v4.
        fourth = client.get(
            "/echo", headers={CORRELATION_ID_HEADER: "garbage"}
        )
        assert uuid.UUID(fourth.headers[CORRELATION_ID_HEADER]).version == 4

    # 4. The contextvar was cleared after the last request.
    assert get_correlation_id() is None
    clear_correlation_id()  # idempotent


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    _selftest()