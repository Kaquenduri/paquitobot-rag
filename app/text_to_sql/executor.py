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


def execute_readonly(session, sql: str, *, tenant_id, row_limit: int = 200) -> list[dict]:
    """Execute validated SQL with transaction-local Postgres safeguards."""
    safe_sql = validate_sql(sql)
    bounded = _with_limit(safe_sql, row_limit)
    try:
        with session.begin():
            session.execute(text("SET LOCAL default_transaction_read_only = on"))
            session.execute(text("SET LOCAL statement_timeout = 2000"))
            session.execute(text("SET LOCAL idle_in_transaction_session_timeout = 10000"))
            session.execute(text("SET LOCAL app.tenant_id = :tenant_id"), {"tenant_id": str(tenant_id)})
            result = session.execute(text(bounded), {"tenant_id": str(tenant_id)})
            return [dict(row) for row in result.mappings().all()]
    except Exception as exc:
        raise SQLExecutionError("read-only query execution failed") from exc


def _selftest() -> None:
    assert "LIMIT 5" in _with_limit("SELECT 1", 5)
    assert _with_limit("SELECT 1 LIMIT 3", 5).endswith("LIMIT 3")


if __name__ == "__main__":
    _selftest()
