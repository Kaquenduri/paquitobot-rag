"""LLM-assisted, allow-list-only SQL template selection.

The LLM never writes SQL. It only picks one name from a closed enum of
already-registered :mod:`app.text_to_sql.allow_list` templates and fills
the slots that template declares. ID slots (``assignment_id``/
``course_id``) are grounded against the tenant's real courses/assignments
(fetched by the caller before this runs, via the allow-listed
``courses_list``/``assignments_list`` templates) — any id the LLM returns
that isn't literally present in those lists is dropped, and the caller
falls back to the always-safe ``assignments_due_between`` template.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.text_to_sql.allow_list import ALLOW_LIST

if TYPE_CHECKING:
    from app.rag.agent import SQLToolRuntime

logger = get_logger("app.text_to_sql.template_selector")

FALLBACK_TEMPLATE = "assignments_due_between"
FALLBACK_SLOTS: dict[str, Any] = {"start_at": "1970-01-01", "end_at": "2099-12-31"}

# Token used as the ``term_pattern`` slot when the current date is
# out-of-cycle (January or February). It is an impossible LIKE pattern —
# nothing in the ``term_name`` column will ever match it — so the SQL
# safely returns zero rows instead of leaking a stale previous period.
NO_MATCH_PATTERN = "__NO_MATCH__"


class TemplateSelection(BaseModel):
    """Structured-output schema the LLM must fill — enum + grounded slots.

    ``template`` is a closed enum (function-calling / JSON-mode
    constrained), so the model literally cannot request anything outside
    these five names — there is no free-text SQL field anywhere in this
    schema.
    """

    template: Literal[
        "assignments_due_between",
        "assignment_score",
        "course_aggregate",
        "submission_status_for_assignment",
        "class_score_statistics",
    ] = Field(description="Exactly one allowed SQL template name.")
    start_at: str | None = Field(
        default=None, description="ISO date; only used for assignments_due_between."
    )
    end_at: str | None = Field(
        default=None, description="ISO date; only used for assignments_due_between."
    )
    assignment_id: str | None = Field(
        default=None,
        description="Copied verbatim from the assignments list below. Never invented.",
    )
    course_id: str | None = Field(
        default=None,
        description="Copied verbatim from the courses list below. Never invented.",
    )


def _valid_iso_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def _format_courses(courses: list[dict[str, Any]]) -> str:
    if not courses:
        return "(no courses)"
    return "\n".join(
        f"- id={c.get('id')} name={c.get('name') or '(unnamed)'} code={c.get('course_code') or ''}"
        for c in courses
    )


def _format_assignments(assignments: list[dict[str, Any]]) -> str:
    if not assignments:
        return "(no assignments)"
    return "\n".join(
        f"- id={a.get('id')} name={a.get('name') or '(unnamed)'} "
        f"course_id={a.get('course_id')} due_at={a.get('due_at')}"
        for a in assignments
    )


def _build_prompt(
    question: str, courses: list[dict[str, Any]], assignments: list[dict[str, Any]]
) -> str:
    return (
        "Pick exactly one SQL template for this student's question from this "
        "fixed list — never invent a new one:\n"
        "- assignments_due_between: list assignments in a date range "
        "(start_at/end_at, ISO dates; default to a wide range if the "
        "question doesn't mention dates).\n"
        "- assignment_score: the student's score/grade for ONE assignment "
        "(needs assignment_id).\n"
        "- course_aggregate: assignment count / average points for ONE "
        "course (needs course_id).\n"
        "- submission_status_for_assignment: submission status for ONE "
        "assignment (needs assignment_id).\n"
        "- class_score_statistics: score statistics for ONE course "
        "(needs course_id).\n\n"
        "If a template needs assignment_id or course_id, copy the value "
        "VERBATIM from the lists below. If nothing matches, leave it "
        "empty — never invent an id.\n\n"
        f"Courses:\n{_format_courses(courses)}\n\n"
        f"Assignments:\n{_format_assignments(assignments)}\n\n"
        f"Student question: {question}"
    )


def select_template(
    llm: Any,
    question: str,
    *,
    courses: list[dict[str, Any]],
    assignments: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Return ``(template_name, slots)`` — ``slots`` never includes ``tenant_id``.

    Falls back to :data:`FALLBACK_TEMPLATE` / :data:`FALLBACK_SLOTS` on any
    failure: LLM unreachable, malformed output, or a grounded id that
    doesn't match a real course/assignment for this tenant.
    """
    course_ids = {str(c.get("id")) for c in courses}
    assignment_ids = {str(a.get("id")) for a in assignments}

    try:
        structured_llm = llm.with_structured_output(TemplateSelection)
        selection = structured_llm.invoke(_build_prompt(question, courses, assignments))
    except Exception:
        logger.exception("rag_template_selection_llm_failed")
        return FALLBACK_TEMPLATE, dict(FALLBACK_SLOTS)

    template = selection.template
    if template not in ALLOW_LIST.names():
        return FALLBACK_TEMPLATE, dict(FALLBACK_SLOTS)  # pragma: no cover - enum already restricts this

    if template == "assignments_due_between":
        return template, {
            "start_at": _valid_iso_date(selection.start_at) or FALLBACK_SLOTS["start_at"],
            "end_at": _valid_iso_date(selection.end_at) or FALLBACK_SLOTS["end_at"],
        }
    if template in ("assignment_score", "submission_status_for_assignment"):
        if selection.assignment_id in assignment_ids:
            return template, {"assignment_id": selection.assignment_id}
        return FALLBACK_TEMPLATE, dict(FALLBACK_SLOTS)
    # template in ("course_aggregate", "class_score_statistics")
    if selection.course_id in course_ids:
        return template, {"course_id": selection.course_id}
    return FALLBACK_TEMPLATE, dict(FALLBACK_SLOTS)


