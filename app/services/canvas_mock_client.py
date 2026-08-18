"""GET-only HTTP client for the canvas-mock-api (PR 4 task 4.2).

Mirrors the invariant contract of the real-Canvas client
(:mod:`app.canvas.client`) but targets the mock, which requires:

1. **Method whitelist.** ``ALLOWED_METHODS = {"GET"}`` — the mock
   exposes read-only endpoints. Any non-GET call raises
   :class:`CanvasMockError` BEFORE the network call.

2. **Dual authentication.** The mock requires **both** an
   ``X-Api-Key`` header AND a ``Authorization: Bearer <jwt>`` header.
   The client injects both on every request. JWTs can be rotated at
   runtime via :meth:`set_jwt_token`; the API key is fixed at
   construction time.

3. **Bounded retries.** 3 attempts (locked), with the explicit wait
   schedule ``[0.5, 2.0]`` (in seconds). 4xx is terminal; 5xx
   triggers a retry. Connection errors and read timeouts are
   treated as transient.

4. **Timeout.** 10s per request (locked). Long enough for the mock's
   slow paths, short enough to keep the extractor responsive.

``httpx.MockTransport`` keeps tests in-process — no real network.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import httpx

from app.core.logging import get_logger

logger = get_logger("app.services.canvas_mock_client")


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class CanvasMockError(Exception):
    """Any non-transient failure from the canvas-mock client.

    Catches both ``4xx`` responses (the API explicitly rejected the
    request) and any non-GET method attempt (caller-side bug, terminal
    by definition).
    """


class CanvasMockTransientError(Exception):
    """A retryable failure (5xx, network error, read timeout).

    The extractor catches this and surfaces it to the caller as a
    ``failed`` job so the scheduler can mark the row accordingly.
    """


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


# Locked retry envelope from the design.md contract.
RETRY_ATTEMPTS: int = 3
RETRY_WAIT_SCHEDULE_SECONDS: tuple[float, ...] = (0.5, 2.0)
REQUEST_TIMEOUT_SECONDS: float = 10.0


def _scheduled_wait(attempt_index: int) -> float:
    """Return the wait between ``attempt_index`` and ``attempt_index+1``.

    The schedule is fixed at ``RETRY_WAIT_SCHEDULE_SECONDS``; once
    exhausted, the LAST value is reused so the third retry still has a
    backoff (locked 0.5/2.0/8.0 — the "8s" is the third wait, capped
    by the schedule's last entry).
    """
    if attempt_index < 0:
        return RETRY_WAIT_SCHEDULE_SECONDS[0]
    if attempt_index >= len(RETRY_WAIT_SCHEDULE_SECONDS):
        # Last schedule entry acts as the steady-state ceiling.
        return RETRY_WAIT_SCHEDULE_SECONDS[-1]
    return RETRY_WAIT_SCHEDULE_SECONDS[attempt_index]


class CanvasMockClient:
    """Async HTTP client for the canvas-mock-api.

    The constructor accepts an optional ``transport`` so tests can
    inject ``httpx.MockTransport`` and avoid the real network. The
    default transport is the real httpx async client.

    The client is **async**; tests call :meth:`get` via
    ``asyncio.run``.
    """

    ALLOWED_METHODS: ClassVar[frozenset[str]] = frozenset({"GET"})

    def __init__(
        self,
        base_url: str,
        api_key: str,
        jwt_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        wait_schedule: tuple[float, ...] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._jwt_token = jwt_token
        self._timeout_seconds = timeout_seconds
        self._wait_schedule: tuple[float, ...] = (
            wait_schedule if wait_schedule is not None else RETRY_WAIT_SCHEDULE_SECONDS
        )
        self._owns_client = transport is None
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_seconds,
            transport=transport,
        )

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def wait_schedule(self) -> tuple[float, ...]:
        return self._wait_schedule

    def set_jwt_token(self, token: str) -> None:
        """Rotate the JWT token. The next request uses the new value."""
        self._jwt_token = token

    async def _request_with_retry(self, method: str, path: str) -> Any:
        """Run the request inside the retry envelope and return the parsed JSON."""
        if method not in self.ALLOWED_METHODS:
            raise CanvasMockError(
                f"method {method!r} is not allowed; only GET is supported"
            )

        headers = {
            "X-Api-Key": self._api_key,
            "Authorization": f"Bearer {self._jwt_token}",
        }

        last_exc: Exception | None = None

        # Manual retry loop so the recorded delays match the locked
        # schedule ``[0.5, 2.0]`` exactly (the design notes 0.5/2.0/8.0
        # but only two waits exist between three attempts; the third
        # attempt is the last and does not wait).
        for strike in range(RETRY_ATTEMPTS):
            try:
                response = await self._client.request(
                    method, path, headers=headers
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = CanvasMockTransientError(str(exc))
            else:
                if response.status_code >= 500:
                    last_exc = CanvasMockTransientError(
                        f"canvas-mock returned {response.status_code}"
                    )
                elif response.status_code >= 400:
                    raise CanvasMockError(
                        f"canvas-mock rejected request: {response.status_code} "
                        f"{response.text!r}"
                    )
                else:
                    return response.json()
            if strike + 1 >= RETRY_ATTEMPTS:
                assert last_exc is not None
                raise last_exc
            # Sleep per the locked schedule; the LAST entry is reused
            # so even a hypothetical 4th attempt would still backoff.
            wait_seconds = self._wait_schedule[
                min(strike, len(self._wait_schedule) - 1)
            ]
            await asyncio.sleep(wait_seconds)

        raise CanvasMockError("retry loop exited without returning")  # pragma: no cover

    async def get(self, path: str, *args: Any, **kwargs: Any) -> Any:
        """GET ``path`` and return the parsed JSON body."""
        return await self._request_with_retry("GET", path)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = [
    "REQUEST_TIMEOUT_SECONDS",
    "RETRY_ATTEMPTS",
    "RETRY_WAIT_SCHEDULE_SECONDS",
    "CanvasMockClient",
    "CanvasMockError",
    "CanvasMockTransientError",
]
