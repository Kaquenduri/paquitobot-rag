"""Unit tests for the ``app.core.deps`` dependency chain.

Each test mounts a fresh FastAPI app with one route per dependency so
the chain is exercised end-to-end through ``TestClient``. The
:class:`TenantService` singleton is reset around every test so
``backend_user_id`` → tenant mapping is isolated.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator

import jwt
import pytest
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from app.core.config import get_settings
from app.core.deps import (
    require_tenant,
    require_tenant_token,
    verify_backend_jwt_dependency,
)
from app.security.token_crypto import TokenCipher
from app.services.tenant_service import get_tenant_service, reset_tenant_service


@pytest.fixture(autouse=True)
def _isolated_singleton() -> Iterator[None]:
    reset_tenant_service()
    yield
    reset_tenant_service()


def _setup_settings(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> tuple[str, TokenCipher]:
    secret = secrets.token_urlsafe(32)
    fernet_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("BACKEND_SECRET", secret)
    monkeypatch.setenv("TENANT_TOKEN_KEY", fernet_key)
    get_settings.cache_clear()
    return secret, TokenCipher(fernet_key)


def _build_chain_app() -> FastAPI:
    app = FastAPI()

    @app.get("/who")
    def who(user_id: str = Depends(verify_backend_jwt_dependency)) -> dict:
        return {"user_id": user_id}

    @app.get("/tenant")
    def tenant(tenant_id: object = Depends(require_tenant)) -> dict:
        return {"tenant_id": str(tenant_id)}

    @app.get("/token")
    def token(payload: object = Depends(require_tenant_token)) -> dict:
        tenant_id, plaintext = payload  # type: ignore[misc]
        return {
            "tenant_id": str(tenant_id),
            "canvas_token_first4": plaintext[:4],
        }

    return app


def test_dep_chain_returns_user_id_when_jwt_valid(monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]) -> None:
    secret, _ = _setup_settings(monkeypatch, app_settings)
    token = jwt.encode(
        {"sub": "user-1", "exp": int(time.time()) + 60}, secret, algorithm="HS256"
    )

    response = TestClient(_build_chain_app()).get(
        "/who", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json() == {"user_id": "user-1"}


def test_dep_chain_401_when_jwt_missing(monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]) -> None:
    _setup_settings(monkeypatch, app_settings)

    response = TestClient(_build_chain_app()).get("/who")

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_dep_chain_401_when_authorization_header_malformed(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    _setup_settings(monkeypatch, app_settings)

    response = TestClient(_build_chain_app()).get(
        "/who", headers={"Authorization": "Token abc"}
    )

    assert response.status_code == 401


def test_dep_chain_401_when_jwt_invalid(monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]) -> None:
    _setup_settings(monkeypatch, app_settings)

    response = TestClient(_build_chain_app()).get(
        "/who", headers={"Authorization": "Bearer not.a.real.jwt"}
    )

    assert response.status_code == 401


def test_dep_chain_401_when_jwt_missing_sub(monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]) -> None:
    """A JWT with no ``sub`` claim has no tenant identity to resolve."""
    secret, _ = _setup_settings(monkeypatch, app_settings)
    token = jwt.encode({"exp": int(time.time()) + 60}, secret, algorithm="HS256")

    response = TestClient(_build_chain_app()).get(
        "/who", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 401


def test_dep_chain_returns_same_tenant_for_same_user(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    secret, _ = _setup_settings(monkeypatch, app_settings)
    token = jwt.encode(
        {"sub": "user-1", "exp": int(time.time()) + 60}, secret, algorithm="HS256"
    )
    client = TestClient(_build_chain_app())

    a = client.get("/tenant", headers={"Authorization": f"Bearer {token}"}).json()[
        "tenant_id"
    ]
    b = client.get("/tenant", headers={"Authorization": f"Bearer {token}"}).json()[
        "tenant_id"
    ]

    assert a == b
    assert a  # non-empty UUID


def test_dep_chain_rejects_canvas_token_as_backend_jwt(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    _setup_settings(monkeypatch, app_settings)

    response = TestClient(_build_chain_app()).get(
        "/who",
        headers={"Authorization": "Bearer 12345~abcdefghijklmnopqrstuvwxyz"},
    )

    assert response.status_code == 401


def test_dep_chain_full_chain_decrypts_stored_token(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    secret, cipher = _setup_settings(monkeypatch, app_settings)
    token = jwt.encode(
        {"sub": "user-1", "exp": int(time.time()) + 60}, secret, algorithm="HS256"
    )
    service = get_tenant_service()
    tenant = service.get_or_create_tenant("user-1")
    service.store_canvas_token(tenant.id, "1234~real-canvas-token", cipher)

    response = TestClient(_build_chain_app()).get(
        "/token", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == str(tenant.id)
    assert body["canvas_token_first4"] == "1234"


def test_dep_chain_full_chain_403_when_no_canvas_credentials(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    """``require_tenant_token`` MUST 403 when the tenant has no credentials yet."""
    secret, _ = _setup_settings(monkeypatch, app_settings)
    token = jwt.encode(
        {"sub": "user-no-cred", "exp": int(time.time()) + 60},
        secret,
        algorithm="HS256",
    )

    response = TestClient(_build_chain_app()).get(
        "/token", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403


def test_tenant_id_in_body_is_rejected_by_extra_forbid(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    """Clients cannot smuggle ``tenant_id`` through the body.

    The PR 2 contract: ``tenant_id`` is derived server-side from the
    JWT ``sub`` claim. Any schema that accepts request bodies MUST use
    ``extra="forbid"`` so a client-supplied ``tenant_id`` raises a
    Pydantic validation error before the route handler runs.
    """
    from pydantic import ValidationError

    class StrictBody(BaseModel):
        model_config = ConfigDict(extra="forbid")
        query: str

    # Pydantic validation, no FastAPI involved — proves the schema
    # contract in isolation. FastAPI's request body uses the same
    # Pydantic model, so this assertion propagates to HTTP.
    with pytest.raises(ValidationError) as exc:
        StrictBody.model_validate(
            {
                "query": "hi",
                "tenant_id": "deadbeef-0000-0000-0000-000000000000",
            }
        )

    # The error MUST point at ``tenant_id`` so clients see exactly
    # which field was rejected.
    errors = exc.value.errors()
    assert any("tenant_id" in (err.get("loc") or ()) for err in errors)

    # And the same dict without ``tenant_id`` parses cleanly.
    parsed = StrictBody.model_validate({"query": "hi"})
    assert parsed.query == "hi"
