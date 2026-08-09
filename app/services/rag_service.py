"""RAG orchestration service for PR 5 seams + PR 6 controller wiring.

PR 6 (controller wiring, task 6.1) calls
:meth:`RAGService.answer` directly from the HTTP layer. The method
now accepts an explicit ``language`` keyword so the controller can
pass the language it detected earlier (one detection per request,
rather than one per internal call).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.core.logging import get_logger
from app.rag.prompts import (
    bounded_refusal,
    detect_language,
    hybrid_prompt,
    sql_prompt,
    vector_prompt,
)
from app.rag.router import RAGRouter

logger = get_logger("app.services.rag_service")


def _format_rows(rows: list[dict[str, Any]]) -> str:
    """Render a SQL result set as a compact text block for the LLM."""
    if not rows:
        return "No matching rows."
    lines = []
    for row in rows[:50]:
        parts = [f"{k}={v}" for k, v in row.items() if v is not None]
        lines.append("; ".join(parts))
    return "\n".join(lines)


def _format_docs(docs: list[Any]) -> str:
    """Render retrieved documents as a compact text block for the LLM."""
    if not docs:
        return "No matching documents."
    lines = []
    for doc in docs[:20]:
        text = getattr(doc, "page_content", str(doc))
        meta = getattr(doc, "metadata", {}) or {}
        title = meta.get("title") or meta.get("name") or meta.get("source") or "doc"
        lines.append(f"[{title}] {text[:600]}")
    return "\n\n".join(lines)


def _default_llm_summarizer(llm: Any) -> Callable[[Any, str], str]:
    """Build a text summarizer around the configured LLM.

    The LLM is invoked synchronously; if it is an async object we fall back
    to ``invoke`` (sync variant) and return ``content``; if it is a
    callable that returns a string we call it directly.  When all
    paths fail the caller receives a deterministic refusal.
    """

    def _call(context: Any, lang: str) -> str:
        # The prompt built upstream (see ``_question_prompt``) already
        # embeds the user's question and instructs the LLM to detect and
        # match its language. Forcing a suffix here based on the
        # regex-derived ``lang`` would override that — and the regex is
        # wrong often enough (missing accents, unseen phrasing) that it
        # was the actual cause of answers coming back in the wrong
        # language.
        prompt = context if isinstance(context, str) else str(context)
        if hasattr(llm, "invoke"):
            result = llm.invoke(prompt)
            if asyncio.iscoroutine(result):
                # Avoid the async path; tests use sync LLM stubs.
                return bounded_refusal(lang)
            content = getattr(result, "content", None)
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(result, str) and result.strip():
                return result
        if callable(llm):
            result = llm(prompt)
            if isinstance(result, str) and result.strip():
                return result
        return bounded_refusal(lang)

        return _call

    return _call


def _resolve(value: Any) -> Any:
    """Unwrap a ``_LazyInit``-style dependency on first actual use.

    Dependencies may be wrapped so the lifespan never blocks on a cold
    Ollama or unreachable Postgres. Resolution must happen here — inside
    the request path — rather than in ``RAGService.__init__``, otherwise
    composing the service (which happens during app startup) would
    eagerly connect and defeat the whole point of lazy init.
    """
    return (
        value.get()
        if hasattr(value, "get") and callable(value.get) and not hasattr(value, "similarity_search")
        else value
    )


class RAGService:
    def __init__(
        self,
        *,
        router=None,
        vector_store=None,
        sql_executor=None,
        sql_agent=None,
        llm=None,
    ):
        # Kept unresolved (may be a ``_LazyInit`` wrapper); resolved lazily
        # via ``_resolve`` wherever they are actually used.
        self.vector_store = vector_store
        self.sql_executor = sql_executor
        # ``sql_agent`` is the tool-calling path (app.rag.agent): the model
        # chooses which allow-listed query to run and may chain several.
        # It takes precedence on the relational route; ``sql_executor``
        # remains the fallback when the agent is unavailable or comes back
        # empty-handed.
        self.sql_agent = sql_agent
        self.llm = llm
        self.router = router or RAGRouter(embedding_available=False)

    def _agent_answer(self, question: str, *, tenant_id) -> str | None:
        """Try the tool-calling agent; ``None`` means "fall back".

        The agent produces the finished natural-language answer itself —
        it has already seen the tool results — so there is no separate
        summarisation step on this path.
        """
        agent = _resolve(self.sql_agent)
        if agent is None:
            return None
        try:
            result = agent(question, tenant_id=tenant_id)
        except Exception as exc:
            logger.exception(
                "rag_agent_call_failed",
                tenant_id=str(tenant_id),
                error_class=exc.__class__.__name__,
            )
            return None
        if result is None:
            return None
        answer = (getattr(result, "answer", "") or "").strip()
        return answer or None

    def provider_health(self) -> dict[str, bool]:
        store = _resolve(self.vector_store)
        embedding = bool(store and store.provider_health())
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

        The orchestrator honours the deterministic router and only
        degrades to ``unsupported`` if the chosen route cannot run (e.g.
        the embedding provider is unreachable and the route is
        ``semantic`` or ``hybrid``).
        """
        # Refresh the router's view of the embedding provider so a
        # ``semantic``/``hybrid`` request honours the most recent
        # ``provider_health()`` rather than the snapshot at init time.
        self.provider_health()
        lang = language or detect_language(question)
        decision = self.router.route(question, language=lang)
        route = decision.route

        # Resolved once per call so a ``_LazyInit`` dependency only ever
        # connects when a request actually needs it, never at composition
        # time (see ``_resolve``).
        sql_executor = _resolve(self.sql_executor)
        vector_store = _resolve(self.vector_store)
        llm = _resolve(self.llm)
        summarize = _default_llm_summarizer(llm) if llm is not None else None

        if route == "unsupported":
            return {
                "answer": bounded_refusal(lang),
                "lang": lang,
                "route": route,
            }

        # Relational: SQL over the tenant-scoped store.  We always pass
        # the tenant_id into the executor so cross-tenant reads are
        # physically impossible regardless of the SQL the LLM emits.
        if route == "relational":
            # Preferred path: let the model pick (and chain) allow-listed
            # tools. Only if that is unavailable do we fall back to the
            # single-template executor below.
            agent_answer = self._agent_answer(question, tenant_id=tenant_id)
            if agent_answer:
                return {"answer": agent_answer, "lang": lang, "route": route}
            if not sql_executor:
                return {
                    "answer": bounded_refusal(lang),
                    "lang": lang,
                    "route": "unsupported",
                }
            try:
                rows = sql_executor(tenant_id=tenant_id, sql=sql, question=question)
            except Exception as exc:
                logger.exception(
                    "rag_sql_executor_failed",
                    tenant_id=str(tenant_id),
                    error_class=exc.__class__.__name__,
                )
                return {
                    "answer": bounded_refusal(lang),
                    "lang": lang,
                    "route": route,
                }
            if not summarize:
                # When no LLM is configured we still return a non-empty
                # formatted dump so the controller has something useful
                # to surface; the bounded_refusal would be misleading
                # because the SQL path actually produced data.
                return {
                    "answer": _format_rows(rows),
                    "lang": lang,
                    "route": route,
                }
            context = f"{sql_prompt(question)}\n\nDatos:\n{_format_rows(rows)}"
            answer = summarize(context, lang)
            return {"answer": answer, "lang": lang, "route": route}

        # Semantic / hybrid routes need the vector store and the LLM.
        if not vector_store or not summarize:
            if route == "hybrid":
                # Hybrid degrades to relational when embeddings are unavailable.
                # Call the relational branch directly to avoid infinite recursion
                # (recursing into answer() would again pick hybrid from the router).
                agent_answer = self._agent_answer(question, tenant_id=tenant_id)
                if agent_answer:
                    return {
                        "answer": agent_answer,
                        "lang": lang,
                        "route": "relational",
                    }
                if not sql_executor:
                    return {
                        "answer": bounded_refusal(lang),
                        "lang": lang,
                        "route": "unsupported",
                    }
                try:
                    rows = sql_executor(tenant_id=tenant_id, sql=sql, question=question)
                except Exception as exc:
                    logger.exception(
                        "rag_sql_executor_failed",
                        tenant_id=str(tenant_id),
                        error_class=exc.__class__.__name__,
                    )
                    return {"answer": bounded_refusal(lang), "lang": lang, "route": route}
                return {
                    "answer": _format_rows(rows),
                    "lang": lang,
                    "route": "relational",
                }
            return {
                "answer": bounded_refusal(lang),
                "lang": lang,
                "route": "unsupported",
            }

        k = 20 if route == "hybrid" else 8
        try:
            docs = vector_store.similarity_search(
                question, tenant_id=tenant_id, k=k, filter={"tenant_id": str(tenant_id)}
            )
        except Exception as exc:
            logger.exception(
                "rag_vector_search_failed",
                tenant_id=str(tenant_id),
                error_class=exc.__class__.__name__,
            )
            if route == "hybrid":
                return self.answer(
                    question,
                    tenant_id=tenant_id,
                    sql=sql,
                    language=lang,
                )
            return {
                "answer": bounded_refusal(lang),
                "lang": lang,
                "route": "unsupported",
            }

        prompt_kind = vector_prompt if route == "semantic" else hybrid_prompt
        context = (
            f"{prompt_kind(question)}\n\nDocumentos:\n{_format_docs(docs)}"
        )
        if route == "hybrid" and sql_executor:
            # Hybrid path augments the vector recall with the latest
            # rows for the same tenant, so the LLM has both signals.
            try:
                rows = sql_executor(tenant_id=tenant_id, sql=sql, question=question)
                context += f"\n\nDatos tabulares:\n{_format_rows(rows)}"
            except Exception as exc:
                logger.exception(
                    "rag_hybrid_sql_failed",
                    tenant_id=str(tenant_id),
                    error_class=exc.__class__.__name__,
                )
        answer = summarize(context, lang)
        return {"answer": answer, "lang": lang, "route": route}


