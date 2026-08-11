"""``GET /healthz`` controller (PR 6 task 6.2).

The endpoint reports the live status of every dependency the
service needs to serve traffic:

- **Ollama** — embedding provider health (drives
  ``provider_health()`` on the RAG service so vector / hybrid routes
  know when to fall back to relational-only).
- **DB** — a lightweight ``SELECT 1`` probe against the configured
  engine; degrades to ``false`` when the engine is unreachable.
- **Scheduler** — whether the in-process APScheduler is running.

The response always includes the configured ``status`` so a
client/probe can decide in one read whether the service is
"healthy enough" for them. Degradation is visible per-dependency
(``available: false``) so operators see exactly which piece of the
stack is down without consulting external systems.

When :attr:`Settings.disable_rag_routes` is on the endpoint stays
``200`` so a Kubernetes probe does not cycle the pod just because
the operator flipped a feature flag — but the body adds an
``rag_routes_disabled`` flag.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.rag_service import RAGService

logger = get_logger("app.controllers.health")

router = APIRouter(tags=["health"])


def _probe_ollama() -> dict[str, Any]:
    """Return a degraded Ollama status report.

    The RAG service owns the embedding-provider probe; we read its
    cached health so the /healthz endpoint never makes a real network
    call when Ollama is already known to be down.
    """
    try:
        health = RAGService().provider_health()
    except Exception as exc:  # noqa: BLE001 - defensive
        logger.warning("health_ollama_probe_failed", error_class=exc.__class__.__name__)
        return {"available": False, "error_class": exc.__class__.__name__}
    return {"available": bool(health.get("embedding_available", False))}


def _probe_db(settings: Settings) -> dict[str, Any]:
    """Return a degraded DB status report.

    Uses a single ``SELECT 1`` over a freshly-built engine with a
    short connect timeout. The engine is disposed after the probe
    so the request doesn't leave a long-lived pool around. Failures
    are logged but never raised — the health endpoint must always
    return a JSON body so the orchestrator can interpret it.
    """
    from app.core.db import make_engine_from_settings

    engine = None
    try:
        engine = make_engine_from_settings(
            settings,
            # Tight pool + connect timeouts so an unreachable DB host
            # does not stall the /healthz endpoint.
            connect_args={"connect_timeout": 2},
        )
    except Exception as exc:  # noqa: BLE001 - defensive
        logger.warning("health_db_engine_failed", error_class=exc.__class__.__name__)
        return {"available": False, "error_class": exc.__class__.__name__}

    if engine is None:
        return {"available": False, "error_class": "no_engine"}

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - defensive
        logger.warning("health_db_probe_failed", error_class=exc.__class__.__name__)
        return {"available": False, "error_class": exc.__class__.__name__}
    finally:
        try:
            engine.dispose()
        except (RuntimeError, OSError):  # defensive
            pass
    return {"available": True}


def _probe_scheduler(request: Request) -> dict[str, Any]:
    """Return whether the lifespan-managed scheduler is running."""
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return {"running": False, "enabled": False}
    running = bool(getattr(scheduler, "running", False))
    return {"running": running, "enabled": True}


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    """Return the full service health report.

    The response shape is::

        {
            "status": "ok" | "degraded",
            "ollama": {"available": bool, ...},
            "db": {"available": bool, ...},
            "scheduler": {"running": bool, "enabled": bool},
            "rag_routes_disabled": bool
        }

    The top-level ``status`` flips to ``degraded`` whenever ANY
    dependency is unavailable so probes can fail fast without
    parsing the per-dependency blocks.
    """
    settings: Settings = get_settings()

    ollama = _probe_ollama()
    db = _probe_db(settings)
    scheduler = _probe_scheduler(request)
    rag_disabled = bool(getattr(settings, "disable_rag_routes", False))

    healthy = ollama["available"] and db["available"] and scheduler["running"]

    return {
        "status": "ok" if healthy else "degraded",
        "ollama": ollama,
        "db": db,
        "scheduler": scheduler,
        "rag_routes_disabled": rag_disabled,
    }


__all__ = ["healthz", "router"]


# ---------------------------------------------------------------------------
# Self-test (executable via ``python -m app.controllers.health``)
# ---------------------------------------------------------------------------


def _selftest() -> None:
    """Run healthz assertions without real network or DB I/O."""
    import os

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    os.environ.setdefault("SUPABASE_DATABASE_URL", "postgresql+psycopg://127.0.0.1:1/test")
    os.environ.setdefault("TENANT_TOKEN_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    os.environ.setdefault("BACKEND_SECRET", "test-only-secret")
    os.environ.setdefault("MINIMAX_API_KEY", "test")
    os.environ.setdefault("OLLAMA_HOST", "http://127.0.0.1:1")
    os.environ.setdefault("CANVAS_API_BASE_URL", "https://canvas.invalid/api/v1")

    from app.core.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()

    app = FastAPI()
    app.include_router(router)

    # Stub a scheduler on the app state for the "running" branch.
    class _FakeScheduler:
        running = True

    app.state.scheduler = _FakeScheduler()
    app.state.settings = settings

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200, response.text
    body = response.json()
    # Always-present keys.
    assert body["status"] in {"ok", "degraded"}
    assert "ollama" in body
    assert "db" in body
    assert "scheduler" in body
    assert isinstance(body["rag_routes_disabled"], bool)

    # The Ollama probe uses RAGService() (embedding_available=False by
    # default in the service) so ollama.available is False here.
    assert body["ollama"]["available"] is False

    # The scheduler stub reports running=True; db probe uses a real
    # engine pointed at 127.0.0.1:1 which is expected to fail, so
    # the top-level status should be "degraded" for this run.
    assert body["scheduler"]["running"] is True
    assert body["scheduler"]["enabled"] is True
    assert body["db"]["available"] is False

    # The top-level status reflects at least one degraded dependency.
    assert body["status"] == "degraded"


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    _selftest()