"""In-process six-hour Canvas synchronization scheduler.

``SyncScheduler`` is intentionally small: APScheduler owns the clock, this
module adds bounded per-tenant jitter, and the worker pool limits fan-out to
four concurrent tenants per process.  The default execution path acquires the
same transaction-scoped lock used by manual sync before delegating to
:func:`app.sync.pipeline.sync_tenant`.
"""

from __future__ import annotations

import asyncio
import inspect
import random
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.canvas.client import CanvasClient
from app.core.config import Settings
from app.core.logging import get_logger
from app.models import CanvasCredential
from app.services.tenant_service import TenantService
from app.sync.lock import release_memory_lock, try_acquire_sync_lock
from app.sync.pipeline import sync_tenant

logger = get_logger("app.sync.scheduler")

MAX_WORKERS_PER_PROCESS = 4


class _WorkerPool:
    """Async semaphore-backed worker pool with a hard process cap."""

    def __init__(self, max_workers: int = MAX_WORKERS_PER_PROCESS) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        self._max_workers = min(MAX_WORKERS_PER_PROCESS, int(max_workers))
        self._semaphore = asyncio.Semaphore(self._max_workers)

    @property
    def max_workers(self) -> int:
        return self._max_workers

    async def run(self, coro_factory: Callable[[], Awaitable[Any]]) -> Any:
        async with self._semaphore:
            return await coro_factory()


def jitter_seconds(settings: Settings) -> float:
    """Return the non-negative configured jitter window."""
    return max(0.0, float(getattr(settings, "sync_jitter_seconds", 0)))


def apply_jitter(
    base: float,
    jitter: float,
    *,
    rand: random.Random | Any | None = None,
) -> float:
    """Return ``base + random.uniform(0, jitter)`` without negative delay."""
    rng = rand or random
    return max(0.0, float(base) + rng.uniform(0.0, max(0.0, float(jitter))))


SyncRunner = Callable[[Session, uuid.UUID, CanvasClient], Awaitable[Any]]
ClientFactory = Callable[..., CanvasClient]


def _build_sync_coro(
    *,
    tenant_id: uuid.UUID,
    canvas_token: str,
    canvas_api_base_url: str,
    pipeline: Callable[..., Awaitable[Any]],
    session_factory: sessionmaker[Session] | None = None,
    client_factory: ClientFactory = CanvasClient,
) -> Callable[[], Awaitable[Any]]:
    """Build a self-contained worker coroutine for tests and integrations."""

    async def _run() -> Any:
        if session_factory is None:
            raise RuntimeError("session_factory is required for a scheduler worker")
        client = client_factory(
            base_url=canvas_api_base_url,
            token_provider=lambda: canvas_token,
        )
        try:
            with session_factory() as session:
                return await pipeline(session, tenant_id, client)
        finally:
            close = getattr(client, "aclose", None)
            if callable(close):
                value = close()
                if inspect.isawaitable(value):
                    await value

    return _run


