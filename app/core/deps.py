"""Tenant dependency chain for FastAPI (PR 2 task 2.4).

The chain is::

    verify_backend_jwt() → require_tenant() → require_tenant_token()

Every step is mandatory: skipping a step raises ``HTTPException(403)``
(or 401 for the JWT step). ``tenant_id`` is derived exclusively from
the JWT ``sub`` claim; clients that send ``tenant_id`` in the body or
query string are rejected by the Pydantic schema (``extra="forbid"``)
and by the dependency signature itself — the parameter list never
reads ``tenant_id`` from the request.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status

from app.security.backend_auth import InvalidToken, verify_backend_jwt
from app.security.token_crypto import InvalidToken as CanvasTokenInvalid
from app.services.tenant_service import (
    TenantNotFound,
    get_tenant_service,
)


def _bearer_authorization(authorization: str | None = Header(default=None)) -> str:
    """Extract the bearer token from the ``Authorization`` header."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must be 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return parts[1].strip()


def verify_backend_jwt_dependency(
    token: Annotated[str, Depends(_bearer_authorization)],
) -> str:
    """FastAPI dependency: verify the JWT and return the ``sub`` claim."""
    try:
        return verify_backend_jwt(token)
    except InvalidToken as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid backend token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def require_tenant(
    user_id: Annotated[str, Depends(verify_backend_jwt_dependency)],
) -> UUID:
    """FastAPI dependency: resolve (or create) the tenant for ``user_id``."""
    service = get_tenant_service()
    try:
        record = service.get_or_create_tenant(user_id)
    except TenantNotFound as exc:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tenant resolution failed",
        ) from exc
    return record.id


def require_tenant_token(
    tenant_id: Annotated[UUID, Depends(require_tenant)],
) -> tuple[UUID, str]:
    """FastAPI dependency: decrypt the Canvas token for the tenant.

    Returns ``(tenant_id, plaintext_canvas_token)``. The plaintext is
    held only in the request scope; callers MUST NOT persist or echo
    it back to a client response.
    """
    service = get_tenant_service()
    try:
        plaintext = service.get_decrypted_canvas_token(tenant_id)
    except (TenantNotFound, CanvasTokenInvalid, UnicodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="no Canvas credentials for tenant",
        ) from exc
    return tenant_id, plaintext


__all__ = [
    "require_tenant",
    "require_tenant_token",
    "verify_backend_jwt_dependency",
]
