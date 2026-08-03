"""Authenticated manual synchronization controller.

The route is intentionally opt-in: importing this module creates an
``APIRouter`` but ``app.main`` does not include it yet.  Its dependency graph
is explicit and ordered by nesting::

    verify_backend_jwt_dependency -> require_tenant -> require_tenant_token

The route receives only the resulting server-derived tenant context; no
request body or query parameter can override ``tenant_id``.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.core.deps import require_tenant_token
from app.core.errors import new_correlation_id
from app.core.logging import get_logger
from app.schemas.errors import ErrorBody
from app.services.canvas_service import (
    CanvasService,
    SyncThrottled,
    TenantCredentialsMissing,
)
from app.services.tenant_service import get_tenant_service

logger = get_logger("app.controllers.sync")


class _SyncRoute(APIRoute):
    """Keep the bare-router credential error shape safe and explicit."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except HTTPException as exc:
                if exc.status_code == status.HTTP_403_FORBIDDEN and "Canvas credentials" in str(
                    exc.detail
                ):
                    return _forbidden_response(
                        code="tenant_credentials_missing",
                        message="No Canvas credentials for tenant",
                        correlation_id=new_correlation_id(),
                    )
                raise

        return handler


router = APIRouter(
    prefix="/sync",
    tags=["sync"],
    route_class=_SyncRoute,
)


def get_canvas_service(
    settings: Annotated[Settings, Depends(get_settings)],
    tenant_service: Annotated[Any, Depends(get_tenant_service)],
) -> CanvasService:
    """Build the service from application settings and the tenant store."""
    return CanvasService(settings=settings, tenant_service=tenant_service)


def _retry_after_response(
    *,
    code: str,
    message: str,
    retry_after_seconds: int,
    correlation_id: str | None = None,
) -> JSONResponse:
    retry_after = max(1, int(retry_after_seconds))
    cid = correlation_id or new_correlation_id()
    body = ErrorBody(
        code=code,
        message=message,
        correlation_id=cid,
        details={"retry_after_seconds": retry_after},
    )
    response = JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content=body.model_dump(mode="json"),
    )
    response.headers["Retry-After"] = str(retry_after)
    response.headers["X-Correlation-ID"] = cid
    return response


def _forbidden_response(
    *,
    code: str,
    message: str,
    correlation_id: str,
) -> JSONResponse:
    body = ErrorBody(
        code=code,
        message=message,
        correlation_id=correlation_id,
    )
    response = JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content=body.model_dump(mode="json"),
    )
    response.headers["X-Correlation-ID"] = correlation_id
    return response


def _coerce_uuid(value: str | uuid.UUID) -> uuid.UUID:
    """Coerce the dependency result to a UUID without reading payload data."""
    if isinstance(value, uuid.UUID):
        return value
    raw = str(value)
    if raw.startswith("UUID(") and raw.endswith(")"):
        raw = raw[5:-1].strip("'\"")
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_tenant_id", "message": "invalid tenant id"},
        ) from exc


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def post_sync(
    tenant_context: Annotated[tuple[uuid.UUID, str], Depends(require_tenant_token)],
    settings: Annotated[Settings, Depends(get_settings)],
    service: Annotated[CanvasService, Depends(get_canvas_service)],
) -> JSONResponse:
    """Start one authenticated tenant sync and return its safe status."""
    # ``require_tenant_token`` has already executed the complete nested chain.
    # The plaintext token is intentionally unpacked only to prove the context
    # shape; CanvasService resolves the encrypted credential itself and the
    # value is never placed in a payload or log.
    raw_tenant_id, _canvas_token = tenant_context
    tenant_uuid = _coerce_uuid(raw_tenant_id)
    correlation_id = new_correlation_id()

    # A few local harnesses replace the generator with a zero-argument
    # fixture.  Inspecting the callable keeps that seam compatible while the
    # production dependency still receives Settings.
    if len(inspect.signature(get_db_session).parameters) == 0:
        session_generator = get_db_session()
    else:
        session_generator = get_db_session(settings)
    session: Session = next(session_generator)
    try:
        try:
            service.enforce_manual_rate_limit(session, tenant_uuid)
            # Persist the throttle stamp before the potentially long Canvas
            # request.  A lock-busy result still returns 429 and rolls back
            # any transient pipeline work.
            session.commit()
        except SyncThrottled as exc:
            session.rollback()
            logger.info(
                "sync_throttled",
                tenant_id=str(tenant_uuid),
                retry_after_seconds=exc.retry_after_seconds,
                correlation_id=correlation_id,
            )
            return _retry_after_response(
                code="sync_throttled",
                message="Manual sync rate limit exceeded",
                retry_after_seconds=exc.retry_after_seconds,
                correlation_id=correlation_id,
            )

        try:
            run_method = service.run_sync_for_tenant
            try:
                accepts_session = "session" in inspect.signature(run_method).parameters
            except (TypeError, ValueError):
                accepts_session = True
            if accepts_session:
                result = await run_method(tenant_uuid, session=session)
            else:
                result = await run_method(tenant_uuid)
        except TenantCredentialsMissing:
            session.rollback()
            return _forbidden_response(
                code="tenant_credentials_missing",
                message="No Canvas credentials for tenant",
                correlation_id=correlation_id,
            )

        if result.status == "locked" or result.last_status == "locked":
            session.rollback()
            logger.info(
                "sync_lock_busy",
                tenant_id=str(tenant_uuid),
                correlation_id=correlation_id,
            )
            return _retry_after_response(
                code="sync_locked",
                message="A sync is already running for this tenant",
                retry_after_seconds=getattr(
                    settings, "manual_sync_min_interval_seconds", 60
                ),
                correlation_id=correlation_id,
            )

        session.commit()
        content: dict[str, Any] = {
            "status": result.status,
            "last_successful_at": (
                result.last_successful_at.isoformat()
                if result.last_successful_at is not None
                else None
            ),
            "last_status": result.last_status,
            "last_error_class": result.last_error_class,
            # Kept as an additive diagnostic for existing clients; the four
            # fields above are the stable PR 4 response contract.
            "correlation_id": correlation_id,
        }
        response = JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=content,
        )
        response.headers["X-Correlation-ID"] = correlation_id
        return response
    finally:
        try:
            next(session_generator, None)
        except StopIteration:
            pass
        except Exception:  # pragma: no cover - dependency cleanup guard
            logger.exception("sync_session_cleanup_failed")


def register_sync_router(app: Any) -> None:
    """Mount the router explicitly; never called from ``app.main`` in PR 4."""
    app.include_router(router)


__all__ = ["get_canvas_service", "post_sync", "register_sync_router", "router"]


def _selftest() -> None:
    """Run controller response/tenant-context assertions locally."""
    response = _retry_after_response(
        code="sync_locked",
        message="busy",
        retry_after_seconds=0,
        correlation_id="selftest-correlation",
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert _coerce_uuid("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa").version == 4


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    _selftest()
