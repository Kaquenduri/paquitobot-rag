"""Pydantic DTOs for the whitelisted Canvas payload shapes.

Canvas is an evolving API, so DTOs intentionally ignore unknown keys. The
whitelist below is limited to fields documented in the exploration artifact;
fields not listed here never cross the Canvas boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError


class CanvasDTO(BaseModel):
    """Common configuration for all Canvas DTOs."""

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )


class UserDTO(CanvasDTO):
    id: int
    name: str | None = None
    sortable_name: str | None = None
    short_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    avatar_url: str | None = None
    created_at: datetime | None = None
    locale: str | None = None
    effective_locale: str | None = None
    permissions: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return only columns accepted by the existing user repository."""
        return {
            key: value
            for key, value in self.model_dump(exclude_none=True).items()
            if key in {"name", "sortable_name", "short_name", "locale", "effective_locale"}
        }


class EnrollmentDTO(CanvasDTO):
    id: int | None = None
    type: str | None = None
    role: str | None = None
    role_id: int | None = None
    user_id: int | None = None
    enrollment_state: str | None = None
    limit_privileges_to_course_section: bool | None = None
    user: dict[str, Any] | None = None
    course_id: int | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return persistence fields without the embedded user object."""
        return self.model_dump(
            exclude_none=True,
            exclude={"id", "type", "user"},
        )


class TermDTO(CanvasDTO):
    id: int | None = None
    name: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    sis_term_id: int | None = None


class CourseDTO(CanvasDTO):
    id: int
    name: str | None = None
    course_code: str | None = None
    account_id: int | None = None
    root_account_id: int | None = None
    enrollment_term_id: int | None = None
    uuid: str | None = None
    workflow_state: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    default_view: str | None = None
    is_public: bool | None = None
    license: str | None = None
    enrollments: list[EnrollmentDTO] | None = None
    calendar: dict[str, Any] | None = None
    time_zone: str | None = None
    storage_quota_mb: int | None = None
    enrollments_count: int | None = None
    term: TermDTO | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return repository fields and map Canvas calendar ICS to its column."""
        data = self.model_dump(
            exclude_none=True,
            exclude={"id", "enrollments", "calendar", "term"},
        )
        if self.calendar and self.calendar.get("ics") is not None:
            data["calendar_ics"] = self.calendar["ics"]
        if self.term and self.term.name is not None:
            data["term_name"] = self.term.name
        return data


class SubmissionDTO(CanvasDTO):
    id: int
    assignment_id: int | None = None
    user_id: int | None = None
    grader_id: int | None = None
    workflow_state: str | None = None
    submission_type: str | None = None
    submitted_at: datetime | None = None
    score: float | None = None
    entered_score: float | None = None
    grade: str | None = None
    entered_grade: str | None = None
    grade_matches_current_submission: bool | None = None
    graded_at: datetime | None = None
    posted_at: datetime | None = None
    cached_due_date: datetime | None = None
    late: bool | None = None
    missing: bool | None = None
    excused: bool | None = None
    redo_request: bool | None = None
    attempt: int | None = None
    seconds_late: int | None = None
    late_policy_status: int | str | None = None
    body: str | None = None
    url: str | None = None
    preview_url: str | None = None
    attachments: list[dict[str, Any]] | None = None
    points_deducted: float | None = None
    extra_attempts: int | None = None
    sticker: dict[str, Any] | None = None
    custom_grade_status_id: int | None = None
    grading_period_id: int | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return fields represented by the existing submissions table."""
        return self.model_dump(
            exclude_none=True,
            exclude={
                "id",
                "grader_id",
                "attachments",
                "sticker",
                "custom_grade_status_id",
                "grading_period_id",
            },
        )


class AvailabilityStatusDTO(CanvasDTO):
    status: str | None = None
    date: datetime | None = None


class LockInfoDTO(CanvasDTO):
    lock_at: datetime | None = None
    can_view: bool | None = None
    asset_string: str | None = None


class ScoreStatisticsDTO(CanvasDTO):
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    median: float | None = None
    lower_q: float | None = None
    upper_q: float | None = None


class AssignmentDTO(CanvasDTO):
    id: int
    course_id: int | None = None
    name: str | None = None
    description: str | None = None
    points_possible: float | None = None
    due_at: datetime | None = None
    lock_at: datetime | None = None
    unlock_at: datetime | None = None
    workflow_state: str | None = None
    grading_type: str | None = None
    submission_types: list[str] | None = None
    html_url: str | None = None
    important_dates: Any | None = None
    muted: bool | None = None
    availability_status: dict[str, Any] | None = None
    lock_info: dict[str, Any] | None = None
    integration_data: dict[str, Any] | None = None
    submission: SubmissionDTO | None = None
    score_statistics: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return assignment columns; nested submission is synced separately."""
        return self.model_dump(
            exclude_none=True,
            exclude={"id", "submission"},
        )


_SENSITIVE_SUBMISSION_FIELDS = frozenset(
    {"body", "preview_url", "url", "attachments"}
)


