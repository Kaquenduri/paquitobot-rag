"""Unit tests for the Canvas HTTP client (PR 4 task 4.2).

Coverage:

- POST/PUT/PATCH/DELETE raise :class:`CanvasMethodRejected` BEFORE
  any network call (no transport is wired).
- ``tenacity`` retries 5xx up to 3 times and returns the final 4xx.
- Tokens are resolved on every call via the supplied provider, so
  rotation does not require rebuilding the client.
- ``httpx.MockTransport`` drives the network round-trip without
  touching the real internet.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from app.canvas.client import (
    CanvasClient,
    CanvasMethodRejected,
    CanvasRequestError,
)

# ---------------------------------------------------------------------------
# Method rejection (FAIL FAST, no network)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_client_rejects_mutation_methods_without_network(
    method: str,
) -> None:
    """The mutation guard must fire BEFORE any transport call."""

    transport_calls: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        transport_calls.append(request)
        return httpx.Response(200, json={"sentinel": True})

    # ``MockTransport`` records every dispatched request; we expect
    # to see **zero** calls for every mutation method.
    transport = httpx.MockTransport(_handler)
    client = CanvasClient(
        base_url="https://canvas.test/api/v1",
        token_provider=lambda: "fake-token",
        transport=transport,
    )

    async def _run() -> None:
        with pytest.raises(CanvasMethodRejected) as exc_info:
            await client._request(method, "/users/self")
        assert exc_info.value.method == method.upper()
        assert exc_info.value.allowed == {"GET"}

    import asyncio

    asyncio.run(_run())
    assert transport_calls == []


def test_post_through_public_get_helper_raises_before_network() -> None:
    """The public ``get`` helper is the only entry point; ensure it
    only accepts GET-style calls and refuses everything else from
    the internal ``_request`` if the caller bypasses it."""
    client = CanvasClient(
        base_url="https://canvas.test/api/v1",
        token_provider=lambda: "fake-token",
    )
    with pytest.raises(CanvasMethodRejected):
        # Direct call with POST — this exercises the guard.
        import asyncio

        asyncio.run(client._request("post", "/users"))


def test_allowed_methods_can_be_overridden_per_instance() -> None:
    """Design uses ``ALLOWED_METHODS`` as a class-level whitelist, but
    tests may monkeypatch it for a single instance."""

    class _StrictClient(CanvasClient):
        ALLOWED_METHODS = {"GET", "HEAD"}

    client = _StrictClient(
        base_url="https://canvas.test/api/v1",
        token_provider=lambda: "fake-token",
    )
    assert "GET" in client.ALLOWED_METHODS
    assert "POST" not in client.ALLOWED_METHODS


# ---------------------------------------------------------------------------
# Retry / timeout behaviour
# ---------------------------------------------------------------------------


def _fixed_token() -> str:
    return "fake-canvas-token"


def _client_with(handler: Callable[[httpx.Request], httpx.Response]) -> CanvasClient:
    return CanvasClient(
        base_url="https://canvas.test/api/v1",
        token_provider=_fixed_token,
        transport=httpx.MockTransport(handler),
        max_attempts=3,
    )


def test_client_returns_2xx_with_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 1, "name": "Alice"})

    import asyncio

    client = _client_with(handler)
    response = asyncio.run(client.get("/users/self"))
    assert response.status_code == 200
    assert response.json() == {"id": 1, "name": "Alice"}


def test_client_retries_5xx_then_returns_final_response() -> None:
    call_count = {"value": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["value"] += 1
        if call_count["value"] < 3:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"id": 1})

    import asyncio

    client = _client_with(handler)
    response = asyncio.run(client.get("/users/self"))
    assert response.status_code == 200
    assert call_count["value"] == 3


def test_client_returns_4xx_without_retry() -> None:
    call_count = {"value": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["value"] += 1
        return httpx.Response(404, json={"errors": [{"message": "not found"}]})

    import asyncio

    client = _client_with(handler)
    response = asyncio.run(client.get("/users/self"))
    assert response.status_code == 404
    assert call_count["value"] == 1


def test_client_raises_canvas_request_error_after_retry_exhausted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    import asyncio

    client = _client_with(handler)
    with pytest.raises(CanvasRequestError) as exc_info:
        asyncio.run(client.get("/users/self"))
    assert exc_info.value.status_code == 503


def test_client_sends_authorization_header() -> None:
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json={"id": 1})

    import asyncio

    client = _client_with(handler)
    asyncio.run(client.get("/users/self"))
    assert captured_headers.get("authorization") == "Bearer fake-canvas-token"


def test_client_resolves_token_on_every_request() -> None:
    """Token rotation is supported by re-invoking the provider."""
    counter = {"count": 0}

    def provider() -> str:
        counter["count"] += 1
        return f"token-#{counter['count']}"

    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers["authorization"])
        return httpx.Response(200, json={"id": 1})

    import asyncio

    client = CanvasClient(
        base_url="https://canvas.test/api/v1",
        token_provider=provider,
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(client.get("/users/self"))
    asyncio.run(client.get("/users/self"))
    assert captured == ["Bearer token-#1", "Bearer token-#2"]


def test_client_resolves_async_token_provider() -> None:
    """The provider may be async; the client awaits it."""
    async def async_provider() -> str:
        return "async-token"

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"id": 1})

    import asyncio

    client = CanvasClient(
        base_url="https://canvas.test/api/v1",
        token_provider=async_provider,
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(client.get("/users/self"))
    assert captured["auth"] == "Bearer async-token"


def test_client_rejects_empty_base_url() -> None:
    with pytest.raises(ValueError):
        CanvasClient("", token_provider=lambda: "x")


def test_client_rejects_non_callable_token_provider() -> None:
    with pytest.raises(ValueError):
        CanvasClient("https://x.test/api/v1", token_provider=None)  # type: ignore[arg-type]


def test_client_resolves_absolute_url_for_link_following() -> None:
    """The ``next`` link from ``paginate`` is absolute; the client
    must pass it through unchanged in the URL path."""

    captured_url: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_url["url"] = str(request.url)
        return httpx.Response(200, json={"id": 1})

    import asyncio

    client = _client_with(handler)
    asyncio.run(client._request("GET", "https://other.test/api/v1/users"))
    assert captured_url["url"] == "https://other.test/api/v1/users"


def test_client_retries_on_connection_error() -> None:
    """httpx connection errors are retried up to ``max_attempts``."""

    attempt = {"value": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempt["value"] += 1
        if attempt["value"] < 3:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"id": 1})

    import asyncio

    client = _client_with(handler)
    response = asyncio.run(client.get("/users/self"))
    assert response.status_code == 200
    assert attempt["value"] == 3
