"""Smoke tests for the ``POST /sync`` controller (PR 4 task 4.8).

Coverage:

- Successful manual sync returns 202 Accepted with the
  ``SyncResult`` payload.
- Lock-busy returns 429 with ``Retry-After``.
- Throttled returns 429 with ``Retry-After`` reflect the panel
  interval.
- Missing tenant credentials -> 403 (via the dep chain).
- The router integrates with the FastAPI exception handler so the
  response body matches the design's error envelope.
"""

from __future__ import annotations

import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt import encode as jwt_encode

from app.controllers import sync as sync_module
from app.core.config import Settings, get_settings
from app.security.token_crypto import TokenCipher
from app.services.canvas_service import (
    CanvasService,
    TenantCredentialsMissing,
)
from app.services.tenant_service import (
    TenantRecord,
    TenantService,
    reset_tenant_service,
)
from app.sync import lock as sync_lock
from app.sync.pipeline import SyncResult


TENANT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_singleton() -> None:
    reset_tenant_service()
    sync_lock.reset_memory_locks()
    yield
    reset_tenant_service()
    sync_lock.reset_memory_locks()


def _jwt(secret: str, sub: str = "user-1") -> str:
    return jwt_encode(
        {"sub": sub, "exp": int(time.time()) + 60},
        secret,
        algorithm="HS256",
    )


def _build_app(
    *,
    tenant_service: TenantService,
    settings: Settings,
) -> FastAPI:
    """Mount the sync router on a dedicated FastAPI instance."""
    app = FastAPI()
    app.include_router(sync_module.router)

    canvas_service = CanvasService(
        settings=settings,
        tenant_service=tenant_service,
    )
    app.dependency_overrides[sync_module.get_canvas_service] = lambda: canvas_service
    app.dependency_overrides[sync_module.get_settings] = lambda: settings
    # Override the DB session dep so the test uses an in-memory
    # SQLite engine instead of trying to connect to a real Postgres.
    from collections.abc import Generator

    from app.core.db import engine_for_url, session_factory_for
    from sqlalchemy.orm import Session

    test_engine = engine_for_url("sqlite:///:memory:")
    from app.models import Base

    Base.metadata.create_all(test_engine)
    test_factory = session_factory_for(test_engine)

    def _override_db_session() -> Generator[Session]:
        session = test_factory()
        try:
            yield session
        finally:
            session.close()

    # The controller uses get_db_session as a helper, not a dep.
    # We monkeypatch the module's symbol so the controller uses
    # the in-memory engine.
    import app.controllers.sync as sync_module_ns

    sync_module_ns.get_db_session = _override_db_session

    # The dep chain's ``require_tenant`` calls ``get_tenant_service``
    # directly (not via FastAPI's dep system), so we plant the
    # test's service as the process-wide singleton for the duration
    # of the test. The autouse fixture resets it after the test.
    import app.services.tenant_service as tenant_module

    tenant_module._default_service = tenant_service
    return app


def _make_settings(**overrides: Any) -> Settings:
    base = {
        "supabase_database_url": "postgresql+psycopg://127.0.0.1:1/test",
        "tenant_token_key": Fernet.generate_key().decode("ascii"),
        "backend_secret": secrets.token_urlsafe(32),
        "gemini_api_key": "test",
        "ollama_host": "http://127.0.0.1:1",
        "canvas_api_base_url": "https://canvas.test/api/v1",
        "manual_sync_min_interval_seconds": 60,
    }
    base.update(overrides)
    return Settings(**base)


def _seed_tenant(
    tenant_service: TenantService, tenant_id: uuid.UUID, backend_user_id: str
) -> None:
    """Insert a tenant with stored Canvas credentials."""
    tenant_service._tenants_by_id[tenant_id] = TenantRecord(
        id=tenant_id, backend_user_id=backend_user_id
    )
    tenant_service._tenants_by_backend_user[backend_user_id] = TenantRecord(
        id=tenant_id, backend_user_id=backend_user_id
    )
    cipher = TokenCipher(Fernet.generate_key().decode("ascii"))
    tenant_service.store_canvas_token(tenant_id, "plain-canvas-token", cipher)


def _seed_tenant_no_credentials(
    tenant_service: TenantService, backend_user_id: str
) -> None:
    """Insert a tenant WITHOUT Canvas credentials."""
    tenant_service.get_or_create_tenant(backend_user_id)


# ---------------------------------------------------------------------------
# 202 Accepted
# ---------------------------------------------------------------------------