class SyncScheduler:
    """APScheduler wrapper for the tenant sync job."""

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: sessionmaker[Session],
        tenant_service: TenantService,
        worker_pool_size: int = MAX_WORKERS_PER_PROCESS,
        sync_service: Any | None = None,
        client_factory: ClientFactory = CanvasClient,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._tenant_service = tenant_service
        self._worker_pool = _WorkerPool(worker_pool_size)
        self._sync_service = sync_service
        self._client_factory = client_factory
        self._scheduler: AsyncIOScheduler | None = None
        self._job_id = "primer_rag_sync_due"

    @property
    def scheduler(self) -> AsyncIOScheduler | None:
        return self._scheduler

    @property
    def worker_pool_size(self) -> int:
        return self._worker_pool.max_workers

    def start(self) -> None:
        """Start the six-hour cron job unless explicitly disabled."""
        if not bool(getattr(self._settings, "scheduler_enabled", True)):
            logger.info("sync_scheduler_disabled")
            return
        if self._scheduler is not None:
            return

        # APScheduler 3.x is pinned by this project.  Keep the trigger
        # literal visible: it is the contract, independent of any future
        # settings value for ad-hoc sync intervals.
        trigger = CronTrigger(hour="*/6")
        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.add_job(
            self._tick,
            trigger=trigger,
            id=self._job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        self._scheduler = scheduler
        logger.info(
            "sync_scheduler_started",
            cron="*/6h",
            jitter_seconds=jitter_seconds(self._settings),
            max_workers=self._worker_pool.max_workers,
        )

    def shutdown(self) -> None:
        """Stop the scheduler safely and idempotently."""
        scheduler = self._scheduler
        if scheduler is None:
            return
        try:
            scheduler.shutdown(wait=False)
        except Exception:  # pragma: no cover - APScheduler lifecycle race
            logger.exception("sync_scheduler_shutdown_failed")
        finally:
            self._scheduler = None
            logger.info("sync_scheduler_stopped")

    async def _tick(self) -> None:
        """Fan out one worker per credentialed tenant with independent jitter."""
        tenant_ids = await asyncio.to_thread(self._list_tenant_ids)
        if not tenant_ids:
            logger.info("sync_tick_no_tenants")
            return
        window = jitter_seconds(self._settings)
        await asyncio.gather(
            *(self._dispatch_with_jitter(tenant_id, window) for tenant_id in tenant_ids),
            return_exceptions=True,
        )

    def _list_tenant_ids(self) -> list[uuid.UUID]:
        """List tenants that have a stored Canvas credential."""
        with self._session_factory() as session:
            rows = session.execute(
                select(CanvasCredential.tenant_id).distinct()
            ).fetchall()
        tenant_ids: list[uuid.UUID] = []
        for row in rows:
            value = row[0]
            try:
                tenant_ids.append(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
            except (TypeError, ValueError, AttributeError):
                logger.warning("sync_invalid_tenant_id_in_credentials")
        return tenant_ids

    async def _dispatch_with_jitter(self, tenant_id: uuid.UUID, window: float) -> None:
        # The random draw is per tenant, not per cron tick, which prevents a
        # large tenant set from waking in lock-step.
        delay = random.uniform(0.0, max(0.0, float(window)))
        if delay:
            await asyncio.sleep(delay)
        await self._worker_pool.run(lambda: self._run_for_tenant(tenant_id))

    async def _run_for_tenant(self, tenant_id: uuid.UUID) -> None:
        """Resolve the token and run one locked, isolated tenant transaction."""
        if self._sync_service is not None:
            # A supplied service owns client construction and the same lock
            # composition as the manual controller.
            await self._sync_service.run_sync_for_tenant(tenant_id)
            return

        try:
            token = await asyncio.to_thread(
                self._tenant_service.get_decrypted_canvas_token,
                tenant_id,
            )
        except Exception as exc:  # noqa: BLE001 - scheduler isolates tenants
            logger.warning(
                "sync_skip_tenant_no_token",
                tenant_id=str(tenant_id),
                error_class=exc.__class__.__name__,
            )
            return

        client = self._client_factory(
            base_url=self._settings.canvas_api_base_url,
            token_provider=lambda: token,
        )
        acquired = False
        try:
            with self._session_factory() as session, session.begin():
                # Scheduler and manual requests share this exact lock.  The
                # pipeline receives ``lock_id`` so it does not acquire twice.
                acquired = try_acquire_sync_lock(session, tenant_id)
                if not acquired:
                    logger.info(
                        "sync_scheduler_lock_busy",
                        tenant_id=str(tenant_id),
                    )
                    return
                await sync_tenant(
                    session,
                    tenant_id,
                    client,
                    lock_id=str(tenant_id),
                )
        except Exception:  # noqa: BLE001 - isolate one tenant from the tick
            logger.error("sync_tenant_unhandled", tenant_id=str(tenant_id))
        finally:
            if acquired:
                # No-op for PostgreSQL; releases the SQLite test fallback.
                # A short synthetic session is not needed because the lock
                # registry is process-local on non-Postgres dialects.
                try:
                    # The helper is a no-op for PostgreSQL because that
                    # dialect owns release at transaction end; on SQLite it
                    # drops the process-local fallback.
                    release_memory_lock(tenant_id)
                except Exception:  # pragma: no cover - defensive cleanup
                    logger.exception("sync_scheduler_lock_release_failed")
            close = getattr(client, "aclose", None)
            if callable(close):
                value = close()
                if inspect.isawaitable(value):
                    await value


__all__ = [
    "MAX_WORKERS_PER_PROCESS",
    "SyncRunner",
    "SyncScheduler",
    "_WorkerPool",
    "_build_sync_coro",
    "apply_jitter",
    "jitter_seconds",
]


def _selftest() -> None:
    """Run scheduler invariants without starting a background scheduler."""
    pool = _WorkerPool(99)
    assert pool.max_workers == MAX_WORKERS_PER_PROCESS
    assert apply_jitter(10, 5, rand=random.Random(4)) >= 10
    assert apply_jitter(0, 0, rand=random.Random(4)) == 0
    trigger = CronTrigger(hour="*/6")
    assert any(getattr(field, "name", "") == "hour" for field in trigger.fields)


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    _selftest()
