"""Smoke tests for the ``app.controllers.auth`` router (PR 2 task 2.6).

The router is mounted on a dedicated :class:`fastapi.FastAPI` instance
because ``app.main`` is intentionally untouched in PR 2 (see
``apply-progress.md``). ``httpx.AsyncClient`` is monkeypatched via
``app.controllers.auth._probe_canvas`` so no real network call is made.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.controllers import auth as auth_module
from app.core.config import get_settings
from app.services.tenant_service import reset_tenant_service


@pytest.fixture(autouse=True)
def _isolated_singleton() -> Iterator[None]:
    reset_tenant_service()
    yield
    reset_tenant_service()


def _setup(monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]) -> tuple[str, str]:
    secret = secrets.token_urlsafe(32)
    fernet_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("BACKEND_SECRET", secret)
    monkeypatch.setenv("TENANT_TOKEN_KEY", fernet_key)
    monkeypatch.setenv(
        "CANVAS_API_BASE_URL", "https://canvas.invalid/api/v1"
    )
    get_settings.cache_clear()
    return secret, fernet_key


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth_module.router)
    return app


def _jwt(secret: str, sub: str = "user-1") -> str:
    return jwt.encode(
        {"sub": sub, "exp": int(time.time()) + 60},
        secret,
        algorithm="HS256",
    )


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


@pytest.mark.asyncio
async def test_connect_returns_204_on_successful_canvas_probe(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    secret, _ = _setup(monkeypatch, app_settings)
    captured: dict[str, str] = {}

    async def fake_probe(canvas_token: str, settings: object) -> _FakeResponse:
        captured["token"] = canvas_token
        return _FakeResponse(200)

    monkeypatch.setattr(auth_module, "_probe_canvas", fake_probe)

    response = TestClient(_build_app()).post(
        "/auth/canvas/connect",
        headers={
            "Authorization": f"Bearer {_jwt(secret)}",
            "X-Canvas-Token": "1234~fresh-canvas-token",
        },
    )

    assert response.status_code == 204
    assert captured["token"] == "1234~fresh-canvas-token"


def test_connect_returns_401_canvas_token_invalid_when_canvas_rejects(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    secret, _ = _setup(monkeypatch, app_settings)

    async def fake_probe(canvas_token: str, settings: object) -> _FakeResponse:
        return _FakeResponse(401)

    monkeypatch.setattr(auth_module, "_probe_canvas", fake_probe)

    response = TestClient(_build_app()).post(
        "/auth/canvas/connect",
        headers={
            "Authorization": f"Bearer {_jwt(secret)}",
            "X-Canvas-Token": "1234~bad-canvas-token",
        },
    )

    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "canvas_token_invalid"
    assert body["message"] == "Canvas rejected the token"
    assert "correlation_id" in body
    assert response.headers.get("X-Correlation-ID") == body["correlation_id"]
    assert "Bearer eyJ" not in response.text
    assert "1234~bad-canvas-token" not in response.text


def test_connect_401_when_canvas_probe_raises_network_error(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    secret, _ = _setup(monkeypatch, app_settings)

    async def fake_probe(canvas_token: str, settings: object) -> _FakeResponse:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(auth_module, "_probe_canvas", fake_probe)

    response = TestClient(_build_app()).post(
        "/auth/canvas/connect",
        headers={
            "Authorization": f"Bearer {_jwt(secret)}",
            "X-Canvas-Token": "1234~canvas-token",
        },
    )

    assert response.status_code == 401
    assert response.json()["code"] == "canvas_token_invalid"


def test_connect_422_when_canvas_token_header_missing(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    """``X-Canvas-Token`` is a required header; FastAPI returns 422."""
    secret, _ = _setup(monkeypatch, app_settings)

    response = TestClient(_build_app()).post(
        "/auth/canvas/connect",
        headers={"Authorization": f"Bearer {_jwt(secret)}"},
    )

    assert response.status_code == 422


def test_connect_401_when_backend_jwt_missing(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    _setup(monkeypatch, app_settings)

    response = TestClient(_build_app()).post(
        "/auth/canvas/connect",
        headers={"X-Canvas-Token": "1234~canvas-token"},
    )

    assert response.status_code == 401
