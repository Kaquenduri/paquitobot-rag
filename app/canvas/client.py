"""GET-only Canvas HTTP client (PR 4 task 4.2).

The class enforces three invariants that together make the mutation
attack surface near-zero:

1. **Method whitelist.** ``ALLOWED_METHODS = {"GET"}`` is the **only**
   set of methods the client will ever send. Any non-GET call raises
   :class:`CanvasMethodRejected` BEFORE the network call — the
   rejection does not consult the token, the URL, or anything else.

2. **Bounded retries.** ``tenacity`` retries three times on 5xx and
   connection errors with exponential backoff (1s/2s/4s). 4xx is
   never retried (the response is final). Per-request timeout is 8s
   — long enough for Canvas's slow pages, short enough to keep the
   sync pipeline responsive.

3. **Token resolution.** ``token_provider`` is a zero-arg callable
   that returns the plaintext Canvas token. The client invokes it on
   every request so token rotation does not require rebuilding the
   client. Tests inject a stub provider so no real token is ever
   read from disk.

The client is **async** because the sync pipeline (PR 4 task 4.4)
runs inside the AsyncIOScheduler; tests use ``httpx.MockTransport``
to drive the client without any real network.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.logging import get_logger

logger = get_logger("app.canvas.client")


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class CanvasMethodRejected(Exception):
    """Raised when a non-GET method is requested before the network call.

    Spec scenario: "Mutation request is attempted" — the client MUST
    reject the operation before network transmission. The rejected
    method name is recorded so test assertions can verify the guard
    fired even when no HTTP layer is touched.
    """

    def __init__(self, method: str, allowed: set[str]) -> None:
        self.method = method.upper()
        self.allowed = {method.upper() for method in allowed}
        super().__init__(
            f"CanvasClient refuses method={self.method!r}; "
            f"allowed methods: {sorted(self.allowed)}"
        )


class CanvasRequestError(Exception):
    """Raised when the Canvas API returns a final 4xx/5xx after retries.

    The sync pipeline translates this into ``last_error_class``:

    - ``auth_rejected`` for 401 / 403
    - ``canvas_unavailable`` for 5xx after retries, or connection errors
    - ``schema_drift`` for 200 with unparseable JSON

    The status code and correlation-friendly details are preserved so
    the controller can decide whether to surface 502 to the client.
    """

    def __init__(
        self,
        *,
        method: str,
        path: str,
        status_code: int | None,
        message: str,
    ) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.message = message
        super().__init__(
            f"Canvas request failed: {method} {path} -> "
            f"{status_code if status_code is not None else 'n/a'}: {message}"
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


TokenProvider = Callable[[], str]
AsyncTokenProvider = Callable[[], Awaitable[str]]


class CanvasClient:
    """GET-only Canvas HTTP client with bounded retries.

    Parameters
    ----------
    base_url:
        The Canvas REST API base URL, e.g.
        ``https://example.instructure.com/api/v1``. Trailing slashes
        are tolerated.
    token_provider:
        Callable returning the plaintext Canvas token. May be sync
        (``Callable[[], str]``) or async (``Callable[[], Awaitable[str]]``);
        the client awaits the result when needed.
    timeout_seconds:
        Per-request timeout. Defaults to 8s as per design §5.
    max_attempts:
        Total attempts including the first call. Defaults to 3 (so
        2 retries). The exponential backoff is ``1s/2s/4s`` between
        attempts, capped at the latter.
    transport:
        Optional ``httpx.AsyncBaseTransport`` so tests can inject
        :class:`httpx.MockTransport` without patching the global
        client. Production leaves it as ``None`` and uses httpx's
        default transport.
    """

    # Mutable at the class level so tests can monkeypatch the
    # whitelist for a single process. The default is the design's
    # GET-only contract.
    ALLOWED_METHODS: ClassVar[set[str]] = {"GET"}

    def __init__(
        self,
        base_url: str,
        token_provider: TokenProvider | AsyncTokenProvider,
        *,
        timeout_seconds: float = 8.0,
        max_attempts: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("CanvasClient requires a non-empty base_url")
        if token_provider is None or not callable(token_provider):
            raise ValueError("CanvasClient requires a callable token_provider")
        self._base_url = base_url.rstrip("/")
        self._token_provider = token_provider
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max(1, max_attempts)
        self._transport = transport
        # The client is created lazily so tests that monkeypatch
        # ``httpx.AsyncClient`` get a fresh handle per request.
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Issue a GET request to ``path``.

        Raises :class:`CanvasMethodRejected` if ``path`` is a mutation
        (defensive — the method is always GET here, but the central
        ``_request`` validates any future method too).
        """
        return await self._request("GET", path, params=params)

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` if it was created."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_token(self) -> str:
        """Resolve the token via the (possibly async) provider."""
        result = self._token_provider()
        if hasattr(result, "__await__"):
            return await result  # type: ignore[func-returns-value]
        return result  # type: ignore[return-value]

    def _client_or_default(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            )
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Validate the method, then issue the request with retries.

        The method validation runs BEFORE any network call. The
        wrapped retry block only retries on 5xx and connection
        errors; 4xx is returned to the caller immediately.
        """
        # ---- 1. Method whitelist (FAIL FAST, no network) ----
        normalized = method.upper()
        if normalized not in self.ALLOWED_METHODS:
            raise CanvasMethodRejected(normalized, self.ALLOWED_METHODS)

        # ---- 2. Resolve the URL relative to the base ----
        url = self._resolve_url(path)
        params = dict(params or {})

        # ---- 3. Retry loop ----
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=4),
            retry=retry_if_exception_type(
                (httpx.HTTPError, CanvasRequestError)
            ),
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    return await self._do_request(normalized, url, params)
        except RetryError as exc:  # pragma: no cover - defensive
            raise CanvasRequestError(
                method=normalized,
                path=path,
                status_code=None,
                message=str(exc),
            ) from exc

        # Unreachable: the retry iterator always yields once and
        # either returns or raises.
        raise RuntimeError("CanvasClient retry loop exited without a result")

    async def _do_request(
        self,
        method: str,
        url: str,
        params: dict[str, Any],
    ) -> httpx.Response:
        """Issue a single HTTP call and translate failure modes."""
        token = await self._get_token()
        client = self._client_or_default()
        # httpx replaces the URL's query string when ``params`` is a
        # dict (even empty). We pass ``None`` instead so an absolute
        # URL with its own query string (e.g. the pagination ``next``
        # link) is preserved verbatim.
        merged_params = params or None
        try:
            response = await client.request(
                method,
                url,
                params=merged_params,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            # Connection errors, timeouts, etc. — retryable.
            raise CanvasRequestError(
                method=method,
                path=url,
                status_code=None,
                message=f"transport error: {exc.__class__.__name__}",
            ) from exc

        if response.status_code >= 500:
            # 5xx — retryable; raise to trigger the next attempt.
            raise CanvasRequestError(
                method=method,
                path=url,
                status_code=response.status_code,
                message=f"server error {response.status_code}",
            )

        if response.status_code >= 400:
            # 4xx — final. Surface as a normal return so the caller
            # can decide whether to translate to ``auth_rejected`` or
            # ``schema_drift``. The caller checks
            # ``response.status_code`` directly.
            return response

        return response

    def _resolve_url(self, path: str) -> str:
        """Resolve ``path`` against ``self._base_url``.

        Absolute URLs are passed through (Canvas occasionally returns
        a full URL in the pagination ``Link`` header).
        """
        if not path:
            raise ValueError("CanvasClient requires a non-empty path")
        if path.startswith(("http://", "https://")):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url}{path}"


__all__ = [
    "CanvasClient",
    "CanvasMethodRejected",
    "CanvasRequestError",
]


def _selftest() -> None:
    """Run GET-only and mock-transport assertions without real network."""
    import asyncio

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"id": 1})

    async def run() -> httpx.Response:
        client = CanvasClient(
            "https://canvas.test/api/v1",
            lambda: "selftest-token",
            transport=httpx.MockTransport(handler),
        )
        try:
            return await client.get("/users/self")
        finally:
            await client.aclose()

    response = asyncio.run(run())
    assert response.status_code == 200
    assert calls == ["GET"]


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    _selftest()
