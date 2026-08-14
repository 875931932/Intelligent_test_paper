import os

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex, CreateTable

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


def test_course_scoped_foreign_key_rejects_cross_course_parent_reference(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'course-isolation.db'}")
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            connection.execute(Base.metadata.tables["users"].insert(), {"id": "owner", "display_name": "Owner", "role": "teacher"})
            connection.execute(Base.metadata.tables["courses"].insert(), [
                {"id": "course-a", "owner_id": "owner", "slug": "a", "name": "A"},
                {"id": "course-b", "owner_id": "owner", "slug": "b", "name": "B"},
            ])
            connection.execute(Base.metadata.tables["materials"].insert(), {
                "id": "material-a", "course_id": "course-a", "logical_name": "a.pdf", "material_type": "teaching", "status": "staged",
            })
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(Base.metadata.tables["material_versions"].insert(), {
                    "id": "version-wrong-course", "course_id": "course-b", "material_id": "material-a", "version_no": 1, "status": "staged",
                })
    finally:
        engine.dispose()


def test_course_scoped_parent_relationships_use_composite_foreign_keys():
    required_parent_links = {
        "material_versions": "materials",
        "document_parse_runs": "material_versions",
        "document_artifacts": "document_parse_runs",
        "framework_versions": "framework_build_runs",
        "knowledge_catalog_versions": "framework_versions",
        "knowledge_evidence_links": "knowledge_cards",
        "index_memberships": "index_versions",
        "blueprint_versions": "exam_projects",
        "plan_items": "blueprint_versions",
        "generation_runs": "blueprint_versions",
        "generation_attempts": "generation_runs",
        "generated_questions": "generation_runs",
        "quality_checks": "generated_questions",
        "paper_items": "paper_versions",
        "outbox_events": "task_runs",
    }
    for child_name, parent_name in required_parent_links.items():
        constraints = Base.metadata.tables[child_name].foreign_key_constraints
        assert any(
            set(constraint.column_keys) >= {"course_id"}
            and {element.target_fullname for element in constraint.elements} >= {f"{parent_name}.id", f"{parent_name}.course_id"}
            for constraint in constraints
        ), child_name


def test_postgresql_bootstrap_uses_transactional_advisory_lock(monkeypatch):
    from app.db import init_db

    executed_sql = []

    class Connection:
        def execute(self, statement, *_args, **_kwargs):
            executed_sql.append(str(statement))

    class Transaction:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return False

    class Engine:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def begin(self):
            return Transaction()

        def dispose(self):
            pass

    monkeypatch.setattr(init_db, "_engine", lambda _: Engine())
    monkeypatch.setattr(Base.metadata, "create_all", lambda *_: None)

    init_db.bootstrap_database("postgresql+psycopg://example/exam", seed=False)

    assert any("pg_advisory_xact_lock" in statement for statement in executed_sql)


def test_postgresql_ddl_compiles_with_identifier_safe_constraint_and_index_names():
    dialect = postgresql.dialect()

    for table in Base.metadata.sorted_tables:
        for constraint in table.constraints:
            if constraint.name is not None:
                assert len(constraint.name) <= 63, constraint.name
        for index in table.indexes:
            assert len(index.name) <= 63, index.name
            str(CreateIndex(index).compile(dialect=dialect))
        str(CreateTable(table).compile(dialect=dialect))
