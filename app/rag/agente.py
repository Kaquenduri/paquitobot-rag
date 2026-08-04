"""Agente de PaquitoBot: el modelo decide que consultar, no una regex.

El planificador de :mod:`app.rag.planner` traducia la pregunta a una
plantilla con expresiones regulares escritas a mano. Eso se rompe con
la forma real en que escribe un alumno ("moviles avanzados" vs
"Móviles Avanzado", "cuanto saqe", "el lab de la 2"): cada variante
exigia una regla nueva.

Aca el modelo recibe un catalogo de herramientas y decide. Le pasamos
la lista de sus cursos numerada, asi que resolver "moviles avanzados"
al curso correcto es trabajo suyo, no de un regex.

La seguridad no se afloja: cada herramienta esta amarrada a una
plantilla del allow-list, con parametros tipados y acotados al tenant.
El modelo elige QUE preguntar; el SQL sigue siendo codigo nuestro.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.logging import get_logger
from app.rag.llm import ChatLLM, LLMUnavailable
from app.text_to_sql.allow_list import ALLOW_LIST
from app.text_to_sql.executor import execute_readonly

logger = get_logger("app.rag.agente")

MAX_PASOS = 5


# ---------------------------------------------------------------------------
# Catalogo de herramientas (esquema OpenAI, entendido por Ollama, DeepSeek…)
# ---------------------------------------------------------------------------

HERRAMIENTAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "listar_cursos",
            "description": (
                "Reconfirma la lista de cursos. NORMALMENTE NO HACE FALTA: "
                "los cursos con su numero ya estan en SITUACION ACTUAL. "
                "Usala solo si esa lista no aparece."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notas_del_curso",
            "description": (
                "Promedio, nota minima, maxima y cantidad de evaluaciones "
                "calificadas de UN curso. Para 'como voy en X'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "curso": {
                        "type": "integer",
                        "description": "Numero del curso segun listar_cursos.",
                    }
                },
                "required": ["curso"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notas_por_tarea",
            "description": (
                "Nota de cada tarea calificada de un curso. Usa 'filtro' para "
                "acotar: los nombres de Tecsup traen la semana codificada "
                "(PLAB-S02-..., _MTEO-S07-...), asi que filtro='S02' trae la "
                "semana 2. Sin filtro devuelve todas."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "curso": {"type": "integer", "description": "Numero del curso."},
                    "filtro": {
                        "type": "string",
                        "description": "Texto dentro del nombre, ej. 'S02', 'Lab', 'Practica'.",
                    },
                },
                "required": ["curso"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tareas_entre_fechas",
            "description": (
                "Tareas con vencimiento en un rango, con su curso, puntaje y "
                "si ya tienen nota. Para 'que vence', 'que entregue en junio', "
                "'que tengo pendiente'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "desde": {"type": "string", "description": "Fecha inicial AAAA-MM-DD."},
                    "hasta": {"type": "string", "description": "Fecha final AAAA-MM-DD."},
                },
                "required": ["desde", "hasta"],
            },
        },
    },
]


SISTEMA = """Eres PaquitoBot, asistente de estudiantes de Tecsup, Peru.

REGLAS (obligatorias):
1. La lista de cursos con su NUMERO ya esta abajo. No la pidas otra vez.
2. Para responder sobre notas, LLAMA UNA HERRAMIENTA con ese numero.
   Nunca respondas de memoria.
3. PROHIBIDO pedirle datos al alumno. Sus notas las sacas tu de las
   herramientas. El no tiene que decirte nada.
4. PROHIBIDO inventar. Si la herramienta devuelve vacio, dices exacto:
   "no tienes nota registrada en eso".
5. Notas de 0 a 20. Nunca porcentajes. Nunca digas que una nota es 0 si
   la herramienta no lo dice.
6. Español de Peru: "tienes", "sacaste", "tu nota". PROHIBIDO "tenes",
   "vos", "hagaslo".
7. Maximo 3 frases. Sin JSON, sin nombres de campos, sin ids.

