"""Unit tests for ``app.services.tenant_service.TenantService``."""

from __future__ import annotations

from datetime import UTC

import pytest
from cryptography.fernet import Fernet

from app.security.token_crypto import TokenCipher
from app.services.tenant_service import TenantNotFound, TenantService


def _fresh_cipher() -> TokenCipher:
    return TokenCipher(Fernet.generate_key().decode("ascii"))


def test_get_or_create_tenant_is_idempotent_per_backend_user() -> None:
    service = TenantService()

    first = service.get_or_create_tenant("backend-user-42")
    second = service.get_or_create_tenant("backend-user-42")
    third = service.get_or_create_tenant("backend-user-42")

    assert first.id == second.id == third.id
    assert first.backend_user_id == "backend-user-42"


def test_get_or_create_tenant_creates_distinct_tenants_per_user() -> None:
    service = TenantService()

    a = service.get_or_create_tenant("alice")
    b = service.get_or_create_tenant("bob")

    assert a.id != b.id


def test_get_or_create_tenant_rejects_empty_backend_user_id() -> None:
    service = TenantService()
    with pytest.raises(ValueError):
        service.get_or_create_tenant("")


def test_store_canvas_token_then_decrypt_returns_plaintext() -> None:
    service = TenantService()
    cipher = _fresh_cipher()
    tenant = service.get_or_create_tenant("user-1")

    service.store_canvas_token(tenant.id, "1234~real-canvas-token", cipher)

    assert service.get_decrypted_canvas_token(tenant.id) == "1234~real-canvas-token"


def test_get_decrypted_canvas_token_raises_when_tenant_has_no_credentials() -> None:
    service = TenantService()
    tenant = service.get_or_create_tenant("user-1")

    with pytest.raises(TenantNotFound):
        service.get_decrypted_canvas_token(tenant.id)


def test_get_decrypted_canvas_token_raises_when_cipher_for_version_missing() -> None:
    """If no cipher is bound for ``key_version``, decryption MUST fail closed.

    The scenario: a credential was created referencing a ``key_version``
    that no cipher slot knows how to decrypt. We exercise it by
    registering a credential whose ``key_version`` has no bound cipher.
    """
    from datetime import datetime

    from app.security.token_crypto import EncryptedToken
    from app.services.tenant_service import _StoredCredential

    service = TenantService()
    tenant = service.get_or_create_tenant("user-1")

    cipher_v1 = _fresh_cipher()
    ciphertext = cipher_v1.encrypt("placeholder").ciphertext
    service.bind_cipher(1, cipher_v1)
    service._credentials[tenant.id] = _StoredCredential(
        envelope=EncryptedToken(ciphertext=ciphertext, key_version=42),
        cipher=cipher_v1,
        created_at=datetime.now(UTC),
    )

    with pytest.raises(TenantNotFound):
        service.get_decrypted_canvas_token(tenant.id)


def test_store_canvas_token_persists_only_ciphertext() -> None:
    """``_StoredCredential.envelope`` MUST be bytes, never a plaintext str."""
    service = TenantService()
    cipher = _fresh_cipher()
    tenant = service.get_or_create_tenant("user-1")

    stored = service.store_canvas_token(tenant.id, "plaintext-should-not-leak", cipher)

    assert isinstance(stored.envelope.ciphertext, bytes)
    assert b"plaintext-should-not-leak" not in stored.envelope.ciphertext
    assert stored.envelope.key_version == cipher.key_version
