"""FastAPI application factory and lifespan.

Boot entry point: ``uvicorn app.main:app``. The top-level ``main.py``
wrapper delegates here when ``LEGACY_MODE=0``.

PR 6 wires every controller into the application factory and adds
the ``CorrelationIdMiddleware`` so every request gets a UUID v4
``correlation_id`` propagated to logs and the response header. The
optional OTLP tracer is initialized once during lifespan startup;
when ``OTEL_ENABLED`` is unset, the tracer is a no-op (see
:mod:`app.observability.tracing`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.middleware.correlation_id import CorrelationIdMiddleware
from app.observability.tracing import init_tracing, shutdown_tracing


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Boot-time setup: load settings, configure logging, emit ready event.

    PR 4 starts the optional APScheduler sync worker; the sync router
    is opt-in and is mounted on the app here in PR 6. PR 6 also
    initializes the optional OpenTelemetry exporter.
    """
    settings: Settings = get_settings()
    configure_logging(settings.log_level)

    log = get_logger("app.boot")
    log.info(
        "app_starting",
        log_level=settings.log_level,
        scheduler_enabled=settings.scheduler_enabled,
        rag_routes_disabled=settings.disable_rag_routes,
    )

    # Expose settings on app.state for downstream routers/controllers.
    app.state.settings = settings

    tracer_provider = init_tracing()
    app.state.tracer_provider = tracer_provider

    scheduler = None
    engine = None
    if settings.scheduler_enabled:
        # PR 4 starts only the sync scheduler here.
        from app.core.db import make_engine_from_settings, session_factory_for
        from app.services.tenant_service import get_tenant_service
        from app.sync.scheduler import SyncScheduler

        engine = make_engine_from_settings(settings)
        scheduler = SyncScheduler(
            settings,
            session_factory=session_factory_for(engine),
            tenant_service=get_tenant_service(),
        )
        scheduler.start()
        app.state.sync_scheduler = scheduler
        app.state.sync_engine = engine
        app.state.scheduler = scheduler
        app.state.engine = engine
    else:
        app.state.sync_scheduler = None
        app.state.sync_engine = None
        app.state.scheduler = None
        app.state.engine = None

    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown()
        if engine is not None:
            engine.dispose()
        shutdown_tracing(tracer_provider)
        log.info("app_stopped")


def create_app() -> FastAPI:
    """Build and configure the FastAPI application.

    PR 6 mounts every controller (auth, query, sync, health) and
    adds the :class:`CorrelationIdMiddleware` so the modern API
    surface is the canonical entry point. ``LEGACY_MODE=0`` keeps
    this path the default; ``LEGACY_MODE=1`` remains a future
    follow-up but does NOT toggle which routes are registered
    here — the modern API is always live so existing clients keep
    working.
    """
    app = FastAPI(
        title="Canvas RAG Backend",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    register_exception_handlers(app)
    app.add_middleware(CorrelationIdMiddleware)

    # Mount every controller (PR 6 task 6.7).
    from app.controllers.auth import router as auth_router
    from app.controllers.health import router as health_router
    from app.controllers.query import router as query_router
    from app.controllers.sync import router as sync_router

    app.include_router(auth_router)
    app.include_router(query_router)
    app.include_router(sync_router)
    app.include_router(health_router)

    return app


app = create_app()


__all__ = ["create_app", "lifespan"]