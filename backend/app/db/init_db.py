"""Fresh database bootstrap; no legacy migration is performed."""

from __future__ import annotations

import argparse
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.db.schema import Base, Course, User


def _engine(database_url: str) -> Engine:
    return create_engine(database_url, future=True)


def _seed_dev_data(bind: Engine | Connection) -> None:
    """Insert development records atomically, without a read-before-write race."""

    dialect_name = bind.dialect.name
    insert = postgresql_insert if dialect_name == "postgresql" else sqlite_insert
    with Session(bind=bind) as session:
        session.execute(
            insert(User)
            .values(id="owner-dev", display_name="Development Owner", role="teacher")
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.execute(
            insert(Course)
            .values(id="course-dev", owner_id="owner-dev", slug="sample-course", name="示例课程", description="开发种子课程")
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.commit()


def bootstrap_database(database_url: str | None = None, seed: bool | None = None) -> None:
    """Create extensions, tables, indexes and optional idempotent dev seed."""

    database_url = database_url or os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL is required")
    seed = bool(seed) if seed is not None else os.getenv("SEED_DEV_DATA", "false").lower() in {"1", "true", "yes", "on"}
    engine = _engine(database_url)
    try:
        # A transaction-scoped advisory lock serializes fresh PostgreSQL initialization.
        if engine.dialect.name == "postgresql":
            with engine.begin() as conn:
                conn.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": 824036462})
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                Base.metadata.create_all(conn)
                if seed:
                    _seed_dev_data(conn)
        else:
            Base.metadata.create_all(engine)
            if seed:
                _seed_dev_data(engine)
    finally:
        engine.dispose()


def table_exists(database_url: str, table_name: str) -> bool:
    engine = _engine(database_url)
    try:
        return inspect(engine).has_table(table_name)
    finally:
        engine.dispose()


def extension_exists(database_url: str, extension_name: str) -> bool:
    engine = _engine(database_url)
    try:
        if engine.dialect.name != "postgresql":
            return False
        with engine.connect() as conn:
            return bool(conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = :name"), {"name": extension_name}).first())
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed",
        action="store_true",
        default=None,
        help="insert idempotent development owner and sample course; omitted uses SEED_DEV_DATA",
    )
    args = parser.parse_args(argv)
    bootstrap_database(seed=args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
