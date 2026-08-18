"""Unit tests for the canvas_mock_* ORM models (PR 1 task 1.1).

These tests pin the **shape** of the new tables (column names, types,
nullability, unique constraints, and check-constraint expressions) so the
PR 1 migration and the canvas-mock extractor can rely on them. They are
intentionally written BEFORE the models exist in this PR — they go RED
on the first run and GREEN once :mod:`app.models.canvas_mock` ships
the matching classes (TASK-1-2).

Tables covered (9 total, prefixed ``canvas_mock_``):

- ``canvas_mock_users`` — tenant-scoped self-profile, optional api_key_prefix
- ``canvas_mock_courses`` — natural-key ``(tenant_id, canvas_mock_id)``
- ``canvas_mock_enrollments`` — natural-key ``(tenant_id, canvas_mock_id)``
- ``canvas_mock_class_sessions`` — natural-key ``(tenant_id, canvas_mock_id)``
- ``canvas_mock_assignments`` — natural-key ``(tenant_id, canvas_mock_id)``
- ``canvas_mock_attendance_records`` — CHECK on ``status``
- ``canvas_mock_grades`` — natural-key ``(tenant_id, assignment_canvas_mock_id, user_canvas_mock_id)``
- ``canvas_mock_webhook_events`` — composite idempotency key
- ``canvas_mock_webhook_subscriptions`` — per-tenant target URL
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import MappedColumn

from app.models import Base
from app.models.canvas_mock import (
    CanvasMockAssignment,
    CanvasMockAttendanceRecord,
    CanvasMockClassSession,
    CanvasMockCourse,
    CanvasMockEnrollment,
    CanvasMockGrade,
    CanvasMockUser,
    CanvasMockWebhookEvent,
    CanvasMockWebhookSubscription,
)

EXPECTED_MODEL_NAMES: tuple[str, ...] = (
    "CanvasMockUser",
    "CanvasMockCourse",
    "CanvasMockEnrollment",
    "CanvasMockClassSession",
    "CanvasMockAssignment",
    "CanvasMockAttendanceRecord",
    "CanvasMockGrade",
    "CanvasMockWebhookEvent",
    "CanvasMockWebhookSubscription",
)

EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "canvas_mock_users",
        "canvas_mock_courses",
        "canvas_mock_enrollments",
        "canvas_mock_class_sessions",
        "canvas_mock_assignments",
        "canvas_mock_attendance_records",
        "canvas_mock_grades",
        "canvas_mock_webhook_events",
        "canvas_mock_webhook_subscriptions",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_model_classes() -> tuple[type[Any], ...]:
    """Return every model class declared in :mod:`app.models.canvas_mock`."""
    from app.models import canvas_mock as module

    return tuple(
        getattr(module, name)
        for name in EXPECTED_MODEL_NAMES
    )


def _uq_column_names(constraint: UniqueConstraint) -> frozenset[str]:
    return frozenset(col.name for col in constraint.columns)


def _all_uq_names(model: type[Any]) -> frozenset[str]:
    return frozenset(c.name for c in model.__table__.constraints if isinstance(c, UniqueConstraint))


def _all_check_expressions(model: type[Any]) -> frozenset[str]:
    return frozenset(
        c.sqltext if hasattr(c, "sqltext") else str(c)
        for c in model.__table__.constraints
        if isinstance(c, CheckConstraint)
    )


# ---------------------------------------------------------------------------
# Module structure
# ---------------------------------------------------------------------------


def test_all_nine_models_are_importable() -> None:
    """Every locked canvas_mock_* model must be importable from the module."""
    for name in EXPECTED_MODEL_NAMES:
        cls = getattr(__import__("app.models.canvas_mock", fromlist=[name]), name)
        assert cls is not None, name


def test_all_nine_tables_are_registered_in_base_metadata() -> None:
    """The nine ``canvas_mock_*`` tables must be on ``Base.metadata``."""
    actual = {t.name for t in Base.metadata.tables.values()}
    missing = EXPECTED_TABLES - actual
    assert not missing, f"missing tables: {missing}"


# ---------------------------------------------------------------------------
# Per-model column + constraint assertions
# ---------------------------------------------------------------------------


def test_canvas_mock_users_columns_and_unique(db_session: Any) -> None:
    tenant = uuid.uuid4()
    user = CanvasMockUser(
        tenant_id=tenant,
        canvas_mock_id=12,
        name="Ana",
        email="ana@example.com",
        role="student",
    )
    db_session.add(user)
    db_session.flush()

    # Re-fetch from DB to make sure the mapped columns round-trip.
    fetched = db_session.query(CanvasMockUser).one()
    assert fetched.tenant_id == tenant
    assert fetched.canvas_mock_id == 12
    assert fetched.name == "Ana"
    assert fetched.email == "ana@example.com"
    assert fetched.role == "student"

    # Tenant-scoped natural key uniqueness.
    db_session.add(
        CanvasMockUser(tenant_id=tenant, canvas_mock_id=12, role="student")
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_canvas_mock_courses_columns_and_unique(db_session: Any) -> None:
    tenant = uuid.uuid4()
    other = uuid.uuid4()
    course = CanvasMockCourse(
        tenant_id=tenant,
        canvas_mock_id=101,
        name="Cálculo I",
        course_code="CALC-1",
        workflow_state="available",
    )
    other_tenant_same_id = CanvasMockCourse(
        tenant_id=other,
        canvas_mock_id=101,
        name="Other Cálculo",
        course_code="CALC-9",
    )
    db_session.add_all([course, other_tenant_same_id])
    db_session.flush()
    assert course.id is not None

    # Same (tenant_id, canvas_mock_id) cannot duplicate.
    db_session.add(
        CanvasMockCourse(
            tenant_id=tenant,
            canvas_mock_id=101,
            name="Dup",
            course_code="X",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_canvas_mock_enrollments_columns(db_session: Any) -> None:
    tenant = uuid.uuid4()
    enrollment = CanvasMockEnrollment(
        tenant_id=tenant,
        canvas_mock_id=555,
        user_canvas_mock_id=77,
        course_canvas_mock_id=101,
        type="StudentEnrollment",
        enrollment_state="active",
    )
    db_session.add(enrollment)
    db_session.flush()
    assert enrollment.id is not None


def test_canvas_mock_class_sessions_columns(db_session: Any) -> None:
    tenant = uuid.uuid4()
    session = CanvasMockClassSession(
        tenant_id=tenant,
        canvas_mock_id=9001,
        course_canvas_mock_id=101,
        start_at=None,  # populated by the extractor
        end_at=None,
    )
    db_session.add(session)
    db_session.flush()
    assert session.id is not None


def test_canvas_mock_assignments_columns(db_session: Any) -> None:
    tenant = uuid.uuid4()
    assignment = CanvasMockAssignment(
        tenant_id=tenant,
        canvas_mock_id=42,
        course_canvas_mock_id=101,
        name="Parcial 1",
        points_possible=20.0,
    )
    db_session.add(assignment)
    db_session.flush()
    assert assignment.id is not None


def test_canvas_mock_attendance_records_check_constraint(db_session: Any) -> None:
    """The CHECK constraint on ``status`` rejects anything outside the enum."""
    tenant = uuid.uuid4()
    record = CanvasMockAttendanceRecord(
        tenant_id=tenant,
        class_session_canvas_mock_id=9001,
        user_canvas_mock_id=77,
        status="tardy",  # not in {"present", "absent"}
    )
    db_session.add(record)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

    # The two valid statuses must insert without error.
    for status in ("present", "absent"):
        db_session.add(
            CanvasMockAttendanceRecord(
                tenant_id=tenant,
                class_session_canvas_mock_id=9001,
                user_canvas_mock_id=77,
                status=status,
            )
        )
    db_session.flush()
    assert (
        db_session.query(CanvasMockAttendanceRecord).count() == 2
    )


def test_canvas_mock_grades_composite_unique(db_session: Any) -> None:
    """Grades have a 3-column composite natural key."""
    tenant = uuid.uuid4()
    grade = CanvasMockGrade(
        tenant_id=tenant,
        assignment_canvas_mock_id=42,
        user_canvas_mock_id=77,
        score=18.0,
        grade="18",
    )
    db_session.add(grade)
    db_session.flush()

    # Same (tenant, assignment, user) cannot duplicate.
    dup = CanvasMockGrade(
        tenant_id=tenant,
        assignment_canvas_mock_id=42,
        user_canvas_mock_id=77,
        score=20.0,
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_canvas_mock_webhook_events_composite_idempotency(db_session: Any) -> None:
    """The composite ``(tenant_id, event, resource_id, attempt_ts)`` is unique."""
    tenant = uuid.uuid4()
    event = CanvasMockWebhookEvent(
        tenant_id=tenant,
        event="grade.posted",
        resource_id=42,
        attempt_ts=1_700_000_000,
        delivery_id=uuid.uuid4(),
        payload={"foo": "bar"},
        result="processed",
    )
    db_session.add(event)
    db_session.flush()

    # Same composite key must collide even with a different delivery_id.
    dup = CanvasMockWebhookEvent(
        tenant_id=tenant,
        event="grade.posted",
        resource_id=42,
        attempt_ts=1_700_000_000,
        delivery_id=uuid.uuid4(),  # fresh — idempotency keys on the composite
        payload={"foo": "different"},
        result="duplicate",
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_canvas_mock_webhook_subscriptions_unique_per_tenant(db_session: Any) -> None:
    """Each tenant can have at most one subscription per target URL."""
    tenant = uuid.uuid4()
    other = uuid.uuid4()
    sub = CanvasMockWebhookSubscription(
        tenant_id=tenant,
        target_url="https://example.com/hooks/canvas-mock",
        secret="whsec_test",
        event_types=["grade.posted"],
    )
    other_tenant_same_url = CanvasMockWebhookSubscription(
        tenant_id=other,
        target_url="https://example.com/hooks/canvas-mock",
        secret="whsec_other",
        event_types=["grade.posted"],
    )
    db_session.add_all([sub, other_tenant_same_url])
    db_session.flush()

    # Same (tenant_id, target_url) cannot duplicate.
    db_session.add(
        CanvasMockWebhookSubscription(
            tenant_id=tenant,
            target_url="https://example.com/hooks/canvas-mock",
            secret="whsec_dup",
            event_types=["assignment.created"],
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_canvas_mock_data_is_tenant_scoped(db_session: Any) -> None:
    """Two tenants with the same canvas_mock_id never collide or leak."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    db_session.add_all(
        [
            CanvasMockCourse(
                tenant_id=tenant_a,
                canvas_mock_id=101,
                name="Cálculo I",
                course_code="CALC-1",
            ),
            CanvasMockCourse(
                tenant_id=tenant_b,
                canvas_mock_id=101,
                name="Otro Cálculo",
                course_code="CALC-9",
            ),
        ]
    )
    db_session.flush()

    a_rows = (
        db_session.query(CanvasMockCourse)
        .filter(CanvasMockCourse.tenant_id == tenant_a)
        .all()
    )
    b_rows = (
        db_session.query(CanvasMockCourse)
        .filter(CanvasMockCourse.tenant_id == tenant_b)
        .all()
    )
    assert {r.name for r in a_rows} == {"Cálculo I"}
    assert {r.name for r in b_rows} == {"Otro Cálculo"}


