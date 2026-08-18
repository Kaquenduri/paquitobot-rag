"""Bounded tool-calling agent over the allow-listed SQL catalog.

The model drives, but on a very short leash. Each turn it may either emit
a final natural-language answer or request one or more tools from
:mod:`app.text_to_sql.tools`. Every request goes through
:func:`_validate_call` before a single row is read, and every result —
success or rejection — goes back as a tool message so the model can
correct itself on the next turn.

What makes this safe is what the model is *structurally unable* to say:

* it names a tool, and the name must be a key of ``TOOL_CATALOG``;
* it fills arguments, and they must validate against that tool's
  ``args_schema``, which is ``extra="forbid"`` and contains no free-text
  field — there is no way to express SQL in this vocabulary at all;
* ``tenant_id`` and ``user_id`` are injected downstream by
  :class:`SQLToolRuntime`, so they cannot be overridden from up here;
* row identifiers must parse as UUIDs *and* be grounded — present in the
  set of ids this tenant actually owns.

The loop is bounded by ``max_steps``; when it runs out the model gets one
final turn with tools withheld, so a confused model degrades into a plain
answer rather than an unbounded query storm.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from app.core.logging import get_logger
from app.text_to_sql.period import current_academic_period
from app.text_to_sql.tools import SERVER_SLOTS, TOOL_CATALOG, SQLTool, tool_specs

logger = get_logger("app.rag.agent")

# One "step" is one model turn that requests tools. Four is enough for the
# deepest legitimate chain in this catalog (list courses -> list that
# course's assignments -> that assignment's details) with a turn to spare.
MAX_TOOL_STEPS = 4

# Rows handed to the model per tool call. The executor already caps at 200;
# this second cap keeps a wide gradebook from crowding out the question.
MAX_ROWS_TO_MODEL = 60

SYSTEM_PROMPT_BASE = (
    "You are PaquitoBot, an assistant that answers a single student's "
    "questions about their own Canvas LMS data.\n"
    "\n"
    "Rules:\n"
    "1. Answer ONLY from data returned by the tools. Never state a grade, "
    "date, course or assignment that a tool did not return. If the tools do "
    "not return anything relevant, clearly state that you do not have the data "
    "for it; be direct and keep your answers simple.\n"
    # "Always respond with courses that belong to the active term. If the user"
    # "asks you about courses and assignments from another term, do not answer.\n"
    "2. The tools are already scoped to the authenticated student. Do not "
    "ask for, and never pass, a user id or tenant id.\n"
    "3. Arguments named `course_id` or `assignment_id` are UUIDs that must "
    "be copied verbatim from an earlier tool result. If you do not have "
    "the id yet, call `get_user_courses` (for a course) or "
    "`get_course_assignments` (for an assignment) first, then call the "
    "tool you actually need. Never guess an id.\n"
    "4. When the student names a course loosely ('cálculo', 'the math "
    "one'), match it against the names returned by `get_user_courses`.\n"
    "5. Detect the language of the student's question and reply in that "
    "same language. Be concise and concrete: cite the actual names, "
    "scores and dates from the tool results.\n"
    "6. Do not mention tools, SQL, tables or ids in your final answer. "
    "Speak to the student about their courses and assignments."
)


def build_system_prompt(today: date | None = None) -> str:
    """Assemble the agent's system prompt with today's temporal context.

    The base prompt is identical for every request. The temporal block
    changes per call so the model knows which academic period to anchor
    on when interpreting "this semester", "this period" or any phrase
    that depends on the calendar. Out-of-cycle dates (January,
    February) say so explicitly so the model does not silently fall
    back to last year's Period 2.
    """
    reference = today or datetime.now(UTC).date()
    period = current_academic_period(reference)
    if period is None:
        temporal_block = (
            "\n\nContexto temporal:\n"
            f"- Hoy es {reference.isoformat()}. Estamos fuera del período "
            "académico (períodos: 1=marzo–julio, 2=agosto–diciembre).\n"
            "- La herramienta `get_user_courses_current_term` no devolverá "
            "cursos ahora mismo; dilo al estudiante en lugar de inventar "
            "un período."
        )
    else:
        year, period_num = period
        temporal_block = (
            "\n\nContexto temporal:\n"
            f"- Hoy es {reference.isoformat()} ({year}, período {period_num} "
            "según calendario académico: 1=marzo–julio, 2=agosto–diciembre).\n"
            "- Para listar los cursos del período actual usa la herramienta "
            "`get_user_courses_current_term`; no filtres tú mismo por el "
            "período a partir del nombre."
        )
    return SYSTEM_PROMPT_BASE + temporal_block + "\n"


# Backwards-compatible alias for callers that still reference the
# module-level constant name.
SYSTEM_PROMPT = build_system_prompt()


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentStep:
    """One attempted tool call, kept for logging and metrics.

    ``args`` holds only the model-supplied arguments; the server-injected
    ``tenant_id`` / ``user_id`` are deliberately absent so a step can be
    logged without leaking tenant identifiers.
    """

    tool: str
    args: dict[str, Any]
    ok: bool
    row_count: int = 0
    error: str | None = None


@dataclass
class AgentResult:
    answer: str
    steps: list[AgentStep] = field(default_factory=list)
    exhausted: bool = False

    @property
    def tools_used(self) -> list[str]:
        return [step.tool for step in self.steps if step.ok]


class AgentUnavailable(RuntimeError):
    """Raised when the agent cannot run at all (no LLM, no tool support)."""


# ---------------------------------------------------------------------------
# Runtime: the only thing allowed to touch the database
# ---------------------------------------------------------------------------


@dataclass
class SQLToolRuntime:
    """Executes a validated tool call and answers grounding questions.

    Both callables are supplied by :mod:`app.services.rag_factory`, which
    owns the database session and the authenticated ``tenant_id``. The
    agent never sees either.

    ``execute`` receives the tool plus the model's arguments and returns
    rows. ``known_ids`` returns the set of ids this tenant owns for a
    given slot (``"course_id"`` / ``"assignment_id"``); it is called
    lazily — only when a call actually carries an id — and is expected to
    cache internally.

    ``tenant_id`` is the authenticated tenant's identifier. The
    ``get_user_courses_current_term`` tool resolves its ``term_pattern``
    slot from ``tenant_id`` together with today's academic period; other
    tools do not need this field but it is always populated so server
    slot builders have a single source of truth.
    """

    execute: Callable[[SQLTool, dict[str, Any]], list[dict[str, Any]]]
    known_ids: Callable[[str], set[str]]
    tenant_id: Any = None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_ID_SLOT_HINT = {
    "course_id": "get_user_courses",
    "assignment_id": "get_course_assignments",
    "course_id_mock": "get_user_mock_courses",
    "assignment_id_mock": "get_mock_course_assignments",
}


class ToolCallRejected(ValueError):
    """A tool call the model must fix. The message is fed back to the model."""


def _validate_call(
    name: str,
    raw_args: Any,
    runtime: SQLToolRuntime,
) -> tuple[SQLTool, dict[str, Any]]:
    """Return ``(tool, args)`` for a legal call, or raise :class:`ToolCallRejected`.

    Rejection messages are written *for the model*: they name the offence
    and the tool it should call to recover, because they are handed back
    as the tool's result and are the model's only chance to self-correct.
    """
    tool = TOOL_CATALOG.get(name)
    if tool is None:
        raise ToolCallRejected(
            f"Unknown tool {name!r}. Choose one of: {', '.join(TOOL_CATALOG)}."
        )

    if raw_args is None:
        raw_args = {}
    if not isinstance(raw_args, dict):
        raise ToolCallRejected("Tool arguments must be a JSON object.")

    # Reported separately from the generic Pydantic error because a model
    # reaching for tenant_id/user_id needs to be told the scoping rule,
    # not just that a key was unexpected.
    smuggled = sorted(set(raw_args) & SERVER_SLOTS)
    if smuggled:
        raise ToolCallRejected(
            f"Arguments {smuggled} are set by the server, not by you. "
            "Call the tool again without them."
        )

    try:
        args = tool.args_schema(**raw_args).model_dump()
    except Exception as exc:
        raise ToolCallRejected(f"Invalid arguments for {name}: {exc}") from exc

    for slot, value in args.items():
        if slot not in _ID_SLOT_HINT:
            continue
        # Format check first: a malformed value against a typed column
        # is a database type error, not an empty result set. The
        # validator dispatch is selected by the tool's ``slot_type``
        # (UUID-by-default, integer for the mock tools).
        if tool.slot_type == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ToolCallRejected(
                    f"{slot}={value!r} is not a valid integer id. Call "
                    f"{_ID_SLOT_HINT[slot]} first and copy an id from its result."
                )
            serialized = str(value)
        else:
            try:
                uuid.UUID(str(value))
            except (ValueError, AttributeError, TypeError) as exc:
                raise ToolCallRejected(
                    f"{slot}={value!r} is not a valid id. Call "
                    f"{_ID_SLOT_HINT[slot]} first and copy an id from its result."
                ) from exc
            serialized = str(value)
        # Then grounding: the id must belong to this student.
        if serialized not in runtime.known_ids(slot):
            raise ToolCallRejected(
                f"{slot}={value!r} does not belong to this student. Call "
                f"{_ID_SLOT_HINT[slot]} and copy an id from its result."
            )

    return tool, args


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------


def _messages_module() -> Any:
    """Import langchain message classes lazily.

    Kept out of module scope so importing :mod:`app.rag.agent` never hard
    depends on langchain being installed — the caller degrades to the
    non-agent path instead of failing app startup.
    """
    try:
        from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise AgentUnavailable("langchain_core is not installed") from exc
    # ``AIMessage`` is not needed: the loop appends the model's replies
    # verbatim rather than constructing them.
    return HumanMessage, SystemMessage, ToolMessage


def _bind_tools(llm: Any) -> Any:
    """Attach the catalog to the model as callable tools.

    Explicit function declarations rather than the Pydantic arg classes:
    the tool name and description live on :class:`SQLTool`, not on the
    schema class, and several tools legitimately share one schema
    (``CourseArgs`` backs three of them).
    """
    if not hasattr(llm, "bind_tools"):
        raise AgentUnavailable("configured LLM does not support tool calling")
    return llm.bind_tools(tool_specs())


def _render_rows(rows: list[dict[str, Any]]) -> str:
    """Serialise rows for the model, truncated and JSON-safe."""
    if not rows:
        return json.dumps({"row_count": 0, "rows": []})
    shown = rows[:MAX_ROWS_TO_MODEL]
    payload: dict[str, Any] = {"row_count": len(rows), "rows": shown}
    if len(rows) > len(shown):
        payload["truncated"] = True
        payload["shown"] = len(shown)
    return json.dumps(payload, default=str, ensure_ascii=False)


def _text_of(message: Any) -> str:
    """Extract plain text from a model reply, tolerating block content."""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip()
    return str(content or "").strip()


def run_sql_agent(
    llm: Any,
    question: str,
    *,
    runtime: SQLToolRuntime,
    max_steps: int = MAX_TOOL_STEPS,
) -> AgentResult:
    """Answer ``question`` by letting the model pick and chain SQL tools.

    Raises :class:`AgentUnavailable` when the environment cannot support a
    tool-calling loop at all; the caller is expected to fall back. Any
    other failure inside the loop is contained: the model is told the tool
    failed and gets to try something else.
    """
    HumanMessage, SystemMessage, ToolMessage = _messages_module()
    bound = _bind_tools(llm)

    messages: list[Any] = [
        SystemMessage(content=build_system_prompt()),
        HumanMessage(content=question),
    ]
    steps: list[AgentStep] = []

    for _ in range(max_steps):
        reply = bound.invoke(messages)
        tool_calls = list(getattr(reply, "tool_calls", None) or [])
        if not tool_calls:
            text = _text_of(reply)
            if text:
                return AgentResult(answer=text, steps=steps)
            # A reply with neither text nor tool calls is a dead turn;
            # nudge the model instead of returning an empty answer.
            messages.append(reply)
            messages.append(
                HumanMessage(
                    content="You returned nothing. Answer the question, or "
                    "call a tool to get the data you need."
                )
            )
            continue

        messages.append(reply)
        for call in tool_calls:
            call_id = call.get("id") or ""
            name = call.get("name") or ""
            try:
                tool, args = _validate_call(name, call.get("args"), runtime)
            except ToolCallRejected as exc:
                steps.append(AgentStep(tool=name, args={}, ok=False, error=str(exc)))
                logger.info("rag_agent_tool_rejected", tool=name, reason=str(exc))
                messages.append(
                    ToolMessage(content=f"ERROR: {exc}", tool_call_id=call_id)
                )
                continue

            try:
                rows = runtime.execute(tool, args)
            except Exception as exc:
                steps.append(
                    AgentStep(
                        tool=name,
                        args=args,
                        ok=False,
                        error=exc.__class__.__name__,
                    )
                )
                logger.exception(
                    "rag_agent_tool_failed",
                    tool=name,
                    error_class=exc.__class__.__name__,
                )
                messages.append(
                    ToolMessage(
                        content="ERROR: that query could not be run. Try a "
                        "different tool or answer without it.",
                        tool_call_id=call_id,
                    )
                )
                continue

            steps.append(AgentStep(tool=name, args=args, ok=True, row_count=len(rows)))
            logger.info("rag_agent_tool_ok", tool=name, row_count=len(rows))
            messages.append(
                ToolMessage(content=_render_rows(rows), tool_call_id=call_id)
            )

    # Steps exhausted: one last turn with tools withheld so the model must
    # answer from whatever it already gathered.
    messages.append(
        HumanMessage(
            content="Stop calling tools. Answer the student's question now "
            "using only the data already gathered above. If it is not "
            "enough, say so plainly."
        )
    )
    try:
        final = llm.invoke(messages)
        text = _text_of(final)
    except Exception as exc:
        logger.exception(
            "rag_agent_final_turn_failed", error_class=exc.__class__.__name__
        )
        text = ""
    return AgentResult(answer=text, steps=steps, exhausted=True)


__all__ = [
    "MAX_ROWS_TO_MODEL",
    "MAX_TOOL_STEPS",
    "SYSTEM_PROMPT_BASE",
    "AgentResult",
    "AgentStep",
    "AgentUnavailable",
    "SQLToolRuntime",
    "ToolCallRejected",
    "build_system_prompt",
    "run_sql_agent",
]


def _selftest() -> None:
    from langchain_core.messages import AIMessage

    from app.core.logging import configure_console_encoding

    # This selftest deliberately exercises paths that call
    # ``logger.exception``; without this the traceback render crashes on a
    # cp1252 Windows console.
    configure_console_encoding()

    course_id_mock = 101
    assignment_id_mock = 42

    class _ScriptedLLM:
        """Replays a fixed sequence of model turns and records what it saw."""

        def __init__(self, turns: list[Any]) -> None:
            self._turns = list(turns)
            self.seen: list[list[Any]] = []
            self.bound_specs: Any = None

        def bind_tools(self, specs: Any) -> _ScriptedLLM:
            self.bound_specs = specs
            return self

        def invoke(self, messages: list[Any]) -> Any:
            self.seen.append(list(messages))
            return self._turns.pop(0) if self._turns else AIMessage(content="done")

    executed: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool: SQLTool, args: dict[str, Any]) -> list[dict[str, Any]]:
        executed.append((tool.name, args))
        if tool.name == "get_user_mock_courses":
            return [{"canvas_mock_id": course_id_mock, "name": "Cálculo I"}]
        if tool.name == "get_user_mock_course_grades":
            return [{"assignment_name": "Parcial", "score": 18.0}]
        return []

    runtime = SQLToolRuntime(
        execute=_execute,
        known_ids=lambda slot: {
            "course_id_mock": {str(course_id_mock)},
            "assignment_id_mock": {str(assignment_id_mock)},
        }.get(slot, set()),
    )

    def _call(name: str, args: dict[str, Any], cid: str = "c1") -> dict[str, Any]:
        return {"name": name, "args": args, "id": cid}

    # 1. Two-step chain: discover the course id, then use it.
    llm = _ScriptedLLM(
        [
            AIMessage(content="", tool_calls=[_call("get_user_mock_courses", {})]),
            AIMessage(
                content="",
                tool_calls=[
                    _call(
                        "get_user_mock_course_grades",
                        {"course_id_mock": course_id_mock},
                        "c2",
                    )
                ],
            ),
            AIMessage(content="Sacaste 18 en el Parcial de Cálculo I."),
        ]
    )
    result = run_sql_agent(llm, "¿cuánto saqué en cálculo?", runtime=runtime)
    assert result.answer.startswith("Sacaste 18"), result.answer
    assert result.tools_used == ["get_user_mock_courses", "get_user_mock_course_grades"]
    assert not result.exhausted
    assert executed[1] == (
        "get_user_mock_course_grades",
        {"course_id_mock": course_id_mock},
    )
    # The catalog reached the model as tool declarations, not free text.
    assert {s["name"] for s in llm.bound_specs} == set(TOOL_CATALOG)

    # 2. A hallucinated id is rejected before any SQL runs, and the model
    #    is told which tool to call to recover.
    executed.clear()
    llm = _ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _call(
                        "get_mock_course_details",
                        {"course_id_mock": 999},
                    )
                ],
            ),
            AIMessage(content="No encuentro ese curso."),
        ]
    )
    result = run_sql_agent(llm, "detalles del curso X", runtime=runtime)
    assert executed == [], executed
    assert result.steps[0].ok is False
    assert "get_user_mock_courses" in (result.steps[0].error or "")

    # 3. Malformed id: rejected on format, never reaches the database.
    executed.clear()
    llm = _ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[_call("get_mock_course_details", {"course_id_mock": "cálculo"})],
            ),
            AIMessage(content="Necesito el identificador."),
        ]
    )
    result = run_sql_agent(llm, "detalles", runtime=runtime)
    assert executed == []
    assert (
        "not a valid" in (result.steps[0].error or "")
        or "is not a valid" in (result.steps[0].error or "")
        or "Invalid arguments" in (result.steps[0].error or "")
    )

    # 4. Smuggling tenant_id through the arguments is refused by name.
    executed.clear()
    llm = _ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _call("get_user_mock_courses", {"tenant_id": "some-other-tenant"})
                ],
            ),
            AIMessage(content="Listo."),
        ]
    )
    result = run_sql_agent(llm, "mis cursos", runtime=runtime)
    assert executed == []
    assert "set by the server" in (result.steps[0].error or "")

    # 5. Unknown tool names are refused with the legal set.
    llm = _ScriptedLLM(
        [
            AIMessage(content="", tool_calls=[_call("drop_all_tables", {})]),
            AIMessage(content="No puedo hacer eso."),
        ]
    )
    result = run_sql_agent(llm, "borra todo", runtime=runtime)
    assert "Unknown tool" in (result.steps[0].error or "")

    # 6. A model that only ever calls tools gets cut off and forced to
    #    answer from what it has.
    looping = _ScriptedLLM(
        [
            AIMessage(content="", tool_calls=[_call("get_user_mock_courses", {})]),
            AIMessage(content="", tool_calls=[_call("get_user_mock_courses", {})]),
            AIMessage(content="", tool_calls=[_call("get_user_mock_courses", {})]),
            AIMessage(content="", tool_calls=[_call("get_user_mock_courses", {})]),
            AIMessage(content="Tienes 1 curso: Cálculo I."),
        ]
    )
    result = run_sql_agent(looping, "mis cursos", runtime=runtime, max_steps=4)
    assert result.exhausted is True
    assert result.answer == "Tienes 1 curso: Cálculo I."
    assert len(result.steps) == 4

    # 7. A tool that raises is contained; the model still answers.
    def _boom(tool: SQLTool, args: dict[str, Any]) -> list[dict[str, Any]]:
        raise RuntimeError("db down")

    llm = _ScriptedLLM(
        [
            AIMessage(content="", tool_calls=[_call("get_user_mock_courses", {})]),
            AIMessage(content="No pude consultar tus cursos ahora."),
        ]
    )
    result = run_sql_agent(
        llm,
        "mis cursos",
        runtime=SQLToolRuntime(execute=_boom, known_ids=lambda slot: set()),
    )
    assert result.steps[0].ok is False
    assert result.steps[0].error == "RuntimeError"
    assert result.answer.startswith("No pude")

    # 8. Row rendering truncates and stays JSON-decodable.
    rendered = json.loads(_render_rows([{"i": n} for n in range(MAX_ROWS_TO_MODEL + 5)]))
    assert rendered["row_count"] == MAX_ROWS_TO_MODEL + 5
    assert len(rendered["rows"]) == MAX_ROWS_TO_MODEL
    assert rendered["truncated"] is True

    # 9. An LLM with no tool support is reported, not silently degraded.
    try:
        run_sql_agent(object(), "hola", runtime=runtime)
    except AgentUnavailable:
        pass
    else:
        raise AssertionError("an LLM without bind_tools must raise AgentUnavailable")


if __name__ == "__main__":
    _selftest()
