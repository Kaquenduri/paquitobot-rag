"""Build the live RAG service composition for the application lifespan.

The factory wires three production dependencies:

* a :class:`~app.rag.vector_store.VectorStore` backed by PGVector;
* a :class:`~app.text_to_sql.executor.execute_readonly` call site
  bound to a read-only SQLAlchemy session;
* a MiniMax chat model (Anthropic-compatible endpoint) used to redact
  the candidate answer into the final natural-language reply. The model
  accepts the same tool-calling interface as the rest of the LangChain
  ecosystem (``bind_tools``, ``with_structured_output``, ``invoke``).

The factory is lazy by design: it never blocks the lifespan on a
cold Ollama, unreachable Postgres, or MiniMax outage.  Each dependency
is initialised on the first request that needs it and cached afterwards.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any, ClassVar

from app.core.config import Settings
from app.core.db import get_db_session
from app.core.logging import get_logger
from app.observability.metrics import record_rag_request
from app.rag.agent import AgentResult, AgentUnavailable, SQLToolRuntime, run_sql_agent
from app.rag.vector_store import VectorStore
from app.services.rag_service import RAGService
from app.text_to_sql.allow_list import ALLOW_LIST
from app.text_to_sql.executor import execute_readonly
from app.text_to_sql.period import current_academic_period
from app.text_to_sql.template_selector import (
    FALLBACK_SLOTS,
    FALLBACK_TEMPLATE,
    current_term_slots,
    select_template,
)
from app.text_to_sql.tools import SQLTool

logger = get_logger("app.services.rag_service_factory")


class _LazyInit:
    """Thread-safe lazy initialiser for production dependencies."""

    def __init__(self, builder: Any, name: str) -> None:
        self._builder = builder
        self._name = name
        self._value: Any = None
        self._lock = threading.Lock()
        self._resolved = False

    def get(self) -> Any:
        if self._resolved:
            return self._value
        with self._lock:
            if self._resolved:
                return self._value
            try:
                self._value = self._builder()
            except Exception as exc:
                logger.exception(
                    "rag_lazy_init_failed",
                    dependency=self._name,
                    error_class=exc.__class__.__name__,
                )
                self._value = None
            self._resolved = True
        return self._value

    def reset(self) -> None:
        with self._lock:
            self._value = None
            self._resolved = False


def _build_vector_store(settings: Settings, db_session_factory: Any) -> VectorStore | None:
    """Return a PGVector-backed ``VectorStore`` or ``None`` on failure."""
    try:
        from langchain_ollama import OllamaEmbeddings
        from langchain_postgres import PGVector

        embeddings = OllamaEmbeddings(
            base_url=settings.ollama_host,
            model=settings.ollama_embedding_model,
            validate_model_on_init=False,
            client_kwargs={"timeout": 5.0},
            async_client_kwargs={"timeout": 5.0},
        )
        # BUG FIX: Use the settings URL directly. Introspecting db_session_factory()
        # was creating a dangling Session and returning the engine URL which could
        # differ from what PGVector needs. The SUPABASE_DATABASE_URL is always correct.
        connection = settings.supabase_database_url
        store = PGVector(
            embeddings=embeddings,
            connection=connection,
            collection_name="primer_rag_documents",
            use_jsonb=True,
            create_extension=False,  # extension already exists; skip the connection attempt
        )
        return VectorStore(store=store, embedder=embeddings)
    except Exception as exc:
        logger.exception(
            "rag_vector_store_init_failed",
            error_class=exc.__class__.__name__,
        )
        record_rag_request(route="init", lang="n/a", outcome="error")
        return None


def _build_sql_executor(settings: Settings, db_session_factory: Any, llm_dep: Any):
    """Return a closure that runs an allow-listed query against the DB.

    When the caller supplies the raw ``question`` text (the controller
    always does), the closure asks the LLM to pick which allow-listed
    template fits and grounds any id slot against the tenant's real
    courses/assignments (see :mod:`app.text_to_sql.template_selector`) —
    the LLM never writes SQL, it only names one of the five registered
    templates and fills already-declared slots. Without a question, or if
    the LLM/selection fails, this falls back to the always-safe "list
    everything" template so the answer is never empty.
    """

    def _run(tenant_id: Any, sql: str | None, question: str | None = None) -> list[dict[str, Any]]:
        _ = sql  # no current caller passes an explicit template name
        session = db_session_factory()
        try:
            llm = None
            if question:
                llm = llm_dep.get() if hasattr(llm_dep, "get") else llm_dep

            if llm is not None:
                try:
                    courses = execute_readonly(
                        session,
                        ALLOW_LIST.resolve("courses_list", {"tenant_id": str(tenant_id)}),
                        tenant_id=tenant_id,
                    )
                    assignments = execute_readonly(
                        session,
                        ALLOW_LIST.resolve("assignments_list", {"tenant_id": str(tenant_id)}),
                        tenant_id=tenant_id,
                    )
                except Exception as exc:
                    logger.exception(
                        "rag_template_grounding_failed",
                        tenant_id=str(tenant_id),
                        error_class=exc.__class__.__name__,
                    )
                    courses, assignments = [], []
                template, extra_slots = select_template(
                    llm, question, courses=courses, assignments=assignments
                )
            else:
                template, extra_slots = FALLBACK_TEMPLATE, dict(FALLBACK_SLOTS)

            slots: dict[str, Any] = {"tenant_id": str(tenant_id), **extra_slots}
            try:
                rendered = ALLOW_LIST.resolve(template, slots)
            except Exception as exc:
                logger.exception(
                    "rag_sql_allowlist_failed",
                    error_class=exc.__class__.__name__,
                )
                return []
            return execute_readonly(session, rendered, tenant_id=tenant_id, params=extra_slots)
        finally:
            session.close()

    return _run


class SelfUserUnresolved(RuntimeError):
    """The tenant has no ``users`` row, so user-scoped tools cannot run."""


class _TenantToolRuntime:
    """Per-request bridge between the agent and the tenant's own rows.

    Holds one session for the whole agent loop (a handful of small
    ``SELECT``s share a connection rather than opening one each) and is
    the *only* place ``tenant_id`` and ``user_id`` are supplied. The agent
    hands over a validated tool plus the model's arguments; everything
    identifying the caller is added here, below the model's reach.

    Both the self ``user_id`` and the grounding id sets are cached for the
    life of the request: the agent may reference the same course list
    across several turns, and re-reading it every turn would triple the
    query count for no new information.
    """

    _ID_TEMPLATES: ClassVar[dict[str, str]] = {
        "course_id": "courses_list",
        "assignment_id": "assignments_list",
        "course_id_mock": "mock_courses_list",
        "assignment_id_mock": "mock_assignments_list",
    }

    def __init__(self, session: Any, tenant_id: Any) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._user_id: str | None = None
        self._user_id_loaded = False
        self._user_id_mock: int | None = None
        self._user_id_mock_loaded = False
        self._id_cache: dict[str, set[str]] = {}

    def _run_template(self, name: str, extra_slots: dict[str, Any]) -> list[dict[str, Any]]:
        # ``resolve`` needs tenant_id to render ``{{tenant_id}}``; the bind
        # value itself is supplied by ``execute_readonly``, which also sets
        # the transaction-local read-only and tenant guards.
        rendered = ALLOW_LIST.resolve(name, {"tenant_id": str(self._tenant_id), **extra_slots})
        return execute_readonly(
            self._session,
            rendered,
            tenant_id=self._tenant_id,
            params=extra_slots,
        )

    def self_user_id(self) -> str | None:
        """Resolve the tenant's own ``users.id`` — never model-supplied.

        The ``users`` table only ever holds the authenticated student's
        self-profile, so ``tenant_id`` determines this uniquely.
        """
        if self._user_id_loaded:
            return self._user_id
        rows = self._run_template("self_user_id", {})
        self._user_id = str(rows[0]["id"]) if rows else None
        self._user_id_loaded = True
        return self._user_id

    def self_mock_user_id(self) -> int | None:
        """Resolve the tenant's own ``canvas_mock_users.canvas_mock_id``.

        Mirrors :meth:`self_user_id` for the mock catalog — the table
        only ever holds the authenticated student's self-profile, so
        ``tenant_id`` determines this uniquely.
        """
        if self._user_id_mock_loaded:
            return self._user_id_mock
        rows = self._run_template("self_mock_user_id", {})
        self._user_id_mock = int(rows[0]["canvas_mock_id"]) if rows else None
        self._user_id_mock_loaded = True
        return self._user_id_mock

    def execute(self, tool: SQLTool, args: dict[str, Any]) -> list[dict[str, Any]]:
        slots = dict(args)
        # ``get_user_courses_current_term`` derives its only non-tenant
        # slot (``term_pattern``) from the current academic period.
        # Computing it here, in the per-request runtime, keeps the value
        # deterministic against ``date.today()`` even when the same
        # runtime instance is reused across calls.
        if tool.name == "get_user_courses_current_term":
            period = current_academic_period(datetime.now(UTC).date())
            term_slots = current_term_slots(
                SQLToolRuntime(
                    execute=self.execute,
                    known_ids=self.known_ids,
                    tenant_id=self._tenant_id,
                ),
                period,
            )
            slots["term_pattern"] = term_slots["term_pattern"]
        if "user_id" in tool.server_slots:
            user_id = self.self_user_id()
            if user_id is None:
                raise SelfUserUnresolved("tenant has no synced user row")
            slots["user_id"] = user_id
        if "user_id_mock" in tool.server_slots:
            mock_user_id = self.self_mock_user_id()
            if mock_user_id is None:
                raise SelfUserUnresolved("tenant has no mocked user row")
            slots["user_id_mock"] = mock_user_id
        return self._run_template(tool.name, slots)

    def known_ids(self, slot: str) -> set[str]:
        if slot not in self._id_cache:
            rows = self._run_template(self._ID_TEMPLATES[slot], {})
            # Mock grounding templates project their natural key as
            # ``canvas_mock_id``; legacy templates use ``id``. Pick the
            # column by what the template returned rather than
            # hard-coding.
            if rows and "canvas_mock_id" in rows[0]:
                self._id_cache[slot] = {str(row["canvas_mock_id"]) for row in rows}
            else:
                self._id_cache[slot] = {str(row["id"]) for row in rows}
        return self._id_cache[slot]

    def as_runtime(self) -> SQLToolRuntime:
        return SQLToolRuntime(
            execute=self.execute,
            known_ids=self.known_ids,
            tenant_id=self._tenant_id,
        )


def _build_sql_agent(settings: Settings, db_session_factory: Any, llm_dep: Any):
    """Return a closure that answers a question via the tool-calling agent.

    Returns ``None`` (rather than raising) whenever the agent cannot run —
    no LLM configured, a model without tool-calling support, or a failure
    inside the loop — so :class:`RAGService` can fall back to the older
    single-template path and the request still gets an answer.
    """
    _ = settings

    def _run(question: str, *, tenant_id: Any) -> AgentResult | None:
        llm = llm_dep.get() if hasattr(llm_dep, "get") else llm_dep
        if llm is None:
            return None
        session = db_session_factory()
        try:
            runtime = _TenantToolRuntime(session, tenant_id)
            result = run_sql_agent(llm, question, runtime=runtime.as_runtime())
        except AgentUnavailable as exc:
            logger.warning("rag_agent_unavailable", reason=str(exc))
            return None
        except Exception as exc:
            logger.exception(
                "rag_agent_failed",
                tenant_id=str(tenant_id),
                error_class=exc.__class__.__name__,
            )
            return None
        finally:
            session.close()
        logger.info(
            "rag_agent_completed",
            tenant_id=str(tenant_id),
            tools_used=result.tools_used,
            steps=len(result.steps),
            exhausted=result.exhausted,
        )
        return result

    return _run


def _build_llm(settings: Settings) -> Any | None:
    """Return a MiniMax chat model (Anthropic-compatible) or ``None`` on failure.

    MiniMax exposes an Anthropic-compatible endpoint at
    ``https://api.minimax.io/anthropic`` that speaks the same Messages API
    shape as Claude. ``ChatAnthropic`` accepts a ``base_url`` override so we
    reuse the LangChain integration unchanged; ``bind_tools`` and
    ``with_structured_output`` continue to work because they speak the
    Anthropic contract.
    """
    try:
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=settings.minimax_model,
            api_key=settings.minimax_api_key,
            base_url=settings.minimax_base_url,
            temperature=0.0,
            max_tokens=4096,
        )
    except Exception as exc:
        logger.exception(
            "rag_llm_init_failed",
            error_class=exc.__class__.__name__,
        )
        record_rag_request(route="init", lang="n/a", outcome="error")
        return None


def build_rag_service(
    settings: Settings,
    db_session_factory: Any,
) -> RAGService:
    """Compose the production RAG service with lazy dependency initialisation.

    The returned service is fully functional even when Ollama or MiniMax
    are unreachable: each dependency is resolved on the first request
    that needs it, and a transient failure is contained to that
    request.
    """
    vector_store = _LazyInit(
        lambda: _build_vector_store(settings, db_session_factory),
        "vector_store",
    )
    llm = _LazyInit(lambda: _build_llm(settings), "llm")
    sql_executor = _LazyInit(
        lambda: _build_sql_executor(settings, db_session_factory, llm),
        "sql_executor",
    )
    # The agent is the primary relational path; ``sql_executor`` stays wired
    # as the fallback for when the agent is unavailable (see
    # ``_build_sql_agent``).
    sql_agent = _LazyInit(
        lambda: _build_sql_agent(settings, db_session_factory, llm),
        "sql_agent",
    )
    return RAGService(
        vector_store=vector_store,
        sql_executor=sql_executor,
        sql_agent=sql_agent,
        llm=llm,
    )


def _selftest() -> None:
    """Build a RAG service; should never raise even when Ollama/MiniMax are
    unreachable.  Calling ``answer`` is a no-op for the lazy slots until
    the service actually needs them."""
    from app.core.config import Settings
    from app.core.logging import configure_console_encoding

    configure_console_encoding()

    settings = Settings(
        supabase_database_url="postgresql+psycopg://127.0.0.1:1/selftest",
        tenant_token_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        backend_secret="selftest-backend-secret-with-sufficient-length",
        minimax_api_key="selftest-minimax",
        ollama_host="http://127.0.0.1:1",
        canvas_api_base_url="https://canvas.invalid/api/v1",
        google_client_id="selftest.apps.googleusercontent.com",
        canvas_mock_webhook_secret="selftest-canvas-mock-webhook-secret",
    )
    service = build_rag_service(settings, db_session_factory=get_db_session)
    assert service is not None
    # The lazy slots should be present but unresolved at this stage.
    assert isinstance(service.vector_store, _LazyInit)
    assert isinstance(service.sql_executor, _LazyInit)
    assert isinstance(service.sql_agent, _LazyInit)
    assert isinstance(service.llm, _LazyInit)

    # The per-request runtime injects tenant_id/user_id and answers
    # grounding questions without ever consulting the model.
    from app.text_to_sql.tools import TOOL_CATALOG

    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeSession:
        def get_bind(self):
            return None

    def _fake_execute(session, sql, *, tenant_id, params=None, row_limit=200):
        calls.append((sql, dict(params or {})))
        if "FROM canvas_mock_users" in sql and "ORDER BY created_at" in sql:
            return [{"canvas_mock_id": 77}]
        if "FROM canvas_mock_courses" in sql and "ORDER BY canvas_mock_id" in sql:
            return [{"canvas_mock_id": 101}, {"canvas_mock_id": 102}]
        return [{"ok": True}]

    # Patch this module's own globals rather than
    # ``app.services.rag_factory``: under ``python -m`` this code runs as
    # ``__main__``, a distinct module object, and ``_TenantToolRuntime``
    # resolves ``execute_readonly`` from whichever globals it was defined in.
    module_globals = globals()
    original = module_globals["execute_readonly"]
    module_globals["execute_readonly"] = _fake_execute
    try:
        runtime = _TenantToolRuntime(_FakeSession(), "tenant-9")
        # user_id_mock is derived from tenant_id, cached, and passed as a bind param.
        runtime.execute(TOOL_CATALOG["get_user_mock_grades"], {})
        assert calls[-1][1] == {"user_id_mock": 77}
        runtime.execute(TOOL_CATALOG["get_user_missing_mock_assignments"], {})
        assert calls[-1][1] == {"user_id_mock": 77}
        # Two tool calls, but self_mock_user_id was only read once.
        assert sum(1 for sql, _ in calls if "ORDER BY created_at" in sql) == 1

        # Grounding sets come from the mock list templates, cached.
        assert runtime.known_ids("course_id_mock") == {"101", "102"}
        before = len(calls)
        assert runtime.known_ids("course_id_mock") == {"101", "102"}
        assert len(calls) == before, "grounding set must be cached per request"

        # A model-supplied course_id_mock rides along as a bind parameter.
        runtime.execute(TOOL_CATALOG["get_mock_course_details"], {"course_id_mock": 101})
        assert calls[-1][1] == {"course_id_mock": 101}
    finally:
        module_globals["execute_readonly"] = original


if __name__ == "__main__":
    _selftest()
