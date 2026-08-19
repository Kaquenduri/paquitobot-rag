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

    def get_or_create_tenant(self, backend_user_id: str) -> uuid.UUID:
        """Return the durable tenant UUID, inserting the row when absent."""
        if not backend_user_id:
            raise ValueError("backend_user_id is required")
        existing = self._session.execute(
            select(Tenant).where(Tenant.backend_user_id == backend_user_id)
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id
        row = Tenant(backend_user_id=backend_user_id)
        self._session.add(row)
        self._session.flush()
        return row.id

    def get_tenant(self, tenant_id: uuid.UUID) -> Tenant | None:
        return self._session.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        ).scalar_one_or_none()

    def get_canvas_credential(self, tenant_id: uuid.UUID) -> CanvasCredential | None:
        return self._session.execute(
            select(CanvasCredential).where(CanvasCredential.tenant_id == tenant_id)
        ).scalar_one_or_none()

    def store_canvas_token(
        self,
        tenant_id: uuid.UUID,
        encrypted_ciphertext: bytes,
        key_version: int,
    ) -> bool:
        """Upsert encrypted Canvas credential material for ``tenant_id``.

        The repository never receives plaintext.  It returns ``False`` only
        when the tenant does not exist; database failures propagate so callers
        cannot mistake a failed write for durable persistence.
        """
        if self.get_tenant(tenant_id) is None:
            return False
        if not isinstance(encrypted_ciphertext, bytes):
            raise TypeError("encrypted_ciphertext must be bytes")
        if not isinstance(key_version, int):
            raise TypeError("key_version must be an int")

        existing = self.get_canvas_credential(tenant_id)
        if existing is None:
            self._session.add(
                CanvasCredential(
                    tenant_id=tenant_id,
                    ciphertext=encrypted_ciphertext,
                    key_version=key_version,
                )
            )
        else:
            existing.ciphertext = encrypted_ciphertext
            existing.key_version = key_version
            existing.rotated_at = datetime.now(UTC)
        self._session.flush()
        return True

    def get_canvas_token(
        self,
        tenant_id: uuid.UUID,
        key_version: int | None = None,
    ) -> bytes | None:
        """Return encrypted ciphertext for a tenant, optionally by key slot."""
        credential = self.get_canvas_credential(tenant_id)
        if credential is None:
            return None
        if key_version is not None and credential.key_version != key_version:
            return None
        return bytes(credential.ciphertext)

    def upsert_canvas_credential(
        self,
        tenant_id: uuid.UUID,
        ciphertext: bytes,
        key_version: int,
    ) -> CanvasCredential:
        """Compatibility wrapper returning the upserted ORM row."""
        if not self.store_canvas_token(tenant_id, ciphertext, key_version):
            raise TenantNotFound(f"unknown tenant {tenant_id}")
        row = self.get_canvas_credential(tenant_id)
        assert row is not None
        return row


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
        self._session = session
        self._repository = repository or (
            TenantRepository(session) if session is not None else None
        )
        # Compatibility contract used by the PR 2/3 tests and standalone
        # router harnesses.  Canonical application requests pass a Session and
        # therefore set this flag to False.
        self._memory_store = self._repository is None

        # The in-memory fallback state is ONLY initialised when the service
        # is constructed without a repository.  When a session-backed
        # repository is present, the four lookup dicts + lock are never
        # allocated; the service is a pure pass-through to the repository and
        # must not touch any in-memory state.
        if self._memory_store:
            self._lock = threading.Lock()
            self._tenants_by_backend_user: dict[str, TenantRecord] = {}
            self._tenants_by_id: dict[uuid.UUID, TenantRecord] = {}
            self._credentials: dict[uuid.UUID, _StoredCredential] = {}
            self._ciphers: dict[int, TokenCipher] = dict(ciphers or {})
        else:
            # Session/repository mode: defer ciphers to first access.
            # ``_initial_ciphers`` keeps the constructor argument available so
            # ``bind_cipher`` / ``get_decrypted_canvas_token`` can materialise
            # the cipher map without ever allocating in-memory state dicts in
            # ``__init__``.
            self._initial_ciphers: dict[int, TokenCipher] = dict(ciphers or {})

    def _cipher_map(self) -> dict[int, TokenCipher]:
        """Return the cipher map, lazy-initialising on first access.

        The ciphers dict is shared configuration used by both modes (the
        in-memory plaintext path writes to it; the repository path reads from
        it during decryption).  Lazy init ensures it does not exist on a
        pure repository-backed service until the controller (or a test)
        explicitly registers a cipher via :meth:`bind_cipher`.
        """
        if not hasattr(self, "_ciphers"):
            self._ciphers = dict(getattr(self, "_initial_ciphers", {}))
        return self._ciphers

    @property
    def session(self) -> Session | None:
        return self._session

    def bind_session(self, session: Session) -> None:
        """Attach a SQLAlchemy session; subsequent calls use the repository."""
        self._session = session
        self._repository = TenantRepository(session)
        self._memory_store = False

    def bind_cipher(self, key_version: int, cipher: TokenCipher) -> None:
        """Register a :class:`TokenCipher` for ``key_version``.

        Used by the in-memory path. The session path looks up the
        cipher at decrypt time via :attr:`_cipher_map`; controllers
        must call this regardless of mode so PR 5 callers can
        decrypt the freshly-read ciphertext.
        """
        self._cipher_map()[key_version] = cipher

    def get_or_create_tenant(self, backend_user_id: str) -> TenantRecord:
        """Return the tenant for ``backend_user_id``; create one if missing."""
        if not backend_user_id:
            raise ValueError("backend_user_id is required")
        if self._repository is not None:
            tenant_id = self._repository.get_or_create_tenant(backend_user_id)
            return TenantRecord(id=tenant_id, backend_user_id=backend_user_id)

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
        canvas_token: str | bytes | None = None,
        cipher: TokenCipher | int | None = None,
        *,
        encrypted_ciphertext: bytes | None = None,
        key_version: int | None = None,
    ) -> _StoredCredential | bool:
        """Store a Canvas token while preserving both service APIs.

        Legacy callers pass ``(plaintext: str, TokenCipher)`` and receive the
        in-memory-compatible :class:`_StoredCredential`.  Persistence wiring
        may instead pass already encrypted bytes plus ``key_version``; this
        path returns the repository's boolean write result and never exposes
        plaintext to the repository.
        """
        if encrypted_ciphertext is not None:
            if canvas_token is not None:
                raise TypeError("pass canvas_token or encrypted_ciphertext, not both")
            canvas_token = encrypted_ciphertext
        if key_version is not None:
            if cipher is not None:
                raise TypeError("pass cipher or key_version, not both")
            cipher = key_version

        if isinstance(canvas_token, bytes):
            if not isinstance(cipher, int):
                raise TypeError("encrypted ciphertext requires an int key_version")
            if self._repository is not None:
                return self._repository.store_canvas_token(
                    tenant_id,
                    canvas_token,
                    cipher,
                )
            if tenant_id not in self._tenants_by_id:
                raise TenantNotFound(f"unknown tenant {tenant_id}")
            bound_cipher = self._ciphers.get(cipher)
            if bound_cipher is None:
                raise TenantNotFound(f"no cipher bound for key_version={cipher}")
            stored = _StoredCredential(
                envelope=EncryptedToken(
                    ciphertext=canvas_token,
                    key_version=cipher,
                ),
                cipher=bound_cipher,
                created_at=datetime.now(UTC),
            )
            with self._lock:
                self._credentials[tenant_id] = stored
            return True

        if not isinstance(canvas_token, str) or not isinstance(cipher, TokenCipher):
            raise TypeError("plaintext storage requires a str token and TokenCipher")
        envelope = cipher.encrypt(canvas_token)

        if self._repository is not None:
            persisted = self._repository.store_canvas_token(
                tenant_id=tenant_id,
                encrypted_ciphertext=envelope.ciphertext,
                key_version=envelope.key_version,
            )
            if not persisted:
                raise TenantNotFound(f"unknown tenant {tenant_id}")
            # Pure delegation: do NOT touch ``_ciphers`` or any in-memory
            # state dicts.  The repository is the sole source of truth for
            # the persisted credential; the next ``get_decrypted_canvas_token``
            # call resolves the cipher via :meth:`_cipher_map` (lazily
            # populated by :meth:`bind_cipher`).
            return True

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

    def get_canvas_token(
        self,
        tenant_id: uuid.UUID,
        key_version: int | None = None,
    ) -> bytes | None:
        """Return stored encrypted bytes without decrypting or logging them."""
        if self._repository is not None:
            return self._repository.get_canvas_token(tenant_id, key_version)
        with self._lock:
            stored = self._credentials.get(tenant_id)
        if stored is None:
            return None
        if key_version is not None and stored.envelope.key_version != key_version:
            return None
        return bytes(stored.envelope.ciphertext)

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
            cipher = self._cipher_map().get(row.key_version)
            if cipher is None:
                raise TenantNotFound(
                    f"no cipher bound for key_version={row.key_version}"
                )
            ciphertext = self._repository.get_canvas_token(
                tenant_id,
                key_version=row.key_version,
            )
            if ciphertext is None:  # pragma: no cover - row changed mid-transaction
                raise TenantNotFound(f"no Canvas credentials for {tenant_id}")
            envelope = EncryptedToken(
                ciphertext=ciphertext,
                key_version=row.key_version,
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

    def get_mock_api_key_prefix(self, tenant_id: uuid.UUID) -> str:
        """Return the registered canvas-mock ``api_key_prefix`` for ``tenant_id``.

        The mock catalog lives in the ``canvas_mock_users`` table; the
        full API key is never persisted (the canvas-mock-api only
        echoes the first 8 characters back per the connector contract
        in ``app.controllers.auth_canvas_mock``). The session must be
        present — the in-memory fallback has no parallel mock store,
        so the lookup raises :class:`TenantNotFound` when no session
        is bound. The 403 mapping is up to the dependency layer.
        """
        if self._session is None:
            raise TenantNotFound(
                f"no canvas-mock key registered for tenant {tenant_id}"
            )
        from sqlalchemy import select

        from app.models import CanvasMockUser

        row = self._session.execute(
            select(CanvasMockUser).where(CanvasMockUser.tenant_id == tenant_id)
        ).scalars().first()
        if row is None or row.api_key_prefix is None:
            raise TenantNotFound(
                f"no canvas-mock key registered for tenant {tenant_id}"
            )
        return row.api_key_prefix


# --- application persistence mode -------------------------------------------

SESSION_STORE_STATE_FLAG = "use_sqlalchemy_tenant_store"


def should_use_session_store(app_state: object, session: Session) -> bool:
    """Select SQL persistence for canonical apps and explicit SQLite harnesses.

    Standalone routers created by the original PR 2/3 tests have no canonical
    app-state flag and retain the legacy in-memory service.  ``app.main`` sets
    the flag for production, while SQLite sessions opt in automatically for
    offline integration self-tests.
    """
    if bool(getattr(app_state, SESSION_STORE_STATE_FLAG, False)):
        return True
    bind = session.get_bind()
    return bind is not None and bind.dialect.name == "sqlite"


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
    "SESSION_STORE_STATE_FLAG",
    "TenantNotFound",
    "TenantRecord",
    "TenantRepository",
    "TenantService",
    "get_tenant_service",
    "reset_tenant_service",
    "should_use_session_store",
]