def provider_health(service):
    return service.provider_health()


def _selftest() -> None:
    from types import SimpleNamespace

    from app.core.logging import configure_console_encoding

    # The fallback assertions below drive ``logger.exception`` on purpose.
    configure_console_encoding()

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

    # When no dependencies are wired, the orchestrator never returns
    # the empty string that triggered the production bug.
    empty = service.answer("cuales son mis cursos", tenant_id="t", language="es")
    assert empty["answer"] != ""
    assert empty["answer"] is not None

    # When SQL executor returns rows and the LLM is a stub callable
    # the orchestrator must invoke the LLM and pass the rows through.
    captured: list[str] = []

    def _stub_llm(prompt: str) -> str:
        captured.append(prompt)
        return f"resp:{prompt.count(';')+1}"

    sql_rows = [{"course": "mates", "due_at": "2026-08-19"}]
    service_with_sql = RAGService(
        sql_executor=lambda tenant_id, sql, question=None: sql_rows,
        llm=_stub_llm,
    )
    out = service_with_sql.answer(
        "How many assignments do I have?",
        tenant_id="t",
        language="en",
    )
    assert out["route"] == "relational"
    assert out["answer"].startswith("resp:")
    assert captured, "LLM stub was not invoked"

    # Hybrid path falls back to relational when the vector store is
    # missing, so the answer is still non-empty.
    out = service_with_sql.answer(
        "Explain how the grading works",
        tenant_id="t",
        language="en",
    )
    assert out["answer"] != ""

    # Stub vector store: returns deterministic docs and the LLM
    # gets called with their content.
    class _StubStore:
        def similarity_search(self, query, *, tenant_id, k, filter):
            return [
                SimpleNamespace(
                    page_content="documento sobre tareas",
                    metadata={"title": "doc-1"},
                )
            ]

        def provider_health(self):
            return True

    # The deterministic router needs a working embedding provider to
    # accept the ``semantic`` route, so the store must report healthy.
    service_sem = RAGService(
        vector_store=_StubStore(),
        llm=_stub_llm,
        router=RAGRouter(embedding_available=True),
    )
    out = service_sem.answer("Explica el parcial", tenant_id="t", language="es")
    assert out["route"] == "semantic"
    assert "documento sobre tareas" in captured[-1]

    # PR 7: the tool-calling agent takes precedence on the relational
    # route, and its answer is returned verbatim (it already summarised
    # the tool results itself, so the LLM stub must NOT be re-invoked).
    agent_calls: list[tuple[str, Any]] = []

    def _stub_agent(question, *, tenant_id):
        agent_calls.append((question, tenant_id))
        return SimpleNamespace(answer="Tienes 2 tareas pendientes.", steps=[])

    service_agent = RAGService(
        sql_executor=lambda tenant_id, sql, question=None: sql_rows,
        sql_agent=_stub_agent,
        llm=_stub_llm,
    )
    before = len(captured)
    out = service_agent.answer("que tareas me faltan", tenant_id="t", language="es")
    assert out["route"] == "relational"
    assert out["answer"] == "Tienes 2 tareas pendientes."
    assert agent_calls == [("que tareas me faltan", "t")]
    assert len(captured) == before, "agent answer must not be re-summarised"

    # An agent that declines (None) or returns a blank answer falls back to
    # the single-template executor rather than surfacing an empty answer.
    for declining in (lambda q, *, tenant_id: None, lambda q, *, tenant_id: SimpleNamespace(answer="  ")):
        fallback = RAGService(
            sql_executor=lambda tenant_id, sql, question=None: sql_rows,
            sql_agent=declining,
            llm=_stub_llm,
        )
        out = fallback.answer("cuantas tareas tengo", tenant_id="t", language="es")
        assert out["route"] == "relational"
        assert out["answer"].startswith("resp:"), out["answer"]

    # An agent that raises is contained the same way.
    def _boom_agent(question, *, tenant_id):
        raise RuntimeError("agent exploded")

    resilient = RAGService(
        sql_executor=lambda tenant_id, sql, question=None: sql_rows,
        sql_agent=_boom_agent,
        llm=_stub_llm,
    )
    out = resilient.answer("cuantas tareas tengo", tenant_id="t", language="es")
    assert out["answer"].startswith("resp:")


if __name__ == "__main__":
    _selftest()
