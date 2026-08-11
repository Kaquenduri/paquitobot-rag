"""Smoke tests for the Phase 1 FastAPI scaffold.

These tests exercise the ``GET /healthz`` endpoint through FastAPI's
``TestClient``, which runs the application's lifespan startup/shutdown
hooks in-process. They require the env vars defined in
``tests.conftest.app_settings`` to be present BEFORE ``Settings`` is
instantiated (which happens during the lifespan).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import get_settings


def _build_client() -> TestClient:
    """Return a fresh TestClient with the lru_cache on ``get_settings`` cleared."""
    get_settings.cache_clear()
    from app.main import create_app  # local import so monkeypatch has fired

    return TestClient(create_app(), raise_server_exceptions=True)


def test_healthz_returns_ok_with_status_200(app_settings) -> None:
    with _build_client() as client:
        response = client.get("/healthz")

    # PR 6 extends /healthz to report per-dependency status. The
    # top-level ``status`` flag still drives liveness; the per-block
    # keys surface degradation (Ollama down, DB unreachable,
    # scheduler stopped, ``DISABLE_RAG_ROUTES`` set).
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    assert "ollama" in body
    assert "db" in body
    assert "scheduler" in body
    assert isinstance(body["rag_routes_disabled"], bool)


def test_healthz_includes_correlation_id_header_on_error(app_settings) -> None:
    """A 404 path exercises the exception handler and surfaces X-Correlation-ID."""
    with _build_client() as client:
        response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert "X-Correlation-ID" in response.headers
    body = response.json()
    assert body["code"] == "http_404"
    assert body["correlation_id"] == response.headers["X-Correlation-ID"]
    assert "***REDACTED***" not in body["message"] or body["message"] == "404: Not Found"


def test_healthz_lifespan_loads_settings(app_settings) -> None:
    """Lifespan startup must read Settings from the test env without error."""
    with _build_client() as client:
        # Touch a route so lifespan startup runs.
        client.get("/healthz")
        settings = client.app.state.settings

    assert settings.supabase_database_url.startswith("postgresql+psycopg://")
    assert settings.tenant_token_key == app_settings["TENANT_TOKEN_KEY"]
    assert settings.backend_secret == app_settings["BACKEND_SECRET"]
    assert settings.minimax_api_key == app_settings["MINIMAX_API_KEY"]
    assert settings.canvas_api_base_url == app_settings["CANVAS_API_BASE_URL"]
    assert settings.ollama_host == app_settings["OLLAMA_HOST"]


def test_app_factory_is_idempotent(app_settings) -> None:
    """create_app() must be callable more than once without leaking state."""
    from app.main import create_app

    a = create_app()
    b = create_app()
    assert a is not b
    assert a.title == b.title
