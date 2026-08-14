"""Safe, named SQL templates for relational retrieval."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass


class TemplateNotAllowed(ValueError):
    """Raised when a template or slot is not registered/declared."""


@dataclass(frozen=True)
class SQLTemplate:
    name: str
    sql: str
    slots: frozenset[str]


class AllowList:
    """Registry that renders only code-owned SQL templates."""

    def __init__(self) -> None:
        self._templates: dict[str, SQLTemplate] = {}

    def register(self, name: str, sql: str, *, slots: set[str] | frozenset[str]) -> None:
        declared = frozenset(slots)
        placeholders = set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", sql))
        if "tenant_id" not in placeholders or not declared.issuperset(placeholders):
            raise TemplateNotAllowed("templates must declare tenant_id and every placeholder")
        self._templates[name] = SQLTemplate(name, sql, declared)

    def resolve(self, template_name: str, slots: Mapping[str, object]) -> str:
        try:
            template = self._templates[template_name]
        except KeyError as exc:
            raise TemplateNotAllowed("unknown SQL template") from exc
        unknown = set(slots) - template.slots
        missing = template.slots - set(slots)
        if unknown or missing:
            raise TemplateNotAllowed("invalid SQL template slots")
        rendered = template.sql
        for key in slots:
            if key == "tenant_id":
                replacement = ":tenant_id"
            else:
                replacement = ":" + key
            rendered = re.sub(r"\{\{\s*" + re.escape(key) + r"\s*\}\}", replacement, rendered)
        return rendered

    def names(self) -> tuple[str, ...]:
        return tuple(self._templates)

    def template_slots(self, template_name: str) -> frozenset[str]:
        """Return the slots a template declares.

        Used by :mod:`app.text_to_sql.tools` to assert at import time that
        every tool's argument schema plus its server-injected slots cover
        the template exactly.
        """
        try:
            return self._templates[template_name].slots
        except KeyError as exc:
            raise TemplateNotAllowed("unknown SQL template") from exc


def default_allow_list() -> AllowList:
    registry = AllowList()
    registry.register(
        "assignments_due_between",
        "SELECT id, name, due_at FROM assignments WHERE tenant_id = {{tenant_id}} AND due_at BETWEEN :start_at AND :end_at AND deleted_at IS NULL",
        slots={"tenant_id", "start_at", "end_at"},
    )
    registry.register(
        "courses_due_between",
        "SELECT c.id, c.name, a.id AS assignment_id, a.name AS assignment_name, a.due_at FROM courses c JOIN assignments a ON a.course_id = c.id AND a.tenant_id = c.tenant_id WHERE c.tenant_id = {{tenant_id}} AND a.due_at BETWEEN :start_at AND :end_at AND a.deleted_at IS NULL ORDER BY a.due_at",
        slots={"tenant_id", "start_at", "end_at"},
    )
    registry.register(
        "courses_list",
        "SELECT id, name, course_code FROM courses WHERE tenant_id = {{tenant_id}} AND deleted_at IS NULL ORDER BY name",
        slots={"tenant_id"},
    )
    registry.register(
        "assignment_score",
        "SELECT a.id, a.name, s.score, s.grade FROM assignments a JOIN submissions s ON s.assignment_id = a.id AND s.tenant_id = a.tenant_id WHERE a.tenant_id = {{tenant_id}} AND a.id = :assignment_id AND s.deleted_at IS NULL",
        slots={"tenant_id", "assignment_id"},
    )
    registry.register(
        "course_aggregate",
        "SELECT course_id, COUNT(*) AS assignment_count, AVG(points_possible) AS average_points FROM assignments WHERE tenant_id = {{tenant_id}} AND course_id = :course_id AND deleted_at IS NULL GROUP BY course_id",
        slots={"tenant_id", "course_id"},
    )
    registry.register(
        "submission_status_for_assignment",
        "SELECT id, workflow_state, submitted_at, late, missing FROM submissions WHERE tenant_id = {{tenant_id}} AND assignment_id = :assignment_id AND deleted_at IS NULL",
        slots={"tenant_id", "assignment_id"},
    )
    registry.register(
        "class_score_statistics",
        "SELECT AVG(score) AS average_score, MIN(score) AS minimum_score, MAX(score) AS maximum_score, COUNT(score) AS scored_count FROM submissions WHERE tenant_id = {{tenant_id}} AND course_id = :course_id AND deleted_at IS NULL",
        slots={"tenant_id", "course_id"},
    )
    # Grounding-only template: not selectable by the LLM (see
    # app.text_to_sql.template_selector) — used to fetch the tenant's real
    # assignment ids so assignment_id slots above are never filled with a
    # hallucinated value. ``courses_list`` above serves the same purpose
    # for course_id.
    registry.register(
        "assignments_list",
        "SELECT id, name, course_id, due_at FROM assignments WHERE tenant_id = {{tenant_id}} AND deleted_at IS NULL ORDER BY due_at",
        slots={"tenant_id"},
    )
    _register_agent_tools(registry)
    return registry


def _register_agent_tools(registry: AllowList) -> None:
    """Register the templates exposed to the LLM as tools.

    These back the :mod:`app.text_to_sql.tools` catalog, which the
    tool-calling agent in :mod:`app.rag.agent` binds to the model. Two
    conventions matter here:

    * ``{{user_id}}`` is a *server* slot — resolved from ``tenant_id`` via
      ``self_user_id`` below, never supplied by the model. The ``users``
      table only ever holds the tenant's own self-profile, so there is
      exactly one correct value per tenant.
    * every statement carries ``deleted_at IS NULL`` so soft-deleted rows
      stay invisible, and no statement ends in ``;`` (the executor wraps
      the SQL in a ``SELECT * FROM (...) LIMIT n`` subquery).
    """
    registry.register(
        "self_user_id",
        "SELECT id FROM users WHERE tenant_id = {{tenant_id}} AND deleted_at IS NULL ORDER BY created_at LIMIT 1",
        slots={"tenant_id"},
    )
    registry.register(
        "get_user_profile",
        "SELECT id, name, short_name, email, canvas_id "
        "FROM users "
        "WHERE tenant_id = {{tenant_id}} AND id = {{user_id}} AND deleted_at IS NULL",
        slots={"tenant_id", "user_id"},
    )
    registry.register(
        "get_user_courses",
        "SELECT c.id, c.name, c.course_code, c.start_at, c.end_at, e.role, e.enrollment_state "
        "FROM courses c "
        "JOIN enrollments e ON c.id = e.course_id AND e.tenant_id = c.tenant_id "
        "WHERE c.tenant_id = {{tenant_id}} AND e.user_id = {{user_id}} "
        "AND c.deleted_at IS NULL AND e.deleted_at IS NULL "
        "ORDER BY c.name",
        slots={"tenant_id", "user_id"},
    )
    registry.register(
        "get_course_assignments",
        "SELECT id, name, description, points_possible, due_at, grading_type "
        "FROM assignments "
        "WHERE tenant_id = {{tenant_id}} AND course_id = {{course_id}} AND deleted_at IS NULL "
        "ORDER BY due_at ASC",
        slots={"tenant_id", "course_id"},
    )
    registry.register(
        "get_assignment_details",
        "SELECT id, name, description, points_possible, due_at, unlock_at, lock_at, "
        "grading_type, workflow_state "
        "FROM assignments "
        "WHERE tenant_id = {{tenant_id}} AND id = {{assignment_id}} AND deleted_at IS NULL",
        slots={"tenant_id", "assignment_id"},
    )
    registry.register(
        "get_user_course_submissions",
        "SELECT a.name AS assignment_name, s.score, a.points_possible, s.grade, "
        "s.submitted_at, s.late, s.missing, s.excused "
        "FROM submissions s "
        "JOIN assignments a ON s.assignment_id = a.id AND a.tenant_id = s.tenant_id "
        "WHERE s.tenant_id = {{tenant_id}} AND s.user_id = {{user_id}} "
        "AND a.course_id = {{course_id}} "
        "AND s.deleted_at IS NULL AND a.deleted_at IS NULL "
        "ORDER BY a.due_at ASC",
        slots={"tenant_id", "user_id", "course_id"},
    )
    registry.register(
        "get_user_missing_submissions",
        "SELECT c.name AS course_name, a.name AS assignment_name, a.due_at, a.points_possible "
        "FROM submissions s "
        "JOIN assignments a ON s.assignment_id = a.id AND a.tenant_id = s.tenant_id "
        "JOIN courses c ON a.course_id = c.id AND c.tenant_id = a.tenant_id "
        "WHERE s.tenant_id = {{tenant_id}} AND s.user_id = {{user_id}} AND s.missing = true "
        "AND s.deleted_at IS NULL AND a.deleted_at IS NULL AND c.deleted_at IS NULL "
        "ORDER BY a.due_at ASC",
        slots={"tenant_id", "user_id"},
    )
    registry.register(
        "get_user_late_submissions",
        "SELECT c.name AS course_name, a.name AS assignment_name, s.submitted_at, "
        "a.due_at, s.score, s.grade "
        "FROM submissions s "
        "JOIN assignments a ON s.assignment_id = a.id AND a.tenant_id = s.tenant_id "
        "JOIN courses c ON a.course_id = c.id AND c.tenant_id = a.tenant_id "
        "WHERE s.tenant_id = {{tenant_id}} AND s.user_id = {{user_id}} AND s.late = true "
        "AND s.deleted_at IS NULL AND a.deleted_at IS NULL AND c.deleted_at IS NULL "
        "ORDER BY s.submitted_at DESC",
        slots={"tenant_id", "user_id"},
    )
    registry.register(
        "get_course_details",
        "SELECT id, name, course_code, start_at, end_at, workflow_state, enrollments_count "
        "FROM courses "
        "WHERE tenant_id = {{tenant_id}} AND id = {{course_id}} AND deleted_at IS NULL",
        slots={"tenant_id", "course_id"},
    )
    registry.register(
        "get_user_courses_current_term",
        "SELECT id, name, course_code, term_name "
        "FROM courses "
        "WHERE tenant_id = {{tenant_id}} AND deleted_at IS NULL "
        "AND term_name LIKE :term_pattern "
        "ORDER BY name LIMIT 100",
        slots={"tenant_id", "term_pattern"},
    )


ALLOW_LIST = default_allow_list()


def _selftest() -> None:
    from app.text_to_sql.validator import validate_sql

    sql = ALLOW_LIST.resolve("assignment_score", {"tenant_id": "safe", "assignment_id": 7})
    assert ":tenant_id" in sql and "assignment_id" in sql
    try:
        ALLOW_LIST.resolve("assignment_score", {"tenant_id": "safe", "evil": "DROP"})
    except TemplateNotAllowed:
        pass
    else:
        raise AssertionError("undeclared slots must be rejected")

    # Every agent-facing template must render with its declared slots and
    # survive the structural validator the executor runs.
    agent_slots = {
        "self_user_id": set(),
        "get_user_profile": {"user_id"},
        "get_user_courses": {"user_id"},
        "get_course_assignments": {"course_id"},
        "get_assignment_details": {"assignment_id"},
        "get_user_course_submissions": {"user_id", "course_id"},
        "get_user_missing_submissions": {"user_id"},
        "get_user_late_submissions": {"user_id"},
        "get_course_details": {"course_id"},
        "get_user_courses_current_term": set(),
    }
    for name, extra in agent_slots.items():
        slots = {"tenant_id": "t", **dict.fromkeys(extra, "x")}
        rendered = ALLOW_LIST.resolve(name, slots)
        assert ":tenant_id" in rendered, name
        # No leftover ``{{...}}`` placeholders and no statement separator.
        assert "{{" not in rendered, name
        assert ";" not in rendered, name
        validate_sql(rendered)


if __name__ == "__main__":
    _selftest()
