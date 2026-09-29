"""Fresh database bootstrap; no legacy migration is performed."""

from __future__ import annotations

import argparse
import os
from uuid import uuid4

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


def _migrate_content_block_columns(engine: Engine) -> None:
    """Idempotently add embedding / embedding_model columns to content_blocks.

    助手 v2 资料内容问答（RAG）：解析块逐块嵌入落库，语义检索用。旧表无这些列，
    create_all 不会 ALTER 已存在表，需显式迁移（与 evidence_chunks 向量列同语义）。
    """

    try:
        insp = inspect(engine)
        if not insp.has_table("content_blocks"):
            return
        existing = {c["name"] for c in insp.get_columns("content_blocks")}
    except Exception:
        # 迁移是尽力而为的幂等维护：无法内省时不阻断启动（与 link score 迁移同口径）。
        return
    with engine.begin() as conn:
        if "embedding" not in existing:
            conn.execute(text("ALTER TABLE content_blocks ADD COLUMN embedding JSON"))
        if "embedding_model" not in existing:
            conn.execute(text("ALTER TABLE content_blocks ADD COLUMN embedding_model VARCHAR(64)"))
        if "embedding_text_version" not in existing:
            conn.execute(
                text(
                    "ALTER TABLE content_blocks ADD COLUMN embedding_text_version "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            )


def _backfill_assistant_sessions(conn: Connection) -> None:
    """为 session_id IS NULL 的历史消息逐课程建会话并回填（空库天然无行）。"""

    courses = conn.execute(
        text(
            "SELECT course_id, MIN(created_at), MAX(created_at) "
            "FROM assistant_messages WHERE session_id IS NULL GROUP BY course_id"
        )
    ).fetchall()
    for course_id, first_at, last_at in courses:
        sample = conn.execute(
            text(
                "SELECT content FROM assistant_messages "
                "WHERE course_id = :cid AND session_id IS NULL AND role = 'user' "
                "ORDER BY created_at ASC, id ASC LIMIT 1"
            ),
            {"cid": course_id},
        ).scalar_one_or_none()
        title = str(sample or "").replace("\n", " ").strip()[:24] or "历史会话"
        session_id = uuid4().hex
        conn.execute(
            text(
                "INSERT INTO assistant_sessions (id, course_id, title, created_at, updated_at) "
                "VALUES (:id, :cid, :title, :first_at, :last_at)"
            ),
            {"id": session_id, "cid": course_id, "title": title, "first_at": first_at, "last_at": last_at},
        )
        conn.execute(
            text(
                "UPDATE assistant_messages SET session_id = :sid "
                "WHERE course_id = :cid AND session_id IS NULL"
            ),
            {"sid": session_id, "cid": course_id},
        )


def _migrate_assistant_session(engine: Engine) -> None:
    """助手 v3 多会话：补 session_id 列 + 索引 + 回填历史会话 + CHECK 加 stopped。

    - 列/索引/回填双方言幂等（create_all 不 ALTER 已存在表；回填只处理
      session_id IS NULL 的历史行）。
    - CHECK 约束：PostgreSQL 用 DROP IF EXISTS + ADD（先例 _migrate_evidence_link_fk）；
      SQLite 不能原地改约束 → 跳过（测试与 CI 全为新建库、create_all 自带新 CHECK；
      开发库为 PostgreSQL）。老 SQLite 库如需续用需重建，文档已注明。
    """

    try:
        insp = inspect(engine)
        if not insp.has_table("assistant_messages"):
            return
        existing = {c["name"] for c in insp.get_columns("assistant_messages")}
        has_sessions = insp.has_table("assistant_sessions")
    except Exception:
        # 迁移是尽力而为的幂等维护：无法内省（bootstrap 单测的 mock engine）时不阻断启动。
        return
    with engine.begin() as conn:
        if "session_id" not in existing:
            conn.execute(text("ALTER TABLE assistant_messages ADD COLUMN session_id VARCHAR(64)"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_assistant_messages_course_session "
                "ON assistant_messages (course_id, session_id, created_at)"
            )
        )
        if has_sessions:
            _backfill_assistant_sessions(conn)
        if engine.dialect.name == "postgresql":
            conn.execute(
                text("ALTER TABLE assistant_messages DROP CONSTRAINT IF EXISTS ck_assistant_messages_stream_status")
            )
            conn.execute(
                text(
                    "ALTER TABLE assistant_messages ADD CONSTRAINT ck_assistant_messages_stream_status "
                    "CHECK (stream_status IN ('streaming', 'complete', 'failed', 'stopped'))"
                )
            )


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


def _migrate_knowledge_link_role(engine: Engine) -> None:
    """Idempotently归一 knowledge_evidence_links.evidence_role 到相关性域。

    卡级链接 role 曾误落答案域值（answer_basis——direct 决策按 content_kind 推导的
    答案角色），而 grounded 判定（published-knowledge 的 evidence_role='direct'）与
    前端证据标签读的是相关性域，导致已发布目录全量「未落地」（2026-09-25 根因）。
    写端已改为落 relevance_class；历史 answer_basis 只可能由 direct 决策产生，
    确定性归一为 direct。'fact'（旧写端对空 role 的兜底，源自非 direct 决策）无法
    回推原值，保持不动。
    """

    try:
        insp = inspect(engine)
        if not insp.has_table("knowledge_evidence_links"):
            return
    except Exception:
        # 迁移是尽力而为的幂等维护：无法内省时不阻断启动（同 retrieval_score 口径）。
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE knowledge_evidence_links "
                "SET evidence_role = 'direct' "
                "WHERE evidence_role = 'answer_basis'"
            )
        )


