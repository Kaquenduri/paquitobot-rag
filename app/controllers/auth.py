"""Persistent ``POST /auth/canvas/connect`` controller.

The controller validates the supplied Canvas token with ``GET /users/self``
before any database write.  Accepted tokens are encrypted with
:class:`~app.security.token_crypto.TokenCipher` and persisted through a
request-scoped SQLAlchemy :class:`~sqlalchemy.orm.Session`; plaintext exists
only for the outbound probe and encryption call.

Standalone router harnesses from PR 2/3 retain the explicit in-memory
compatibility mode.  The canonical ``app.main`` application opts into the
SQLAlchemy store through app state, and SQLite-backed harnesses opt in
automatically.
"""

from __future__ import annotations

from typing import Annotated

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
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.core.deps import verify_backend_jwt_dependency
from app.core.errors import new_correlation_id
from app.core.logging import get_correlation_id, get_logger
from app.schemas.errors import ErrorBody
from app.security.token_crypto import TokenCipher
from app.services.tenant_service import (
    TenantService,
    get_tenant_service,
    should_use_session_store,
)

router = APIRouter(prefix="/auth/canvas", tags=["auth"])


def get_canvas_probe_transport() -> httpx.AsyncBaseTransport | None:
    """Return an optional HTTPX transport; production uses the real transport."""
    return None


async def _probe_canvas(
    canvas_token: str,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    """Probe Canvas ``GET /users/self`` with the transient plaintext token."""
    probe_url = settings.canvas_api_base_url.rstrip("/") + "/users/self"
    async with httpx.AsyncClient(timeout=8.0, transport=transport) as client:
        return await client.get(
            probe_url,
            headers={"Authorization": f"Bearer {canvas_token}"},
        )


def _canvas_invalid_response() -> JSONResponse:
    """Build the secret-safe 401 response prescribed by design section 12.7."""
    correlation_id = get_correlation_id() or new_correlation_id()
    body = ErrorBody(
        code="canvas_token_invalid",
        message="Canvas rejected the token",
        correlation_id=correlation_id,
    )
    get_logger("app.controllers.auth").warning(
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


def _tenant_service_for_request(
    request: Request,
    session: Session,
    cipher: TokenCipher,
) -> TenantService:
    """Resolve the canonical SQL store or the explicit legacy memory store."""
    if should_use_session_store(request.app.state, session):
        return TenantService(
            ciphers={cipher.key_version: cipher},
            session=session,
        )
    service = get_tenant_service()
    service.bind_cipher(cipher.key_version, cipher)
    return service


@router.post(
    "/connect",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def connect_canvas(
    request: Request,
    canvas_token: Annotated[str, Header(alias="X-Canvas-Token")],
    user_id: Annotated[str, Depends(verify_backend_jwt_dependency)],
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[Session, Depends(get_db_session)],
    probe_transport: Annotated[
        httpx.AsyncBaseTransport | None,
        Depends(get_canvas_probe_transport),
    ],
) -> Response:
    """Validate, encrypt, and durably associate a Canvas token to the caller."""
    if not canvas_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing X-Canvas-Token header",
        )

    try:
        if probe_transport is None:
            # Preserve the two-argument seam used by the original PR 2 smoke
            # tests while production still follows the normal HTTPX transport.
            response = await _probe_canvas(canvas_token, settings)
        else:
            response = await _probe_canvas(
                canvas_token,
                settings,
                transport=probe_transport,
            )
    except httpx.HTTPError as exc:
        get_logger("app.controllers.auth").warning(
            "canvas_probe_failed",
            error_class=exc.__class__.__name__,
        )
        return _canvas_invalid_response()

    if response.status_code == status.HTTP_401_UNAUTHORIZED:
        return _canvas_invalid_response()
    if response.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="canvas_unavailable",
        )
    if response.status_code >= status.HTTP_400_BAD_REQUEST:
        return _canvas_invalid_response()

    # The repository receives ciphertext only, and no durable write occurs
    # until the Canvas probe has accepted the token.
    cipher = TokenCipher(settings.tenant_token_key)
    envelope = cipher.encrypt(canvas_token)
    service = _tenant_service_for_request(request, session, cipher)
    tenant = service.get_or_create_tenant(user_id)
    persisted = service.store_canvas_token(
        tenant.id,
        encrypted_ciphertext=envelope.ciphertext,
        key_version=envelope.key_version,
    )
    if persisted is False:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="tenant_persistence_failed",
        )
    if not service._memory_store:
        session.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]


