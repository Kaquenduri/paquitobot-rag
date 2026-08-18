"""End-to-end tests for the tool-calling SQL agent against a real database.

The module selftests cover the loop's control flow with a fake runtime.
These tests go the other way: a scripted model, but a genuine SQLite
database seeded with two tenants, so the assertions are about the SQL that
actually runs — the joins, the ``deleted_at`` guards, and above all the
tenant boundary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from app.models import Assignment, Course, Enrollment, Submission, User
from app.rag.agent import AgentUnavailable, run_sql_agent
from app.services.rag_factory import SelfUserUnresolved, _TenantToolRuntime
from app.text_to_sql.tools import TOOL_CATALOG

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Ids:
    """Stable UUIDs so scripted tool calls can reference them literally."""

    tenant_a = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
    tenant_b = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000001")
    user_a = uuid.UUID("11111111-0000-4000-8000-000000000001")
    user_b = uuid.UUID("11111111-0000-4000-8000-000000000002")
    course_calc = uuid.UUID("22222222-0000-4000-8000-000000000001")
    course_gone = uuid.UUID("22222222-0000-4000-8000-000000000002")
    course_b = uuid.UUID("22222222-0000-4000-8000-000000000003")
    asg_parcial = uuid.UUID("33333333-0000-4000-8000-000000000001")
    asg_final = uuid.UUID("33333333-0000-4000-8000-000000000002")
    asg_b = uuid.UUID("33333333-0000-4000-8000-000000000003")


def _dt(month: int, day: int, hour: int = 12) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=UTC)


@pytest.fixture
def seeded_session(db_session: Any) -> Any:
    """Two tenants with overlapping-looking data, plus one soft-deleted course."""
    s = db_session

    s.add_all(
        [
            User(
                id=_Ids.user_a,
                tenant_id=_Ids.tenant_a,
                canvas_id=901,
                name="Ana Torres",
                short_name="Ana",
                email="ana@tecsup.edu.pe",
            ),
            User(
                id=_Ids.user_b,
                tenant_id=_Ids.tenant_b,
                canvas_id=902,
                name="Otro Alumno",
                short_name="Otro",
                email="otro@tecsup.edu.pe",
            ),
            Course(
                id=_Ids.course_calc,
                tenant_id=_Ids.tenant_a,
                canvas_id=501,
                name="Cálculo I",
                course_code="CALC-1",
                enrollments_count=30,
            ),
            # Soft-deleted: must never appear in any tool result.
            Course(
                id=_Ids.course_gone,
                tenant_id=_Ids.tenant_a,
                canvas_id=502,
                name="Curso Archivado",
                course_code="OLD-1",
                deleted_at=datetime(2020, 1, 1, tzinfo=UTC),
            ),
            Course(
                id=_Ids.course_b,
                tenant_id=_Ids.tenant_b,
                canvas_id=503,
                name="Curso Del Otro Tenant",
                course_code="OTHER-1",
            ),
        ]
    )
    s.flush()

    s.add_all(
        [
            Enrollment(
                tenant_id=_Ids.tenant_a,
                canvas_id=601,
                course_id=_Ids.course_calc,
                user_id=_Ids.user_a,
                role="StudentEnrollment",
                enrollment_state="active",
            ),
            Enrollment(
                tenant_id=_Ids.tenant_a,
                canvas_id=602,
                course_id=_Ids.course_gone,
                user_id=_Ids.user_a,
                role="StudentEnrollment",
                enrollment_state="active",
            ),
            Enrollment(
                tenant_id=_Ids.tenant_b,
                canvas_id=603,
                course_id=_Ids.course_b,
                user_id=_Ids.user_b,
                role="StudentEnrollment",
                enrollment_state="active",
            ),
            Assignment(
                id=_Ids.asg_parcial,
                tenant_id=_Ids.tenant_a,
                canvas_id=701,
                course_id=_Ids.course_calc,
                name="Parcial",
                points_possible=20.0,
                due_at=_dt(5, 1),
            ),
            Assignment(
                id=_Ids.asg_final,
                tenant_id=_Ids.tenant_a,
                canvas_id=702,
                course_id=_Ids.course_calc,
                name="Final",
                points_possible=20.0,
                due_at=_dt(7, 1),
            ),
            Assignment(
                id=_Ids.asg_b,
                tenant_id=_Ids.tenant_b,
                canvas_id=703,
                course_id=_Ids.course_b,
                name="Tarea Ajena",
                points_possible=20.0,
                due_at=_dt(5, 1),
            ),
        ]
    )
    s.flush()

    s.add_all(
        [
            Submission(
                tenant_id=_Ids.tenant_a,
                canvas_id=801,
                assignment_id=_Ids.asg_parcial,
                user_id=_Ids.user_a,
                score=18.0,
                grade="18",
                submitted_at=_dt(4, 30, 10),
                late=False,
                missing=False,
            ),
            Submission(
                tenant_id=_Ids.tenant_a,
                canvas_id=802,
                assignment_id=_Ids.asg_final,
                user_id=_Ids.user_a,
                score=None,
                grade=None,
                submitted_at=None,
                late=False,
                missing=True,
            ),
            Submission(
                tenant_id=_Ids.tenant_b,
                canvas_id=803,
                assignment_id=_Ids.asg_b,
                user_id=_Ids.user_b,
                score=5.0,
                grade="5",
                late=True,
                missing=False,
            ),
        ]
    )
    s.commit()
    return s


@pytest.fixture
def runtime_a(seeded_session: Any) -> _TenantToolRuntime:
    return _TenantToolRuntime(seeded_session, _Ids.tenant_a)


class ScriptedLLM:
    """Replays fixed model turns; records the tool declarations it received."""

    def __init__(self, turns: list[AIMessage]) -> None:
        self._turns = list(turns)
        self.bound_specs: Any = None
        self.invocations = 0

    def bind_tools(self, specs: Any) -> ScriptedLLM:
        self.bound_specs = specs
        return self

    def invoke(self, messages: list[Any]) -> AIMessage:
        self.invocations += 1
        return self._turns.pop(0) if self._turns else AIMessage(content="fin")


def call(name: str, args: dict[str, Any] | None = None, cid: str = "c1") -> dict[str, Any]:
    return {"name": name, "args": args or {}, "id": cid}


# ---------------------------------------------------------------------------
# Runtime: server-injected scoping
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="legacy self_user_id template deprecated; replaced by canvas-mock catalog")
def test_self_user_id_is_derived_from_tenant(runtime_a: _TenantToolRuntime) -> None:
    assert runtime_a.self_user_id() == str(_Ids.user_a)


@pytest.mark.skip(reason="legacy tool deprecated; replaced by canvas-mock catalog")
def test_profile_tool_returns_only_the_tenants_own_user(
    runtime_a: _TenantToolRuntime,
) -> None:
    rows = runtime_a.execute(TOOL_CATALOG["get_user_profile"], {})
    assert len(rows) == 1
    assert rows[0]["name"] == "Ana Torres"
    assert rows[0]["email"] == "ana@tecsup.edu.pe"


@pytest.mark.skip(reason="legacy tool deprecated; replaced by canvas-mock catalog")
def test_courses_tool_excludes_soft_deleted_and_other_tenants(
    runtime_a: _TenantToolRuntime,
) -> None:
    rows = runtime_a.execute(TOOL_CATALOG["get_user_courses"], {})
    names = {row["name"] for row in rows}
    assert names == {"Cálculo I"}
    assert "Curso Archivado" not in names
    assert "Curso Del Otro Tenant" not in names


def test_grounding_sets_exclude_other_tenants(runtime_a: _TenantToolRuntime) -> None:
    course_ids = runtime_a.known_ids("course_id")
    assignment_ids = runtime_a.known_ids("assignment_id")
    assert str(_Ids.course_b) not in course_ids
    assert str(_Ids.asg_b) not in assignment_ids
    assert str(_Ids.course_calc) in course_ids
    assert {str(_Ids.asg_parcial), str(_Ids.asg_final)} <= assignment_ids


@pytest.mark.skip(reason="legacy tool deprecated; replaced by canvas-mock catalog")
def test_missing_submissions_tool_finds_only_the_missing_one(
    runtime_a: _TenantToolRuntime,
) -> None:
    rows = runtime_a.execute(TOOL_CATALOG["get_user_missing_submissions"], {})
    assert [r["assignment_name"] for r in rows] == ["Final"]
    assert rows[0]["course_name"] == "Cálculo I"


@pytest.mark.skip(reason="legacy tool deprecated; replaced by canvas-mock catalog")
def test_course_submissions_tool_joins_scores(runtime_a: _TenantToolRuntime) -> None:
    rows = runtime_a.execute(
        TOOL_CATALOG["get_user_course_submissions"],
        {"course_id": str(_Ids.course_calc)},
    )
    by_name = {r["assignment_name"]: r for r in rows}
    assert by_name["Parcial"]["score"] == 18.0
    # Truthiness rather than ``is True``: SQLite returns 1/0 for booleans
    # where Postgres returns True/False, and this SQL runs on both.
    assert by_name["Final"]["missing"]
    assert not by_name["Parcial"]["missing"]


@pytest.mark.skip(reason="legacy tool deprecated; replaced by canvas-mock catalog")
def test_cross_tenant_course_id_returns_no_rows(runtime_a: _TenantToolRuntime) -> None:
    """Defence in depth: even if grounding were bypassed, SQL yields nothing."""
    rows = runtime_a.execute(
        TOOL_CATALOG["get_course_details"], {"course_id": str(_Ids.course_b)}
    )
    assert rows == []


@pytest.mark.skip(reason="legacy tool deprecated; replaced by canvas-mock catalog")
def test_user_scoped_tool_raises_when_tenant_has_no_user(
    seeded_session: Any,
) -> None:
    orphan = _TenantToolRuntime(seeded_session, uuid.uuid4())
    with pytest.raises(SelfUserUnresolved):
        orphan.execute(TOOL_CATALOG["get_user_missing_submissions"], {})


# ---------------------------------------------------------------------------
# Agent loop over the real database
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="legacy tool chain deprecated; replaced by mock-chain")
def test_agent_chains_course_lookup_then_grades(runtime_a: _TenantToolRuntime) -> None:
    """The canonical two-step: discover the course id, then use it."""
    llm = ScriptedLLM(
        [
            AIMessage(content="", tool_calls=[call("get_user_courses")]),
            AIMessage(
                content="",
                tool_calls=[
                    call(
                        "get_user_course_submissions",
                        {"course_id": str(_Ids.course_calc)},
                        "c2",
                    )
                ],
            ),
            AIMessage(content="En Cálculo I sacaste 18 en el Parcial."),
        ]
    )
    result = run_sql_agent(llm, "¿cómo voy en cálculo?", runtime=runtime_a.as_runtime())

    assert result.tools_used == ["get_user_courses", "get_user_course_submissions"]
    assert result.answer == "En Cálculo I sacaste 18 en el Parcial."
    assert not result.exhausted
    assert all(step.ok for step in result.steps)
    assert result.steps[1].row_count == 2


def test_agent_receives_the_whole_catalog_as_declarations(
    runtime_a: _TenantToolRuntime,
) -> None:
    llm = ScriptedLLM([AIMessage(content="hola")])
    run_sql_agent(llm, "hola", runtime=runtime_a.as_runtime())

    assert {spec["name"] for spec in llm.bound_specs} == set(TOOL_CATALOG)
    # No declaration may offer the model a way to express SQL or scoping.
    for spec in llm.bound_specs:
        props = set(spec["parameters"]["properties"])
        assert props <= {"course_id", "assignment_id", "course_id_mock", "assignment_id_mock"}, spec["name"]


@pytest.mark.skip(reason="legacy tool deprecated; replaced by canvas-mock catalog")
def test_agent_rejects_cross_tenant_id_before_touching_the_database(
    runtime_a: _TenantToolRuntime,
) -> None:
    """A real id from another tenant is refused by grounding, not by luck."""
    llm = ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    call("get_course_details", {"course_id": str(_Ids.course_b)})
                ],
            ),
            AIMessage(content="No tengo ese curso."),
        ]
    )
    result = run_sql_agent(llm, "detalles de ese curso", runtime=runtime_a.as_runtime())

    step = result.steps[0]
    assert step.ok is False
    assert "does not belong to this student" in (step.error or "")
    assert "get_user_courses" in (step.error or "")


@pytest.mark.skip(reason="legacy tool deprecated; replaced by canvas-mock catalog")
def test_agent_rejects_soft_deleted_course_id(runtime_a: _TenantToolRuntime) -> None:
    llm = ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    call("get_course_details", {"course_id": str(_Ids.course_gone)})
                ],
            ),
            AIMessage(content="Ese curso no está disponible."),
        ]
    )
    result = run_sql_agent(llm, "curso archivado", runtime=runtime_a.as_runtime())
    assert result.steps[0].ok is False


@pytest.mark.skip(reason="legacy tool chain deprecated; replaced by mock-chain")
def test_agent_recovers_after_a_rejected_call(runtime_a: _TenantToolRuntime) -> None:
    """The rejection is fed back, and the next turn succeeds."""
    llm = ScriptedLLM(
        [
            AIMessage(
                content="", tool_calls=[call("get_course_details", {"course_id": "calculo"})]
            ),
            AIMessage(content="", tool_calls=[call("get_user_courses", {}, "c2")]),
            AIMessage(
                content="",
                tool_calls=[
                    call("get_course_details", {"course_id": str(_Ids.course_calc)}, "c3")
                ],
            ),
            AIMessage(content="Cálculo I tiene 30 matriculados."),
        ]
    )
    result = run_sql_agent(llm, "cuantos alumnos hay en calculo", runtime=runtime_a.as_runtime())

    assert [s.ok for s in result.steps] == [False, True, True]
    assert result.answer == "Cálculo I tiene 30 matriculados."


@pytest.mark.skip(reason="legacy tool deprecated; replaced by canvas-mock catalog")
def test_agent_refuses_server_owned_arguments(runtime_a: _TenantToolRuntime) -> None:
    llm = ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    call(
                        "get_user_courses",
                        {"tenant_id": str(_Ids.tenant_b), "user_id": str(_Ids.user_b)},
                    )
                ],
            ),
            AIMessage(content="Estos son tus cursos."),
        ]
    )
    result = run_sql_agent(llm, "cursos del otro alumno", runtime=runtime_a.as_runtime())

    step = result.steps[0]
    assert step.ok is False
    assert "set by the server" in (step.error or "")
    assert "tenant_id" in (step.error or "") and "user_id" in (step.error or "")


def test_agent_refuses_unknown_tool(runtime_a: _TenantToolRuntime) -> None:
    llm = ScriptedLLM(
        [
            AIMessage(content="", tool_calls=[call("delete_everything")]),
            AIMessage(content="No puedo."),
        ]
    )
    result = run_sql_agent(llm, "borra mis notas", runtime=runtime_a.as_runtime())
    assert "Unknown tool" in (result.steps[0].error or "")


@pytest.mark.skip(reason="legacy tool deprecated; replaced by canvas-mock catalog")
def test_agent_is_bounded_and_forces_a_final_answer(
    runtime_a: _TenantToolRuntime,
) -> None:
    # A model that would keep calling tools forever: exactly max_steps
    # tool-calling turns are consumed, then the forced final turn runs with
    # tools withheld and must produce prose.
    llm = ScriptedLLM(
        [AIMessage(content="", tool_calls=[call("get_user_courses")]) for _ in range(3)]
        + [AIMessage(content="Tienes 1 curso.")]
    )
    result = run_sql_agent(
        llm, "mis cursos", runtime=runtime_a.as_runtime(), max_steps=3
    )

    assert result.exhausted is True
    assert len(result.steps) == 3
    assert result.answer == "Tienes 1 curso."
    # 3 loop turns + 1 forced final turn.
    assert llm.invocations == 4


def test_agent_requires_tool_calling_support(runtime_a: _TenantToolRuntime) -> None:
    class _NoTools:
        def invoke(self, messages: list[Any]) -> AIMessage:
            return AIMessage(content="hi")

    with pytest.raises(AgentUnavailable):
        run_sql_agent(_NoTools(), "hola", runtime=runtime_a.as_runtime())


@pytest.mark.skip(reason="legacy tool deprecated; replaced by canvas-mock catalog")
def test_grounding_query_count_is_capped_per_request(
    runtime_a: _TenantToolRuntime,
) -> None:
    """Repeated id-bearing calls must not re-read the grounding list."""
    llm = ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    call("get_course_details", {"course_id": str(_Ids.course_calc)}, "c1"),
                    call(
                        "get_course_assignments", {"course_id": str(_Ids.course_calc)}, "c2"
                    ),
                ],
            ),
            AIMessage(content="Listo."),
        ]
    )
    run_sql_agent(llm, "detalles y tareas de cálculo", runtime=runtime_a.as_runtime())

    # ``known_ids`` populated the cache on the first call and reused it.
    assert runtime_a._id_cache["course_id"]
    assert "assignment_id" not in runtime_a._id_cache
