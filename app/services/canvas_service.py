"""Application service for manual and scheduled Canvas synchronization.

The service is the boundary that receives a server-derived tenant context.  It
never accepts a tenant id from a request payload and never accepts a Canvas
token from a request body.  It resolves the encrypted credential through
``TenantService``, builds a GET-only client, and delegates the transaction,
lock, DTO mapping, soft-delete, and watermark semantics to
:func:`app.sync.pipeline.sync_tenant`.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from app.canvas.client import CanvasClient
from app.core.config import Settings
from app.core.logging import get_logger
from app.security.token_crypto import InvalidToken as CanvasTokenInvalid
from app.security.token_crypto import TokenCipher
from app.services.tenant_service import TenantNotFound, TenantService
from app.sync.lock import release_sync_lock, try_acquire_sync_lock
from app.sync.pipeline import SyncResult, sync_tenant
from app.sync.watermark import read_watermark

logger = get_logger("app.services.canvas_service")


class TenantCredentialsMissing(Exception):
    """Raised when a tenant has no decryptable Canvas credential."""


class SyncThrottled(Exception):
    """Raised when a manual sync is still inside its tenant rate window."""

    def __init__(self, *, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        super().__init__(f"sync throttled; retry after {self.retry_after_seconds}s")


class CanvasService:
    """Compose credential resolution, client construction, and the pipeline."""

    def __init__(
        self,
        settings: Settings,
        tenant_service: TenantService,
        *,
        time_source: Callable[[], datetime] | None = None,
        session_factory: sessionmaker[Session] | None = None,
        client_factory: Callable[..., CanvasClient] = CanvasClient,
    ) -> None:
        self._settings = settings
        self._tenant_service = tenant_service
        self._time_source = time_source or (lambda: datetime.now(UTC))
        self._session_factory = session_factory
        self._client_factory = client_factory

    async def run_sync_for_tenant(
        self,
        tenant_id: uuid.UUID,
        *,
        session: Session | None = None,
    ) -> SyncResult:
        """Run synchronization for a server-derived ``tenant_id``.

        A caller-provided session is used for both credential lookup and the
        atomic pipeline.  Without one, the service opens an isolated session
        from its factory (or the standard database dependency).  Persisted SQL
        credentials are preferred; the legacy in-memory store is consulted
        only when no SQL row exists, preserving PR 2/3 harness compatibility.
        """
        if session is not None:
            return await self._run_with_session(session, tenant_id)
        return await self._run_with_owned_session(tenant_id)

    def _resolve_canvas_token(
        self,
        session: Session,
        tenant_id: uuid.UUID,
    ):
        """Resolve a tenant credential from SQL, then the legacy store."""
        if isinstance(self._tenant_service, TenantService):
            cipher = TokenCipher(self._settings.tenant_token_key)
            persisted_service = TenantService(
                ciphers={cipher.key_version: cipher},
                session=session,
            )
            if persisted_service.has_credentials(tenant_id):
                return persisted_service.get_decrypted_canvas_token(tenant_id)
        return self._tenant_service.get_decrypted_canvas_token(tenant_id)

    async def _run_with_session(
        self,
        session: Session,
        tenant_id: uuid.UUID,
    ) -> SyncResult:
        try:
            token = self._resolve_canvas_token(session, tenant_id)
            if inspect.isawaitable(token):
                token = await token
        except (TenantNotFound, CanvasTokenInvalid, UnicodeError) as exc:
            raise TenantCredentialsMissing(str(tenant_id)) from exc

        client = self._client_factory(
            base_url=self._settings.canvas_api_base_url,
            token_provider=lambda: token,
        )
        try:
            return await self._run_locked(session, tenant_id, client)
        finally:
            close = getattr(client, "aclose", None)
            if callable(close):
                value = close()
                if inspect.isawaitable(value):
                    await value

    async def _run_locked(
        self,
        session: Session,
        tenant_id: uuid.UUID,
        client: CanvasClient,
    ) -> SyncResult:
        """Acquire the shared lock, then delegate the atomic pipeline."""
        scope = session.begin_nested() if session.in_transaction() else session.begin()
        acquired = False
        try:
            with scope:
                acquired = try_acquire_sync_lock(session, tenant_id)
                if not acquired:
                    watermark = read_watermark(session, str(tenant_id), "users")
                    now = datetime.now(UTC)
                    return SyncResult(
                        tenant_id=tenant_id,
                        status="locked",
                        last_status="locked",
                        last_error_class=None,
                        last_successful_at=watermark,
                        last_run_at=now,
                        counts={},
                        lock_acquired=False,
                    )
                return await sync_tenant(
                    session,
                    tenant_id,
                    client,
                    lock_id=str(tenant_id),
                )
        finally:
            if acquired:
                release_sync_lock(session, tenant_id)

    async def _run_with_owned_session(
        self,
        tenant_id: uuid.UUID,
    ) -> SyncResult:
        if self._session_factory is not None:
            with self._session_factory() as owned_session:
                try:
                    result = await self._run_with_session(owned_session, tenant_id)
                    owned_session.commit()
                    return result
                except Exception:
                    owned_session.rollback()
                    raise

        # Import lazily so pure unit imports do not instantiate a production
        # engine or require a remote database.
        from app.core.db import get_db_session

        session_generator = get_db_session(self._settings)
        owned_session = next(session_generator)
        try:
            result = await self._run_with_session(owned_session, tenant_id)
            owned_session.commit()
            return result
        except Exception:
            owned_session.rollback()
            raise
        finally:
            try:
                next(session_generator, None)
            except StopIteration:
                pass

    def enforce_manual_rate_limit(
        self,
        session: Session,
        tenant_id: uuid.UUID,
    ) -> datetime:
        """Stamp a manual run or raise with the remaining retry window."""
        last_run = select_sync_state_last_run(session, tenant_id, "manual_sync")
        now = self._time_source()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        minimum = max(
            1,
            int(getattr(self._settings, "manual_sync_min_interval_seconds", 60)),
        )
        if last_run is not None:
            elapsed = (now - last_run).total_seconds()
            if elapsed < minimum:
                raise SyncThrottled(
                    retry_after_seconds=max(1, int(minimum - elapsed + 0.999))
                )
        self._stamp_manual_run(session, tenant_id, now)
        return now

    def _stamp_manual_run(
        self,
        session: Session,
        tenant_id: uuid.UUID,
        when: datetime,
    ) -> None:
        from app.models import SyncState

        state = select_sync_state_for_tenant(session, tenant_id, "manual_sync")
        if state is None:
            session.add(
                SyncState(
                    tenant_id=tenant_id,
                    table_name="manual_sync",
                    last_watermark=when,
                    last_run_at=when,
                    last_status="ok",
                    last_error_class=None,
                )
            )
            return
        state.last_run_at = when
        state.last_watermark = when
        state.last_status = "ok"
        state.last_error_class = None


def select_sync_state_for_tenant(
    session: Session,
    tenant_id: uuid.UUID,
    table_name: str,
):
    """Return one sync-state row for a tenant/table pair."""
    from sqlalchemy import select

    from app.models import SyncState

    return session.execute(
        select(SyncState).where(
            SyncState.tenant_id == tenant_id,
            SyncState.table_name == table_name,
        )
    ).scalar_one_or_none()


def select_sync_state_last_run(
    session: Session,
    tenant_id: uuid.UUID,
    table_name: str,
) -> datetime | None:
    """Return a timezone-aware last-run timestamp."""
    row = select_sync_state_for_tenant(session, tenant_id, table_name)
    if row is None or row.last_run_at is None:
        return None
    value = row.last_run_at
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


__all__ = [
    "CanvasService",
    "SyncThrottled",
    "TenantCredentialsMissing",
    "select_sync_state_for_tenant",
    "select_sync_state_last_run",
]


def _selftest() -> None:
    """Run credential fallback invariants without network or real secrets."""
    from cryptography.fernet import Fernet

    from app.core.db import engine_for_url, session_factory_for
    from app.models import Base

    error = SyncThrottled(retry_after_seconds=0)
    assert error.retry_after_seconds == 1
    assert CanvasService.run_sync_for_tenant.__name__ == "run_sync_for_tenant"

    key = Fernet.generate_key().decode("ascii")
    settings = Settings(
        supabase_database_url="postgresql+psycopg://127.0.0.1:1/selftest",
        tenant_token_key=key,
        backend_secret="selftest-backend-secret-with-sufficient-length",
        gemini_api_key="selftest-gemini-placeholder",
        ollama_host="http://127.0.0.1:1",
        canvas_api_base_url="https://canvas.invalid/api/v1",
        scheduler_enabled=False,
    )
    engine = engine_for_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = session_factory_for(engine)
    cipher = TokenCipher(key)
    try:
        with factory() as seed_session:
            persisted = TenantService(
                ciphers={cipher.key_version: cipher},
                session=seed_session,
            )
            tenant = persisted.get_or_create_tenant("canvas-service-selftest")
            persisted.store_canvas_token(tenant.id, "selftest-canvas-token", cipher)
            seed_session.commit()

        with factory() as read_session:
            service = CanvasService(
                settings=settings,
                tenant_service=TenantService(),
            )
            assert (
                service._resolve_canvas_token(read_session, tenant.id)
                == "selftest-canvas-token"
            )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    _selftest()
