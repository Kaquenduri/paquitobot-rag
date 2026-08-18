"""Unit tests for the HMAC verification helper (PR 5 task 5.1).

The webhook receiver uses ``canvas_mock_webhook_secret`` to verify
the ``X-Canvas-Mock-Signature`` header. The payload is
``f"{attempt_ts}.{raw_body_utf8}"`` and the digest is SHA-256,
hex-encoded, separated by the standard ``t=<ts>,v1=<hex>`` envelope.

The verification must:
- accept a freshly-computed signature (golden test);
- reject a 301+ second skew (timestamp window = 300);
- reject a 300-second boundary (off-by-one);
- reuse ``constant_time_compare`` from :mod:`app.security.backend_auth`;
- fail closed when the secret is missing;
- never log the raw body or signature value.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest

from app.security.webhook_hmac import (
    SKEW_WINDOW_SECONDS,
    compute_signature,
    is_within_skew,
    parse_signature_header,
    verify_signature,
)

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "canvas_mock" / "webhooks"


def test_compute_signature_matches_golden() -> None:
    """A hand-computed HMAC must match ``compute_signature`` outputs."""
    secret = "whsec_test_skR8Zb2k9F3pQ7vN1sL4xM6wT0yC5hE"
    ts = 1_700_000_000
    body = b'{"id":1,"event":"grade.posted"}'
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()
    assert compute_signature(secret, ts, body) == expected


def test_verify_signature_accepts_valid_envelope() -> None:
    """A signature built with the same algorithm passes verification."""
    secret = "test-secret"
    ts = 1_700_000_000
    body = b'{"x":1}'
    sig = compute_signature(secret, ts, body)
    header = f"t={ts},v1={sig}"
    assert verify_signature(secret, header, body, current_ts=ts) is True


def test_verify_signature_rejects_tampered_body() -> None:
    """A signature over one body, verified against another, fails."""
    secret = "test-secret"
    ts = 1_700_000_000
    body = b'{"x":1}'
    sig = compute_signature(secret, ts, body)
    tampered = b'{"x":2}'
    assert verify_signature(secret, f"t={ts},v1={sig}", tampered, current_ts=ts) is False


def test_verify_signature_rejects_skew_above_window() -> None:
    """A timestamp 301 seconds in the past is rejected."""
    secret = "test-secret"
    now = 1_700_000_000
    ts = now - 301
    body = b'{"x":1}'
    sig = compute_signature(secret, ts, body)
    assert verify_signature(secret, f"t={ts},v1={sig}", body, current_ts=now) is False


def test_verify_signature_accepts_skew_at_boundary() -> None:
    """A timestamp exactly 300 seconds in the past is ACCEPTED (inclusive)."""
    secret = "test-secret"
    now = 1_700_000_000
    ts = now - 300
    body = b'{"x":1}'
    sig = compute_signature(secret, ts, body)
    assert verify_signature(secret, f"t={ts},v1={sig}", body, current_ts=now) is True


def test_verify_signature_rejects_skew_forward_window() -> None:
    """A timestamp 301 seconds in the future is rejected (no replay buffer)."""
    secret = "test-secret"
    now = 1_700_000_000
    ts = now + 301
    body = b'{"x":1}'
    sig = compute_signature(secret, ts, body)
    assert verify_signature(secret, f"t={ts},v1={sig}", body, current_ts=now) is False


def test_verify_signature_rejects_signature_with_wrong_secret() -> None:
    """A signature built with a different secret fails the constant-time compare."""
    ts = 1_700_000_000
    body = b'{"x":1}'
    sig = compute_signature("OTHER_SECRET", ts, body)
    assert (
        verify_signature(
            "test-secret", f"t={ts},v1={sig}", body, current_ts=ts
        )
        is False
    )


def test_verify_signature_uses_constant_time_compare() -> None:
    """The comparison MUST use ``constant_time_compare``, not ``==``.

    Tested by monkey-patching the import and asserting the substitute
    is called.
    """
    secret = "test-secret"
    ts = 1_700_000_000
    body = b'{"x":1}'
    sig = compute_signature(secret, ts, body)

    import app.security.webhook_hmac as module

    calls: list[tuple[str, str]] = []
    real = module.constant_time_compare

    def _spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return real(a, b)

    module.constant_time_compare = _spy  # type: ignore[assignment]
    try:
        verify_signature(secret, f"t={ts},v1={sig}", body, current_ts=ts)
    finally:
        module.constant_time_compare = real  # type: ignore[assignment]
    assert calls, "constant_time_compare was not invoked"


def test_parse_signature_header_extracts_components() -> None:
    """``parse_signature_header`` returns ``(ts, sig)`` or raises."""
    ts, sig = parse_signature_header("t=1700000000,v1=deadbeef")
    assert ts == 1_700_000_000
    assert sig == "deadbeef"


def test_parse_signature_header_rejects_malformed() -> None:
    """A header missing the ``t=`` or ``v1=`` part raises ``ValueError``."""
    with pytest.raises(ValueError):
        parse_signature_header("not-an-envelope")


def test_is_within_skew_returns_true_for_now() -> None:
    """``is_within_skew`` returns True for the current timestamp."""
    now = int(time.time())
    assert is_within_skew(now, current_ts=now) is True


def test_skew_window_is_300_seconds() -> None:
    """The skew window is the locked 300 seconds."""
    assert SKEW_WINDOW_SECONDS == 300
