"""Repository layer (PR 3).

This package owns the SQLAlchemy-side data access primitives that the
sync pipeline (PR 4) and the text-to-SQL executor (PR 5) build on top
of. PR 3 introduces:

- :func:`upsert_by_canvas_id` (in :mod:`app.repositories.upsert`):
  idempotent insert-or-update keyed on ``(tenant_id, canvas_id)``.
- :class:`CrossTenantFKError` and :func:`assert_tenant_fk_target`:
  reject cross-tenant foreign-key references.
- :func:`soft_delete_if_inactive`: set ``deleted_at`` when a
  ``workflow_state`` flips to an inactive value.

The helpers are deliberately thin and synchronous so they can be
invoked from any SQLAlchemy session (sync or async wrapper). The
sync pipeline (PR 4) drives them inside its own transaction.
"""

from __future__ import annotations

from app.repositories.upsert import (
    INACTIVE_WORKFLOW_STATES,
    CrossTenantFKError,
    assert_tenant_fk_target,
    soft_delete_if_inactive,
    upsert_by_canvas_id,
)

__all__ = [
    "INACTIVE_WORKFLOW_STATES",
    "CrossTenantFKError",
    "assert_tenant_fk_target",
    "soft_delete_if_inactive",
    "upsert_by_canvas_id",
]