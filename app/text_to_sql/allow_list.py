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
    # Mock grounding (PR 2 / PR 3): list tables scoped to a tenant so the
    # agent can validate ``course_id_mock`` / ``assignment_id_mock`` ints
    # before letting them reach the SQL template.
    registry.register(
        "mock_courses_list",
        "SELECT canvas_mock_id FROM canvas_mock_courses WHERE tenant_id = {{tenant_id}} AND deleted_at IS NULL ORDER BY canvas_mock_id",
        slots={"tenant_id"},
    )
    registry.register(
        "mock_assignments_list",
        "SELECT canvas_mock_id FROM canvas_mock_assignments WHERE tenant_id = {{tenant_id}} AND deleted_at IS NULL ORDER BY canvas_mock_id",
        slots={"tenant_id"},
    )
    _register_mock_agent_tools(registry)
    return registry


# ---------------------------------------------------------------------------
# Mock Agent Tools (PR 2 / PR 3)
# ---------------------------------------------------------------------------
#
# Nine templates mirroring the legacy nine agent tools. They read from
# the ``canvas_mock_*`` tables introduced in PR 1. Stubs in PR 2;
# PR 3 fills the missing FROM/JOIN blocks when the mock extractor
# (PR 4) is online.
#
# IMPORTANT: these templates are the LLM-facing catalog. The legacy
# ``_register_agent_tools`` block below is kept (commented out) for
# forensics so an operator can grep the prior SQL easily.


def _register_mock_agent_tools(registry: AllowList) -> None:
    """Register the nine mock templates exposed to the LLM as tools.

    Same conventions as the legacy block: every statement carries
    ``tenant_id`` and ``deleted_at IS NULL``; no statement ends in
    ``;``. ``user_id_mock`` is a server slot — resolved from
    ``tenant_id`` via ``self_mock_user_id`` below, never supplied by
    the model.
    """
    registry.register(
        "self_mock_user_id",
        "SELECT canvas_mock_id FROM canvas_mock_users WHERE tenant_id = {{tenant_id}} AND deleted_at IS NULL ORDER BY created_at LIMIT 1",
        slots={"tenant_id"},
    )
    registry.register(
        "get_user_mock_courses",
        "SELECT c.canvas_mock_id, c.name, c.course_code, c.workflow_state, c.start_at, c.end_at "
        "FROM canvas_mock_courses c "
        "JOIN canvas_mock_enrollments e ON e.course_canvas_mock_id = c.canvas_mock_id AND e.tenant_id = c.tenant_id "
        "WHERE c.tenant_id = {{tenant_id}} AND e.user_canvas_mock_id = {{user_id_mock}} "
        "AND c.deleted_at IS NULL AND e.deleted_at IS NULL "
        "ORDER BY c.name",
        slots={"tenant_id", "user_id_mock"},
    )
    registry.register(
        "get_mock_course_details",
        "SELECT canvas_mock_id, name, course_code, workflow_state, start_at, end_at, enrollments_count "
        "FROM canvas_mock_courses "
        "WHERE tenant_id = {{tenant_id}} AND canvas_mock_id = {{course_id}} AND deleted_at IS NULL",
        slots={"tenant_id", "course_id"},
    )
    registry.register(
        "get_mock_course_assignments",
        "SELECT canvas_mock_id, name, description, points_possible, due_at, grading_type, workflow_state "
        "FROM canvas_mock_assignments "
        "WHERE tenant_id = {{tenant_id}} AND course_canvas_mock_id = {{course_id}} AND deleted_at IS NULL "
        "ORDER BY due_at ASC",
        slots={"tenant_id", "course_id"},
    )
    registry.register(
        "get_mock_assignment_details",
        "SELECT canvas_mock_id, name, description, points_possible, due_at, grading_type, workflow_state "
        "FROM canvas_mock_assignments "
        "WHERE tenant_id = {{tenant_id}} AND canvas_mock_id = {{assignment_id}} AND deleted_at IS NULL",
        slots={"tenant_id", "assignment_id"},
    )
    registry.register(
        "get_user_mock_grades",
        "SELECT g.assignment_canvas_mock_id, g.user_canvas_mock_id, g.score, g.grade, g.graded_at, g.grader_id "
        "FROM canvas_mock_grades g "
        "WHERE g.tenant_id = {{tenant_id}} AND g.user_canvas_mock_id = {{user_id_mock}} "
        "AND g.deleted_at IS NULL "
        "ORDER BY g.graded_at DESC",
        slots={"tenant_id", "user_id_mock"},
    )
    registry.register(
        "get_user_mock_course_grades",
        "SELECT g.assignment_canvas_mock_id, g.user_canvas_mock_id, g.score, g.grade, g.graded_at, g.grader_id "
        "FROM canvas_mock_grades g "
        "JOIN canvas_mock_assignments a ON a.canvas_mock_id = g.assignment_canvas_mock_id AND a.tenant_id = g.tenant_id "
        "WHERE g.tenant_id = {{tenant_id}} AND g.user_canvas_mock_id = {{user_id_mock}} "
        "AND a.course_canvas_mock_id = {{course_id}} "
        "AND g.deleted_at IS NULL AND a.deleted_at IS NULL "
        "ORDER BY g.graded_at DESC",
        slots={"tenant_id", "user_id_mock", "course_id"},
    )
    registry.register(
        "get_user_missing_mock_assignments",
        "SELECT a.canvas_mock_id, a.name, a.due_at, a.points_possible, a.course_canvas_mock_id, c.name AS course_name "
        "FROM canvas_mock_assignments a "
        "JOIN canvas_mock_courses c ON c.canvas_mock_id = a.course_canvas_mock_id AND c.tenant_id = a.tenant_id "
        "LEFT JOIN canvas_mock_grades g ON g.assignment_canvas_mock_id = a.canvas_mock_id "
        "AND g.user_canvas_mock_id = {{user_id_mock}} AND g.tenant_id = a.tenant_id "
        "WHERE a.tenant_id = {{tenant_id}} "
        "AND g.id IS NULL AND a.due_at IS NOT NULL AND a.due_at < now() "
        "AND a.deleted_at IS NULL AND c.deleted_at IS NULL "
        "ORDER BY a.due_at ASC",
        slots={"tenant_id", "user_id_mock"},
    )
    registry.register(
        "get_user_attendance",
        "SELECT r.class_session_canvas_mock_id, r.user_canvas_mock_id, r.status, "
        "s.canvas_mock_id AS session_id, s.course_canvas_mock_id, s.start_at, s.end_at "
        "FROM canvas_mock_attendance_records r "
        "JOIN canvas_mock_class_sessions s ON s.canvas_mock_id = r.class_session_canvas_mock_id AND s.tenant_id = r.tenant_id "
        "WHERE r.tenant_id = {{tenant_id}} AND r.user_canvas_mock_id = {{user_id_mock}} "
        "AND r.deleted_at IS NULL AND s.deleted_at IS NULL "
        "ORDER BY s.start_at DESC",
        slots={"tenant_id", "user_id_mock"},
    )
    registry.register(
        "get_mock_class_sessions",
        "SELECT canvas_mock_id, course_canvas_mock_id, start_at, end_at "
        "FROM canvas_mock_class_sessions "
        "WHERE tenant_id = {{tenant_id}} AND course_canvas_mock_id = {{course_id}} AND deleted_at IS NULL "
        "ORDER BY start_at ASC",
        slots={"tenant_id", "course_id"},
    )


