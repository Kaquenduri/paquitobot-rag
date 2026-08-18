"""Unit tests for the catalog + forensic deprecation (PR 2 + PR 3).

The catalog must continue to expose exactly nine tools (the prompt's
hard assertion), but the contents change to the nine mock tools. The
nine legacy tuples MUST stay on disk as ``# DEPRECATED:`` lines so an
operator can grep them for forensics — but they MUST NOT be in the
live catalog, MUST NOT be in ``_TOOL_SPECS``, and MUST NOT appear in
``tool_specs()`` (the list the agent hands to the model).

PR 3 adds per-tool rendering tests that exercise the mock args
schemas, the ``slot_type="int"`` declaration, and the allow-list
template registration for every mock tool.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.text_to_sql.tools import (
    TOOL_CATALOG,
    TOOL_NAMES,
    MockAssignmentArgs,
    MockCourseArgs,
    NoArgs,
    _TOOL_SPECS,
    tool_specs,
)

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


def _source_text() -> str:
    return Path(__file__).resolve().parents[2].joinpath(
        "app", "text_to_sql", "tools.py"
    ).read_text(encoding="utf-8")


def test_catalog_has_exactly_nine_mock_tools() -> None:
    """The hard ``len(TOOL_CATALOG) == 9`` still holds — count is preserved."""
    assert len(TOOL_CATALOG) == 9
    assert len(TOOL_NAMES) == 9
    assert MOCK_TOOL_NAMES == set(TOOL_NAMES)


def test_legacy_tool_names_are_not_in_the_catalog() -> None:
    """The nine legacy tools must not be reachable from the live catalog."""
    assert not (LEGACY_TOOL_NAMES & set(TOOL_NAMES))


def test_legacy_tool_names_are_not_in_tool_specs() -> None:
    """The LLM-facing spec list must contain only the mock tools."""
    spec_names = {spec["name"] for spec in tool_specs()}
    assert spec_names == MOCK_TOOL_NAMES


def test_tool_specs_source_has_at_least_nine_deprecated_lines() -> None:
    """The deprecation proof-of-life: 9 ``# DEPRECATED:`` headers on disk."""
    source = _source_text()
    deprecated_count = sum(
        1 for line in source.splitlines() if line.lstrip().startswith("# DEPRECATED")
    )
    assert deprecated_count >= 9, deprecated_count


def test_tooL_specs_is_a_tuple_of_length_nine() -> None:
    """``_TOOL_SPECS`` itself is the canonical 9-tuple the runtime iterates."""
    assert isinstance(_TOOL_SPECS, tuple)
    assert len(_TOOL_SPECS) == 9


# ---------------------------------------------------------------------------
# Per-tool rendering (PR 3 task 3.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(MOCK_TOOL_NAMES))
def test_every_mock_tool_declares_int_slot_type(name: str) -> None:
    """Every mock tool uses ``slot_type="int"`` (canvas_mock_id is INT)."""
    tool = TOOL_CATALOG[name]
    assert tool.slot_type == "int", name


@pytest.mark.parametrize("name", sorted(MOCK_TOOL_NAMES))
def test_every_mock_tool_has_a_non_empty_description(name: str) -> None:
    """Every mock tool description is a non-empty string (Spanish)."""
    tool = TOOL_CATALOG[name]
    assert tool.description.strip(), name


@pytest.mark.parametrize("name", sorted(MOCK_TOOL_NAMES))
def test_every_mock_tool_has_a_registered_template(name: str) -> None:
    """Every mock tool has a corresponding SQL template in the allow-list."""
    from app.text_to_sql.allow_list import ALLOW_LIST

    assert name in ALLOW_LIST.names(), name


def test_get_user_mock_courses_uses_no_args() -> None:
    """The user-scoped mock tool accepts no model arguments."""
    assert TOOL_CATALOG["get_user_mock_courses"].args_schema is NoArgs


def test_get_mock_course_details_uses_mock_course_args() -> None:
    """The single-course mock tool uses the int ``MockCourseArgs`` schema."""
    assert TOOL_CATALOG["get_mock_course_details"].args_schema is MockCourseArgs


def test_get_mock_assignment_details_uses_mock_assignment_args() -> None:
    """The single-assignment mock tool uses the int ``MockAssignmentArgs`` schema."""
    assert (
        TOOL_CATALOG["get_mock_assignment_details"].args_schema is MockAssignmentArgs
    )


def test_mock_course_args_accepts_int() -> None:
    """``MockCourseArgs(course_id=12)`` parses and round-trips."""
    args = MockCourseArgs(course_id=12)
    assert args.course_id == 12
    assert args.model_dump() == {"course_id": 12}


def test_mock_assignment_args_accepts_int() -> None:
    """``MockAssignmentArgs(assignment_id=42)`` parses and round-trips."""
    args = MockAssignmentArgs(assignment_id=42)
    assert args.assignment_id == 42
    assert args.model_dump() == {"assignment_id": 42}


def test_mock_course_args_rejects_extra_keys() -> None:
    """``MockCourseArgs`` is ``extra="forbid"`` — a smuggling attempt fails."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MockCourseArgs(course_id=12, tenant_id="other")  # type: ignore[call-arg]


def test_tool_specs_only_advertises_mock_slots() -> None:
    """No declaration may advertise a free-text SQL argument or a server slot."""
    for spec in tool_specs():
        props = spec["parameters"]["properties"]
        assert "sql" not in props and "query" not in props, spec["name"]
        assert "tenant_id" not in props and "user_id" not in props, spec["name"]
        assert "user_id_mock" not in props, spec["name"]


# ---------------------------------------------------------------------------
# Mock self-user resolver (PR 3 task 3.6)
# ---------------------------------------------------------------------------


def test_self_mock_user_id_template_is_registered() -> None:
    """The mock self-user resolver is registered as a template."""
    from app.text_to_sql.allow_list import ALLOW_LIST

    assert "self_mock_user_id" in ALLOW_LIST.names()