def _selftest() -> None:
    """Exercise repository upsert semantics against an offline SQLite store."""
    from app.core.db import engine_for_url, session_factory_for
    from app.models import Base

    memory_service = TenantService()
    assert memory_service._memory_store is True
    assert memory_service.get_or_create_tenant("memory-selftest").backend_user_id == (
        "memory-selftest"
    )

    engine = engine_for_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = session_factory_for(engine)
    try:
        with factory() as session:
            repository = TenantRepository(session)
            tenant_id = repository.get_or_create_tenant("repository-selftest")
            assert isinstance(tenant_id, uuid.UUID)
            assert repository.get_or_create_tenant("repository-selftest") == tenant_id
            assert repository.store_canvas_token(tenant_id, b"ciphertext-v1", 1)
            assert repository.get_canvas_token(tenant_id) == b"ciphertext-v1"
            assert repository.get_canvas_token(tenant_id, key_version=2) is None
            assert repository.store_canvas_token(tenant_id, b"ciphertext-v2", 2)
            assert repository.get_canvas_token(tenant_id, key_version=2) == b"ciphertext-v2"
            session.commit()

        with factory() as verification_session:
            persisted = TenantRepository(verification_session)
            assert persisted.get_canvas_token(tenant_id, key_version=2) == b"ciphertext-v2"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    _selftest()
