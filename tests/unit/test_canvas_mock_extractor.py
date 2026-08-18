"""Unit tests for the canvas-mock extractor (PR 4 task 4.4).

The extractor:
1. Fetches JSON from the mock via :class:`CanvasMockClient`.
2. Validates each row against the matching Pydantic DTO.
3. Upserts into the corresponding ``canvas_mock_*`` table, scoped by
   ``tenant_id``.
4. Rolls back the whole transaction on any shape failure (atomic
   rejection: malformed 200 → 0 rows persist).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import DataError, IntegrityError

from app.models import (
    CanvasMockAssignment,
    CanvasMockAttendanceRecord,
    CanvasMockCourse,
    CanvasMockGrade,
    CanvasMockUser,
)
from app.services.canvas_mock_client import CanvasMockClient
from app.services.canvas_mock_extractor import (
    CanvasMockExtractor,
    CanvasMockShapeError,
)
from app.schemas.canvas_mock import (
    CanvasMockAssignmentDTO,
    CanvasMockAttendanceRecordDTO,
    CanvasMockCourseDTO,
    CanvasMockGradeDTO,
)


def _client(handlers: dict[str, Any]) -> CanvasMockClient:
    """Stand up a CanvasMockClient whose :func:`get` is monkey-patched."""

    class _Stub:
        def __init__(self, h: dict[str, Any]) -> None:
            self._h = h

        async def get(self, path: str) -> Any:
            return self._h[path]

    client = CanvasMockClient(
        base_url="https://canvas-mock.example.com",
        api_key="adm_test",
        jwt_token="jwt",
        transport=httpx.MockTransport(httpx.Response(200)),
    )
    client.get = _Stub(handlers).get  # type: ignore[method-assign]
    return client


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_extractor_fetch_courses_validates_and_returns(db_session: Any) -> None:
    """``fetch_courses`` returns the validated DTOs."""
    client = _client(
        {
            "/courses": [
                {"id": 101, "name": "Cálculo I", "course_code": "CALC-1", "workflow_state": "available"},
            ],
        }
    )
    extractor = CanvasMockExtractor(client=client)
    rows = _run(extractor.fetch_courses())
    assert len(rows) == 1
    assert rows[0].id == 101


def test_extractor_upsert_courses_writes_to_db(db_session: Any) -> None:
    """``upsert_courses`` inserts a course row scoped by tenant."""
    client = _client(
        {
            "/courses": [
                {"id": 101, "name": "Cálculo I", "course_code": "CALC-1", "workflow_state": "available"},
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


# ---------------------------------------------------------------------------
# Atomic rejection
# ---------------------------------------------------------------------------


def test_extractor_rejects_malformed_with_zero_rows(db_session: Any) -> None:
    """A malformed payload in a 5-row batch lands 0 rows in the DB."""
    client = _client(
        {
            "/courses": [
                {"id": 101, "name": "Cálculo I", "course_code": "CALC-1", "workflow_state": "available"},
                {"id": 102, "name": "Álgebra", "course_code": "ALG-1"},  # missing workflow_state
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
    client = _client({})
    client.get = _stub_404  # type: ignore[method-assign]
    tenant = uuid.uuid4()
    extractor = CanvasMockExtractor(client=client, session_factory=lambda: db_session)
    with pytest.raises(Exception):
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
            "/grades": [
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
            "/attendance": [
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


def test_extractor_upsert_assignments(db_session: Any) -> None:
    client = _client(
        {
            "/assignments": [
                {
                    "id": 42,
                    "course_id": 101,
                    "name": "Parcial",
                    "points_possible": 20.0,
                    "due_at": None,
                    "workflow_state": "published",
                },
            ],
        }
    )
    tenant = uuid.uuid4()
    extractor = CanvasMockExtractor(client=client, session_factory=lambda: db_session)
    rows = _run(extractor.fetch_assignments())
    _run(extractor.upsert_assignments(tenant_id=tenant, assignments=rows))
    db_session.commit()
    fetched = db_session.query(CanvasMockAssignment).filter_by(tenant_id=tenant).all()
    assert len(fetched) == 1
    assert fetched[0].name == "Parcial"
