"""Repository primitives for tenant-scoped upserts and soft-delete.

These helpers are the seam between the sync pipeline (PR 4) and the
SQLAlchemy ORM. Every domain row inherits :class:`TenantMixin` so the
cross-tenant FK check has a stable ``tenant_id`` column to compare on.

Contract:

- :func:`upsert_by_canvas_id` looks up the row by ``(tenant_id,
  canvas_id)`` and either updates the listed columns in place or
  creates a fresh row. The ``payload`` dict is filtered to known
  columns so unknown keys (e.g. a peer-identifying field) are dropped
  before they reach the database.

- :func:`assert_tenant_fk_target` is the explicit cross-tenant FK
  guard. Callers that want to set ``course_id`` / ``user_id`` /
  ``assignment_id`` look up the target row first and reject any row
  whose ``tenant_id`` differs from the source ``tenant_id``.

- :func:`soft_delete_if_inactive` flips ``deleted_at`` to the current
  timestamp when ``workflow_state`` is in
  :data:`INACTIVE_WORKFLOW_STATES`. Otherwise it returns ``False``
  without mutating the row, so the sync pipeline can keep the prior
  state when Canvas reports an "active" state.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Base

# Workflow-state values that mark a record as no longer queryable. The
# sync pipeline treats these as "soft-delete the row". The set is
# deliberately small and conservative; production may extend it per
# model (the helper accepts a custom set via ``inactive_states``).
INACTIVE_WORKFLOW_STATES: frozenset[str] = frozenset(
    {
        "deleted",
        "inactive",
        "completed",
        "concluded",
        "unpublished",
    }
)


class CrossTenantFKError(Exception):
    """Raised when an FK target row belongs to a different tenant.

    Carries the model name, the source ``tenant_id`` and the target's
    ``canvas_id`` so logs and 409 responses can describe the
    misconfiguration without echoing the target's ``tenant_id`` (a
    defense-in-depth precaution against log leakage of tenant
    identifiers).
    """

    def __init__(
        self,
        *,
        source_model: type[Base],
        target_model: type[Base],
        tenant_id: uuid.UUID,
        canvas_id: int,
        reason: str,
    ) -> None:
        self.source_model = source_model
        self.target_model = target_model
        self.tenant_id = tenant_id
        self.canvas_id = canvas_id
        self.reason = reason
        super().__init__(
            f"cross-tenant FK rejected: {source_model.__name__}->{target_model.__name__} "
            f"canvas_id={canvas_id}: {reason}"
        )


def _known_columns(model: type[Base]) -> set[str]:
    """Return the set of column names declared on ``model``."""
    return {column.key for column in model.__table__.columns}


def upsert_by_canvas_id(
    session: Session,
    model: type[Base],
    tenant_id: uuid.UUID,
    canvas_id: int,
    payload: dict[str, Any],
) -> Base:
    """Insert-or-update a row keyed on ``(tenant_id, canvas_id)``.

    ``payload`` keys that are not declared columns of ``model`` are
    silently dropped (Canvas DTOs may carry peer fields that the model
    intentionally ignores — see the spec scenario
    "Canvas payload includes a classmate"). The returned instance is
    attached to ``session``; the caller decides when to commit.

    Behaviour:

    - If a row with ``(tenant_id, canvas_id)`` exists, update each
      ``payload`` key that maps to a known column. ``tenant_id`` and
      ``canvas_id`` are always re-asserted on the row.
    - Otherwise, create a new row with the filtered payload plus
      ``tenant_id`` and ``canvas_id``; ``deleted_at`` stays ``None``.
    """
    if tenant_id is None or canvas_id is None:
        raise ValueError("upsert_by_canvas_id requires tenant_id and canvas_id")

    columns = _known_columns(model)
    # ``id`` is server-assigned; ``created_at`` / ``updated_at`` are
    # server-stamped. ``tenant_id`` / ``canvas_id`` come from the
    # repository arguments, not the payload.
    filtered: dict[str, Any] = {
        key: value
        for key, value in payload.items()
        if key in columns and key not in {"id", "created_at", "updated_at"}
    }

    existing = session.execute(
        select(model).where(
            model.tenant_id == tenant_id,
            model.canvas_id == canvas_id,
        )
    ).scalar_one_or_none()

    if existing is not None:
        for key, value in filtered.items():
            setattr(existing, key, value)
        existing.tenant_id = tenant_id
        existing.canvas_id = canvas_id
        return existing

    instance = model(tenant_id=tenant_id, canvas_id=canvas_id, **filtered)
    session.add(instance)
    return instance


def assert_tenant_fk_target(
    session: Session,
    *,
    source_model: type[Base],
    target_model: type[Base],
    tenant_id: uuid.UUID,
    target_canvas_id: int,
) -> Base:
    """Look up a target row and reject if it belongs to a different tenant.

    Returns the matched row so callers can set the FK without an
    extra round-trip. Raises :class:`CrossTenantFKError` when:

    - The target row does not exist.
    - The target row's ``tenant_id`` does not match ``tenant_id``.
    - The target model does not declare ``canvas_id`` (i.e. is not
      Canvas-natural-keyed; cross-tenant checks are meaningless for
      tables like ``tenants`` or ``sync_state``).
    """
    if "canvas_id" not in _known_columns(target_model):
        raise CrossTenantFKError(
            source_model=source_model,
            target_model=target_model,
            tenant_id=tenant_id,
            canvas_id=target_canvas_id,
            reason="target model has no canvas_id",
        )

    target = session.execute(
        select(target_model).where(
            target_model.canvas_id == target_canvas_id,
        )
    ).scalar_one_or_none()

    if target is None:
        raise CrossTenantFKError(
            source_model=source_model,
            target_model=target_model,
            tenant_id=tenant_id,
            canvas_id=target_canvas_id,
            reason="target row not found",
        )

    if getattr(target, "tenant_id", None) != tenant_id:
        raise CrossTenantFKError(
            source_model=source_model,
            target_model=target_model,
            tenant_id=tenant_id,
            canvas_id=target_canvas_id,
            reason="target tenant_id mismatch",
        )

    return target


def _resolve_inactive_states(
    inactive_states: Iterable[str] | None,
) -> frozenset[str]:
    if inactive_states is None:
        return INACTIVE_WORKFLOW_STATES
    return frozenset(state.lower() for state in inactive_states)


def soft_delete_if_inactive(
    session: Session,
    model: type[Base],
    tenant_id: uuid.UUID,
    canvas_id: int,
    workflow_state: str | None,
    *,
    inactive_states: Iterable[str] | None = None,
    now: datetime | None = None,
) -> bool:
    """Soft-delete the row when ``workflow_state`` is in the inactive set.

    Returns ``True`` when the row was mutated, ``False`` otherwise
    (no row found, row already soft-deleted, or state is still
    active). The function never raises for a missing row — the sync
    pipeline calls it speculatively against rows that may not have
    been upserted yet.

    The ``deleted_at`` value is timestamped with the supplied ``now``
    (default: UTC now) so the timestamp is reproducible in tests.
    """
    inactive = _resolve_inactive_states(inactive_states)
    if workflow_state is None:
        return False
    if workflow_state.lower() not in inactive:
        return False

    row = session.execute(
        select(model).where(
            model.tenant_id == tenant_id,
            model.canvas_id == canvas_id,
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    if getattr(row, "deleted_at", None) is not None:
        return False

    row.deleted_at = now or datetime.now(UTC)
    return True


__all__ = [
    "INACTIVE_WORKFLOW_STATES",
    "CrossTenantFKError",
    "assert_tenant_fk_target",
    "soft_delete_if_inactive",
    "upsert_by_canvas_id",
]