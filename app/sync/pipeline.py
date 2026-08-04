"""Tenant-scoped, idempotent Canvas synchronization pipeline.

A run has one transaction boundary.  The watermark is read after that
boundary starts, every DTO is normalized and upserted inside it, and the
watermark is advanced only after all requested resources finish.  A failure
rolls the work transaction back and is then recorded in a short, separate
transaction without changing the previous watermark.

The public coroutine intentionally accepts a synchronous SQLAlchemy
:class:`~sqlalchemy.orm.Session`: Canvas I/O is asynchronous while the ORM
helpers are deliberately transaction-oriented and are kept in one place.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.canvas.client import CanvasClient, CanvasMethodRejected, CanvasRequestError
from app.canvas.dto import (
    AssignmentDTO,
    AvailabilityStatusDTO,
    CourseDTO,
    EnrollmentDTO,
    LockInfoDTO,
    ScoreStatisticsDTO,
    SubmissionDTO,
    UserDTO,
    parse_dto,
    strip_peer_data,
)
from app.canvas.pagination import CanvasPaginationError, paginate
from app.core.logging import get_logger
from app.models import Assignment, Course, Enrollment, Submission, SyncState, User
from app.repositories.upsert import (
    soft_delete_if_inactive,
    upsert_by_canvas_id,
)
from app.sync.lock import release_sync_lock, try_acquire_sync_lock
from app.sync.watermark import advance_watermark, read_watermark

logger = get_logger("app.sync.pipeline")

# Keep one canonical sync-state row.  PR 3's watermark seam uses ``users``;
# retaining that name also makes the state compatible with existing rows.
SYNC_STATE_TABLE = "users"

ERROR_CANVAS_UNAVAILABLE = "canvas_unavailable"
ERROR_SCHEMA_DRIFT = "schema_drift"
ERROR_AUTH_REJECTED = "auth_rejected"
ERROR_UNEXPECTED = "unexpected"


class CanvasSchemaDriftError(RuntimeError):
    """Raised when a successful Canvas response cannot match its DTO shape."""


class SyncResult:
    """Safe, serializable outcome of a tenant sync run."""

    __slots__ = (
        "counts",
        "last_error_class",
        "last_run_at",
        "last_status",
        "last_successful_at",
        "lock_acquired",
        "status",
        "tenant_id",
        "throttled",
    )

    def __init__(
        self,
        *,
        tenant_id: uuid.UUID,
        status: str,
        last_status: str,
        last_error_class: str | None,
        last_successful_at: datetime | None,
        last_run_at: datetime,
        counts: dict[str, int],
        lock_acquired: bool,
        throttled: bool = False,
    ) -> None:
        self.tenant_id = tenant_id
        self.status = status
        self.last_status = last_status
        self.last_error_class = last_error_class
        self.last_successful_at = last_successful_at
        self.last_run_at = last_run_at
        self.counts = counts
        self.lock_acquired = lock_acquired
        self.throttled = throttled

    def to_dict(self) -> dict[str, Any]:
        """Return the controller-safe response shape."""
        return {
            "status": self.status,
            "last_successful_at": _isoformat(self.last_successful_at),
            "last_run_at": _isoformat(self.last_run_at),
            "last_status": self.last_status,
            "last_error_class": self.last_error_class,
            "counts": dict(self.counts),
            "lock_acquired": self.lock_acquired,
        }


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _tenant_uuid(value: uuid.UUID | str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("tenant_id must be a UUID") from exc


def _new_counts() -> dict[str, int]:
    return {
        "users": 0,
        "courses": 0,
        "enrollments": 0,
        "assignments": 0,
        "submissions": 0,
        "soft_deleted": 0,
        "dropped_peer": 0,
    }


def _transaction_scope(
    session: Session,
) -> tuple[AbstractContextManager[Any], bool]:
    """Return a transaction context and whether this function owns it.

    A caller may already be composing a larger request transaction.  In that
    case a savepoint protects the sync work without committing unrelated work;
    with a fresh session the normal transaction is committed on successful
    context exit.
    """
    if session.in_transaction():
        return session.begin_nested(), False
    return session.begin(), True


async def sync_tenant(
    session: Session,
    tenant_id: uuid.UUID | str,
    canvas_client: CanvasClient,
    *,
    lock_id: object | None = None,
    now: datetime | None = None,
) -> SyncResult:
    """Synchronize one tenant atomically.

    The supported Canvas reads are:

    * ``GET /users/self``
    * ``GET /users/self/favorites/courses``
    * ``GET /courses/{id}/assignments`` with both required ``include[]``
      values.

    Favorites normally contain the tenant's enrollment.  A compatibility
    fallback to ``/users/self/enrollments`` is used only when a real Canvas
    client returns no embedded enrollment, preserving older Canvas fixtures
    without making peer data part of the normal path.

    ``lock_id`` means the caller already acquired the same transaction-scoped
    lock.  When it is ``None`` this function acquires the lock itself.  The
    optional ``now`` argument is a deterministic clock seam for local
    assertions; production callers should omit it.
    """
    tenant_uuid = _tenant_uuid(tenant_id)
    run_at = _utc(now) or datetime.now(UTC)
    counts = _new_counts()
    previous_watermark: datetime | None = None
    previous_successful_at: datetime | None = None
    acquired_here = False
    result: SyncResult | None = None
    scope, owns_transaction = _transaction_scope(session)

    try:
        with scope:
            # Both watermark operations are deliberately inside this same
            # transaction context as the lock and all upserts.
            previous_watermark = read_watermark(
                session, str(tenant_uuid), SYNC_STATE_TABLE
            )
            previous_state = _read_sync_state(session, tenant_uuid)
            previous_successful_at = _last_successful_at(
                previous_state, previous_watermark
            )

            if lock_id is None:
                acquired = try_acquire_sync_lock(session, tenant_uuid)
                acquired_here = acquired
            else:
                acquired = True

            if not acquired:
                result = SyncResult(
                    tenant_id=tenant_uuid,
                    status="locked",
                    last_status="locked",
                    last_error_class=None,
                    last_successful_at=previous_successful_at,
                    last_run_at=run_at,
                    counts=counts,
                    lock_acquired=False,
                )
            else:
                tenant_user_id = await _sync_self_user(
                    session, canvas_client, tenant_uuid, counts
                )
                course_ids, embedded_enrollments = await _sync_favorite_courses(
                    session,
                    canvas_client,
                    tenant_uuid,
                    tenant_user_id,
                    counts,
                    updated_after=previous_watermark,
                )

                # Canvas embeds the authenticated user's enrollment in the
                # favorites response.  The fallback exists for older/test
                # payloads and never requests a full peer enrollment list.
                if embedded_enrollments == 0:
                    await _sync_fallback_enrollments(
                        session,
                        canvas_client,
                        tenant_uuid,
                        tenant_user_id,
                        counts,
                    )

                for course_id in course_ids:
                    await _sync_course_assignments(
                        session,
                        canvas_client,
                        tenant_uuid,
                        tenant_user_id,
                        course_id,
                        counts,
                        updated_after=previous_watermark,
                    )

                # This is the only successful watermark advance.  It occurs
                # before the transaction context exits, so any later commit
                # failure cannot publish a partially advanced cursor.
                advance_watermark(
                    session,
                    str(tenant_uuid),
                    SYNC_STATE_TABLE,
                    run_at,
                    status="ok",
                    error_class=None,
                    run_at=run_at,
                )
                result = SyncResult(
                    tenant_id=tenant_uuid,
                    status="ok",
                    last_status="ok",
                    last_error_class=None,
                    last_successful_at=run_at,
                    last_run_at=run_at,
                    counts=counts,
                    lock_acquired=True,
                )

        # ``scope`` commits here when this function owns the transaction.  A
        # caller-owned outer transaction only releases its savepoint.
        assert result is not None
        return result
    except CanvasMethodRejected as exc:
        logger.error(
            "sync_method_rejected",
            tenant_id=str(tenant_uuid),
            method=exc.method,
        )
        return _finish_failure(
            session,
            tenant_uuid,
            run_at,
            counts,
            previous_watermark,
            previous_successful_at,
            ERROR_UNEXPECTED,
            owns_transaction=owns_transaction,
        )
    except CanvasRequestError as exc:
        error_class = _classify_canvas_error(exc)
        logger.warning(
            "sync_canvas_failure",
            tenant_id=str(tenant_uuid),
            status_code=exc.status_code,
            error_class=error_class,
        )
        return _finish_failure(
            session,
            tenant_uuid,
            run_at,
            counts,
            previous_watermark,
            previous_successful_at,
            error_class,
            owns_transaction=owns_transaction,
        )
    except CanvasSchemaDriftError:
        logger.warning(
            "sync_schema_drift",
            tenant_id=str(tenant_uuid),
            error_class=ERROR_SCHEMA_DRIFT,
        )
        return _finish_failure(
            session,
            tenant_uuid,
            run_at,
            counts,
            previous_watermark,
            previous_successful_at,
            ERROR_SCHEMA_DRIFT,
            owns_transaction=owns_transaction,
        )
    except CanvasPaginationError:
        logger.warning(
            "sync_schema_drift",
            tenant_id=str(tenant_uuid),
            error_class=ERROR_SCHEMA_DRIFT,
        )
        return _finish_failure(
            session,
            tenant_uuid,
            run_at,
            counts,
            previous_watermark,
            previous_successful_at,
            ERROR_SCHEMA_DRIFT,
            owns_transaction=owns_transaction,
        )
    except httpx.HTTPError as exc:
        error_class = (
            ERROR_SCHEMA_DRIFT
            if "json" in str(exc).lower() or "payload" in str(exc).lower()
            else ERROR_CANVAS_UNAVAILABLE
        )
        logger.warning(
            "sync_http_failure",
            tenant_id=str(tenant_uuid),
            error_class=error_class,
        )
        return _finish_failure(
            session,
            tenant_uuid,
            run_at,
            counts,
            previous_watermark,
            previous_successful_at,
            error_class,
            owns_transaction=owns_transaction,
        )
    except ValidationError:
        return _finish_failure(
            session,
            tenant_uuid,
            run_at,
            counts,
            previous_watermark,
            previous_successful_at,
            ERROR_SCHEMA_DRIFT,
            owns_transaction=owns_transaction,
        )
    except Exception as exc:  # noqa: BLE001 - normalize the sync boundary
        logger.error(
            "sync_unexpected",
            tenant_id=str(tenant_uuid),
            error_class=exc.__class__.__name__,
        )
        return _finish_failure(
            session,
            tenant_uuid,
            run_at,
            counts,
            previous_watermark,
            previous_successful_at,
            ERROR_UNEXPECTED,
            owns_transaction=owns_transaction,
        )
    finally:
        # PostgreSQL releases at transaction end.  This call only matters for
        # the SQLite fallback and only when this invocation acquired it.
        if acquired_here:
            release_sync_lock(session, tenant_uuid)


async def _sync_self_user(
    session: Session,
    client: Any,
    tenant_id: uuid.UUID,
    counts: dict[str, int],
) -> int:
    payload = await _get_json(client, "/users/self")
    if not isinstance(payload, dict):
        raise CanvasSchemaDriftError("/users/self must return an object")
    dto = _parse(UserDTO, payload, "/users/self")
    instance = upsert_by_canvas_id(
        session,
        User,
        tenant_id=tenant_id,
        canvas_id=dto.id,
        payload=dto.to_payload(),
    )
    session.flush()
    counts["users"] += 1
    # ``instance`` is intentionally retained only to ensure the repository
    # returned an attached ORM row; no payload is logged.
    assert instance.tenant_id == tenant_id
    return int(dto.id)


async def _sync_favorite_courses(
    session: Session,
    client: Any,
    tenant_id: uuid.UUID,
    tenant_user_id: int,
    counts: dict[str, int],
    *,
    updated_after: datetime | None,
) -> tuple[list[int], int]:
    course_ids: list[int] = []
    embedded_enrollments = 0
    params = _updated_after_params(updated_after)

    async for item in _paginate_items(
        client,
        "/users/self/favorites/courses",
        params=params or None,
    ):
        if not isinstance(item, dict):
            raise CanvasSchemaDriftError("course page contained a non-object")
        dto = _parse(CourseDTO, item, "/users/self/favorites/courses")
        filtered = strip_peer_data(dto, tenant_user_id)
        if filtered is None or not isinstance(filtered, CourseDTO):
            counts["dropped_peer"] += 1
            continue

        upsert_by_canvas_id(
            session,
            Course,
            tenant_id=tenant_id,
            canvas_id=filtered.id,
            payload=filtered.to_payload(),
        )
        session.flush()
        if soft_delete_if_inactive(
            session,
            Course,
            tenant_id,
            filtered.id,
            filtered.workflow_state,
        ):
            counts["soft_deleted"] += 1
        counts["courses"] += 1
        course_ids.append(int(filtered.id))

        for enrollment in filtered.enrollments or []:
            if enrollment.id is None:
                counts["dropped_peer"] += 1
                continue
            if enrollment.user_id is not None and not _same_user(
                enrollment.user_id, tenant_user_id
            ):
                counts["dropped_peer"] += 1
                continue
            _upsert_enrollment(
                session,
                tenant_id,
                enrollment,
                fallback_course_id=filtered.id,
                counts=counts,
            )
            embedded_enrollments += 1

    return course_ids, embedded_enrollments


async def _sync_fallback_enrollments(
    session: Session,
    client: Any,
    tenant_id: uuid.UUID,
    tenant_user_id: int,
    counts: dict[str, int],
) -> None:
    """Read the optional self enrollment endpoint for legacy payloads."""
    try:
        async for item in _paginate_items(client, "/users/self/enrollments"):
            if not isinstance(item, dict):
                raise CanvasSchemaDriftError(
                    "/users/self/enrollments contained a non-object"
                )
            dto = _parse(EnrollmentDTO, item, "/users/self/enrollments")
            filtered = strip_peer_data(dto, tenant_user_id)
            if filtered is None or not isinstance(filtered, EnrollmentDTO):
                counts["dropped_peer"] += 1
                continue
            if filtered.id is None:
                counts["dropped_peer"] += 1
                continue
            _upsert_enrollment(
                session,
                tenant_id,
                filtered,
                fallback_course_id=filtered.course_id,
                counts=counts,
            )
    except CanvasRequestError as exc:
        # This endpoint is a compatibility fallback, not part of the normal
        # three-request contract.  A deployment that does not expose it can
        # still synchronize courses and assignments.
        if exc.status_code != 404:
            raise


def _upsert_enrollment(
    session: Session,
    tenant_id: uuid.UUID,
    dto: EnrollmentDTO,
    *,
    fallback_course_id: int | None,
    counts: dict[str, int],
) -> None:
    payload = dto.to_payload()
    course_canvas_id = dto.course_id or fallback_course_id
    course_uuid = _resolve_local_uuid(session, Course, tenant_id, course_canvas_id)
    user_uuid = _resolve_local_uuid(session, User, tenant_id, dto.user_id)
    if course_uuid is None:
        payload.pop("course_id", None)
    else:
        payload["course_id"] = course_uuid
    if user_uuid is None:
        payload.pop("user_id", None)
    else:
        payload["user_id"] = user_uuid

    instance = upsert_by_canvas_id(
        session,
        Enrollment,
        tenant_id=tenant_id,
        canvas_id=int(dto.id),
        payload=payload,
    )
    session.flush()
    if soft_delete_if_inactive(
        session,
        Enrollment,
        tenant_id,
        int(dto.id),
        dto.enrollment_state,
    ):
        counts["soft_deleted"] += 1
    counts["enrollments"] += 1
    assert instance.tenant_id == tenant_id


async def _sync_course_assignments(
    session: Session,
    client: Any,
    tenant_id: uuid.UUID,
    tenant_user_id: int,
    course_id: int,
    counts: dict[str, int],
    *,
    updated_after: datetime | None,
) -> None:
    course_uuid = _resolve_local_uuid(session, Course, tenant_id, course_id)
    params: dict[str, Any] = {
        "include[]": ["submission", "score_statistics"],
    }
    params.update(_updated_after_params(updated_after))

    async for item in _paginate_items(
        client,
        f"/courses/{course_id}/assignments",
        params=params,
    ):
        if not isinstance(item, dict):
            raise CanvasSchemaDriftError("assignment page contained a non-object")
        dto = _parse(AssignmentDTO, item, f"/courses/{course_id}/assignments")
        filtered = strip_peer_data(dto, tenant_user_id)
        if filtered is None or not isinstance(filtered, AssignmentDTO):
            counts["dropped_peer"] += 1
            continue

        assignment_payload = filtered.to_payload()
        if course_uuid is None:
            assignment_payload.pop("course_id", None)
        else:
            assignment_payload["course_id"] = course_uuid
        assignment_payload = _normalize_assignment_nested(assignment_payload)

        instance = upsert_by_canvas_id(
            session,
            Assignment,
            tenant_id=tenant_id,
            canvas_id=filtered.id,
            payload=assignment_payload,
        )
        session.flush()
        if soft_delete_if_inactive(
            session,
            Assignment,
            tenant_id,
            filtered.id,
            filtered.workflow_state,
        ):
            counts["soft_deleted"] += 1
        counts["assignments"] += 1
        assert instance.tenant_id == tenant_id

        submission = filtered.submission
        if submission is None:
            continue
        # An explicit foreign user id is a peer record.  Do not persist its
        # score, body, or URL; aggregate score_statistics stays permitted.
        if submission.user_id is not None and not _same_user(
            submission.user_id, tenant_user_id
        ):
            counts["dropped_peer"] += 1
            continue
        _upsert_submission(
            session,
            tenant_id,
            submission,
            assignment_canvas_id=filtered.id,
            tenant_user_id=tenant_user_id,
            counts=counts,
        )


def _normalize_assignment_nested(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply the nested DTO whitelists while retaining JSON-safe values."""
    normalized = dict(payload)
    nested_models = (
        ("score_statistics", ScoreStatisticsDTO),
        ("availability_status", AvailabilityStatusDTO),
        ("lock_info", LockInfoDTO),
    )
    for key, model in nested_models:
        value = normalized.get(key)
        if value is None:
            continue
        if not isinstance(value, dict):
            logger.warning(
                "sync_nested_not_object",
                assignment_field=key,
                received_type=type(value).__name__,
            )
            raise CanvasSchemaDriftError(f"assignment field {key!r} must be an object")
        try:
            nested = model.model_validate(value)
        except ValidationError as exc:
            logger.warning(
                "sync_nested_drift",
                assignment_field=key,
                exc_type=exc.__class__.__name__,
                error=str(exc)[:500],
            )
            raise CanvasSchemaDriftError(
                f"assignment field {key!r} failed DTO validation"
            ) from exc
        normalized[key] = nested.model_dump(mode="json", exclude_none=True)
    return normalized


