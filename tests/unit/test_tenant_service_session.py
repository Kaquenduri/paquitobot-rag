"""Unit tests for the session-backed :class:`TenantService` (PR 3).

PR 2 introduced the in-memory :class:`TenantService`; PR 3 adds an
optional SQLAlchemy session so the same service interface can drive
the real ``tenants`` / ``canvas_credentials`` tables. These tests
exercise the session path against a SQLite in-memory database and
verify the existing in-memory contract (PR 2) is preserved by the
default constructor.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from cryptography.fernet import Fernet

from app.security.token_crypto import TokenCipher
from app.services.tenant_service import (
    TenantNotFound,
    TenantRepository,
    TenantService,
)


def _fresh_cipher() -> TokenCipher:
    return TokenCipher(Fernet.generate_key().decode("ascii"))


# ---------------------------------------------------------------------------
# In-memory mode (PR 2 contract preserved)
# ---------------------------------------------------------------------------


def test_in_memory_mode_does_not_require_session() -> None:
    """``TenantService()`` with no args still works for PR 2 tests."""
    service = TenantService()
    tenant = service.get_or_create_tenant("backend-user-1")
    assert tenant.backend_user_id == "backend-user-1"


# ---------------------------------------------------------------------------
# Session-backed mode (PR 3)
# ---------------------------------------------------------------------------


TENANT_UUID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def test_session_backed_get_or_create_persists_tenant(db_session: Any) -> None:
    service = TenantService(session=db_session)
    record = service.get_or_create_tenant("backend-user-pr3")
    db_session.commit()

    # New session reads the row back.
    repository = TenantRepository(db_session)
    row = repository.get_tenant(record.id)
    assert row is not None
    assert row.backend_user_id == "backend-user-pr3"


def test_session_backed_get_or_create_is_idempotent(db_session: Any) -> None:
    service = TenantService(session=db_session)
    first = service.get_or_create_tenant("backend-user-pr3")
    db_session.commit()
    second = service.get_or_create_tenant("backend-user-pr3")
    assert first.id == second.id


def test_session_backed_store_canvas_token_persists_ciphertext(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TenantService(session=db_session)
    record = service.get_or_create_tenant("backend-user-pr3")
    db_session.commit()

    cipher = _fresh_cipher()
    service.store_canvas_token(record.id, "real-canvas-token", cipher)
    db_session.commit()

    repository = TenantRepository(db_session)
    row = repository.get_canvas_credential(record.id)
    assert row is not None
    assert row.key_version == cipher.key_version
    assert isinstance(row.ciphertext, bytes)
    assert b"real-canvas-token" not in row.ciphertext


def test_session_backed_get_decrypted_canvas_token_round_trip(db_session: Any) -> None:
    service = TenantService(session=db_session)
    record = service.get_or_create_tenant("backend-user-pr3")
    db_session.commit()

    cipher = _fresh_cipher()
    service.bind_cipher(cipher.key_version, cipher)
    service.store_canvas_token(record.id, "real-canvas-token", cipher)
    db_session.commit()

    plaintext = service.get_decrypted_canvas_token(record.id)
    assert plaintext == "real-canvas-token"


def test_session_backed_get_decrypted_canvas_token_raises_when_missing(
    db_session: Any,
) -> None:
    service = TenantService(session=db_session)
    record = service.get_or_create_tenant("backend-user-pr3")
    db_session.commit()

    with pytest.raises(TenantNotFound):
        service.get_decrypted_canvas_token(record.id)


def test_session_backed_get_decrypted_canvas_token_raises_when_cipher_missing(
    db_session: Any,
) -> None:
    """Decryption fails closed when no cipher matches the recorded key_version.

    Stores a ciphertext under ``key_version`` but never binds a cipher
    for that version — mirrors the PR 2 in-memory failure path.
    """
    service = TenantService(session=db_session)
    record = service.get_or_create_tenant("backend-user-pr3")
    db_session.commit()

    # Write a row directly with a key_version that no cipher will be bound for.
    from cryptography.fernet import Fernet

    from app.models import CanvasCredential

    credential = CanvasCredential(
        tenant_id=record.id,
        ciphertext=Fernet.generate_key(),  # arbitrary bytes; won't decrypt
        key_version=99,
    )
    db_session.add(credential)
    db_session.commit()

    with pytest.raises(TenantNotFound):
        service.get_decrypted_canvas_token(record.id)


def test_bind_session_switches_service_into_repository_mode(
    db_session: Any,
) -> None:
    """``bind_session`` lets a service transition mid-request."""
    service = TenantService()  # in-memory first
    record = service.get_or_create_tenant("backend-user-pr3")
    in_memory_id = record.id

    service.bind_session(db_session)
    persisted = service.get_or_create_tenant("backend-user-pr3")
    db_session.commit()

    assert persisted.id != in_memory_id
    repository = TenantRepository(db_session)
    assert repository.get_tenant(persisted.id) is not None