"""Unit tests for the Canvas DTO whitelist and peer-data stripping."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.canvas.dto import (
    AssignmentDTO,
    AvailabilityStatusDTO,
    CourseDTO,
    EnrollmentDTO,
    LockInfoDTO,
    ScoreStatisticsDTO,
    SubmissionDTO,
    UserDTO,
    strip_peer_data,
)

MINIMUM_PAYLOADS = [
    (
        UserDTO,
        {
            "id": 101,
            "name": "<redacted-user>",
            "created_at": "2026-01-02T03:04:05Z",
            "permissions": {"can_update_name": False},
        },
    ),
    (
        CourseDTO,
        {
            "id": 202,
            "name": "<redacted-course>",
            "calendar": {"ics": "<redacted-calendar>"},
            "enrollments": [{"id": 303, "user_id": 101, "role": "StudentEnrollment"}],
        },
    ),
    (
        EnrollmentDTO,
        {
            "id": 303,
            "course_id": 202,
            "user_id": 101,
            "role": "StudentEnrollment",
        },
    ),
    (
        AssignmentDTO,
        {
            "id": 404,
            "course_id": 202,
            "name": "<redacted-assignment>",
            "due_at": "2026-02-03T10:00:00Z",
            "availability_status": {"status": "available", "date": None},
            "lock_info": {"can_view": True},
            "integration_data": {},
            "score_statistics": {"mean": 7.5},
            "submission": {"id": 505, "user_id": 101, "score": 8.0},
        },
    ),
    (
        SubmissionDTO,
        {
            "id": 505,
            "assignment_id": 404,
            "user_id": 101,
            "submitted_at": "2026-02-03T11:00:00Z",
            "score": 8.0,
        },
    ),
    (ScoreStatisticsDTO, {"min": 1.0, "max": 10.0, "mean": 7.5, "median": 8.0}),
    (AvailabilityStatusDTO, {"status": "available", "date": "2026-02-01T00:00:00Z"}),
    (LockInfoDTO, {"lock_at": "2026-02-04T00:00:00Z", "can_view": True}),
]


@pytest.mark.parametrize(("dto_type", "payload"), MINIMUM_PAYLOADS)
def test_each_dto_accepts_minimum_payload(
    dto_type: type, payload: dict[str, object]
) -> None:
    dto = dto_type.model_validate(payload)

    assert dto is not None
    if "created_at" in payload:
        assert isinstance(dto.created_at, datetime)


@pytest.mark.parametrize(("dto_type", "payload"), MINIMUM_PAYLOADS)
def test_each_dto_ignores_unknown_canvas_keys(
    dto_type: type, payload: dict[str, object]
) -> None:
    dto = dto_type.model_validate(
        {**payload, "future_canvas_field": "ignored", "peer_identifier": "ignored"}
    )

    assert "future_canvas_field" not in dto.model_dump()
    assert "peer_identifier" not in dto.model_dump()


def test_iso8601_fields_are_datetimes() -> None:
    assignment = AssignmentDTO.model_validate(
        {"id": 404, "due_at": "2026-02-03T10:00:00Z"}
    )
    assert assignment.due_at == datetime(2026, 2, 3, 10, tzinfo=UTC)


def test_strip_peer_data_removes_sensitive_submission_fields_for_peer_dict() -> None:
    payload = {
        "submission": {
            "id": 505,
            "user_id": 909,
            "score": 4.0,
            "body": "<peer-content>",
            "preview_url": "<peer-preview-url>",
            "url": "<peer-url>",
            "attachments": [{"url": "<peer-attachment>"}],
        }
    }

    stripped = strip_peer_data(payload, tenant_user_id=101)

    assert isinstance(stripped, dict)
    submission = stripped["submission"]
    assert submission["score"] == 4.0
    for field in ("body", "preview_url", "url", "attachments"):
        assert field not in submission


def test_strip_peer_data_removes_sensitive_submission_fields_for_peer_dto() -> None:
    submission = SubmissionDTO(
        id=505,
        user_id=909,
        score=4.0,
        body="<peer-content>",
        preview_url="<peer-preview-url>",
        url="<peer-url>",
        attachments=[{"url": "<peer-attachment>"}],
    )

    stripped = strip_peer_data(submission, tenant_user_id=101)

    assert isinstance(stripped, SubmissionDTO)
    assert stripped.score == 4.0
    assert stripped.body is None
    assert stripped.preview_url is None
    assert stripped.url is None
    assert stripped.attachments is None


def test_strip_peer_data_keeps_sensitive_submission_fields_for_tenant() -> None:
    submission = {
        "id": 505,
        "user_id": 101,
        "body": "<own-content>",
        "preview_url": "<own-preview-url>",
        "url": "<own-url>",
        "attachments": [{"url": "<own-attachment>"}],
    }

    stripped = strip_peer_data(submission, tenant_user_id=101)

    assert stripped == submission


def test_strip_peer_data_discards_peer_enrollments_nested_in_course() -> None:
    course = CourseDTO.model_validate(
        {
            "id": 202,
            "enrollments": [
                {"id": 303, "user_id": 101, "role": "StudentEnrollment"},
                {
                    "id": 304,
                    "role": "StudentEnrollment",
                    "user": {"id": 909, "display_name": "<peer>"},
                },
            ],
        }
    )

    stripped = strip_peer_data(course, tenant_user_id=101)

    assert isinstance(stripped, CourseDTO)
    assert [enrollment.user_id for enrollment in stripped.enrollments or []] == [101]


def test_course_dto_parses_term_object_from_canvas_payload() -> None:
    course = CourseDTO.model_validate(
        {
            "id": 202,
            "name": "Cálculo I",
            "term": {
                "id": 17,
                "name": "PFR A 2026 - 2",
                "start_at": "2026-08-01T00:00:00Z",
                "end_at": "2026-12-31T23:59:59Z",
                "sis_term_id": 20262,
            },
        }
    )

    assert course.term is not None
    assert course.term.id == 17
    assert course.term.name == "PFR A 2026 - 2"
    assert course.term.start_at == datetime(2026, 8, 1, tzinfo=UTC)
    assert course.term.sis_term_id == 20262

    payload = course.to_payload()
    assert payload["term_name"] == "PFR A 2026 - 2"
    # The nested ``term`` object never crosses the persistence boundary
    # directly — only its flat ``name`` does.
    assert "term" not in payload


def test_course_dto_term_name_is_absent_when_term_is_missing() -> None:
    course = CourseDTO.model_validate({"id": 202, "name": "Sin término"})

    assert course.term is None
    assert "term_name" not in course.to_payload()


def test_course_dto_term_keeps_metadata_through_strip_peer_data() -> None:
    course = CourseDTO.model_validate(
        {
            "id": 202,
            "enrollments": [{"id": 303, "user_id": 101, "role": "StudentEnrollment"}],
            "term": {"id": 17, "name": "PFR A 2026 - 2"},
        }
    )

    stripped = strip_peer_data(course, tenant_user_id=101)

    assert isinstance(stripped, CourseDTO)
    assert stripped.term is not None
    assert stripped.term.name == "PFR A 2026 - 2"
    assert stripped.to_payload()["term_name"] == "PFR A 2026 - 2"
