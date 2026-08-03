"""First Alembic migration: add the seven application tables.

Revision ID: 0001_init
Revises:
Create Date: 2026-08-02

This migration is **additive**: it creates ``tenants``,
``canvas_credentials``, ``users``, ``courses``, ``enrollments``,
``assignments``, ``submissions`` and ``sync_state`` plus the indexes
required by the repository helpers and the sync pipeline. It does
NOT touch the existing PGVector ``documents`` table.

The DDL is written by hand so the migration is explicit about every
constraint and index; auto-generation from ``Base.metadata`` would
also emit the right SQL but would couple the migration to whatever
metadata the dev happens to have on ``PYTHONPATH``. Hand-written
DDL survives tooling churn (e.g. SQLAlchemy upgrades) and is easier
to review against the spec.

Downgrade drops the tables in reverse order; the FK references are
preserved by the order so the drop succeeds on a clean database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- tenants ---------------------------------------------------------
    op.create_table(
        "tenants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("backend_user_id", sa.String(length=255), nullable=False),
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
        sa.UniqueConstraint("backend_user_id", name="uq_tenants_backend_user_id"),
    )
    op.create_index("ix_tenants_backend_user_id", "tenants", ["backend_user_id"])

    # --- canvas_credentials ----------------------------------------------
    op.create_table(
        "canvas_credentials",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", name="uq_canvas_credentials_tenant_id"),
    )
    op.create_index(
        "ix_canvas_credentials_tenant_id", "canvas_credentials", ["tenant_id"]
    )

    # --- users -----------------------------------------------------------
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canvas_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=True),
        sa.Column("short_name", sa.String(length=255), nullable=True),
        sa.Column("sortable_name", sa.String(length=255), nullable=True),
        sa.Column("email", sa.String(length=512), nullable=True),
        sa.Column("locale", sa.String(length=32), nullable=True),
        sa.Column("effective_locale", sa.String(length=32), nullable=True),
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
        sa.UniqueConstraint("tenant_id", "canvas_id", name="uq_users_tenant_canvas"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
    op.create_index("ix_users_canvas_id", "users", ["canvas_id"])

    # --- courses ---------------------------------------------------------
    op.create_table(
        "courses",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canvas_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=True),
        sa.Column("course_code", sa.String(length=255), nullable=True),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("root_account_id", sa.BigInteger(), nullable=True),
        sa.Column("enrollment_term_id", sa.BigInteger(), nullable=True),
        sa.Column("uuid", sa.String(length=64), nullable=True),
        sa.Column("workflow_state", sa.String(length=32), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("default_view", sa.String(length=32), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=True),
        sa.Column("license", sa.String(length=64), nullable=True),
        sa.Column("time_zone", sa.String(length=64), nullable=True),
        sa.Column("enrollments_count", sa.Integer(), nullable=True),
        sa.Column("calendar_ics", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("tenant_id", "canvas_id", name="uq_courses_tenant_canvas"),
    )
    op.create_index("ix_courses_tenant_id", "courses", ["tenant_id"])
    op.create_index("ix_courses_canvas_id", "courses", ["canvas_id"])

    # --- enrollments -----------------------------------------------------
    op.create_table(
        "enrollments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canvas_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "course_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("courses.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("role", sa.String(length=64), nullable=True),
        sa.Column("role_id", sa.BigInteger(), nullable=True),
        sa.Column("enrollment_state", sa.String(length=32), nullable=True),
        sa.Column("limit_privileges_to_course_section", sa.Boolean(), nullable=True),
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
            "tenant_id", "canvas_id", name="uq_enrollments_tenant_canvas"
        ),
    )
    op.create_index("ix_enrollments_tenant_id", "enrollments", ["tenant_id"])
    op.create_index("ix_enrollments_canvas_id", "enrollments", ["canvas_id"])
    op.create_index("ix_enrollments_course_id", "enrollments", ["course_id"])
    op.create_index("ix_enrollments_user_id", "enrollments", ["user_id"])

    # --- assignments -----------------------------------------------------
    op.create_table(
        "assignments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canvas_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "course_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("courses.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=512), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("points_possible", sa.Float(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unlock_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("workflow_state", sa.String(length=32), nullable=True),
        sa.Column("grading_type", sa.String(length=32), nullable=True),
        sa.Column("submission_types", postgresql.JSONB(), nullable=True),
        sa.Column("html_url", sa.String(length=1024), nullable=True),
        sa.Column("important_dates", postgresql.JSONB(), nullable=True),
        sa.Column("muted", sa.Boolean(), nullable=True),
        sa.Column("availability_status", postgresql.JSONB(), nullable=True),
        sa.Column("lock_info", postgresql.JSONB(), nullable=True),
        sa.Column("integration_data", postgresql.JSONB(), nullable=True),
        sa.Column("score_statistics", postgresql.JSONB(), nullable=True),
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
            "tenant_id", "canvas_id", name="uq_assignments_tenant_canvas"
        ),
    )
    op.create_index("ix_assignments_tenant_id", "assignments", ["tenant_id"])
    op.create_index("ix_assignments_canvas_id", "assignments", ["canvas_id"])
    op.create_index("ix_assignments_course_id", "assignments", ["course_id"])

    # --- submissions -----------------------------------------------------
    op.create_table(
        "submissions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canvas_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "assignment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("assignments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("workflow_state", sa.String(length=32), nullable=True),
        sa.Column("submission_type", sa.String(length=64), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("entered_score", sa.Float(), nullable=True),
        sa.Column("grade", sa.String(length=64), nullable=True),
        sa.Column("entered_grade", sa.String(length=64), nullable=True),
        sa.Column("grade_matches_current_submission", sa.Boolean(), nullable=True),
        sa.Column("graded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cached_due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("late", sa.Boolean(), nullable=True),
        sa.Column("missing", sa.Boolean(), nullable=True),
        sa.Column("excused", sa.Boolean(), nullable=True),
        sa.Column("redo_request", sa.Boolean(), nullable=True),
        sa.Column("attempt", sa.BigInteger(), nullable=True),
        sa.Column("seconds_late", sa.BigInteger(), nullable=True),
        sa.Column("late_policy_status", sa.String(length=64), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("url", sa.String(length=1024), nullable=True),
        sa.Column("preview_url", sa.String(length=1024), nullable=True),
        sa.Column("points_deducted", sa.Float(), nullable=True),
        sa.Column("extra_attempts", sa.Integer(), nullable=True),
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
            "tenant_id", "canvas_id", name="uq_submissions_tenant_canvas"
        ),
    )
    op.create_index("ix_submissions_tenant_id", "submissions", ["tenant_id"])
    op.create_index("ix_submissions_canvas_id", "submissions", ["canvas_id"])
    op.create_index("ix_submissions_assignment_id", "submissions", ["assignment_id"])
    op.create_index("ix_submissions_user_id", "submissions", ["user_id"])

    # --- sync_state ------------------------------------------------------
    op.create_table(
        "sync_state",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("table_name", sa.String(length=64), nullable=False),
        sa.Column("last_watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=32), nullable=True),
        sa.Column("last_error_class", sa.String(length=64), nullable=True),
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
        sa.UniqueConstraint(
            "tenant_id", "table_name", name="uq_sync_state_tenant_table"
        ),
    )
    op.create_index("ix_sync_state_tenant_id", "sync_state", ["tenant_id"])


def downgrade() -> None:
    # Drop order respects FK dependencies.
    op.drop_index("ix_sync_state_tenant_id", table_name="sync_state")
    op.drop_table("sync_state")

    op.drop_index("ix_submissions_user_id", table_name="submissions")
    op.drop_index("ix_submissions_assignment_id", table_name="submissions")
    op.drop_index("ix_submissions_canvas_id", table_name="submissions")
    op.drop_index("ix_submissions_tenant_id", table_name="submissions")
    op.drop_table("submissions")

    op.drop_index("ix_assignments_course_id", table_name="assignments")
    op.drop_index("ix_assignments_canvas_id", table_name="assignments")
    op.drop_index("ix_assignments_tenant_id", table_name="assignments")
    op.drop_table("assignments")

    op.drop_index("ix_enrollments_user_id", table_name="enrollments")
    op.drop_index("ix_enrollments_course_id", table_name="enrollments")
    op.drop_index("ix_enrollments_canvas_id", table_name="enrollments")
    op.drop_index("ix_enrollments_tenant_id", table_name="enrollments")
    op.drop_table("enrollments")

    op.drop_index("ix_courses_canvas_id", table_name="courses")
    op.drop_index("ix_courses_tenant_id", table_name="courses")
    op.drop_table("courses")

    op.drop_index("ix_users_canvas_id", table_name="users")
    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_table("users")

    op.drop_index("ix_canvas_credentials_tenant_id", table_name="canvas_credentials")
    op.drop_table("canvas_credentials")

    op.drop_index("ix_tenants_backend_user_id", table_name="tenants")
    op.drop_table("tenants")