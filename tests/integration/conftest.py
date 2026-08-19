"""Integration conftest for the canvas-mock end-to-end suite (PR 6 task 6.5).

Lives next to ``tests/integration/`` so the cross-tenant smoke tests
have a private fixture that:

- builds a fresh SQLite-backed schema (via the canonical
  ``Base.metadata``);
- inserts a tenant + ``canvas_mock_users`` + ``canvas_mock_webhook_subscriptions``
  rows so the receiver can resolve the tenant by ``target_url``;
- rolls the schema back on teardown.

The conftest is intentionally small: it does NOT depend on the
unit-suite conftest so the integration tests can run standalone
(via pytest's collection of the ``tests/integration/`` directory
only — e.g. ``INTEGRATION=1 uv run pytest tests/integration``).
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    CanvasMockUser,
    CanvasMockWebhookSubscription,
)


@pytest.fixture
def sqlite_engine() -> Generator[Any]:
    """Yield a fresh SQLite engine with the full schema."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session_factory(sqlite_engine: Any) -> Any:
    """Yield a sessionmaker bound to ``sqlite_engine``."""
    return sessionmaker(bind=sqlite_engine)


@pytest.fixture
def mock_tenant(session_factory: Any) -> Generator[dict[str, Any]]:
    """Insert a tenant + canvas_mock_users + subscription row.

    Yields a dict with the tenant id, the session factory, and the
    subscription row so individual tests can plug them into the
    receiver's ``_session_factory`` and the controller's payload.
    """
    tenant_id = uuid.uuid4()
    user_canvas_mock_id = 77

    with session_factory() as session:
        session.add(
            CanvasMockUser(
                tenant_id=tenant_id,
                canvas_mock_id=user_canvas_mock_id,
                role="student",
                api_key_prefix="ab12cd34",
            )
        )
        subscription = CanvasMockWebhookSubscription(
            tenant_id=tenant_id,
            target_url="https://example.com/hooks/canvas-mock",
            secret="integration-webhook-secret",
            event_types=["grade.posted", "assignment.created", "assignment.updated"],
        )
        session.add(subscription)
        session.commit()

    yield {
        "tenant_id": tenant_id,
        "user_canvas_mock_id": user_canvas_mock_id,
        "subscription": subscription,
        "session_factory": session_factory,
    }
