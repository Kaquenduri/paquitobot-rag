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


def default_allow_list() -> AllowList:
    registry = AllowList()
    # Incluye el curso y el puntaje: sin el nombre del curso el modelo no
    # puede decir "el Lab 4 de Cloud", solo "el Lab 4".
    registry.register("assignments_due_between", "SELECT a.name, a.due_at, a.points_possible, c.name AS curso, s.score FROM assignments a LEFT JOIN courses c ON c.id = a.course_id LEFT JOIN submissions s ON s.assignment_id = a.id WHERE a.tenant_id = {{tenant_id}} AND a.due_at BETWEEN :start_at AND :end_at AND a.deleted_at IS NULL ORDER BY a.due_at", slots={"tenant_id", "start_at", "end_at"})
    registry.register("assignment_score", "SELECT a.id, a.name, s.score, s.grade FROM assignments a JOIN submissions s ON s.assignment_id = a.id AND s.tenant_id = a.tenant_id WHERE a.tenant_id = {{tenant_id}} AND a.id = :assignment_id AND s.deleted_at IS NULL", slots={"tenant_id", "assignment_id"})
    registry.register("course_aggregate", "SELECT course_id, COUNT(*) AS assignment_count, AVG(points_possible) AS average_points FROM assignments WHERE tenant_id = {{tenant_id}} AND course_id = :course_id AND deleted_at IS NULL GROUP BY course_id", slots={"tenant_id", "course_id"})
    registry.register("submission_status_for_assignment", "SELECT id, workflow_state, submitted_at, late, missing FROM submissions WHERE tenant_id = {{tenant_id}} AND assignment_id = :assignment_id AND deleted_at IS NULL", slots={"tenant_id", "assignment_id"})
    # ``submissions`` no tiene ``course_id``; el curso se alcanza via ``assignments``.
    registry.register("class_score_statistics", "SELECT AVG(s.score) AS average_score, MIN(s.score) AS minimum_score, MAX(s.score) AS maximum_score, COUNT(s.score) AS scored_count FROM submissions s JOIN assignments a ON a.id = s.assignment_id AND a.tenant_id = s.tenant_id WHERE s.tenant_id = {{tenant_id}} AND a.course_id = :course_id AND s.deleted_at IS NULL AND a.deleted_at IS NULL", slots={"tenant_id", "course_id"})
    # Notas tarea por tarea dentro de un curso. Los nombres de Tecsup traen
    # la semana codificada ("PLAB-S02-...", "_MTEO-S02-..."), asi que un
    # patron como '%S02%' responde "mi nota de la semana 2" con exactitud,
    # en vez de devolver el promedio del curso entero.
    # Panorama de cursos: es lo que se le entrega al modelo para que EL
    # resuelva a que curso se refiere el alumno, sin regex de por medio.
    registry.register("courses_overview", "SELECT c.id, c.name, c.course_code, COUNT(a.id) AS tareas, ROUND(AVG(s.score)::numeric, 2) AS promedio, COUNT(s.score) AS evaluadas FROM courses c LEFT JOIN assignments a ON a.course_id = c.id AND a.deleted_at IS NULL LEFT JOIN submissions s ON s.assignment_id = a.id AND s.score IS NOT NULL WHERE c.tenant_id = {{tenant_id}} AND c.deleted_at IS NULL GROUP BY c.id, c.name, c.course_code ORDER BY promedio DESC NULLS LAST", slots={"tenant_id"})
    registry.register("course_assignment_scores", "SELECT a.name, a.due_at, s.score, a.points_possible FROM assignments a JOIN submissions s ON s.assignment_id = a.id AND s.tenant_id = a.tenant_id WHERE a.tenant_id = {{tenant_id}} AND a.course_id = :course_id AND a.name ILIKE :patron AND s.score IS NOT NULL AND a.deleted_at IS NULL AND s.deleted_at IS NULL ORDER BY a.due_at", slots={"tenant_id", "course_id", "patron"})
    return registry


ALLOW_LIST = default_allow_list()


def _selftest() -> None:
    sql = ALLOW_LIST.resolve("assignment_score", {"tenant_id": "safe", "assignment_id": 7})
    assert ":tenant_id" in sql and "assignment_id" in sql
    try:
        ALLOW_LIST.resolve("assignment_score", {"tenant_id": "safe", "evil": "DROP"})
    except TemplateNotAllowed:
        pass
    else:
        raise AssertionError("undeclared slots must be rejected")


if __name__ == "__main__":
    _selftest()
