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


def _migrate_evidence_link_fk(engine: Engine) -> None:
    """Replace the run-scoped evidence link FK with a course-scoped one.

    evidence_chunks 的 id 已改为内容寻址（material_version+内容哈希派生），
    重建同一资料时新 run 复用旧 run 的 chunk 行（organization_run_id 保留
    首次创建值）。旧外键 fk_exam_point_evidence_links_chunk_run_course 要求
    (chunk_id, run_id, course_id) 三元组匹配，链接行带新 run_id 时与复用行
    对不上，publish 阶段触发 ForeignKeyViolation（生产事故 run 88c59159）。
    新外键改为 (chunk_id, course_id) 双列，课程隔离语义不变。
    PostgreSQL 幂等执行；SQLite 不持久化外键名，跳过。
    """

    if engine.dialect.name != "postgresql":
        return
    try:
        insp = inspect(engine)
        if not insp.has_table("exam_point_evidence_links"):
            return
        fks = {fk["name"]: fk for fk in insp.get_foreign_keys("exam_point_evidence_links")}
    except Exception:
        # 迁移是尽力而为的幂等维护：无法内省（如 bootstrap 单测的 mock engine）
        # 时跳过，不阻断启动。
        return
    old = fks.get("fk_exam_point_evidence_links_chunk_run_course")
    new = fks.get("fk_exam_point_evidence_links_chunk_course")
    if old is None and new is not None:
        return
    with engine.begin() as conn:
        if old is not None:
            conn.execute(
                text(
                    "ALTER TABLE exam_point_evidence_links "
                    "DROP CONSTRAINT fk_exam_point_evidence_links_chunk_run_course"
                )
            )
        if new is None:
            conn.execute(
                text(
                    "ALTER TABLE exam_point_evidence_links ADD CONSTRAINT "
                    "fk_exam_point_evidence_links_chunk_course "
                    "FOREIGN KEY (evidence_chunk_id, course_id) "
                    "REFERENCES evidence_chunks (id, course_id)"
                )
            )


def _migrate_evidence_chunk_columns(engine: Engine) -> None:
    """Idempotently add kind / source_evidence_chunk_id / embedding_model columns.

    知识点抽取环节新增：区分原始块(raw)与蒸馏陈述(statement)，并记录陈述的
    溯源原始块 id；embedding_model 记录向量的生成模型（换模型后旧向量不可比，
    命中缓存前需比对）。旧表无这些列，create_all 不会 ALTER 已存在表，需显式迁移。
    """

    insp = inspect(engine)
    if not insp.has_table("evidence_chunks"):
        return
    existing = {c["name"] for c in insp.get_columns("evidence_chunks")}
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "postgresql":
            if "kind" not in existing:
                conn.execute(text("ALTER TABLE evidence_chunks ADD COLUMN kind VARCHAR(20) NOT NULL DEFAULT 'raw'"))
            if "source_evidence_chunk_id" not in existing:
                conn.execute(text("ALTER TABLE evidence_chunks ADD COLUMN source_evidence_chunk_id VARCHAR(64)"))
        elif "kind" not in existing:
            conn.execute(text("ALTER TABLE evidence_chunks ADD COLUMN kind VARCHAR(20) NOT NULL DEFAULT 'raw'"))
            if "source_evidence_chunk_id" not in existing:
                conn.execute(text("ALTER TABLE evidence_chunks ADD COLUMN source_evidence_chunk_id VARCHAR(64)"))
        if "embedding_model" not in existing:
            conn.execute(text("ALTER TABLE evidence_chunks ADD COLUMN embedding_model VARCHAR(64)"))


def _migrate_evidence_link_score(engine: Engine) -> None:
    """Idempotently add retrieval_score column to exam_point_evidence_links.

    检索分数观测列（垃圾率×分数分布的调参依据）；旧表无此列，create_all 不会
    ALTER 已存在表，需显式迁移。无法内省（bootstrap 单测的 mock engine）时跳过。
    """

    try:
        insp = inspect(engine)
        if not insp.has_table("exam_point_evidence_links"):
            return
        existing = {c["name"] for c in insp.get_columns("exam_point_evidence_links")}
    except Exception:
        # 迁移是尽力而为的幂等维护：无法内省时不阻断启动（与 evidence link FK 迁移同口径）。
        return
    if "retrieval_score" in existing:
        return
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("ALTER TABLE exam_point_evidence_links ADD COLUMN retrieval_score DOUBLE PRECISION"))
        else:
            conn.execute(text("ALTER TABLE exam_point_evidence_links ADD COLUMN retrieval_score FLOAT"))


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


def _drop_all(engine: Engine) -> None:
    """Drop all tables so a fresh bootstrap is possible.

    Uses SQLAlchemy's metadata ``drop_all`` rather than ``DROP SCHEMA``:
    hosted PostgreSQL connection users usually are not the owner of the
    ``public`` schema, so dropping the schema would raise
    ``InsufficientPrivilege``. ``drop_all`` issues per-table ``DROP``
    statements, which the table owner can run.
    """
    Base.metadata.drop_all(engine)


def bootstrap_database(database_url: str | None = None, seed: bool | None = None, drop: bool = False) -> None:
    """Create extensions, tables, indexes and optional idempotent dev seed.

    When ``drop`` is true the current schema is fully wiped first.
    """

    database_url = database_url or os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL is required")
    seed = bool(seed) if seed is not None else os.getenv("SEED_DEV_DATA", "false").lower() in {"1", "true", "yes", "on"}
    engine = _engine(database_url)
    try:
        # A transaction-scoped advisory lock serializes fresh PostgreSQL initialization.
        if engine.dialect.name == "postgresql":
            if drop:
                _drop_all(engine)
            with engine.begin() as conn:
                conn.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": 824036462})
            # pgvector 扩展可选：失败时跳过（schema 用 JSON 存 embedding）
            with engine.connect() as ext_conn:
                try:
                    ext_conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                    ext_conn.commit()
                except Exception:
                    ext_conn.rollback()
            with engine.begin() as conn:
                Base.metadata.create_all(conn)
                _migrate_user_columns(engine)
                _migrate_evidence_link_fk(engine)
                _migrate_evidence_chunk_columns(engine)
                _migrate_evidence_link_score(engine)
                if seed:
                    _seed_dev_data(conn)
        else:
            if drop:
                _drop_all(engine)
            Base.metadata.create_all(engine)
            _migrate_user_columns(engine)
            _migrate_evidence_link_fk(engine)
            _migrate_evidence_chunk_columns(engine)
            _migrate_evidence_link_score(engine)
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
    parser.add_argument(
        "--drop-all",
        action="store_true",
        default=False,
        help="drop the entire schema and all data before bootstrapping",
    )
    args = parser.parse_args(argv)
    bootstrap_database(seed=args.seed, drop=args.drop_all)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