# ---------------------------------------------------------------------------
# Column-shape contract (used by the Pydantic schemas in PR 1 task 1.4)
# ---------------------------------------------------------------------------


def test_models_expose_typed_mapped_columns() -> None:
    """Every model must have the documented attribute name on the class."""
    expectations: dict[type, tuple[str, ...]] = {
        CanvasMockUser: ("tenant_id", "canvas_mock_id"),
        CanvasMockCourse: ("tenant_id", "canvas_mock_id", "name"),
        CanvasMockEnrollment: ("tenant_id", "canvas_mock_id"),
        CanvasMockClassSession: ("tenant_id", "canvas_mock_id"),
        CanvasMockAssignment: ("tenant_id", "canvas_mock_id"),
        CanvasMockAttendanceRecord: ("tenant_id", "status"),
        CanvasMockGrade: ("tenant_id", "score"),
        CanvasMockWebhookEvent: ("tenant_id", "event", "attempt_ts"),
        CanvasMockWebhookSubscription: ("tenant_id", "target_url"),
    }
    for cls, attrs in expectations.items():
        for attr in attrs:
            assert hasattr(cls, attr), f"{cls.__name__}.{attr}"


def test_models_carry_the_unique_constraints_described_in_design() -> None:
    """Spot-check the composite UQ names match the design.md contract."""
    # Syntax-only; the actual enforcement is exercised above.
    for cls in (CanvasMockUser, CanvasMockCourse, CanvasMockGrade):
        names = _all_uq_names(cls)
        assert any("tenant" in n for n in names), (cls.__name__, names)
