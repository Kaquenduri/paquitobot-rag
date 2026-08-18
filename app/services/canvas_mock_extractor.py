"""Canvas Mock extractor service (PR 4 task 4.5).

The extractor is the bridge between the mock's HTTP API and the
``canvas_mock_*`` tables. It owns three invariants:

1. **Tenant scoping.** Every upsert carries the resolved
   ``tenant_id``; the SQL filter is non-optional so a cross-tenant
   attack through the JSON payload cannot land a row.

2. **Shape validation.** Every JSON payload is validated against
   the matching Pydantic DTO before the row touches the database. A
   single malformed element in a batch is enough to abort the
   whole transaction — no partial writes.

3. **Atomic rejection.** Either every row in the batch lands, or
   none does. The :class:`CanvasMockShapeError` exception is
   raised with the offending index so the caller can log it
   without leaking the raw payload.

The extractor is **async** because the HTTP client is async; tests
use ``asyncio.run`` to bridge from sync test code.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import (
    CanvasMockAssignment,
    CanvasMockAttendanceRecord,
    CanvasMockCourse,
    CanvasMockGrade,
)
from app.schemas.canvas_mock import (
    CanvasMockAssignmentDTO,
    CanvasMockAttendanceRecordDTO,
    CanvasMockCourseDTO,
    CanvasMockGradeDTO,
)
from app.services.canvas_mock_client import CanvasMockClient, CanvasMockError

logger = get_logger("app.services.canvas_mock_extractor")


class CanvasMockShapeError(Exception):
    """A row pulled from the mock failed DTO validation.

    The ``index`` attribute tells the caller which element in the
    batch was offending. The raw payload is **not** retained.
    """

    def __init__(self, message: str, *, index: int, resource: str) -> None:
        super().__init__(message)
        self.index = index
        self.resource = resource


# ---------------------------------------------------------------------------
# Endpoint paths (mock URLs)
# ---------------------------------------------------------------------------

# These are the GET endpoints the mock exposes. The extractor calls
# each one with the bare path; the client injects the base URL.
PATH_COURSES = "/courses"
PATH_ASSIGNMENTS = "/assignments"
PATH_GRADES = "/grades"
PATH_ATTENDANCE = "/attendance"
PATH_USERS_SELF = "/users/self"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CanvasMockExtractor:
    """Pull-and-upsert helper for the canvas-mock-api.

    The constructor accepts a ``client`` (a :class:`CanvasMockClient`)
    and an optional ``session_factory`` (a zero-arg callable that
    returns a SQLAlchemy :class:`Session`). The factory is invoked
    once per upsert; the session is closed inside the helper.

    Tests that exercise only the validation/staging path can pass
    ``session_factory=None`` and call :meth:`fetch_*` directly.
    """

    def __init__(
        self,
        client: CanvasMockClient,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._client = client
        self._session_factory = session_factory

    # -- fetch stage -----------------------------------------------------

    async def _fetch_all(self, path: str) -> list[dict[str, Any]]:
        """GET ``path`` and return the raw JSON list."""
        rows = await self._client.get(path)
        if not isinstance(rows, list):
            # A 200 with a non-list payload is a malformed response.
            raise CanvasMockShapeError(
                f"{path} returned non-list payload",
                index=0,
                resource=path,
            )
        return rows

    async def fetch_courses(self) -> list[CanvasMockCourseDTO]:
        rows = await self._fetch_all(PATH_COURSES)
        return self._validate_dtos(rows, CanvasMockCourseDTO, "courses")

    async def fetch_assignments(self) -> list[CanvasMockAssignmentDTO]:
        rows = await self._fetch_all(PATH_ASSIGNMENTS)
        return self._validate_dtos(rows, CanvasMockAssignmentDTO, "assignments")

    async def fetch_grades(self) -> list[CanvasMockGradeDTO]:
        rows = await self._fetch_all(PATH_GRADES)
        return self._validate_dtos(rows, CanvasMockGradeDTO, "grades")

    async def fetch_attendance(self) -> list[CanvasMockAttendanceRecordDTO]:
        rows = await self._fetch_all(PATH_ATTENDANCE)
        return self._validate_dtos(
            rows, CanvasMockAttendanceRecordDTO, "attendance"
        )

    def _validate_dtos(
        self,
        rows: list[dict[str, Any]],
        dto: type[BaseModel],
        resource: str,
    ) -> list[BaseModel]:
        """Validate every row against ``dto``; raise on first failure.

        The wrapped DTO list is the result. The exception type is
        :class:`CanvasMockShapeError` so callers can distinguish shape
        failures from transport failures.
        """
        out: list[BaseModel] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise CanvasMockShapeError(
                    f"{resource}[{index}] is not a JSON object",
                    index=index,
                    resource=resource,
                )
            try:
                out.append(dto.model_validate(row))
            except ValidationError as exc:
                raise CanvasMockShapeError(
                    f"{resource}[{index}] failed Pydantic validation: {exc}",
                    index=index,
                    resource=resource,
                ) from exc
        return out

    # -- upsert stage -----------------------------------------------------

    async def _upsert(
        self,
        tenant_id: Any,
        model: type[BaseModel],
        rows: list[BaseModel],
        *,
        attribute_map: dict[str, str],
        natural_key: str,
    ) -> int:
        """Upsert ``rows`` into the table behind ``model`` atomically.

        ``attribute_map`` translates DTO attribute names to column
        names (e.g. ``id`` → ``canvas_mock_id``). ``natural_key`` is
        the DTO attribute name whose value is paired with
        ``tenant_id`` to detect duplicates.

        Returns the number of rows that landed (positive integer).
        Raises :class:`CanvasMockShapeError` if any row produces a
        SQL error (the transaction is rolled back by the caller).
        """
        if self._session_factory is None:
            raise RuntimeError("session_factory is required for upsert")
        natural_key_column = attribute_map[natural_key]
        session = self._session_factory()
        try:
            for index, row in enumerate(rows):
                try:
                    data = row.model_dump(exclude_none=True)
                    payload = {
                        attribute_map[k]: v for k, v in data.items() if k in attribute_map
                    }
                    payload["tenant_id"] = tenant_id
                    if natural_key_column not in payload:
                        raise CanvasMockShapeError(
                            f"{natural_key} missing from {row.__class__.__name__}",
                            index=index,
                            resource=model.__name__,
                        )
                    session.merge(model(**payload))
                except SQLAlchemyError as exc:
                    raise CanvasMockShapeError(
                        f"db error: {exc.__class__.__name__}",
                        index=index,
                        resource=model.__name__,
                    ) from exc
            session.commit()
            return len(rows)
        finally:
            session.close()

    async def upsert_courses(
        self, tenant_id: Any, courses: list[CanvasMockCourseDTO]
    ) -> int:
        return await self._upsert(
            tenant_id,
            CanvasMockCourse,
            courses,
            attribute_map={
                "id": "canvas_mock_id",
                "name": "name",
                "course_code": "course_code",
                "workflow_state": "workflow_state",
                "start_at": "start_at",
                "end_at": "end_at",
                "enrollments_count": "enrollments_count",
            },
            natural_key="id",
        )

    async def upsert_assignments(
        self, tenant_id: Any, assignments: list[CanvasMockAssignmentDTO]
    ) -> int:
        return await self._upsert(
            tenant_id,
            CanvasMockAssignment,
            assignments,
            attribute_map={
                # ``CanvasMockAssignmentDTO.id`` IS the natural key; the
                # DTO carries the mock id (int) under ``id``.
                "id": "canvas_mock_id",
                "course_id": "course_canvas_mock_id",
                "name": "name",
                "description": "description",
                "points_possible": "points_possible",
                "due_at": "due_at",
                "grading_type": "grading_type",
                "submission_types": "submission_types",
                "workflow_state": "workflow_state",
                "html_url": "html_url",
                "url": "url",
                "created_at": "created_at_mock",
                "updated_at": "updated_at_mock",
            },
            natural_key="id",
        )

    async def upsert_grades(
        self, tenant_id: Any, grades: list[CanvasMockGradeDTO]
    ) -> int:
        return await self._upsert(
            tenant_id,
            CanvasMockGrade,
            grades,
            attribute_map={
                "assignment_id": "assignment_canvas_mock_id",
                "user_id": "user_canvas_mock_id",
                "score": "score",
                "grade": "grade",
                "graded_at": "graded_at",
                "grader_id": "grader_id",
            },
            natural_key="assignment_id",
        )

    async def upsert_attendance(
        self, tenant_id: Any, records: list[CanvasMockAttendanceRecordDTO]
    ) -> int:
        return await self._upsert(
            tenant_id,
            CanvasMockAttendanceRecord,
            records,
            attribute_map={
                "class_session_id": "class_session_canvas_mock_id",
                "user_id": "user_canvas_mock_id",
                "status": "status",
            },
            natural_key="class_session_id",
        )

    # -- end-to-end helper ------------------------------------------------

    async def fetch_and_upsert(
        self,
        tenant_id: Any,
        resources: list[str],
    ) -> dict[str, int]:
        """Pull every resource in ``resources`` and upsert atomically.

        Raises :class:`CanvasMockShapeError` on any per-row failure;
        the transaction is rolled back so the database stays consistent.
        """
        counts: dict[str, int] = {}
        for resource in resources:
            if resource == "courses":
                rows = await self.fetch_courses()
                counts["courses"] = await self.upsert_courses(tenant_id, rows)
            elif resource == "assignments":
                rows = await self.fetch_assignments()
                counts["assignments"] = await self.upsert_assignments(
                    tenant_id, rows
                )
            elif resource == "grades":
                rows = await self.fetch_grades()
                counts["grades"] = await self.upsert_grades(tenant_id, rows)
            elif resource == "attendance":
                rows = await self.fetch_attendance()
                counts["attendance"] = await self.upsert_attendance(tenant_id, rows)
            else:
                raise CanvasMockError(f"unknown resource: {resource}")
        return counts


__all__ = [
    "PATH_ASSIGNMENTS",
    "PATH_ATTENDANCE",
    "PATH_COURSES",
    "PATH_GRADES",
    "PATH_USERS_SELF",
    "CanvasMockExtractor",
    "CanvasMockShapeError",
]
