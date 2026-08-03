"""Public synchronization package surface.

The package keeps the transaction primitives together: locks, watermarks,
the tenant pipeline, and the optional in-process scheduler.  Importing from
here gives controllers and workers one stable seam without exposing Canvas
credentials or request payloads.
"""

from __future__ import annotations

from app.sync.lock import (
    ADVISORY_LOCK_SQL,
    lock_key_for,
    release_memory_lock,
    release_sync_lock,
    reset_memory_locks,
    try_acquire_sync_lock,
)
from app.sync.pipeline import (
    ERROR_AUTH_REJECTED,
    ERROR_CANVAS_UNAVAILABLE,
    ERROR_SCHEMA_DRIFT,
    ERROR_UNEXPECTED,
    SYNC_STATE_TABLE,
    CanvasSchemaDriftError,
    SyncResult,
    sync_tenant,
)
from app.sync.scheduler import (
    SyncScheduler,
    apply_jitter,
    jitter_seconds,
)
from app.sync.watermark import advance_watermark, read_watermark

__all__ = [
    "ADVISORY_LOCK_SQL",
    "ERROR_AUTH_REJECTED",
    "ERROR_CANVAS_UNAVAILABLE",
    "ERROR_SCHEMA_DRIFT",
    "ERROR_UNEXPECTED",
    "SYNC_STATE_TABLE",
    "CanvasSchemaDriftError",
    "SyncResult",
    "SyncScheduler",
    "advance_watermark",
    "apply_jitter",
    "jitter_seconds",
    "lock_key_for",
    "read_watermark",
    "release_memory_lock",
    "release_sync_lock",
    "reset_memory_locks",
    "sync_tenant",
    "try_acquire_sync_lock",
]


def _selftest() -> None:
    """Verify that the package exports its transaction seams."""
    assert callable(sync_tenant)
    assert callable(try_acquire_sync_lock)
    assert callable(read_watermark)
    assert "hashtext(:tenant_id)" in ADVISORY_LOCK_SQL
    assert SYNC_STATE_TABLE == "users"


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    _selftest()
