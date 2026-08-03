"""Backend JWT verification (PR 2 task 2.2).

Backend sessions are independent of Canvas. A valid backend JWT carries
the user's backend identity in the ``sub`` claim and is signed HS256
with the ``BACKEND_SECRET`` environment variable. A Canvas token
MUST NOT be accepted as backend authentication.

The signature comparison goes through PyJWT which uses
``hmac.compare_digest`` internally, so the verification is
constant-time.
"""

from __future__ import annotations

import hmac
from typing import Any

import jwt

from app.core.errors import safe_message


class InvalidToken(Exception):
    """Raised when a backend JWT is missing, malformed, or invalid.

    Distinct from :class:`cryptography.fernet.InvalidToken` so callers
    importing ``app.security.backend_auth`` do not accidentally catch a
    Fernet decryption failure as a JWT failure (or vice versa).
    """


def _scrub(value: Any) -> Any:
    """Apply :func:`safe_message` to any string we might surface."""
    if isinstance(value, str):
        return safe_message(value)
    return value


def verify_backend_jwt(token: str) -> str:
    """Verify a backend JWT and return its ``sub`` claim.

    Raises :class:`InvalidToken` on any failure (missing, malformed,
    wrong signature, wrong algorithm, expired, missing ``sub``). The
    exception message is scrubbed through :func:`safe_message` before
    returning so a malformed token header can never echo back into a
    log line or HTTP response body.
    """
    if not token:
        raise InvalidToken("missing token")
    if not isinstance(token, str):
        raise InvalidToken("token must be a string")

    # Peek at the header without verifying the signature so we can reject
    # anything that isn't HS256 BEFORE doing signature work. This is the
    # deterministic gate that keeps an opaque Canvas token from passing
    # as a backend credential.
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise InvalidToken(_scrub(str(exc))) from exc
    if header.get("alg") != "HS256":
        raise InvalidToken("unsupported JWT algorithm; expected HS256")

    try:
        # ``algorithms=["HS256"]`` forces the verifier to reject any
        # other algorithm even if the header claims something else.
        # ``options`` makes the claim requirements explicit.
        payload: dict[str, Any] = jwt.decode(
            token,
            _get_backend_secret(),
            algorithms=["HS256"],
            options={"require": ["sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidToken("token expired") from exc
    except jwt.InvalidTokenError as exc:
        # Catch-all for PyJWT validation errors (bad signature, bad
        # claim, etc.). Scrub the message before re-raising so we never
        # echo a secret back to the caller or to a log line.
        raise InvalidToken(_scrub(str(exc))) from exc

    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise InvalidToken("token missing sub claim")
    return sub


def constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string compare for callers that need it.

    The same helper is used by the HMAC check on the
    ``X-Internal-Scheduler`` header (PR 4). Exposed here so tests and
    downstream modules have a single implementation to audit.
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _get_backend_secret() -> str:
    """Read ``BACKEND_SECRET`` lazily so tests can monkeypatch it cheaply."""
    from app.core.config import get_settings

    return get_settings().backend_secret


__all__ = ["InvalidToken", "constant_time_compare", "verify_backend_jwt"]
