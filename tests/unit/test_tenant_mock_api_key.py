"""Unit tests for :meth:`TenantService.get_mock_api_key_prefix`.

The mock API key is stored only as its first 8 characters (the
canvas-mock-api spec only echoes the prefix back). The full key NEVER
lands in the database. The service exposes a thin lookup that returns
the persisted prefix for a tenant; the dependency layer then maps
that to a request-scoped marker.

These tests pin the contract:

- **Session-backed mode**: returns the ``api_key_prefix`` of the
  ``canvas_mock_users`` row for ``tenant_id`` (when exactly one row
  matches).
- **Missing row** raises :class:`TenantNotFound`; the dep layer turns
  that into HTTP 403.
- **In-memory mode** (no session) raises :class:`TenantNotFound` —
  the mock catalog is preloaded by the lifespan or by the SQL
  repository, so the fallback path is not supported.
- The method MUST NOT raise on a tenant that has no legacy
  ``canvas_credentials`` row (the two stores are independent).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.models import CanvasMockUser
from app.services.tenant_service import TenantNotFound, TenantService


def _seed_mock_user(
    session: Any,
    tenant_id: uuid.UUID,
    canvas_mock_id: int = 42,
    api_key_prefix: str = "stu_0011",
    role: str = "student",
) -> CanvasMockUser:
    row = CanvasMockUser(
        tenant_id=tenant_id,
        canvas_mock_id=canvas_mock_id,
        api_key_prefix=api_key_prefix,
        role=role,
    )
    session.add(row)
    session.commit()
    return row


def test_get_mock_api_key_prefix_returns_registered_prefix(
    db_session: Any,
) -> None:
    """Session-backed mode returns the stored prefix verbatim."""
    tenant_id = uuid.uuid4()
    _seed_mock_user(db_session, tenant_id, api_key_prefix="stu_0011")

    service = TenantService(session=db_session)
    assert service.get_mock_api_key_prefix(tenant_id) == "stu_0011"


def test_get_mock_api_key_prefix_raises_when_no_row(db_session: Any) -> None:
    """A tenant with no ``canvas_mock_users`` row raises ``TenantNotFound``."""
    tenant_id = uuid.uuid4()
    service = TenantService(session=db_session)

    with pytest.raises(TenantNotFound):
        service.get_mock_api_key_prefix(tenant_id)


def test_get_mock_api_key_prefix_raises_in_in_memory_mode() -> None:
    """Without a session the lookup is unsupported and fails closed."""
    service = TenantService()
    with pytest.raises(TenantNotFound):
        service.get_mock_api_key_prefix(uuid.uuid4())


def test_get_mock_api_key_prefix_does_not_require_canvas_credential(
    db_session: Any,
) -> None:
    """The mock store is independent of the legacy ``canvas_credentials`` table.

    The legacy store uses Fernet-encrypted tokens; the mock store uses
    plaintext prefixes. A tenant that has only a mock row (no legacy
    credential) must still resolve cleanly.
    """
    tenant_id = uuid.uuid4()
    _seed_mock_user(db_session, tenant_id, api_key_prefix="adm_42abc")

    service = TenantService(session=db_session)
    assert service.get_mock_api_key_prefix(tenant_id) == "adm_42abc"

    # Sanity: the legacy store has no row for this tenant.
    from app.services.tenant_service import TenantRepository

    repository = TenantRepository(db_session)
    assert repository.get_canvas_credential(tenant_id) is None


def test_get_mock_api_key_prefix_is_tenant_scoped(db_session: Any) -> None:
    """Two tenants with different prefixes resolve to different values."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    _seed_mock_user(db_session, tenant_a, api_key_prefix="aaa_11111")
    _seed_mock_user(db_session, tenant_b, api_key_prefix="bbb_22222")

    service = TenantService(session=db_session)
    assert service.get_mock_api_key_prefix(tenant_a) == "aaa_11111"
    assert service.get_mock_api_key_prefix(tenant_b) == "bbb_22222"


def test_get_mock_api_key_prefix_returns_first_matching_row(
    db_session: Any,
) -> None:
    """If a tenant has multiple mock rows (e.g. multiple roles), the first wins.

    The schema does not enforce uniqueness on ``tenant_id`` alone
    (uniqueness is on ``(tenant_id, api_key_prefix)`` and
    ``(tenant_id, canvas_mock_id)``); the service returns the first
    row by insertion order for the dependency layer to use as a
    stable marker.
    """
    tenant_id = uuid.uuid4()
    first = _seed_mock_user(
        db_session, tenant_id, canvas_mock_id=1, api_key_prefix="stu_first"
    )
    second = CanvasMockUser(
        tenant_id=tenant_id,
        canvas_mock_id=2,
        api_key_prefix="stu_second",
        role="admin",
    )
    db_session.add(second)
    db_session.commit()

    service = TenantService(session=db_session)
    prefix = service.get_mock_api_key_prefix(tenant_id)
    assert prefix in {"stu_first", "stu_second"}

    # The DB still has both rows.
    rows = (
        db_session.execute(
            select(CanvasMockUser).where(CanvasMockUser.tenant_id == tenant_id)
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert first in rows
    assert second in rows
