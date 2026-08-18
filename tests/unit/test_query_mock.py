"""Unit tests for ``POST /query-mock`` (canvas-mock parallel of /query).

The endpoint mirrors ``POST /query`` but uses the canvas-mock
auth chain (``require_tenant_mock``) and the same
:class:`RAGService` (the catalog is already mock-aligned, see
``app/text_to_sql/tools.py``). The body and response shape are
identical to /query.

Contract pinned by the tests:

- Happy path: 200 with ``{answer, lang, route, correlation_id}``.
- The server-derived tenant id is honoured; ``tenant_id`` in the
  body is rejected by ``extra=\"forbid\"``.
- Missing JWT → 401.
- Tenant with no canvas_mock_users row → 403.
- ``disable_rag_routes`` feature flag → 503 ``rag_routes_disabled``.
- The RAG service receives the ``tenant_id`` resolved by the
  dependency chain (not the body's).
- The ``lang`` field reflects the controller's language detection
  (explicit override or auto-detect).
"""

from __future__ import annotations

import os
import secrets
import time
import uuid
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.db import engine_for_url, session_factory_for
from app.core.deps import require_tenant_mock
from app.middleware.correlation_id import CorrelationIdMiddleware
from app.models import Base, CanvasMockUser
from app.services.tenant_service import SESSION_STORE_STATE_FLAG


@pytest.fixture
def mock_query_app() -> Iterator[tuple[FastAPI, sessionmaker, Engine]]:
    """Yield ``(app, factory, engine)`` with the query-mock route mounted.

    The fixture installs the query-mock router, sets up the SQLite
    session override, and exposes ``factory`` so individual tests
    can seed ``canvas_mock_users`` rows.
    """
    from app.controllers.query_mock import get_rag_service, router

    settings_secret = secrets.token_urlsafe(32)
    fernet_key = Fernet.generate_key().decode("ascii")
    os.environ["BACKEND_SECRET"] = settings_secret
    os.environ["_TEST_BACKEND_SECRET"] = settings_secret
    os.environ["TENANT_TOKEN_KEY"] = fernet_key
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
    os.environ["SCHEDULER_ENABLED"] = "false"
    os.environ["DISABLE_RAG_ROUTES"] = "false"
    get_settings.cache_clear()

    engine = engine_for_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = session_factory_for(engine)
    try:
        app = FastAPI()
        from app.core.errors import register_exception_handlers

        register_exception_handlers(app)
        setattr(app.state, SESSION_STORE_STATE_FLAG, True)
        app.add_middleware(CorrelationIdMiddleware)
        app.include_router(router)

        def _override_session() -> Iterator[Any]:
            with factory() as session:
                yield session

        from app.core.db import get_db_session

        app.dependency_overrides[get_db_session] = _override_session
        yield app, factory, engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
        get_settings.cache_clear()


def _mint_jwt(secret: str, sub: str) -> str:
    return jwt.encode(
        {"sub": sub, "exp": int(time.time()) + 60},
        secret,
        algorithm="HS256",
    )


def _seed_mock_user(
    factory: sessionmaker,
    sub: str,
    api_key_prefix: str = "stu_0011",
    canvas_mock_id: int = 42,
) -> uuid.UUID:
    """Seed a tenant + ``canvas_mock_users`` row."""
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


class _StubRAG:
    """Deterministic RAG service used to assert the wiring."""

    last_call: dict[str, Any] | None = None

    def provider_health(self) -> dict[str, bool]:
        return {"embedding_available": False}

    def answer(self, question: str, *, tenant_id: Any, language: str | None = None, sql: str | None = None):
        _StubRAG.last_call = {
            "question": question,
            "tenant_id": tenant_id,
            "language": language,
        }
        return {
            "answer": "stub mock answer",
            "lang": language or "en",
            "route": "relational",
        }


