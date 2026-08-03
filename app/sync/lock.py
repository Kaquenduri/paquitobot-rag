"""Per-tenant, non-blocking synchronization locks.

PostgreSQL owns the production lock.  The lock is deliberately transaction
scoped so a successful sync cannot release its exclusion before its watermark
and rows are committed::

    SELECT pg_try_advisory_xact_lock(hashtext(:tenant_id)::bigint)

SQLite has no advisory-lock implementation, therefore local tests use a
process-wide ``threading.Lock`` keyed by the tenant.  SQLAlchemy's
``after_transaction_end`` hook mirrors transaction-scoped release for real
sessions; :func:`release_sync_lock` remains an explicit seam for lightweight
stubs.  PostgreSQL releases the advisory lock automatically on commit or
rollback.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.core.logging import get_logger

logger = get_logger("app.sync.lock")

TenantKey = uuid.UUID | str

# Keep the SQL as a named constant.  Apart from making the intent auditable,
# this gives integration tests a stable seam for asserting the exact
# transaction-scoped PostgreSQL primitive.
ADVISORY_LOCK_SQL = (
    "SELECT pg_try_advisory_xact_lock(hashtext(:tenant_id)::bigint)"
)

_pool_guard = threading.Lock()
_tenant_locks: dict[str, threading.Lock] = {}
_SESSION_LOCKS_KEY = "_canvas_sync_fallback_locks"


def _remember_session_lock(session: Session, tenant_id: TenantKey) -> None:
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        info.setdefault(_SESSION_LOCKS_KEY, set()).add(_tenant_key(tenant_id))


def _forget_session_lock(session: Session, tenant_id: TenantKey) -> None:
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return
    held = info.get(_SESSION_LOCKS_KEY)
    if isinstance(held, set):
        held.discard(_tenant_key(tenant_id))
        if not held:
            info.pop(_SESSION_LOCKS_KEY, None)


@event.listens_for(Session, "after_transaction_end")
def _release_session_fallback_locks(
    session: Session,
    transaction: Any,
) -> None:
    """Mirror PostgreSQL tx-scoped release for SQLite sessions."""
    if getattr(transaction, "parent", None) is not None:
        return
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return
    held = info.pop(_SESSION_LOCKS_KEY, set())
    if isinstance(held, set):
        for tenant_id in held:
            release_memory_lock(tenant_id)


def _tenant_key(tenant_id: TenantKey) -> str:
    if tenant_id is None:  # type: ignore[comparison-overlap]
        raise ValueError("tenant_id is required")
    value = str(tenant_id).strip()
    if not value:
        raise ValueError("tenant_id is required")
    return value


def _memory_lock_for(tenant_id: TenantKey) -> threading.Lock:
    """Return (and lazily create) the process-local lock for a tenant."""
    key = _tenant_key(tenant_id)
    with _pool_guard:
        lock = _tenant_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _tenant_locks[key] = lock
        return lock


def reset_memory_locks() -> None:
    """Clear all local locks.

    This is intentionally a test/helper seam.  Production PostgreSQL locks do
    not use this registry and are released by the database transaction.
    """
    with _pool_guard:
        _tenant_locks.clear()


def release_memory_lock(tenant_id: TenantKey) -> None:
    """Release a local tenant lock, if this process currently holds it."""
    key = _tenant_key(tenant_id)
    with _pool_guard:
        lock = _tenant_locks.get(key)
    if lock is not None and lock.locked():
        # ``threading.Lock`` does not expose ownership.  The helper is only
        # used by the synchronous transaction owner, so a guarded release is
        # the least surprising and idempotent test seam.
        try:
            lock.release()
        except RuntimeError:
            # Another thread released it between ``locked`` and ``release``.
            # Treat that race as an already-released lock.
            pass


def _dialect_name(session: Session) -> str:
    """Return the bound SQLAlchemy dialect name without starting a query."""
    try:
        bind: Any = session.get_bind()
    except AttributeError:  # pragma: no cover - minimal test double
        bind = getattr(session, "bind", None)
    return str(getattr(getattr(bind, "dialect", None), "name", "")).lower()


def _is_sqlite(session: Session) -> bool:
    return _dialect_name(session) == "sqlite"


def _try_acquire_sqlite(session: Session, tenant_id: TenantKey) -> bool:
    acquired = _memory_lock_for(tenant_id).acquire(blocking=False)
    if acquired:
        _remember_session_lock(session, tenant_id)
    return acquired


def _try_acquire_postgres(session: Session, tenant_id: TenantKey) -> bool:
    """Execute the transaction-scoped PostgreSQL advisory lock."""
    result = session.execute(
        text(ADVISORY_LOCK_SQL),
        {"tenant_id": _tenant_key(tenant_id)},
    )
    if hasattr(result, "scalar"):
        value = result.scalar()
    elif hasattr(result, "first"):  # lightweight fake used by self-tests
        row = result.first()
        value = row[0] if row else False
    else:  # pragma: no cover - malformed DB adapter
        value = False
    return bool(value)


def try_acquire_sync_lock(session: Session, tenant_id: TenantKey) -> bool:
    """Try to acquire the per-tenant synchronization lock.

    The function never waits.  On PostgreSQL it MUST be called while a
    transaction is active because ``pg_try_advisory_xact_lock`` is scoped to
    that transaction.  SQLite and other local dialects use the per-tenant
    in-process fallback.
    """
    key = _tenant_key(tenant_id)
    dialect = _dialect_name(session)
    if dialect == "postgresql":
        return _try_acquire_postgres(session, key)
    # SQLite is the supported test dialect.  Falling back for another local
    # dialect keeps development fixtures safe without pretending to provide a
    # distributed lock outside PostgreSQL.
    return _try_acquire_sqlite(session, key)


def release_sync_lock(session: Session, tenant_id: TenantKey) -> None:
    """Release the local fallback; PostgreSQL is released by tx end."""
    if _dialect_name(session) != "postgresql":
        _forget_session_lock(session, tenant_id)
        release_memory_lock(tenant_id)


def lock_key_for(tenant_id: TenantKey) -> int:
    """Return a stable diagnostic 32-bit key for local assertions.

    PostgreSQL computes the authoritative key with ``hashtext``.  This helper
    is deliberately only a deterministic diagnostic seam; callers MUST NOT
    substitute it for the SQL expression above.
    """
    value = _tenant_key(tenant_id).encode("utf-8")
    hashed = 0x811C9DC5
    for byte in value:
        hashed ^= byte
        hashed = (hashed * 0x01000193) & 0xFFFFFFFF
    return hashed - 0x100000000 if hashed >= 0x80000000 else hashed


__all__ = [
    "ADVISORY_LOCK_SQL",
    "lock_key_for",
    "release_memory_lock",
    "release_sync_lock",
    "reset_memory_locks",
    "try_acquire_sync_lock",
]


def _selftest() -> None:
    """Run local lock invariants without a database or network."""

    class _Dialect:
        name = "sqlite"

    class _Session:
        def get_bind(self) -> Any:
            return type("_Bind", (), {"dialect": _Dialect()})()

    tenant = "selftest-tenant"
    reset_memory_locks()
    session = _Session()
    assert try_acquire_sync_lock(session, tenant) is True
    assert try_acquire_sync_lock(session, tenant) is False
    release_sync_lock(session, tenant)
    assert try_acquire_sync_lock(session, tenant) is True
    release_sync_lock(session, tenant)
    assert "pg_try_advisory_xact_lock" in ADVISORY_LOCK_SQL
    assert ":tenant_id" in ADVISORY_LOCK_SQL
    reset_memory_locks()


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    _selftest()
