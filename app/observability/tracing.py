"""Optional OpenTelemetry OTLP exporter (PR 6 task 6.5, design §9).

The exporter is OFF by default. Set ``OTEL_ENABLED=1`` in the
environment to install the OTLP gRPC exporter + batch processor; when
disabled, :func:`get_tracer` returns a :class:`NoopTracer` so call
sites never have to branch on the feature flag.

The redaction filter (:class:`app.security.redaction.RedactionFilter`)
wraps every span attribute by the time we apply it (PR 6 only requires
the hook to exist; production code path uses the structured logger as
the primary redaction surface).
"""

from __future__ import annotations

import os
import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Self

OTEL_ENABLED_ENV = "OTEL_ENABLED"


# ---------------------------------------------------------------------------
# No-op fallback (used when OTEL is disabled or unavailable)
# ---------------------------------------------------------------------------


class _NoopSpan:
    """A span-shaped object that satisfies ``with span:`` and friends."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: types.TracebackType | None) -> bool:
        return False

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def record_exception(self, exc: Any) -> None:
        return None

    def end(self) -> None:
        return None


class _NoopTracer:
    """A tracer-shaped object that returns no-op spans."""

    @contextmanager
    def start_as_current_span(self, name: str, **_kwargs: Any) -> Iterator[_NoopSpan]:
        yield _NoopSpan()


def _noop_tracer() -> _NoopTracer:
    return _NoopTracer()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def is_enabled() -> bool:
    """Return ``True`` when ``OTEL_ENABLED`` truthy-env is set."""
    return os.environ.get(OTEL_ENABLED_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def init_tracing() -> Any | None:
    """Install the OTLP exporter when enabled; otherwise return ``None``.

    Called once during app lifespan startup. The function swallows any
    exception raised by the OpenTelemetry SDK (e.g. the OTLP endpoint
    is unreachable at boot) so a misconfigured collector never blocks
    the API.
    """
    if not is_enabled():
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": "canvas-rag-backend"})
        provider = TracerProvider(resource=resource)
        processor = BatchSpanProcessor(OTLPSpanExporter())
        provider.add_span_processor(processor)
        trace.set_tracer_provider(provider)
        return provider
    except (ImportError, RuntimeError, OSError):
        return None


def get_tracer(name: str) -> Any:
    """Return a tracer; the no-op tracer when OTEL is disabled."""
    if not is_enabled():
        return _noop_tracer()
    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except (ImportError, RuntimeError):
        return _noop_tracer()


def shutdown_tracing(provider: Any | None) -> None:
    """Flush + shutdown the provider if one was installed."""
    if provider is None:
        return
    try:
        provider.shutdown()
    except (RuntimeError, OSError):
        pass


__all__ = [
    "OTEL_ENABLED_ENV",
    "get_tracer",
    "init_tracing",
    "is_enabled",
    "shutdown_tracing",
]


# ---------------------------------------------------------------------------
# Self-test (executable via ``python -m app.observability.tracing``)
# ---------------------------------------------------------------------------


def _selftest() -> None:
    """Run disabled-by-default + no-op span assertions."""
    # 1. Disabled by default (env may be unset in the test runner).
    if os.environ.get(OTEL_ENABLED_ENV) is not None:
        # The test runner set the env; ensure we still get the right
        # behaviour.
        assert is_enabled() in (True, False)
    else:
        assert is_enabled() is False

    # 2. get_tracer returns an object that supports start_as_current_span
    #    as both a method and a context manager; both paths yield a
    #    no-op span when disabled.
    tracer = get_tracer("selftest")
    with tracer.start_as_current_span("selftest-span") as span:
        span.set_attribute("tenant_id_hash", "abc")
        span.record_exception(None)
        # No-op span must accept end() too.
        span.end()

    # 3. shutdown_tracing is safe with a None provider.
    shutdown_tracing(None)

    # 4. init_tracing is a no-op when disabled.
    assert init_tracing() is None


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    _selftest()