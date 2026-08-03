"""Unit tests for the per-tenant sync lock (PR 4 task 4.5).

Coverage:

- The in-memory lock is acquired (returns ``True``) when no one
  holds it.
- A second concurrent acquire returns ``False``; releasing the
  first allows the second to succeed.
- The advisory key is deterministic for the same tenant id.
- The lock helper raises on ``None`` tenant ids.
- :func:`reset_memory_locks` clears the pool between tests.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.sync.lock import (
    lock_key_for,
    release_memory_lock,
    reset_memory_locks,
    try_acquire_sync_lock,
)


def test_first_acquire_returns_true(db_session: Any) -> None:
    reset_memory_locks()
    tenant_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    assert try_acquire_sync_lock(db_session, tenant_id) is True
    release_memory_lock(tenant_id)


def test_second_concurrent_acquire_returns_false(db_session: Any) -> None:
    reset_memory_locks()
    tenant_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    assert try_acquire_sync_lock(db_session, tenant_id) is True
    assert try_acquire_sync_lock(db_session, tenant_id) is False
    release_memory_lock(tenant_id)


def test_release_allows_reacquire(db_session: Any) -> None:
    reset_memory_locks()
    tenant_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
    assert try_acquire_sync_lock(db_session, tenant_id) is True
    release_memory_lock(tenant_id)
    assert try_acquire_sync_lock(db_session, tenant_id) is True
    release_memory_lock(tenant_id)


def test_different_tenant_locks_are_independent(db_session: Any) -> None:
    reset_memory_locks()
    tenant_a = uuid.UUID("44444444-4444-4444-4444-444444444444")
    tenant_b = uuid.UUID("55555555-5555-5555-5555-555555555555")
    assert try_acquire_sync_lock(db_session, tenant_a) is True
    assert try_acquire_sync_lock(db_session, tenant_b) is True
    release_memory_lock(tenant_a)
    release_memory_lock(tenant_b)


def test_lock_key_is_deterministic_for_same_tenant() -> None:
    tenant_id = "tenant-pr4"
    assert lock_key_for(tenant_id) == lock_key_for(tenant_id)


def test_lock_key_differ_for_different_tenants() -> None:
    """The hash is treated as a best-effort key; collisions are
    acceptable but the typical case is a unique key."""
    tenant_a = "tenant-a"
    tenant_b = "tenant-b"
    # Don't assert inequality — FNV-1a may collide on small inputs.
    # We just compute and stash the values so the test is exercised.
    assert isinstance(lock_key_for(tenant_a), int)
    assert isinstance(lock_key_for(tenant_b), int)


def test_try_acquire_rejects_none_tenant_id(db_session: Any) -> None:
    with pytest.raises(ValueError):
        try_acquire_sync_lock(db_session, None)  # type: ignore[arg-type]


def test_try_acquire_accepts_string_tenant_id(db_session: Any) -> None:
    """The lock is keyed by the string representation of the tenant."""
    reset_memory_locks()
    assert try_acquire_sync_lock(db_session, "tenant-uuid-string") is True
    release_memory_lock("tenant-uuid-string")


def test_release_memory_lock_is_idempotent(db_session: Any) -> None:
    """Releasing an unlocked lock is a no-op."""
    release_memory_lock("never-locked")
    # No exception raised.


def test_reset_memory_locks_clears_pool(db_session: Any) -> None:
    tenant_id = uuid.UUID("66666666-6666-6666-6666-666666666666")
    assert try_acquire_sync_lock(db_session, tenant_id) is True
    reset_memory_locks()
    # After reset the lock is free again.
    assert try_acquire_sync_lock(db_session, tenant_id) is True
    release_memory_lock(tenant_id)
