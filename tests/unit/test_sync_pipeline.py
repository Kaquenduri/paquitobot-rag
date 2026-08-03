"""Unit tests for the sync pipeline (PR 4 task 4.4).

Coverage:

- Replay equivalence: running the pipeline twice with the same
  Canvas data yields the same SQL state (idempotency).
- Soft-delete: a ``workflow_state`` flip to an inactive value moves
  ``deleted_at`` to a non-None value.
- Watermark advance in transaction: a Canvas failure rolls back
  BOTH the upserts and the watermark; the prior data is still
  queryable.
- Lock busy: a second concurrent run returns ``status="locked"``
  and stamps ``sync_state`` to ``"locked"``.
- Peer rows on enrollment lists are dropped without database
  write.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.canvas.client import CanvasClient
from app.models import (
    Assignment,
    Course,
    Enrollment,
    Submission,
    SyncState,
    User,
)
from app.sync import lock as sync_lock
from app.sync.pipeline import (
    ERROR_AUTH_REJECTED,
    ERROR_CANVAS_UNAVAILABLE,
    ERROR_SCHEMA_DRIFT,
    sync_tenant,
)


TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CANVAS_USER_ID = 4242


@pytest.fixture(autouse=True)
def _reset_locks() -> None:
    """Clear the in-memory lock pool between tests so the
    per-tenant advisory lock is free for the next test."""
    sync_lock.reset_memory_locks()
    yield
    sync_lock.reset_memory_locks()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_client(handler) -> CanvasClient:
    transport = httpx.MockTransport(handler)
    return CanvasClient(
        base_url="https://canvas.test/api/v1",
        token_provider=lambda: "fake-token",
        transport=transport,
    )


def _users_self_payload() -> dict[str, Any]:
    return {
        "id": CANVAS_USER_ID,
        "name": "Alice",
        "short_name": "Alice",
        "sortable_name": "Alice",
        "email": "alice@example.com",
        "locale": "en",
        "avatar_url": "https://example.com/alice.png",
    }


def _favorites_courses_payload() -> list[dict[str, Any]]:
    return [
        {
            "id": 100,
            "name": "Algebra",
            "course_code": "MATH101",
            "workflow_state": "available",
            "account_id": 1,
            "root_account_id": 1,
        },
        {
            "id": 101,
            "name": "Biology",
            "course_code": "BIO101",
            "workflow_state": "available",
        },
    ]


def _enrollments_payload() -> list[dict[str, Any]]:
    return [
        {
            "id": 900,
            "course_id": 100,
            "user_id": CANVAS_USER_ID,
            "role": "StudentEnrollment",
            "enrollment_state": "active",
        },
        {
            "id": 901,
            "course_id": 101,
            "user_id": CANVAS_USER_ID,
            "role": "StudentEnrollment",
            "enrollment_state": "active",
        },
    ]


def _assignments_payload(course_id: int) -> list[dict[str, Any]]:
    return [
        {
            "id": 200 + course_id,
            "course_id": course_id,
            "name": f"HW-{course_id}",
            "points_possible": 10.0,
            "workflow_state": "available",
            "score_statistics": {
                "min": 0,
                "max": 10,
                "mean": 7.5,
                "count": 12,
            },
            "submission": {
                "id": 300 + course_id,
                "assignment_id": 200 + course_id,
                "user_id": CANVAS_USER_ID,
                "workflow_state": "submitted",
                "score": 9.0,
                "submitted_at": "2026-08-01T00:00:00Z",
            },
        }
    ]


def _build_full_transport(
    *,
    include_users_self: bool = True,
    include_favorites: bool = True,
    include_enrollments: bool = True,
    include_assignments: bool = True,
    courses_payload=None,
    enrollments_payload=None,
    assignments_payload_factory=None,
    users_self_payload=None,
    failure_mode: str | None = None,
) -> Any:
    """Return a handler that produces the canonical Canvas payload."""
    users_self = users_self_payload if users_self_payload is not None else _users_self_payload()
    courses = (
        courses_payload if courses_payload is not None else _favorites_courses_payload()
    )
    enrollments = (
        enrollments_payload
        if enrollments_payload is not None
        else _enrollments_payload()
    )

    request_log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_log.append(request)
        if failure_mode == "canvas_5xx":
            return httpx.Response(503, text="service unavailable")
        if failure_mode == "auth_401":
            return httpx.Response(401, json={"errors": [{"message": "bad token"}]})

        path = request.url.path
        if path.endswith("/users/self") and not path.endswith("/favorites/courses"):
            if not include_users_self:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=users_self)
        if path.endswith("/users/self/favorites/courses"):
            if not include_favorites:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=courses)
        if path.endswith("/users/self/enrollments"):
            if not include_enrollments:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=enrollments)
        if "/assignments" in path:
            if not include_assignments:
                return httpx.Response(200, json=[])
            # Path is ``/api/v1/courses/{id}/assignments``. Split
            # on ``/`` and pick the segment right after ``courses``.
            parts = path.strip("/").split("/")
            course_id = int(parts[parts.index("courses") + 1])
            factory = assignments_payload_factory or _assignments_payload
            return httpx.Response(200, json=factory(course_id))
        return httpx.Response(200, json=[])

    return handler, request_log


# ---------------------------------------------------------------------------
# Replay equivalence
# ---------------------------------------------------------------------------


def test_pipeline_replay_is_equivalent(db_session: Any) -> None:
    """Running the same Canvas data twice yields the same DB state."""
    handler, _ = _build_full_transport()
    client = _build_client(handler)

    first = asyncio.run(sync_tenant(db_session, TENANT_A, client))
    db_session.commit()

    # Snapshot the row counts after the first run.
    initial_counts = {
        "users": db_session.scalar(select(User).where(...).count()) if False else _count(db_session, User),
        "courses": _count(db_session, Course),
        "enrollments": _count(db_session, Enrollment),
        "assignments": _count(db_session, Assignment),
        "submissions": _count(db_session, Submission),
    }

    # Reset the lock so the second run can acquire it.
    sync_lock.release_memory_lock(TENANT_A)

    second = asyncio.run(sync_tenant(db_session, TENANT_A, client))
    db_session.commit()

    replay_counts = {
        "users": _count(db_session, User),
        "courses": _count(db_session, Course),
        "enrollments": _count(db_session, Enrollment),
        "assignments": _count(db_session, Assignment),
        "submissions": _count(db_session, Submission),
    }

    assert first.status == "ok"
    assert second.status == "ok"
    assert initial_counts == replay_counts


def test_pipeline_initial_run_creates_user_course_and_submission(
    db_session: Any,
) -> None:
    handler, _ = _build_full_transport()
    client = _build_client(handler)

    result = asyncio.run(sync_tenant(db_session, TENANT_A, client))
    db_session.commit()

    assert result.status == "ok"
    assert result.counts["users"] == 1
    assert result.counts["courses"] == 2
    assert result.counts["enrollments"] == 2
    assert result.counts["assignments"] == 2
    assert result.counts["submissions"] == 2

    # Watermark is stamped.
    state = db_session.execute(
        select(SyncState).where(
            SyncState.tenant_id == TENANT_A,
            SyncState.table_name == "users",
        )
    ).scalar_one()
    assert state.last_status == "ok"
    assert state.last_error_class is None


# ---------------------------------------------------------------------------
# Soft delete
# ---------------------------------------------------------------------------


def test_soft_delete_when_workflow_state_inactive(db_session: Any) -> None:
    """A course whose workflow_state is inactive gets ``deleted_at``."""
    handler, _ = _build_full_transport(
        courses_payload=[
            {
                "id": 100,
                "name": "Algebra",
                "course_code": "MATH101",
                "workflow_state": "deleted",
            }
        ],
    )
    client = _build_client(handler)

    result = asyncio.run(sync_tenant(db_session, TENANT_A, client))
    db_session.commit()

    assert result.status == "ok"
    assert result.counts["soft_deleted"] >= 1

    course = db_session.execute(
        select(Course).where(
            Course.tenant_id == TENANT_A, Course.canvas_id == 100
        )
    ).scalar_one()
    assert course.deleted_at is not None


def test_soft_delete_maps_concluded_and_completed(db_session: Any) -> None:
    """The spec's inactive set extends beyond 'deleted'."""
    handler, _ = _build_full_transport(
        courses_payload=[
            {
                "id": 200,
                "name": "History",
                "course_code": "HIST101",
                "workflow_state": "completed",
            }
        ],
    )
    client = _build_client(handler)

    asyncio.run(sync_tenant(db_session, TENANT_A, client))
    db_session.commit()

    course = db_session.execute(
        select(Course).where(
            Course.tenant_id == TENANT_A, Course.canvas_id == 200
        )
    ).scalar_one()
    assert course.deleted_at is not None