# ---------------------------------------------------------------------------
# DEPRECATED: legacy _register_agent_tools (PR 2 task 2.3)
# ---------------------------------------------------------------------------
# The legacy nine templates (UUID-based, real Canvas tables) are no
# longer bound to the LLM. They remain on disk with `# DEPRECATED:`
# headers so an operator can grep them for forensics. The live catalog
# is built by ``_register_mock_agent_tools`` above.


# pragma: no cover — the legacy block is kept as a static, dead-code
# artifact for forensics. Removing it would decouple the audit trail
# from the migration story.
_DEPRECATED_REGISTER_AGENT_TOOLS = """
# DEPRECATED: legacy _register_agent_tools (PR 2 task 2.3)
def _register_agent_tools(registry: AllowList) -> None:
    \"\"\"Legacy UUID-keyed Canvas templates — superseded by
    :func:`_register_mock_agent_tools`. Kept here for forensic
    reference; do not register or invoke it.\"\"\"
    registry.register(
        \"self_user_id\",
        \"SELECT id FROM users WHERE tenant_id = {{tenant_id}} AND deleted_at IS NULL ORDER BY created_at LIMIT 1\",
        slots={\"tenant_id\"},
    )
    registry.register(
        \"get_user_profile\",
        \"SELECT id, name, short_name, email, canvas_id FROM users WHERE tenant_id = {{tenant_id}} AND id = {{user_id}} AND deleted_at IS NULL\",
        slots={\"tenant_id\", \"user_id\"},
    )
    # ... (six more entries truncated; see git history)
"""


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
