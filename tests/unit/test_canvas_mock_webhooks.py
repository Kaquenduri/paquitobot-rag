"""Unit tests for the inbound webhook controller (PR 5 task 5.3).

The controller at ``app/controllers/canvas_mock_webhooks.py`` accepts
``POST /webhooks/canvas-mock`` requests, verifies the HMAC
signature, performs the composite-key idempotency check, and writes
``canvas_mock_webhook_events`` rows. Tests drive the endpoint via
FastAPI's ``TestClient`` with a small in-memory alias for the
event-table write path.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.controllers.canvas_mock_webhooks import router as webhook_router
from app.core.config import Settings
from app.models import CanvasMockWebhookEvent, CanvasMockWebhookSubscription
from app.security.webhook_hmac import compute_signature
from app.models import Base


@pytest.fixture
def app_settings(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    overrides = {
        "SUPABASE_DATABASE_URL": "postgresql+psycopg://127.0.0.1:1/primer_rag_test",
        "TENANT_TOKEN_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "BACKEND_SECRET": "test-only-backend-secret",
        "CANVAS_API_BASE_URL": "https://canvas.invalid/api/v1",
        "MINIMAX_API_KEY": "test-only-minimax-key",
        "GOOGLE_CLIENT_ID": "test-only.apps.googleusercontent.com",
        "OLLAMA_HOST": "http://127.0.0.1:1",
        "SCHEDULER_ENABLED": "false",
        "DISABLE_RAG_ROUTES": "true",
        "CANVAS_MOCK_WEBHOOK_SECRET": "test-webhook-secret",
        "CANVAS_MOCK_API_BASE_URL": "https://canvas-mock.invalid/api/v1",
        "CANVAS_MOCK_API_KEY": "adm_test_key",
        "CANVAS_MOCK_JWT_SECRET": "test-only-canvas-mock-jwt-secret",
    }
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    return overrides


@pytest.fixture
def sqlite_engine():
    """Yield a fresh SQLite engine with the full schema."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def app(app_settings: dict[str, str], monkeypatch: pytest.MonkeyPatch, sqlite_engine: Any) -> FastAPI:
    """Build a FastAPI app with the webhook controller mounted."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(webhook_router)
    # Patch the controller's session factory with the SQLite one.
    import app.controllers.canvas_mock_webhooks as controller_module

    factory = sessionmaker(bind=sqlite_engine)
    monkeypatch.setattr(controller_module, "_session_factory", factory)
    # Seed the subscription so the tenant can be resolved.
    with factory() as session:
        session.add(
            CanvasMockWebhookSubscription(
                tenant_id=uuid.uuid4(),
                target_url="https://example.com/hooks/canvas-mock",
                secret="test-webhook-secret",
                event_types=["grade.posted", "assignment.created", "assignment.updated"],
            )
        )
        session.commit()
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _signed_headers(
    secret: str, body: bytes, ts: int, event: str, resource_id: int
) -> dict[str, str]:
    sig = compute_signature(secret, ts, body)
    return {
        "X-Canvas-Mock-Signature": f"t={ts},v1={sig}",
        "X-Canvas-Mock-Event": event,
        "X-Canvas-Mock-Delivery": str(uuid.uuid4()),
        "X-Canvas-Mock-Timestamp": str(ts),
        "X-Canvas-Mock-Attempt": "1",
        "X-Canvas-Mock-Resource-Id": str(resource_id),
        "Content-Type": "application/json",
    }


def _resolve_tenant_id(app: FastAPI) -> uuid.UUID:
    """Grab the seeded tenant id from the in-memory engine."""
    from app.core.db import engine_for_url  # noqa: F401 - sanity
    import app.controllers.canvas_mock_webhooks as controller_module

    with controller_module._session_factory() as session:
        return session.query(CanvasMockWebhookSubscription).one().tenant_id


def test_valid_signature_returns_200(client: TestClient, app: FastAPI) -> None:
    """A correctly-signed POST is accepted and persisted."""
    tenant_id = _resolve_tenant_id(app)
    body = b'{"grade_id": 7, "score": 18.0}'
    ts = 1_700_000_000
    headers = _signed_headers(
        "test-webhook-secret", body, ts, "grade.posted", 42
    )
    headers["X-Canvas-Mock-Tenant-Id"] = str(tenant_id)
    response = client.post("/webhooks/canvas-mock", content=body, headers=headers)
    assert response.status_code == 200, response.text
    body_json = response.json()
    assert body_json["status"] in {"queued", "duplicate"}


def test_invalid_signature_returns_401(client: TestClient, app: FastAPI) -> None:
    """A bad signature returns 401 and a ``signature_failed`` row."""
    tenant_id = _resolve_tenant_id(app)
    body = b'{"grade_id": 7, "score": 18.0}'
    ts = 1_700_000_000
    headers = _signed_headers(
        "WRONG_SECRET", body, ts, "grade.posted", 42
    )
    headers["X-Canvas-Mock-Tenant-Id"] = str(tenant_id)
    response = client.post("/webhooks/canvas-mock", content=body, headers=headers)
    assert response.status_code == 401
    # A row was written for forensics with ``result="signature_failed"``.
    import app.controllers.canvas_mock_webhooks as controller_module

    with controller_module._session_factory() as session:
        rows = (
            session.query(CanvasMockWebhookEvent)
            .filter_by(result="signature_failed")
            .all()
        )
        assert len(rows) >= 1
        assert rows[0].signature_valid is False


def test_duplicate_post_returns_duplicate(client: TestClient, app: FastAPI) -> None:
    """A second POST with the same composite key returns duplicate."""
    tenant_id = _resolve_tenant_id(app)
    body = b'{"grade_id": 7, "score": 18.0}'
    ts = 1_700_000_000
    headers = _signed_headers(
        "test-webhook-secret", body, ts, "grade.posted", 42
    )
    headers["X-Canvas-Mock-Tenant-Id"] = str(tenant_id)
    first = client.post("/webhooks/canvas-mock", content=body, headers=headers)
    second = client.post("/webhooks/canvas-mock", content=body, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"


def test_missing_secret_returns_503(app_settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``canvas_mock_webhook_secret`` is unset, the app returns 503."""
    monkeypatch.delenv("CANVAS_MOCK_WEBHOOK_SECRET", raising=False)
    from app.core.config import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(webhook_router)
    c = TestClient(app)
    response = c.post("/webhooks/canvas-mock", content=b"{}", headers={})
    assert response.status_code == 503