def test_post_sync_returns_202_on_success(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    monkeypatch.setenv("BACKEND_SECRET", secrets.token_urlsafe(32))
    monkeypatch.setenv("TENANT_TOKEN_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    settings = get_settings()

    service = TenantService()
    _seed_tenant(service, TENANT_ID, "user-1")

    async def fake_run(self, tenant_id, *, session):
        return SyncResult(
            tenant_id=tenant_id,
            status="ok",
            last_status="ok",
            last_error_class=None,
            last_successful_at=datetime.now(UTC),
            last_run_at=datetime.now(UTC),
            counts={
                "users": 1, "courses": 2, "enrollments": 0,
                "assignments": 0, "submissions": 0,
                "soft_deleted": 0, "dropped_peer": 0,
            },
            lock_acquired=True,
        )

    monkeypatch.setattr(CanvasService, "run_sync_for_tenant", fake_run)

    app = _build_app(tenant_service=service, settings=settings)
    token = _jwt(settings.backend_secret, sub="user-1")

    with TestClient(app) as client:
        response = client.post(
            "/sync", headers={"Authorization": f"Bearer {token}"}
        )

    if response.status_code != 202:
        print(f"DEBUG: status={response.status_code}, body={response.text}")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "ok"
    assert body["last_status"] == "ok"
    assert body["last_error_class"] is None
    assert "correlation_id" in body
    assert response.headers["X-Correlation-ID"] == body["correlation_id"]


# ---------------------------------------------------------------------------
# 429 throttled
# ---------------------------------------------------------------------------


def test_post_sync_returns_429_when_throttled(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    monkeypatch.setenv("BACKEND_SECRET", secrets.token_urlsafe(32))
    monkeypatch.setenv("TENANT_TOKEN_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    settings = get_settings()

    service = TenantService()
    _seed_tenant(service, TENANT_ID, "user-1")

    async def fake_run(self, tenant_id, *, session):
        return SyncResult(
            tenant_id=tenant_id,
            status="ok",
            last_status="ok",
            last_error_class=None,
            last_successful_at=None,
            last_run_at=datetime.now(UTC),
            counts={},
            lock_acquired=True,
        )

    monkeypatch.setattr(CanvasService, "run_sync_for_tenant", fake_run)

    app = _build_app(tenant_service=service, settings=settings)
    token = _jwt(settings.backend_secret, sub="user-1")

    with TestClient(app) as client:
        first = client.post("/sync", headers={"Authorization": f"Bearer {token}"})
        second = client.post("/sync", headers={"Authorization": f"Bearer {token}"})

    assert first.status_code == 202
    assert second.status_code == 429
    assert "Retry-After" in second.headers
    assert int(second.headers["Retry-After"]) > 0
    body = second.json()
    assert body["code"] == "sync_throttled"
    assert body["details"]["retry_after_seconds"] >= 1


# ---------------------------------------------------------------------------
# 429 lock busy
# ---------------------------------------------------------------------------


def test_post_sync_returns_429_when_lock_busy(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    monkeypatch.setenv("BACKEND_SECRET", secrets.token_urlsafe(32))
    monkeypatch.setenv("TENANT_TOKEN_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    settings = get_settings()

    service = TenantService()
    _seed_tenant(service, TENANT_ID, "user-1")

    async def fake_run(self, tenant_id, *, session):
        return SyncResult(
            tenant_id=tenant_id,
            status="locked",
            last_status="locked",
            last_error_class=None,
            last_successful_at=None,
            last_run_at=datetime.now(UTC),
            counts={},
            lock_acquired=False,
        )

    monkeypatch.setattr(CanvasService, "run_sync_for_tenant", fake_run)

    app = _build_app(tenant_service=service, settings=settings)
    token = _jwt(settings.backend_secret, sub="user-1")

    with TestClient(app) as client:
        response = client.post("/sync", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 429
    assert "Retry-After" in response.headers
    body = response.json()
    assert body["code"] == "sync_locked"


# ---------------------------------------------------------------------------
# 401 / 403
# ---------------------------------------------------------------------------


def test_post_sync_returns_401_when_jwt_missing(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    monkeypatch.setenv("BACKEND_SECRET", secrets.token_urlsafe(32))
    monkeypatch.setenv("TENANT_TOKEN_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    settings = get_settings()

    service = TenantService()
    _seed_tenant(service, TENANT_ID, "user-1")

    app = _build_app(tenant_service=service, settings=settings)

    with TestClient(app) as client:
        response = client.post("/sync")

    assert response.status_code == 401


def test_post_sync_returns_403_when_tenant_credentials_missing(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    """Tenant exists but has no Canvas credentials."""
    monkeypatch.setenv("BACKEND_SECRET", secrets.token_urlsafe(32))
    monkeypatch.setenv("TENANT_TOKEN_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    settings = get_settings()

    service = TenantService()
    _seed_tenant_no_credentials(service, "user-1")

    async def fake_run(self, tenant_id, *, session):
        raise TenantCredentialsMissing(str(tenant_id))

    monkeypatch.setattr(CanvasService, "run_sync_for_tenant", fake_run)

    app = _build_app(tenant_service=service, settings=settings)
    token = _jwt(settings.backend_secret, sub="user-1")

    with TestClient(app) as client:
        response = client.post(
            "/sync", headers={"Authorization": f"Bearer {token}"}
        )

    # The controller converts ``TenantCredentialsMissing`` into a
    # 403 with ``tenant_credentials_missing``.
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "tenant_credentials_missing"


# ---------------------------------------------------------------------------
# 202 with failed status
# ---------------------------------------------------------------------------


def test_post_sync_returns_202_with_failed_status(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    """A failed sync returns 202 Accepted with the failure envelope."""
    monkeypatch.setenv("BACKEND_SECRET", secrets.token_urlsafe(32))
    monkeypatch.setenv("TENANT_TOKEN_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    settings = get_settings()

    service = TenantService()
    _seed_tenant(service, TENANT_ID, "user-1")

    async def fake_run(self, tenant_id, *, session):
        return SyncResult(
            tenant_id=tenant_id,
            status="failed",
            last_status="failed",
            last_error_class="canvas_unavailable",
            last_successful_at=None,
            last_run_at=datetime.now(UTC),
            counts={},
            lock_acquired=False,
        )

    monkeypatch.setattr(CanvasService, "run_sync_for_tenant", fake_run)

    app = _build_app(tenant_service=service, settings=settings)
    token = _jwt(settings.backend_secret, sub="user-1")

    with TestClient(app) as client:
        response = client.post(
            "/sync", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "failed"
    assert body["last_error_class"] == "canvas_unavailable"
