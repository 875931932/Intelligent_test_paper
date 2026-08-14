import os

import pytest
from sqlalchemy import create_engine, inspect

from app.db.init_db import bootstrap_database, extension_exists, table_exists
from app.db.schema import Base, CORE_TABLE_NAMES


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
