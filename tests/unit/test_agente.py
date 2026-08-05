"""Contrato de herramientas, barreras y bucle del agente.

Todo corre sin base de datos y sin modelo: lo que se verifica es que el
agente ejecute las herramientas que pide el modelo, que no entregue
datos cuando el modelo adivina un curso, y que el catalogo cumpla el
esquema que espera un proveedor OpenAI.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.rag.agente import (
    HERRAMIENTAS,
    Agente,
    Herramientas,
    fecha_es,
    nombre_corto,
)


# ---------------------------------------------------------------------------
# Nombres y fechas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        ("Programación en Móviles Avanzado - C24 5to F-A - C24 5to E-A-A", "Programación en Móviles Avanzado"),
        ("Tutoría 5 - C24 5to F-A - C24 5to E-A-A", "Tutoría 5"),
        # Sin sufijo de seccion queda igual.
        ("Tutoría 2° C24CD 20242", "Tutoría 2° C24CD 20242"),
        ("", ""),
    ],
)
def test_nombre_corto_quita_el_sufijo_de_seccion(crudo: str, esperado: str) -> None:
    assert nombre_corto(crudo) == esperado


def test_fecha_en_castellano_sin_depender_del_locale() -> None:
    assert fecha_es(datetime(2026, 8, 4, tzinfo=UTC)) == "martes 4 de agosto de 2026"


# ---------------------------------------------------------------------------
# Catalogo de herramientas
# ---------------------------------------------------------------------------


def test_el_catalogo_cumple_el_esquema_openai() -> None:
    nombres = set()
    for herramienta in HERRAMIENTAS:
        assert herramienta["type"] == "function"
        funcion = herramienta["function"]
        assert {"name", "description", "parameters"} <= funcion.keys()
        assert funcion["parameters"]["type"] == "object"
        nombres.add(funcion["name"])
    assert nombres == {
        "listar_cursos",
        "notas_del_curso",
        "notas_por_tarea",
        "tareas_entre_fechas",
    }


# ---------------------------------------------------------------------------
# Barrera: no entregar datos de un curso adivinado
# ---------------------------------------------------------------------------


class _SesionMuda:
    """Sesion que falla si alguien intenta consultar la base."""

    def execute(self, *_args, **_kwargs):  # pragma: no cover - no debe llamarse
        raise AssertionError("no deberia consultar la base")

    def rollback(self) -> None:  # pragma: no cover
        pass


def test_no_entrega_datos_si_el_modelo_no_listo_los_cursos() -> None:
    """Un modelo chico inventa "curso 3" sin haber mirado la lista."""
    herramientas = Herramientas(_SesionMuda(), "tenant-1")
    resultado = herramientas.despachar("notas_del_curso", {"curso": 3})
    assert "error" in resultado
    assert "listar_cursos" in resultado["error"]


def test_rechaza_un_numero_de_curso_fuera_de_rango() -> None:
    herramientas = Herramientas(_SesionMuda(), "tenant-1")
    herramientas._cursos = [{"id": "u1", "name": "Curso Uno", "promedio": None, "evaluadas": 0, "tareas": 0}]
    resultado = herramientas.despachar("notas_del_curso", {"curso": 9})
    assert "error" in resultado
    assert "9" in resultado["error"]


def test_herramienta_desconocida_no_revienta() -> None:
    herramientas = Herramientas(_SesionMuda(), "tenant-1")
    resultado = herramientas.despachar("borrar_todo", {})
    assert "error" in resultado


# ---------------------------------------------------------------------------
# El bucle
# ---------------------------------------------------------------------------


class _HerramientasFalsas:
    def __init__(self) -> None:
        self.llamadas: list[tuple[str, dict]] = []

    def cursos(self):
        return []

    def despachar(self, nombre: str, argumentos: dict):
        self.llamadas.append((nombre, argumentos))
        return [{"nombre": "Curso Demo", "promedio": 18.0}]


class _ModeloFalso:
    """Pide una herramienta y despues redacta."""

    def __init__(self, guion: list[dict]) -> None:
        self.guion = guion
        self.vueltas = 0

    def chat(self, mensajes, tools=None, temperature=0.2):
        self.vueltas += 1
        return self.guion[min(self.vueltas - 1, len(self.guion) - 1)]


def _sin_panorama(monkeypatch) -> None:
    import app.rag.agente as modulo

    monkeypatch.setattr(modulo, "panorama", lambda *a, **k: "sin datos")


def test_ejecuta_la_herramienta_y_devuelve_el_texto_final(monkeypatch) -> None:
    _sin_panorama(monkeypatch)
    herramientas = _HerramientasFalsas()
    modelo = _ModeloFalso(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "notas_del_curso", "arguments": '{"curso": 1}'}}
                ],
            },
            {"role": "assistant", "content": "Vas en 18 de 20."},
        ]
    )
    respuesta = Agente(modelo, herramientas).responder("como voy")

    assert respuesta.texto == "Vas en 18 de 20."
    assert respuesta.herramientas_usadas == 1
    assert herramientas.llamadas == [("notas_del_curso", {"curso": 1})]


def test_responde_sin_herramientas_cuando_el_modelo_no_las_pide(monkeypatch) -> None:
    _sin_panorama(monkeypatch)
    herramientas = _HerramientasFalsas()
    modelo = _ModeloFalso([{"role": "assistant", "content": "Vas en 18.4 de 20."}])
    respuesta = Agente(modelo, herramientas).responder("como voy en cloud")

    assert respuesta.herramientas_usadas == 0
    assert herramientas.llamadas == []


def test_un_modelo_en_bucle_no_cuelga_la_peticion(monkeypatch) -> None:
    """Si el modelo pide herramientas para siempre, se corta y responde."""
    _sin_panorama(monkeypatch)
    pedir = {
        "role": "assistant",
        "tool_calls": [{"id": "c", "function": {"name": "listar_cursos", "arguments": "{}"}}],
    }
    modelo = _ModeloFalso([pedir, pedir, pedir, pedir, pedir, {"role": "assistant", "content": "Listo."}])
    respuesta = Agente(modelo, _HerramientasFalsas()).responder("hola")

    assert respuesta.texto == "Listo."
    assert respuesta.herramientas_usadas <= 5


def test_argumentos_ilegibles_no_tumban_la_respuesta(monkeypatch) -> None:
    _sin_panorama(monkeypatch)
    herramientas = _HerramientasFalsas()
    modelo = _ModeloFalso(
        [
            {
                "role": "assistant",
                "tool_calls": [{"id": "c1", "function": {"name": "listar_cursos", "arguments": "{no es json"}}],
            },
            {"role": "assistant", "content": "Igual te respondo."},
        ]
    )
    respuesta = Agente(modelo, herramientas).responder("como voy")

    assert respuesta.texto == "Igual te respondo."
    assert herramientas.llamadas == [("listar_cursos", {})]
