"""Fernet-based Canvas token custody (PR 2 task 2.1).

Plaintext Canvas tokens are encrypted at rest with Fernet
(AES-128-CBC + HMAC-SHA256 + timestamp) using the key derived from
``TENANT_TOKEN_KEY``. The :class:`TokenCipher` is the single source of
truth for encrypt / decrypt; the rest of the codebase MUST NOT call
``cryptography.fernet.Fernet`` directly so the log redaction filter
can guarantee no plaintext token ever reaches a log line.

Each ciphertext records its ``key_version`` so a future rotation can
re-encrypt old rows without changing the on-disk layout.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


@dataclass(frozen=True)
class EncryptedToken:
    """Encrypted Canvas token plus the key version that produced it."""

    ciphertext: bytes
    key_version: int

    def __repr__(self) -> str:  # pragma: no cover - defensive only
        return (
            f"EncryptedToken(key_version={self.key_version}, "
            "ciphertext=***REDACTED***)"
        )


class TokenCipher:
    """Wrap a Fernet instance with a versioned key slot.

    The version is opaque to callers: it is recorded next to the
    ciphertext and read back at decrypt time. Rotating the key means
    binding a new :class:`TokenCipher` to a different ``key_version``
    and re-encrypting; old ciphertexts are still readable when the
    matching version is bound.
    """

    CURRENT_KEY_VERSION = 1

    def __init__(self, key: str, *, key_version: int = CURRENT_KEY_VERSION) -> None:
        if not isinstance(key, str):
            raise TypeError(
                "TokenCipher requires the Fernet key as a urlsafe-base64 string"
            )
        try:
            decoded = base64.urlsafe_b64decode(key)
        except (ValueError, TypeError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
            raise ValueError(
                "TokenCipher requires a urlsafe-base64 Fernet key (32 raw bytes)"
            ) from exc
        if len(decoded) != 32:
            raise ValueError(
                "TokenCipher requires a Fernet key that decodes to 32 raw bytes "
                f"(got {len(decoded)})"
            )
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, TypeError) as exc:  # pragma: no cover - belt and braces
            raise ValueError(str(exc)) from exc
        self.key_version = key_version

    def encrypt(self, plaintext: str) -> EncryptedToken:
        """Encrypt ``plaintext`` (the Canvas token) and return its envelope."""
        if not isinstance(plaintext, str):
            raise TypeError("TokenCipher.encrypt expects a str Canvas token")
        ciphertext = self._fernet.encrypt(plaintext.encode("utf-8"))
        return EncryptedToken(ciphertext=ciphertext, key_version=self.key_version)

    def decrypt(self, envelope: EncryptedToken) -> str:
        """Decrypt an :class:`EncryptedToken`; raises :class:`InvalidToken`.

        The Fernet library raises :class:`cryptography.fernet.InvalidToken`
        on any tampering, key mismatch, or expired TTL; we re-export the
        same name through ``TokenCipher`` so callers have a single
        exception to catch.
        """
        if envelope.key_version != self.key_version:
            raise InvalidToken(
                f"TokenCipher key_version mismatch: cipher={envelope.key_version} "
                f"key={self.key_version}"
            )
        plaintext_bytes = self._fernet.decrypt(envelope.ciphertext)
        return plaintext_bytes.decode("utf-8")

    @staticmethod
    def is_ciphertext(value: object) -> bool:
        """Return True if ``value`` looks like a Fernet envelope (``gAAAAA...``)."""
        return isinstance(value, str) and value.startswith("gAAAAA")


__all__ = ["EncryptedToken", "InvalidToken", "TokenCipher"]
