"""Unit tests for ``POST /sync-mock`` (canvas-mock only).

Mirrors the ``app.controllers.auth_canvas_mock`` test seam: a FastAPI
test client driven through an ``httpx.MockTransport`` so the mock is
exercised in-process. The endpoint is **dedicated to the
canvas-mock-api**; it MUST NOT touch ``settings.canvas_api_base_url``
(the real Canvas URL).

Contract pinned by the tests:

- **Header ``X-Canvas-Mock-Api-Key`` + bearer JWT.** The key's prefix
  is matched against ``canvas_mock_users.api_key_prefix`` for the
  tenant resolved from the JWT.
- **404 ``mock_key_not_registered``** when no row exists. The user
  must call ``POST /auth/canvas-mock/connect`` first.
- **200 with sync counts** when the extractor succeeds. The mock
  client is built from the full key + a backend JWT (HS256).
- **502 ``sync_failed``** when the extractor surfaces a shape error.
- **503 ``mock_unavailable``** when the mock is unreachable after
  retries.
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
from app.models import (
    Base,
    CanvasMockAttendanceRecord,
    CanvasMockCourse,
    CanvasMockGrade,
    CanvasMockUser,
)
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
        canvas_mock_api_key="adm_test_key",
        canvas_mock_jwt_secret="selftest-mock-jwt-secret",
        scheduler_enabled=False,
    )


@pytest.fixture
def sync_mock_app() -> Generator[
    tuple[FastAPI, sessionmaker, Engine], None, None
]:
    """Yield ``(app, session_factory, engine)`` for sync-mock tests.

    The fixture wires the router with a default ``MockTransport`` that
    serves empty lists. Individual tests re-build the app with their
    own handler by calling :func:`_make_app`.
    """
    settings = _build_settings()
    engine = engine_for_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = session_factory_for(engine)
    try:
        yield _make_app(settings, factory, httpx.MockTransport(_empty_lists)), factory, engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _empty_lists(request: httpx.Request) -> httpx.Response:
    """Default mock: every endpoint returns ``[]``."""
    return httpx.Response(200, json=[])


def _make_app(
    settings: Any,
    factory: Any,
    transport: httpx.AsyncBaseTransport,
) -> FastAPI:
    """Build a fresh FastAPI app wiring the sync-mock route."""
    from app.controllers.sync_mock import (
        _get_sync_mock_client_factory,
        router as sync_mock_router,
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
    app.include_router(sync_mock_router)
    app.dependency_overrides[_get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = _override_session
    app.dependency_overrides[verify_backend_jwt_dependency] = lambda: "selftest-user"
    app.dependency_overrides[_get_sync_mock_client_factory] = lambda: (
        lambda _settings, api_key, jwt_token: _StubClient(api_key, jwt_token, transport)
    )
    return app


def _make_app_with_handler(
    factory: Any,
    handler: Callable[[httpx.Request], httpx.Response],
) -> FastAPI:
    """Build a fresh app with the given mock handler."""
    return _make_app(_build_settings(), factory, httpx.MockTransport(handler))


class _StubClient:
    """Lightweight stand-in for :class:`CanvasMockClient`.

    The test only needs ``get(path, params=...)`` to fire the
    ``MockTransport`` handler. The transport inspects the URL path
    so the production URL contract
    (``/users/self/courses?include[]=term``,
    ``/users/self/attendance?days=14``, ``/users/self/grades``)
    can be exercised end-to-end.

    Mirrors the real client's contract: 5xx raises
    :class:`CanvasMockTransientError` so the controller maps it to
    503 ``mock_unavailable``.
    """

    def __init__(
        self,
        api_key: str,
        jwt_token: str,
        transport: httpx.AsyncBaseTransport,
    ) -> None:
        self.api_key = api_key
        self.jwt_token = jwt_token
        self._client = httpx.AsyncClient(
            base_url="https://canvas-mock.invalid/api/v1",
            timeout=8.0,
            transport=transport,
        )

    async def get(
        self,
        path: str,
        *args: Any,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        from app.services.canvas_mock_client import CanvasMockTransientError

        response = await self._client.get(
            path,
            params=params,
            headers={
                "X-Api-Key": self.api_key,
                "Authorization": f"Bearer {self.jwt_token}",
            },
        )
        if response.status_code >= 500:
            raise CanvasMockTransientError(
                f"canvas-mock returned {response.status_code}"
            )
        if response.status_code >= 400:
            response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()


def _seed_mock_user(
    factory: Any,
    *,
    api_key_prefix: str = "stu_001",
    canvas_mock_id: int = 42,
    role: str = "student",
) -> None:
    """Seed a ``canvas_mock_users`` row for the selftest tenant."""
    from app.services.tenant_service import TenantService

    with factory() as session:
        service = TenantService(session=session)
        tenant = service.get_or_create_tenant("selftest-user")
        session.add(
            CanvasMockUser(
                tenant_id=tenant.id,
                canvas_mock_id=canvas_mock_id,
                api_key_prefix=api_key_prefix,
                role=role,
            )
        )
        session.commit()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_sync_mock_happy_path_returns_counts(
    sync_mock_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    """200 with sync counts + rows landed in canvas_mock_* tables."""
    app, factory, _engine = sync_mock_app

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/users/self/courses":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 101,
                        "name": "Cálculo I",
                        "course_code": "CALC-1",
                        "workflow_state": "available",
                    }
                ],
            )
        if request.url.path == "/api/v1/users/self/attendance":
            return httpx.Response(
                200,
                json=[
                    {
                        "class_session_id": 9001,
                        "user_id": 42,
                        "status": "present",
                    }
                ],
            )
        if request.url.path == "/api/v1/users/self/grades":
            return httpx.Response(
                200,
                json=[
                    {
                        "assignment_id": 501,
                        "user_id": 42,
                        "score": 18.0,
                        "grade": "18",
                        "graded_at": "2026-08-15T10:00:00Z",
                    },
                    {
                        "assignment_id": 502,
                        "user_id": 42,
                        "score": 16.0,
                        "grade": "16",
                        "graded_at": "2026-08-16T10:00:00Z",
                    },
                ],
            )
        return httpx.Response(200, json=[])

    _seed_mock_user(factory, api_key_prefix="stu_001")
    app = _make_app_with_handler(factory, _handler)
    with TestClient(app) as client:
        response = client.post(
            "/sync-mock",
            headers={
                "Authorization": "Bearer selftest-backend-token",
                "X-Canvas-Mock-Api-Key": "stu_001",
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["synced"]["courses"] == 1
    assert body["synced"]["attendance"] == 1
    assert body["synced"]["grades"] == 2
    assert "tenant_id" in body

    with factory() as session:
        courses = session.execute(select(CanvasMockCourse)).scalars().all()
        grades = session.execute(select(CanvasMockGrade)).scalars().all()
        attendance = session.execute(select(CanvasMockAttendanceRecord)).scalars().all()
    assert len(courses) == 1
    assert len(grades) == 2
    assert len(attendance) == 1


# ---------------------------------------------------------------------------
# 404: key not registered
# ---------------------------------------------------------------------------


def test_sync_mock_key_not_registered_returns_404(
    sync_mock_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    """No canvas_mock_users row → 404 ``mock_key_not_registered``."""
    app = sync_mock_app[0]
    with TestClient(app) as client:
        response = client.post(
            "/sync-mock",
            headers={
                "Authorization": "Bearer selftest-backend-token",
                "X-Canvas-Mock-Api-Key": "stu_001",
            },
        )
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["code"] == "mock_key_not_registered"
    assert "stu_001" not in response.text


# ---------------------------------------------------------------------------
# 503: mock unreachable
# ---------------------------------------------------------------------------


def test_sync_mock_unavailable_returns_503(
    sync_mock_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    """Mock returns 5xx on every retry → 503 ``mock_unavailable``."""
    _, factory, _engine = sync_mock_app

    def _boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    _seed_mock_user(factory, api_key_prefix="stu_001")
    app = _make_app_with_handler(factory, _boom)
    with TestClient(app) as client:
        response = client.post(
            "/sync-mock",
            headers={
                "Authorization": "Bearer selftest-backend-token",
                "X-Canvas-Mock-Api-Key": "stu_001",
            },
        )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "mock_unavailable"


# ---------------------------------------------------------------------------
# Probe must hit the mock, not real Canvas
# ---------------------------------------------------------------------------


def test_sync_mock_targets_mock_not_real_canvas(
    sync_mock_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    """The outbound calls MUST go to ``canvas_mock_api_base_url``."""
    captured_urls: list[str] = []
    captured_headers: list[dict[str, str]] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        captured_headers.append(dict(request.headers))
        return httpx.Response(200, json=[])

    _, factory, _engine = sync_mock_app
    _seed_mock_user(factory, api_key_prefix="adm_001")
    app = _make_app_with_handler(factory, _capture)
    with TestClient(app) as client:
        response = client.post(
            "/sync-mock",
            headers={
                "Authorization": "Bearer selftest-backend-token",
                "X-Canvas-Mock-Api-Key": "adm_001",
            },
        )
    assert response.status_code == 200, response.text
    assert captured_urls, "no outbound calls"
    for url in captured_urls:
        assert url.startswith("https://canvas-mock.invalid/api/v1/")
        assert "canvas.invalid" not in url
    # Both X-Api-Key and Authorization: Bearer must be present.
    for headers in captured_headers:
        assert headers.get("x-api-key") == "adm_001"
        assert headers.get("authorization", "").startswith("Bearer ")