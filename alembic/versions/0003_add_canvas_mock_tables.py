"""Add the nine canvas_mock_* tables (PR 1 task 1.5).

Revision ID: 0003_add_canvas_mock_tables
Revises: 0002_add_course_term_name
Create Date: 2026-08-17

This migration is **additive**: it creates ``canvas_mock_users``,
``canvas_mock_courses``, ``canvas_mock_enrollments``,
``canvas_mock_class_sessions``, ``canvas_mock_assignments``,
``canvas_mock_attendance_records``, ``canvas_mock_grades``,
``canvas_mock_webhook_events`` and ``canvas_mock_webhook_subscriptions``
plus the indexes and composite unique constraints required by the
extractor and the webhook receiver. It does NOT touch the legacy
``documents`` / ``users`` / ``courses`` / ``assignments`` /
``submissions`` / ``enrollments`` / ``sync_state`` /
``canvas_credentials`` / ``tenants`` tables.

The DDL is written by hand so the migration is explicit about every
constraint and CHECK expression. Auto-generation from
``Base.metadata`` would couple the migration to whatever metadata the
dev happens to have on ``PYTHONPATH``; hand-written DDL survives
tooling churn (e.g. SQLAlchemy upgrades) and is easier to review
against the locked spec.

Composite unique constraints direct from the spec:

- ``canvas_mock_users (tenant_id, canvas_mock_id)``
- ``canvas_mock_users (tenant_id, api_key_prefix)`` (TASK-1-8)
- ``canvas_mock_courses (tenant_id, canvas_mock_id)``
- ``canvas_mock_enrollments (tenant_id, canvas_mock_id)``
- ``canvas_mock_class_sessions (tenant_id, canvas_mock_id)``
- ``canvas_mock_assignments (tenant_id, canvas_mock_id)``
- ``canvas_mock_attendance_records (tenant_id, class_session_canvas_mock_id, user_canvas_mock_id)``
- ``canvas_mock_grades (tenant_id, assignment_canvas_mock_id, user_canvas_mock_id)``
- ``canvas_mock_webhook_events (tenant_id, event, resource_id, attempt_ts)``
- ``canvas_mock_webhook_subscriptions (tenant_id, target_url)``

CHECK constraint direct from the spec:

- ``canvas_mock_attendance_records.status IN ('present', 'absent')``

Downgrade drops the tables in reverse FK-dependency order; there are
no FKs between the new tables, so the order is the same one Alembic
records them in.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0003_add_canvas_mock_tables"
down_revision = "0002_add_course_term_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- canvas_mock_users ----------------------------------------------
    op.create_table(
        "canvas_mock_users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canvas_mock_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=True),
        sa.Column("short_name", sa.String(length=255), nullable=True),
        sa.Column("email", sa.String(length=512), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=True),
        sa.Column("api_key_prefix", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "canvas_mock_id",
            name="uq_canvas_mock_users_tenant_id_mock_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "api_key_prefix",
            name="uq_canvas_mock_users_tenant_api_key_prefix",
        ),
    )
    op.create_index(
        "ix_canvas_mock_users_tenant_id", "canvas_mock_users", ["tenant_id"]
    )
    op.create_index(
        "ix_canvas_mock_users_canvas_mock_id",
        "canvas_mock_users",
        ["canvas_mock_id"],
    )

    # --- canvas_mock_courses --------------------------------------------
    op.create_table(
        "canvas_mock_courses",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canvas_mock_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=True),
        sa.Column("course_code", sa.String(length=255), nullable=True),
        sa.Column("workflow_state", sa.String(length=32), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enrollments_count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "canvas_mock_id",
            name="uq_canvas_mock_courses_tenant_id_mock_id",
        ),
    )
    op.create_index(
        "ix_canvas_mock_courses_tenant_id", "canvas_mock_courses", ["tenant_id"]
    )
    op.create_index(
        "ix_canvas_mock_courses_canvas_mock_id",
        "canvas_mock_courses",
        ["canvas_mock_id"],
    )

    # --- canvas_mock_enrollments ----------------------------------------
    op.create_table(
        "canvas_mock_enrollments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canvas_mock_id", sa.BigInteger(), nullable=False),
        sa.Column("user_canvas_mock_id", sa.BigInteger(), nullable=False),
        sa.Column("course_canvas_mock_id", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=True),
        sa.Column("enrollment_state", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "canvas_mock_id",
            name="uq_canvas_mock_enrollments_tenant_id_mock_id",
        ),
    )
    op.create_index(
        "ix_canvas_mock_enrollments_tenant_id",
        "canvas_mock_enrollments",
        ["tenant_id"],
    )
    op.create_index(
        "ix_canvas_mock_enrollments_canvas_mock_id",
        "canvas_mock_enrollments",
        ["canvas_mock_id"],
    )
    op.create_index(
        "ix_canvas_mock_enrollments_course_canvas_mock_id",
        "canvas_mock_enrollments",
        ["course_canvas_mock_id"],
    )
    op.create_index(
        "ix_canvas_mock_enrollments_user_canvas_mock_id",
        "canvas_mock_enrollments",
        ["user_canvas_mock_id"],
    )

    # --- canvas_mock_class_sessions -------------------------------------
    op.create_table(
        "canvas_mock_class_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canvas_mock_id", sa.BigInteger(), nullable=False),
        sa.Column("course_canvas_mock_id", sa.BigInteger(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "canvas_mock_id",
            name="uq_canvas_mock_class_sessions_tenant_id_mock_id",
        ),
    )
    op.create_index(
        "ix_canvas_mock_class_sessions_tenant_id",
        "canvas_mock_class_sessions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_canvas_mock_class_sessions_canvas_mock_id",
        "canvas_mock_class_sessions",
        ["canvas_mock_id"],
    )
    op.create_index(
        "ix_canvas_mock_class_sessions_course_canvas_mock_id",
        "canvas_mock_class_sessions",
        ["course_canvas_mock_id"],
    )

    # --- canvas_mock_assignments ----------------------------------------
    op.create_table(
        "canvas_mock_assignments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canvas_mock_id", sa.BigInteger(), nullable=False),
        sa.Column("course_canvas_mock_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("points_possible", sa.Float(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grading_type", sa.String(length=32), nullable=True),
        sa.Column("submission_types", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("workflow_state", sa.String(length=32), nullable=True),
        sa.Column("html_url", sa.String(length=1024), nullable=True),
        sa.Column("url", sa.String(length=1024), nullable=True),
        sa.Column("created_at_mock", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at_mock", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "canvas_mock_id",
            name="uq_canvas_mock_assignments_tenant_id_mock_id",
        ),
    )
    op.create_index(
        "ix_canvas_mock_assignments_tenant_id",
        "canvas_mock_assignments",
        ["tenant_id"],
    )
    op.create_index(
        "ix_canvas_mock_assignments_canvas_mock_id",
        "canvas_mock_assignments",
        ["canvas_mock_id"],
    )
    op.create_index(
        "ix_canvas_mock_assignments_course_canvas_mock_id",
        "canvas_mock_assignments",
        ["course_canvas_mock_id"],
    )

    # --- canvas_mock_attendance_records ---------------------------------
    op.create_table(
        "canvas_mock_attendance_records",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("class_session_canvas_mock_id", sa.BigInteger(), nullable=False),
        sa.Column("user_canvas_mock_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "class_session_canvas_mock_id",
            "user_canvas_mock_id",
            name="uq_canvas_mock_attendance_records_session_user",
        ),
        sa.CheckConstraint(
            "status IN ('present', 'absent')",
            name="ck_canvas_mock_attendance_records_status",
        ),
    )
    op.create_index(
        "ix_canvas_mock_attendance_records_tenant_id",
        "canvas_mock_attendance_records",
        ["tenant_id"],
    )
    op.create_index(
        "ix_canvas_mock_attendance_records_class_session_canvas_mock_id",
        "canvas_mock_attendance_records",
        ["class_session_canvas_mock_id"],
    )
    op.create_index(
        "ix_canvas_mock_attendance_records_user_canvas_mock_id",
        "canvas_mock_attendance_records",
        ["user_canvas_mock_id"],
    )

    # --- canvas_mock_grades ---------------------------------------------
    op.create_table(
        "canvas_mock_grades",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignment_canvas_mock_id", sa.BigInteger(), nullable=False),
        sa.Column("user_canvas_mock_id", sa.BigInteger(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("grade", sa.String(length=64), nullable=True),
        sa.Column("graded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grader_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "assignment_canvas_mock_id",
            "user_canvas_mock_id",
            name="uq_canvas_mock_grades_tenant_assignment_user",
        ),
    )
    op.create_index(
        "ix_canvas_mock_grades_tenant_id", "canvas_mock_grades", ["tenant_id"]
    )
    op.create_index(
        "ix_canvas_mock_grades_assignment_canvas_mock_id",
        "canvas_mock_grades",
        ["assignment_canvas_mock_id"],
    )
    op.create_index(
        "ix_canvas_mock_grades_user_canvas_mock_id",
        "canvas_mock_grades",
        ["user_canvas_mock_id"],
    )

    # --- canvas_mock_webhook_events -------------------------------------
    op.create_table(
        "canvas_mock_webhook_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.BigInteger(), nullable=False),
        sa.Column("attempt_ts", sa.BigInteger(), nullable=False),
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("processed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "event",
            "resource_id",
            "attempt_ts",
            name="uq_canvas_mock_webhook_events_tenant_event_resource_attempt",
        ),
    )
    op.create_index(
        "ix_canvas_mock_webhook_events_tenant_id",
        "canvas_mock_webhook_events",
        ["tenant_id"],
    )
    op.create_index(
        "ix_canvas_mock_webhook_events_event",
        "canvas_mock_webhook_events",
        ["event"],
    )

    # --- canvas_mock_webhook_subscriptions ------------------------------
    op.create_table(
        "canvas_mock_webhook_subscriptions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_url", sa.String(length=1024), nullable=False),
        sa.Column("secret", sa.String(length=255), nullable=False),
        sa.Column(
            "event_types", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "target_url",
            name="uq_canvas_mock_webhook_subscriptions_tenant_target",
        ),
    )
    op.create_index(
        "ix_canvas_mock_webhook_subscriptions_tenant_id",
        "canvas_mock_webhook_subscriptions",
        ["tenant_id"],
    )

    # --- Seed row (PR 1 task 1.8) -----------------------------------------
    # One deterministic CanvasMock user row is inserted so production
    # deployments have a reference entry for the webhook receiver's
    # ``api_key_prefix`` correlation. The seed is idempotent: a second
    # run on a populated DB no-ops because the natural key already
    # exists. A real deployment overrides this row with its own
    # tenant-scoped entry via the same SQLAlchemy session the runtime
    # uses.
    op.execute(
        sa.text(
            "INSERT INTO canvas_mock_users "
            "(id, tenant_id, canvas_mock_id, name, role, api_key_prefix, "
            " created_at, updated_at) "
            "VALUES ("
            " :id, :tenant_id, :canvas_mock_id, :name, :role, "
            " :api_key_prefix, now(), now())"
        ).bindparams(
            id=uuid.uuid4(),
            tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            canvas_mock_id=1,
            name="Paquito Seed",
            role="student",
            api_key_prefix="seed0001",
        )
    )


def downgrade() -> None:
    # Drop order: simply the reverse of the create order. No inter-table
    # FKs among the new tables, so no dependency resolution is needed.
    op.drop_index(
        "ix_canvas_mock_webhook_subscriptions_tenant_id",
        table_name="canvas_mock_webhook_subscriptions",
    )
    op.drop_table("canvas_mock_webhook_subscriptions")

    op.drop_index(
        "ix_canvas_mock_webhook_events_event",
        table_name="canvas_mock_webhook_events",
    )
    op.drop_index(
        "ix_canvas_mock_webhook_events_tenant_id",
        table_name="canvas_mock_webhook_events",
    )
    op.drop_table("canvas_mock_webhook_events")

    op.drop_index(
        "ix_canvas_mock_grades_user_canvas_mock_id",
        table_name="canvas_mock_grades",
    )
    op.drop_index(
        "ix_canvas_mock_grades_assignment_canvas_mock_id",
        table_name="canvas_mock_grades",
    )
    op.drop_index("ix_canvas_mock_grades_tenant_id", table_name="canvas_mock_grades")
    op.drop_table("canvas_mock_grades")

    op.drop_index(
        "ix_canvas_mock_attendance_records_user_canvas_mock_id",
        table_name="canvas_mock_attendance_records",
    )
    op.drop_index(
        "ix_canvas_mock_attendance_records_class_session_canvas_mock_id",
        table_name="canvas_mock_attendance_records",
    )
    op.drop_index(
        "ix_canvas_mock_attendance_records_tenant_id",
        table_name="canvas_mock_attendance_records",
    )
    op.drop_table("canvas_mock_attendance_records")

    op.drop_index(
        "ix_canvas_mock_assignments_course_canvas_mock_id",
        table_name="canvas_mock_assignments",
    )
    op.drop_index(
        "ix_canvas_mock_assignments_canvas_mock_id",
        table_name="canvas_mock_assignments",
    )
    op.drop_index(
        "ix_canvas_mock_assignments_tenant_id",
        table_name="canvas_mock_assignments",
    )
    op.drop_table("canvas_mock_assignments")

    op.drop_index(
        "ix_canvas_mock_class_sessions_course_canvas_mock_id",
        table_name="canvas_mock_class_sessions",
    )
    op.drop_index(
        "ix_canvas_mock_class_sessions_canvas_mock_id",
        table_name="canvas_mock_class_sessions",
    )
    op.drop_index(
        "ix_canvas_mock_class_sessions_tenant_id",
        table_name="canvas_mock_class_sessions",
    )
    op.drop_table("canvas_mock_class_sessions")

    op.drop_index(
        "ix_canvas_mock_enrollments_user_canvas_mock_id",
        table_name="canvas_mock_enrollments",
    )
    op.drop_index(
        "ix_canvas_mock_enrollments_course_canvas_mock_id",
        table_name="canvas_mock_enrollments",
    )
    op.drop_index(
        "ix_canvas_mock_enrollments_canvas_mock_id",
        table_name="canvas_mock_enrollments",
    )
    op.drop_index(
        "ix_canvas_mock_enrollments_tenant_id",
        table_name="canvas_mock_enrollments",
    )
    op.drop_table("canvas_mock_enrollments")

    op.drop_index(
        "ix_canvas_mock_courses_canvas_mock_id",
        table_name="canvas_mock_courses",
    )
    op.drop_index(
        "ix_canvas_mock_courses_tenant_id", table_name="canvas_mock_courses"
    )
    op.drop_table("canvas_mock_courses")

    op.drop_index(
        "ix_canvas_mock_users_canvas_mock_id",
        table_name="canvas_mock_users",
    )
    op.drop_index(
        "ix_canvas_mock_users_tenant_id", table_name="canvas_mock_users"
    )
    op.drop_table("canvas_mock_users")
