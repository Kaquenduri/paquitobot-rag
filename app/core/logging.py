"""Structured logging setup for the FastAPI service.

PR 1 wired the JSON renderer and a stub :class:`RedactionFilter`. PR 2
replaces the stub with the full redaction logic from
:mod:`app.security.redaction` and adds ``correlation_id``
contextvar helpers so every log line carries the request id when one
is bound by the middleware (PR 6) or by a controller.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

from app.security.redaction import RedactionFilter

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def bind_correlation_id(correlation_id: str) -> None:
    """Bind ``correlation_id`` to the current context for log emission."""
    _correlation_id.set(correlation_id)
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)


def clear_correlation_id() -> None:
    """Unbind the correlation_id so the next request starts fresh."""
    _correlation_id.set(None)
    try:
        structlog.contextvars.unbind_contextvars("correlation_id")
    except LookupError:
        # LookupError fires when no binding exists yet — safe to ignore.
        pass


def get_correlation_id() -> str | None:
    """Return the currently bound correlation_id, or ``None`` if unset."""
    return _correlation_id.get()


def configure_logging(log_level: str = "INFO") -> RedactionFilter:
    """Configure structlog + stdlib logging to emit redacted JSON to stdout.

    Returns the shared :class:`RedactionFilter` so the caller can attach
    it to additional loggers later.
    """
    redaction_filter = RedactionFilter()

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        _safe_event_processor(redaction_filter),
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(redaction_filter.filter)
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    for noisy in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger_obj = logging.getLogger(noisy)
        logger_obj.handlers = [handler]
        logger_obj.propagate = False

    return redaction_filter


def _safe_event_processor(redaction_filter: RedactionFilter):
    """Apply redaction to the event dict before downstream processors."""

    def processor(
        logger: Any, method_name: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
        if "event" in event_dict and isinstance(event_dict["event"], dict):
            event_dict["event"] = redaction_filter.redact_dict(event_dict["event"])
        return event_dict

    return processor


def get_logger(name: str | None = None) -> Any:
    """Return a structlog logger. Convenience wrapper used by app modules."""
    return structlog.get_logger(name)


def configure_console_encoding() -> None:
    """Force UTF-8 on stdout/stderr so structlog tracebacks can be printed.

    structlog renders exceptions with Unicode box-drawing characters. On a
    Windows cp1252 console the print inside ``logger.exception`` raises
    :class:`UnicodeEncodeError`, which turns a *logged* error into a
    *crash*. Called by ``tests/conftest.py`` and by the module selftests
    that exercise error paths.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


__all__ = [
    "RedactionFilter",
    "bind_correlation_id",
    "clear_correlation_id",
    "configure_console_encoding",
    "configure_logging",
    "get_correlation_id",
    "get_logger",
]
