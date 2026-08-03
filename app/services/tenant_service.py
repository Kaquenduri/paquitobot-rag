"""Tenant resolution service (PR 2 task 2.7; PR 3 SQLAlchemy seam).

PR 2 shipped an in-memory stub so the dep chain was exercisable
without a real database. PR 3 keeps that contract (so the existing
unit tests stay green) and adds a **session-backed** path:

- :meth:`TenantService.get_or_create_tenant` accepts an optional
  ``session`` argument. When supplied, the lookup / insert is routed
  through :class:`TenantRepository`, which uses the session's
  transaction. When the session is ``None`` the service falls back to
  the in-memory stub (preserved verbatim from PR 2).
- :meth:`TenantService.store_canvas_token` / ``get_decrypted_canvas_token``
  mirror the same dual-mode behaviour: session when present,
  in-memory otherwise.

The service keeps ``get_tenant_service`` / ``reset_tenant_service``
so FastAPI ``Depends`` can resolve the process-wide singleton, but
callers that already hold a session (e.g. the controller wiring in
PR 6) can construct a fresh :class:`TenantService` per request.

Cross-cutting helpers:

- :func:`get_db_session` lives in :mod:`app.core.db` (PR 3 task 4 of
  the user prompt) and yields a SQLAlchemy ``Session`` bound to
  ``SUPABASE_DATABASE_URL``.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CanvasCredential, Tenant
from app.security.token_crypto import EncryptedToken, TokenCipher


class TenantNotFound(Exception):
    """Raised when a tenant_id has no encrypted Canvas token."""


@dataclass(frozen=True)
class TenantRecord:
    """In-memory representation of a tenant row.

    Mirrors the canonical ``tenants`` table so callers that do not
    hold an active SQLAlchemy session can still consume the service
    (the PR 2 tests rely on this dataclass).
    """

    id: uuid.UUID
    backend_user_id: str


@dataclass
class _StoredCredential:
    """Encrypted Canvas token plus the cipher that produced it."""

    envelope: EncryptedToken
    cipher: TokenCipher
    created_at: datetime
    rotated_at: datetime | None = None


class TenantRepository:
    """SQLAlchemy-backed tenant + Canvas-credential repository.

    Owns the queries that :class:`TenantService` delegates to when a
    session is available. Methods commit nothing — the caller drives
    the transaction (FastAPI dependency injection rolls back on
    exception).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_or_create_tenant(self, backend_user_id: str) -> Tenant:
        """Resolve ``backend_user_id`` to a tenant row, creating if missing."""
        if not backend_user_id:
            raise ValueError("backend_user_id is required")
        existing = self._session.execute(
            select(Tenant).where(Tenant.backend_user_id == backend_user_id)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = Tenant(backend_user_id=backend_user_id)
        self._session.add(row)
        self._session.flush()
        return row

    def get_tenant(self, tenant_id: uuid.UUID) -> Tenant | None:
        return self._session.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        ).scalar_one_or_none()

    def get_canvas_credential(self, tenant_id: uuid.UUID) -> CanvasCredential | None:
        return self._session.execute(
            select(CanvasCredential).where(CanvasCredential.tenant_id == tenant_id)
        ).scalar_one_or_none()

    def upsert_canvas_credential(
        self,
        tenant_id: uuid.UUID,
        ciphertext: bytes,
        key_version: int,
    ) -> CanvasCredential:
        """Insert or update the ciphertext row for ``tenant_id``."""
        existing = self.get_canvas_credential(tenant_id)
        if existing is None:
            row = CanvasCredential(
                tenant_id=tenant_id,
                ciphertext=ciphertext,
                key_version=key_version,
            )
            self._session.add(row)
            self._session.flush()
            return row

        existing.ciphertext = ciphertext
        existing.key_version = key_version
        existing.rotated_at = datetime.now(UTC)
        return existing


