"""Unit tests for ``app.security.token_crypto.TokenCipher``."""

from __future__ import annotations

import base64

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.security.token_crypto import EncryptedToken, TokenCipher


def _fresh_cipher() -> TokenCipher:
    return TokenCipher(Fernet.generate_key().decode("ascii"))


def test_fernet_round_trip_encrypts_and_decrypts_canvas_token() -> None:
    cipher = _fresh_cipher()
    plaintext = "1234~abcdefghijklmnopqrstuvwxyz"

    envelope = cipher.encrypt(plaintext)

    assert isinstance(envelope, EncryptedToken)
    assert envelope.ciphertext != plaintext.encode("utf-8")
    assert envelope.key_version == cipher.key_version
    # Fernet envelope is urlsafe-base64 with the "gAAAAA" magic prefix;
    # the log redaction filter matches on it.
    assert TokenCipher.is_ciphertext(envelope.ciphertext.decode("ascii"))
    assert cipher.decrypt(envelope) == plaintext


def test_fernet_tamper_raises_invalid_token() -> None:
    cipher = _fresh_cipher()
    envelope = cipher.encrypt("super-secret-token")
    tampered = EncryptedToken(
        ciphertext=envelope.ciphertext[:-1] + b"X",
        key_version=envelope.key_version,
    )

    with pytest.raises(InvalidToken):
        cipher.decrypt(tampered)


def test_token_cipher_rejects_malformed_key() -> None:
    with pytest.raises(ValueError):
        TokenCipher("not-a-valid-fernet-key")


def test_token_cipher_rejects_wrong_length_key() -> None:
    too_short = base64.urlsafe_b64encode(b"x" * 16).decode("ascii")
    with pytest.raises(ValueError):
        TokenCipher(too_short)


def test_is_ciphertext_only_matches_fernet_envelopes() -> None:
    assert TokenCipher.is_ciphertext("gAAAAAabcdefghijklmnop")
    assert not TokenCipher.is_ciphertext("Bearer eyJhbGciOiJIUzI1NiJ9")
    assert not TokenCipher.is_ciphertext("1234~not-a-jwt")
    assert not TokenCipher.is_ciphertext(12345)  # type: ignore[arg-type]


def test_key_version_mismatch_raises_invalid_token() -> None:
    cipher_v1 = _fresh_cipher()
    cipher_v2 = TokenCipher(Fernet.generate_key().decode("ascii"), key_version=2)
    envelope_v1 = cipher_v1.encrypt("hello")

    with pytest.raises(InvalidToken):
        cipher_v2.decrypt(envelope_v1)


def test_encrypted_token_repr_masks_ciphertext() -> None:
    cipher = _fresh_cipher()
    envelope = cipher.encrypt("plaintext-never-leaks")
    rendered = repr(envelope)

    assert "***REDACTED***" in rendered
    assert "plaintext-never-leaks" not in rendered
    assert b"plaintext-never-leaks" not in envelope.ciphertext
