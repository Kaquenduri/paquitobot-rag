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


# ---------------------------------------------------------------------------
# /auth/login (Google id_token exchange -> backend JWT)
# ---------------------------------------------------------------------------


def _fake_id_token(sub: str = "google-user-42", email: str | None = "alice@example.com") -> str:
    """Return a syntactically valid (but unsigned) JWT for body validation."""
    payload: dict[str, object] = {"sub": sub}
    if email is not None:
        payload["email"] = email
    return jwt.encode(payload, "irrelevant", algorithm="HS256")


def test_login_issues_backend_jwt_when_id_token_is_valid(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    secret, _ = _setup(monkeypatch, app_settings)

    def fake_verify(token: str, request: object, audience: str) -> dict[str, str]:
        assert audience == app_settings["GOOGLE_CLIENT_ID"]
        return {"sub": "google-user-42", "email": "alice@example.com"}

    monkeypatch.setattr(
        auth_module.google_id_token,
        "verify_oauth2_token",
        fake_verify,
    )

    response = TestClient(_build_app()).post(
        "/auth/login",
        json={"id_token": _fake_id_token()},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["sub"] == "google-user-42"
    assert body["email"] == "alice@example.com"
    # The returned token is a PaquitoBot JWT, signed with BACKEND_SECRET.
    decoded = jwt.decode(
        body["access_token"],
        secret,
        algorithms=["HS256"],
        options={"require": ["sub", "iat", "exp"]},
    )
    assert decoded["sub"] == "google-user-42"
    assert decoded["iss"] == "paquitobot"
    # expires_in is in (0, login_token_ttl_seconds].
    assert 1 <= body["expires_in"] <= 86400


def test_login_returns_401_when_id_token_is_invalid(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    _setup(monkeypatch, app_settings)

    def fake_verify(token: str, request: object, audience: str) -> dict[str, str]:
        raise ValueError("Token expired")

    monkeypatch.setattr(
        auth_module.google_id_token,
        "verify_oauth2_token",
        fake_verify,
    )

    response = TestClient(_build_app()).post(
        "/auth/login",
        json={"id_token": _fake_id_token()},
    )

    assert response.status_code == 401
    assert "id_token" in response.text


def test_login_returns_401_when_id_token_missing_sub(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    _setup(monkeypatch, app_settings)

    def fake_verify(token: str, request: object, audience: str) -> dict[str, object]:
        return {"email": "no-sub@example.com"}  # type: ignore[return-value]

    monkeypatch.setattr(
        auth_module.google_id_token,
        "verify_oauth2_token",
        fake_verify,
    )

    response = TestClient(_build_app()).post(
        "/auth/login",
        json={"id_token": _fake_id_token()},
    )

    assert response.status_code == 401
    assert "sub" in response.text


def test_login_returns_422_when_id_token_missing(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    _setup(monkeypatch, app_settings)

    response = TestClient(_build_app()).post("/auth/login", json={})

    assert response.status_code == 422


def test_login_returns_422_on_extra_field(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    _setup(monkeypatch, app_settings)

    response = TestClient(_build_app()).post(
        "/auth/login",
        json={"id_token": _fake_id_token(), "tenant_id": "evil-uuid"},
    )

    assert response.status_code == 422