def current_term_slots(
    runtime: SQLToolRuntime,
    period: tuple[int, int] | None,
) -> dict[str, object]:
    """Build the slot dict for ``get_user_courses_current_term``.

    ``period`` is the result of :func:`app.text_to_sql.period.current_academic_period`
    for today's date. When ``None`` (January or February), the function
    substitutes :data:`NO_MATCH_PATTERN` for ``term_pattern`` so the SQL
    matches zero rows — a deliberate "empty by construction" outcome
    rather than a fallback to the previous academic period.

    ``runtime.tenant_id`` is the canonical source of truth for the slot;
    the function does not consult any other argument.
    """
    if period is None:
        term_pattern = NO_MATCH_PATTERN
    else:
        year, period_num = period
        term_pattern = f"%{year} - {period_num}"
    return {"tenant_id": str(runtime.tenant_id), "term_pattern": term_pattern}


def _selftest() -> None:
    from app.core.logging import configure_console_encoding

    # The ``_BrokenLLM`` case below drives ``logger.exception``; without
    # this the traceback render crashes on a cp1252 Windows console.
    configure_console_encoding()

    courses = [{"id": "c-1", "name": "Math", "course_code": "MTH101"}]
    assignments = [{"id": "a-1", "name": "Midterm", "course_id": "c-1", "due_at": "2026-05-01"}]

    class _StubRunnable:
        def __init__(self, result: TemplateSelection) -> None:
            self._result = result

        def invoke(self, _prompt: str) -> TemplateSelection:
            return self._result

    class _StubLLM:
        def __init__(self, result: TemplateSelection) -> None:
            self._result = result

        def with_structured_output(self, _schema: Any) -> _StubRunnable:
            return _StubRunnable(self._result)

    # Grounded id: accepted verbatim.
    llm = _StubLLM(TemplateSelection(template="assignment_score", assignment_id="a-1"))
    template, slots = select_template(
        llm, "cual es mi nota en el midterm", courses=courses, assignments=assignments
    )
    assert template == "assignment_score"
    assert slots == {"assignment_id": "a-1"}

    # Hallucinated id: rejected, falls back to the safe default.
    llm = _StubLLM(TemplateSelection(template="assignment_score", assignment_id="does-not-exist"))
    template, slots = select_template(
        llm, "cual es mi nota", courses=courses, assignments=assignments
    )
    assert template == FALLBACK_TEMPLATE
    assert slots == FALLBACK_SLOTS

    # LLM unreachable: falls back to the safe default.
    class _BrokenLLM:
        def with_structured_output(self, _schema: Any) -> Any:
            raise RuntimeError("unreachable")

    template, slots = select_template(
        _BrokenLLM(), "hola", courses=courses, assignments=assignments
    )
    assert template == FALLBACK_TEMPLATE
    assert slots == FALLBACK_SLOTS

    # Valid ISO dates pass through untouched.
    llm = _StubLLM(
        TemplateSelection(
            template="assignments_due_between", start_at="2026-01-01", end_at="2026-12-31"
        )
    )
    template, slots = select_template(
        llm, "tareas de este anio", courses=courses, assignments=assignments
    )
    assert slots == {"start_at": "2026-01-01", "end_at": "2026-12-31"}


if __name__ == "__main__":
    _selftest()
