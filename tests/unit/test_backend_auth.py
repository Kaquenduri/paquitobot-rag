"""Unit tests for ``app.security.backend_auth``."""

from __future__ import annotations

import secrets
import time

import jwt
import pytest

from app.core.config import get_settings
from app.security.backend_auth import (
    InvalidToken,
    constant_time_compare,
    issue_backend_jwt,
    verify_backend_jwt,
)


def _settings_secret(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> str:
    """Point ``BACKEND_SECRET`` at a fresh random value for one test."""
    secret = secrets.token_urlsafe(32)
    monkeypatch.setenv("BACKEND_SECRET", secret)
    get_settings.cache_clear()
    return secret


def test_verify_backend_jwt_accepts_valid_hs256_token(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    secret = _settings_secret(monkeypatch, app_settings)
    token = jwt.encode(
        {"sub": "user-42", "exp": int(time.time()) + 60},
        secret,
        algorithm="HS256",
    )

    assert verify_backend_jwt(token) == "user-42"


def test_verify_backend_jwt_rejects_wrong_signature(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    _settings_secret(monkeypatch, app_settings)
    token = jwt.encode({"sub": "user-42"}, "wrong-secret", algorithm="HS256")

    with pytest.raises(InvalidToken):
        verify_backend_jwt(token)


def test_verify_backend_jwt_rejects_expired_token(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    secret = _settings_secret(monkeypatch, app_settings)
    token = jwt.encode(
        {"sub": "user-42", "exp": int(time.time()) - 1},
        secret,
        algorithm="HS256",
    )

    with pytest.raises(InvalidToken):
        verify_backend_jwt(token)


def test_verify_backend_jwt_rejects_token_without_sub(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    secret = _settings_secret(monkeypatch, app_settings)
    token = jwt.encode({"exp": int(time.time()) + 60}, secret, algorithm="HS256")

    with pytest.raises(InvalidToken):
        verify_backend_jwt(token)


def test_verify_backend_jwt_rejects_missing_token() -> None:
    with pytest.raises(InvalidToken):
        verify_backend_jwt("")


def test_canvas_token_is_not_accepted_as_backend_jwt(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    """An opaque Canvas token (~) MUST be rejected by ``verify_backend_jwt``."""
    _settings_secret(monkeypatch, app_settings)
    canvas_token = "12345~abcdefghijklmnopqrstuvwxyz0123456789"

    with pytest.raises(InvalidToken):
        verify_backend_jwt(canvas_token)


def test_non_hs256_algorithm_is_rejected(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    """A JWT signed with HS512 must be rejected even if the signature is valid."""
    secret = _settings_secret(monkeypatch, app_settings)
    token = jwt.encode({"sub": "u"}, secret, algorithm="HS512")

    with pytest.raises(InvalidToken):
        verify_backend_jwt(token)


def test_constant_time_compare_uses_hmac() -> None:
    assert constant_time_compare("hello", "hello")
    assert not constant_time_compare("hello", "world")


def test_invalid_token_error_message_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    """The InvalidToken message MUST NOT echo a secret back to the caller."""
    _settings_secret(monkeypatch, app_settings)
    # Hand-craft a JWT-shaped string with token-looking substrings inside
    # the payload so PyJWT's exception message would carry them through.
    bad_token = "eyJhbGciOiJIUzI1NiJ9.Bearer.gAAAAAdeadbeefcafebabe.invalid"

    with pytest.raises(InvalidToken) as exc:
        verify_backend_jwt(bad_token)

    assert "gAAAAA" not in str(exc.value)
    assert "Bearer" not in str(exc.value) or "***REDACTED***" in str(exc.value)


# ---------------------------------------------------------------------------
# issue_backend_jwt (the inverse of verify_backend_jwt)
# ---------------------------------------------------------------------------


def test_issue_backend_jwt_round_trips_through_verify(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    """A token signed by ``issue_backend_jwt`` MUST verify cleanly."""
    secret = _settings_secret(monkeypatch, app_settings)

    token, ttl = issue_backend_jwt("google-user-99")

    assert ttl == get_settings().login_token_ttl_seconds
    sub = verify_backend_jwt(token)
    assert sub == "google-user-99"
    # And the signature was made with the same secret the verifier checks.
    decoded = jwt.decode(token, secret, algorithms=["HS256"])
    assert decoded["iss"] == "paquitobot"
    assert "iat" in decoded and "exp" in decoded


def test_issue_backend_jwt_rejects_empty_sub() -> None:
    with pytest.raises(InvalidToken):
        issue_backend_jwt("")


def test_issue_backend_jwt_expires_within_ttl_window(
    monkeypatch: pytest.MonkeyPatch, app_settings: dict[str, str]
) -> None:
    _settings_secret(monkeypatch, app_settings)
    before = int(time.time())
    token, ttl = issue_backend_jwt("u")
    decoded = jwt.decode(token, options={"verify_signature": False})
    after = int(time.time())
    assert before <= decoded["iat"] <= after
    assert decoded["exp"] - decoded["iat"] == ttl
