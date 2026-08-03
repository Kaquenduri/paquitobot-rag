"""``POST /auth/canvas/connect`` controller (PR 2 task 2.6).

Flow (design §12.1, §12.7):

1. Verify the backend JWT.
2. Get-or-create the tenant for the JWT ``sub`` claim.
3. Probe Canvas ``GET /users/self`` with the plaintext token
   (held in memory only for this request).
4. On success: encrypt the token, persist it, return ``204 No Content``.
5. On Canvas rejection: 401 with ``code: "canvas_token_invalid"``.
   The error body is scrubbed so neither the ``Authorization`` header
   nor the plaintext token ever reaches the wire or a log line.

The router is registered here but is NOT wired into ``app.main`` —
``app.main`` is intentionally untouched in PR 2 (see
``apply-progress.md``). Tests mount the router via
``fastapi.testclient.TestClient`` on a dedicated app fixture.
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse

from app.core.config import Settings, get_settings
from app.core.deps import verify_backend_jwt_dependency
from app.core.errors import new_correlation_id
from app.core.logging import get_logger
from app.schemas.errors import ErrorBody
from app.security.token_crypto import TokenCipher
from app.services.tenant_service import get_tenant_service

logger = logging.getLogger("app.controllers.auth")

router = APIRouter(prefix="/auth/canvas", tags=["auth"])


async def _probe_canvas(canvas_token: str, settings: Settings) -> httpx.Response:
    """Probe Canvas ``GET /users/self`` with the plaintext token."""
    probe_url = settings.canvas_api_base_url.rstrip("/") + "/users/self"
    async with httpx.AsyncClient(timeout=8.0) as client:
        return await client.get(
            probe_url,
            headers={"Authorization": f"Bearer {canvas_token}"},
        )


def _canvas_invalid_response() -> JSONResponse:
    """Build the 401 response that the design §12.7 prescribes."""
    correlation_id = new_correlation_id()
    body = ErrorBody(
        code="canvas_token_invalid",
        message="Canvas rejected the token",
        correlation_id=correlation_id,
    )
    log = get_logger("app.controllers.auth")
    log.warning(
        "canvas_token_invalid",
        correlation_id=correlation_id,
    )
    response = JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content=body.model_dump(mode="json"),
    )
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


@router.post(
    "/connect",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def connect_canvas(
    canvas_token: Annotated[str, Header(alias="X-Canvas-Token")],
    user_id: Annotated[str, Depends(verify_backend_jwt_dependency)],
) -> Response:
    """Connect (or rotate) the caller's Canvas token."""
    if not canvas_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing X-Canvas-Token header",
        )

    settings: Settings = get_settings()
    service = get_tenant_service()
    tenant = service.get_or_create_tenant(user_id)
    cipher = TokenCipher(settings.tenant_token_key)

    try:
        response = await _probe_canvas(canvas_token, settings)
    except httpx.HTTPError as exc:
        get_logger("app.controllers.auth").warning(
            "canvas_probe_failed",
            tenant_id=str(tenant.id),
            error_class=exc.__class__.__name__,
        )
        return _canvas_invalid_response()

    if response.status_code == 401:
        return _canvas_invalid_response()
    if response.status_code >= 500:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="canvas_unavailable",
        )
    if response.status_code >= 400:
        return _canvas_invalid_response()

    # Persist the encrypted token (idempotent on tenant_id).
    service.store_canvas_token(tenant.id, canvas_token, cipher)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
