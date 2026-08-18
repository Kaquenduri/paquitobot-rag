"""SQLAlchemy declarative base, tenant mixin, and domain models (PR 3).

Every domain row inherits :class:`TenantMixin` so the cross-tenant FK
comparator (see :mod:`app.repositories.upsert`) has a stable anchor and
every table carries ``tenant_id``, ``created_at``, ``updated_at``,
``deleted_at`` for free.

Canvas-natural keys are ``canvas_id BIGINT`` and are unique **per
tenant**, not globally. ``sync_state`` uses ``(tenant_id, table_name)``
as its natural key. ``canvas_credentials`` is already in the schema from
PR 2 (task 2.5); it now also gains ``created_at`` / ``rotated_at``
audit timestamps on top of the original ``ciphertext`` /
``key_version`` columns.

This module deliberately keeps the PG-specific column types
(``UUID``, ``JSONB``, ``BigInteger``) so the metadata is faithful to
the production schema. Tests run against SQLite in-memory via the
``sqlite_url`` helper that swaps ``UUID`` for ``String(36)`` and
``JSONB`` for ``JSON`` (see :func:`app.core.db.engine_for_url`); the
canonical column definitions stay PG-shaped so Alembic emits the right
DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    declared_attr,
    mapped_column,
)
from sqlalchemy.types import CHAR, TypeDecorator


class GUID(TypeDecorator):
    """Cross-dialect UUID column.

    Production (Postgres) stores native ``uuid``; tests (SQLite) get a
    ``CHAR(36)`` plus bind/result conversion so :class:`uuid.UUID`
    instances survive a round-trip without explicit casting.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[override]
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):  # type: ignore[override]
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        if isinstance(value, uuid.UUID):
            return str(value)
        return str(uuid.UUID(str(value)))

    def process_result_value(self, value, dialect):  # type: ignore[override]
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


# ---------------------------------------------------------------------------
# Base + mixin
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Declarative base for every SQLAlchemy model in the project.

    PR 2 declared this; PR 3 keeps the same anchor and adds the rest of
    the domain tables to its metadata.
    """


# JSON column type used by domain rows. Production is ``JSONB``; tests
# swap to plain ``JSON`` so SQLite can store them. Both share the same
# Python-side semantics (dict / list / scalar).
JSONType = JSONB().with_variant(JSON(), "sqlite")


class TenantMixin:
    """Mixin attached to every domain model in PR 3.

    Provides:

    - ``tenant_id``: server-derived UUID; never client-supplied.
    - ``created_at`` / ``updated_at``: server-stamped audit timestamps.
    - ``deleted_at``: soft-delete marker (``NULL`` = active).
    """

    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:
        # ``index=True`` keeps single-column lookups fast; the
        # ``UNIQUE(tenant_id, canvas_id)`` composite indexes handle the
        # natural-key path.
        return mapped_column(
            GUID(),
            nullable=False,
            index=True,
        )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )


# ---------------------------------------------------------------------------
# Tenant + Canvas credential
# ---------------------------------------------------------------------------


class Tenant(Base):
    """One row per backend user. Resolved by ``backend_user_id``.

    PR 2 declared this with just the PK + ``backend_user_id``. PR 3
    keeps the same shape and feeds it into Alembic.
    """

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        primary_key=True,
        default=uuid.uuid4,
    )
    backend_user_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - defensive only
        return f"Tenant(id={self.id}, backend_user_id={self.backend_user_id!r})"


class CanvasCredential(Base):
    """Encrypted Canvas token custody. One row per tenant.

    PR 2 introduced ``ciphertext`` + ``key_version``. PR 3 makes the
    audit timestamps (``created_at``, ``rotated_at``) explicit so the
    first Alembic migration can stamp them on the existing rows.
    """

    __tablename__ = "canvas_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        nullable=False,
        unique=True,
        index=True,
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    def __repr__(self) -> str:  # pragma: no cover - defensive only
        # Never leak the ciphertext into a log line; even ``__repr__`` masks it.
        return (
            f"CanvasCredential(tenant_id={self.tenant_id}, "
            f"key_version={self.key_version}, ciphertext=***REDACTED***)"
        )


# ---------------------------------------------------------------------------
# Domain rows
# ---------------------------------------------------------------------------


class User(TenantMixin, Base):
    """Authenticated Canvas user (the tenant's own user only).

    Peer-identifying data is never persisted; the model only stores
    self-profile fields returned by Canvas ``GET /users/self`` and
    peer enrollment records that scope to the authenticated user.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "canvas_id", name="uq_users_tenant_canvas"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        primary_key=True,
        default=uuid.uuid4,
    )
    canvas_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    short_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sortable_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(512), nullable=True)
    locale: Mapped[str | None] = mapped_column(String(32), nullable=True)
    effective_locale: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Course(TenantMixin, Base):
    """Course metadata. Unique per tenant on ``(tenant_id, canvas_id)``."""

    __tablename__ = "courses"
    __table_args__ = (
        UniqueConstraint("tenant_id", "canvas_id", name="uq_courses_tenant_canvas"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        primary_key=True,
        default=uuid.uuid4,
    )
    canvas_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    course_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    root_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    enrollment_term_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    default_view: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_public: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    license: Mapped[str | None] = mapped_column(String(64), nullable=True)
    time_zone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enrollments_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    calendar_ics: Mapped[str | None] = mapped_column(Text, nullable=True)
    term_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )


