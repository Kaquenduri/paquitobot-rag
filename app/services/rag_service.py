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
    def __init__(self, *, router=None, vector_store=None, sql_executor=None, llm=None):
        self.vector_store = vector_store
        self.sql_executor = sql_executor
        self.llm = llm
        self.router = router or RAGRouter(embedding_available=bool(vector_store and vector_store.provider_health()))

    def provider_health(self) -> dict[str, bool]:
        embedding = bool(self.vector_store and self.vector_store.provider_health())
        self.router.embedding_available = embedding
        return {"embedding_available": embedding}

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
        """
        lang = language or detect_language(question)
        decision = self.router.route(question, language=lang)
        if decision.route == "unsupported":
            return {"answer": bounded_refusal(lang), "lang": lang, "route": decision.route}
        if decision.route == "relational":
            rows = self.sql_executor(tenant_id=tenant_id, sql=sql) if self.sql_executor and sql else []
            answer = self.llm(rows, lang=lang) if self.llm else str(rows)
            return {"answer": answer, "lang": lang, "route": decision.route}
        if not self.vector_store:
            return {"answer": bounded_refusal(lang), "lang": lang, "route": "unsupported"}
        docs = self.vector_store.similarity_search(question, tenant_id=tenant_id, k=20 if decision.route == "hybrid" else 8)
        answer = self.llm(docs, lang=lang) if self.llm else str(docs)
        return {"answer": answer, "lang": lang, "route": decision.route}


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
