import os

import pytest
from sqlalchemy import create_engine, inspect

from app.db.init_db import bootstrap_database, extension_exists, table_exists
from app.db.schema import Base, CORE_TABLE_NAMES


EXPECTED_CORE_TABLES = {
    "users", "courses", "materials", "material_versions", "upload_sessions",
    "parser_profiles", "document_parse_runs", "document_artifacts", "content_blocks",
    "framework_build_runs", "framework_versions", "framework_anchors", "framework_conflicts",
    "organization_runs", "evidence_chunks", "knowledge_catalog_versions", "content_domains",
    "assessment_units", "knowledge_cards", "knowledge_evidence_links", "index_versions",
    "index_memberships", "exam_projects", "blueprint_versions", "blueprint_sections", "plan_items",
    "generation_runs", "generation_attempts", "generated_questions", "quality_checks", "paper_versions",
    "paper_items", "model_calls", "task_runs", "outbox_events",
}


@pytest.fixture
def database_url(tmp_path):
    configured = os.getenv("TEST_DATABASE_URL")
    if configured:
        return configured
    # SQLite exercises metadata/constraint behavior without pretending to verify PostgreSQL.
    return f"sqlite:///{tmp_path / 'bootstrap.db'}"


def test_bootstrap_creates_core_schema(database_url):
    bootstrap_database(database_url, seed=False)
    for table_name in ("courses", "knowledge_cards", "generation_runs"):
        assert table_exists(database_url, table_name)
    engine = create_engine(database_url)
    try:
        assert EXPECTED_CORE_TABLES == CORE_TABLE_NAMES
        assert CORE_TABLE_NAMES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    if database_url.startswith(("postgresql://", "postgresql+")):
        assert extension_exists(database_url, "vector")


def test_bootstrap_seed_is_idempotent(database_url):
    bootstrap_database(database_url, seed=True)
    bootstrap_database(database_url, seed=True)
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM users WHERE id='owner-dev'").scalar_one() == 1
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM courses WHERE id='course-dev'").scalar_one() == 1
    finally:
        engine.dispose()


def test_core_business_schema_declares_course_isolation_and_key_constraints():
    course_owned_tables = CORE_TABLE_NAMES - {"users", "courses"}
    assert all("course_id" in Base.metadata.tables[name].columns for name in course_owned_tables)
    assert any(constraint.name == "uq_courses_owner_slug" for constraint in Base.metadata.tables["courses"].constraints)
    assert {"ix_materials_course_id", "uq_materials_course_name"} <= {
        item.name for item in Base.metadata.tables["materials"].indexes | Base.metadata.tables["materials"].constraints
    }


def test_schema_models_knowledge_card_boundary_and_generation_version_freeze():
    knowledge_card = Base.metadata.tables["knowledge_cards"]
    assert {
        "catalog_version_id", "assessment_unit_id", "name", "performance_statement", "assessable_content",
        "scope_boundary", "cognitive_targets", "allowed_question_types", "importance", "content_hash", "status", "version",
    } <= set(knowledge_card.columns.keys())
    assert "source_refs" not in knowledge_card.columns
    assert {foreign_key.target_fullname for foreign_key in knowledge_card.foreign_keys} >= {
        "knowledge_catalog_versions.id", "assessment_units.id",
    }

    generation_run = Base.metadata.tables["generation_runs"]
    assert {"framework_version_id", "catalog_version_id", "index_version_id", "blueprint_version_id", "prompt_template_version"} <= set(generation_run.columns.keys())
    assert {foreign_key.target_fullname for foreign_key in generation_run.foreign_keys} >= {
        "framework_versions.id", "knowledge_catalog_versions.id", "index_versions.id", "blueprint_versions.id",
    }
    assert any(len(index.columns) > 1 for index in generation_run.indexes)


def test_main_uses_seed_environment_when_seed_flag_is_omitted(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'seed-from-env.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SEED_DEV_DATA", "true")

    from app.db import init_db

    assert init_db.main([]) == 0
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM users WHERE id='owner-dev'").scalar_one() == 1
    finally:
        engine.dispose()