def _selftest() -> None:
    """Prove the HTTP route writes encrypted credentials using only local fakes."""
    from collections.abc import Generator

    from cryptography.fernet import Fernet
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.core.db import engine_for_url, session_factory_for
    from app.middleware.correlation_id import CorrelationIdMiddleware
    from app.models import Base, CanvasCredential, Tenant
    from app.security.token_crypto import EncryptedToken
    from app.services.tenant_service import SESSION_STORE_STATE_FLAG

    plaintext = "selftest-canvas-token"
    fernet_key = Fernet.generate_key().decode("ascii")
    settings = Settings(
        supabase_database_url="postgresql+psycopg://127.0.0.1:1/selftest",
        tenant_token_key=fernet_key,
        backend_secret="selftest-backend-secret-with-sufficient-length",
        gemini_api_key="selftest-gemini-placeholder",
        ollama_host="http://127.0.0.1:1",
        canvas_api_base_url="https://canvas.invalid/api/v1",
        scheduler_enabled=False,
    )
    engine = engine_for_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = session_factory_for(engine)

    def override_session() -> Generator[Session]:
        with factory() as db_session:
            yield db_session

    def canvas_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/users/self"
        authorization = request.headers["Authorization"]
        if authorization == "Bearer selftest-rejected-token":
            return httpx.Response(401, json={"error": "unauthorized"})
        assert authorization == f"Bearer {plaintext}"
        return httpx.Response(200, json={"id": 42})

    transport = httpx.MockTransport(canvas_handler)
    app = FastAPI()
    setattr(app.state, SESSION_STORE_STATE_FLAG, True)
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[verify_backend_jwt_dependency] = lambda: "selftest-user"
    app.dependency_overrides[get_canvas_probe_transport] = lambda: transport

    try:
        with TestClient(app) as client:
            response = client.post(
                "/auth/canvas/connect",
                headers={
                    "Authorization": "Bearer selftest-backend-token",
                    "X-Canvas-Token": plaintext,
                },
            )
            rejected = client.post(
                "/auth/canvas/connect",
                headers={
                    "Authorization": "Bearer selftest-backend-token",
                    "X-Canvas-Token": "selftest-rejected-token",
                },
            )
        assert response.status_code == status.HTTP_204_NO_CONTENT, response.text
        assert rejected.status_code == status.HTTP_401_UNAUTHORIZED
        assert rejected.json()["code"] == "canvas_token_invalid"
        assert "selftest-rejected-token" not in rejected.text

        with factory() as verification_session:
            tenant = verification_session.execute(
                select(Tenant).where(Tenant.backend_user_id == "selftest-user")
            ).scalar_one()
            credential = verification_session.execute(
                select(CanvasCredential).where(
                    CanvasCredential.tenant_id == tenant.id
                )
            ).scalar_one()
            assert credential.key_version == TokenCipher.CURRENT_KEY_VERSION
            assert isinstance(credential.ciphertext, bytes)
            assert plaintext.encode("utf-8") not in credential.ciphertext
            assert (
                TokenCipher(fernet_key).decrypt(
                    EncryptedToken(
                        ciphertext=credential.ciphertext,
                        key_version=credential.key_version,
                    )
                )
                == plaintext
            )
            persisted_service = TenantService(
                ciphers={
                    TokenCipher.CURRENT_KEY_VERSION: TokenCipher(fernet_key),
                },
                session=verification_session,
            )
            assert persisted_service.get_decrypted_canvas_token(tenant.id) == plaintext
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    _selftest()
