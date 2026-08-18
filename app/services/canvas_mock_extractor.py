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

Endpoint contract (verified against canvas-mock-api
``app/api/routers/users_self.py``):

- ``GET /users/self/courses`` — list of the caller's enrolled courses.
  No query params supported; the mock's ``?include[]=term`` handler
  hits a missing ``terms`` table (500), so we do NOT send it.
- ``GET /users/self/attendance?days=N`` — global; ``days`` defaults
  to 14 in the mock router.
- ``GET /users/self/grades`` — global, no params.

Per-course assignments/grades are admin-only under ``/admin/*`` and
cannot be reached by a self-service caller.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import (
    CanvasMockAssignment,
    CanvasMockAttendanceRecord,
    CanvasMockClassSession,
    CanvasMockCourse,
    CanvasMockGrade,
)
from app.schemas.canvas_mock import (
    CanvasMockAssignmentDTO,
    CanvasMockAttendanceRecordDTO,
    CanvasMockClassSessionDTO,
    CanvasMockCourseDTO,
    CanvasMockGradeDTO,
)
from app.services.canvas_mock_client import CanvasMockClient, CanvasMockError

logger = get_logger("app.services.canvas_mock_extractor")

T_DTO = TypeVar("T_DTO", bound=BaseModel)


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

# The canvas-mock-api exposes these endpoints for the authenticated
# self-caller under ``/users/self/*``. The extractor forwards each
# request through :class:`CanvasMockClient.get` with the right query
# params so the right sub-object payloads come back.
PATH_COURSES = "/users/self/courses"
PATH_ATTENDANCE = "/users/self/attendance"
PATH_GRADES = "/users/self/grades"
PATH_COURSE_ASSIGNMENTS = "/users/self/courses/{course_id}/assignments"
PATH_COURSE_CLASS_SESSIONS = "/users/self/courses/{course_id}/class_sessions"

# Locked default for the attendance window. The mock router accepts
# ``days`` in [1, 365] and defaults to 14; we send it explicitly so
# downstream operators know what the extractor is asking for.
ATTENDANCE_DAYS_DEFAULT: int = 14


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

    async def _fetch_all(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """GET ``path`` (with optional query ``params``) and return raw JSON."""
        rows = await self._client.get(path, params=params)
        if not isinstance(rows, list):
            raise CanvasMockShapeError(
                f"{path} returned non-list payload",
                index=0,
                resource=path,
            )
        return rows

    async def fetch_courses(self) -> list[CanvasMockCourseDTO]:
        rows = await self._fetch_all(PATH_COURSES)
        return self._validate_dtos_typed(rows, CanvasMockCourseDTO, "courses")

    async def fetch_grades(self) -> list[CanvasMockGradeDTO]:
        rows = await self._fetch_all(PATH_GRADES)
        return self._validate_dtos_typed(rows, CanvasMockGradeDTO, "grades")

    async def fetch_attendance(self) -> list[CanvasMockAttendanceRecordDTO]:
        rows = await self._fetch_all(
            PATH_ATTENDANCE,
            params={"days": ATTENDANCE_DAYS_DEFAULT},
        )
        return self._validate_dtos_typed(
            rows, CanvasMockAttendanceRecordDTO, "attendance"
        )

    async def fetch_assignments_for_course(
        self, course_id: int
    ) -> list[CanvasMockAssignmentDTO]:
        path = PATH_COURSE_ASSIGNMENTS.format(course_id=course_id)
        rows = await self._fetch_all(path)
        return self._validate_dtos_typed(
            rows, CanvasMockAssignmentDTO, "assignments"
        )

    async def fetch_class_sessions_for_course(
        self, course_id: int
    ) -> list[CanvasMockClassSessionDTO]:
        path = PATH_COURSE_CLASS_SESSIONS.format(course_id=course_id)
        rows = await self._fetch_all(path)
        return self._validate_dtos_typed(
            rows, CanvasMockClassSessionDTO, "class_sessions"
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

    def _validate_dtos_typed(
        self,
        rows: list[dict[str, Any]],
        dto: type[T_DTO],
        resource: str,
    ) -> list[T_DTO]:
        """Same as :meth:`_validate_dtos` but typed for the caller.

        The runtime behaviour is identical; the dedicated method just
        lets mypy narrow the return type to ``dto`` without a cast.
        """
        out: list[T_DTO] = []
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
        model: type[Any],
        rows: list[Any],
        *,
        attribute_map: dict[str, str],
        natural_key: str,
    ) -> int:
        """Upsert ``rows`` into the table behind ``model`` atomically.

        ``attribute_map`` translates DTO attribute names to column
        names (e.g. ``id`` → ``canvas_mock_id``). ``natural_key`` is
        the DTO attribute name whose value is paired with
        ``tenant_id`` to detect duplicates.

        Each row is looked up by ``(tenant_id, natural_key_column)``;
        if found, the existing row is updated in place; otherwise a
        new row is inserted. This satisfies the natural-key uniqueness
        declared on every ``canvas_mock_*`` table so a second upsert
        on the same key is a no-op rather than a constraint violation.

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
                    natural_key_value = payload[natural_key_column]
                    existing = session.execute(
                        select(model).where(
                            model.tenant_id == tenant_id,
                            getattr(model, natural_key_column)
                            == natural_key_value,
                        )
                    ).scalar_one_or_none()
                    if existing is None:
                        session.add(model(**payload))
                    else:
                        for key, value in payload.items():
                            setattr(existing, key, value)
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

    async def upsert_assignments(
        self, tenant_id: Any, assignments: list[CanvasMockAssignmentDTO]
    ) -> int:
        return await self._upsert(
            tenant_id,
            CanvasMockAssignment,
            assignments,
            attribute_map={
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

    async def upsert_class_sessions(
        self, tenant_id: Any, class_sessions: list[CanvasMockClassSessionDTO]
    ) -> int:
        return await self._upsert(
            tenant_id,
            CanvasMockClassSession,
            class_sessions,
            attribute_map={
                "id": "canvas_mock_id",
                "course_id": "course_canvas_mock_id",
                "start_at": "start_at",
                "end_at": "end_at",
            },
            natural_key="id",
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

        ``resources`` accepts ``"courses"``, ``"assignments"``,
        ``"class_sessions"``, ``"attendance"``, and ``"grades"``.

        ``"assignments"`` and ``"class_sessions"`` are fetched
        **per-course** (the canvas-mock-api exposes them under
        ``/users/self/courses/{id}/...``), so the courses endpoint is
        hydrated first when any of those two is requested. The fan-out
        is sequential: one request per course, one transaction per
        resource.
        """
        counts: dict[str, int] = {}

        courses: list[CanvasMockCourseDTO] | None = None
        if any(
            r in resources for r in ("courses", "assignments", "class_sessions")
        ):
            courses = await self.fetch_courses()
            if "courses" in resources:
                counts["courses"] = await self.upsert_courses(tenant_id, courses)

        if "assignments" in resources:
            assert courses is not None
            total = 0
            for course in courses:
                assignment_rows = await self.fetch_assignments_for_course(course.id)
                total += await self.upsert_assignments(tenant_id, assignment_rows)
            counts["assignments"] = total

        if "class_sessions" in resources:
            assert courses is not None
            total = 0
            for course in courses:
                session_rows = await self.fetch_class_sessions_for_course(course.id)
                total += await self.upsert_class_sessions(tenant_id, session_rows)
            counts["class_sessions"] = total

        if "attendance" in resources:
            attendance_rows = await self.fetch_attendance()
            counts["attendance"] = await self.upsert_attendance(
                tenant_id, attendance_rows
            )

        if "grades" in resources:
            grade_rows = await self.fetch_grades()
            counts["grades"] = await self.upsert_grades(tenant_id, grade_rows)

        for resource in resources:
            if resource not in (
                "courses",
                "assignments",
                "class_sessions",
                "attendance",
                "grades",
            ):
                raise CanvasMockError(f"unknown resource: {resource}")

        return counts


__all__ = [
    "ATTENDANCE_DAYS_DEFAULT",
    "PATH_ATTENDANCE",
    "PATH_COURSES",
    "PATH_COURSE_ASSIGNMENTS",
    "PATH_COURSE_CLASS_SESSIONS",
    "PATH_GRADES",
    "CanvasMockExtractor",
    "CanvasMockShapeError",
]