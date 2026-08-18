"""Tests for the canvas-mock wire-up in app.services.rag_factory (PR 6 task 6.2).

The runtime's ``mock_extractor_factory`` hook is the seam that lets the
RAG service bootstrap fresh mock data on demand. The mock tools
executed by the runtime MUST return only the rows belonging to the
authenticated tenant — a cross-tenant mock id MUST NOT leak.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models import (
    CanvasMockCourse,
    CanvasMockUser,
)
from app.services.rag_factory import _TenantToolRuntime
from app.text_to_sql.tools import TOOL_CATALOG


def _runtime_with_session(session: Session, tenant_id: uuid.UUID) -> _TenantToolRuntime:
    return _TenantToolRuntime(session, tenant_id)


def test_self_mock_user_id_resolves_to_int(db_session: Any) -> None:
    """The mock self-user resolver returns the seeded int."""
    tenant = uuid.uuid4()
    db_session.add(
        CanvasMockUser(
            tenant_id=tenant,
            canvas_mock_id=42,
            role="student",
        )
    )
    db_session.commit()
    runtime = _runtime_with_session(db_session, tenant)
    assert runtime.self_mock_user_id() == 42


def test_mock_tool_returns_only_tenant_rows(db_session: Any) -> None:
    """Two tenants with the same mock id never bleed into each other."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    db_session.add_all(
        [
            CanvasMockCourse(
                tenant_id=tenant_a,
                canvas_mock_id=101,
                name="Cálculo A",
                course_code="CALC-A",
            ),
            CanvasMockCourse(
                tenant_id=tenant_b,
                canvas_mock_id=101,
                name="Cálculo B",
                course_code="CALC-B",
            ),
        ]
    )
    db_session.commit()

    runtime_a = _runtime_with_session(db_session, tenant_a)
    rows = runtime_a.execute(TOOL_CATALOG["get_mock_course_details"], {"course_id_mock": 101})
    assert len(rows) == 1
    assert rows[0]["name"] == "Cálculo A"


def test_mock_user_id_unresolved_raises_self_user_unresolved(db_session: Any) -> None:
    """A tenant with no mock user row raises ``SelfUserUnresolved``."""
    from app.services.rag_factory import SelfUserUnresolved

    orphan = _tenant_with_no_user(db_session)
    with pytest.raises(SelfUserUnresolved):
        orphan.execute(TOOL_CATALOG["get_user_mock_grades"], {})


def _tenant_with_no_user(db_session: Any) -> _TenantToolRuntime:
    return _runtime_with_session(db_session, uuid.uuid4())
