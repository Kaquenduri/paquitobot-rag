"""Pytest fixtures shared across the test suite.

PR 1 introduces the ``python_executable`` and ``app_settings``
fixtures; PR 3 adds a per-test SQLite engine + session so unit
tests can exercise the SQLAlchemy models and the repository helpers
without touching the real Supabase database.
"""

from __future__ import annotations

import sys
from collections.abc import Generator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.db import create_all, drop_all, engine_for_url, session_factory_for

WINDOWS_PYTHON_EXECUTABLE = r"C:\Users\Administrador\langchain\Scripts\python.exe"

# Reconfigure stdout/stderr to UTF-8 so structlog's traceback
# rendering (which emits Unicode box-drawing characters) does not
# crash on Windows cp1252 shells.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


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
        "GEMINI_API_KEY": "test-only-gemini-key",
        "OLLAMA_HOST": "http://127.0.0.1:1",
        "SCHEDULER_ENABLED": "false",
        "DISABLE_RAG_ROUTES": "true",
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