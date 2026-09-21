"""模型调用与生成任务的日志可见性测试。

背景：线上排查时"日志里看不到任何模型调用记录"，而 model_calls 表明
调用全部成功——根因是 DatabaseModelCallRecorder 只写库不打日志，
生成任务执行器也没有任何日志。本组测试锁定：

1. recorder 每次落库同时输出一行 INFO（stage/model/status/耗时/token）；
2. 失败调用升级为 WARNING 且带 error_code；
3. 生成任务 开始/图返回/完成/失败 四个节点均有日志，含 run 与题数；
4. DeepSeekGateway 构造时打印生效的 base_url/model（api_key 脱敏）。
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.model.deepseek_gateway import DeepSeekGateway
from app.db.schema import (
    Base,
    Course,
    User,
    assessment_units,
    blueprint_versions,
    content_domains,
    exam_points,
    exam_projects,
    framework_build_runs,
    framework_versions,
    generation_runs,
    knowledge_catalog_versions,
    model_calls,
    plan_items,
    task_runs,
)
from app.domain.model_calls import ModelCallContext
from app.infrastructure.tasks.models import create_task_run
from app.services.generation_runner_service import execute_generation_task_handler
from app.services.model_call_service import DatabaseModelCallRecorder


# ---------------------------------------------------------------------------
# recorder 日志
# ---------------------------------------------------------------------------

@pytest.fixture
def recorder_env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recorder.db'}")
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(User(id="u1", display_name="T1", role="teacher"))
        s.flush()
        s.add(Course(id="c1", owner_id="u1", slug="cs101", name="CS101"))
        s.commit()
        with s.begin():
            s.execute(framework_build_runs.insert().values(
                id="run1", course_id="c1", status="running", input_snapshot={},
            ))
    yield engine
    engine.dispose()


def _ctx(stage: str = "paper_generation") -> ModelCallContext:
    return ModelCallContext(course_id="c1", framework_build_run_id="run1", stage=stage)


def test_record_emits_info_log_with_model_and_tokens(recorder_env, caplog):
    caplog.set_level(logging.INFO, logger="model.call")
    DatabaseModelCallRecorder(sessionmaker(bind=recorder_env)).record(
        context=_ctx(),
        provider="deepseek",
        model="step-3.7-flash",
        status="succeeded",
        prompt_hash="a" * 64,
        input_tokens=1200,
        output_tokens=350,
        duration_ms=4200,
        error_code=None,
        error_message=None,
    )
    records = [r for r in caplog.records if r.name == "model.call"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.INFO
    message = record.getMessage()
    assert "paper_generation" in message
    assert "step-3.7-flash" in message
    assert "succeeded" in message
    assert "4200" in message
    # 日志不得破坏落库主流程
    with Session(recorder_env) as s:
        assert s.execute(select(model_calls.c.id)).scalar_one() is not None


def test_record_emits_warning_log_on_failure(recorder_env, caplog):
    caplog.set_level(logging.INFO, logger="model.call")
    DatabaseModelCallRecorder(sessionmaker(bind=recorder_env)).record(
        context=_ctx(),
        provider="deepseek",
        model="step-3.7-flash",
        status="failed",
        prompt_hash="b" * 64,
        input_tokens=None,
        output_tokens=None,
        duration_ms=800,
        error_code="upstream_error",
        error_message="502 Bad Gateway",
    )
    records = [r for r in caplog.records if r.name == "model.call"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "failed" in message
    assert "upstream_error" in message
    assert "502" in message


# ---------------------------------------------------------------------------
# 生成任务生命周期日志
# ---------------------------------------------------------------------------

@pytest.fixture
def gen_env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'gen.db'}")
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(User(id="u1", display_name="T1", role="teacher"))
        s.flush()
        s.add(Course(id="c1", owner_id="u1", slug="cs101", name="CS101"))
        s.commit()
        with s.begin():
            s.execute(framework_versions.insert().values(
                id="fv1", course_id="c1", version_no=1, status="published", payload={},
            ))
            s.execute(knowledge_catalog_versions.insert().values(
                id="cv1", course_id="c1", framework_version_id="fv1",
                version_no=1, status="published",
            ))
            s.execute(exam_points.insert().values(
                id="ep_x", course_id="c1", framework_version_id="fv1", anchor_key="A1",
                code="EP1", title="考点1", assessment_requirement="掌握A",
                weight_value=100, weight_source="teacher_confirmed", weight_group_id="A1",
                priority="normal", cognitive_targets=[], assessment_orientations=[],
                allowed_question_types=[], operational_detail_policy="supporting_only",
                scope_boundary={}, required_evidence_roles=[], retrieval_intent="围绕A检索",
                teaching_anchor_keys=[], status="active",
            ))
            s.execute(content_domains.insert().values(
                id="cd1", course_id="c1", catalog_version_id="cv1", parent_domain_id=None,
                level=1, framework_anchor_key="A1", code="A1", name="章1", status="active",
            ))
            s.execute(assessment_units.insert().values(
                id="au1", course_id="c1", catalog_version_id="cv1", content_domain_id="cd1",
                exam_point_id="ep_x", code="U1", title="单元1", performance_statement="ps1",
                weight=100, status="active",
            ))
            s.execute(exam_projects.insert().values(
                id="ep1", course_id="c1", name="P", status="generating",
            ))
            s.execute(blueprint_versions.insert().values(
                id="bv1", course_id="c1", exam_project_id="ep1",
                framework_version_id="fv1", catalog_version_id="cv1",
                version_no=1, status="confirmed",
            ))
            s.execute(generation_runs.insert().values(
                id="gr1", course_id="c1", framework_version_id="fv1",
                catalog_version_id="cv1", blueprint_version_id="bv1",
                prompt_template_version="v1", run_type="paper_generation",
                status="queued",
                contract_snapshot={"slots": [{"item_index": 0, "score": 5}]},
            ))
            s.execute(plan_items.insert().values(
                id="pi1", course_id="c1", blueprint_version_id="bv1",
                assessment_unit_id="au1", question_type="single_choice",
                item_index=0, score=5,
            ))
    yield engine
    engine.dispose()


def _mock_graph(session, gr, snap):
    return [{
        "plan_item_id": "pi1",
        "knowledge_card_id": None,
        "item_index": 0,
        "stem": "题干",
        "options": {"A": "a"},
        "answer": "A",
        "difficulty": "easy",
        "cognitive_level": "remember",
        "quality": {"status": "pass"},
    }]


def _make_task(session) -> dict:
    task_id = create_task_run(
        session,
        course_id="c1",
        task_type="generation_run",
        idempotency_key="key1",
        input_version="v1",
        payload={"project_id": "ep1", "generation_run_id": "gr1"},
    )
    row = session.execute(select(task_runs).where(task_runs.c.id == task_id)).one()
    return dict(row._mapping)


def test_generation_task_logs_start_and_completion(gen_env, caplog):
    caplog.set_level(logging.INFO, logger="generation.runner")
    with Session(gen_env) as s:
        task = _make_task(s)
        execute_generation_task_handler(
            s, task, graph_invoke=_mock_graph, write_paper_version=False,
        )
    messages = [r.getMessage() for r in caplog.records if r.name == "generation.runner"]
    assert any("生成任务开始" in m and "gr1" in m for m in messages), messages
    assert any("生成图返回" in m and "questions=1" in m for m in messages), messages
    assert any("生成任务完成" in m and "questions=1" in m for m in messages), messages


def test_generation_task_logs_failure_with_run_id(gen_env, caplog):
    caplog.set_level(logging.INFO, logger="generation.runner")

    def _boom(session, gr, snap):
        raise RuntimeError("模型返回结构非法")

    with Session(gen_env) as s:
        task = _make_task(s)
        with pytest.raises(RuntimeError, match="模型返回结构非法"):
            execute_generation_task_handler(
                s, task, graph_invoke=_boom, write_paper_version=False,
            )
    error_records = [
        r for r in caplog.records
        if r.name == "generation.runner" and r.levelno >= logging.ERROR
    ]
    assert error_records, [r.getMessage() for r in caplog.records]
    assert any("生成任务失败" in r.getMessage() and "gr1" in r.getMessage() for r in error_records)


# ---------------------------------------------------------------------------
# gateway 配置日志
# ---------------------------------------------------------------------------

def test_gateway_logs_effective_configuration(caplog):
    caplog.set_level(logging.INFO, logger="model.gateway")
    DeepSeekGateway(
        api_key="sk-secret-value",
        base_url="https://api.stepfun.com/v1",
        model="step-3.7-flash",
    )
    records = [r for r in caplog.records if r.name == "model.gateway"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "step-3.7-flash" in message
    assert "api.stepfun.com" in message
    assert "sk-secret-value" not in message
