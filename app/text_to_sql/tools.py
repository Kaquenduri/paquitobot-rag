"""Tool catalog exposed to the LLM — one tool per allow-listed SQL template.

This is the contract between the model and the database. The model sees
only what is in this module: a tool *name*, a natural-language
*description*, and a JSON schema of the arguments it may fill. It never
sees SQL, and there is no argument anywhere in these schemas that can
carry SQL.

Three slot categories keep that boundary honest:

``server_slots``
    Filled by the backend from the authenticated request — ``tenant_id``
    (from the JWT dependency chain) and ``user_id`` (derived from
    ``tenant_id`` via the ``self_user_id`` template). These never appear
    in an ``args_schema``, so a model that tries to pass ``tenant_id``
    hits a Pydantic ``extra="forbid"`` error instead of a database.

``model_slots``
    The remaining slots. ``course_id`` / ``assignment_id`` are UUIDs
    (legacy Canvas). The mock tools use ``course_id_mock`` /
    ``assignment_id_mock`` instead and carry integer IDs — see
    :class:`SQLTool` ``slot_type``.

Everything else about the query — the tables, the columns, the joins, the
``deleted_at`` guards, the ``tenant_id`` predicate — is code the
developer wrote, registered in :mod:`app.text_to_sql.allow_list`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.text_to_sql.allow_list import ALLOW_LIST

# Slots the backend always injects itself. Listing them here (rather than
# per tool) makes the invariant checkable: no ``args_schema`` field may
# collide with one of these names.
SERVER_SLOTS = frozenset({"tenant_id", "user_id"})


# ---------------------------------------------------------------------------
# Argument schemas
# ---------------------------------------------------------------------------
#
# ``extra="forbid"`` is the enforcement point for the server/model slot
# split: any key the model invents (``tenant_id``, ``user_id``, ``sql``,
# ``limit``, ...) is a validation error, reported back to the model as a
# tool error so it can retry with a legal call.


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(_StrictArgs):
    """Tools scoped entirely by the authenticated student — no arguments."""


class CourseArgs(_StrictArgs):
    course_id: str = Field(
        description=(
            "The course's `id` (a UUID), copied verbatim from a previous "
            "get_user_courses result. Never invent or guess this value."
        )
    )


class AssignmentArgs(_StrictArgs):
    assignment_id: str = Field(
        description=(
            "The assignment's `id` (a UUID), copied verbatim from a previous "
            "get_course_assignments result. Never invent or guess this value."
        )
    )


# ---------------------------------------------------------------------------
# Mock tool argument schemas (PR 2 / PR 3)
# ---------------------------------------------------------------------------
#
# The mock tools take INTEGER ids (NOT UUIDs) because the canvas-mock-api
# uses an independent INT counter. Strict Pydantic mode rejects
# ``"12"`` and ``True`` at the boundaries, so the model's first attempt
# is denied and the error message tells it to call the listing tool
# first.


class MockCourseArgs(_StrictArgs):
    model_config = ConfigDict(extra="forbid", strict=True)

    course_id_mock: int = Field(
        description=(
            "The course's integer id (canvas_mock_id), copied verbatim from "
            "a previous get_user_mock_courses result. Never invent or guess."
        )
    )


class MockAssignmentArgs(_StrictArgs):
    model_config = ConfigDict(extra="forbid", strict=True)

    assignment_id_mock: int = Field(
        description=(
            "The assignment's integer id (canvas_mock_id), copied verbatim "
            "from a previous get_mock_course_assignments result. Never invent or guess."
        )
    )


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SQLTool:
    """One selectable tool, backed 1:1 by an allow-listed SQL template.

    ``name`` doubles as the allow-list template name, so there is no
    mapping table to drift out of sync: if a tool exists, its SQL was
    registered by hand, and if it was not registered, the tool cannot be
    constructed (see :func:`build_catalog`).

    ``slot_type`` picks the validator the agent uses for the tool's
    model-owned slots. ``"uuid"`` (the default) parses the value as
    ``uuid.UUID``; ``"int"`` parses it as ``int``. The mock tools are
    the first callers of ``"int"`` because the canvas-mock-api uses an
    INT counter, not a UUID.
    """

    name: str
    description: str
    args_schema: type[BaseModel]
    server_slots: frozenset[str]
    slot_type: Literal["uuid", "int"] = "uuid"

    @property
    def model_slots(self) -> frozenset[str]:
        return frozenset(self.args_schema.model_fields)


# ---------------------------------------------------------------------------
# DEPRECATED: legacy _TOOL_SPECS (PR 2 task 2.2)
# ---------------------------------------------------------------------------
# The nine legacy tools (``get_user_profile`` / ``get_user_courses`` / …)
# are no longer bound to the LLM. They stay on disk with ``# DEPRECATED:``
# headers so an operator can grep them for forensics and so the regression
# budget for ``# DEPRECATED:`` lines is independent of the live catalog.
# The catalog itself (``_TOOL_SPECS`` below) contains the nine mock
# tools; the legacy nine exist only as the audit trail captured by
# ``_DEPRECATED_TOOL_SPECS``.

_DEPRECATED_TOOL_SPECS: tuple[tuple[str, str, type[BaseModel], frozenset[str]], ...] = (
    # DEPRECATED: get_user_profile — replaced by get_user_mock_courses / self-profile tooling in PR 3
    (
        "get_user_profile",
        (
            "Obtiene la información básica del perfil del estudiante: nombre "
            "completo, nombre corto, correo electrónico y su ID de Canvas. Úsalo "
            "cuando el usuario pregunte por sus datos personales o de perfil."
        ),
        NoArgs,
        frozenset({"tenant_id", "user_id"}),
    ),
    # DEPRECATED: get_user_courses — replaced by get_user_mock_courses
    (
        "get_user_courses",
        (
            "Lista los cursos en los que el estudiante está matriculado, con "
            "nombre, código, fechas de inicio y fin, y su rol y estado de "
            "matrícula. Úsalo cuando pregunte '¿en qué cursos estoy inscrito?' o "
            "por detalles generales de sus materias. También es la forma de "
            "obtener el course_id que necesitan otras herramientas."
        ),
        NoArgs,
        frozenset({"tenant_id", "user_id"}),
    ),
    # DEPRECATED: get_course_assignments — replaced by get_mock_course_assignments
    (
        "get_course_assignments",
        (
            "Lista todas las tareas, exámenes o entregables de UN curso, con su "
            "descripción, puntaje posible y fecha de entrega. Úsalo cuando "
            "pregunte qué tareas hay en un curso o por las fechas de entrega de "
            "una materia. También es la forma de obtener el assignment_id que "
            "necesitan otras herramientas."
        ),
        CourseArgs,
        frozenset({"tenant_id"}),
    ),
    # DEPRECATED: get_assignment_details — replaced by get_mock_assignment_details
    (
        "get_assignment_details",
        (
            "Detalles a fondo de UNA sola tarea: descripción, puntos posibles, "
            "fechas de entrega, desbloqueo y bloqueo, y su estado. Úsalo cuando "
            "el estudiante tenga dudas sobre los requerimientos, el valor o el "
            "límite de entrega de un trabajo específico."
        ),
        AssignmentArgs,
        frozenset({"tenant_id"}),
    ),
    # DEPRECATED: get_user_course_submissions — replaced by get_user_mock_course_grades
    (
        "get_user_course_submissions",
        (
            "Calificaciones, puntajes y estado de entrega (a tiempo, tarde, no "
            "entregado, dispensado) de todas las tareas del estudiante en UN "
            "curso. Úsalo cuando pregunte por sus notas en una materia o si ya "
            "le revisaron un trabajo."
        ),
        CourseArgs,
        frozenset({"tenant_id", "user_id"}),
    ),
    # DEPRECATED: get_user_missing_submissions — replaced by get_user_missing_mock_assignments
    (
        "get_user_missing_submissions",
        (
            "Encuentra las tareas que el estudiante NO ha entregado, en todos "
            "sus cursos. Úsalo cuando pregunte '¿qué tareas me faltan entregar?' "
            "o '¿tengo trabajos pendientes?'."
        ),
        NoArgs,
        frozenset({"tenant_id", "user_id"}),
    ),
    # DEPRECATED: get_user_late_submissions — replaced by get_user_missing_mock_assignments (no late semantics)
    (
        "get_user_late_submissions",
        (
            "Lista los trabajos que el estudiante sí entregó pero fuera de "
            "tiempo, en todos sus cursos. Úsalo cuando pregunte '¿qué tareas "
            "entregué tarde?' o por penalizaciones por retraso."
        ),
        NoArgs,
        frozenset({"tenant_id", "user_id"}),
    ),
    # DEPRECATED: get_course_details — replaced by get_mock_course_details
    (
        "get_course_details",
        (
            "Información y configuración general de UN curso: código, fechas, "
            "estado y cantidad de alumnos matriculados. Úsalo para consultas "
            "administrativas o generales sobre una sola materia."
        ),
        CourseArgs,
        frozenset({"tenant_id"}),
    ),
    # DEPRECATED: get_user_courses_current_term — replaced by get_user_mock_courses (no per-period pattern)
    (
        "get_user_courses_current_term",
        (
            "Devuelve los cursos del usuario en el período académico actual "
            "(calculado a partir de la fecha del servidor: período 1 = "
            "marzo–julio, período 2 = agosto–diciembre). Úsalo cuando el "
            "estudiante pregunte por los cursos 'de este período', 'del "
            "semestre actual' o 'que estoy cursando ahora'. En enero y "
            "febrero no hay período activo y el resultado es vacío."
        ),
        NoArgs,
        frozenset({"tenant_id", "term_pattern"}),
    ),
)


# ---------------------------------------------------------------------------
# Live mock catalog (PR 2; PR 3 adds the full SQL templates)
# ---------------------------------------------------------------------------


_MOCK_TOOL_SPECS: tuple[tuple[str, str, type[BaseModel], frozenset[str], Literal["uuid", "int"]], ...] = (
    (
        "get_user_mock_courses",
        (
            "Lista los cursos del estudiante en el mock de Paquito, con su "
            "id entero, nombre, código y estado. Úsalo cuando pregunte "
            "¿en qué cursos estoy inscrito? en el entorno de demostración. "
            "También es la forma de obtener el course_id entero que "
            "necesitan otras herramientas de mock."
        ),
        NoArgs,
        frozenset({"tenant_id", "user_id_mock"}),
        "int",
    ),
    (
        "get_mock_course_details",
        (
            "Información y configuración general de UN curso del mock: "
            "id entero, código, fechas, estado y cantidad de alumnos "
            "matriculados. Úsalo para detalles administrativos o generales "
            "sobre una sola materia en el entorno de demostración."
        ),
        MockCourseArgs,
        frozenset({"tenant_id"}),
        "int",
    ),
    (
        "get_mock_course_assignments",
        (
            "Lista todas las tareas, exámenes o entregables de UN curso del "
            "mock, con su descripción, puntaje posible y fecha de entrega. "
            "Úsalo cuando pregunte qué tareas hay en un curso del entorno "
            "de demostración. También es la forma de obtener el "
            "assignment_id entero que necesitan otras herramientas."
        ),
        MockCourseArgs,
        frozenset({"tenant_id"}),
        "int",
    ),
    (
        "get_mock_assignment_details",
        (
            "Detalles a fondo de UNA sola tarea del mock: descripción, "
            "puntos posibles, fecha de entrega, tipo de calificación y "
            "estado. Úsalo cuando el estudiante tenga dudas sobre los "
            "requerimientos, el valor o el límite de entrega de un "
            "trabajo específico del entorno de demostración."
        ),
        MockAssignmentArgs,
        frozenset({"tenant_id"}),
        "int",
    ),
    (
        "get_user_mock_grades",
        (
            "Calificaciones del estudiante en todas las tareas del mock, "
            "en todos sus cursos. Úsalo cuando pregunte por sus notas "
            "globales en el entorno de demostración."
        ),
        NoArgs,
        frozenset({"tenant_id", "user_id_mock"}),
        "int",
    ),
    (
        "get_user_mock_course_grades",
        (
            "Calificaciones del estudiante en UN curso del mock, con "
            "puntaje, nota, fecha y calificador. Úsalo cuando pregunte "
            "por sus notas en una materia del entorno de demostración."
        ),
        MockCourseArgs,
        frozenset({"tenant_id", "user_id_mock"}),
        "int",
    ),
    (
        "get_user_missing_mock_assignments",
        (
            "Tareas del mock que el estudiante NO ha calificado todavía y "
            "cuya fecha de entrega ya pasó, en todos sus cursos. Úsalo "
            "cuando pregunte '¿qué tareas me faltan entregar?' o '¿tengo "
            "trabajos pendientes?' en el entorno de demostración."
        ),
        NoArgs,
        frozenset({"tenant_id", "user_id_mock"}),
        "int",
    ),
    (
        "get_user_attendance",
        (
            "Asistencia del estudiante en todas las sesiones del mock, "
            "con la sesión, fecha y estado (presente o ausente). Úsalo "
            "cuando pregunte por su asistencia o por sesiones pasadas en "
            "el entorno de demostración."
        ),
        NoArgs,
        frozenset({"tenant_id", "user_id_mock"}),
        "int",
    ),
    (
        "get_mock_class_sessions",
        (
            "Sesiones de clase de UN curso del mock, ordenadas por "
            "fecha de inicio, con sus horarios de comienzo y fin. Úsalo "
            "cuando pregunte por los horarios de un curso en el entorno "
            "de demostración."
        ),
        MockCourseArgs,
        frozenset({"tenant_id"}),
        "int",
    ),
)


# ``_TOOL_SPECS`` is the canonical 9-tuple the runtime iterates. The
# 5th element is the ``slot_type`` literal; legacy tuples (now living
# in ``_DEPRECATED_TOOL_SPECS``) default to ``"uuid"`` because their
# slots are UUIDs.
_TOOL_SPECS: tuple[tuple[str, str, type[BaseModel], frozenset[str], Literal["uuid", "int"]], ...] = (
    _MOCK_TOOL_SPECS
)


class ToolNotAllowed(ValueError):
    """Raised when a tool name is outside the catalog."""


def build_catalog() -> dict[str, SQLTool]:
    """Return the catalog, asserting each tool matches its SQL template.

    The checks run at import time on purpose: a tool whose declared
    arguments do not line up with its template's slots is a wiring bug
    that must fail the build, not silently produce a
    ``TemplateNotAllowed`` at request time.
    """
    catalog: dict[str, SQLTool] = {}
    registered = set(ALLOW_LIST.names())
    for name, description, args_schema, server_slots, slot_type in _TOOL_SPECS:
        if name not in registered:
            raise ToolNotAllowed(f"tool {name!r} has no registered SQL template")
        model_slots = frozenset(args_schema.model_fields)
        overlap = model_slots & SERVER_SLOTS
        if overlap:
            raise ToolNotAllowed(
                f"tool {name!r} exposes server-owned slots to the model: {sorted(overlap)}"
            )
        # The template's slot set must be exactly what the backend injects
        # plus what the model may fill — no more, no less.
        template_slots = ALLOW_LIST.template_slots(name)
        if template_slots != server_slots | model_slots:
            raise ToolNotAllowed(
                f"tool {name!r} slots {sorted(server_slots | model_slots)} "
                f"do not match template slots {sorted(template_slots)}"
            )
        catalog[name] = SQLTool(
            name=name,
            description=description,
            args_schema=args_schema,
            server_slots=server_slots,
            slot_type=slot_type,
        )
    return catalog


TOOL_CATALOG: dict[str, SQLTool] = build_catalog()
TOOL_NAMES: tuple[str, ...] = tuple(TOOL_CATALOG)
MOCK_TOOL_NAMES: frozenset[str] = frozenset(TOOL_CATALOG)


def tool_specs() -> list[dict[str, object]]:
    """Render the catalog as OpenAI/Anthropic-style function declarations.

    Consumed by :func:`app.rag.agent.bind_catalog`; kept here so the
    model-facing shape lives next to the catalog it describes.
    """
    specs: list[dict[str, object]] = []
    for tool in TOOL_CATALOG.values():
        schema = tool.args_schema.model_json_schema()
        # ``additionalProperties: false`` is Pydantic's rendering of
        # ``extra="forbid"``; some providers reject it in function
        # declarations, and we enforce it ourselves on the way back in.
        schema.pop("additionalProperties", None)
        schema.pop("title", None)
        specs.append(
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": {
                    "type": "object",
                    "properties": schema.get("properties", {}),
                    "required": schema.get("required", []),
                },
            }
        )
    return specs


__all__ = [
    "SERVER_SLOTS",
    "MOCK_TOOL_NAMES",
    "TOOL_CATALOG",
    "TOOL_NAMES",
    "AssignmentArgs",
    "CourseArgs",
    "MockAssignmentArgs",
    "MockCourseArgs",
    "NoArgs",
    "SQLTool",
    "ToolNotAllowed",
    "build_catalog",
    "tool_specs",
]


def _selftest() -> None:
    assert len(TOOL_CATALOG) == 9, TOOL_NAMES
    # No tool may expose a server-owned slot as a model argument.
    for tool in TOOL_CATALOG.values():
        assert not (tool.model_slots & SERVER_SLOTS), tool.name
        assert tool.description.strip()
        # Every mock tool must declare an integer slot_type.
        assert tool.slot_type == "int", tool.name

    # Forensic proof: the source file must contain at least nine
    # ``# DEPRECATED:`` lines so an operator can grep the legacy
    # catalog without rebuilding the runtime.
    import inspect
    from pathlib import Path

    source = Path(inspect.getsourcefile(_selftest) or __file__).read_text(encoding="utf-8")
    deprecated_count = sum(
        1 for line in source.splitlines() if line.lstrip().startswith("# DEPRECATED")
    )
    assert deprecated_count >= 9, deprecated_count

    from pydantic import ValidationError

    # ``extra="forbid"`` blocks tenant_id smuggling through the args.
    try:
        MockCourseArgs(course_id_mock=12, tenant_id="other-tenant")  # type: ignore[call-arg]
    except ValidationError:
        pass
    else:
        raise AssertionError("extra args must be rejected")

    # Mock integer slots reject strings and booleans at the Pydantic boundary.
    try:
        MockCourseArgs(course_id_mock="12")  # type: ignore[arg-type]
    except ValidationError:
        pass
    else:
        raise AssertionError("MockCourseArgs must reject string course_id_mock")

    try:
        MockCourseArgs(course_id_mock=True)  # type: ignore[arg-type]
    except ValidationError:
        pass
    else:
        raise AssertionError("MockCourseArgs must reject boolean course_id_mock")

    # NoArgs tools accept an empty payload and nothing else.
    assert NoArgs().model_dump() == {}
    try:
        NoArgs(course_id="c")  # type: ignore[call-arg]
    except ValidationError:
        pass
    else:
        raise AssertionError("NoArgs must reject any argument")

    specs = tool_specs()
    assert len(specs) == 9
    by_name = {s["name"]: s for s in specs}
    assert by_name["get_user_mock_courses"]["parameters"]["properties"] == {}
    assert by_name["get_mock_course_details"]["parameters"]["required"] == ["course_id_mock"]
    # No declaration may advertise a free-text SQL argument.
    for spec in specs:
        props = spec["parameters"]["properties"]  # type: ignore[index]
        assert "sql" not in props and "query" not in props, spec["name"]


if __name__ == "__main__":
    _selftest()
