"""Unit tests for the canvas-mock extractor (PR 4 task 4.4).

The extractor:
1. Fetches JSON from the mock via :class:`CanvasMockClient`.
2. Validates each row against the matching Pydantic DTO.
3. Upserts into the corresponding ``canvas_mock_*`` table, scoped by
   ``tenant_id``.
4. Rolls back the whole transaction on any shape failure (atomic
   rejection: malformed 200 → 0 rows persist).

The mock exposes ONLY these ``/users/self/*`` endpoints (verified
against canvas-mock-api/app/api/routers/users_self.py):

- ``GET /users/self/courses`` — list of enrolled courses. The mock's
  ``?include[]=term`` handler hits a missing ``terms`` table (500),
  so we deliberately do NOT send it.
- ``GET /users/self/attendance?days=N`` — global (no per-course scope).
- ``GET /users/self/grades`` — global (no per-course scope).
- ``GET /users/self/profile``.

Per-course endpoints (``/users/self/courses/{id}/assignments``) DO NOT
EXIST — they are admin-only under ``/admin/*``. The extractor must
therefore call the three global endpoints above; assignments are no
longer fetched per-course (the mock has no self-service equivalent).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest

from app.models import (
    CanvasMockAttendanceRecord,
    CanvasMockCourse,
    CanvasMockGrade,
)
from app.services.canvas_mock_client import CanvasMockClient
from app.services.canvas_mock_extractor import (
    CanvasMockExtractor,
    CanvasMockShapeError,
)


def _client(handlers: dict[str, Any]) -> CanvasMockClient:
    """Stand up a CanvasMockClient whose :func:`get` is monkey-patched."""

    class _Stub:
        def __init__(self, h: dict[str, Any]) -> None:
            self._h = h

        async def get(self, path: str, *args: Any, **kwargs: Any) -> Any:
            return self._h[path]

    client = CanvasMockClient(
        base_url="https://canvas-mock.example.com",
        api_key="adm_test",
        jwt_token="jwt",
        transport=httpx.MockTransport(httpx.Response(200)),
    )
    client.get = _Stub(handlers).get  # type: ignore[method-assign]
    return client


def _recording_client(
    response_bodies: dict[str, Any],
) -> tuple[CanvasMockClient, list[httpx.Request]]:
    """Build a client backed by httpx.MockTransport that records requests.

    The handler routes by URL path (without query) so callers can
    assert that ``/users/self/courses`` was called with
    ``?include[]=term`` and ``/users/self/attendance`` with
    ``?days=14``.
    """
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = response_bodies.get(request.url.path, [])
        return httpx.Response(200, json=body)

    client = CanvasMockClient(
        base_url="https://canvas-mock.example.com",
        api_key="adm_test",
        jwt_token="jwt",
        transport=httpx.MockTransport(_handler),
    )
    return client, captured


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Path/params contract — verified against canvas-mock-api router
# ---------------------------------------------------------------------------


def test_extractor_fetch_courses_calls_self_courses_no_params() -> None:
    """``fetch_courses`` MUST hit ``/users/self/courses`` with no query params.

    The mock's ``?include[]=term`` handler hits a missing ``terms`` table
    (returns 500), so the extractor deliberately avoids it. The term sub-object
    is not embedded; the local ORM has no term columns.
    """
    client, captured = _recording_client(
        {
            "/users/self/courses": [
                {
                    "id": 101,
                    "name": "Cálculo I",
                    "course_code": "CALC-1",
                    "workflow_state": "available",
                }
            ],
        }
    )
    extractor = CanvasMockExtractor(client=client)
    rows = _run(extractor.fetch_courses())
    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/users/self/courses"
    # No query params: the mock's include[]=term handler is broken
    # (references a missing `terms` table).
    assert len(request.url.params) == 0
    assert len(rows) == 1
    assert rows[0].id == 101


def test_extractor_fetch_attendance_calls_self_attendance_with_days() -> None:
    """``fetch_attendance`` MUST hit ``/users/self/attendance?days=14``.

    The mock requires ``days`` in the [1, 365] range; 14 is the
    default per the mock router.
    """
    client, captured = _recording_client(
        {
            "/users/self/attendance": [
                {
                    "class_session_id": 9001,
                    "user_id": 77,
                    "status": "present",
                    "marked_at": "2026-08-18T10:00:00+00:00",
                }
            ],
        }
    )
    extractor = CanvasMockExtractor(client=client)
    rows = _run(extractor.fetch_attendance())
    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/users/self/attendance"
    assert request.url.params["days"] == "14"
    assert len(rows) == 1
    assert rows[0].status == "present"


def test_extractor_fetch_grades_calls_self_grades_no_params() -> None:
    """``fetch_grades`` MUST hit ``/users/self/grades`` with no params."""
    client, captured = _recording_client(
        {
            "/users/self/grades": [
                {
                    "assignment_id": 42,
                    "user_id": 77,
                    "score": 18.0,
                    "grade": "18",
                    "graded_at": "2026-08-15T10:00:00+00:00",
                    "grader_id": 5,
                }
            ],
        }
    )
    extractor = CanvasMockExtractor(client=client)
    rows = _run(extractor.fetch_grades())
    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/users/self/grades"
    assert len(request.url.params) == 0
    assert len(rows) == 1
    assert rows[0].score == 18.0


# ---------------------------------------------------------------------------
# Per-course fetch (fan-out): assignments + class sessions
# ---------------------------------------------------------------------------


def test_extractor_fetch_assignments_for_course_hits_per_course_path() -> None:
    """``fetch_assignments_for_course`` MUST hit the per-course path."""
    client, captured = _recording_client(
        {
            "/users/self/courses/42/assignments": [
                {
                    "id": 501,
                    "course_id": 42,
                    "name": "TP1",
                    "points_possible": 10.0,
                },
                {
                    "id": 502,
                    "course_id": 42,
                    "name": "TP2",
                    "points_possible": 20.0,
                },
            ],
        }
    )
    extractor = CanvasMockExtractor(client=client)
    rows = _run(extractor.fetch_assignments_for_course(42))
    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/users/self/courses/42/assignments"
    assert len(rows) == 2
    assert rows[0].id == 501
    assert rows[0].course_id == 42
    assert rows[1].name == "TP2"


def test_extractor_fetch_class_sessions_for_course_hits_per_course_path() -> None:
    """``fetch_class_sessions_for_course`` MUST hit the per-course path."""
    client, captured = _recording_client(
        {
            "/users/self/courses/42/class_sessions": [
                {
                    "id": 9001,
                    "course_id": 42,
                    "start_at": "2026-08-18T10:00:00+00:00",
                    "end_at": "2026-08-18T12:00:00+00:00",
                },
            ],
        }
    )
    extractor = CanvasMockExtractor(client=client)
    rows = _run(extractor.fetch_class_sessions_for_course(42))
    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/users/self/courses/42/class_sessions"
    assert len(rows) == 1
    assert rows[0].id == 9001
    assert rows[0].course_id == 42


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_extractor_upsert_courses_writes_to_db(db_session: Any) -> None:
    """``upsert_courses`` inserts a course row scoped by tenant."""
    client = _client(
        {
            "/users/self/courses": [
                {
                    "id": 101,
                    "name": "Cálculo I",
                    "course_code": "CALC-1",
                    "workflow_state": "available",
                },
            ],
        }
    )
    tenant = uuid.uuid4()
    extractor = CanvasMockExtractor(client=client, session_factory=lambda: db_session)
    rows = _run(extractor.fetch_courses())
    _run(extractor.upsert_courses(tenant_id=tenant, courses=rows))
    db_session.commit()
    fetched = db_session.query(CanvasMockCourse).filter_by(tenant_id=tenant).all()
    assert len(fetched) == 1
    assert fetched[0].name == "Cálculo I"
    assert fetched[0].canvas_mock_id == 101


# ---------------------------------------------------------------------------
# Atomic rejection
# ---------------------------------------------------------------------------


def test_extractor_rejects_malformed_with_zero_rows(db_session: Any) -> None:
    """A malformed payload in a 5-row batch lands 0 rows in the DB."""
    client = _client(
        {
            "/users/self/courses": [
                {"id": 101, "name": "Cálculo I", "course_code": "CALC-1", "workflow_state": "available"},
                {"id": 102, "name": "Álgebra", "course_code": "ALG-1"},
                {"id": 103, "name": "Física", "course_code": "FIS-1", "workflow_state": "available"},
                {"id": 104, "name": "Química", "course_code": "QUI-1", "workflow_state": "available"},
                {"id": 105, "name": "Biología", "course_code": "BIO-1", "workflow_state": "available"},
            ],
        }
    )
    tenant = uuid.uuid4()
    extractor = CanvasMockExtractor(client=client, session_factory=lambda: db_session)
    with pytest.raises(CanvasMockShapeError):
        _run(
            extractor.fetch_and_upsert(
                tenant_id=tenant,
                resources=["courses"],
            )
        )
    db_session.rollback()
    count = (
        db_session.query(CanvasMockCourse).filter_by(tenant_id=tenant).count()
    )
    assert count == 0


def test_extractor_4xx_no_persist(db_session: Any) -> None:
    """A 4xx response raises without writing any rows."""
    from app.services.canvas_mock_client import CanvasMockError

    client = _client({})
    client.get = _stub_404  # type: ignore[method-assign]
    tenant = uuid.uuid4()
    extractor = CanvasMockExtractor(client=client, session_factory=lambda: db_session)
    with pytest.raises(CanvasMockError):
        _run(extractor.fetch_and_upsert(tenant_id=tenant, resources=["courses"]))
    db_session.rollback()
    count = db_session.query(CanvasMockCourse).count()
    assert count == 0


async def _stub_404(*args: Any, **kwargs: Any) -> Any:
    from app.services.canvas_mock_client import CanvasMockError

    raise CanvasMockError("404 not found")


# ---------------------------------------------------------------------------
# Other resources
# ---------------------------------------------------------------------------


def test_extractor_upsert_grades(db_session: Any) -> None:
    client = _client(
        {
            "/users/self/grades": [
                {
                    "assignment_id": 42,
                    "user_id": 77,
                    "score": 18.0,
                    "grade": "18",
                    "graded_at": None,
                    "grader_id": 5,
                },
            ],
        }
    )
    tenant = uuid.uuid4()
    extractor = CanvasMockExtractor(client=client, session_factory=lambda: db_session)
    rows = _run(extractor.fetch_grades())
    _run(extractor.upsert_grades(tenant_id=tenant, grades=rows))
    db_session.commit()
    fetched = db_session.query(CanvasMockGrade).filter_by(tenant_id=tenant).all()
    assert len(fetched) == 1
    assert fetched[0].score == 18.0


def test_extractor_upsert_attendance(db_session: Any) -> None:
    client = _client(
        {
            "/users/self/attendance": [
                {"class_session_id": 9001, "user_id": 77, "status": "present"},
            ],
        }
    )
    tenant = uuid.uuid4()
    extractor = CanvasMockExtractor(client=client, session_factory=lambda: db_session)
    rows = _run(extractor.fetch_attendance())
    _run(extractor.upsert_attendance(tenant_id=tenant, records=rows))
    db_session.commit()
    fetched = (
        db_session.query(CanvasMockAttendanceRecord).filter_by(tenant_id=tenant).all()
    )
    assert len(fetched) == 1
    assert fetched[0].status == "present"