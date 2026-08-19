"""Unit tests for ``POST /auth/canvas-mock/connect`` (canvas-mock only).

Mirrors the legacy ``app.controllers.auth`` test seam: a FastAPI test
client driven through a ``MockTransport`` so the probe is exercised
in-process. The endpoint is **dedicated to the canvas-mock-api**;
it MUST NOT touch ``settings.canvas_api_base_url`` (the real Canvas
URL). The controller probes the mock with the user-supplied
``X-Canvas-Mock-Api-Key`` header, parses the role / ``mock_user_id``
out of the probe response, and upserts a row into
``canvas_mock_users``.

Contract pinned by the tests:

- **Valid key → 204 No Content.** A ``canvas_mock_users`` row exists
  with ``(tenant_id, api_key_prefix=first 8 chars, role, canvas_mock_id)``
  after the request returns. The full API key is NEVER stored.
- **Invalid key (mock returns 401) → 401 ``invalid_mock_key``.** No
  row is written; the raw key is never echoed back.
- **Mock unavailable (5xx) → 503 ``mock_unavailable``.** No row is written.
- **Network error → 503 ``mock_unavailable``.** No row is written.
- **The probe MUST go to ``settings.canvas_mock_api_base_url``** (NOT
  the real Canvas URL).
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.db import engine_for_url, session_factory_for
from app.middleware.correlation_id import CorrelationIdMiddleware
from app.models import Base, CanvasMockUser
from app.services.tenant_service import SESSION_STORE_STATE_FLAG


def _build_settings() -> Any:
    """Settings with the canvas-mock family configured (PR 1 + PR 4)."""
    from app.core.config import Settings

    fernet_key = Fernet.generate_key().decode("ascii")
    return Settings(
        supabase_database_url="postgresql+psycopg://127.0.0.1:1/selftest",
        tenant_token_key=fernet_key,
        backend_secret="selftest-backend-secret-with-sufficient-length",
        minimax_api_key="selftest-minimax-placeholder",
        ollama_host="http://127.0.0.1:1",
        canvas_api_base_url="https://canvas.invalid/api/v1",
        google_client_id="selftest.apps.googleusercontent.com",
        canvas_mock_webhook_secret="selftest-webhook-secret",
        canvas_mock_api_base_url="https://canvas-mock.invalid/api/v1",
        canvas_mock_api_key="adm_test_key",  # NOT the user's key
        canvas_mock_jwt_secret="selftest-mock-jwt-secret",
        scheduler_enabled=False,
    )


@pytest.fixture
def canvas_mock_connect_app() -> Generator[
    tuple[FastAPI, sessionmaker, Engine], None, None
]:
    """Yield ``(app, session_factory, engine)`` for canvas-mock connect tests.

    The app uses a single injectable transport slot; individual tests
    re-build the app with their own handler by calling :func:`_make_app`.
    """
    settings = _build_settings()
    engine = engine_for_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = session_factory_for(engine)
    try:
        yield _make_app(settings, factory, httpx.MockTransport(_ok_probe)), factory, engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _make_app(
    settings: Any,
    factory: Any,
    transport: httpx.AsyncBaseTransport,
) -> FastAPI:
    """Build a fresh FastAPI app wiring the canvas-mock connect route."""
    from app.controllers.auth_canvas_mock import (
        _get_mock_probe_transport,
        router as canvas_mock_auth_router,
    )
    from app.core.config import get_settings as _get_settings
    from app.core.db import get_db_session
    from app.core.deps import verify_backend_jwt_dependency

    def _override_session() -> Generator[Any, None, None]:
        with factory() as s:
            yield s

    app = FastAPI()
    setattr(app.state, SESSION_STORE_STATE_FLAG, True)
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(canvas_mock_auth_router)
    app.dependency_overrides[_get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = _override_session
    app.dependency_overrides[verify_backend_jwt_dependency] = lambda: "selftest-user"
    app.dependency_overrides[_get_mock_probe_transport] = lambda: transport
    return app


def _ok_probe(request: httpx.Request) -> httpx.Response:
    """Default 200 probe: admin user id=42, role=student."""
    return httpx.Response(200, json={"id": 42, "role": "student"})


def _make_app_with_handler(
    factory: Any,
    handler: Callable[[httpx.Request], httpx.Response],
) -> FastAPI:
    """Build a fresh app with the given probe handler."""
    return _make_app(_build_settings(), factory, httpx.MockTransport(handler))


def test_connect_valid_key_persists_row(
    canvas_mock_connect_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    """Valid probe → 204 + a canvas_mock_users row exists."""
    app, factory, _engine = canvas_mock_connect_app
    with TestClient(app) as client:
        response = client.post(
            "/auth/canvas-mock/connect",
            headers={
                "Authorization": "Bearer selftest-backend-token",
                "X-Canvas-Mock-Api-Key": "stu_001",
            },
        )
    assert response.status_code == 204, response.text
    with factory() as session:
        rows = session.execute(select(CanvasMockUser)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.api_key_prefix == "stu_001"[:8]
    assert row.role == "student"
    assert row.canvas_mock_id == 42


def test_connect_invalid_key_returns_401(
    canvas_mock_connect_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    """Mock returns 401 → controller returns 401 ``invalid_mock_key``."""
    _, factory, _engine = canvas_mock_connect_app

    def _reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    app = _make_app_with_handler(factory, _reject)
    with TestClient(app) as client:
        response = client.post(
            "/auth/canvas-mock/connect",
            headers={
                "Authorization": "Bearer selftest-backend-token",
                "X-Canvas-Mock-Api-Key": "stu_001",
            },
        )
    assert response.status_code == 401, response.text
    body = response.json()
    assert body["code"] == "invalid_mock_key"
    assert "stu_001" not in response.text
    with factory() as session:
        rows = session.execute(select(CanvasMockUser)).scalars().all()
    assert rows == []


def test_connect_mock_5xx_returns_503(
    canvas_mock_connect_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    """5xx on the probe → 503 ``mock_unavailable`` (and no row)."""
    _, factory, _engine = canvas_mock_connect_app

    def _boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    app = _make_app_with_handler(factory, _boom)
    with TestClient(app) as client:
        response = client.post(
            "/auth/canvas-mock/connect",
            headers={
                "Authorization": "Bearer selftest-backend-token",
                "X-Canvas-Mock-Api-Key": "stu_001",
            },
        )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "mock_unavailable"
    with factory() as session:
        rows = session.execute(select(CanvasMockUser)).scalars().all()
    assert rows == []


def test_connect_network_error_returns_503(
    canvas_mock_connect_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    """Connection refused on the probe → 503 ``mock_unavailable`` (and no row)."""
    _, factory, _engine = canvas_mock_connect_app

    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    app = _make_app_with_handler(factory, _raise)
    with TestClient(app) as client:
        response = client.post(
            "/auth/canvas-mock/connect",
            headers={
                "Authorization": "Bearer selftest-backend-token",
                "X-Canvas-Mock-Api-Key": "stu_001",
            },
        )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "mock_unavailable"
    with factory() as session:
        rows = session.execute(select(CanvasMockUser)).scalars().all()
    assert rows == []


def test_connect_probe_targets_mock_not_real_canvas(
    canvas_mock_connect_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    """The probe MUST hit ``canvas_mock_api_base_url`` (not the real Canvas URL)."""
    captured_urls: list[str] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        return httpx.Response(200, json={"id": 7, "role": "admin"})

    _, factory, _engine = canvas_mock_connect_app
    app = _make_app_with_handler(factory, _capture)
    with TestClient(app) as client:
        response = client.post(
            "/auth/canvas-mock/connect",
            headers={
                "Authorization": "Bearer selftest-backend-token",
                "X-Canvas-Mock-Api-Key": "adm_secret_001",
            },
        )
    assert response.status_code == 204, response.text
    assert captured_urls, "probe never fired"
    url = captured_urls[0]
    assert url.startswith("https://canvas-mock.invalid/api/v1/")
    assert "canvas.invalid" not in url
