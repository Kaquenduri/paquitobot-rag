"""Log redaction for credential-bearing payloads (PR 2 task 2.3).

The :class:`RedactionFilter` is wired into the root logger and the
uvicorn loggers by :mod:`app.core.logging`. Every log emission —
stdlib ``logging`` records, structlog event dicts, exception
tracebacks — must pass through this filter so the redaction contract
is uniform across every output stream.

Coverage:

- Dict keys (case-insensitive): ``authorization``, ``token``,
  ``ciphertext``, ``password``, ``database_url``,
  ``supabase_database_url``, ``tenant_token_key``, ``backend_secret``,
  ``minimax_api_key``, ``gemini_api_key`` (legacy), ``canvas_api_token``, ``api_key``, ``secret``.
- Free-text patterns: ``Bearer …``, ``gAAAAA…`` Fernet envelopes,
  ``postgresql://`` and ``postgresql+psycopg://`` URLs.
- Nested dicts and lists are walked recursively.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

_MASK = "***REDACTED***"

# Patterns that MUST never reach a log line or HTTP response body.
_FERNET_PATTERN = re.compile(r"gAAAA[A-Za-z0-9_\-=]+")
_BEARER_PATTERN = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-+/=]+")
_PG_URL_PATTERN = re.compile(r"postgres(?:ql)?(?:\+[A-Za-z]+)?://[^\s\"'<>]+")


class RedactionFilter:
    """Apply :data:`MASK` to credentials before they hit the log stream."""

    SENSITIVE_KEYS: frozenset[str] = frozenset(
        {
            "authorization",
            "token",
            "ciphertext",
            "password",
            "database_url",
            "supabase_database_url",
            "tenant_token_key",
            "backend_secret",
            "gemini_api_key",
            "minimax_api_key",
            "canvas_api_token",
            "api_key",
            "secret",
        }
    )

    def filter(self, record: logging.LogRecord) -> bool:
        """Mutate the record so its message and args are redacted in place.

        Returning ``True`` keeps the record (Python logging contract).
        The filter is attached to the root logger and uvicorn loggers
        via ``addFilter`` so every record reaches it.
        """
        try:
            if isinstance(record.msg, str):
                record.msg = self.redact_message(record.msg)
            args = record.args
            if isinstance(args, Mapping):
                record.args = self.redact_dict(dict(args))
            elif isinstance(args, tuple):
                record.args = tuple(self._redact_value(a) for a in args)
            elif isinstance(args, list):
                record.args = [self._redact_value(a) for a in args]
        except Exception:  # noqa: BLE001, S110 - defensive, never crash logging
            pass
        return True

    def redact_message(self, message: str) -> str:
        """Mask every credential-shaped substring in ``message``."""
        if not message:
            return message
        scrubbed = _FERNET_PATTERN.sub(_MASK, message)
        scrubbed = _BEARER_PATTERN.sub(_MASK, scrubbed)
        scrubbed = _PG_URL_PATTERN.sub(_MASK, scrubbed)
        return scrubbed

    def redact_dict(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return a copy of ``payload`` with sensitive values masked."""
        redacted: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(key, str) and key.lower() in self.SENSITIVE_KEYS:
                redacted[key] = _MASK
            else:
                redacted[key] = self._redact_value(value)
        return redacted

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return self.redact_dict(value)
        if isinstance(value, list):
            return [self._redact_value(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self._redact_value(v) for v in value)
        if isinstance(value, str):
            return self.redact_message(value)
        return value


__all__ = ["RedactionFilter"]
