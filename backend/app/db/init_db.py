"""Fresh database bootstrap; no legacy migration is performed."""

from __future__ import annotations

import argparse
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.db.schema import Base, User
from app.services.auth_service import hash_password

ADMIN_USER_ID = "admin"
LEGACY_DEV_OWNER_ID = "owner-dev"


def _engine(database_url: str) -> Engine:
    connect_args: dict[str, object] = {}
    if database_url.startswith("postgresql"):
        connect_args["options"] = "-c search_path=public"
    return create_engine(database_url, future=True, connect_args=connect_args)


def _migrate_user_columns(engine: Engine) -> None:
    """Idempotently add auth columns to an existing users table (no legacy migration layer)."""

    insp = inspect(engine)
    if not insp.has_table("users"):
        return
    existing = {c["name"] for c in insp.get_columns("users")}
    to_add = [name for name, _ddl in (("username", "VARCHAR(120)"), ("password_hash", "VARCHAR(255)")) if name not in existing]
    if not to_add:
        return
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            for name, ddl in (("username", "VARCHAR(120)"), ("password_hash", "VARCHAR(255)")):
                if name in to_add:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {name} {ddl}"))
        else:
            for name, ddl in (("username", "VARCHAR(120)"), ("password_hash", "VARCHAR(255)")):
                if name in to_add:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {ddl}"))


def _seed_dev_data(bind: Engine | Connection) -> None:
    """Upsert the admin test account and fold any legacy 'owner-dev' data into it."""

    dialect_name = bind.dialect.name
    insert = postgresql_insert if dialect_name == "postgresql" else sqlite_insert
    admin_password_hash = hash_password("123456")
    with Session(bind=bind) as session:
        stmt = (
            insert(User)
            .values(
                id=ADMIN_USER_ID,
                username="admin",
                password_hash=admin_password_hash,
                display_name="系统管理员",
                role="admin",
            )
            .on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "username": "admin",
                    "password_hash": admin_password_hash,
                    "display_name": "系统管理员",
                    "role": "admin",
                },
            )
        )
        session.execute(stmt)
        # 保留历史 owner-dev 名下的课程，统一归属到 admin
        session.execute(text("UPDATE courses SET owner_id=:admin WHERE owner_id=:legacy"), {"admin": ADMIN_USER_ID, "legacy": LEGACY_DEV_OWNER_ID})
        session.execute(text("UPDATE paper_versions SET created_by=:admin WHERE created_by=:legacy"), {"admin": ADMIN_USER_ID, "legacy": LEGACY_DEV_OWNER_ID})
        session.execute(text("DELETE FROM users WHERE id=:legacy"), {"legacy": LEGACY_DEV_OWNER_ID})
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
            # pgvector 扩展可选：失败时跳过（schema 用 JSON 存 embedding）
            with engine.connect() as ext_conn:
                try:
                    ext_conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                    ext_conn.commit()
                except Exception:
                    ext_conn.rollback()
            with engine.begin() as conn:
                conn.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": 824036462})
                Base.metadata.create_all(conn)
                _migrate_user_columns(engine)
                if seed:
                    _seed_dev_data(conn)
        else:
            Base.metadata.create_all(engine)
            _migrate_user_columns(engine)
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