8. PROHIBIDO escribir nombres de herramientas en tu respuesta. Las
   herramientas se ejecutan, no se narran. El alumno jamas debe leer
   "notas_por_tarea" ni "curso=3" ni parentesis con parametros.

COMO SE VE UNA BUENA RESPUESTA (solo el texto final, nada mas):
"En la semana 2 sacaste 20 de 20 en el Lab 02."
"Vas en 18.4 de 20 con 17 evaluaciones calificadas."
"No tienes nota registrada en la semana 7 de ese curso."

SITUACION ACTUAL
{panorama}
"""


@dataclass
class Respuesta:
    texto: str
    pasos: list[str] = field(default_factory=list)
    herramientas_usadas: int = 0


# ---------------------------------------------------------------------------
# Ejecucion de herramientas
# ---------------------------------------------------------------------------


def _consultar(session: Any, tenant_id: Any, plantilla: str, slots: dict, limite: int):
    """Consulta y CIERRA la transaccion antes de devolver.

    Entre una herramienta y la siguiente el modelo puede pensar decenas de
    segundos. Si la transaccion queda abierta, Postgres la mata por
    ``idle_in_transaction_session_timeout`` y la request revienta con un
    500. Como todo esto es de solo lectura, un rollback al terminar
    libera la conexion sin perder nada.
    """
    sql = ALLOW_LIST.resolve(plantilla, {"tenant_id": tenant_id, **slots})
    try:
        return execute_readonly(
            session, sql, tenant_id=tenant_id, row_limit=limite, params=slots
        )
    finally:
        session.rollback()


def _limpiar(filas: list[dict]) -> list[dict]:
    """Vuelve las filas serializables y les quita los ids internos."""
    salida = []
    for fila in filas:
        limpia = {}
        for clave, valor in fila.items():
            if clave == "id":
                continue
            if valor is None or isinstance(valor, (str, int, float, bool)):
                limpia[clave] = valor
            else:
                limpia[clave] = str(valor)
        salida.append(limpia)
    return salida


class Herramientas:
    """Ejecuta las herramientas contra la base, acotadas al tenant."""

    def __init__(self, session: Any, tenant_id: Any, *, row_limit: int = 200) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.row_limit = row_limit
        self._cursos: list[dict] | None = None

    # -- catalogo de cursos (numerado para que el modelo no maneje UUIDs) --
    def cursos(self) -> list[dict]:
        if self._cursos is None:
            filas = _consultar(
                self.session, self.tenant_id, "courses_overview", {}, self.row_limit
            )
            self._cursos = [dict(f) for f in filas]
        return self._cursos

    def _uuid_de(self, numero: Any) -> Any:
        # Barrera anti-adivinanza: un modelo chico tiende a inventar el
        # numero de curso sin haber pedido la lista. Si no la pidio, no le
        # damos datos: le devolvemos una instruccion para que la pida.
        if self._cursos is None:
            raise KeyError(
                "Todavia no sabes que cursos existen. Llama primero a "
                "listar_cursos y usa el numero exacto que devuelva."
            )
        try:
            indice = int(numero) - 1
        except (TypeError, ValueError):
            raise KeyError(
                "El numero de curso debe ser un entero de listar_cursos."
            ) from None
        cursos = self.cursos()
        if not 0 <= indice < len(cursos):
            raise KeyError(
                f"No existe el curso {numero}. Los validos van del 1 al "
                f"{len(cursos)}; llama a listar_cursos para verlos."
            )
        return cursos[indice]["id"]

    def listar_cursos(self) -> Any:
        return [
            {
                "curso": i + 1,
                "nombre": c["name"],
                "promedio": float(c["promedio"]) if c["promedio"] is not None else None,
                "evaluadas": c["evaluadas"],
                "tareas": c["tareas"],
            }
            for i, c in enumerate(self.cursos())
        ]

    def _nombre_de(self, numero: Any) -> str:
        return str(self.cursos()[int(numero) - 1]["name"])

    def notas_del_curso(self, curso: Any) -> Any:
        uuid_curso = self._uuid_de(curso)
        filas = _consultar(
            self.session,
            self.tenant_id,
            "class_score_statistics",
            {"course_id": uuid_curso},
            self.row_limit,
        )
        # Devolvemos el curso consultado: si el modelo eligio el numero
        # equivocado, su respuesta nombrara ESE curso y el error se ve.
        # Sin esto puede narrar el nombre correcto con los datos de otro.
        return {"curso_consultado": self._nombre_de(curso), "datos": _limpiar(filas)}

    def notas_por_tarea(self, curso: Any, filtro: str = "") -> Any:
        patron = f"%{filtro}%" if filtro else "%"
        uuid_curso = self._uuid_de(curso)
        filas = _consultar(
            self.session,
            self.tenant_id,
            "course_assignment_scores",
            {"course_id": uuid_curso, "patron": patron},
            self.row_limit,
        )
        return {
            "curso_consultado": self._nombre_de(curso),
            "filtro_aplicado": filtro or "(todas)",
            "datos": _limpiar(filas),
        }

    def tareas_entre_fechas(self, desde: str, hasta: str) -> Any:
        filas = _consultar(
            self.session,
            self.tenant_id,
            "assignments_due_between",
            {"start_at": desde, "end_at": hasta},
            self.row_limit,
        )
        return _limpiar(filas)[:40]

    def despachar(self, nombre: str, argumentos: dict) -> Any:
        metodo = {
            "listar_cursos": self.listar_cursos,
            "notas_del_curso": self.notas_del_curso,
            "notas_por_tarea": self.notas_por_tarea,
            "tareas_entre_fechas": self.tareas_entre_fechas,
        }.get(nombre)
        if metodo is None:
            return {"error": f"herramienta desconocida: {nombre}"}
        try:
            return metodo(**argumentos)
        except KeyError as exc:
            return {"error": str(exc)}
        except TypeError as exc:
            return {"error": f"argumentos invalidos: {exc}"}


# ---------------------------------------------------------------------------
# Panorama inicial: lo que permite ser proactivo sin que el alumno pregunte
# ---------------------------------------------------------------------------


DIAS = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")
MESES_ES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "setiembre", "octubre", "noviembre", "diciembre",
)


def fecha_es(momento: datetime) -> str:
    """Fecha en castellano sin depender del locale del sistema."""
    return (
        f"{DIAS[momento.weekday()]} {momento.day} "
        f"de {MESES_ES[momento.month - 1]} de {momento.year}"
    )


def panorama(herramientas: Herramientas, ahora: datetime | None = None) -> str:
    ahora = ahora or datetime.now(UTC)
    lineas = [f"Hoy es {fecha_es(ahora)}."]
    try:
        cursos = herramientas.listar_cursos()
    except Exception:  # noqa: BLE001 - el panorama nunca debe tumbar la respuesta
        return lineas[0]

    lineas.append(f"El alumno lleva {len(cursos)} cursos:")
    for c in cursos:
        prom = f"{c['promedio']:.1f}/20" if c["promedio"] is not None else "sin notas"
        lineas.append(f"  {c['curso']}. {c['nombre'][:52]} — {prom} ({c['evaluadas']} evaluadas)")

    try:
        proximas = herramientas.tareas_entre_fechas(
            ahora.date().isoformat(), (ahora + timedelta(days=14)).date().isoformat()
        )
    except Exception:  # noqa: BLE001
        proximas = []
    if proximas:
        lineas.append("Vence en los proximos 14 dias:")
        for t in proximas[:6]:
            lineas.append(f"  - {str(t.get('name'))[:52]} ({t.get('curso')}) el {t.get('due_at')}")
    else:
        lineas.append("No tiene ninguna entrega pendiente en los proximos 14 dias.")
    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# El bucle
# ---------------------------------------------------------------------------


class Agente:
    def __init__(self, llm: ChatLLM, herramientas: Herramientas) -> None:
        self.llm = llm
        self.herramientas = herramientas

    def responder(self, pregunta: str, *, historial: list[dict] | None = None) -> Respuesta:
        mensajes: list[dict[str, Any]] = [
            {"role": "system", "content": SISTEMA.format(panorama=panorama(self.herramientas))}
        ]
        mensajes.extend(historial or [])
        mensajes.append({"role": "user", "content": pregunta})

        pasos: list[str] = []
        usadas = 0

        for _ in range(MAX_PASOS):
            # Temperatura baja: queremos que elija bien la herramienta, no
            # que sea creativo. La creatividad la gasta en la redaccion.
            mensaje = self.llm.chat(mensajes, tools=HERRAMIENTAS, temperature=0.1)
            llamadas = mensaje.get("tool_calls") or []
            if not llamadas:
                texto = (mensaje.get("content") or "").strip()
                if not texto:
                    texto = "No pude armar una respuesta con lo que encontre."
                return Respuesta(texto, pasos, usadas)

            mensajes.append(mensaje)
            for llamada in llamadas:
                funcion = llamada.get("function", {})
                nombre = funcion.get("name", "")
                try:
                    argumentos = json.loads(funcion.get("arguments") or "{}")
                except ValueError:
                    argumentos = {}
                resultado = self.herramientas.despachar(nombre, argumentos)
                usadas += 1
                pasos.append(f"{nombre}({', '.join(f'{k}={v}' for k, v in argumentos.items())})")
                logger.info("agente_herramienta", herramienta=nombre)
                mensajes.append(
                    {
                        "role": "tool",
                        "tool_call_id": llamada.get("id", nombre),
                        "name": nombre,
                        "content": json.dumps(resultado, ensure_ascii=False, default=str)[:6000],
                    }
                )

        # Se acabaron los pasos: pedimos el cierre sin herramientas.
        mensajes.append(
            {
                "role": "user",
                "content": "Responde ya con lo que tienes, sin usar mas herramientas.",
            }
        )
        try:
            final = self.llm.chat(mensajes)
            return Respuesta((final.get("content") or "").strip(), pasos, usadas)
        except LLMUnavailable:
            return Respuesta("No pude terminar de armar la respuesta.", pasos, usadas)


__all__ = ["Agente", "HERRAMIENTAS", "Herramientas", "Respuesta", "fecha_es", "panorama"]


def _selftest() -> None:
    """Verifica el contrato de herramientas y el bucle, sin red ni base."""
    # 1. El catalogo cumple el esquema que espera un proveedor OpenAI.
    nombres = set()
    for h in HERRAMIENTAS:
        assert h["type"] == "function"
        f = h["function"]
        assert {"name", "description", "parameters"} <= f.keys()
        assert f["parameters"]["type"] == "object"
        nombres.add(f["name"])
    assert nombres == {
        "listar_cursos",
        "notas_del_curso",
        "notas_por_tarea",
        "tareas_entre_fechas",
    }

    # 2. La fecha sale en castellano sin importar el locale.
    assert fecha_es(datetime(2026, 8, 4, tzinfo=UTC)) == "martes 4 de agosto de 2026"

    # 3. El bucle ejecuta la herramienta y devuelve el texto final.
    class HerramientasFalsas:
        def despachar(self, nombre, argumentos):
            return [{"nombre": "curso demo", "promedio": 18.0}]

    class LLMFalso:
        def __init__(self):
            self.vueltas = 0

        def chat(self, mensajes, tools=None, temperature=0.2):
            self.vueltas += 1
            if self.vueltas == 1:
                return {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "listar_cursos", "arguments": "{}"},
                        }
                    ],
                }
            return {"role": "assistant", "content": "Vas 18 de 20."}

    agente = Agente.__new__(Agente)
    agente.llm = LLMFalso()
    agente.herramientas = HerramientasFalsas()
    # El panorama necesita la base; lo salteamos parcheando el formato.
    import app.rag.agente as modulo

    original = modulo.panorama
    modulo.panorama = lambda *a, **k: "sin datos"
    try:
        r = agente.responder("como voy")
    finally:
        modulo.panorama = original
    assert r.texto == "Vas 18 de 20."
    assert r.herramientas_usadas == 1
    assert r.pasos == ["listar_cursos()"]


if __name__ == "__main__":
    _selftest()
