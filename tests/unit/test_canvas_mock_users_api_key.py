"""Unit tests for the api_key_prefix column on CanvasMockUser (PR 1 task 1.7).

These tests pin the contract for the out-of-band ``api_key_prefix``
correlation: the mock only echoes the first 8 characters of the API
key back to the client (REQ-AUTH-2 in the canvas-mock-api spec), so
the column is nullable and unique per tenant. A request that wants
to correlate an inbound webhook with its registered subscription
can scan the prefix column without ever seeing the full secret.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import CanvasMockUser


def test_api_key_prefix_column_is_present() -> None:
    """The column is declared on the model."""
    assert hasattr(CanvasMockUser, "api_key_prefix")
    column = CanvasMockUser.__table__.columns["api_key_prefix"]
    assert column.nullable is True
    assert column.type.length == 16


def test_api_key_prefix_is_unique_per_tenant(db_session: Any) -> None:
    """UQ(tenant_id, api_key_prefix) — two tenants MAY share a prefix."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    db_session.add_all(
        [
            CanvasMockUser(
                tenant_id=tenant_a,
                canvas_mock_id=1,
                api_key_prefix="abcd1234",
            ),
            CanvasMockUser(
                tenant_id=tenant_b,
                canvas_mock_id=1,
                api_key_prefix="abcd1234",  # same prefix, different tenant
            ),
        ]
    )
    db_session.flush()

    # Same tenant + same prefix MUST collide.
    db_session.add(
        CanvasMockUser(
            tenant_id=tenant_a,
            canvas_mock_id=2,
            api_key_prefix="abcd1234",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_api_key_prefix_is_nullable(db_session: Any) -> None:
    """A user row may omit the prefix — the mock only echoes it on auth."""
    tenant = uuid.uuid4()
    db_session.add(
        CanvasMockUser(
            tenant_id=tenant,
            canvas_mock_id=99,
            api_key_prefix=None,
        )
    )
    db_session.flush()
    fetched = db_session.query(CanvasMockUser).one()
    assert fetched.api_key_prefix is None
