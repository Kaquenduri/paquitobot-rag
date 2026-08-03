"""Sync pipeline utilities (PR 3 + PR 4 seam).

PR 3 introduces the watermark helpers so the sync pipeline (PR 4) can
drive idempotent reads with a single transaction:

- :func:`read_watermark` returns the latest ``last_watermark`` for
  ``(tenant_id, table_name)`` or ``None`` when no row exists yet.

- :func:`advance_watermark` stamps ``last_watermark``, ``last_run_at``
  and ``last_status`` on the matching row, creating it on first use.

The actual *transactional* guarantee — that the watermark advance
shares a transaction with the upserts — is the sync pipeline's
responsibility (PR 4 task 4.4). PR 3 only provides the primitives so
PR 4 can compose them inside ``session.begin()``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SyncState


def read_watermark(
    session: Session,
    tenant_id: str,
    table_name: str,
) -> datetime | None:
    """Return the latest ``last_watermark`` for the tenant + table.

    Returns ``None`` when no ``sync_state`` row exists yet (the very
    first sync run). The value is server-side timezone-aware so the
    sync pipeline can compare against Canvas' ``updated_at`` cursors
    without naive/aware mismatches. SQLite drops tzinfo on round-trip;
    we re-attach UTC so callers always see a tz-aware value.
    """
    row = session.execute(
        select(SyncState).where(
            SyncState.tenant_id == tenant_id,
            SyncState.table_name == table_name,
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    value = row.last_watermark
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def advance_watermark(
    session: Session,
    tenant_id: str,
    table_name: str,
    watermark: datetime,
    *,
    status: str = "ok",
    error_class: str | None = None,
    run_at: datetime | None = None,
) -> SyncState:
    """Stamp ``last_watermark`` / ``last_run_at`` / ``last_status``.

    Creates the ``sync_state`` row on first use so the caller never
    needs to seed it. ``run_at`` defaults to ``datetime.now(UTC)``;
    tests can pass a fixed timestamp for determinism.

    The function does **not** commit — the caller wraps it in the
    sync transaction so the watermark either advances with the
    upserts or rolls back with them (see the "Canvas fails mid-sync"
    scenario in the delta spec).
    """
    if watermark.tzinfo is None:
        # Normalise to UTC so the column round-trips cleanly.
        watermark = watermark.replace(tzinfo=UTC)
    if run_at is not None and run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=UTC)

    row = session.execute(
        select(SyncState).where(
            SyncState.tenant_id == tenant_id,
            SyncState.table_name == table_name,
        )
    ).scalar_one_or_none()

    stamped_run_at = run_at or datetime.now(UTC)

    if row is None:
        row = SyncState(
            tenant_id=tenant_id,
            table_name=table_name,
            last_watermark=watermark,
            last_run_at=stamped_run_at,
            last_status=status,
            last_error_class=error_class,
        )
        session.add(row)
        return row

    row.last_watermark = watermark
    row.last_run_at = stamped_run_at
    row.last_status = status
    row.last_error_class = error_class
    return row


__all__ = ["advance_watermark", "read_watermark"]