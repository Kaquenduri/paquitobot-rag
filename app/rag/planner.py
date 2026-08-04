"""Traduce una pregunta en lenguaje natural a una plantilla SQL permitida.

Este modulo es el eslabon que faltaba entre :mod:`app.rag.router` (que
decide *que tipo* de pregunta es) y :mod:`app.text_to_sql.allow_list`
(que sabe *como* consultarla). El router dice "relational"; el planner
dice "``assignments_due_between`` con esta ventana de fechas".

Decision de diseno: el LLM NO escribe SQL. El planner elige entre
plantillas que ya estan en el allow-list y rellena solo los slots
declarados. Un modelo alucinado no puede producir una consulta nueva,
apenas errarle a la plantilla — y el peor caso es una respuesta
"no encontre nada", nunca una fuga de datos de otro alumno.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "setiembre": 9, "septiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

# Palabras que aparecen en todos los nombres de curso de Tecsup y por lo
# tanto no distinguen nada al buscar ("C24 5to F-A" esta en los 10).
RUIDO = {
    "de", "del", "la", "el", "los", "las", "en", "y", "a", "con", "para",
    "c24", "5to", "1ero", "2do", "3ero", "4to", "f", "e", "aa", "curso",
    "seccion", "sec", "nrc", "2024", "2025", "2026",
}


def normalizar(texto: str) -> str:
    """Minusculas sin tildes, para comparar 'Calculo' con 'cálculo'."""
    sin_tildes = unicodedata.normalize("NFKD", texto or "")
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    return sin_tildes.lower()


def _palabras(texto: str) -> set[str]:
    return {
        palabra
        for palabra in re.findall(r"[a-z0-9]+", normalizar(texto))
        if len(palabra) > 2 and palabra not in RUIDO
    }


@dataclass(frozen=True)
class Plan:
    """Una consulta lista para ejecutar."""

    template: str
    slots: dict[str, Any]
    intent: str
    contexto: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Resolucion de curso
# ---------------------------------------------------------------------------


def cursos_del_tenant(session: Any, tenant_id: Any) -> list[dict[str, Any]]:
    """Devuelve ``[{id, name, course_code}]`` de los cursos del alumno."""
    filas = session.execute(
        text(
            "SELECT id, name, course_code FROM courses "
            "WHERE tenant_id = :tenant_id AND deleted_at IS NULL"
        ),
        {"tenant_id": str(tenant_id)},
    ).mappings().all()
    return [dict(fila) for fila in filas]


def resolver_curso(
    pregunta: str, cursos: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Elige el curso mas parecido a lo que menciona la pregunta.

    Puntua por palabras compartidas entre la pregunta y el nombre o el
    codigo del curso. Sin coincidencias devuelve ``None`` — preferimos
    no responder a responder sobre el curso equivocado.
    """
    tokens = _palabras(pregunta)
    if not tokens:
        return None

    mejor, mejor_puntaje = None, 0
    for curso in cursos:
        candidatos = _palabras(curso.get("name") or "") | _palabras(
            curso.get("course_code") or ""
        )
        puntaje = len(tokens & candidatos)
        # Prefijos en AMBAS direcciones. Solo hacia un lado, "avanzados"
        # (como lo escribe el alumno) no pega con "Avanzado" (como lo
        # escribe Tecsup), los dos cursos de moviles empatan y gana el
        # primero de la lista, que es el equivocado.
        for token in tokens:
            if len(token) < 4:
                continue
            if any(
                c.startswith(token) or (len(c) >= 4 and token.startswith(c))
                for c in candidatos
            ):
                puntaje += 1
        if puntaje > mejor_puntaje:
            mejor, mejor_puntaje = curso, puntaje
    return mejor if mejor_puntaje > 0 else None


# ---------------------------------------------------------------------------
# Ventanas de tiempo
# ---------------------------------------------------------------------------


def resolver_ventana(pregunta: str, ahora: datetime) -> tuple[datetime, datetime, str]:
    """Devuelve ``(inicio, fin, etiqueta)`` segun lo que pida la pregunta."""
    q = normalizar(pregunta)

    if "hoy" in q:
        inicio = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
        return inicio, inicio + timedelta(days=1), "hoy"

    if "manana" in q:
        base = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
        return base + timedelta(days=1), base + timedelta(days=2), "manana"

    if "semana" in q:
        if "pasada" in q or "anterior" in q:
            return ahora - timedelta(days=7), ahora, "la semana pasada"
        return ahora, ahora + timedelta(days=7), "los proximos 7 dias"

    for nombre, numero in MESES.items():
        if nombre in q:
            anio = ahora.year
            inicio = datetime(anio, numero, 1, tzinfo=UTC)
            fin = (
                datetime(anio + 1, 1, 1, tzinfo=UTC)
                if numero == 12
                else datetime(anio, numero + 1, 1, tzinfo=UTC)
            )
            return inicio, fin, nombre

    if "mes" in q:
        inicio = ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        fin = (inicio + timedelta(days=32)).replace(day=1)
        return inicio, fin, "este mes"

    if "ciclo" in q or "todo" in q or "pasado" in q or "vencio" in q:
        return ahora - timedelta(days=365), ahora, "el ultimo ano"

    return ahora, ahora + timedelta(days=7), "los proximos 7 dias"


