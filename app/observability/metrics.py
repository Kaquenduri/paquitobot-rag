"""Prometheus counters and helpers for PR 6 (design §9).

The module declares the six counters/gauges the design contract
requires and exposes thin ``record_*`` helpers so callers don't have
to know the exact label set. Helpers swallow any ``prometheus_client``
error so a missing metric registry (e.g. inside a unit test that
imports this module twice) never breaks the request path.
"""

from __future__ import annotations

import hashlib
from typing import Any

import prometheus_client

# ---------------------------------------------------------------------------
# Counter / gauge declarations
# ---------------------------------------------------------------------------


rag_requests_total = prometheus_client.Counter(
    "rag_requests_total",
    "RAG requests served by the /query controller.",
    labelnames=("route", "lang", "outcome"),
)

sql_validations_total = prometheus_client.Counter(
    "sql_validations_total",
    "Text-to-SQL validator outcomes (allowed or rejected).",
    labelnames=("result",),
)

canvas_requests_total = prometheus_client.Counter(
    "canvas_requests_total",
    "Canvas HTTP requests by endpoint and result class.",
    labelnames=("endpoint", "result"),
)

sync_runs_total = prometheus_client.Counter(
    "sync_runs_total",
    "Manual + scheduled sync runs by hashed tenant.",
    labelnames=("tenant_id_hash", "result"),
)

sync_lag_seconds = prometheus_client.Gauge(
    "sync_lag_seconds",
    "Lag between now and the last successful sync per tenant.",
    labelnames=("tenant_id_hash",),
)

token_decrypt_failures_total = prometheus_client.Counter(
    "token_decrypt_failures_total",
    "Fernet Canvas-token decryption failures.",
    labelnames=("result",),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_tenant_id(tenant_id: Any) -> str:
    """Return a short, stable hash of ``tenant_id`` for use as a label.

    We never want a raw UUID in a metric label because the cardinality
    is unbounded and the labels leak tenant identity. A truncated
    SHA-256 prefix is enough for label uniqueness and keeps PII out
    of the Prometheus scrape output.
    """
    if tenant_id is None:
        return "unknown"
    try:
        return hashlib.sha256(str(tenant_id).encode("utf-8")).hexdigest()[:16]
    except (TypeError, ValueError):  # defensive
        return "unknown"


def _safe_inc(metric: Any, **labels: Any) -> None:
    """Increment ``metric`` with ``labels``; never raise."""
    try:
        if labels:
            metric.labels(**labels).inc()
        else:
            metric.inc()
    except (RuntimeError, ValueError, TypeError):  # defensive
        pass


def _safe_set_gauge(metric: Any, value: float, **labels: Any) -> None:
    """Set ``metric`` to ``value`` with ``labels``; never raise."""
    try:
        if labels:
            metric.labels(**labels).set(value)
        else:
            metric.set(value)
    except (RuntimeError, ValueError, TypeError):  # defensive
        pass


def record_rag_request(route: str, lang: str, outcome: str) -> None:
    """Increment ``rag_requests_total``."""
    _safe_inc(
        rag_requests_total,
        route=str(route or "unknown"),
        lang=str(lang or "unknown"),
        outcome=str(outcome or "unknown"),
    )


def record_sql_validation(result: str) -> None:
    """Increment ``sql_validations_total``. ``result`` is ``allowed`` or
    ``rejected``."""
    _safe_inc(sql_validations_total, result=str(result or "unknown"))


def record_canvas_request(endpoint: str, result: str) -> None:
    """Increment ``canvas_requests_total``."""
    _safe_inc(
        canvas_requests_total,
        endpoint=str(endpoint or "unknown"),
        result=str(result or "unknown"),
    )


def record_sync_run(tenant_id: Any, result: str) -> None:
    """Increment ``sync_runs_total`` with a hashed tenant label."""
    _safe_inc(
        sync_runs_total,
        tenant_id_hash=_hash_tenant_id(tenant_id),
        result=str(result or "unknown"),
    )


def set_sync_lag(tenant_id: Any, lag_seconds: float) -> None:
    """Set ``sync_lag_seconds`` for ``tenant_id`` (hashed label)."""
    try:
        _safe_set_gauge(
            sync_lag_seconds,
            float(lag_seconds),
            tenant_id_hash=_hash_tenant_id(tenant_id),
        )
    except (TypeError, ValueError):
        # ``lag_seconds`` is best-effort; never crash the request.
        pass


def record_token_decrypt(result: str) -> None:
    """Increment ``token_decrypt_failures_total``.

    ``result`` is ``ok`` on a successful decrypt or one of
    ``invalid_cipher``, ``wrong_key_version``, ``missing_credential``
    on failure.
    """
    _safe_inc(token_decrypt_failures_total, result=str(result or "unknown"))


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------


__all__ = [
    "canvas_requests_total",
    "rag_requests_total",
    "record_canvas_request",
    "record_rag_request",
    "record_sql_validation",
    "record_sync_run",
    "record_token_decrypt",
    "set_sync_lag",
    "sql_validations_total",
    "sync_lag_seconds",
    "sync_runs_total",
    "token_decrypt_failures_total",
]


# ---------------------------------------------------------------------------
# Self-test (executable via ``python -m app.observability.metrics``)
# ---------------------------------------------------------------------------


def _selftest() -> None:
    """Run counter / helper assertions without external Prometheus."""
    # 1. Counters can be incremented through the helpers without
    #    raising; we don't assert exact values because prometheus
    #    values are process-global and may be shared with other tests.
    record_rag_request(route="relational", lang="en", outcome="ok")
    record_sql_validation(result="allowed")
    record_canvas_request(endpoint="/users/self", result="ok")
    record_sync_run(tenant_id="tenant-abc", result="ok")
    set_sync_lag(tenant_id="tenant-abc", lag_seconds=42.5)
    record_token_decrypt(result="ok")

    # 2. Helper does not crash on bad input.
    record_rag_request(route=None, lang=None, outcome=None)
    record_sql_validation(result=None)
    record_canvas_request(endpoint=None, result=None)
    record_sync_run(tenant_id=None, result=None)
    set_sync_lag(tenant_id=None, lag_seconds=None)
    record_token_decrypt(result=None)

    # 3. _hash_tenant_id is stable and short.
    h1 = _hash_tenant_id("tenant-x")
    h2 = _hash_tenant_id("tenant-x")
    h3 = _hash_tenant_id("tenant-y")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 16

    # 4. Counter objects expose ``labels`` (the prometheus contract).
    assert hasattr(rag_requests_total, "labels")
    assert hasattr(sql_validations_total, "labels")
    assert hasattr(canvas_requests_total, "labels")
    assert hasattr(sync_runs_total, "labels")
    assert hasattr(sync_lag_seconds, "labels")
    assert hasattr(token_decrypt_failures_total, "labels")


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    _selftest()