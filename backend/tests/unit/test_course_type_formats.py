"""生成装配的题型格式合并：类别预设打底，考核规则 type_formats 逐题型压过。

约定键随知识卡字典注入图输入（见 generation_runner_service._default_graph_invoke），
本文件测其数据合成部分。
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.db.schema import Base, Course, User, framework_versions
from app.services.generation_runner_service import _course_type_formats


def _session(tmp_path, *, category: str | None = None, exam_rules: dict | None = None):
    engine = create_engine(f"sqlite:///{tmp_path / 'type_formats.db'}")
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(User(id="u1", display_name="U", role="teacher"))
    session.flush()
    course = Course(id="c1", owner_id="u1", slug="c1", name="C1")
    if category is not None:
        course.category = category
    session.add(course)
    session.flush()
    if exam_rules is not None:
        session.execute(
            framework_versions.insert().values(
                id="fv1",
                course_id="c1",
                version_no=1,
                status="published",
                payload={"anchors": [], "final_exam_rules": exam_rules},
                published_at=datetime.now(UTC),
            )
        )
    session.commit()
    return engine, session


def test_category_preset_supplies_formats_without_framework(tmp_path):
    engine, session = _session(tmp_path, category="computer")
    try:
        merged = _course_type_formats(session, "c1")
        # 类别预设提供覆盖题型，未覆盖的题型不落键（回落全局档案）
        assert "single_choice" in merged and "true_false" in merged
        # 类别模板是完整任务卡（空数约束 + 唯一性防线），不是占位符
        assert "恰好包含 1 个空" in merged["fill_blank"]
        assert "唯一" in merged["fill_blank"]
    finally:
        session.close()
        engine.dispose()


def test_exam_rules_type_formats_override_category_per_type(tmp_path):
    engine, session = _session(
        tmp_path,
        category="computer",
        exam_rules={"type_formats": {"single_choice": "教师改的单选格式"}},
    )
    try:
        merged = _course_type_formats(session, "c1")
        # 逐题型压过：改过的题型用考核规则值
        assert merged["single_choice"] == "教师改的单选格式"
        # 未改的题型仍按类别预设
        assert "恰好包含 1 个空" in merged["fill_blank"]
    finally:
        session.close()
        engine.dispose()


def test_general_category_without_overrides_yields_empty(tmp_path):
    engine, session = _session(tmp_path, exam_rules={})
    try:
        # 通用档 + 无课程覆盖 = 不注入（生成走全局档案）
        assert _course_type_formats(session, "c1") == {}
    finally:
        session.close()
        engine.dispose()


def test_missing_course_or_framework_falls_back_safely(tmp_path):
    engine, session = _session(tmp_path, category="humanities")
    try:
        # 课程 id 不存在：类别回退通用档（无覆盖）而不是抛错
        assert _course_type_formats(session, "missing") == {}
        # 论述类预设存在
        assert "essay" in _course_type_formats(session, "c1")
    finally:
        session.close()
        engine.dispose()