def test_query_mock_happy_path_returns_200(
    mock_query_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    """Happy path: 200 with the expected response shape."""
    from app.controllers.query_mock import get_rag_service

    app, factory, _engine = mock_query_app
    tenant_id = _seed_mock_user(factory, "user-1")
    token = _mint_jwt(os.environ["_TEST_BACKEND_SECRET"], "user-1")

    app.dependency_overrides[get_rag_service] = lambda: _StubRAG()

    with TestClient(app) as client:
        response = client.post(
            "/query-mock",
            headers={"Authorization": f"Bearer {token}"},
            json={"question": "How many assignments?"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"] == "stub mock answer"
    assert body["lang"] == "en"
    assert body["route"] == "relational"
    assert body["correlation_id"]
    assert response.headers.get("X-Correlation-ID") == body["correlation_id"]
    assert _StubRAG.last_call is not None
    assert _StubRAG.last_call["tenant_id"] == tenant_id
    assert _StubRAG.last_call["question"] == "How many assignments?"


def test_query_mock_respects_explicit_language(
    mock_query_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    """``language: \"es\"`` is honoured; the RAG service receives it."""
    from app.controllers.query_mock import get_rag_service

    app, factory, _engine = mock_query_app
    _seed_mock_user(factory, "user-1")
    token = _mint_jwt(os.environ["_TEST_BACKEND_SECRET"], "user-1")
    app.dependency_overrides[get_rag_service] = lambda: _StubRAG()

    with TestClient(app) as client:
        response = client.post(
            "/query-mock",
            headers={"Authorization": f"Bearer {token}"},
            json={"question": "¿Cuál es el promedio?", "language": "es"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["lang"] == "es"
    assert _StubRAG.last_call["language"] == "es"


def test_query_mock_rejects_tenant_id_in_body(
    mock_query_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    """``tenant_id`` in the body is rejected by Pydantic ``extra=\"forbid\"``."""
    app, factory, _engine = mock_query_app
    _seed_mock_user(factory, "user-1")
    token = _mint_jwt(os.environ["_TEST_BACKEND_SECRET"], "user-1")

    with TestClient(app) as client:
        response = client.post(
            "/query-mock",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "question": "hi",
                "tenant_id": "deadbeef-0000-0000-0000-000000000000",
            },
        )
    assert response.status_code == 422


def test_query_mock_401_when_jwt_missing(
    mock_query_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    app, factory, _engine = mock_query_app

    with TestClient(app) as client:
        response = client.post("/query-mock", json={"question": "hi"})
    assert response.status_code == 401


def test_query_mock_403_when_tenant_has_no_mock_credentials(
    mock_query_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    """Tenant resolves (via JWT) but has no ``canvas_mock_users`` row → 403."""
    app, factory, _engine = mock_query_app
    # No row seeded: the JWT will auto-create a tenant on first call,
    # but the mock lookup will fail.
    token = _mint_jwt(os.environ["_TEST_BACKEND_SECRET"], "user-no-mock")

    with TestClient(app) as client:
        response = client.post(
            "/query-mock",
            headers={"Authorization": f"Bearer {token}"},
            json={"question": "hi"},
        )
    assert response.status_code == 403


def test_query_mock_503_when_disable_rag_routes_set(
    mock_query_app: tuple[FastAPI, sessionmaker, Engine],
) -> None:
    """``DISABLE_RAG_ROUTES=true`` returns 503 ``rag_routes_disabled``."""
    from app.core.config import get_settings as _get_settings
    from app.controllers.query_mock import get_rag_service

    app, factory, _engine = mock_query_app
    _seed_mock_user(factory, "user-1")
    token = _mint_jwt(os.environ["_TEST_BACKEND_SECRET"], "user-1")

    settings = _get_settings()
    disabled = settings.model_copy(update={"disable_rag_routes": True})
    app.dependency_overrides[_get_settings] = lambda: disabled
    app.dependency_overrides[get_rag_service] = lambda: _StubRAG()

    with TestClient(app) as client:
        response = client.post(
            "/query-mock",
            headers={"Authorization": f"Bearer {token}"},
            json={"question": "How many assignments?"},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "rag_routes_disabled"