class Enrollment(TenantMixin, Base):
    """Tenant-scoped enrollment. Optional FK to a ``users`` row."""

    __tablename__ = "enrollments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "canvas_id", name="uq_enrollments_tenant_canvas"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        primary_key=True,
        default=uuid.uuid4,
    )
    canvas_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("courses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    enrollment_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    limit_privileges_to_course_section: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )


class Assignment(TenantMixin, Base):
    """Tenant-scoped assignment with class-level aggregate JSON."""

    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "canvas_id", name="uq_assignments_tenant_canvas"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        primary_key=True,
        default=uuid.uuid4,
    )
    canvas_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("courses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    points_possible: Mapped[float | None] = mapped_column(Float, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lock_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    unlock_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    workflow_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    grading_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    submission_types: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    html_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    important_dates: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    muted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    availability_status: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    lock_info: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    integration_data: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    score_statistics: Mapped[dict | None] = mapped_column(JSONType, nullable=True)


class Submission(TenantMixin, Base):
    """Submission row tied to one assignment and one user."""

    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "canvas_id", name="uq_submissions_tenant_canvas"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        primary_key=True,
        default=uuid.uuid4,
    )
    canvas_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("assignments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    workflow_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    submission_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    entered_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    grade: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entered_grade: Mapped[str | None] = mapped_column(String(64), nullable=True)
    grade_matches_current_submission: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    graded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cached_due_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    late: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    missing: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    excused: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    redo_request: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    attempt: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    seconds_late: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    late_policy_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    preview_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    points_deducted: Mapped[float | None] = mapped_column(Float, nullable=True)
    extra_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SyncState(Base):
    """Per-tenant sync watermark. Unique on ``(tenant_id, table_name)``.

    Lives outside :class:`TenantMixin` because it does not need
    ``deleted_at`` — a sync row is either present or absent.
    """

    __tablename__ = "sync_state"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "table_name", name="uq_sync_state_tenant_table"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        nullable=False,
        index=True,
    )
    table_name: Mapped[str] = mapped_column(String(64), nullable=False)
    last_watermark: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = [
    "Assignment",
    "Base",
    "CanvasCredential",
    "CanvasMockAssignment",
    "CanvasMockAttendanceRecord",
    "CanvasMockClassSession",
    "CanvasMockCourse",
    "CanvasMockEnrollment",
    "CanvasMockGrade",
    "CanvasMockUser",
    "CanvasMockWebhookEvent",
    "CanvasMockWebhookSubscription",
    "Course",
    "Enrollment",
    "JSONType",
    "Submission",
    "SyncState",
    "Tenant",
    "TenantMixin",
    "User",
]

# Re-export the canvas_mock_* models so callers can ``from app.models
# import CanvasMockCourse`` without an extra import path. The import is
# placed after __all__ so the names are part of the public surface even
# if the package is imported for ``dir()``-style introspection.
from app.models.canvas_mock import (  # noqa: E402,F401
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