"""Bounded read-only SQLAlchemy execution."""

from __future__ import annotations

import re

from sqlalchemy import text

from .validator import validate_sql


class SQLExecutionError(RuntimeError):
    """Database execution failure without exposing SQL details."""


def _with_limit(sql: str, row_limit: int) -> str:
    if re.search(r"\bLIMIT\s+\d+\b", sql, re.IGNORECASE):
        return sql
    return f"SELECT * FROM ({sql}) AS bounded_query LIMIT {int(row_limit)}"


def execute_readonly(
    session,
    sql: str,
    *,
    tenant_id,
    params: dict | None = None,
    row_limit: int = 200,
) -> list[dict]:
    """Execute validated SQL with transaction-local Postgres safeguards.

    ``params`` carries the template's non-tenant bind values (e.g.
    ``start_at``/``end_at``); the allow-list already validated every
    placeholder in ``sql`` is declared, so passing them through here is
    safe.
    """
    safe_sql = validate_sql(sql)
    bounded = _with_limit(safe_sql, row_limit)
    try:
        with session.begin():
            # ``SET LOCAL default_transaction_read_only`` is a Postgres-only
            # statement; skip it on SQLite to keep the executor usable from
            # the in-memory test suite.  Production always runs against
            # Postgres, so the guard is a no-op in deployment.
            bind = session.get_bind()
            dialect_name = str(getattr(getattr(bind, "dialect", None), "name", "")).lower()
            if dialect_name == "postgresql":
                session.execute(text("SET LOCAL default_transaction_read_only = on"))
                session.execute(text("SET LOCAL statement_timeout = 2000"))
                session.execute(text("SET LOCAL idle_in_transaction_session_timeout = 10000"))
                # PostgreSQL SET command does NOT support bind parameters ($1 / %(name)s).
                # The value must be a string literal.  tenant_id is always a UUID
                # (hex + hyphens only) so direct interpolation is safe against injection.
                tenant_id_str = str(tenant_id).replace("'", "''")  # extra safety
                session.execute(text(f"SET LOCAL app.tenant_id = '{tenant_id_str}'"))
            bind_params = {"tenant_id": str(tenant_id), **(params or {})}
            result = session.execute(text(bounded), bind_params)
            return [dict(row) for row in result.mappings().all()]
    except Exception as exc:
        raise SQLExecutionError("read-only query execution failed") from exc


def _selftest() -> None:
    assert "LIMIT 5" in _with_limit("SELECT 1", 5)
    assert _with_limit("SELECT 1 LIMIT 3", 5).endswith("LIMIT 3")


if __name__ == "__main__":
    _selftest()
