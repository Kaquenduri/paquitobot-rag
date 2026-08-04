"""RAG orchestration service for PR 5 seams + PR 6 controller wiring.

PR 6 (controller wiring, task 6.1) calls
:meth:`RAGService.answer` directly from the HTTP layer. The method
now accepts an explicit ``language`` keyword so the controller can
pass the language it detected earlier (one detection per request,
rather than one per internal call).
"""

from __future__ import annotations

from typing import Any

from app.rag.prompts import bounded_refusal, detect_language
from app.rag.router import RAGRouter


class RAGService:
    def __init__(
        self,
        *,
        router=None,
        vector_store=None,
        sql_executor=None,
        llm=None,
        planner=None,
        session=None,
        agente=None,
    ):
        self.vector_store = vector_store
        self.sql_executor = sql_executor
        self.llm = llm
        # ``agente`` es el camino principal: el modelo elige que consultar.
        # El planner de regex queda como respaldo si el proveedor cae.
        self.agente = agente
        # ``planner`` traduce la pregunta a (plantilla, slots); ``session``
        # es la sesion con la que se resuelven cursos y se ejecuta el SQL.
        self.planner = planner
        self.session = session
        self.router = router or RAGRouter(embedding_available=bool(vector_store and vector_store.provider_health()))

    def provider_health(self) -> dict[str, bool]:
        embedding = bool(self.vector_store and self.vector_store.provider_health())
        self.router.embedding_available = embedding
        return {"embedding_available": embedding}

    def _redactar(self, rows, *, lang: str, question: str, plan=None) -> str:
        """Pasa las filas al LLM; sin LLM devuelve la representacion cruda."""
        if not self.llm:
            return str(rows)
        return self.llm(
            rows,
            lang=lang,
            question=question,
            intent=getattr(plan, "intent", ""),
            contexto=getattr(plan, "contexto", ""),
        )

    def answer(
        self,
        question: str,
        *,
        tenant_id: Any,
        sql: str | None = None,
        language: str | None = None,
    ):
        """Run a tenant-scoped RAG answer.

        ``language`` is detected once at the controller boundary and
        threaded through to the prompts; ``None`` falls back to
        :func:`app.rag.prompts.detect_language` so the PR 5 seam
        callers keep their default behavior.

        En la ruta relacional el ``planner`` elige una plantilla del
        allow-list y el ``sql_executor`` la ejecuta acotada al tenant.
        Un ``sql`` explicito sigue teniendo prioridad (seam de PR 5).
        """
        lang = language or detect_language(question)

        # Camino principal: el modelo decide que consultar. No pasa por el
        # router de regex, que existe para el respaldo.
        if self.agente is not None and sql is None:
            from app.rag.llm import LLMUnavailable

            try:
                respuesta = self.agente.responder(question)
                return {
                    "answer": respuesta.texto,
                    "lang": lang,
                    "route": "agente",
                    "pasos": respuesta.pasos,
                }
            except LLMUnavailable:
                # El proveedor no responde: seguimos por el camino determinista
                # para no dejar al alumno sin nada.
                pass

        decision = self.router.route(question, language=lang)
        if decision.route == "unsupported":
            return {"answer": bounded_refusal(lang), "lang": lang, "route": decision.route}

        if decision.route == "relational":
            rows: Any = []
            plan = None
            if sql and self.sql_executor:
                rows = self.sql_executor(tenant_id=tenant_id, sql=sql)
            elif self.planner and self.sql_executor:
                plan = self.planner(question, tenant_id=tenant_id, session=self.session)
                if plan is None:
                    return {
                        "answer": bounded_refusal(lang),
                        "lang": lang,
                        "route": "unsupported",
                    }
                rows = self.sql_executor(tenant_id=tenant_id, plan=plan)
            return {
                "answer": self._redactar(rows, lang=lang, question=question, plan=plan),
                "lang": lang,
                "route": decision.route,
            }

        if not self.vector_store:
            return {"answer": bounded_refusal(lang), "lang": lang, "route": "unsupported"}
        docs = self.vector_store.similarity_search(question, tenant_id=tenant_id, k=20 if decision.route == "hybrid" else 8)
        return {
            "answer": self._redactar(docs, lang=lang, question=question),
            "lang": lang,
            "route": decision.route,
        }


def provider_health(service):
    return service.provider_health()


def _selftest() -> None:
    service = RAGService(router=RAGRouter(embedding_available=False))
    # "How many assignments" hits the aggregate deterministic rule.
    assert service.router.route("How many assignments").route == "relational"
    # PR 6: explicit ``language`` is honoured.
    out = service.answer(
        "How many assignments",
        tenant_id="t",
        language="es",
    )
    assert out["lang"] == "es"
    assert out["route"] in {"relational", "semantic", "hybrid", "unsupported"}


if __name__ == "__main__":
    _selftest()
