"""Pydantic v2 schemas for the canvas-mock-api response payloads (PR 1).

These DTOs mirror the mock's response shapes **verbatim** so the
extractor (PR 4) can validate every inbound payload before it lands in
the database. They are intentionally narrow — the same whitelist
discipline that protects the production ``app.canvas.dto`` modules
applies here, and ``extra="ignore"`` lets the mock add fields without
breaking the contract.

The naming convention follows the mock's resource names
(``CanvasMockCourseDTO``, ``CanvasMockAssignmentDTO``, …) so the
test-side ``.model_validate(fixture)`` reads naturally. Each DTO also
exposes a small ``to_payload()`` helper that returns only the columns
its destination ORM model accepts, mirroring the pattern in
``app.canvas.dto``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class CanvasMockDTO(BaseModel):
    """Common configuration for every canvas-mock DTO.

    ``extra="ignore"`` lets the mock add fields we have not whitelisted
    yet — the contract is about reading what we know, not about
    rejecting new fields. ``frozen=True`` keeps the validated cache
    from being mutated after the extractor drops it into a
    transaction.
    """

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
    )


# ---------------------------------------------------------------------------
# Course + enrollment
# ---------------------------------------------------------------------------


class CanvasMockTermDTO(CanvasMockDTO):
    """Mock term sub-document (returned when ``include[]=term``)."""

    id: int
    name: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None


class CanvasMockEnrollmentDTO(CanvasMockDTO):
    """Mock enrollment row (``/courses/{id}/enrollments`` payload)."""

    id: int
    user_id: int
    course_id: int
    type: str | None = None
    enrollment_state: str | None = None


class CanvasMockCourseDTO(CanvasMockDTO):
    """Mock course payload.

    Required (per the mock's contract): ``id``, ``name``,
    ``course_code``, ``workflow_state``. Optional: ``start_at``,
    ``end_at``, ``enrollments_count``.
    """

    id: int
    name: str
    course_code: str
    workflow_state: str
    start_at: datetime | None = None
    end_at: datetime | None = None
    enrollments_count: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# Class sessions + attendance
# ---------------------------------------------------------------------------


class CanvasMockClassSessionDTO(CanvasMockDTO):
    """Mock class session row."""

    id: int
    course_id: int
    start_at: datetime | None = None
    end_at: datetime | None = None


class CanvasMockAttendanceRecordDTO(CanvasMockDTO):
    """Mock attendance record. Status is a binary enum per the mock."""

    class_session_id: int
    user_id: int
    status: Literal["present", "absent"]


# ---------------------------------------------------------------------------
# Assignments + grades
# ---------------------------------------------------------------------------


class CanvasMockAssignmentDTO(CanvasMockDTO):
    """Mock assignment payload.

    All fields are optional except ``id`` and ``course_id``; the mock
    fills the rest conditionally based on the ``include[]`` filter.
    """

    id: int
    course_id: int
    name: str | None = None
    description: str | None = None
    due_at: datetime | None = None
    points_possible: float | None = None
    grading_type: str | None = None
    submission_types: list[str] | None = None
    workflow_state: str | None = None
    html_url: str | None = None
    url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class CanvasMockGradeDTO(CanvasMockDTO):
    """Mock grade payload.

    The mock's grade resource is intentionally minimal: it carries
    ``score`` and ``grade`` (the string representation the instructor
    typed) plus audit metadata. There is no ``late`` / ``missing`` /
    ``excused`` column — those would be canvas-only.
    """

    assignment_id: int
    user_id: int
    score: float | None = None
    grade: str | None = None
    graded_at: datetime | None = None
    grader_id: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# Auth / self
# ---------------------------------------------------------------------------


class CanvasMockUserSelfDTO(CanvasMockDTO):
    """Mock self-profile payload (``GET /users/self``)."""

    id: int
    name: str | None = None
    short_name: str | None = None
    email: str | None = None
    role: str | None = None


# ---------------------------------------------------------------------------
# Webhook envelope helpers
# ---------------------------------------------------------------------------


class CanvasMockWebhookEnvelope(CanvasMockDTO):
    """Placeholder for the inbound webhook body.

    The mock's real webhook payload is **Canvas-raw** (no JSON envelope);
    the headers carry the event metadata. The receiver validates the
    JSON body as a generic dict via
    :meth:`BaseModel.model_validate` after the signature check, so
    this DTO is mostly a marker for the type system. A real envelope
    schema can be added in a follow-up change without breaking
    existing call sites.
    """

    id: int | None = None
    event: str | None = None


__all__ = [
    "CanvasMockAssignmentDTO",
    "CanvasMockAttendanceRecordDTO",
    "CanvasMockClassSessionDTO",
    "CanvasMockCourseDTO",
    "CanvasMockDTO",
    "CanvasMockEnrollmentDTO",
    "CanvasMockGradeDTO",
    "CanvasMockTermDTO",
    "CanvasMockUserSelfDTO",
    "CanvasMockWebhookEnvelope",
]