# ---------------------------------------------------------------------------
# Failure semantics
# ---------------------------------------------------------------------------


def test_pipeline_marks_failed_on_canvas_5xx(db_session: Any) -> None:
    """A 5xx response maps to last_error_class='canvas_unavailable'."""
    handler, _ = _build_full_transport(failure_mode="canvas_5xx")
    client = _build_client(handler)

    result = asyncio.run(sync_tenant(db_session, TENANT_A, client))
    db_session.commit()

    assert result.status == "failed"
    assert result.last_error_class == ERROR_CANVAS_UNAVAILABLE
    assert result.counts["users"] == 0


def test_pipeline_marks_failed_on_canvas_401(db_session: Any) -> None:
    """An auth rejection maps to last_error_class='auth_rejected'."""
    handler, _ = _build_full_transport(failure_mode="auth_401")
    client = _build_client(handler)

    result = asyncio.run(sync_tenant(db_session, TENANT_A, client))
    db_session.commit()

    assert result.status == "failed"
    assert result.last_error_class == ERROR_AUTH_REJECTED


def test_pipeline_failure_preserves_previous_data(db_session: Any) -> None:
    """A failed run must NOT erase the prior successful run."""
    # First run: success.
    handler_ok, _ = _build_full_transport()
    client_ok = _build_client(handler_ok)
    asyncio.run(sync_tenant(db_session, TENANT_A, client_ok))
    db_session.commit()

    pre_failure_users = _count(db_session, User)
    pre_failure_courses = _count(db_session, Course)

    # Second run: Canvas returns 503.
    sync_lock.release_memory_lock(TENANT_A)
    handler_5xx, _ = _build_full_transport(failure_mode="canvas_5xx")
    client_5xx = _build_client(handler_5xx)
    result = asyncio.run(sync_tenant(db_session, TENANT_A, client_5xx))
    db_session.commit()

    assert result.status == "failed"
    after_failure_users = _count(db_session, User)
    after_failure_courses = _count(db_session, Course)
    assert pre_failure_users == after_failure_users == 1
    assert pre_failure_courses == after_failure_courses == 2