def _same_user(value: Any, tenant_user_id: int | str | None) -> bool:
    if value is None or tenant_user_id is None:
        return False
    try:
        return int(value) == int(tenant_user_id)
    except (TypeError, ValueError):
        return str(value) == str(tenant_user_id)


def _enrollment_user_id(enrollment: dict[str, Any]) -> Any:
    if enrollment.get("user_id") is not None:
        return enrollment["user_id"]
    user = enrollment.get("user")
    if isinstance(user, dict):
        return user.get("id", user.get("user_id"))
    return None


def _looks_like_enrollment(value: dict[str, Any]) -> bool:
    return (
        "user_id" in value or isinstance(value.get("user"), dict)
    ) and bool(
        {
            "role",
            "role_id",
            "enrollment_state",
            "limit_privileges_to_course_section",
            "course_id",
            "user",
        }
        & value.keys()
    )


def _strip_mapping(value: dict[str, Any], tenant_user_id: int | str | None) -> dict[str, Any] | None:
    """Sanitize a payload mapping without mutating the caller's object."""
    result = deepcopy(value)

    if _looks_like_enrollment(result) and not _same_user(
        _enrollment_user_id(result), tenant_user_id
    ):
        return None

    if isinstance(result.get("enrollments"), list):
        kept: list[Any] = []
        for enrollment in result["enrollments"]:
            if not isinstance(enrollment, dict):
                continue
            if not _same_user(_enrollment_user_id(enrollment), tenant_user_id):
                continue
            cleaned = _strip_mapping(enrollment, tenant_user_id)
            if cleaned is not None:
                kept.append(cleaned)
        result["enrollments"] = kept

    submission = result.get("submission")
    if isinstance(submission, dict) and not _same_user(
        submission.get("user_id"), tenant_user_id
    ):
        for field in _SENSITIVE_SUBMISSION_FIELDS:
            submission.pop(field, None)

    if "user_id" in result and any(
        field in result for field in _SENSITIVE_SUBMISSION_FIELDS
    ) and not _same_user(result.get("user_id"), tenant_user_id):
        for field in _SENSITIVE_SUBMISSION_FIELDS:
            result.pop(field, None)

    return result



def strip_peer_data(
    dto: CanvasDTO | dict[str, Any],
    tenant_user_id: int | str | None,
) -> CanvasDTO | dict[str, Any] | None:
    """Remove peer-linked records and sensitive submission content.

    DTO inputs are returned as a validated copy of the same DTO type. Dict
    inputs are copied before sanitization. No network or persistence occurs.
    """
    if isinstance(dto, dict):
        return _strip_mapping(dto, tenant_user_id)
    if isinstance(dto, CanvasDTO):
        if isinstance(dto, EnrollmentDTO) and not _same_user(
            _enrollment_user_id(dto.model_dump(mode="python")), tenant_user_id
        ):
            return None
        cleaned = _strip_mapping(dto.model_dump(mode="python"), tenant_user_id)
        if cleaned is None:
            return None
        return type(dto).model_validate(cleaned)
    raise TypeError("strip_peer_data expects a CanvasDTO or dict")


def parse_dto(model: type[CanvasDTO], payload: dict[str, Any]) -> CanvasDTO:
    return model.model_validate(payload)


def parse_many(
    model: type[CanvasDTO], payloads: Iterable[dict[str, Any]]
) -> list[CanvasDTO]:
    parsed: list[CanvasDTO] = []
    for payload in payloads:
        try:
            parsed.append(parse_dto(model, payload))
        except ValidationError:
            continue
    return parsed


__all__ = [
    "AssignmentDTO",
    "AvailabilityStatusDTO",
    "CanvasDTO",
    "CourseDTO",
    "EnrollmentDTO",
    "LockInfoDTO",
    "ScoreStatisticsDTO",
    "SubmissionDTO",
    "TermDTO",
    "UserDTO",
    "parse_dto",
    "parse_many",
    "strip_peer_data",
]


def _selftest() -> None:
    """Run whitelist and peer-isolation assertions with synthetic data."""
    user = UserDTO.model_validate(
        {"id": 7, "name": "selftest", "future_field": "ignored"}
    )
    assert user.id == 7
    assert "future_field" not in user.model_dump()
    cleaned = strip_peer_data(
        {"submission": {"id": 9, "user_id": 8, "body": "peer"}},
        tenant_user_id=7,
    )
    assert isinstance(cleaned, dict)
    assert "body" not in cleaned["submission"]
    # Canvas regression: when an assignment has no important dates the
    # ``important_dates`` field is the boolean ``False`` rather than a dict.
    # The DTO must accept either shape so the sync does not abort with
    # ``schema_drift``.
    parsed = AssignmentDTO.model_validate(
        {
            "id": 42,
            "course_id": 6669,
            "name": "quiz without important dates",
            "important_dates": False,
        }
    )
    assert parsed.important_dates is False
    parsed_dict = AssignmentDTO.model_validate(
        {
            "id": 43,
            "course_id": 6669,
            "name": "assignment with important dates",
            "important_dates": {"id": 7, "start_at": "2026-08-01T00:00:00Z"},
        }
    )
    assert isinstance(parsed_dict.important_dates, dict)


if __name__ == "__main__":
    _selftest()
