"""Canvas Mock ORM models (PR 1 task 1.2).

This module introduces a parallel persistence layer that mirrors the
``canvas-mock-api`` project (the LEGO-style fixture used by the Paquito
demo). Every class here is additive: it does NOT touch the legacy
``users`` / ``courses`` / ``assignments`` / ``submissions`` tables, and
no foreign key to a legacy table is declared.

Conventions (locked in design.md):

- All tables are prefixed ``canvas_mock_*`` so the new and legacy
  schemas coexist in the same database.
- All tenant-scoped rows inherit :class:`TenantMixin` and carry
  ``canvas_mock_id`` (``BIGINT``) as their natural key — same pattern as
  the legacy ``Canvas`` models but with a different column name (the
  mock IDs come from an independent INT counter, not the real Canvas).
- Cross-table references are stored as ``*_canvas_mock_id``
  (``BIGINT``), never as GUIDs. Mocks do not own row UUIDs, so trying
  to FK to a GUID would tie the new layer to the legacy schema (which
  is explicitly forbidden).
- ``canvas_mock_attendance_records`` has a CHECK constraint on
  ``status``: the API only emits ``present`` or ``absent``.
- ``canvas_mock_webhook_events`` carries the composite unique key
  ``(tenant_id, event, resource_id, attempt_ts)`` that the webhook
  receiver uses to dedupe retries.
- ``canvas_mock_webhook_subscriptions`` is unique per
  ``(tenant_id, target_url)`` so the same tenant cannot double-register.
- ``canvas_mock_users`` is the only model that carries an out-of-band
  secret field (``api_key_prefix``); the FULL key never lands in the
  database (the mock only echoes the prefix back).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, JSONType, TenantMixin

# ---------------------------------------------------------------------------
# Canvas Mock user (tenant-scoped self-profile)
# ---------------------------------------------------------------------------


class CanvasMockUser(TenantMixin, Base):
    """Self-profile row for the authenticated mock user.

    The mock's ``canvas_mock_id`` is the ``id`` field on the user object
    (``GET /users/self``); it is the same INT counter used by every
    other canvas_mock resource. ``name`` / ``email`` / ``role`` mirror
    the mock payload verbatim so the extractor can upsert in one shot.

    ``api_key_prefix`` is the first 8 characters of the mock API key
    (the mock only echoes the prefix back to the client). It is unique
    per tenant so a request that crosses the API boundary can be
    correlated without the full secret ever leaving the mock.
    """

    __tablename__ = "canvas_mock_users"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "canvas_mock_id", name="uq_canvas_mock_users_tenant_id_mock_id"
        ),
        UniqueConstraint(
            "tenant_id", "api_key_prefix", name="uq_canvas_mock_users_tenant_api_key_prefix"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    canvas_mock_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    short_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(512), nullable=True)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    api_key_prefix: Mapped[str | None] = mapped_column(String(16), nullable=True)


# ---------------------------------------------------------------------------
# Course + enrollment
# ---------------------------------------------------------------------------


class CanvasMockCourse(TenantMixin, Base):
    """Tenant-scoped mock course.

    The optional ``enrollments_count`` and ``start_at`` fields are
    nullable because the mock only returns them when the caller passes
    the corresponding ``include[]`` parameter; baseline strings
    (``name`` / ``course_code``) are also nullable to mirror the
    permissive contract.
    """

    __tablename__ = "canvas_mock_courses"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "canvas_mock_id", name="uq_canvas_mock_courses_tenant_id_mock_id"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    canvas_mock_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    course_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workflow_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enrollments_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CanvasMockEnrollment(TenantMixin, Base):
    """Tenant-scoped mock enrollment.

    ``user_canvas_mock_id`` and ``course_canvas_mock_id`` are INT
    references into the mock. There is intentionally no FK to the
    legacy ``users`` / ``courses`` tables — the two schemas are
    independent.
    """

    __tablename__ = "canvas_mock_enrollments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "canvas_mock_id",
            name="uq_canvas_mock_enrollments_tenant_id_mock_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    canvas_mock_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_canvas_mock_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    course_canvas_mock_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )
    type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enrollment_state: Mapped[str | None] = mapped_column(String(32), nullable=True)


# ---------------------------------------------------------------------------
# Class sessions + attendance
# ---------------------------------------------------------------------------


class CanvasMockClassSession(TenantMixin, Base):
    """Tenant-scoped mock class session.

    Sessions are chronological per course (the mock emits them sorted
    by ``start_at``). The two TIMESTAMPTZ columns are nullable because
    the mock may surface a session with only ``start_at`` populated.
    """

    __tablename__ = "canvas_mock_class_sessions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "canvas_mock_id",
            name="uq_canvas_mock_class_sessions_tenant_id_mock_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    canvas_mock_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    course_canvas_mock_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CanvasMockAttendanceRecord(TenantMixin, Base):
    """Tenant-scoped mock attendance record.

    The mock emits binary attendance: ``present`` or ``absent``. The
    CHECK constraint enforces that enum at the database level so a
    malformed payload from the extractor cannot land a row.
    """

    __tablename__ = "canvas_mock_attendance_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "class_session_canvas_mock_id",
            "user_canvas_mock_id",
            name="uq_canvas_mock_attendance_records_session_user",
        ),
        CheckConstraint(
            "status IN ('present', 'absent')",
            name="ck_canvas_mock_attendance_records_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    class_session_canvas_mock_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )
    user_canvas_mock_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)


# ---------------------------------------------------------------------------
# Assignments + grades
# ---------------------------------------------------------------------------


class CanvasMockAssignment(TenantMixin, Base):
    """Tenant-scoped mock assignment.

    Mirrors the assignment payload exactly: ``description`` is the only
    free-text field (``TEXT``) and the rest are nullable to absorb the
    partial responses the mock allows on ``include[]`` filters.
    """

    __tablename__ = "canvas_mock_assignments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "canvas_mock_id",
            name="uq_canvas_mock_assignments_tenant_id_mock_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    canvas_mock_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    course_canvas_mock_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    points_possible: Mapped[float | None] = mapped_column(Float, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grading_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    submission_types: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    workflow_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    html_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at_mock: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at_mock: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CanvasMockGrade(TenantMixin, Base):
    """Tenant-scoped mock grade.

    The natural key is the pair ``(assignment_canvas_mock_id,
    user_canvas_mock_id)`` — the mock's upsert contract —
    extended to ``tenant_id`` so two tenants can have the same
    assignment id and the same user id without colliding.
    """

    __tablename__ = "canvas_mock_grades"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "assignment_canvas_mock_id",
            "user_canvas_mock_id",
            name="uq_canvas_mock_grades_tenant_assignment_user",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    assignment_canvas_mock_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )
    user_canvas_mock_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    grade: Mapped[str | None] = mapped_column(String(64), nullable=True)
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grader_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


# ---------------------------------------------------------------------------
# Webhook infrastructure
# ---------------------------------------------------------------------------


class CanvasMockWebhookEvent(TenantMixin, Base):
    """One row per inbound webhook delivery.

    The composite unique key ``(tenant_id, event, resource_id,
    attempt_ts)`` is the **idempotency lock** for the webhook receiver
    (PR 5). The LLM-extracted ``delivery_id`` is stored separately for
    forensics (the mock rotates it per attempt, so it cannot be the
    idempotency key).

    ``payload`` is the JSON body; the receiver never logs the raw
    bytes (``app.controllers.canvas_mock_webhooks`` is responsible for
    that scrubbing).

    ``result`` is one of: ``processed``, ``duplicate``, ``handler_error``,
    ``signature_failed``. The DB stores it as a 32-char string so the
    application-side handler can extend the set without a migration.
    """

    __tablename__ = "canvas_mock_webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "event",
            "resource_id",
            "attempt_ts",
            name="uq_canvas_mock_webhook_events_tenant_event_resource_attempt",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt_ts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    delivery_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CanvasMockWebhookSubscription(TenantMixin, Base):
    """Outbound webhook subscription registered by the mock.

    The tenant-scoped uniqueness on ``target_url`` is the only
    invariant; ``event_types`` is a JSONB array of the event strings
    the mock should fire to this URL. ``secret`` is the
    webhook-signing secret, stored in plaintext because the mock does
    not encrypt at-rest values; production would KMS-encrypt this.
    """

    __tablename__ = "canvas_mock_webhook_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "target_url",
            name="uq_canvas_mock_webhook_subscriptions_tenant_target",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    target_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    secret: Mapped[str] = mapped_column(String(255), nullable=False)
    event_types: Mapped[list] = mapped_column(JSONType, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


__all__ = [
    "CanvasMockAssignment",
    "CanvasMockAttendanceRecord",
    "CanvasMockClassSession",
    "CanvasMockCourse",
    "CanvasMockEnrollment",
    "CanvasMockGrade",
    "CanvasMockUser",
    "CanvasMockWebhookEvent",
    "CanvasMockWebhookSubscription",
]
