"""Academic-period calendar used by the LLM tool ``get_user_courses_current_term``.

The Canvas ``term.name`` field encodes both year and academic period, e.g.
``"PFR A 2026 - 2"`` or ``"REG 2026 - 2"``. The actual academic window is
not encoded in the name itself — it is derived server-side from today's
date.

Calendar rules (deliberately hardcoded — there is exactly one academic
calendar per tenant):

* Period 1 = March through July (inclusive).
* Period 2 = August through December (inclusive).
* January and February are out-of-cycle (between Period 2 of the prior
  year and Period 1 of the new year). The tool returns an empty result
  set during those two months; this function reports the situation as
  ``None`` so the call site can substitute an impossible ``LIKE``
  pattern and short-circuit the SQL.
"""

from __future__ import annotations

from datetime import date


def current_academic_period(today: date) -> tuple[int, int] | None:
    """Return ``(year, period)`` for the academic period containing ``today``.

    Period 1 spans March–July and Period 2 spans August–December. Both
    months are inclusive on both ends. January and February return
    ``None`` — the caller is expected to translate that into an empty
    result rather than picking the previous year's Period 2 (which would
    be wrong once the new Period 1 has started somewhere else).

    >>> from datetime import date
    >>> current_academic_period(date(2026, 3, 15))
    (2026, 1)
    >>> current_academic_period(date(2026, 7, 31))
    (2026, 1)
    >>> current_academic_period(date(2026, 8, 1))
    (2026, 2)
    >>> current_academic_period(date(2026, 12, 31))
    (2026, 2)
    >>> current_academic_period(date(2026, 1, 15)) is None
    True
    >>> current_academic_period(date(2026, 2, 28)) is None
    True
    """
    if today.month in (1, 2):
        return None
    if 3 <= today.month <= 7:
        return (today.year, 1)
    return (today.year, 2)


__all__ = ["current_academic_period"]


def _selftest() -> None:
    from datetime import date

    assert current_academic_period(date(2026, 3, 15)) == (2026, 1)
    assert current_academic_period(date(2026, 7, 31)) == (2026, 1)
    assert current_academic_period(date(2026, 8, 1)) == (2026, 2)
    assert current_academic_period(date(2026, 12, 31)) == (2026, 2)
    assert current_academic_period(date(2026, 1, 15)) is None
    assert current_academic_period(date(2026, 2, 28)) is None
    # Edge: 1 March belongs to Period 1 of that year (not Period 2 of last).
    assert current_academic_period(date(2026, 3, 1)) == (2026, 1)
    # Period 2 of a leap year still uses the same year.
    assert current_academic_period(date(2024, 8, 15)) == (2024, 2)


if __name__ == "__main__":
    _selftest()