def _upsert_submission(
    session: Session,
    tenant_id: uuid.UUID,
    dto: SubmissionDTO,
    *,
    assignment_canvas_id: int,
    tenant_user_id: int,
    counts: dict[str, int],
) -> None:
    payload = dto.to_payload()
    assignment_canvas_id = dto.assignment_id or assignment_canvas_id
    assignment_uuid = _resolve_local_uuid(
        session, Assignment, tenant_id, assignment_canvas_id
    )
    user_uuid = _resolve_local_uuid(session, User, tenant_id, dto.user_id)
    if assignment_uuid is None:
        payload.pop("assignment_id", None)
    else:
        payload["assignment_id"] = assignment_uuid
    if user_uuid is None:
        payload.pop("user_id", None)
    else:
        payload["user_id"] = user_uuid
    if payload.get("late_policy_status") is not None:
        payload["late_policy_status"] = str(payload["late_policy_status"])

    instance = upsert_by_canvas_id(
        session,
        Submission,
        tenant_id=tenant_id,
        canvas_id=dto.id,
        payload=payload,
    )
    session.flush()
    if soft_delete_if_inactive(
        session,
        Submission,
        tenant_id,
        dto.id,
        dto.workflow_state,
    ):
        counts["soft_deleted"] += 1
    counts["submissions"] += 1
    assert instance.tenant_id == tenant_id


