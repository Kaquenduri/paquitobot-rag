"""Pytest fixtures shared across the test suite.

PR 1 introduces the ``python_executable`` and ``app_settings``
fixtures; PR 3 adds a per-test SQLite engine + session so unit
tests can exercise the SQLAlchemy models and the repository helpers
without touching the real Supabase database.

PR 5 adds a ``webhook_signer`` factory for the canvas-mock webhook
tests and a ``webhook_subscription`` factory that seeds a row in
``canvas_mock_webhook_subscriptions`` so the receiver can resolve
the tenant by ``target_url``.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.db import create_all, drop_all, engine_for_url, session_factory_for
from app.core.logging import configure_console_encoding

WINDOWS_PYTHON_EXECUTABLE = r"C:\Users\Administrador\langchain\Scripts\python.exe"

# Reconfigure stdout/stderr to UTF-8 so structlog's traceback
# rendering (which emits Unicode box-drawing characters) does not
# crash on Windows cp1252 shells.
configure_console_encoding()


@pytest.fixture
def python_executable() -> str:
    return WINDOWS_PYTHON_EXECUTABLE


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
        "CANVAS_MOCK_WEBHOOK_SECRET": "test-only-canvas-mock-webhook-secret",
        "CANVAS_MOCK_API_BASE_URL": "https://canvas-mock.invalid/api/v1",
        "CANVAS_MOCK_API_KEY": "adm_test_key",
        "CANVAS_MOCK_JWT_SECRET": "test-only-canvas-mock-jwt-secret",
    }
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    return overrides


# ---------------------------------------------------------------------------
# PR 3: SQLite engine + session
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_engine() -> Generator[Engine]:
    """Yield a fresh SQLite in-memory engine with the full schema.

    Each test gets a brand-new database so they cannot leak rows into
    one another. The engine is disposed after the test to release the
    file descriptor.
    """
    engine = engine_for_url("sqlite:///:memory:")
    create_all(engine)
    try:
        yield engine
    finally:
        drop_all(engine)
        engine.dispose()


@pytest.fixture
def db_session(sqlite_engine: Engine) -> Generator[Session]:
    """Yield a SQLAlchemy session bound to ``sqlite_engine``."""
    factory = session_factory_for(sqlite_engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Webhook (PR 5)
# ---------------------------------------------------------------------------


@pytest.fixture
def webhook_signer() -> Any:
    """Return a helper that builds signed headers for a webhook delivery.

    Usage::

        head = webhook_signer(tenant_id, body=b'{"x":1}')("overridden")
        # head is now a dict with X-Canvas-Mock-Signature, etc.
    """

    import time
    import uuid
    from app.security.webhook_hmac import compute_signature

    secret = "test-webhook-secret"

    def _make(tenant_id: Any, body: bytes, event: str = "grade.posted", resource_id: int = 42):
        def _factory(alt_body: str = "") -> dict[str, str]:
            ts = int(time.time())
            payload = body if alt_body == "" else alt_body.encode("utf-8")
            sig = compute_signature(secret, ts, payload)
            return {
                "X-Canvas-Mock-Signature": f"t={ts},v1={sig}",
                "X-Canvas-Mock-Event": event,
                "X-Canvas-Mock-Delivery": str(uuid.uuid4()),
                "X-Canvas-Mock-Timestamp": str(ts),
                "X-Canvas-Mock-Attempt": "1",
                "X-Canvas-Mock-Resource-Id": str(resource_id),
                "X-Canvas-Mock-Tenant-Id": str(tenant_id),
                "Content-Type": "application/json",
            }

        return _factory

    return _make


@pytest.fixture
def webhook_subscription(db_session: Session) -> Any:
    """Insert a :class:`CanvasMockWebhookSubscription` row and return it."""
    from app.models import CanvasMockWebhookSubscription

    sub = CanvasMockWebhookSubscription(
        tenant_id=uuid.uuid4(),
        target_url="https://example.com/hooks/canvas-mock",
        secret="test-webhook-secret",
        event_types=["grade.posted", "assignment.created", "assignment.updated"],
    )
    db_session.add(sub)
    db_session.commit()
    return sub