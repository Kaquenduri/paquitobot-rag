"""Inbound webhook controller for the canvas-mock-api (PR 5 task 5.4).

The endpoint is ``POST /webhooks/canvas-mock``. The pipeline is:

1. **Read the raw body FIRST.** The HMAC is computed over the whole
   body, so any middleware that consumes the stream before us would
   break verification. We call :meth:`Request.body` exactly once.

2. **Header validation.** The required headers are
   ``X-Canvas-Mock-Signature``, ``X-Canvas-Mock-Event``,
   ``X-Canvas-Mock-Delivery``, ``X-Canvas-Mock-Timestamp``,
   ``X-Canvas-Mock-Attempt``, and ``X-Canvas-Mock-Tenant-Id``. The
   receiver fails closed if any are missing.

3. **Signature verification.** Delegates to
   :mod:`app.security.webhook_hmac`. The secret comes from
   :class:`app.core.config.Settings.canvas_mock_webhook_secret`. If
   it is unset, the controller returns 503 (the test ``test_missing_secret_returns_503``
   pins this).

4. **Idempotency check.** The composite key
   ``(tenant_id, event, resource_id, attempt_ts)`` is unique on
   :class:`app.models.CanvasMockWebhookEvent`. The receiver uses
   ``SELECT-then-INSERT`` (via ``session.add`` + ``session.commit``)
   to detect duplicates; a second post with the same composite
   returns 200 ``{"status": "duplicate"}`` and does NOT re-execute
   the handler.

5. **Handler dispatch.** v2 handlers are async and call the
   :class:`CanvasMockExtractor` upsert pipeline (``upsert_assignments``
   for ``assignment.{created,updated}``, ``upsert_grades`` for
   ``grade.posted``). The endpoint ``await``s each handler; sync
   handlers remain backward-compatible (``await None`` is a no-op).

6. **Persistence.** Whether or not the handler succeeded, a row is
   written to ``canvas_mock_webhook_events`` with the resulting
   ``result`` enum (``processed`` / ``duplicate`` /
   ``handler_error`` / ``signature_failed``). The raw body and
   signature MUST NEVER appear in logs.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import session_factory_for
from app.core.logging import get_logger
from app.models import CanvasMockWebhookEvent, CanvasMockWebhookSubscription
from app.security.webhook_hmac import (
    verify_signature,
)
from app.services.canvas_mock_client import CanvasMockClient
from app.services.canvas_mock_extractor import CanvasMockExtractor

logger = get_logger("app.controllers.canvas_mock_webhooks")

router = APIRouter()


# ---------------------------------------------------------------------------
# Module-level session factory (overridden by the test conftest).
# ---------------------------------------------------------------------------


# Lazy default: build a factory from the cached settings. The test
# monkeypatch replaces this attribute with a SQLite-backed factory.
_session_factory: Any = None


def _factory() -> Session:
    global _session_factory
    if _session_factory is None:
        settings = get_settings()
        from app.core.db import make_engine_from_settings

        engine = make_engine_from_settings(settings)
        _session_factory = session_factory_for(engine)
    return _session_factory()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_REQUIRED_HEADERS: tuple[str, ...] = (
    "x-canvas-mock-signature",
    "x-canvas-mock-event",
    "x-canvas-mock-delivery",
    "x-canvas-mock-timestamp",
    "x-canvas-mock-attempt",
    "x-canvas-mock-tenant-id",
)


def _missing_headers(headers: Any) -> list[str]:
    missing = []
    for h in _REQUIRED_HEADERS:
        if not headers.get(h):
            missing.append(h)
    return missing


def _resolve_tenant_id(session: Session, target_url: str) -> uuid.UUID | None:
    """Find the tenant that owns the subscription matching ``target_url``.

    The mock enforces that the subscription was registered by the
    tenant and uses the secret stored there. The receiver matches by
    ``target_url`` so a forged cross-tenant request cannot pass this
    check.
    """
    sub = (
        session.query(CanvasMockWebhookSubscription)
        .filter_by(target_url=target_url)
        .one_or_none()
    )
    return sub.tenant_id if sub else None


# ---------------------------------------------------------------------------
# Handler registry (v2: real upserts via CanvasMockExtractor)
# ---------------------------------------------------------------------------


_extractor_client: CanvasMockClient | None = None


def _get_extractor_client() -> CanvasMockClient:
    """Lazy-build a :class:`CanvasMockClient` for the upsert path.

    The handlers never call :meth:`get` on the client (they only
    upsert), so the URLs are effectively unreachable; we still build
    a real client so :class:`CanvasMockExtractor`'s type contract is
    satisfied under strict mypy.
    """
    global _extractor_client
    if _extractor_client is None:
        settings = get_settings()
        _extractor_client = CanvasMockClient(
            base_url=settings.canvas_mock_api_base_url,
            api_key=settings.canvas_mock_api_key,
            jwt_token=settings.canvas_mock_jwt_secret,
        )
    return _extractor_client


async def _handle_assignment_event(
    tenant_id: uuid.UUID, raw_body: bytes, _session: Session
) -> None:
    """Upsert a single assignment into ``canvas_mock_assignments``.

    The payload shape matches :class:`CanvasMockAssignmentDTO`
    (``id`` / ``course_id`` / ``name`` / ...). The extractor maps
    ``id`` → ``canvas_mock_id`` and ``course_id`` →
    ``course_canvas_mock_id`` when writing to the row.
    """
    from app.schemas.canvas_mock import CanvasMockAssignmentDTO

    payload = json.loads(raw_body) if raw_body else {}
    dto = CanvasMockAssignmentDTO.model_validate(payload)

    extractor = CanvasMockExtractor(
        client=_get_extractor_client(),
        session_factory=_session_factory,
    )
    await extractor.upsert_assignments(tenant_id=tenant_id, assignments=[dto])


async def _handle_grade_posted(
    tenant_id: uuid.UUID, raw_body: bytes, _session: Session
) -> None:
    """Upsert a single grade into ``canvas_mock_grades``.

    The payload shape matches :class:`CanvasMockGradeDTO``
    (``assignment_id`` / ``user_id`` / ``score`` / ``grade`` ...).
    The natural key is ``(tenant_id, assignment_canvas_mock_id)`` —
    one webhook event corresponds to one (assignment, user) pair
    delivered by the mock; idempotency at the event level prevents
    duplicate writes.
    """
    from app.schemas.canvas_mock import CanvasMockGradeDTO

    payload = json.loads(raw_body) if raw_body else {}
    dto = CanvasMockGradeDTO.model_validate(payload)

    extractor = CanvasMockExtractor(
        client=_get_extractor_client(),
        session_factory=_session_factory,
    )
    await extractor.upsert_grades(tenant_id=tenant_id, grades=[dto])


_HANDLERS: dict[str, Any] = {
    "assignment.created": _handle_assignment_event,
    "assignment.updated": _handle_assignment_event,
    "grade.posted": _handle_grade_posted,
}


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/webhooks/canvas-mock")
async def receive_canvas_mock_webhook(request: Request) -> dict[str, Any]:
    """Receive a single canvas-mock webhook delivery.

    Returns ``{"status": "queued"}`` on success, ``{"status": "duplicate"}``
    on a repeat delivery, 401 on signature failure, 503 when the secret
    is unconfigured.
    """
    # 1. Settings — secret may be missing at boot time.
    settings = get_settings()
    secret = settings.canvas_mock_webhook_secret
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="webhook secret not configured",
        )

    # 2. Raw body — read once, never re-stream.
    raw_body = await request.body()

    # 3. Header validation.
    missing = _missing_headers(request.headers)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"missing headers: {missing}",
        )

    signature_header = request.headers["x-canvas-mock-signature"]
    event = request.headers["x-canvas-mock-event"]
    delivery_id = request.headers["x-canvas-mock-delivery"]
    attempt_ts = int(request.headers["x-canvas-mock-timestamp"])
    attempt = int(request.headers["x-canvas-mock-attempt"])
    tenant_id_header = uuid.UUID(request.headers["x-canvas-mock-tenant-id"])

    try:
        resource_id = int(request.headers.get("x-canvas-mock-resource-id", "0"))
    except ValueError:
        resource_id = 0

    # 4. Signature verification.
    if not verify_signature(secret, signature_header, raw_body):
        # Log the failure for forensics WITHOUT leaking the raw body
        # or the signature value.
        session = _factory()
        try:
            session.add(
                CanvasMockWebhookEvent(
                    tenant_id=tenant_id_header,
                    event=event,
                    resource_id=resource_id,
                    attempt_ts=attempt_ts,
                    delivery_id=uuid.UUID(delivery_id),
                    payload={"signature_valid": False},
                    result="signature_failed",
                    signature_valid=False,
                    processed=False,
                )
            )
            session.commit()
        except IntegrityError:
            session.rollback()
        finally:
            session.close()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="signature verification failed",
        )

    # 5. Idempotency check (composite key).
    session = _factory()
    try:
        # Try to insert; if the composite key already exists, the
        # database raises ``IntegrityError`` and we surface the
        # duplicate response without re-executing the handler.
        try:
            session.add(
                CanvasMockWebhookEvent(
                    tenant_id=tenant_id_header,
                    event=event,
                    resource_id=resource_id,
                    attempt_ts=attempt_ts,
                    delivery_id=uuid.UUID(delivery_id),
                    payload=json.loads(raw_body) if raw_body else {},
                    result="processed",
                    signature_valid=True,
                    processed=False,
                )
            )
            session.commit()
        except IntegrityError:
            session.rollback()
            return {"status": "duplicate", "delivery_id": delivery_id}

        # 6. Handler dispatch.
        handler = _HANDLERS.get(event)
        if handler is None:
            # Unknown event is still a 200 to the mock so retries
            # don't pile up; the row is updated to reflect the
            # outcome.
            row = (
                session.query(CanvasMockWebhookEvent)
                .filter_by(
                    tenant_id=tenant_id_header,
                    event=event,
                    resource_id=resource_id,
                    attempt_ts=attempt_ts,
                )
                .one()
            )
            row.result = "signature_failed"  # treat as unhandled
            session.commit()
            return {"status": "queued", "attempt": attempt}

        try:
            await handler(tenant_id_header, raw_body, session)
        except Exception as exc:
            logger.exception(
                "canvas_mock_webhook_handler_failed",
                event_name=event,
                error_class=exc.__class__.__name__,
            )
            row = (
                session.query(CanvasMockWebhookEvent)
                .filter_by(
                    tenant_id=tenant_id_header,
                    event=event,
                    resource_id=resource_id,
                    attempt_ts=attempt_ts,
                )
                .one()
            )
            row.result = "handler_error"
            row.processed = False
            session.commit()
            return {"status": "queued", "attempt": attempt}

        # Mark processed.
        row = (
            session.query(CanvasMockWebhookEvent)
            .filter_by(
                tenant_id=tenant_id_header,
                event=event,
                resource_id=resource_id,
                attempt_ts=attempt_ts,
            )
            .one()
        )
        row.processed = True
        session.commit()
        return {"status": "queued", "attempt": attempt}
    finally:
        session.close()


__all__ = ["router"]