def _resolve_local_uuid(
    session: Session,
    model: type[Any],
    tenant_id: uuid.UUID,
    canvas_id: int | None,
) -> uuid.UUID | None:
    if canvas_id is None:
        return None
    row = session.execute(
        select(model).where(
            model.tenant_id == tenant_id,
            model.canvas_id == canvas_id,
        )
    ).scalar_one_or_none()
    return getattr(row, "id", None) if row is not None else None


def _read_sync_state(session: Session, tenant_id: uuid.UUID) -> SyncState | None:
    return session.execute(
        select(SyncState).where(
            SyncState.tenant_id == tenant_id,
            SyncState.table_name == SYNC_STATE_TABLE,
        )
    ).scalar_one_or_none()


def _last_successful_at(
    state: SyncState | None,
    watermark: datetime | None,
) -> datetime | None:
    # ``last_watermark`` is deliberately the durable last-success cursor;
    # ``last_run_at`` may be overwritten by a failed attempt.
    if watermark is not None:
        return _utc(watermark)
    if state is not None and state.last_status == "ok":
        return _utc(state.last_run_at)
    return None


def _updated_after_params(watermark: datetime | None) -> dict[str, str]:
    if watermark is None:
        return {}
    return {"updated_after": (_utc(watermark) or watermark).isoformat()}


