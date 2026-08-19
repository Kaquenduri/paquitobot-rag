"""Unit tests for ``require_tenant_mock`` (new dep in :mod:`app.core.deps`).

The dependency is the canvas-mock parallel of ``require_tenant_token``:

- ``require_tenant`` yields the tenant id from the JWT ``sub``.
- ``require_tenant_mock`` then looks up the ``canvas_mock_users``
  prefix for that tenant and returns ``(tenant_id, api_key_prefix)``.

Contracts pinned by the tests:

- Happy path: returns ``(tenant_id, prefix)`` for a tenant with a
  ``canvas_mock_users`` row.
- Missing mock row: 403 (no Canvas-mock credentials for tenant).
- Missing JWT: 401 (the inner dep fails first, parallel to the
  legacy chain).
- The dependency MUST NOT touch the legacy ``canvas_credentials``
  table — a tenant with only a mock row is valid.
"""

from __future__ import annotations

import os
import secrets
import time
import uuid
from collections.abc import Iterator

import jwt
import pytest
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.db import engine_for_url, session_factory_for
from app.core.deps import require_tenant_mock
from app.middleware.correlation_id import CorrelationIdMiddleware
from app.models import Base, CanvasMockUser
from app.services.tenant_service import SESSION_STORE_STATE_FLAG


@pytest.fixture
def mock_app() -> Iterator[tuple[FastAPI, sessionmaker, Engine]]:
    """Yield ``(app, factory, engine)`` with a fresh in-memory SQLite DB."""
    settings_secret = secrets.token_urlsafe(32)
    fernet_key = Fernet.generate_key().decode("ascii")
    import os

    os.environ["BACKEND_SECRET"] = settings_secret
    os.environ["TENANT_TOKEN_KEY"] = fernet_key
    os.environ["_TEST_BACKEND_SECRET"] = settings_secret
    os.environ["SUPABASE_DATABASE_URL"] = (
        "postgresql+psycopg://127.0.0.1:1/primer_rag_test"
    )
    os.environ["MINIMAX_API_KEY"] = "test-only-minimax"
    os.environ["OLLAMA_HOST"] = "http://127.0.0.1:1"
    os.environ["CANVAS_API_BASE_URL"] = "https://canvas.invalid/api/v1"
    os.environ["CANVAS_MOCK_API_BASE_URL"] = "https://canvas-mock.invalid/api/v1"
    os.environ["CANVAS_MOCK_API_KEY"] = "adm_test_key"
    os.environ["CANVAS_MOCK_JWT_SECRET"] = "test-only-mock-jwt"
    os.environ["CANVAS_MOCK_WEBHOOK_SECRET"] = "test-only-mock-webhook"
    os.environ["GOOGLE_CLIENT_ID"] = "test-only.apps.googleusercontent.com"
    get_settings.cache_clear()

    engine = engine_for_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = session_factory_for(engine)
    try:
        app = FastAPI()
        setattr(app.state, SESSION_STORE_STATE_FLAG, True)
        app.add_middleware(CorrelationIdMiddleware)

        @app.get("/mock-dep")
        def mock_dep(
            payload: object = Depends(require_tenant_mock),
        ) -> dict:
            tenant_id, prefix = payload  # type: ignore[misc]
            return {"tenant_id": str(tenant_id), "api_key_prefix": prefix}

        def _override_session() -> Iterator[Session]:
            with factory() as session:
                yield session

        from app.core.db import get_db_session

        app.dependency_overrides[get_db_session] = _override_session
        yield app, factory, engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
        get_settings.cache_clear()


def _mint_jwt(secret: str, sub: str = "user-1") -> str:
    return jwt.encode(
        {"sub": sub, "exp": int(time.time()) + 60},
        secret,
        algorithm="HS256",
    )


def _seed_mock_user(
    factory: sessionmaker,
    sub: str,
    api_key_prefix: str,
    canvas_mock_id: int = 42,
) -> uuid.UUID:
    """Create a tenant (via backend_user_id) + a canvas_mock_users row."""
    from app.services.tenant_service import TenantService

    with factory() as session:
        tenant_service = TenantService(session=session)
        tenant = tenant_service.get_or_create_tenant(sub)
        tenant_id = tenant.id
        session.commit()
    with factory() as session:
        row = CanvasMockUser(
            tenant_id=tenant_id,
            canvas_mock_id=canvas_mock_id,
            api_key_prefix=api_key_prefix,
            role="student",
        )
        session.add(row)
        session.commit()
    return tenant_id


def test_require_tenant_mock_returns_prefix_for_registered_tenant(
    mock_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    app, factory, _engine = mock_app
    tenant_id = _seed_mock_user(factory, "user-1", "stu_0011")
    secret = os.environ["_TEST_BACKEND_SECRET"]
    token = _mint_jwt(secret, "user-1")

    response = TestClient(app).get(
        "/mock-dep", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_id"] == str(tenant_id)
    assert body["api_key_prefix"] == "stu_0011"


def test_require_tenant_mock_403_when_no_mock_row(
    mock_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    """Tenant resolves but has no canvas_mock_users row → 403."""
    app, factory, _engine = mock_app
    # The JWT will resolve to a tenant (auto-created), but we don't
    # seed any mock row.
    secret = os.environ["_TEST_BACKEND_SECRET"]
    token = _mint_jwt(secret, "user-no-mock")

    response = TestClient(app).get(
        "/mock-dep", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403, response.text


def test_require_tenant_mock_401_when_jwt_missing(
    mock_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    app, _factory, _engine = mock_app

    response = TestClient(app).get("/mock-dep")

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_require_tenant_mock_401_when_jwt_invalid(
    mock_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    app, _factory, _engine = mock_app

    response = TestClient(app).get(
        "/mock-dep", headers={"Authorization": "Bearer not.a.real.jwt"}
    )

    assert response.status_code == 401


def test_require_tenant_mock_isolated_from_legacy_credentials(
    mock_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    """A tenant with ONLY a mock row (no legacy canvas_credentials) is valid.

    The mock store is independent of the legacy Fernet-encrypted
    store. The dep must not require a legacy row to succeed.
    """
    app, factory, _engine = mock_app
    tenant_id = _seed_mock_user(factory, "user-mock-only", "adm_42abc")
    secret = os.environ["_TEST_BACKEND_SECRET"]
    token = _mint_jwt(secret, "user-mock-only")

    with factory() as session:
        # Sanity: no legacy credential, only the mock row.
        from app.services.tenant_service import TenantRepository

        repository = TenantRepository(session)
        assert repository.get_canvas_credential(tenant_id) is None
        rows = (
            session.execute(
                select(CanvasMockUser).where(CanvasMockUser.tenant_id == tenant_id)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1

    response = TestClient(app).get(
        "/mock-dep", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["api_key_prefix"] == "adm_42abc"
