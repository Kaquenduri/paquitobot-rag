"""Add ``term_name`` to courses so the agent can filter by academic period.

Revision ID: 0002_add_course_term_name
Revises: 0001_init
Create Date: 2026-08-14

Canvas exposes the term as a nested object via ``include[]=term`` on
``GET /users/self/favorites/courses``. Persisting only ``term.name`` is
enough to power the ``get_user_courses_current_term`` tool — the tool
matches ``term_name LIKE '%<year> - <period>'`` to cover every Canvas
period-prefix naming convention (``"PFR A 2026 - 2"``, ``"REG 2026 - 2"``,
...). The index keeps the LIKE scan cheap once a tenant accumulates
several years of historical terms.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_add_course_term_name"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "courses",
        sa.Column("term_name", sa.String(length=255), nullable=True),
    )
    op.create_index("ix_courses_term_name", "courses", ["term_name"])


def downgrade() -> None:
    op.drop_index("ix_courses_term_name", table_name="courses")
    op.drop_column("courses", "term_name")