# ---------------------------------------------------------------------------
# Deteccion de intencion
# ---------------------------------------------------------------------------

RE_VENCIMIENTO = re.compile(
    r"\b(vence|vencen|vencio|vencieron|entrega|entregar|entregas|pendient|"
    r"debo|tengo que|deadline|plazo|fecha|cuando|proxim|examen|parcial|"
    r"laboratorio|lab\b|tarea|trabajo|actividad)"
)
RE_NOTA = re.compile(
    r"\b(nota|notas|promedio|calificacion|calificaciones|puntaje|score|"
    r"como voy|como me fue|rendimiento|desaprob|aprob|jalado)"
)
RE_CONTEO = re.compile(r"\b(cuant|numero de|total de)")
# "semana 2", "sem 2", "lab 3", "laboratorio 03", "s02": en Tecsup el nombre
# de la tarea trae la semana ("PLAB-S02-...").
RE_ITEM = re.compile(r"\b(?:semana|sem|laboratorio|lab)\s*0?(\d{1,2})\b|\bs0?(\d{1,2})\b")
# "todas mis notas de X" pide el detalle, no el promedio.
RE_DETALLE = re.compile(r"\b(todas|detalle|una por una|lista de notas|mis notas)")
# Expresiones de tiempo que por si solas justifican mirar vencimientos.
RE_TIEMPO = re.compile(
    r"\b(hoy|manana|semana|mes|ciclo|"
    + "|".join(MESES)
    + r"|proxim|proximo|pendient|proxima)"
)


def planificar(
    pregunta: str,
    *,
    tenant_id: Any,
    session: Any,
    ahora: datetime | None = None,
) -> Plan | None:
    """Devuelve el :class:`Plan` para ``pregunta`` o ``None`` si no aplica."""
    ahora = ahora or datetime.now(UTC)
    q = normalizar(pregunta)
    cursos = cursos_del_tenant(session, tenant_id)
    curso = resolver_curso(pregunta, cursos)

    hay_nota = bool(RE_NOTA.search(q))
    hay_vencimiento = bool(RE_VENCIMIENTO.search(q))

    # "cuantas tareas tiene X" -> conteo del curso
    if RE_CONTEO.search(q) and curso and not hay_nota:
        return Plan(
            template="course_aggregate",
            slots={"tenant_id": tenant_id, "course_id": curso["id"]},
            intent=f"conteo de tareas del curso {curso['name']}",
            contexto=f"Curso: {curso['name']}",
        )

    # "mi nota de la semana 2 en X" -> esa tarea puntual, no el promedio.
    if hay_nota and curso:
        item = RE_ITEM.search(q)
        if item or RE_DETALLE.search(q):
            if item:
                numero = int(item.group(1) or item.group(2))
                patron = f"%S{numero:02d}%"
                detalle = f"la semana {numero}"
            else:
                patron = "%"
                detalle = "todas las evaluaciones con nota"
            return Plan(
                template="course_assignment_scores",
                slots={
                    "tenant_id": tenant_id,
                    "course_id": curso["id"],
                    "patron": patron,
                },
                intent=f"notas de {detalle} en {curso['name']}",
                contexto=(
                    f"Curso: {curso['name']}. Se pidio {detalle}. Notas "
                    "vigesimales (0-20); 'score' es la nota y "
                    "'points_possible' el maximo de esa tarea. Si DATOS "
                    "viene vacio, esa evaluacion no tiene nota registrada."
                ),
            )

    # "como voy en X" / "mi promedio de X" -> estadisticas de notas
    if hay_nota and curso:
        return Plan(
            template="class_score_statistics",
            slots={"tenant_id": tenant_id, "course_id": curso["id"]},
            intent=f"estadisticas de mis notas en {curso['name']}",
            contexto=(
                f"Curso: {curso['name']}. Las notas son vigesimales (0-20). "
                "scored_count es cuantas evaluaciones ya tienen nota."
            ),
        )

    # "que vence" / "que entregue" -> ventana de fechas. Exigimos una senal
    # academica explicita: sin ella, "cual es el color del sol" terminaria
    # consultando tareas y respondiendo "no encontre nada" en vez de rehusar.
    if hay_vencimiento or (curso is not None) or RE_TIEMPO.search(q):
        inicio, fin, etiqueta = resolver_ventana(pregunta, ahora)
        pasado = fin <= ahora
        return Plan(
            template="assignments_due_between",
            slots={
                "tenant_id": tenant_id,
                "start_at": inicio.isoformat(),
                "end_at": fin.isoformat(),
            },
            intent=f"tareas con vencimiento en {etiqueta}",
            contexto=(
                f"Ventana consultada: {etiqueta} "
                f"({inicio:%d/%m/%Y} a {fin:%d/%m/%Y}). "
                f"Fecha de hoy: {ahora:%d/%m/%Y}. "
                + ("Estas fechas YA PASARON." if pasado else "")
            ),
            extras={"ventana": etiqueta},
        )

    # Nota sin curso identificable: no adivinamos de cual habla.
    return None


__all__ = ["Plan", "planificar", "resolver_curso", "resolver_ventana", "normalizar"]