async def _get_json(
    client: Any,
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> Any:
    response = await client.get(path, params=params)
    if isinstance(response, (dict, list)):
        return response
    status_code = int(getattr(response, "status_code", 200))
    if status_code >= 400:
        raise CanvasRequestError(
            method="GET",
            path=path,
            status_code=status_code,
            message=f"Canvas endpoint failed with status {status_code}",
        )
    json_method = getattr(response, "json", None)
    if not callable(json_method):
        raise CanvasSchemaDriftError(f"Canvas response for {path} has no JSON body")
    try:
        payload = json_method()
        if inspect.isawaitable(payload):
            payload = await payload
    except (TypeError, ValueError) as exc:
        raise CanvasSchemaDriftError(f"Canvas response for {path} is not valid JSON") from exc
    return payload


async def _paginate_items(
    client: Any,
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Use the canonical paginator, with a small in-memory stub seam."""
    get_paginated = getattr(client, "get_paginated", None)
    if callable(get_paginated):
        values = get_paginated(path, params=params)
        if inspect.isawaitable(values):
            values = await values
        if hasattr(values, "__aiter__"):
            async for value in values:
                yield value
        else:
            for value in values:
                yield value
        return

    async for item in paginate(client, path, params=params):
        yield item


def _parse(model: type[Any], payload: dict[str, Any], path: str) -> Any:
    try:
        return parse_dto(model, payload)
    except (ValidationError, TypeError, ValueError) as exc:
        # Surface the exact field that failed so the next sync_starts
        # log entry points at the offending Canvas key rather than a
        # generic "mismatch at /path" message.
        error_fields: list[tuple[str, ...]] = []
        if hasattr(exc, "errors"):
            for err in exc.errors():
                if isinstance(err, dict) and "loc" in err:
                    error_fields.append(tuple(str(part) for part in err["loc"]))
        logger.warning(
            "sync_schema_drift_detail",
            path=path,
            exc_type=exc.__class__.__name__,
            error=str(exc)[:500],
            fields=error_fields or None,
        )
        raise CanvasSchemaDriftError(f"Canvas DTO mismatch at {path}") from exc


def _same_user(value: Any, expected: int | str | None) -> bool:
    if value is None or expected is None:
        return False
    try:
        return int(value) == int(expected)
    except (TypeError, ValueError):
        return str(value) == str(expected)


def _classify_canvas_error(exc: CanvasRequestError) -> str:
    if exc.status_code in (401, 403):
        return ERROR_AUTH_REJECTED
    return ERROR_CANVAS_UNAVAILABLE


def _write_failure_state(
    session: Session,
    tenant_id: uuid.UUID,
    run_at: datetime,
    previous_watermark: datetime | None,
    error_class: str,
) -> None:
    """Persist failure metadata without changing the durable watermark."""
    state = _read_sync_state(session, tenant_id)
    if state is None:
        state = SyncState(
            tenant_id=tenant_id,
            table_name=SYNC_STATE_TABLE,
            last_watermark=previous_watermark,
            last_run_at=run_at,
            last_status="failed",
            last_error_class=error_class,
        )
        session.add(state)
    else:
        state.last_run_at = run_at
        state.last_status = "failed"
        state.last_error_class = error_class
        # Deliberately do not assign ``last_watermark`` here.
    session.flush()


def _finish_failure(
    session: Session,
    tenant_id: uuid.UUID,
    run_at: datetime,
    counts: dict[str, int],
    previous_watermark: datetime | None,
    previous_successful_at: datetime | None,
    error_class: str,
    *,
    owns_transaction: bool,
) -> SyncResult:
    """Rollback sync work, then durably record only failure metadata."""
    try:
        if owns_transaction:
            # The work context has already rolled back before control reaches
            # this helper.  Start a fresh transaction for the status row.
            if session.in_transaction():
                session.rollback()
            with session.begin():
                _write_failure_state(
                    session,
                    tenant_id,
                    run_at,
                    previous_watermark,
                    error_class,
                )
        else:
            # A caller-owned outer transaction remains active after the
            # savepoint rollback.  Keep the failure marker in that transaction
            # and let the caller decide its final commit boundary.
            _write_failure_state(
                session,
                tenant_id,
                run_at,
                previous_watermark,
                error_class,
            )
    except Exception:  # noqa: BLE001 - failure recording is best effort
        logger.error(
            "sync_failure_status_not_recorded",
            tenant_id=str(tenant_id),
            error_class=error_class,
        )

    return SyncResult(
        tenant_id=tenant_id,
        status="failed",
        last_status="failed",
        last_error_class=error_class,
        last_successful_at=previous_successful_at,
        last_run_at=run_at,
        counts=counts,
        lock_acquired=False,
    )


__all__ = [
    "ERROR_AUTH_REJECTED",
    "ERROR_CANVAS_UNAVAILABLE",
    "ERROR_SCHEMA_DRIFT",
    "ERROR_UNEXPECTED",
    "SYNC_STATE_TABLE",
    "CanvasSchemaDriftError",
    "SyncResult",
    "sync_tenant",
]


def _selftest() -> None:
    """Run pure pipeline invariants without Canvas, SQL, or secrets."""
    fixed = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    result = SyncResult(
        tenant_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        status="ok",
        last_status="ok",
        last_error_class=None,
        last_successful_at=fixed,
        last_run_at=fixed,
        counts={"users": 1},
        lock_acquired=True,
    )
    assert result.to_dict()["status"] == "ok"
    assert result.to_dict()["last_successful_at"] == fixed.isoformat()
    assert _classify_canvas_error(
        CanvasRequestError(
            method="GET",
            path="/users/self",
            status_code=401,
            message="rejected",
        )
    ) == ERROR_AUTH_REJECTED
    assert _classify_canvas_error(
        CanvasRequestError(
            method="GET",
            path="/users/self",
            status_code=503,
            message="unavailable",
        )
    ) == ERROR_CANVAS_UNAVAILABLE
    assert _updated_after_params(fixed)["updated_after"].startswith("2026-08-03")


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    _selftest()
