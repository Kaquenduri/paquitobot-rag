"""Cross-tenant smoke test for the canvas-mock webhook flow (PR 6 task 6.1).

Runs ONLY when the env var ``INTEGRATION=1`` is set. The test:
- bootstraps two tenants (A and B) with subscriptions pointing at
  the same target URL.
- posts a webhook signed as tenant A → 200 + row written.
- posts the same webhook as tenant B (with B's signature) → 200 + a
  SEPARATE row written; tenant A's row is untouched.

This is the cross-tenant safety net the open spec calls for. It
exercises the receiver, the idempotency check, and the composite
unique index together.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import pytest

# Self-sufficient env setup so the integration tests can run without
# the test suite's conftest.py (the conftest path is on the test
# runner; the integration conftest will be added in PR 6 task 6.5).
if os.environ.get("INTEGRATION") == "1":
    os.environ.setdefault(
        "SUPABASE_DATABASE_URL",
        "postgresql+psycopg://127.0.0.1:1/primer_rag_integration",
    )
    os.environ.setdefault(
        "TENANT_TOKEN_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    )
    os.environ.setdefault("BACKEND_SECRET", "integration-backend-secret")
    os.environ.setdefault("CANVAS_API_BASE_URL", "https://canvas.invalid/api/v1")
    os.environ.setdefault("MINIMAX_API_KEY", "integration-minimax-key")
    os.environ.setdefault(
        "GOOGLE_CLIENT_ID", "integration.apps.googleusercontent.com"
    )
    os.environ.setdefault("OLLAMA_HOST", "http://127.0.0.1:1")
    os.environ.setdefault("SCHEDULER_ENABLED", "false")
    os.environ.setdefault("DISABLE_RAG_ROUTES", "true")
    os.environ.setdefault("CANVAS_MOCK_WEBHOOK_SECRET", "test-webhook-secret")
    os.environ.setdefault(
        "CANVAS_MOCK_API_BASE_URL", "https://canvas-mock.invalid/api/v1"
    )
    os.environ.setdefault("CANVAS_MOCK_API_KEY", "adm_integration_key")
    os.environ.setdefault("CANVAS_MOCK_JWT_SECRET", "integration-canvas-mock-jwt")


# Skip unless the operator has explicitly opted in.
pytestmark = pytest.mark.skipif(
    os.environ.get("INTEGRATION") != "1",
    reason="integration test; set INTEGRATION=1 to run",
)


def _signed(secret: str, ts: int, body: bytes) -> str:
    from app.security.webhook_hmac import compute_signature

    return f"t={ts},v1={compute_signature(secret, ts, body)}"


def test_cross_tenant_webhooks_round_trip() -> None:
    """Two tenants with the same webhook URL are kept strictly separate."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.controllers.canvas_mock_webhooks import router as webhook_router
    from app.models import (
        Base,
        CanvasMockWebhookEvent,
        CanvasMockWebhookSubscription,
    )

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    with factory() as session:
        for tid in (tenant_a, tenant_b):
            session.add(
                CanvasMockWebhookSubscription(
                    tenant_id=tid,
                    target_url="https://example.com/hooks/canvas-mock",
                    secret="test-webhook-secret",
                    event_types=["grade.posted"],
                )
            )
        session.commit()

    app = FastAPI()
    app.include_router(webhook_router)
    import app.controllers.canvas_mock_webhooks as controller_module

    controller_module._session_factory = factory

    client = TestClient(app)

    body = b'{"grade_id": 7, "score": 18.0}'
    ts = int(time.time())

    # Tenant A
    headers_a = {
        "X-Canvas-Mock-Signature": _signed("test-webhook-secret", ts, body),
        "X-Canvas-Mock-Event": "grade.posted",
        "X-Canvas-Mock-Delivery": str(uuid.uuid4()),
        "X-Canvas-Mock-Timestamp": str(ts),
        "X-Canvas-Mock-Attempt": "1",
        "X-Canvas-Mock-Resource-Id": "42",
        "X-Canvas-Mock-Tenant-Id": str(tenant_a),
        "Content-Type": "application/json",
    }
    response_a = client.post("/webhooks/canvas-mock", content=body, headers=headers_a)
    assert response_a.status_code == 200, response_a.text

    # Tenant B with the SAME composite key (event, resource_id, attempt_ts)
    # but a different tenant_id. The composite UQ is per-tenant, so the
    # second insert MUST succeed.
    headers_b = dict(headers_a)
    headers_b["X-Canvas-Mock-Tenant-Id"] = str(tenant_b)
    response_b = client.post("/webhooks/canvas-mock", content=body, headers=headers_b)
    assert response_b.status_code == 200, response_b.text

    with factory() as session:
        rows = (
            session.query(CanvasMockWebhookEvent)
            .order_by(CanvasMockWebhookEvent.tenant_id)
            .all()
        )
        assert len(rows) == 2
        tenants = {str(row.tenant_id) for row in rows}
        assert tenants == {str(tenant_a), str(tenant_b)}
        for row in rows:
            assert row.event == "grade.posted"
            assert row.resource_id == 42
            assert row.attempt_ts == ts
            assert row.signature_valid is True
            assert row.result == "processed"
            assert row.processed is True


def test_missing_webhook_secret_returns_503() -> None:
    """When ``canvas_mock_webhook_secret`` is empty, the receiver returns 503."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.controllers.canvas_mock_webhooks import router as webhook_router
    from app.core.config import get_settings

    settings = get_settings()
    original = settings.canvas_mock_webhook_secret
    settings.canvas_mock_webhook_secret = ""
    try:
        app = FastAPI()
        app.include_router(webhook_router)
        client = TestClient(app)
        response = client.post("/webhooks/canvas-mock", content=b"{}", headers={})
        assert response.status_code == 503
    finally:
        settings.canvas_mock_webhook_secret = original
