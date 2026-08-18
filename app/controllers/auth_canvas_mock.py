"""``POST /auth/canvas-mock/connect`` controller (canvas-mock only).

This module is the **parallel** of :mod:`app.controllers.auth` but
targets the ``canvas-mock-api`` instead of the real Canvas LMS. It
probes the mock with the user-supplied ``X-Canvas-Mock-Api-Key``
header, parses the role + ``mock_user_id`` out of the probe
response, and upserts a row into ``canvas_mock_users`` so future
``/sync-mock`` calls can resolve the same tenant without re-asking
for credentials.

The route is **dedicated to the canvas-mock-api**: it MUST NOT touch
``settings.canvas_api_base_url``. The probe URL is constructed from
``settings.canvas_mock_api_base_url`` only. Any setting that looks
like a real-Canvas URL is intentionally never read here.

Behavior:

- **Probe 200 → 204 No Content.** The full key is NEVER persisted;
  only the first 8 characters (``api_key_prefix``) are stored.
- **Probe 401 → 401 ``invalid_mock_key``.** No row is written.
- **Probe 5xx or network error → 503 ``mock_unavailable``.** No row
  is written.

The tenant is resolved the same way as the legacy controller: the
``verify_backend_jwt_dependency`` yields ``user_id`` (the Google
``sub``), and :class:`TenantService` looks up (or creates) the
canonical ``tenants`` row.
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
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
from app.services.tenant_service import (
    TenantService,
    get_tenant_service,
    should_use_session_store,
)

logger = get_logger("app.controllers.auth_canvas_mock")

router = APIRouter(prefix="/auth/canvas-mock", tags=["auth-canvas-mock"])


def get_canvas_mock_probe_transport() -> httpx.AsyncBaseTransport | None:
    """Return an optional HTTPX transport; production uses the real transport."""
    return None


# Alias the test seam name (imported by name in the test module).
_get_mock_probe_transport = get_canvas_mock_probe_transport


def _tenant_service_for_request(
    request: Request,
    session: Session,
) -> TenantService:
    """Resolve the canonical SQL store or the explicit legacy memory store.

    The control flow is identical to :mod:`app.controllers.auth`; we
    re-import the helper here so the test seam can override either
    service independently of the legacy controller.
    """
    if should_use_session_store(request.app.state, session):
        return TenantService(session=session)
    return get_tenant_service()


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build a redacted error response (never echo the key)."""
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


async def _probe_mock(
    settings: Settings,
    api_key: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    """GET ``<canvas_mock_api_base_url>/_auth/probe`` with ``X-Api-Key``.

    Uses the settings' ``canvas_mock_api_base_url`` ONLY — the real
    Canvas URL is never touched.
    """
    probe_url = settings.canvas_mock_api_base_url.rstrip("/") + "/_auth/probe"
    async with httpx.AsyncClient(timeout=8.0, transport=transport) as client:
        return await client.get(
            probe_url,
            headers={"X-Api-Key": api_key},
        )


@router.post(
    "/connect",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def connect_canvas_mock(
    request: Request,
    api_key: Annotated[str, Header(alias="X-Canvas-Mock-Api-Key")],
    user_id: Annotated[str, Depends(verify_backend_jwt_dependency)],
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[Session, Depends(get_db_session)],
    probe_transport: Annotated[
        httpx.AsyncBaseTransport | None,
        Depends(get_canvas_mock_probe_transport),
    ],
) -> Response:
    """Probe the mock, then upsert the (tenant, prefix) binding.

    Returns 204 on success, 401 when the mock rejects the key, and
    503 when the mock is unreachable or returns 5xx. The full key is
    never persisted and never echoed back; only ``api_key_prefix``
    (first 8 chars) lands in the database.
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing X-Canvas-Mock-Api-Key header",
        )

    try:
        if probe_transport is None:
            response = await _probe_mock(settings, api_key)
        else:
            response = await _probe_mock(settings, api_key, transport=probe_transport)
    except httpx.HTTPError as exc:
        logger.warning(
            "canvas_mock_probe_failed",
            error_class=exc.__class__.__name__,
        )
        return _error_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="mock_unavailable",
            message="Canvas mock is unavailable",
        )

    if response.status_code == status.HTTP_401_UNAUTHORIZED:
        logger.warning("invalid_mock_key")
        return _error_response(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="invalid_mock_key",
            message="Canvas mock rejected the API key",
        )
    if response.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        logger.warning(
            "canvas_mock_5xx",
            status_code=response.status_code,
        )
        return _error_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="mock_unavailable",
            message="Canvas mock is unavailable",
        )
    if response.status_code >= status.HTTP_400_BAD_REQUEST:
        return _error_response(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="invalid_mock_key",
            message="Canvas mock rejected the API key",
        )

    # Parse the probe body. The mock returns ``{id, role, ...}`` per
    # REQ-AUTH-1 in the canvas-mock-api spec.
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        logger.warning(
            "canvas_mock_probe_bad_json",
            error_class=exc.__class__.__name__,
        )
        return _error_response(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="mock_bad_response",
            message="Canvas mock returned an unreadable response",
        )
    if not isinstance(payload, dict):
        return _error_response(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="mock_bad_response",
            message="Canvas mock returned an invalid response",
        )
    mock_user_id = payload.get("id")
    role = payload.get("role")
    if not isinstance(mock_user_id, int) or not isinstance(role, str):
        return _error_response(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="mock_bad_response",
            message="Canvas mock response missing id or role",
        )

    # Resolve the tenant via the canonical chain (same seam as the
    # legacy /auth/canvas/connect controller).
    service = _tenant_service_for_request(request, session)
    tenant = service.get_or_create_tenant(user_id)
    tenant_id = tenant.id

    # Upsert canvas_mock_users on (tenant_id, canvas_mock_id). The
    # api_key_prefix is the first 8 chars of the key (the mock only
    # echoes the prefix back per REQ-AUTH-2 in the mock spec).
    api_key_prefix = api_key[:8]
    existing = session.execute(
        select(CanvasMockUser).where(
            CanvasMockUser.tenant_id == tenant_id,
            CanvasMockUser.canvas_mock_id == mock_user_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = session.execute(
            select(CanvasMockUser).where(
                CanvasMockUser.tenant_id == tenant_id,
                CanvasMockUser.api_key_prefix == api_key_prefix,
            )
        ).scalar_one_or_none()
    if existing is None:
        session.add(
            CanvasMockUser(
                tenant_id=tenant_id,
                canvas_mock_id=mock_user_id,
                api_key_prefix=api_key_prefix,
                role=role,
            )
        )
    else:
        existing.api_key_prefix = api_key_prefix
        existing.role = role
    session.commit()
    logger.info(
        "canvas_mock_connected",
        tenant_id=str(tenant_id),
        canvas_mock_id=mock_user_id,
        role=role,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = [
    "_get_mock_probe_transport",
    "connect_canvas_mock",
    "get_canvas_mock_probe_transport",
    "router",
]
