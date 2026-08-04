"""Cliente de chat contra cualquier proveedor compatible con OpenAI.

El backend no habla con un SDK propietario: habla con
``POST {base_url}/chat/completions``, que es el contrato que exponen
Ollama (via ``/v1``), DeepSeek, Qwen, Kimi y OpenAI. Cambiar de un
modelo local gratis a una API de pago es cambiar ``LLM_BASE_URL``,
``LLM_MODEL`` y ``LLM_API_KEY`` en el entorno — sin tocar codigo.

El cliente NUNCA inventa datos: recibe las filas que devolvio el SQL
acotado y solo las redacta. Si no hay filas, lo dice.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.logging import get_logger

logger = get_logger("app.rag.llm")


SYSTEM_PROMPT = """Eres PaquitoBot, el asistente de los estudiantes de Tecsup.

Reglas que NO puedes romper:
1. Respondes UNICAMENTE con los datos que te paso en DATOS. No inventas
   cursos, tareas, fechas ni notas. Si DATOS viene vacio, dices que no
   encontraste nada para esa consulta y sugieres como reformularla.
2. Las notas de Tecsup son vigesimales (0 a 20). Nunca las conviertas a
   porcentaje ni a otra escala.
3. Hablas en {idioma}, en segunda persona, directo y breve. Nada de
   "como modelo de lenguaje" ni de repetir la pregunta.
4. Maximo 4 oraciones, salvo que listes tareas: ahi usa vinetas con la
   fecha al costado.
5. Si una fecha ya paso, hablas en pasado. No digas "vence" de algo
   que ya vencio.
"""


class LLMUnavailable(RuntimeError):
    """El proveedor de chat no responde."""


def _fmt(value: Any) -> Any:
    """Serializa valores que json no maneja (UUID, datetime, Decimal)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class ChatLLM:
    """Cliente minimo de chat completions."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "ollama",
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    # -- salud -----------------------------------------------------------
    def available(self) -> bool:
        """True si el proveedor contesta el listado de modelos."""
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            return response.status_code < 400
        except httpx.HTTPError:
            return False

    # -- generacion ------------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """Una vuelta de chat. Devuelve el ``message`` crudo del proveedor.

        Cuando se pasan ``tools``, el modelo puede responder con
        ``tool_calls`` en vez de ``content``; el agente ejecuta esas
        llamadas y vuelve a invocar este metodo con los resultados.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise LLMUnavailable("el proveedor de chat no responde") from exc
        if response.status_code >= 400:
            logger.warning("llm_http_error", status_code=response.status_code)
            raise LLMUnavailable(f"el proveedor respondio {response.status_code}")
        try:
            return dict(response.json()["choices"][0]["message"])
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable("respuesta del proveedor ilegible") from exc

    def complete(self, *, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise LLMUnavailable("el proveedor de chat no responde") from exc

        if response.status_code >= 400:
            logger.warning("llm_http_error", status_code=response.status_code)
            raise LLMUnavailable(f"el proveedor respondio {response.status_code}")

        try:
            body = response.json()
            return str(body["choices"][0]["message"]["content"]).strip()
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable("respuesta del proveedor ilegible") from exc

    # -- interfaz que consume RAGService ---------------------------------
    def __call__(
        self,
        rows: Any,
        *,
        lang: str = "es",
        question: str = "",
        intent: str = "",
        contexto: str = "",
    ) -> str:
        idioma = "espanol" if lang.startswith("es") else "ingles"
        limpias = [
            {key: _fmt(value) for key, value in dict(row).items()}
            for row in (rows or [])
            if hasattr(row, "keys") or isinstance(row, dict)
        ]
        partes = [f"PREGUNTA DEL ESTUDIANTE: {question}"]
        if intent:
            partes.append(f"CONSULTA EJECUTADA: {intent}")
        if contexto:
            partes.append(f"CONTEXTO: {contexto}")
        partes.append(
            "DATOS (filas exactas de la base; no agregues ninguna otra):\n"
            + json.dumps(limpias, ensure_ascii=False, indent=1)[:6000]
        )
        return self.complete(
            system=SYSTEM_PROMPT.format(idioma=idioma),
            user="\n\n".join(partes),
        )


def llm_from_settings(settings: Any) -> ChatLLM:
    """Construye el cliente desde Settings."""
    return ChatLLM(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout_seconds,
    )


__all__ = ["ChatLLM", "LLMUnavailable", "SYSTEM_PROMPT", "llm_from_settings"]
