"""Unit tests for the SQL allow-list (PR 2 task 2.4 + PR 3 task 3.3).

PR 2 retires the legacy ``_register_agent_tools`` block by commenting
it out with a ``# DEPRECATED:`` header; the nine mock templates are
registered by ``_register_mock_agent_tools`` instead. The legacy
tests that exercised the legacy templates are skipped (see
``tests/unit/test_sql_agent.py``); the new tests below assert:

1. The legacy registration block is documented as deprecated but does
   not produce any live templates (``test_register_agent_tools_legacy_disabled``).
2. The nine mock templates exist (this is RED in PR 2 → GREEN once
   PR 3 registers the full SQL).
3. The two mock grounding templates (``mock_courses_list``,
   ``mock_assignments_list``) exist and render as proper SQL.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.text_to_sql.allow_list import ALLOW_LIST

MOCK_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "get_user_mock_courses",
        "get_mock_course_details",
        "get_mock_course_assignments",
        "get_mock_assignment_details",
        "get_user_mock_grades",
        "get_user_mock_course_grades",
        "get_user_missing_mock_assignments",
        "get_user_attendance",
        "get_mock_class_sessions",
    }
)

LEGACY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "get_user_profile",
        "get_user_courses",
        "get_course_assignments",
        "get_assignment_details",
        "get_user_course_submissions",
        "get_user_missing_submissions",
        "get_user_late_submissions",
        "get_course_details",
        "get_user_courses_current_term",
    }
)


def _allow_list_source() -> str:
    return Path(__file__).resolve().parents[2].joinpath(
        "app", "text_to_sql", "allow_list.py"
    ).read_text(encoding="utf-8")


def test_legacy_registration_block_is_disabled() -> None:
    """The legacy ``_register_agent_tools`` is wrapped in a deprecation
    comment; calling it must not register any templates against the
    in-memory ``AllowList`` passed in.
    """
    source = _allow_list_source()
    assert "DEPRECATED: legacy _register_agent_tools" in source
    # The legacy templates MUST NOT be present in the live allow-list.
    for name in LEGACY_TOOL_NAMES:
        assert name not in ALLOW_LIST.names(), name


def test_mock_templates_exist() -> None:
    """All nine mock templates are registered by ``_register_mock_agent_tools``."""
    registered = set(ALLOW_LIST.names())
    missing = MOCK_TOOL_NAMES - registered
    assert not missing, f"missing mock templates: {missing}"


def test_mock_templates_require_tenant_id() -> None:
    """Every mock template declares ``tenant_id`` as a slot."""
    for name in MOCK_TOOL_NAMES:
        slots = ALLOW_LIST.template_slots(name)
        assert "tenant_id" in slots, name


def test_mock_grounding_templates_exist() -> None:
    """The two mock grounding templates are registered (PR 3)."""
    assert "mock_courses_list" in ALLOW_LIST.names()
    assert "mock_assignments_list" in ALLOW_LIST.names()


def test_mock_courses_list_renders_with_tenant() -> None:
    """``mock_courses_list`` renders with a bind parameter for ``tenant_id``."""
    sql = ALLOW_LIST.resolve(
        "mock_courses_list", {"tenant_id": "tenant-stu-001"}
    )
    assert ":tenant_id" in sql
    assert "canvas_mock_courses" in sql
    assert "deleted_at IS NULL" in sql


def test_mock_assignments_list_renders_with_tenant() -> None:
    """``mock_assignments_list`` renders with a bind parameter for ``tenant_id``."""
    sql = ALLOW_LIST.resolve(
        "mock_assignments_list", {"tenant_id": "tenant-stu-001"}
    )
    assert ":tenant_id" in sql
    assert "canvas_mock_assignments" in sql
    assert "deleted_at IS NULL" in sql


def test_unknown_legacy_tool_is_not_in_allow_list() -> None:
    """Defence in depth: none of the legacy names slipped through."""
    for name in LEGACY_TOOL_NAMES:
        assert name not in ALLOW_LIST.names(), name


@pytest.mark.skip(reason="legacy self_user_id template deprecated; replaced by self_mock_user_id")
def test_register_agent_tools_legacy_self_user_id() -> None:
    """Legacy fixture preserved for the audit trail."""
    assert "self_user_id" in ALLOW_LIST.names()


def test_self_mock_user_id_is_a_template() -> None:
    """The mock self-user resolver is registered."""
    assert "self_mock_user_id" in ALLOW_LIST.names()
    sql = ALLOW_LIST.resolve("self_mock_user_id", {"tenant_id": "tenant-9"})
    assert ":tenant_id" in sql
    assert "canvas_mock_users" in sql
