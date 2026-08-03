"""Structural SQL safety validation."""

from __future__ import annotations

import re


class SQLNotAllowed(ValueError):
    """Raised when SQL is not a single safe SELECT."""


_DANGEROUS = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|CALL|COPY)\b|\b(pg_[a-z_]+|set_config|current_setting)\s*\(", re.IGNORECASE)


def validate_sql(sql: str) -> str:
    text = sql.strip().rstrip(";").strip()
    if not text or ";" in text or not re.match(r"^SELECT\b", text, re.IGNORECASE):
        raise SQLNotAllowed("only one SELECT statement is allowed")
    if _DANGEROUS.search(text) or re.search(r"\bINTO\b", text, re.IGNORECASE):
        raise SQLNotAllowed("SQL statement is not allowed")
    try:
        import sqlglot

        statements = sqlglot.parse(text, read="postgres")
        if len(statements) != 1 or statements[0].key != "select":
            raise SQLNotAllowed("only SELECT is allowed")
    except SQLNotAllowed:
        raise
    except (ImportError, ValueError, TypeError):
        try:
            import sqlparse

            parsed = sqlparse.parse(text)
            if len(parsed) != 1 or parsed[0].get_type() != "SELECT":
                raise SQLNotAllowed("SQL statement is not allowed")
        except ImportError as exc:
            raise SQLNotAllowed("SQL parser unavailable") from exc
    return text


# Compatibility alias used by callers.
validate = validate_sql


def _selftest() -> None:
    assert validate_sql("SELECT 1") == "SELECT 1"
    for bad in ("UPDATE assignments SET name='x'", "SELECT pg_sleep(2)", "SELECT 1; DROP TABLE users"):
        try:
            validate_sql(bad)
        except SQLNotAllowed:
            pass
        else:
            raise AssertionError(bad)


if __name__ == "__main__":
    _selftest()
