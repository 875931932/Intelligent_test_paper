"""Schema extension tests for PaperVersion kernel and serviceization (S1).

These tests validate that schema.py has the new columns and cross-course
foreign-key constraints required by tasks 1-6.  They fail in the baseline
code and pass after schema extensions.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKeyConstraint, Integer, JSON, String, Text, create_engine, event, inspect
from sqlalchemy.exc import IntegrityError

from app.db.schema import Base


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path / 'pv-kernel-schema.db'}")
    event.listen(e, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(e)
    return e


# --- TR-1.1 paper_versions / paper_items new columns ---

def test_paper_versions_declares_metadata_lifecycle_and_creator_columns():
    pv = Base.metadata.tables["paper_versions"]
    expected = {
        "created_at", "confirmed_at", "finalized_at", "created_by",
        "metadata", "answer_detail_schema_version",
    }
    missing = expected - set(pv.columns.keys())
    assert not missing, f"paper_versions missing columns: {missing}"
    # created_at must have a server default like the other lifecycle columns
    assert pv.c.created_at.server_default is not None
    assert isinstance(pv.c.metadata.type, JSON)
    # created_by FK references users.id
    fk_targets = {fk.target_fullname for fk in pv.c.created_by.foreign_keys}
    assert fk_targets == {"users.id"}
    assert pv.c.created_by.nullable, "created_by must remain nullable for seed-less tests"


def test_paper_items_declares_override_finalized_and_review_fields():
    pi = Base.metadata.tables["paper_items"]
    expected = {
        "teacher_override", "finalized_text", "needs_review",
        "needs_review_reason", "quality_audit",
    }
    missing = expected - set(pi.columns.keys())
    assert not missing, f"paper_items missing columns: {missing}"
    assert isinstance(pi.c.teacher_override.type, JSON)
    assert isinstance(pi.c.needs_review.type, Boolean)
    assert pi.c.needs_review.server_default is not None


def test_generation_runs_declares_contract_and_lifecycle_columns():
    gr = Base.metadata.tables["generation_runs"]
    expected = {
        "contract_snapshot", "centrality_threshold_used",
        "updated_at", "completed_at", "error_message",
    }
    missing = expected - set(gr.columns.keys())
    assert not missing, f"generation_runs missing columns: {missing}"
    assert isinstance(gr.c.contract_snapshot.type, JSON)
    assert gr.c.updated_at.server_default is not None
    assert gr.c.created_at.server_default is not None


def test_blueprint_versions_declares_rules_and_lifecycle_columns():
    bv = Base.metadata.tables["blueprint_versions"]
    expected = {"type_rules", "chapter_weights", "confirmed_at", "created_at"}
    missing = expected - set(bv.columns.keys())
    assert not missing, f"blueprint_versions missing columns: {missing}"
    assert isinstance(bv.c.type_rules.type, JSON)
    assert bv.c.created_at.server_default is not None


def test_exam_projects_declares_active_version_foreign_keys():
    ep = Base.metadata.tables["exam_projects"]
    expected = {
        "active_blueprint_version_id": "blueprint_versions",
        "active_generation_run_id": "generation_runs",
        "active_paper_version_id": "paper_versions",
    }
    for col_name, parent_name in expected.items():
        assert col_name in ep.columns.keys(), f"exam_projects missing {col_name}"
        col = ep.c[col_name]
        assert col.nullable, f"{col_name} must be nullable"
        # composite FK (col + course_id) against parent.{id, course_id}
        found = False
        for fk in ep.foreign_key_constraints:
            if not {"course_id", col_name} <= set(fk.column_keys):
                continue
            targets = {el.target_fullname for el in fk.elements}
            if targets >= {f"{parent_name}.id", f"{parent_name}.course_id"}:
                found = True
                break
        assert found, (
            f"exam_projects.{col_name} missing cross-course FK to {parent_name}. "
            f"actual FKs column_keys = {[sorted(fk.column_keys) for fk in ep.foreign_key_constraints]}"
        )


# --- cross-course FK integrity smoke (SQLite) ---

def test_active_paper_version_fk_rejects_cross_course_reference(engine):
    with engine.begin() as conn:
        conn.execute(Base.metadata.tables["users"].insert(), {"id": "u1", "display_name": "U", "role": "teacher"})
        conn.execute(Base.metadata.tables["courses"].insert(), [
            {"id": "c-a", "owner_id": "u1", "slug": "a", "name": "A"},
            {"id": "c-b", "owner_id": "u1", "slug": "b", "name": "B"},
        ])
        conn.execute(Base.metadata.tables["exam_projects"].insert(), [
            {"id": "p-a", "course_id": "c-a", "name": "PA", "status": "draft"},
            {"id": "p-b", "course_id": "c-b", "name": "PB", "status": "draft"},
        ])
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            # paper_version lives in c-b; trying to set p-a (c-a) to reference it.
            conn.execute(
                Base.metadata.tables["paper_versions"].insert(),
                {
                    "id": "pv-b", "course_id": "c-b",
                    "exam_project_id": "p-b", "version_no": 1, "status": "draft",
                },
            )
            conn.execute(
                Base.metadata.tables["exam_projects"].update()
                .where(Base.metadata.tables["exam_projects"].c.id == "p-a")
                .values(active_paper_version_id="pv-b"),
            )
