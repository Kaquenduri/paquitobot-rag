"""Alembic environment — async SQLAlchemy against SUPABASE_DATABASE_URL.

PR 1 verified that ``alembic current`` could open the configured
database without errors. PR 3 wires the canonical SQLAlchemy
``Base.metadata`` into the env so:

- ``alembic upgrade --sql`` (offline mode) emits DDL using the same
  metadata that the application sees.
- The first migration (``0001_init.py``) can be diffed against
  ``Base.metadata`` for future changes.

Importing ``app.models`` is the seam that lets Alembic compare server
state against our declarative schema; the import is intentionally
local so PR 1's no-app-import path no longer holds for PR 3+.
"""

from __future__ import annotations

import asyncio
import os
import selectors
from logging.config import fileConfig
from typing import Any
from dotenv import load_dotenv

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.models import Base  # noqa: E402 - intentional late import

# --- Alembic config object -------------------------------------------------
config = context.config

# Inject SUPABASE_DATABASE_URL (and a sensible async psycopg default) into
# the Alembic config so ``alembic current`` works without a manual override.
load_dotenv()  # Load .env variables if present
database_url = os.environ.get("SUPABASE_DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)
elif not config.get_main_option("sqlalchemy.url"):
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres",
    )

# Configure stdlib logging from alembic.ini (best-effort).
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# PR 3 wires the canonical metadata object so ``alembic upgrade --sql``
# and future autogenerate revisions diff against ``app.models.Base``.
target_metadata: Any = Base.metadata


# --- Migration runners -----------------------------------------------------
def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_online_async() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point invoked by Alembic when ``--sql`` is NOT used."""
    # Windows defaults to ProactorEventLoop, which psycopg async cannot use
    # (raises ``InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to
    # run in async mode``). Force SelectorEventLoop so ``alembic current``
    # works on Windows-native Python as well as on WSL/Linux.
    asyncio.run(
        _run_migrations_online_async(),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )


def run_migrations_offline() -> None:
    """Emit SQL scripts without connecting (used by ``alembic upgrade --sql``)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
