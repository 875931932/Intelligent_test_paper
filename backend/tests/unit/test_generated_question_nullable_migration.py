"""存回试卷区的前置结构变更：generated_questions 两列放宽为可空。

旧库的 generation_run_id / plan_item_id 是 NOT NULL（只有生成链路写它）。资料库
「存回试卷区」与教师手拟的题没有生成 run、也没有蓝图槽位，落库前必须幂等
DROP NOT NULL。SQLite 不支持该语法，按方言直接跳过（新库由 create_all 天生可空）。

PG 真机路径由 `uv run python -m app.db.init_db` 对既有库实跑验证（迁移本身
按 inspect 结果只放宽仍为 NOT NULL 的列，重复执行即空操作）。
"""
from __future__ import annotations

from sqlalchemy import create_engine, inspect

from app.db.init_db import _migrate_generated_question_nullable, bootstrap_database
from app.db.schema import generated_questions as gq_table


def _columns(engine):
    return {c["name"]: c for c in inspect(engine).get_columns("generated_questions")}


def _create_legacy_table(engine) -> None:
    """造一张「旧结构」：两列 NOT NULL（不含 create_all，手工等价 DDL）。"""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE generated_questions ("
            "id VARCHAR(64) PRIMARY KEY, "
            "generation_run_id VARCHAR(64) NOT NULL, "
            "plan_item_id VARCHAR(64) NOT NULL, "
            "revision_no INTEGER NOT NULL, "
            "status VARCHAR(40) NOT NULL, "
            "payload TEXT NOT NULL)"
        )


def test_migration_skips_on_non_postgresql_dialect(tmp_path):
    """SQLite 上迁移按方言跳过：不发不被支持的 DROP NOT NULL，也不报错。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy_gq.db'}")
    try:
        _create_legacy_table(engine)
        assert _columns(engine)["generation_run_id"]["nullable"] is False

        _migrate_generated_question_nullable(engine)

        assert engine.dialect.name != "postgresql"
        assert _columns(engine)["generation_run_id"]["nullable"] is False
        # 幂等：重复执行仍是空操作
        _migrate_generated_question_nullable(engine)
    finally:
        engine.dispose()


def test_migration_is_noop_when_table_absent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    try:
        _migrate_generated_question_nullable(engine)   # 连表都没有 → 直接返回
    finally:
        engine.dispose()


def test_fresh_schema_keeps_columns_nullable_and_migrates_idempotently(tmp_path):
    """空库：create_all 已是可空结构，迁移再跑一次不得改坏它。"""
    url = f"sqlite:///{tmp_path / 'fresh_gq.db'}"
    bootstrap_database(url, seed=False)
    engine = create_engine(url)
    try:
        cols = _columns(engine)
        assert cols["generation_run_id"]["nullable"] is True
        assert cols["plan_item_id"]["nullable"] is True

        _migrate_generated_question_nullable(engine)

        cols = _columns(engine)
        assert cols["generation_run_id"]["nullable"] is True
        assert cols["plan_item_id"]["nullable"] is True
        # 表结构与 schema.py 声明一致（改 schema 不同步迁移会在 PG 上留隐患）
        assert {c.name for c in gq_table.columns} <= set(cols)
    finally:
        engine.dispose()
