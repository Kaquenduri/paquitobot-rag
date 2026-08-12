"""Unit tests for the canvas service (PR 4 task 4.7).

Coverage:

- The service constructs a :class:`CanvasClient` from the tenant's
  stored token and forwards to the pipeline.
- The service raises :class:`TenantCredentialsMissing` when the
  tenant has no token.
- The manual rate-limit raises :class:`SyncThrottled` with the
  correct retry-after window when called inside the same interval.
- The first manual call stamps the ``sync_state`` row for the
  ``"manual_sync"`` table.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.canvas.client import CanvasClient
from app.services.canvas_service import (
    CanvasService,
    SyncThrottled,
    TenantCredentialsMissing,
)
from app.services.tenant_service import TenantNotFound
from app.sync.pipeline import SyncResult

TENANT_ID = uuid.UUID("aaaa1111-aaaa-1111-aaaa-111111111111")


def _settings() -> Any:
    from app.core.config import Settings

    return Settings(
        supabase_database_url="postgresql+psycopg://127.0.0.1:1/test",
        tenant_token_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        backend_secret="x" * 32,
        minimax_api_key="test",
        ollama_host="http://127.0.0.1:1",
        canvas_api_base_url="https://canvas.test/api/v1",
        google_client_id="test-only.apps.googleusercontent.com",
        manual_sync_min_interval_seconds=60,
    )


def _stub_tenant_service() -> Any:
    service = MagicMock()
    service.get_decrypted_canvas_token.return_value = "plain-canvas-token"
    return service


def test_run_sync_for_tenant_returns_pipeline_result(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service builds a client and forwards to the pipeline."""
    from app.services import canvas_service as service_module

    captured: dict[str, Any] = {}

    async def fake_sync_tenant(session, tenant_id, client, **kwargs):
        captured["tenant_id"] = tenant_id
        captured["client"] = client
        return SyncResult(
            tenant_id=tenant_id,
            status="ok",
            last_status="ok",
            last_error_class=None,
            last_successful_at=datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC),
            last_run_at=datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC),
            counts={},
            lock_acquired=True,
        )

    monkeypatch.setattr(service_module, "sync_tenant", fake_sync_tenant)

    service = CanvasService(
        settings=_settings(),
        tenant_service=_stub_tenant_service(),
    )
    result = asyncio.run(_arun(service, db_session, TENANT_ID))

    assert result.status == "ok"
    assert captured["tenant_id"] == TENANT_ID
    assert isinstance(captured["client"], CanvasClient)
    # The token is sourced from the tenant service, not the payload.
    service._tenant_service.get_decrypted_canvas_token.assert_called_once_with(TENANT_ID)


def test_run_sync_for_tenant_raises_when_credentials_missing(
    db_session: Any,
) -> None:
    service = CanvasService(
        settings=_settings(),
        tenant_service=_stub_tenant_service(),
    )
    service._tenant_service.get_decrypted_canvas_token.side_effect = TenantNotFound(
        f"no Canvas credentials for {TENANT_ID}"
    )

    with pytest.raises(TenantCredentialsMissing):
        asyncio.run(_arun(service, db_session, TENANT_ID))


def test_enforce_manual_rate_limit_stamps_first_call(db_session: Any) -> None:
    """The first call writes ``sync_state`` for the manual table."""
    service = CanvasService(
        settings=_settings(),
        tenant_service=_stub_tenant_service(),
    )
    fixed_now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
    service._time_source = lambda: fixed_now

    stamped = service.enforce_manual_rate_limit(db_session, TENANT_ID)
    assert stamped == fixed_now
    db_session.commit()

    # Now the second call must raise SyncThrottled.
    service._time_source = lambda: fixed_now + timedelta(seconds=30)
    with pytest.raises(SyncThrottled) as exc_info:
        service.enforce_manual_rate_limit(db_session, TENANT_ID)
    assert exc_info.value.retry_after_seconds >= 30


def test_enforce_manual_rate_limit_respects_window(db_session: Any) -> None:
    """A second call exactly at the window boundary is allowed."""
    service = CanvasService(
        settings=_settings(),
        tenant_service=_stub_tenant_service(),
    )
    fixed_now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
    service._time_source = lambda: fixed_now

    service.enforce_manual_rate_limit(db_session, TENANT_ID)
    db_session.commit()

    # Advance past the window.
    service._time_source = lambda: fixed_now + timedelta(seconds=120)
    # Should not raise.
    service.enforce_manual_rate_limit(db_session, TENANT_ID)
    db_session.commit()


def test_enforce_manual_rate_limit_uses_partial_retry_after(
    db_session: Any,
) -> None:
    """The retry-after reflects the remaining window, not the whole window."""
    settings = _settings()
    settings.manual_sync_min_interval_seconds = 60
    service = CanvasService(
        settings=settings,
        tenant_service=_stub_tenant_service(),
    )
    fixed_now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
    service._time_source = lambda: fixed_now

    service.enforce_manual_rate_limit(db_session, TENANT_ID)
    db_session.commit()

    # Advance 10 seconds -> 50 seconds remaining.
    service._time_source = lambda: fixed_now + timedelta(seconds=10)
    with pytest.raises(SyncThrottled) as exc_info:
        service.enforce_manual_rate_limit(db_session, TENANT_ID)
    assert exc_info.value.retry_after_seconds == 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _arun(service: CanvasService, session: Any, tenant_id: uuid.UUID):
    return await service.run_sync_for_tenant(tenant_id, session=session)