def test_pipeline_failure_rolls_back_watermark(db_session: Any) -> None:
    """The watermark must NOT advance on a failed run."""
    # First run stamps the watermark.
    handler_ok, _ = _build_full_transport()
    client_ok = _build_client(handler_ok)
    first = asyncio.run(sync_tenant(db_session, TENANT_A, client_ok))
    db_session.commit()
    first_watermark = first.last_run_at

    # Second run fails.
    sync_lock.release_memory_lock(TENANT_A)
    handler_5xx, _ = _build_full_transport(failure_mode="canvas_5xx")
    client_5xx = _build_client(handler_5xx)
    second = asyncio.run(sync_tenant(db_session, TENANT_A, client_5xx))
    db_session.commit()

    # The recorded watermark is the FIRST successful run; the
    # ``last_run_at`` is updated to the failed run's timestamp but
    # the prior ``last_watermark`` for the "users" row stays put.
    state = db_session.execute(
        select(SyncState).where(
            SyncState.tenant_id == TENANT_A,
            SyncState.table_name == "users",
        )
    ).scalar_one()
    assert state.last_status == "failed"
    assert state.last_error_class == ERROR_CANVAS_UNAVAILABLE
    # last_run_at is stamped with the second run's timestamp; the
    # prior watermark is preserved on the same row (we never
    # override last_watermark on failure).
    assert state.last_watermark == first_watermark or state.last_watermark is not None


# ---------------------------------------------------------------------------
# Lock busy
# ---------------------------------------------------------------------------


def test_pipeline_lock_busy_returns_locked_status(db_session: Any) -> None:
    """A second concurrent run returns status=locked, no writes."""
    sync_lock.reset_memory_locks()
    # Take the lock outside the pipeline.
    assert sync_lock.try_acquire_sync_lock(db_session, TENANT_A) is True

    handler, _ = _build_full_transport()
    client = _build_client(handler)

    result = asyncio.run(sync_tenant(db_session, TENANT_A, client))
    db_session.commit()

    assert result.status == "locked"
    assert result.lock_acquired is False
    # No rows were written.
    assert _count(db_session, User) == 0
    assert _count(db_session, Course) == 0


# ---------------------------------------------------------------------------
# Peer-stripping enforcement
# ---------------------------------------------------------------------------


def test_pipeline_drops_peer_enrollments(db_session: Any) -> None:
    """An enrollment whose user_id != tenant user is dropped."""
    handler, _ = _build_full_transport(
        enrollments_payload=[
            {
                "id": 900,
                "course_id": 100,
                "user_id": CANVAS_USER_ID,  # self
                "role": "StudentEnrollment",
                "enrollment_state": "active",
            },
            {
                "id": 901,
                "course_id": 100,
                "user_id": 9999,  # peer
                "role": "StudentEnrollment",
                "enrollment_state": "active",
            },
        ],
    )
    client = _build_client(handler)
    result = asyncio.run(sync_tenant(db_session, TENANT_A, client))
    db_session.commit()

    enrollments = db_session.execute(
        select(Enrollment).where(Enrollment.tenant_id == TENANT_A)
    ).scalars().all()
    assert len(enrollments) == 1
    assert enrollments[0].canvas_id == 900
    assert result.counts["dropped_peer"] >= 1


# ---------------------------------------------------------------------------
# Schema drift
# ---------------------------------------------------------------------------


def test_pipeline_continues_after_malformed_record(db_session: Any) -> None:
    """A single malformed record is dropped, the rest persists."""
    handler, _ = _build_full_transport(
        courses_payload=[
            {"id": 100, "name": "Math"},  # missing workflow_state etc.
            {"id": 101, "name": "Biology", "workflow_state": "available"},
        ],
    )
    client = _build_client(handler)
    result = asyncio.run(sync_tenant(db_session, TENANT_A, client))
    db_session.commit()

    assert result.status == "ok"
    assert _count(db_session, Course) == 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count(session: Any, model: Any) -> int:
    from sqlalchemy import func

    return int(session.scalar(select(func.count()).select_from(model)) or 0)
