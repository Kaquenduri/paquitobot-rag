"""Resolucion de curso y ventanas de tiempo.

Estos casos salieron de errores reales vistos en la demo: el bot
respondia con datos de otro curso porque el comparador no distinguia
"Tutoria 5" de "Tutoria 2", ni relacionaba "cloud" con "Nube".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.rag.planner import (
    normalizar,
    resolver_curso,
    resolver_ventana,
)

CURSOS = [
    {"id": "u1", "name": "Aplicaciones Móviles Multiplataforma - C24 5to F-A - C24 5to E-A-A", "course_code": "AppMultiplataforma"},
    {"id": "u2", "name": "Desarrollo de Aplicaciones Web Avanzado - C24 5to F-A - C24 5to E-A-A", "course_code": "DesAplWav"},
    {"id": "u3", "name": "Desarrollo de Soluciones en la Nube - C24 5to F-A - C24 5to E-A-A", "course_code": "DesSolNube"},
    {"id": "u4", "name": "Programación en Móviles Avanzado - C24 5to F-A - C24 5to E-A-A", "course_code": "PrgMovAv"},
    {"id": "u5", "name": "Tutoría 5 - C24 5to F-A - C24 5to E-A-A", "course_code": "Tutoría"},
    {"id": "u6", "name": "Tutoría 2° C24CD 20242", "course_code": "Tutoría 2° C24CD 20242"},
]


def _resolver(pregunta: str) -> str | None:
    curso = resolver_curso(pregunta, CURSOS)
    return None if curso is None else curso["id"]


def test_normalizar_quita_tildes_y_mayusculas() -> None:
    assert normalizar("Programación en Móviles") == "programacion en moviles"


@pytest.mark.parametrize(
    ("pregunta", "esperado"),
    [
        # El plural del alumno contra el singular de Canvas: sin comparar
        # prefijos en ambos sentidos, los dos cursos de moviles empataban
        # y ganaba el primero de la lista.
        ("como voy en moviles avanzados", "u4"),
        ("cuanto saqe en moviles avanzado", "u4"),
        # Traduccion, no falta de ortografia: ninguna comparacion de
        # letras lleva de "cloud" a "Nube".
        ("como voy en cloud", "u3"),
        ("mi promedio en aws", "u3"),
        ("que tal voy en android", "u1"),
        ("notas de backend", "u2"),
        # Estos dos se diferencian SOLO por el numero.
        ("en tutoria 5 que nota tengo", "u5"),
        ("en tutoria 2 como voy", "u6"),
    ],
)
def test_resuelve_el_curso_correcto(pregunta: str, esperado: str) -> None:
    assert _resolver(pregunta) == esperado


def test_el_numero_de_semana_no_se_confunde_con_el_del_curso() -> None:
    """"semana 5" no debe arrastrar la respuesta hacia "Tutoria 5"."""
    assert _resolver("mi nota de la semana 5 en cloud") == "u3"
    assert _resolver("el lab 2 de desarrollo web") == "u2"


def test_sin_curso_mencionado_no_adivina() -> None:
    assert _resolver("cual es el color del sol") is None
    assert _resolver("hola") is None


AHORA = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def test_ventana_de_un_mes_nombrado() -> None:
    inicio, fin, etiqueta = resolver_ventana("que vencio en junio", AHORA)
    assert (inicio.month, fin.month) == (6, 7)
    assert etiqueta == "junio"


def test_ventana_por_defecto_mira_hacia_adelante() -> None:
    inicio, fin, _ = resolver_ventana("que tengo pendiente", AHORA)
    assert inicio == AHORA
    assert (fin - inicio).days == 7


def test_ventana_de_semana_pasada_mira_hacia_atras() -> None:
    inicio, fin, etiqueta = resolver_ventana("que vencio la semana pasada", AHORA)
    assert fin == AHORA
    assert (fin - inicio).days == 7
    assert "pasada" in etiqueta
