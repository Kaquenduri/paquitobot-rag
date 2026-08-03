"""Canvas LMS integration package (PR 4).

This package owns every read-only interaction with the Canvas REST API.

- :mod:`app.canvas.dto` exposes the Pydantic DTOs that whitelists the
  fields the rest of the service is allowed to see. Unknown keys are
  dropped at parse time (``extra="ignore"``) and peer-identifying
  fields are stripped via :func:`strip_peer_data` so a payload that
  carries a classmate's record never reaches the database.
- :mod:`app.canvas.client` is the GET-only HTTP client. The mutation
  guard fails FAST — POST/PUT/PATCH/DELETE raise :class:`CanvasMethodRejected`
  before any network call.
- :mod:`app.canvas.pagination` consumes the ``Link: <url>; rel="next"``
  header Canvas emits, persisting a per-page cursor so every item is
  delivered exactly once per iteration.

The sync pipeline (:mod:`app.sync.pipeline`) composes these three
modules to drive the periodic and manual sync flows.
"""

from __future__ import annotations

from app.canvas.client import (
    CanvasClient,
    CanvasMethodRejected,
    CanvasRequestError,
)
from app.canvas.dto import (
    AssignmentDTO,
    AvailabilityStatusDTO,
    CanvasDTO,
    CourseDTO,
    EnrollmentDTO,
    LockInfoDTO,
    ScoreStatisticsDTO,
    SubmissionDTO,
    UserDTO,
    strip_peer_data,
)
from app.canvas.pagination import CanvasPaginationError, paginate

__all__ = [
    "AssignmentDTO",
    "AvailabilityStatusDTO",
    "CanvasClient",
    "CanvasDTO",
    "CanvasMethodRejected",
    "CanvasPaginationError",
    "CanvasRequestError",
    "CourseDTO",
    "EnrollmentDTO",
    "LockInfoDTO",
    "ScoreStatisticsDTO",
    "SubmissionDTO",
    "UserDTO",
    "paginate",
    "strip_peer_data",
]


def _selftest() -> None:
    """Verify the package exposes the read-only Canvas surface."""
    assert CanvasClient.ALLOWED_METHODS == {"GET"}
    assert UserDTO.model_validate({"id": 1}).id == 1
    assert callable(paginate)


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    _selftest()
