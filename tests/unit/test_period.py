"""Unit tests for the academic-period calendar.

The function is pure (date -> optional tuple) and is the single source of
truth for whether the LLM tool ``get_user_courses_current_term`` should
return courses at all. Boundary months are the highest-risk inputs
because the period assignment flips there; parametrize over the whole
year to catch any off-by-one.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.text_to_sql.period import current_academic_period

BOUNDARY_CASES: list[tuple[date, tuple[int, int] | None, str]] = [
    (date(2026, 3, 1), (2026, 1), "first day of period 1 belongs to period 1"),
    (date(2026, 3, 15), (2026, 1), "mid-March is period 1"),
    (date(2026, 7, 31), (2026, 1), "last day of July is period 1"),
    (date(2026, 8, 1), (2026, 2), "first day of August is period 2"),
    (date(2026, 12, 31), (2026, 2), "last day of December is period 2"),
    (date(2026, 1, 15), None, "mid-January is out-of-cycle"),
    (date(2026, 2, 28), None, "last day of February is out-of-cycle"),
    (date(2024, 8, 15), (2024, 2), "leap-year August stays in 2024 period 2"),
]


@pytest.mark.parametrize(("today", "expected", "label"), BOUNDARY_CASES)
def test_current_academic_period_returns_expected_window(
    today: date, expected: tuple[int, int] | None, label: str
) -> None:
    assert current_academic_period(today) == expected, label


def test_february_29_in_a_leap_year_is_still_out_of_cycle() -> None:
    """The function does not depend on the day, only the month."""

    assert current_academic_period(date(2024, 2, 29)) is None


def test_december_31_in_a_prior_year_does_not_leak_into_next_year() -> None:
    """December stays in Period 2 of its own year, not Period 1 of the next."""

    assert current_academic_period(date(2025, 12, 31)) == (2025, 2)


def test_january_1_does_not_resurrect_the_previous_period_2() -> None:
    """Out-of-cycle must be ``None`` even at the calendar flip."""

    assert current_academic_period(date(2027, 1, 1)) is None