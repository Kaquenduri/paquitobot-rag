"""``POST /query-mock`` controller (canvas-mock parallel of ``/query``).

This is the canvas-mock twin of :mod:`app.controllers.query`. The
flow is the same:

    verify_backend_jwt_dependency → require_tenant → require_tenant_mock

The ``require_tenant_mock`` dep returns ``(tenant_id,
api_key_prefix)``; the prefix is the only marker the connect
controller persists (the full key never lands in the database, see
:mod:`app.controllers.auth_canvas_mock`). The request handler does
NOT need the prefix — only the tenant id — but the dep chain runs
end-to-end so a tenant with no registered mock key is rejected with
403 before the route fires.

The body and response envelope mirror ``/query`` exactly:

    {answer, lang, route, correlation_id}

This controller reuses the canonical :class:`RAGService`: the
catalog is already mock-aligned because the previous
``implementacion-paquito-canvas-mock`` change replaced
``_TOOL_SPECS`` with the 9 mock tools, so
:class:`app.text_to_sql.tools.TOOL_CATALOG` IS the mock catalog. No
flag or variant is required. The system prompt in
:mod:`app.rag.agent` is also mock-aware.

The ``DISABLE_RAG_ROUTES`` feature flag is honoured for hot-disabling
the endpoint, identical to ``/query``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings, get_settings
from app.core.deps import require_tenant_mock
from app.core.errors import new_correlation_id
from app.core.logging import get_correlation_id, get_logger
from app.observability.metrics import record_rag_request
from app.rag.prompts import detect_language
from app.services.rag_service import RAGService

logger = get_logger("app.controllers.query_mock")

router = APIRouter(prefix="/query-mock", tags=["query-mock"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class QueryMockRequest(BaseModel):
    """Inbound payload for ``POST /query-mock``.

    ``extra=\"forbid\"`` blocks clients from passing ``tenant_id`` (or
    any other field) through the body; the Pydantic validator raises
    422 before the route handler runs. Identical shape to the
    legacy ``/query`` body.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Student's natural-language question.",
    )
    language: str | None = Field(
        default=None,
        max_length=8,
        description="Optional ISO-639-1 override; auto-detected when omitted.",
    )


