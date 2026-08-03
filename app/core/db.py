"""Database engine and FastAPI session dependency (PR 3 task 3 + extras).

The dep chain in PR 2 already returns ``tenant_id`` synchronously
from the in-memory stub. This module adds the SQLAlchemy engine
scaffolding so PR 4 can drive the sync pipeline against a real
session without re-plumbing the lifespan.

Two contracts live here:

- :func:`get_db_session` — FastAPI dependency that yields a
  :class:`sqlalchemy.orm.Session` bound to ``SUPABASE_DATABASE_URL``
  and rolls back on exception. Production uses an async engine via
  :func:`async_engine_for_url`; tests stay synchronous against SQLite.
- :func:`engine_for_url` — build a SQLAlchemy engine for an arbitrary
  URL; the ``sqlite://`` (in-memory) path skips connection-pool
  concerns so unit tests can spin up a fresh schema per test.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import CHAR, TypeDecorator  # noqa: F401  - re-exported

from app.core.config import Settings, get_settings
from app.models import Base


def _coerce_sqlite_url(url: str) -> str:
    """Translate an async SQLite URL into a sync one for tests.

    Production deploys use ``postgresql+psycopg://`` (validated in
    :mod:`app.core.config`); SQLite only appears in tests. We accept
    any SQLite URL passed in via env or settings and leave it
    untouched so the caller can choose ``sqlite:///:memory:`` or
    ``sqlite:///./test.db`` as needed.
    """
    if url.startswith("sqlite+"):
        return url.replace("sqlite+", "sqlite:///", 1)
    return url


def engine_for_url(url: str, **kwargs: Any) -> Engine:
    """Build a synchronous SQLAlchemy engine for ``url``.

    SQLite gets ``check_same_thread=False`` so the same connection
    can be touched from multiple threads inside pytest; Postgres
    uses a small connection pool sized for the lifespan worker.
    """
    if url.startswith("sqlite"):
        sqlite_url = _coerce_sqlite_url(url)
        sqlite_kwargs = dict(kwargs)
        sqlite_kwargs.setdefault("connect_args", {"check_same_thread": False})
        if sqlite_url in {"sqlite:///:memory:", "sqlite://"}:
            # FastAPI/TestClient may create the session in a worker thread;
            # StaticPool keeps an in-memory schema visible across those
            # threads while the caller still controls transaction scope.
            sqlite_kwargs.setdefault("poolclass", StaticPool)
        return create_engine(
            sqlite_url,
            future=True,
            **sqlite_kwargs,
        )
    return create_engine(url, future=True, pool_pre_ping=True, **kwargs)


def session_factory_for(engine: Engine) -> sessionmaker[Session]:
    """Return a :class:`sessionmaker` bound to ``engine``."""
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )


def _settings_db_url(settings: Settings) -> str:
    """Pull the configured DB URL; fall back to ``SUPABASE_DATABASE_URL``.

    The fallback exists so callers without a cached settings object
    (e.g. Alembic env) can still drive a session from env alone.
    """
    url = getattr(settings, "supabase_database_url", None) or os.environ.get(
        "SUPABASE_DATABASE_URL", ""
    )
    return url or ""


def make_engine_from_settings(
    settings: Settings | None = None,
    *,
    override_url: str | None = None,
    connect_args: dict[str, Any] | None = None,
) -> Engine:
    """Build a SQLAlchemy engine from settings or an explicit URL.

    ``override_url`` lets tests pin a ``sqlite:///:memory:`` engine
    without touching process-wide settings. ``connect_args`` is
    forwarded to the underlying driver so callers (e.g. the health
    probe) can pass a short connect timeout.
    """
    url = override_url or _settings_db_url(settings or get_settings())
    if not url:
        raise RuntimeError(
            "No database URL configured: set SUPABASE_DATABASE_URL or pass "
            "override_url=..."
        )
    if connect_args is not None:
        return engine_for_url(url, connect_args=connect_args)
    return engine_for_url(url)


def get_db_session(
    settings: Settings | None = None,
    *,
    override_engine: Engine | None = None,
) -> Generator[Session]:
    """FastAPI dependency: yield a :class:`Session`, rollback on error.

    Production wires this into controllers via ``Depends(get_db_session)``.
    Tests pass ``override_engine`` to inject a SQLite engine without
    touching ``SUPABASE_DATABASE_URL``. The session is closed in the
    ``finally`` block regardless of outcome.

    The lifespan keeps the engine on ``app.state.engine``; the
    dependency binds a fresh :class:`sessionmaker` per request so
    long-running async loops do not share a connection.
    """
    if override_engine is None:
        override_engine = make_engine_from_settings(settings)
    factory = session_factory_for(override_engine)
    session = factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(engine: Engine) -> None:
    """Create every table declared on :class:`Base.metadata`.

    Convenience for tests; production schema changes go through
    Alembic. Calling this against a Postgres URL is safe (idempotent
    if tables already exist) but never used at runtime.
    """
    Base.metadata.create_all(engine)


def drop_all(engine: Engine) -> None:
    """Drop every table declared on :class:`Base.metadata` (tests only)."""
    Base.metadata.drop_all(engine)


__all__ = [
    "create_all",
    "drop_all",
    "engine_for_url",
    "get_db_session",
    "make_engine_from_settings",
    "session_factory_for",
]


# Optional async helpers ---------------------------------------------------
# These are imported lazily so the rest of the codebase does not need
# ``sqlalchemy[asyncio]`` to import :mod:`app.core.db`. PR 4 will
# promote them to first-class deps once the sync pipeline lands.


def async_engine_for_url(url):  # pragma: no cover - PR 4
    """Build an async engine; lazy-import ``sqlalchemy.ext.asyncio``."""
    from sqlalchemy.ext.asyncio import async_engine_from_config

    return async_engine_from_config(
        {"sqlalchemy.url": url},
        prefix="sqlalchemy.",
    )


__all__ += ["async_engine_for_url"]