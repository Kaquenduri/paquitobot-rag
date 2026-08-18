"""HMAC verification helpers for the canvas-mock webhook receiver.

REQ-WEBHOOKS-6 (canvas-mock-api spec) defines the signature scheme:

    signature = hex(HMAC_SHA256(
        secret, f"{attempt_ts}.{raw_body_utf8}"
    ))

The standard ``X-Canvas-Mock-Signature`` header is the comma-
separated envelope ``t=<ts>,v1=<hex>``. The receiver MUST:

1. Parse the header and reject malformed envelopes.
2. Verify the timestamp is within ``SKEW_WINDOW_SECONDS`` (300) of
   ``current_ts`` (a clock-skew check; locked).
3. Compare the signature with ``constant_time_compare`` (reused from
   :mod:`app.security.backend_auth`) so timing attacks cannot leak
   the secret.
4. Fail closed on any deviation: missing header, unknown version,
   wrong secret, expired timestamp.

The helper is intentionally small — the controller is the one that
turns the result into a 401, a 503, or a 200.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Final

from app.core.logging import get_logger
from app.security.backend_auth import constant_time_compare

logger = get_logger("app.security.webhook_hmac")

#: Locked clock-skew window (REQ-WEBHOOKS-6 / design.md).
SKEW_WINDOW_SECONDS: Final[int] = 300

#: The signature envelope is a comma-separated list of
#: ``key=value`` pairs. We only support ``t=`` and ``v1=`` today;
#: other versions are rejected to fail closed.
_SUPPORTED_HEADER_FIELDS: Final[frozenset[str]] = frozenset({"t", "v1"})


def parse_signature_header(header: str) -> tuple[int, str]:
    """Parse ``t=<ts>,v1=<hex>`` into ``(ts, sig)``.

    Raises :class:`ValueError` on any deviation. The controller turns
    this into a 401 response.
    """
    if not isinstance(header, str) or not header:
        raise ValueError("missing signature header")
    parts: dict[str, str] = {}
    for chunk in header.split(","):
        key, _, value = chunk.strip().partition("=")
        if not key or not value:
            raise ValueError("malformed signature header")
        parts[key.strip()] = value.strip()
    extra = set(parts) - _SUPPORTED_HEADER_FIELDS
    if extra:
        raise ValueError(f"unsupported signature fields: {sorted(extra)}")
    if "t" not in parts or "v1" not in parts:
        raise ValueError("missing t or v1 in signature header")
    try:
        ts = int(parts["t"])
    except ValueError as exc:
        raise ValueError("timestamp is not an integer") from exc
    return ts, parts["v1"]


def compute_signature(secret: str, attempt_ts: int, raw_body: bytes) -> str:
    """Compute the canonical signature for ``(secret, ts, raw_body)``.

    The payload is ``f"{attempt_ts}.{raw_body_utf8}"``; the digest is
    SHA-256, hex-encoded lower-case. Exposed for tests and the
    controller's replay path.
    """
    payload = f"{attempt_ts}.".encode("utf-8") + raw_body
    return hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()


def is_within_skew(attempt_ts: int, *, current_ts: int) -> bool:
    """Return True iff ``attempt_ts`` is within ``SKEW_WINDOW_SECONDS`` of now."""
    return abs(attempt_ts - current_ts) <= SKEW_WINDOW_SECONDS


def verify_signature(
    secret: str,
    header: str,
    raw_body: bytes,
    *,
    current_ts: int | None = None,
) -> bool:
    """Return True iff the header's signature is valid for ``raw_body``.

    Wraps the three checks in order: header parse → skew → constant-
    time compare. Any failure returns ``False`` so the caller can
    treat it as a single ``401`` outcome.
    """
    if not secret:
        # Fail closed: a missing secret is a configuration bug.
        return False
    try:
        attempt_ts, signature = parse_signature_header(header)
    except ValueError:
        return False
    now = int(time.time()) if current_ts is None else current_ts
    if not is_within_skew(attempt_ts, current_ts=now):
        return False
    expected = compute_signature(secret, attempt_ts, raw_body)
    return constant_time_compare(expected, signature)


__all__ = [
    "SKEW_WINDOW_SECONDS",
    "compute_signature",
    "is_within_skew",
    "parse_signature_header",
    "verify_signature",
]
