"""Unit tests for the slot-builder that backs ``get_user_courses_current_term``.

The tool is intentionally a no-argument call from the model: every bind
parameter is resolved server-side from the academic-period calendar and
the tenant's identity. These tests pin that contract — there is no way
to add a ``term_pattern`` slot in the agent loop, so the SQL stays safe
regardless of what the LLM tries to pass.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.rag.agent import SQLToolRuntime
from app.text_to_sql.template_selector import (
    NO_MATCH_PATTERN,
    current_term_slots,
)
from app.text_to_sql.tools import SQLTool

# A minimal SQLTool instance — the slot-builder never touches the tool
# itself, only the runtime's tenant_id, so any sentinel shape works.
_DUMMY_TOOL = SQLTool(
    name="get_user_courses_current_term",
    description="",
    args_schema=type("Args", (), {}),
    server_slots=frozenset({"tenant_id"}),
)


def _runtime(tenant_id: uuid.UUID | str | None = None) -> SQLToolRuntime:
    return SQLToolRuntime(
        execute=lambda _tool, _args: [],
        known_ids=lambda _slot: set(),
        tenant_id=tenant_id,
    )


def test_out_of_cycle_returns_no_match_pattern() -> None:
    slots = current_term_slots(_runtime(tenant_id="t-1"), period=None)

    assert slots == {"tenant_id": "t-1", "term_pattern": NO_MATCH_PATTERN}


@pytest.mark.parametrize(
    ("today", "expected_year", "expected_period", "expected_pattern"),
    [
        (date(2026, 3, 15), 2026, 1, "%2026 - 1"),
        (date(2026, 7, 31), 2026, 1, "%2026 - 1"),
        (date(2026, 8, 1), 2026, 2, "%2026 - 2"),
        (date(2026, 12, 31), 2026, 2, "%2026 - 2"),
    ],
)
def test_in_cycle_returns_wildcard_pattern_matching_any_prefix(
    today: date, expected_year: int, expected_period: int, expected_pattern: str
) -> None:
    slots = current_term_slots(_runtime(tenant_id="tenant-x"), period=(expected_year, expected_period))

    assert slots["tenant_id"] == "tenant-x"
    assert slots["term_pattern"] == expected_pattern


def test_pattern_is_a_like_wildcard_for_canvas_period_naming() -> None:
    """``%2026 - 2`` matches both ``PFR A 2026 - 2`` and ``REG 2026 - 2``."""

    slots = current_term_slots(_runtime(tenant_id="t"), period=(2026, 2))

    assert slots["term_pattern"].startswith("%")
    assert slots["term_pattern"].endswith("2026 - 2")


def test_tenant_id_is_coerced_to_string() -> None:
    tenant_uuid = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
    slots = current_term_slots(_runtime(tenant_id=tenant_uuid), period=(2026, 2))

    assert slots["tenant_id"] == str(tenant_uuid)
    assert isinstance(slots["tenant_id"], str)


def test_no_match_pattern_does_not_collide_with_real_canvas_term_names() -> None:
    """The placeholder is intentionally not a substring of any real term name."""

    # The marker is a sentinel: it can never appear inside a Canvas
    # ``term.name`` value, so the ``LIKE :term_pattern`` filter returns
    # zero rows by construction when the date is out-of-cycle.
    assert "__" in NO_MATCH_PATTERN
    assert "PFR A 2026 - 2" != NO_MATCH_PATTERN
    assert "REG 2026 - 2" != NO_MATCH_PATTERN
    assert NO_MATCH_PATTERN not in "PFR A 2026 - 2"
    assert NO_MATCH_PATTERN not in "REG 2026 - 2"


def test_template_slot_contract_matches_allow_list() -> None:
    """The slot-builder's keys line up with the registered template."""

    slots = current_term_slots(_runtime(tenant_id="t"), period=(2026, 1))

    assert set(slots) == {"tenant_id", "term_pattern"}