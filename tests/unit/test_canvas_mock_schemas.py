"""Tests for the canvas-mock Pydantic DTOs (PR 1 task 1.4).

These tests confirm that the DTOs in :mod:`app.schemas.canvas_mock` match
the contract of the ``canvas-mock-api`` payload exactly:

- All required fields are present and non-nullable in the canonical
  fixtures.
- Optional fields default to ``None`` and round-trip through
  ``model_dump`` cleanly.
- The literal enums (attendance status) reject values outside the
  declared set.
- ``extra="ignore"`` swallows unknown keys without raising — the mock
  is allowed to add fields without breaking the contract.
- ``frozen=True`` blocks mutation after validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.canvas_mock import (
    CanvasMockAssignmentDTO,
    CanvasMockAttendanceRecordDTO,
    CanvasMockClassSessionDTO,
    CanvasMockCourseDTO,
    CanvasMockEnrollmentDTO,
    CanvasMockGradeDTO,
    CanvasMockTermDTO,
    CanvasMockUserSelfDTO,
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "canvas_mock"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Course
# ---------------------------------------------------------------------------


def test_course_dto_matches_canonical_payload() -> None:
    payload = _load("course.json")
    dto = CanvasMockCourseDTO.model_validate(payload)
    assert dto.id == payload["id"]
    assert dto.name == payload["name"]
    assert dto.course_code == payload["course_code"]
    assert dto.workflow_state == payload["workflow_state"]
    assert dto.enrollments_count == payload["enrollments_count"]


def test_course_dto_optional_fields_default_to_none() -> None:
    payload = _load("course_minimal.json")
    dto = CanvasMockCourseDTO.model_validate(payload)
    assert dto.start_at is None
    assert dto.end_at is None
    assert dto.enrollments_count is None


def test_course_dto_rejects_missing_required_field() -> None:
    bad = {"id": 1, "name": "x", "course_code": "x"}  # missing workflow_state
    with pytest.raises(ValidationError):
        CanvasMockCourseDTO.model_validate(bad)


def test_course_dto_ignores_unknown_keys() -> None:
    payload = _load("course.json")
    payload["future_field"] = "the mock invented this"
    dto = CanvasMockCourseDTO.model_validate(payload)
    assert not hasattr(dto, "future_field")


def test_course_dto_is_frozen() -> None:
    payload = _load("course.json")
    dto = CanvasMockCourseDTO.model_validate(payload)
    with pytest.raises(ValidationError):
        dto.name = "Nuevo"  # type: ignore[misc]


def test_course_dto_to_payload_excludes_none() -> None:
    payload = _load("course_minimal.json")
    dto = CanvasMockCourseDTO.model_validate(payload)
    out = dto.to_payload()
    # ``exclude_none`` strips the optional fields that were never set.
    assert "start_at" not in out
    assert "end_at" not in out
    assert "enrollments_count" not in out
    # Required fields stay.
    assert out["name"] == "Álgebra"
    assert out["course_code"] == "ALG-1"


# ---------------------------------------------------------------------------
# Enrollment + term
# ---------------------------------------------------------------------------


def test_enrollment_dto_validates() -> None:
    payload = _load("enrollment.json")
    dto = CanvasMockEnrollmentDTO.model_validate(payload)
    assert dto.id == payload["id"]
    assert dto.user_id == payload["user_id"]
    assert dto.course_id == payload["course_id"]
    assert dto.type == payload["type"]
    assert dto.enrollment_state == payload["enrollment_state"]


def test_term_dto_validates() -> None:
    payload = _load("term.json")
    dto = CanvasMockTermDTO.model_validate(payload)
    assert dto.id == payload["id"]
    assert dto.name == payload["name"]


# ---------------------------------------------------------------------------
# Assignments + grades
# ---------------------------------------------------------------------------


def test_assignment_dto_validates_with_optional_fields() -> None:
    payload = _load("assignment.json")
    dto = CanvasMockAssignmentDTO.model_validate(payload)
    assert dto.id == payload["id"]
    assert dto.course_id == payload["course_id"]
    assert dto.description == payload["description"]
    assert dto.points_possible == payload["points_possible"]


def test_grade_dto_optional_score_and_grade() -> None:
    """The mock grade payload allows ``score`` and ``grade`` to be null."""
    payload = {
        "assignment_id": 42,
        "user_id": 77,
        "graded_at": None,
        "grader_id": 5,
    }
    dto = CanvasMockGradeDTO.model_validate(payload)
    assert dto.score is None
    assert dto.grade is None
    assert dto.grader_id == 5


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------


def test_attendance_record_accepts_present_and_absent() -> None:
    payload = _load("attendance.json")
    dto = CanvasMockAttendanceRecordDTO.model_validate(payload)
    assert dto.status in {"present", "absent"}


def test_attendance_record_rejects_other_statuses() -> None:
    bad = {"class_session_id": 1, "user_id": 1, "status": "tardy"}
    with pytest.raises(ValidationError):
        CanvasMockAttendanceRecordDTO.model_validate(bad)


# ---------------------------------------------------------------------------
# Class sessions + user self
# ---------------------------------------------------------------------------


def test_class_session_dto_validates() -> None:
    payload = _load("class_session.json")
    dto = CanvasMockClassSessionDTO.model_validate(payload)
    assert dto.course_id == payload["course_id"]


def test_user_self_dto_validates() -> None:
    payload = _load("user_self.json")
    dto = CanvasMockUserSelfDTO.model_validate(payload)
    assert dto.id == payload["id"]
    assert dto.email == payload["email"]
