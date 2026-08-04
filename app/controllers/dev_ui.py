"""UI de prueba local — SOLO desarrollo (``DEV_UI_ENABLED=true``).

Este router existe para poder ver PaquitoBot funcionando sin haber
construido todavia el login real. Emite un JWT de backend sin pedir
credenciales, asi que montarlo en un despliegue publico equivale a
regalar la sesion de un alumno.

Por eso:

- ``app.main`` solo lo monta cuando ``Settings.dev_ui_enabled`` es true
  (por defecto false), y
- el arranque emite un warning ruidoso cuando se monta.

El login de produccion debe reemplazar ``POST /dev/session`` por el
flujo institucional de Tecsup; el resto del frontend no cambia, porque
solo necesita "un JWT en el header Authorization".
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any

import jwt
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.core.logging import get_logger

logger = get_logger("app.controllers.dev_ui")

router = APIRouter(prefix="/dev", tags=["dev"])

_INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Sirve la UI de prueba desde el mismo origen que la API (sin CORS).

    ``no-store`` es imprescindible: sin el, el navegador cachea el HTML y
    los cambios de estilo no se ven aunque el archivo ya este editado.
    """
    return HTMLResponse(
        _INDEX.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@router.post("/session")
async def session_token(
    settings: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    """Emite un JWT de backend para el usuario de prueba."""
    ahora = int(time.time())
    token = jwt.encode(
        {"sub": settings.dev_ui_user, "iat": ahora, "exp": ahora + 8 * 3600},
        settings.backend_secret,
        algorithm="HS256",
    )
    return JSONResponse({"token": token, "user": settings.dev_ui_user})


@router.get("/resumen")
async def resumen(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """Datos ya sincronizados para pintar la pantalla de inicio.

    Es un atajo de desarrollo: en produccion esto deberia ser un
    endpoint propio con su cadena de auth, no leer por ``backend_user_id``.
    """
    tenant = session.execute(
        text("SELECT id FROM tenants WHERE backend_user_id = :uid"),
        {"uid": settings.dev_ui_user},
    ).scalar()
    if tenant is None:
        return JSONResponse({"tenant": None, "cursos": [], "recientes": []})

    cursos: list[dict[str, Any]] = [
        dict(fila)
        for fila in session.execute(
            text(
                """
                SELECT c.name,
                       c.course_code,
                       COUNT(a.id)                        AS tareas,
                       ROUND(AVG(s.score)::numeric, 2)    AS promedio,
                       COUNT(s.score)                     AS evaluadas
                  FROM courses c
                  LEFT JOIN assignments a
                         ON a.course_id = c.id AND a.deleted_at IS NULL
                  LEFT JOIN submissions s
                         ON s.assignment_id = a.id AND s.score IS NOT NULL
                 WHERE c.tenant_id = :tid AND c.deleted_at IS NULL
              GROUP BY c.id, c.name, c.course_code
              ORDER BY promedio DESC NULLS LAST
                """
            ),
            {"tid": tenant},
        )
        .mappings()
        .all()
    ]

    recientes = [
        dict(fila)
        for fila in session.execute(
            text(
                """
                SELECT a.name, a.due_at, a.points_possible, s.score, c.name AS curso
                  FROM assignments a
                  LEFT JOIN submissions s ON s.assignment_id = a.id
                  LEFT JOIN courses c     ON c.id = a.course_id
                 WHERE a.tenant_id = :tid
                   AND a.due_at IS NOT NULL
                   AND a.deleted_at IS NULL
              ORDER BY a.due_at DESC
                 LIMIT 8
                """
            ),
            {"tid": tenant},
        )
        .mappings()
        .all()
    ]

    # Ritmo del ciclo: cuantas entregas cayeron en cada semana. Tecsup
    # organiza todo por semana (los nombres traen "S12", "S16"), asi que
    # la semana es la unidad real de este dominio, no el mes.
    semanas = [
        dict(fila)
        for fila in session.execute(
            text(
                """
                SELECT date_trunc('week', a.due_at) AS semana,
                       COUNT(*)                      AS tareas,
                       COUNT(s.score)                AS con_nota
                  FROM assignments a
                  LEFT JOIN submissions s ON s.assignment_id = a.id
                 WHERE a.tenant_id = :tid
                   AND a.due_at IS NOT NULL
                   AND a.deleted_at IS NULL
                   AND a.due_at > now() - interval '10 months'
              GROUP BY 1
              ORDER BY 1
                """
            ),
            {"tid": tenant},
        )
        .mappings()
        .all()
    ]

    def limpio(fila: dict[str, Any]) -> dict[str, Any]:
        return {
            clave: (valor if valor is None or isinstance(valor, (int, float, str)) else str(valor))
            for clave, valor in fila.items()
        }

    return JSONResponse(
        {
            "tenant": str(tenant),
            "usuario": settings.dev_ui_user,
            "cursos": [limpio(c) for c in cursos],
            "recientes": [limpio(r) for r in recientes],
            "semanas": [limpio(s) for s in semanas],
        }
    )


__all__ = ["router"]
