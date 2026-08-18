"""Regression tests for the ``user_id_mock`` server-slot auto-injection.

The five mock agent tools (``get_user_mock_courses``,
``get_user_mock_grades``, ``get_user_mock_course_grades``,
``get_user_missing_mock_assignments``, ``get_user_attendance``) declare
``user_id_mock`` in their ``server_slots``. Before this fix, calling
``_TenantToolRuntime._run_template`` directly for one of those tools
raised ``TemplateNotAllowed("invalid SQL template slots")`` because the
runtime only injected ``tenant_id`` plus the caller-supplied ``extra_slots``.

The fix lives in :meth:`_TenantToolRuntime._run_template`: it now looks up
the tool's ``server_slots`` and resolves any missing server slot from the
existing cached getters (``self_user_id`` / ``self_mock_user_id``) before
asking ``ALLOW_LIST`` to render the template.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models import (
    CanvasMockCourse,
    CanvasMockEnrollment,
    CanvasMockUser,
)
from app.services.rag_factory import SelfUserUnresolved, _TenantToolRuntime
from app.text_to_sql.executor import SQLExecutionError
from app.text_to_sql.tools import TOOL_CATALOG


def _runtime_with_session(session: Session, tenant_id: uuid.UUID) -> _TenantToolRuntime:
    return _TenantToolRuntime(session, tenant_id)


# ---------------------------------------------------------------------------
# RED repro: the parametrized cases directly called _run_template with a
# tool that needs user_id_mock. Before the fix this raised
# ``invalid SQL template slots``.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    [
        "get_user_mock_courses",
        "get_user_mock_grades",
        "get_user_attendance",
    ],
)
def test_run_template_injects_user_id_mock_for_user_scoped_mock_tools(
    db_session: Any, tool_name: str
) -> None:
    tenant = uuid.uuid4()
    db_session.add(CanvasMockUser(tenant_id=tenant, canvas_mock_id=7, role="student"))
    db_session.commit()

    runtime = _runtime_with_session(db_session, tenant)
    rows = runtime._run_template(tool_name, {})
    assert isinstance(rows, list)


def test_run_template_resolves_template_for_get_user_missing_mock_assignments(
    db_session: Any,
) -> None:
    """``get_user_missing_mock_assignments`` uses Postgres-only ``now()``,
    so SQLite raises at execution. The fix under test is about template
    resolution — assert the root cause is NOT the slot-mismatch error."""
    tenant = uuid.uuid4()
    db_session.add(CanvasMockUser(tenant_id=tenant, canvas_mock_id=7, role="student"))
    db_session.commit()

    runtime = _runtime_with_session(db_session, tenant)
    with pytest.raises(SQLExecutionError) as info:
        runtime._run_template("get_user_missing_mock_assignments", {})
    msg = str(info.value.__cause__)
    assert "invalid SQL template slots" not in msg


def test_run_template_returns_courses_for_seeded_tenant(db_session: Any) -> None:
    tenant = uuid.uuid4()
    db_session.add(CanvasMockUser(tenant_id=tenant, canvas_mock_id=7, role="student"))
    db_session.add(
        CanvasMockCourse(
            tenant_id=tenant, canvas_mock_id=101, name="Cálculo", course_code="CALC"
        )
    )
    db_session.add(
        CanvasMockEnrollment(
            tenant_id=tenant,
            canvas_mock_id=9001,
            user_canvas_mock_id=7,
            course_canvas_mock_id=101,
        )
    )
    db_session.commit()

    runtime = _runtime_with_session(db_session, tenant)
    rows = runtime._run_template("get_user_mock_courses", {})
    assert len(rows) == 1
    assert rows[0]["name"] == "Cálculo"


def test_run_template_with_missing_mock_user_raises_self_user_unresolved(
    db_session: Any,
) -> None:
    """Without a mock user row the runtime raises ``SelfUserUnresolved``
    — never the cryptic ``invalid SQL template slots`` from the allow list."""
    runtime = _runtime_with_session(db_session, uuid.uuid4())
    with pytest.raises(SelfUserUnresolved):
        runtime._run_template("get_user_mock_courses", {})


def test_run_template_still_works_for_tenant_only_internal_template(
    db_session: Any,
) -> None:
    """Internal templates like ``courses_list`` are NOT in ``TOOL_CATALOG``
    and only need ``tenant_id``; the auto-injection path must be a no-op."""
    runtime = _runtime_with_session(db_session, uuid.uuid4())
    assert runtime._run_template("courses_list", {}) == []


def test_execute_path_binds_user_id_mock_for_user_scoped_mock_tools(
    db_session: Any,
) -> None:
    """The agent-facing ``execute()`` path still works: ``user_id_mock`` is
    resolved and passed to the executor as a bind parameter."""
    tenant = uuid.uuid4()
    db_session.add(CanvasMockUser(tenant_id=tenant, canvas_mock_id=42, role="student"))
    db_session.commit()

    from app.services import rag_factory as rf

    original = rf.execute_readonly
    final: dict[str, Any] = {}

    def _capture(session, sql, *, tenant_id, params=None, row_limit=200):
        # Pass through the self-id lookup; intercept only the final call.
        if "FROM canvas_mock_users" in sql and "ORDER BY created_at" in sql:
            return [{"canvas_mock_id": 42}]
        final["params"] = dict(params or {})
        final["sql"] = sql
        return []

    rf.execute_readonly = _capture
    try:
        runtime = _runtime_with_session(db_session, tenant)
        runtime.execute(TOOL_CATALOG["get_user_mock_courses"], {})
    finally:
        rf.execute_readonly = original

    assert final["params"].get("user_id_mock") == 42
    assert ":user_id_mock" in final["sql"]