class TenantService:
    """Dual-mode tenant + credential store.

    - **No session** (default): in-memory dicts. Preserved verbatim
      from PR 2 so the existing unit tests stay green.
    - **With session**: routes lookups through
      :class:`TenantRepository` and persists encrypted Canvas tokens
      in the ``canvas_credentials`` table.

    The two modes never share state; a service instance is bound to
    whichever mode its constructor selected.
    """

    def __init__(
        self,
        ciphers: Mapping[int, TokenCipher] | None = None,
        session: Session | None = None,
        repository: TenantRepository | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._tenants_by_backend_user: dict[str, TenantRecord] = {}
        self._tenants_by_id: dict[uuid.UUID, TenantRecord] = {}
        self._credentials: dict[uuid.UUID, _StoredCredential] = {}
        self._ciphers: dict[int, TokenCipher] = dict(ciphers or {})
        self._session = session
        self._repository = repository or (
            TenantRepository(session) if session is not None else None
        )

    @property
    def session(self) -> Session | None:
        return self._session

    def bind_session(self, session: Session) -> None:
        """Attach a SQLAlchemy session; subsequent calls use the repository."""
        self._session = session
        self._repository = TenantRepository(session)

    def bind_cipher(self, key_version: int, cipher: TokenCipher) -> None:
        """Register a :class:`TokenCipher` for ``key_version``.

        Used by the in-memory path. The session path looks up the
        cipher at decrypt time via :attr:`_ciphers`; controllers
        must call this regardless of mode so PR 5 callers can
        decrypt the freshly-read ciphertext.
        """
        self._ciphers[key_version] = cipher

    def get_or_create_tenant(self, backend_user_id: str) -> TenantRecord:
        """Return the tenant for ``backend_user_id``; create one if missing."""
        if not backend_user_id:
            raise ValueError("backend_user_id is required")
        if self._repository is not None:
            row = self._repository.get_or_create_tenant(backend_user_id)
            return TenantRecord(id=row.id, backend_user_id=row.backend_user_id)

        with self._lock:
            existing = self._tenants_by_backend_user.get(backend_user_id)
            if existing is not None:
                return existing
            record = TenantRecord(
                id=uuid.uuid4(),
                backend_user_id=backend_user_id,
            )
            self._tenants_by_backend_user[backend_user_id] = record
            self._tenants_by_id[record.id] = record
            return record

    def store_canvas_token(
        self,
        tenant_id: uuid.UUID,
        canvas_token: str,
        cipher: TokenCipher,
    ) -> _StoredCredential:
        """Encrypt ``canvas_token`` and store it for ``tenant_id``."""
        envelope = cipher.encrypt(canvas_token)

        if self._repository is not None:
            self._repository.upsert_canvas_credential(
                tenant_id=tenant_id,
                ciphertext=envelope.ciphertext,
                key_version=envelope.key_version,
            )
            stored = _StoredCredential(
                envelope=envelope,
                cipher=cipher,
                created_at=datetime.now(UTC),
            )
            self._ciphers[cipher.key_version] = cipher
            return stored

        if tenant_id not in self._tenants_by_id:
            raise TenantNotFound(f"unknown tenant {tenant_id}")
        stored = _StoredCredential(
            envelope=envelope,
            cipher=cipher,
            created_at=datetime.now(UTC),
        )
        with self._lock:
            self._credentials[tenant_id] = stored
            self._ciphers[cipher.key_version] = cipher
        return stored

    def get_decrypted_canvas_token(self, tenant_id: uuid.UUID) -> str:
        """Return the plaintext Canvas token for ``tenant_id``.

        Session mode: load the ciphertext from ``canvas_credentials``,
        decrypt with the cipher whose ``key_version`` matches. Raises
        :class:`TenantNotFound` when the row is missing or no cipher
        is bound for the recorded version.
        """
        if self._repository is not None:
            row = self._repository.get_canvas_credential(tenant_id)
            if row is None:
                raise TenantNotFound(f"no Canvas credentials for {tenant_id}")
            cipher = self._ciphers.get(row.key_version)
            if cipher is None:
                raise TenantNotFound(
                    f"no cipher bound for key_version={row.key_version}"
                )
            envelope = EncryptedToken(
                ciphertext=row.ciphertext, key_version=row.key_version
            )
            return cipher.decrypt(envelope)

        with self._lock:
            stored = self._credentials.get(tenant_id)
            cipher = self._ciphers.get(stored.envelope.key_version) if stored else None
        if stored is None:
            raise TenantNotFound(f"no Canvas credentials for {tenant_id}")
        if cipher is None:
            raise TenantNotFound(
                f"no cipher bound for key_version={stored.envelope.key_version}"
            )
        return cipher.decrypt(stored.envelope)

    def has_credentials(self, tenant_id: uuid.UUID) -> bool:
        """Return True when ``tenant_id`` has stored credentials."""
        if self._repository is not None:
            return self._repository.get_canvas_credential(tenant_id) is not None
        with self._lock:
            return tenant_id in self._credentials


# --- module-level singleton -------------------------------------------------

_default_service: TenantService | None = None
_default_lock = threading.Lock()


def get_tenant_service() -> TenantService:
    """Return the process-wide :class:`TenantService` (lazy-initialized).

    The default singleton stays session-less so PR 2 unit tests do
    not have to spin up a SQLAlchemy engine. Callers that want the
    session-backed mode construct their own :class:`TenantService`
    inside the request scope with the session yielded by
    :func:`app.core.db.get_db_session`.
    """
    global _default_service
    with _default_lock:
        if _default_service is None:
            _default_service = TenantService()
        return _default_service


def reset_tenant_service() -> None:
    """Reset the singleton (test helper)."""
    global _default_service
    with _default_lock:
        _default_service = None


__all__ = [
    "TenantNotFound",
    "TenantRecord",
    "TenantRepository",
    "TenantService",
    "get_tenant_service",
    "reset_tenant_service",
]