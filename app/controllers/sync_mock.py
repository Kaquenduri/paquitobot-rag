"""``POST /sync-mock`` controller (canvas-mock only).

Pull-and-upsert bridge from the ``canvas-mock-api`` into the local
``canvas_mock_*`` tables. The flow is:

1. **Resolve the tenant from the JWT.** ``verify_backend_jwt_dependency``
   yields the Google ``sub``; :class:`TenantService` maps it to the
   canonical tenant row.
2. **Look up the user's mock key.** The header
   ``X-Canvas-Mock-Api-Key`` is matched against
   ``canvas_mock_users.api_key_prefix`` for the tenant. A missing row
   returns 404 ``mock_key_not_registered`` so the client knows to run
   ``POST /auth/canvas-mock/connect`` first.
3. **Build an :class:`CanvasMockClient`.** The client carries the
   FULL user key (``X-Api-Key``) plus a backend JWT minted from
   ``settings.canvas_mock_jwt_secret`` (the mock requires both). The
   JWT subject is the tenant UUID so the mock can resolve the role
   independently of the key prefix.
4. **Run the extractor.** :class:`CanvasMockExtractor.fetch_and_upsert`
   drives the GETs against ``/users/self/courses?include[]=term``,
   ``/users/self/attendance?days=14`` and ``/users/self/grades`` and
   upserts each resource into its table. Per-course assignments are
   **not** fetched — the mock exposes them only under admin routes.
5. **Commit and return counts.** The body is
   ``{"synced": {"courses": N, ...}, "tenant_id": "<uuid>"}``.

Error envelope:

- **404 ``mock_key_not_registered``** — no row matches
  ``(tenant_id, api_key_prefix)``.
- **502 ``sync_failed``** — extractor hit a non-retryable 4xx or a
  Pydantic shape error (terminal, do not retry).
- **503 ``mock_unavailable``** — 5xx after the client exhausted its
  retry envelope, or the network itself was unreachable.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.core.deps import verify_backend_jwt_dependency
from app.core.errors import new_correlation_id
from app.core.logging import get_correlation_id, get_logger
from app.models import CanvasMockUser
from app.schemas.errors import ErrorBody
from app.security.backend_auth import issue_backend_jwt
from app.services.canvas_mock_client import (
    CanvasMockClient,
    CanvasMockError,
    CanvasMockTransientError,
)
from app.services.canvas_mock_extractor import (
    CanvasMockExtractor,
    CanvasMockShapeError,
)
from app.services.tenant_service import TenantService

logger = get_logger("app.controllers.sync_mock")

router = APIRouter(prefix="/sync-mock", tags=["sync-mock"])

SYNC_RESOURCES: tuple[str, ...] = ("courses", "attendance", "grades")


# ---------------------------------------------------------------------------
# Dependency seams
# ---------------------------------------------------------------------------


def _default_client_factory(
    settings: Settings,
    api_key: str,
    jwt_token: str,
) -> CanvasMockClient:
    """Build the production :class:`CanvasMockClient`.

    The base URL is the canvas-mock family ONLY — never
    ``settings.canvas_api_base_url``. The transport is the default
    real-network httpx transport; tests override this dependency.
    """
    return CanvasMockClient(
        base_url=settings.canvas_mock_api_base_url,
        api_key=api_key,
        jwt_token=jwt_token,
    )


def get_canvas_mock_client_factory() -> Any:
    """FastAPI dependency returning the client factory callable."""
    return _default_client_factory


_get_sync_mock_client_factory = get_canvas_mock_client_factory


def _resolve_tenant_id(
    request: Request,
    session: Session,
    user_id: str,
) -> uuid.UUID:
    """Resolve the canonical tenant id (mirrors :mod:`app.controllers.auth`)."""
    from app.services.tenant_service import (
        get_tenant_service,
        should_use_session_store,
    )

    if should_use_session_store(request.app.state, session):
        service = TenantService(session=session)
    else:
        service = get_tenant_service()
    tenant = service.get_or_create_tenant(user_id)
    return tenant.id


# ---------------------------------------------------------------------------
# Error envelope helpers
# ---------------------------------------------------------------------------


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    correlation_id = get_correlation_id() or new_correlation_id()
    body = ErrorBody(
        code=code,
        message=message,
        correlation_id=correlation_id,
        details=details,
    )
    response = JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
    )
    response.headers["X-Correlation-ID"] = correlation_id
    return response


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_200_OK)
async def post_sync_mock(
    request: Request,
    api_key: Annotated[str, Header(alias="X-Canvas-Mock-Api-Key")],
    user_id: Annotated[str, Depends(verify_backend_jwt_dependency)],
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[Session, Depends(get_db_session)],
    client_factory: Annotated[
        Any, Depends(get_canvas_mock_client_factory)
    ],
) -> JSONResponse:
    """Pull courses / attendance / grades from the mock and upsert.

    Returns 200 with per-resource counts. The full key never leaves
    the request scope; only its 8-character prefix is matched in the
    database.
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing X-Canvas-Mock-Api-Key header",
        )

    correlation_id = get_correlation_id() or new_correlation_id()
    tenant_id = _resolve_tenant_id(request, session, user_id)
    api_key_prefix = api_key[:8]

    # 1. Look up the registered user/key. A missing row means the
    #    client must call /auth/canvas-mock/connect first.
    mock_user = session.execute(
        select(CanvasMockUser).where(
            CanvasMockUser.tenant_id == tenant_id,
            CanvasMockUser.api_key_prefix == api_key_prefix,
        )
    ).scalar_one_or_none()
    if mock_user is None:
        logger.warning(
            "sync_mock_key_not_registered",
            tenant_id=str(tenant_id),
        )
        return _error_response(
            status_code=status.HTTP_404_NOT_FOUND,
            code="mock_key_not_registered",
            message="Call POST /auth/canvas-mock/connect first",
        )

    # 2. Mint a backend JWT for the mock. The mock requires the same
    #    dual-auth envelope as the extractor (X-Api-Key + Bearer).
    jwt_token, _ttl = issue_backend_jwt(
        str(tenant_id), settings=settings
    )

    # 3. Build the client + extractor; run the end-to-end pipeline.
    client = client_factory(settings, api_key, jwt_token)
    extractor = CanvasMockExtractor(
        client=client,
        session_factory=lambda: session,
    )
    try:
        try:
            counts = await extractor.fetch_and_upsert(
                tenant_id, list(SYNC_RESOURCES)
            )
        except CanvasMockShapeError as exc:
            logger.warning(
                "sync_mock_shape_failed",
                tenant_id=str(tenant_id),
                resource=exc.resource,
                index=exc.index,
            )
            return _error_response(
                status_code=status.HTTP_502_BAD_GATEWAY,
                code="sync_failed",
                message="Canvas mock returned malformed data",
                details={"resource": exc.resource, "index": exc.index},
            )
        except CanvasMockError as exc:
            logger.warning(
                "sync_mock_failed",
                tenant_id=str(tenant_id),
                error_class=exc.__class__.__name__,
            )
            return _error_response(
                status_code=status.HTTP_502_BAD_GATEWAY,
                code="sync_failed",
                message="Canvas mock rejected the request",
            )
        except CanvasMockTransientError as exc:
            logger.warning(
                "sync_mock_unavailable",
                tenant_id=str(tenant_id),
                error_class=exc.__class__.__name__,
            )
            return _error_response(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="mock_unavailable",
                message="Canvas mock is unavailable",
            )
    finally:
        await client.aclose()

    session.commit()

    logger.info(
        "sync_mock_completed",
        tenant_id=str(tenant_id),
        counts=counts,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "synced": {
                "courses": counts.get("courses", 0),
                "attendance": counts.get("attendance", 0),
                "grades": counts.get("grades", 0),
            },
            "tenant_id": str(tenant_id),
        },
        headers={"X-Correlation-ID": correlation_id},
    )


__all__ = [
    "_get_sync_mock_client_factory",
    "get_canvas_mock_client_factory",
    "post_sync_mock",
    "router",
]