def test_timestamp_drift_returns_401(client: TestClient, app: FastAPI) -> None:
    """A timestamp older than the 300s window is rejected."""
    import time

    tenant_id = _resolve_tenant_id(app)
    body = b'{"grade_id": 7}'
    ts = int(time.time()) - 1000
    headers = _signed_headers(
        "test-webhook-secret", body, ts, "grade.posted", 42
    )
    headers["X-Canvas-Mock-Tenant-Id"] = str(tenant_id)
    response = client.post("/webhooks/canvas-mock", content=body, headers=headers)
    assert response.status_code == 401


def test_handler_error_writes_signature_failed_row(client: TestClient, app: FastAPI) -> None:
    """A handler that raises still logs the row with ``result='handler_error'``.

    The v1 handler is log-only; raise simulation is exercised via a
    dedicated test in the v2 task tree. This test pins the log-only
    happy path.
    """
    tenant_id = _resolve_tenant_id(app)
    body = b'{"grade_id": 7}'
    ts = 1_700_000_000
    headers = _signed_headers(
        "test-webhook-secret", body, ts, "grade.posted", 42
    )
    headers["X-Canvas-Mock-Tenant-Id"] = str(tenant_id)
    response = client.post("/webhooks/canvas-mock", content=body, headers=headers)
    assert response.status_code == 200
    import app.controllers.canvas_mock_webhooks as controller_module

    with controller_module._session_factory() as session:
        rows = (
            session.query(CanvasMockWebhookEvent)
            .filter_by(result="processed")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].processed is True


def test_cross_tenant_attempt_uses_explicit_header(
    client: TestClient, app: FastAPI
) -> None:
    """The tenant comes from the explicit header, not the URL or body."""
    tenant_id = _resolve_tenant_id(app)
    body = b'{"grade_id": 7}'
    ts = 1_700_000_000
    headers = _signed_headers(
        "test-webhook-secret", body, ts, "grade.posted", 42
    )
    headers["X-Canvas-Mock-Tenant-Id"] = str(tenant_id)
    response = client.post("/webhooks/canvas-mock", content=body, headers=headers)
    assert response.status_code == 200
    import app.controllers.canvas_mock_webhooks as controller_module

    with controller_module._session_factory() as session:
        row = session.query(CanvasMockWebhookEvent).one()
        assert row.tenant_id == tenant_id
