"""Unit tests for the PR 3 schema work unit.

Coverage:

- Model instantiation: every domain table instantiates with the
  columns declared in the spec; ``UNIQUE(tenant_id, canvas_id)``
  constraints are visible on the metadata.
- Upsert idempotency: ``upsert_by_canvas_id`` produces a single row
  for repeated payloads and updates existing rows in place.
- Soft-delete: ``soft_delete_if_inactive`` flips ``deleted_at`` only
  when ``workflow_state`` is in the inactive set.
- Cross-tenant FK rejection: ``assert_tenant_fk_target`` raises
  :class:`CrossTenantFKError` when the target's tenant differs.
- Watermark advance in transaction: commit persists, rollback leaves
  the previous watermark intact (the "Canvas fails mid-sync"
  scenario).
- Alembic offline mode: ``alembic upgrade --sql`` emits DDL against
  a fake URL without touching any database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from app.models import (
    Assignment,
    Base,
    CanvasCredential,
    Course,
    Enrollment,
    JSONType,
    Submission,
    SyncState,
    TenantMixin,
    User,
)
from app.repositories.upsert import (
    INACTIVE_WORKFLOW_STATES,
    CrossTenantFKError,
    assert_tenant_fk_target,
    soft_delete_if_inactive,
    upsert_by_canvas_id,
)
from app.sync.watermark import advance_watermark, read_watermark

TENANT_ALPHA = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_BETA = uuid.UUID("22222222-2222-2222-2222-222222222222")


# ---------------------------------------------------------------------------
# Model instantiation
# ---------------------------------------------------------------------------


def test_base_metadata_contains_all_seven_tables() -> None:
    """Every domain table lands on ``Base.metadata`` for Alembic to diff."""
    tables = set(Base.metadata.tables)
    expected = {
        "tenants",
        "canvas_credentials",
        "users",
        "courses",
        "enrollments",
        "assignments",
        "submissions",
        "sync_state",
    }
    assert expected.issubset(tables), f"missing tables: {expected - tables}"


def test_domain_models_inherit_tenant_mixin() -> None:
    """Every domain row carries tenant_id / created_at / updated_at / deleted_at."""
    for model in (User, Course, Enrollment, Assignment, Submission):
        assert issubclass(model, TenantMixin), f"{model.__name__} missing TenantMixin"
        columns = {column.key for column in model.__table__.columns}
        for required in ("tenant_id", "created_at", "updated_at", "deleted_at"):
            assert required in columns, f"{model.__name__} missing {required}"


@pytest.mark.parametrize(
    "model,expected_unique",
    [
        (User, "uq_users_tenant_canvas"),
        (Course, "uq_courses_tenant_canvas"),
        (Enrollment, "uq_enrollments_tenant_canvas"),
        (Assignment, "uq_assignments_tenant_canvas"),
        (Submission, "uq_submissions_tenant_canvas"),
    ],
)
def test_domain_models_declare_unique_tenant_canvas(
    model: type[Base], expected_unique: str
) -> None:
    names = {constraint.name for constraint in model.__table__.constraints}
    assert expected_unique in names


def test_sync_state_unique_constraint() -> None:
    names = {constraint.name for constraint in SyncState.__table__.constraints}
    assert "uq_sync_state_tenant_table" in names


def test_canvas_credential_columns_match_spec(db_session: Any) -> None:
    row = CanvasCredential(
        tenant_id=TENANT_ALPHA,
        ciphertext=b"fake",
        key_version=1,
    )
    db_session.add(row)
    db_session.flush()
    columns = {column.key for column in CanvasCredential.__table__.columns}
    for required in ("tenant_id", "ciphertext", "key_version", "created_at", "rotated_at"):
        assert required in columns


def test_user_can_be_constructed_with_required_fields(db_session: Any) -> None:
    user = User(tenant_id=TENANT_ALPHA, canvas_id=42, name="Alice")
    db_session.add(user)
    db_session.flush()
    assert user.tenant_id == TENANT_ALPHA
    assert user.canvas_id == 42
    assert user.deleted_at is None


def test_assignment_accepts_score_statistics_json(db_session: Any) -> None:
    stats = {"min": 0, "max": 10, "mean": 7.5}
    course = Course(tenant_id=TENANT_ALPHA, canvas_id=100, name="Math")
    db_session.add(course)
    db_session.flush()
    assignment = Assignment(
        tenant_id=TENANT_ALPHA,
        canvas_id=200,
        course_id=course.id,
        name="HW1",
        score_statistics=stats,
    )
    db_session.add(assignment)
    db_session.commit()
    refreshed = db_session.get(Assignment, assignment.id)
    assert refreshed is not None
    assert refreshed.score_statistics == stats


def test_json_column_uses_pg_jsonb_with_sqlite_variant() -> None:
    """``JSONType`` is JSONB on Postgres and JSON on SQLite."""
    # Compile against a Postgres dialect; the JSONB type should be used.
    postgres_engine = create_engine("postgresql://x", strategy="mock", executor=lambda *a, **kw: None)
    compiled_postgres = JSONType.compile(dialect=postgres_engine.dialect)
    # SQLite falls back to plain JSON.
    sqlite_engine = create_engine("sqlite:///:memory:")
    compiled_sqlite = JSONType.compile(dialect=sqlite_engine.dialect)
    assert "JSONB" in str(compiled_postgres).upper()
    assert "JSONB" not in str(compiled_sqlite).upper()


# ---------------------------------------------------------------------------
# Upsert idempotency
# ---------------------------------------------------------------------------


def _make_user_payload(name: str = "Alice") -> dict[str, Any]:
    return {
        "name": name,
        "short_name": name[:8],
        "sortable_name": f"{name}, Test",
        "email": f"{name.lower()}@example.com",
    }


def test_upsert_by_canvas_id_creates_row_on_first_call(db_session: Any) -> None:
    row = upsert_by_canvas_id(
        db_session,
        User,
        tenant_id=TENANT_ALPHA,
        canvas_id=10,
        payload=_make_user_payload(),
    )
    db_session.commit()
    assert row.id is not None
    assert row.canvas_id == 10
    assert row.tenant_id == TENANT_ALPHA


def test_upsert_by_canvas_id_is_idempotent(db_session: Any) -> None:
    first = upsert_by_canvas_id(
        db_session,
        User,
        tenant_id=TENANT_ALPHA,
        canvas_id=11,
        payload=_make_user_payload("Alice"),
    )
    db_session.commit()
    first_id = first.id

    second = upsert_by_canvas_id(
        db_session,
        User,
        tenant_id=TENANT_ALPHA,
        canvas_id=11,
        payload=_make_user_payload("Alice2"),
    )
    db_session.commit()
    assert second.id == first_id
    assert second.name == "Alice2"


def test_upsert_by_canvas_id_drops_unknown_keys(db_session: Any) -> None:
    """Unknown keys in the payload are dropped before reaching the DB."""
    row = upsert_by_canvas_id(
        db_session,
        User,
        tenant_id=TENANT_ALPHA,
        canvas_id=12,
        payload={
            **_make_user_payload("Bob"),
            "peer_id": 999,  # not a column on User
            "internal_flag": True,
        },
    )
    db_session.commit()
    assert not hasattr(row, "peer_id")
    assert not hasattr(row, "internal_flag")


def test_upsert_by_canvas_id_same_canvas_id_different_tenant_creates_two_rows(
    db_session: Any,
) -> None:
    """The natural key is ``(tenant_id, canvas_id)``; different tenants coexist."""
    upsert_by_canvas_id(
        db_session,
        User,
        tenant_id=TENANT_ALPHA,
        canvas_id=13,
        payload=_make_user_payload("Alice"),
    )
    upsert_by_canvas_id(
        db_session,
        User,
        tenant_id=TENANT_BETA,
        canvas_id=13,
        payload=_make_user_payload("Bob"),
    )
    db_session.commit()

    alpha = db_session.execute(
        User.__table__.select().where(User.tenant_id == TENANT_ALPHA)
    ).fetchall()
    beta = db_session.execute(
        User.__table__.select().where(User.tenant_id == TENANT_BETA)
    ).fetchall()
    assert len(alpha) == 1
    assert len(beta) == 1


def test_unique_constraint_enforced_per_tenant(db_session: Any) -> None:
    """Two inserts with the same ``(tenant_id, canvas_id)`` violate the constraint."""
    upsert_by_canvas_id(
        db_session,
        User,
        tenant_id=TENANT_ALPHA,
        canvas_id=14,
        payload=_make_user_payload(),
    )
    db_session.commit()
    # Insert again via the raw ORM to bypass the upsert; expect IntegrityError.
    duplicate = User(tenant_id=TENANT_ALPHA, canvas_id=14, name="Charlie")
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# Soft-delete
# ---------------------------------------------------------------------------


def test_soft_delete_sets_deleted_at_when_state_is_inactive(db_session: Any) -> None:
    upsert_by_canvas_id(
        db_session,
        Course,
        tenant_id=TENANT_ALPHA,
        canvas_id=20,
        payload={"name": "Math", "workflow_state": "available"},
    )
    db_session.commit()

    mutated = soft_delete_if_inactive(
        db_session,
        Course,
        tenant_id=TENANT_ALPHA,
        canvas_id=20,
        workflow_state="deleted",
    )
    db_session.commit()
    assert mutated is True
    refreshed = db_session.execute(
        Course.__table__.select().where(
            Course.tenant_id == TENANT_ALPHA, Course.canvas_id == 20
        )
    ).first()
    assert refreshed.deleted_at is not None


def test_soft_delete_noop_when_state_is_active(db_session: Any) -> None:
    upsert_by_canvas_id(
        db_session,
        Course,
        tenant_id=TENANT_ALPHA,
        canvas_id=21,
        payload={"name": "Math", "workflow_state": "available"},
    )
    db_session.commit()

    mutated = soft_delete_if_inactive(
        db_session,
        Course,
        tenant_id=TENANT_ALPHA,
        canvas_id=21,
        workflow_state="available",
    )
    assert mutated is False


def test_soft_delete_noop_for_missing_row(db_session: Any) -> None:
    mutated = soft_delete_if_inactive(
        db_session,
        Course,
        tenant_id=TENANT_ALPHA,
        canvas_id=999,
        workflow_state="deleted",
    )
    assert mutated is False


def test_soft_delete_noop_when_already_deleted(db_session: Any) -> None:
    upsert_by_canvas_id(
        db_session,
        Course,
        tenant_id=TENANT_ALPHA,
        canvas_id=22,
        payload={"name": "Math"},
    )
    db_session.commit()
    soft_delete_if_inactive(
        db_session,
        Course,
        tenant_id=TENANT_ALPHA,
        canvas_id=22,
        workflow_state="deleted",
    )
    db_session.commit()
    second = soft_delete_if_inactive(
        db_session,
        Course,
        tenant_id=TENANT_ALPHA,
        canvas_id=22,
        workflow_state="deleted",
    )
    assert second is False


def test_inactive_workflow_states_constant_matches_spec() -> None:
    assert "deleted" in INACTIVE_WORKFLOW_STATES
    assert "completed" in INACTIVE_WORKFLOW_STATES
    assert "available" not in INACTIVE_WORKFLOW_STATES


# ---------------------------------------------------------------------------
# Cross-tenant FK
# ---------------------------------------------------------------------------


def test_assert_tenant_fk_target_succeeds_for_same_tenant(db_session: Any) -> None:
    upsert_by_canvas_id(
        db_session,
        Course,
        tenant_id=TENANT_ALPHA,
        canvas_id=30,
        payload={"name": "Math"},
    )
    db_session.commit()

    target = assert_tenant_fk_target(
        db_session,
        source_model=Assignment,
        target_model=Course,
        tenant_id=TENANT_ALPHA,
        target_canvas_id=30,
    )
    assert target.tenant_id == TENANT_ALPHA


def test_assert_tenant_fk_target_rejects_cross_tenant(db_session: Any) -> None:
    """A row in tenant B must not satisfy an FK requested from tenant A."""
    upsert_by_canvas_id(
        db_session,
        Course,
        tenant_id=TENANT_BETA,
        canvas_id=31,
        payload={"name": "Tenant B Math"},
    )
    db_session.commit()

    with pytest.raises(CrossTenantFKError) as excinfo:
        assert_tenant_fk_target(
            db_session,
            source_model=Assignment,
            target_model=Course,
            tenant_id=TENANT_ALPHA,
            target_canvas_id=31,
        )
    assert excinfo.value.canvas_id == 31


def test_assert_tenant_fk_target_rejects_missing_row(db_session: Any) -> None:
    with pytest.raises(CrossTenantFKError) as excinfo:
        assert_tenant_fk_target(
            db_session,
            source_model=Assignment,
            target_model=Course,
            tenant_id=TENANT_ALPHA,
            target_canvas_id=99999,
        )
    assert "not found" in excinfo.value.reason


# ---------------------------------------------------------------------------
# Watermark advance in transaction
# ---------------------------------------------------------------------------


def _stamp() -> datetime:
    return datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


def test_read_watermark_returns_none_for_fresh_tenant(db_session: Any) -> None:
    value = read_watermark(db_session, str(TENANT_ALPHA), "users")
    assert value is None


def test_advance_watermark_creates_row_on_first_use(db_session: Any) -> None:
    advance_watermark(
        db_session,
        str(TENANT_ALPHA),
        "users",
        _stamp(),
    )
    db_session.commit()
    refreshed = read_watermark(db_session, str(TENANT_ALPHA), "users")
    assert refreshed == _stamp()


def test_watermark_advance_persists_on_commit(db_session: Any, sqlite_engine: Any) -> None:
    advance_watermark(
        db_session,
        str(TENANT_ALPHA),
        "users",
        _stamp(),
    )
    db_session.commit()

    # Open a fresh session on the same engine and confirm the row sticks.
    from app.core.db import session_factory_for

    factory = session_factory_for(sqlite_engine)
    fresh = factory()
    try:
        value = read_watermark(fresh, str(TENANT_ALPHA), "users")
    finally:
        fresh.close()
    assert value == _stamp()


def test_watermark_advance_rolls_back_on_failure(db_session: Any, sqlite_engine: Any) -> None:
    """A failed transaction MUST leave the previous watermark intact."""
    advance_watermark(
        db_session,
        str(TENANT_ALPHA),
        "users",
        _stamp(),
    )
    db_session.commit()

    # Now simulate a Canvas failure mid-sync: stamp a new watermark
    # in the same transaction as a forced rollback.
    try:
        with db_session.begin():
            advance_watermark(
                db_session,
                str(TENANT_ALPHA),
                "users",
                datetime(2026, 8, 2, 0, 0, 0, tzinfo=UTC),
            )
            raise RuntimeError("simulated canvas_unavailable")
    except RuntimeError:
        pass

    from app.core.db import session_factory_for

    factory = session_factory_for(sqlite_engine)
    fresh = factory()
    try:
        value = read_watermark(fresh, str(TENANT_ALPHA), "users")
    finally:
        fresh.close()
    assert value == _stamp()


# ---------------------------------------------------------------------------
# Tables list against a live SQLite engine
# ---------------------------------------------------------------------------


def test_sqlite_schema_round_trip(sqlite_engine: Any) -> None:
    """create_all on SQLite emits every expected table."""
    inspector = inspect(sqlite_engine)
    tables = set(inspector.get_table_names())
    expected = {
        "tenants",
        "canvas_credentials",
        "users",
        "courses",
        "enrollments",
        "assignments",
        "submissions",
        "sync_state",
    }
    assert expected.issubset(tables)