class QueryMockResponse(BaseModel):
    """Stable response envelope (mirrors ``/query``)."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(..., min_length=1)
    lang: str = Field(..., min_length=2, max_length=8)
    route: str = Field(..., min_length=1)
    correlation_id: str = Field(..., min_length=1, max_length=64)


# ---------------------------------------------------------------------------
# Dependency factories
# ---------------------------------------------------------------------------


def get_rag_service(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> RAGService:
    """Return a :class:`RAGService` for the canvas-mock pipeline.

    The behaviour is identical to :func:`app.controllers.query.get_rag_service`:
    tests override this dependency to inject a stub; the production
    path resolves the runtime service stashed on ``app.state`` during
    the lifespan.

    The catalog the service binds is the 9-tool mock catalog; no
    variant is needed here.
    """
    _ = settings
    runtime_service = getattr(request.app.state, "rag_service", None)
    if runtime_service is not None:
        return runtime_service
    return RAGService()


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=QueryMockResponse,
    status_code=status.HTTP_200_OK,
)
async def post_query_mock(
    payload: QueryMockRequest,
    tenant_context: Annotated[tuple[uuid.UUID, str], Depends(require_tenant_mock)],
    rag_service: Annotated[RAGService, Depends(get_rag_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> QueryMockResponse:
    """Answer a canvas-mock-scoped question using the standard RAG pipeline.

    The response contract is the same as ``/query``::

        {answer, lang, route, correlation_id}

    Failures:

    - ``401`` when the JWT is missing or invalid (handled by the
      inner dep).
    - ``403`` when the tenant has no registered canvas-mock key
      (handled by ``require_tenant_mock``).
    - ``503 rag_routes_disabled`` when ``DISABLE_RAG_ROUTES`` is on.
    - ``422`` when the request body contains fields outside the
      schema (e.g. a client-supplied ``tenant_id``).
    """
    tenant_id, _api_key_prefix = tenant_context

    if getattr(settings, "disable_rag_routes", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "rag_routes_disabled",
                "message": "RAG routes are disabled by configuration",
            },
        )

    correlation_id = get_correlation_id() or new_correlation_id()
    language = payload.language or detect_language(payload.question)

    rag_service.provider_health()

    try:
        result = rag_service.answer(
            payload.question,
            tenant_id=tenant_id,
            language=language,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "rag_mock_answer_failed",
            correlation_id=correlation_id,
            tenant_id=str(tenant_id),
        )
        record_rag_request(route="unknown", lang=language, outcome="error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "rag_answer_failed",
                "message": "RAG pipeline failed",
            },
        ) from exc

    record_rag_request(
        route=result["route"],
        lang=result["lang"],
        outcome="ok",
    )
    return QueryMockResponse(
        answer=result["answer"],
        lang=result["lang"],
        route=result["route"],
        correlation_id=correlation_id,
    )


__all__ = [
    "QueryMockRequest",
    "QueryMockResponse",
    "get_rag_service",
    "post_query_mock",
    "router",
]


# ---------------------------------------------------------------------------
# Self-test (executable via ``python -m app.controllers.query_mock``)
# ---------------------------------------------------------------------------


def _selftest() -> None:
    """Run end-to-end POST /query-mock assertions with a stub RAG service."""
    import os
    import secrets
    import time

    import jwt
    from cryptography.fernet import Fernet
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.core.db import engine_for_url, session_factory_for
    from app.core.errors import register_exception_handlers
    from app.middleware.correlation_id import CorrelationIdMiddleware
    from app.models import Base
    from app.services.tenant_service import TenantService

    backend_secret = secrets.token_urlsafe(32)
    fernet_key = Fernet.generate_key().decode("ascii")
    os.environ["BACKEND_SECRET"] = backend_secret
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
    get_settings.cache_clear()

    settings = get_settings()
    engine = engine_for_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = session_factory_for(engine)
    try:
        with factory() as session:
            tenant_service = TenantService(session=session)
            tenant = tenant_service.get_or_create_tenant("user-mock")
            tenant_id = tenant.id
            session.add(
                CanvasMockUser(
                    tenant_id=tenant_id,
                    canvas_mock_id=42,
                    api_key_prefix="stu_0011",
                    role="student",
                )
            )
            session.commit()

        class _StubRAG:
            last_call: dict[str, Any] | None = None

            def provider_health(self) -> dict[str, bool]:
                return {"embedding_available": False}

            def answer(self, question, *, tenant_id, language=None, sql=None):
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

        app = FastAPI()
        register_exception_handlers(app)
        app.add_middleware(CorrelationIdMiddleware)
        from app.services.tenant_service import SESSION_STORE_STATE_FLAG

        setattr(app.state, SESSION_STORE_STATE_FLAG, True)
        app.include_router(router)

        def _override_session() -> Any:
            with factory() as s:
                yield s

        from app.core.db import get_db_session

        app.dependency_overrides[get_db_session] = _override_session
        app.dependency_overrides[get_rag_service] = lambda: _StubRAG()
        app.dependency_overrides[get_settings] = lambda: settings

        token = jwt.encode(
            {"sub": "user-mock", "exp": int(time.time()) + 60},
            backend_secret,
            algorithm="HS256",
        )

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
            assert _StubRAG.last_call["tenant_id"] == tenant_id

            # 403 when no mock row exists (different user, no seed).
            token_no_mock = jwt.encode(
                {"sub": "no-mock-user", "exp": int(time.time()) + 60},
                backend_secret,
                algorithm="HS256",
            )
            response = client.post(
                "/query-mock",
                headers={"Authorization": f"Bearer {token_no_mock}"},
                json={"question": "hi"},
            )
            assert response.status_code == 403
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
        get_settings.cache_clear()


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    from app.models import CanvasMockUser

    _selftest()
