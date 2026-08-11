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
    The remaining slots, all of them opaque row identifiers
    (``course_id`` / ``assignment_id``). They arrive as bind parameters,
    are format-checked as UUIDs, and are grounded against the rows the
    tenant actually owns (see :mod:`app.rag.agent`).

Everything else about the query — the tables, the columns, the joins, the
``deleted_at`` guards, the ``tenant_id`` predicate — is code the
developer wrote, registered in :mod:`app.text_to_sql.allow_list`.
"""

from __future__ import annotations

from dataclasses import dataclass

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
# Catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SQLTool:
    """One selectable tool, backed 1:1 by an allow-listed SQL template.

    ``name`` doubles as the allow-list template name, so there is no
    mapping table to drift out of sync: if a tool exists, its SQL was
    registered by hand, and if it was not registered, the tool cannot be
    constructed (see :func:`build_catalog`).
    """

    name: str
    description: str
    args_schema: type[BaseModel]
    server_slots: frozenset[str]

    @property
    def model_slots(self) -> frozenset[str]:
        return frozenset(self.args_schema.model_fields)


_TOOL_SPECS: tuple[tuple[str, str, type[BaseModel], frozenset[str]], ...] = (
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
    for name, description, args_schema, server_slots in _TOOL_SPECS:
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
        )
    return catalog


TOOL_CATALOG: dict[str, SQLTool] = build_catalog()
TOOL_NAMES: tuple[str, ...] = tuple(TOOL_CATALOG)


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
    "TOOL_CATALOG",
    "TOOL_NAMES",
    "AssignmentArgs",
    "CourseArgs",
    "NoArgs",
    "SQLTool",
    "ToolNotAllowed",
    "build_catalog",
    "tool_specs",
]


def _selftest() -> None:
    assert len(TOOL_CATALOG) == 8, TOOL_NAMES
    # No tool may expose a server-owned slot as a model argument.
    for tool in TOOL_CATALOG.values():
        assert not (tool.model_slots & SERVER_SLOTS), tool.name
        assert tool.description.strip()

    from pydantic import ValidationError

    # ``extra="forbid"`` blocks tenant_id smuggling through the args.
    try:
        CourseArgs(course_id="c", tenant_id="other-tenant")  # type: ignore[call-arg]
    except ValidationError:
        pass
    else:
        raise AssertionError("extra args must be rejected")

    # NoArgs tools accept an empty payload and nothing else.
    assert NoArgs().model_dump() == {}
    try:
        NoArgs(course_id="c")  # type: ignore[call-arg]
    except ValidationError:
        pass
    else:
        raise AssertionError("NoArgs must reject any argument")

    specs = tool_specs()
    assert len(specs) == 8
    by_name = {s["name"]: s for s in specs}
    assert by_name["get_user_courses"]["parameters"]["properties"] == {}
    assert by_name["get_course_details"]["parameters"]["required"] == ["course_id"]
    # No declaration may advertise a free-text SQL argument.
    for spec in specs:
        props = spec["parameters"]["properties"]  # type: ignore[index]
        assert "sql" not in props and "query" not in props, spec["name"]


if __name__ == "__main__":
    _selftest()
