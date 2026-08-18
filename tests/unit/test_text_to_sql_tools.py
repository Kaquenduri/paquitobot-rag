"""Unit tests for the catalog + forensic deprecation (PR 2 task 2.1).

The catalog must continue to expose exactly nine tools (the prompt's
hard assertion), but the contents change to the nine mock tools. The
nine legacy tuples MUST stay on disk as ``# DEPRECATED:`` lines so an
operator can grep them for forensics — but they MUST NOT be in the
live catalog, MUST NOT be in ``_TOOL_SPECS``, and MUST NOT appear in
``tool_specs()`` (the list the agent hands to the model).

The test below anchors all three invariants in one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.text_to_sql.tools import TOOL_CATALOG, TOOL_NAMES, _TOOL_SPECS, tool_specs

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
    # At least 9 (one per legacy tuple); allow more for the helper-class /
    # module-level headers.
    assert deprecated_count >= 9, deprecated_count


def test_tooL_specs_is_a_tuple_of_length_nine() -> None:
    """``_TOOL_SPECS`` itself is the canonical 9-tuple the runtime iterates."""
    assert isinstance(_TOOL_SPECS, tuple)
    assert len(_TOOL_SPECS) == 9