def _migrate_course_columns(engine: Engine) -> None:
    """Idempotently add courses.category（课程类别 → 题型集合/格式预设的选取键）。

    旧库无此列，create_all 不会 ALTER 已存在表，需显式迁移（与 evidence_chunks
    各列同语义）；默认值 'general'（domain/course/category_profiles.DEFAULT_CATEGORY）。
    """

    try:
        insp = inspect(engine)
        if not insp.has_table("courses"):
            return
        existing = {c["name"] for c in insp.get_columns("courses")}
    except Exception:
        # 迁移是尽力而为的幂等维护：无法内省时不阻断启动（同 retrieval_score 口径）。
        return
    if "category" in existing:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE courses ADD COLUMN category VARCHAR(40) "
                "NOT NULL DEFAULT 'general'"
            )
        )


def _migrate_generated_question_nullable(engine: Engine) -> None:
    """Idempotently 放开 generated_questions.generation_run_id / plan_item_id 非空。

    资料库归档「存回试卷区」的题与教师手拟题没有生成 run、也没有蓝图槽位，
    内容整体落在 payload（读端 plan 侧字段缺失时回落 payload）。旧库这两列是
    NOT NULL，create_all 不会 ALTER 已存在表，需显式 DROP NOT NULL。
    SQLite 无 ALTER COLUMN DROP NOT NULL 语法，且测试库均为 create_all 新建
    （直接建出可空列），故旧库迁移只在 PostgreSQL 上执行。
    """

    if engine.dialect.name != "postgresql":
        return
    try:
        insp = inspect(engine)
        if not insp.has_table("generated_questions"):
            return
        cols = {c["name"]: c for c in insp.get_columns("generated_questions")}
    except Exception:
        # 迁移是尽力而为的幂等维护：无法内省时不阻断启动（同 retrieval_score 口径）。
        return
    to_relax = [
        name
        for name in ("generation_run_id", "plan_item_id")
        if name in cols and not cols[name].get("nullable", True)
    ]
    if not to_relax:
        return
    with engine.begin() as conn:
        for name in to_relax:
            conn.execute(
                text(f"ALTER TABLE generated_questions ALTER COLUMN {name} DROP NOT NULL")
            )


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
        # 裸 CLI（`uv run python -m app.db.init_db`）没有调用方代为加载 .env：
        # 回退到 pydantic settings——它按文件位置向上查找仓库根 .env（AGENTS §3.1 文档流程）。
        from app.config import settings

        database_url = settings.database_url or None
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
            # create_all 必须先提交，再在**其它连接**上跑迁移：迁移会 ALTER 已有表
            # （如 courses.category），而未提交的 create_all 事务因新表外键（_course_table
            # 的 course_id → courses）持有父表锁——迁移等它提交、它等迁移结束，
            # 即自死锁，init_db 会无限挂起。SQLite 分支本就是"先 create_all 后迁移"，此处对齐。
            with engine.begin() as conn:
                Base.metadata.create_all(conn)
            _migrate_user_columns(engine)
            _migrate_evidence_link_fk(engine)
            _migrate_evidence_chunk_columns(engine)
            _migrate_evidence_link_score(engine)
            _migrate_knowledge_link_role(engine)
            _migrate_content_block_columns(engine)
            _migrate_assistant_session(engine)
            _migrate_course_columns(engine)
            _migrate_generated_question_nullable(engine)
            if seed:
                with engine.begin() as conn:
                    _seed_dev_data(conn)
        else:
            if drop:
                _drop_all(engine)
            Base.metadata.create_all(engine)
            _migrate_user_columns(engine)
            _migrate_evidence_link_fk(engine)
            _migrate_evidence_chunk_columns(engine)
            _migrate_evidence_link_score(engine)
            _migrate_knowledge_link_role(engine)
            _migrate_content_block_columns(engine)
            _migrate_assistant_session(engine)
            _migrate_course_columns(engine)
            _migrate_generated_question_nullable(engine)
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
