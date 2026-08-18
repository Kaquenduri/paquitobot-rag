"""Unit tests for the canvas-mock-api HTTP client (PR 4 task 4.1).

Tests pin the GET-only contract, the dual ``X-Api-Key`` + ``Bearer``
authentication, the retry envelope (3 attempts, 0.5s/2s/8s, 4xx
terminal, 5xx transient), and the 10-second timeout. ``httpx.MockTransport``
keeps everything in-process — no real network involved.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.services.canvas_mock_client import (
    CanvasMockClient,
    CanvasMockError,
    CanvasMockTransientError,
)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    """Standard 200 JSON handler. The body is keyed by URL path."""
    payload = {
        "/courses": [{"id": 101, "name": "Cálculo I"}],
        "/assignments": [{"id": 42, "name": "Parcial"}],
        "/grades": [{"assignment_id": 42, "score": 18.0}],
        "/users/self": [{"id": 77, "name": "Ana"}],
        "/class_sessions": [{"id": 1, "course_id": 101}],
    }
    path = request.url.path
    return httpx.Response(200, json=payload.get(path, []))


def _run(coro: Any) -> Any:
    """Run a coroutine from a sync test."""
    return asyncio.run(coro)


def test_client_sends_x_api_key_and_bearer() -> None:
    """Every request carries both ``X-Api-Key`` and ``Authorization: Bearer``."""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    client = CanvasMockClient(
        base_url="https://canvas-mock.example.com",
        api_key="adm_test_key",
        jwt_token="jwt.test.token",
        transport=httpx.MockTransport(_handler),
    )
    _run(client.get("/courses"))
    _run(client.get("/assignments"))
    assert len(seen) == 2
    for request in seen:
        assert request.headers["X-Api-Key"] == "adm_test_key"
        assert request.headers["Authorization"] == "Bearer jwt.test.token"


def test_client_get_only_rejects_post() -> None:
    """The client refuses POST requests — the mock GET-only contract is locked."""
    client = CanvasMockClient(
        base_url="https://canvas-mock.example.com",
        api_key="adm_test",
        jwt_token="jwt",
        transport=httpx.MockTransport(_ok_handler),
    )
    with pytest.raises(CanvasMockError):
        _run(client._request_with_retry("POST", "/courses"))


def test_client_returns_parsed_json() -> None:
    """GET returns a list of rows (parsed JSON)."""
    client = CanvasMockClient(
        base_url="https://canvas-mock.example.com",
        api_key="adm_test",
        jwt_token="jwt",
        transport=httpx.MockTransport(_ok_handler),
    )
    rows = _run(client.get("/courses"))
    assert rows == [{"id": 101, "name": "Cálculo I"}]


def test_client_terminal_4xx_no_retry() -> None:
    """A 4xx response is terminal — the client does not retry it."""
    calls: list[int] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, json={"error": "not found"})

    client = CanvasMockClient(
        base_url="https://canvas-mock.example.com",
        api_key="adm_test",
        jwt_token="jwt",
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(CanvasMockError):
        _run(client.get("/courses"))
    assert len(calls) == 1


def test_client_retries_5xx_three_times() -> None:
    """A 5xx response triggers exactly 3 attempts before surfacing."""
    calls: list[int] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"error": "unavailable"})

    client = CanvasMockClient(
        base_url="https://canvas-mock.example.com",
        api_key="adm_test",
        jwt_token="jwt",
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(CanvasMockTransientError):
        _run(client.get("/courses"))
    assert len(calls) == 3


def test_client_succeeds_after_transient_failures() -> None:
    """A 5xx followed by a 200 returns the parsed rows."""
    calls: list[int] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 2:
            return httpx.Response(502, json={"error": "bad gateway"})
        return httpx.Response(200, json=[{"id": 12}])

    client = CanvasMockClient(
        base_url="https://canvas-mock.example.com",
        api_key="adm_test",
        jwt_token="jwt",
        transport=httpx.MockTransport(_handler),
    )
    rows = _run(client.get("/courses"))
    assert rows == [{"id": 12}]
    assert len(calls) == 2


def test_client_enforces_ten_second_timeout() -> None:
    """The read timeout is 10s (locked)."""
    client = CanvasMockClient(
        base_url="https://canvas-mock.example.com",
        api_key="adm_test",
        jwt_token="jwt",
        transport=httpx.MockTransport(_ok_handler),
    )
    assert client.timeout_seconds == 10.0


def test_client_auth_headers_track_token_changes() -> None:
    """A new JWT token takes effect on the next request."""
    seen_auth: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers["Authorization"])
        return httpx.Response(200, json=[])

    client = CanvasMockClient(
        base_url="https://canvas-mock.example.com",
        api_key="adm_test",
        jwt_token="jwt.v1",
        transport=httpx.MockTransport(_handler),
    )
    _run(client.get("/courses"))
    client.set_jwt_token("jwt.v2")
    _run(client.get("/courses"))
    assert seen_auth == ["Bearer jwt.v1", "Bearer jwt.v2"]


def test_client_backoff_schedule_records_exact_delays() -> None:
    """Recorded delays match the locked ``[0.5, 2.0]`` schedule.

    Two retries means two waits between three attempts; the third
    attempt does not wait. ``asyncio.sleep`` is monkey-patched to
    record each delay without blocking, so the test finishes in
    milliseconds.
    """
    import app.services.canvas_mock_client as module

    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def _recording_sleep(seconds: float) -> None:
        recorded.append(seconds)
        await real_sleep(0)

    calls: list[int] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"error": "transient"})

    client = CanvasMockClient(
        base_url="https://canvas-mock.example.com",
        api_key="adm_test",
        jwt_token="jwt",
        transport=httpx.MockTransport(_handler),
    )
    original = module.asyncio.sleep
    module.asyncio.sleep = _recording_sleep  # type: ignore[assignment]
    try:
        with pytest.raises(CanvasMockTransientError):
            _run(client.get("/courses"))
    finally:
        module.asyncio.sleep = original  # type: ignore[assignment]

    assert recorded == [0.5, 2.0], recorded
    assert len(calls) == 3

