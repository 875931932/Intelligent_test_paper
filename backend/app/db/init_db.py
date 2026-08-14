"""Fresh database bootstrap; no legacy migration is performed."""

from __future__ import annotations

import argparse
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.schema import Base, Course, User


def _engine(database_url: str) -> Engine:
    return create_engine(database_url, future=True)


def _seed_dev_data(engine: Engine) -> None:
    with Session(engine) as session:
        owner = session.get(User, "owner-dev")
        if owner is None:
            owner = User(id="owner-dev", display_name="Development Owner", role="teacher")
            session.add(owner)
            session.flush()
        if session.query(Course).filter_by(owner_id=owner.id, slug="sample-course").first() is None:
            session.add(Course(id="course-dev", owner_id=owner.id, slug="sample-course", name="示例课程", description="开发种子课程"))
        session.commit()


def bootstrap_database(database_url: str | None = None, seed: bool | None = None) -> None:
    """Create extensions, tables, indexes and optional idempotent dev seed."""

    database_url = database_url or os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL is required")
    seed = bool(seed) if seed is not None else os.getenv("SEED_DEV_DATA", "false").lower() in {"1", "true", "yes", "on"}
    engine = _engine(database_url)
    try:
        # pgvector is only available on PostgreSQL; SQLite is supported for unit tests.
        if engine.dialect.name == "postgresql":
            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
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
