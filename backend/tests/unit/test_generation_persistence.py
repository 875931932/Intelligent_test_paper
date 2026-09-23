"""生成执行器单元测试 (T4)。

验证 enqueue 幂等、mock 生成 37 题落库生成 + 试卷、异常时 generation_run 标记 failed
且不产生 paper_version。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, select, func
from sqlalchemy.orm import Session

from app.db.schema import (
    Base,
    Course,
    User,
    assessment_units,
    content_domains,
    exam_points,
    exam_projects,
    framework_versions,
    generated_questions,
    generation_runs,
    knowledge_cards,
    knowledge_catalog_versions,
    paper_items,
    paper_versions,
    plan_items,
    task_runs,
)
from app.services.blueprint_persistence_service import (
    confirm_blueprint,
    create_draft_blueprint,
)
from app.services.contract_execution_service import revise_and_confirm
from app.services.generation_runner_service import (
    GenerationRunnerError,
    _default_graph_invoke,
    enqueue_generation,
    execute_generation_task_handler,
)


def _ep(id_, course, fv, anchor, code, title, req, w, group, intent):
    return {
        "id": id_, "course_id": course,
        "framework_version_id": fv, "anchor_key": anchor,
        "code": code, "title": title,
        "assessment_requirement": req,
        "weight_value": w, "weight_source": "teacher_confirmed",
        "weight_group_id": group, "priority": "normal",
        "cognitive_targets": [], "assessment_orientations": [],
        "allowed_question_types": [],
        "operational_detail_policy": "supporting_only",
        "scope_boundary": {}, "required_evidence_roles": [],
        "retrieval_intent": intent,
        "teaching_anchor_keys": [],
        "status": "active",
    }


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'gp.db'}")
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
            # exam_points
            s.execute(exam_points.insert(), [
                _ep("au1", "c1", "fv1", "A1", "EP1", "考点1", "掌握A", 30, "A1", "围绕A检索"),
                _ep("au2", "c1", "fv1", "A2", "EP2", "考点2", "掌握B", 40, "A2", "围绕B检索"),
                _ep("au3", "c1", "fv1", "A3", "EP3", "考点3", "掌握C", 30, "A3", "围绕C检索"),
            ])
            # 3 个章节 anchor（A1/A2/A3），每个章节 1 个 assessment_unit，2 张知识卡
            s.execute(content_domains.insert(), [
                {"id": "cd1", "course_id": "c1", "catalog_version_id": "cv1",
                 "parent_domain_id": None, "level": 1,
                 "framework_anchor_key": "A1", "code": "A1", "name": "章1", "status": "active"},
                {"id": "cd2", "course_id": "c1", "catalog_version_id": "cv1",
                 "parent_domain_id": None, "level": 1,
                 "framework_anchor_key": "A2", "code": "A2", "name": "章2", "status": "active"},
                {"id": "cd3", "course_id": "c1", "catalog_version_id": "cv1",
                 "parent_domain_id": None, "level": 1,
                 "framework_anchor_key": "A3", "code": "A3", "name": "章3", "status": "active"},
            ])
            s.execute(assessment_units.insert(), [
                {"id": "au1", "course_id": "c1", "catalog_version_id": "cv1",
                 "content_domain_id": "cd1", "exam_point_id": "au1",
                 "code": "U1", "title": "单元1", "performance_statement": "ps1",
                 "weight": 30, "status": "active"},
                {"id": "au2", "course_id": "c1", "catalog_version_id": "cv1",
                 "content_domain_id": "cd2", "exam_point_id": "au2",
                 "code": "U2", "title": "单元2", "performance_statement": "ps2",
                 "weight": 40, "status": "active"},
                {"id": "au3", "course_id": "c1", "catalog_version_id": "cv1",
                 "content_domain_id": "cd3", "exam_point_id": "au3",
                 "code": "U3", "title": "单元3", "performance_statement": "ps3",
                 "weight": 30, "status": "active"},
            ])
            cards = []
            for au, letter in [("au1", "A"), ("au2", "B"), ("au3", "C")]:
                for idx in (1, 2):
                    cards.append({
                        "id": f"c{letter.lower()}{idx}",
                        "course_id": "c1",
                        "catalog_version_id": "cv1",
                        "assessment_unit_id": au,
                        "name": f"卡{letter}{idx}",
                        "performance_statement": f"掌握{letter}{idx}",
                        "assessable_content": [
                            f"{letter}{idx}-原子1定义", f"{letter}{idx}-原子2应用",
                        ],
                        "content_hash": f"h{letter.lower()}{idx}",
                        "status": "active",
                        "concept_cluster": letter,
                        "answer_proposition": f"{letter}{idx}-边界",
                    })
            s.execute(knowledge_cards.insert(), cards)
            s.execute(exam_projects.insert().values(
                id="ep1", course_id="c1", name="Midterm", status="draft",
            ))
        yield s
    engine.dispose()


def _setup_pipeline(session, *, question_counts: dict | None = None):
    """走完 blueprint_draft → confirm → contract_revise_and_confirm，
    返回 (generation_run_id, plan_items_by_index)。

    默认题量：凑到恰好 37 道（TR-4.2），分题型：
      single_choice 20 × 2 = 40 分
      true_false    10 × 1 = 10 分
      fill_blank     5 × 2 = 10 分
      short_answer   2 × 20 = 40 分
      合计 37 题，总分 100
    """
    if question_counts is None:
        type_rules = {
            "single_choice": {"count": 20, "score": 2},
            "true_false": {"count": 10, "score": 1},
            "fill_blank": {"count": 5, "score": 2},
            "short_answer": {"count": 2, "score": 20},
        }
    else:
        type_rules = question_counts
    chapter_weights = {"A1": 30, "A2": 40, "A3": 30}
    units_payload = [
        {"unit_id": "au1", "exam_point_id": "", "anchor_key": "A1",
         "card_ids": ["ca1", "ca2"]},
        {"unit_id": "au2", "exam_point_id": "", "anchor_key": "A2",
         "card_ids": ["cb1", "cb2"]},
        {"unit_id": "au3", "exam_point_id": "", "anchor_key": "A3",
         "card_ids": ["cc1", "cc2"]},
    ]
    sem = {}
    qtypes = {}
    for cid in ["ca1", "ca2", "cb1", "cb2", "cc1", "cc2"]:
        letter = cid[1].upper()
        sem[cid] = {"concept_cluster": letter, "answer_proposition": cid}
        qtypes[cid] = list(type_rules.keys())

    bv_id, plan = create_draft_blueprint(
        session,
        course_id="c1", project_id="ep1",
        framework_version_id="fv1", catalog_version_id="cv1",
        type_rules=type_rules, chapter_weights=chapter_weights,
        units_payload=units_payload,
        card_semantic_profiles=sem,
        card_question_types=qtypes,
    )
    confirm_blueprint(session, course_id="c1", project_id="ep1", blueprint_version_id=bv_id)
    result = revise_and_confirm(
        session,
        course_id="c1", project_id="ep1", blueprint_version_id=bv_id,
        slot_revisions=[],
    )
    # 加载 plan_items: 建立 item_index → {plan_item_id, card_id, ...}
    rows = session.execute(
        select(plan_items.c.id, plan_items.c.item_index, plan_items.c.knowledge_card_id)
        .where(
            plan_items.c.blueprint_version_id == bv_id,
            plan_items.c.course_id == "c1",
        )
        .order_by(plan_items.c.item_index)
    ).all()
    pi_by_index = {r._mapping["item_index"]: {
        "plan_item_id": r._mapping["id"],
        "knowledge_card_id": r._mapping["knowledge_card_id"],
    } for r in rows}
    return result["generation_run_id"], pi_by_index


# ---------------------------------------------------------------------------
# Mock graph_invoke helper: 为每个 plan_item 生成 1 道题，共 len(pi_by_index) 道
# ---------------------------------------------------------------------------

def _make_mock_37_graph(num_questions: int = 37):
    """返回一个闭包 graph_invoke：根据 generation_run 的 plan_items 长度
    生成 num_questions 道题。题量不足或超出时按 plan_items 循环填满。"""

    def graph(session, gr, snap, progress=None):
        slots = (snap or {}).get("slots") or []
        gr_id = gr.get("id")
        # 读取 DB 中的 plan_items（按 generation_run.blueprint_version_id）
        from app.db.schema import plan_items as pi_table
        bv_id = gr.get("blueprint_version_id")
        rows = session.execute(
            select(pi_table.c.id, pi_table.c.item_index, pi_table.c.knowledge_card_id,
                   pi_table.c.question_type, pi_table.c.difficulty, pi_table.c.cognitive_level)
            .where(pi_table.c.blueprint_version_id == bv_id)
            .order_by(pi_table.c.item_index)
        ).all()
        items = [dict(r._mapping) for r in rows]
        # 生成 num_questions 题，按 items 循环
        out = []
        for n in range(num_questions):
            item = items[n % len(items)]
            # 第 13、27、31 道设置质量检查失败（needs_review=True），给 T5 测试用
            check_pass = {"check_type": "schema", "status": "pass",
                          "details": {"message": "OK"}}
            check_fail = {"check_type": "contract_compliance", "status": "warn",
                          "details": {"message": "答案域警告"}}
            needs_fail = n in {12, 26, 30}
            out.append({
                "plan_item_id": item["id"],
                "knowledge_card_id": item.get("knowledge_card_id"),
                "stem": f"题{n+1}：关于{item['question_type']}的题干内容",
                "options": [f"选项A{n+1}", f"选项B{n+1}", f"选项C{n+1}", f"选项D{n+1}"],
                "answer": f"选项A{n+1}",
                "question_type": item["question_type"],
                "difficulty": item["difficulty"],
                "cognitive_level": item["cognitive_level"],
                "quality": {
                    "needs_review": needs_fail,
                    "message": "质量警告" if needs_fail else "全部通过",
                    "quality_checks": [check_pass, check_fail] if needs_fail else [check_pass],
                },
            })
        return out

    return graph


# --- TR-4.1 ---

def test_enqueue_idempotent(session):
    gr_id, _ = _setup_pipeline(session)
    # 项目有 active_generation_run_id
    proj = session.execute(select(exam_projects).where(exam_projects.c.id == "ep1")).one()
    assert proj._mapping["active_generation_run_id"] == gr_id

    t1 = enqueue_generation(session, course_id="c1", project_id="ep1")
    t2 = enqueue_generation(session, course_id="c1", project_id="ep1")
    assert t1 == t2
    # 验证数据库仅 1 行
    cnt = session.execute(
        select(func.count()).select_from(task_runs)
        .where(task_runs.c.course_id == "c1", task_runs.c.task_type == "generation_run")
    ).scalar_one()
    assert cnt == 1
    # 任务仍在途（queued）→ 幂等生效，run 不变
    proj = session.execute(select(exam_projects).where(exam_projects.c.id == "ep1")).one()
    assert proj._mapping["active_generation_run_id"] == gr_id


def test_enqueue_after_terminal_mints_new_run(session):
    """同 run 的任务已终态时再入队必须换 run。

    否则「重新生成」只会拿回同一条已 succeeded 的任务：界面显示"生成完成"，
    却没有新任务、没有新试卷版本，整个入口是个空操作。
    """
    gr_id, _ = _setup_pipeline(session)
    t1 = enqueue_generation(session, course_id="c1", project_id="ep1")
    # 把任务推到终态，模拟上一次生成已经结束
    session.execute(
        task_runs.update().where(task_runs.c.id == t1).values(status="succeeded")
    )
    session.commit()

    t2 = enqueue_generation(session, course_id="c1", project_id="ep1")

    assert t2 != t1
    proj = session.execute(select(exam_projects).where(exam_projects.c.id == "ep1")).one()
    new_run = proj._mapping["active_generation_run_id"]
    assert new_run != gr_id, "终态后再入队必须铸造新的 generation_run"
    # 新任务必须指向新 run：否则 runner 会往旧 run 补写 revision_no=1，
    # 撞上 generated_questions 的唯一约束
    tr = session.execute(select(task_runs).where(task_runs.c.id == t2)).one()
    assert tr._mapping["payload"]["generation_run_id"] == new_run
    cnt = session.execute(
        select(func.count()).select_from(task_runs)
        .where(task_runs.c.course_id == "c1", task_runs.c.task_type == "generation_run")
    ).scalar_one()
    assert cnt == 2


def test_enqueue_after_failed_task_also_rerolls(session):
    """failed 态同样不能返回旧任务——失败后点「重新生成」应真正重跑。"""
    gr_id, _ = _setup_pipeline(session)
    t1 = enqueue_generation(session, course_id="c1", project_id="ep1")
    session.execute(
        task_runs.update().where(task_runs.c.id == t1).values(status="failed")
    )
    session.commit()

    t2 = enqueue_generation(session, course_id="c1", project_id="ep1")
    assert t2 != t1
    proj = session.execute(select(exam_projects).where(exam_projects.c.id == "ep1")).one()
    assert proj._mapping["active_generation_run_id"] != gr_id


# --- TR-4.2 ---

def test_execute_creates_37_generated_questions_and_paper_version(session):
    _gr_id, pi_by_index = _setup_pipeline(session)
    # 题目数应该是 20+10+5+2 = 37
    assert len(pi_by_index) == 37

    # 入队
    task_id = enqueue_generation(session, course_id="c1", project_id="ep1")
    # 读 task_run 行
    tr_row = session.execute(
        select(task_runs).where(task_runs.c.id == task_id)
    ).one()
    # 执行（mock graph）
    execute_generation_task_handler(
        session,
        tr_row,
        graph_invoke=_make_mock_37_graph(37),
        write_paper_version=True,
    )

    # 断言 generated_questions == 37
    gq_cnt = session.execute(
        select(func.count()).select_from(generated_questions)
        .where(generated_questions.c.course_id == "c1",
               generated_questions.c.generation_run_id == _gr_id)
    ).scalar_one()
    assert gq_cnt == 37

    # 断言 paper_items == 37，paper_version status candidate
    pi_cnt = session.execute(
        select(func.count()).select_from(paper_items)
        .join(paper_versions, paper_versions.c.id == paper_items.c.paper_version_id)
        .where(paper_versions.c.generation_run_id == _gr_id,
               paper_versions.c.course_id == "c1")
    ).scalar_one()
    assert pi_cnt == 37

    pv = session.execute(
        select(paper_versions).where(
            paper_versions.c.generation_run_id == _gr_id,
            paper_versions.c.course_id == "c1",
        )
    ).one()
    assert pv._mapping["status"] == "candidate"
    # generation_run 状态应为 succeeded
    gr = session.execute(
        select(generation_runs).where(generation_runs.c.id == _gr_id)
    ).one()
    assert gr._mapping["status"] == "succeeded"


# --- TR-4.3 ---

def test_graph_invoke_raise_causes_run_failed_and_no_paper_version(session):
    gr_id, pi_by_index = _setup_pipeline(session)

    task_id = enqueue_generation(session, course_id="c1", project_id="ep1")
    tr_row = session.execute(
        select(task_runs).where(task_runs.c.id == task_id)
    ).one()

    def exploding_graph(session, gr, snap, progress=None):
        raise RuntimeError("boom 模型不可用")

    with pytest.raises(RuntimeError, match=r"boom"):
        execute_generation_task_handler(
            session,
            tr_row,
            graph_invoke=exploding_graph,
            write_paper_version=True,
        )

    # generation_run.status == failed, error_message 非空
    gr = session.execute(
        select(generation_runs).where(generation_runs.c.id == gr_id)
    ).one()
    assert gr._mapping["status"] == "failed"
    err = gr._mapping["error_message"] or ""
    assert "boom" in err

    # 不产生 paper_version 行（绑定到该 generation_run_id）
    pv = session.execute(
        select(func.count()).select_from(paper_versions)
        .where(paper_versions.c.generation_run_id == gr_id,
               paper_versions.c.course_id == "c1")
    ).scalar_one()
    assert pv == 0


def _capturing_gateway_factory(captured: list):
    """返回可 monkeypatch 的 LLMGateway 替身：按合同规格产出合规题干，
    并把收到的 payload 记入 captured（断言"不向模型暴露来源元数据"）。"""

    class CapturingGateway:
        def __init__(self, **_kwargs):
            pass

        def generate_batch(self, payload):
            rendered = payload.model_dump(mode="json")
            captured.append(rendered)
            for forbidden in ("material_version_id", "source_locator", "evidence_chunk"):
                assert forbidden not in repr(rendered)
            questions = []
            for spec in payload.questions:
                question = {
                    "item_index": spec.item_index,
                    "question_type": spec.question_type,
                    "stem": f"关于{spec.coverage_atom}的题干",
                }
                if spec.question_type == "single_choice":
                    question.update({
                        "options": [spec.answer_boundary, "干扰项一", "干扰项二", "干扰项三"],
                        "answer": spec.answer_boundary,
                    })
                elif spec.question_type == "true_false":
                    question["answer"] = True
                elif spec.question_type == "fill_blank":
                    question.update({
                        "stem": f"{spec.coverage_atom}对应的核心术语是____。",
                        "answer": spec.answer_boundary,
                    })
                else:
                    question.update({
                        "answer": spec.answer_boundary,
                        "explanation": "依据给定知识原子说明。",
                        "rubric": [{"point": "回答核心内容", "score": spec.score}],
                    })
                questions.append(question)
            return questions

    return CapturingGateway


def test_default_graph_invocation_uses_contract_and_pure_knowledge_cards(session, monkeypatch):
    """正式装配链路：真实 LangGraph + 假模型网关，不向模型暴露来源元数据。"""
    from app.adapters.model import llm_gateway
    from app.config import settings

    generation_run_id, _ = _setup_pipeline(session)
    run = session.execute(
        select(generation_runs).where(generation_runs.c.id == generation_run_id)
    ).one()._mapping
    captured_payloads = []

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "https://model.invalid/v1")
    monkeypatch.setattr(settings, "llm_model", "test-model")
    monkeypatch.setattr(
        llm_gateway, "LLMGateway", _capturing_gateway_factory(captured_payloads),
    )

    questions = _default_graph_invoke(session, dict(run), run["contract_snapshot"])

    assert len(questions) == len(run["contract_snapshot"]["slots"])
    assert {question["plan_item_id"] for question in questions}
    assert all(question["quality"]["needs_review"] is False for question in questions)
    assert captured_payloads


# ---------------------------------------------------------------------------
# 生成进度上报：graph_invoke 期间 task_runs.progress 必须逐批推进且立即可见
# ---------------------------------------------------------------------------

def _progress_from_independent_connection(session, task_id: str):
    """用独立连接读 task_runs.progress，模拟前端轮询的连接视角。

    进度写在任务的长事务里若不提交，轮询连接永远读不到——用户看到的就是
    「卡在正在生成试题，请稍候…」。
    """
    probe = create_engine(str(session.bind.url))
    try:
        with probe.connect() as conn:
            return conn.execute(
                select(task_runs.c.progress).where(task_runs.c.id == task_id)
            ).scalar_one()
    finally:
        probe.dispose()


def test_execution_reports_intermediate_progress(session):
    """整图执行期间 progress 回调必须把 task_runs.progress 推到 5~100 之间，
    并以独立事务提交（前端轮询连接立即可见）。"""
    _setup_pipeline(session)
    task_id = enqueue_generation(session, course_id="c1", project_id="ep1")
    tr_row = session.execute(
        select(task_runs).where(task_runs.c.id == task_id)
    ).one()

    seen: list[int] = []

    def progress_graph(session, gr, snap, progress=None):
        assert progress is not None, "execute_generation_task 必须注入进度回调"
        for done, total in ((1, 4), (2, 4), (4, 4)):
            progress(done, total)
            seen.append(_progress_from_independent_connection(session, task_id))
        return _make_mock_37_graph(37)(session, gr, snap, progress=progress)

    execute_generation_task_handler(
        session,
        tr_row,
        graph_invoke=progress_graph,
        write_paper_version=False,
    )

    assert seen, "进度回调一次都没有被调用"
    assert all(5 < value < 100 for value in seen), seen
    assert seen == sorted(seen), "进度必须单调递增"
    # 成功收尾仍以 100% 落位
    final = session.execute(
        select(task_runs.c.status, task_runs.c.progress).where(task_runs.c.id == task_id)
    ).one()
    assert final._mapping["status"] == "succeeded"
    assert final._mapping["progress"] == 100


def test_default_graph_invoke_stream_progress_matches_batches(session, monkeypatch):
    """真实图的 stream 路径：按批回调 (done, total)，最终题目与 invoke 一致。"""
    from app.adapters.model import llm_gateway
    from app.config import settings

    generation_run_id, _ = _setup_pipeline(session)
    run = session.execute(
        select(generation_runs).where(generation_runs.c.id == generation_run_id)
    ).one()._mapping
    captured: list[dict] = []

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "https://model.invalid/v1")
    monkeypatch.setattr(settings, "llm_model", "test-model")
    monkeypatch.setattr(
        llm_gateway, "LLMGateway", _capturing_gateway_factory(captured),
    )

    baseline = _default_graph_invoke(session, dict(run), run["contract_snapshot"])
    calls: list[tuple[int, int]] = []
    streamed = _default_graph_invoke(
        session,
        dict(run),
        run["contract_snapshot"],
        progress=lambda done, total: calls.append((done, total)),
    )

    assert calls, "stream 路径未上报任何批次进度"
    total = calls[0][1]
    assert total >= 1
    assert all(t == total for _, t in calls)
    assert [done for done, _ in calls] == list(range(1, total + 1))
    # stream 聚合结果与 invoke 完全一致
    assert streamed == baseline
    assert captured


def test_worker_path_publishes_progress_for_polling(session, monkeypatch):
    """Celery worker 是线上真实执行路径（manage_task_run=False）：
    图跑批期间 task_runs.progress 必须可被轮询连接读到，收尾仍是 succeeded/100。"""
    from app.infrastructure.tasks import worker
    from app.services import generation_runner_service

    _setup_pipeline(session)
    task_id = enqueue_generation(session, course_id="c1", project_id="ep1")
    mock = _make_mock_37_graph(37)
    seen: list[int] = []

    def worker_graph(session, gr, snap, progress=None):
        assert progress is not None, "worker 路径同样必须注入进度回调"
        progress(1, 2)
        seen.append(_progress_from_independent_connection(session, task_id))
        progress(2, 2)
        seen.append(_progress_from_independent_connection(session, task_id))
        return mock(session, gr, snap, progress=progress)

    monkeypatch.setattr(worker, "get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(generation_runner_service, "_default_graph_invoke", worker_graph)

    assert worker.execute_task(task_id, worker_id="w1") is True

    assert seen == [50, 95], seen
    # execute_task 会关闭借来的 session，收尾状态只能从独立连接读
    probe = create_engine(str(session.bind.url))
    try:
        with probe.connect() as conn:
            final = conn.execute(
                select(task_runs.c.status, task_runs.c.progress)
                .where(task_runs.c.id == task_id)
            ).one()._mapping
    finally:
        probe.dispose()
    assert final["status"] == "succeeded"
    assert final["progress"] == 100

# --- TR-4.4 模型不可用：不许"成功"地产出空卷 ---

def _placeholder_graph(session, gr, snap, progress=None):
    """模拟模型服务完全不可用（线上为账户欠费 402）：每个题位只返回占位题。"""
    from app.db.schema import plan_items as pi_table

    slots = (snap or {}).get("slots") or []
    rows = session.execute(
        select(pi_table.c.id, pi_table.c.item_index)
        .where(pi_table.c.blueprint_version_id == gr.get("blueprint_version_id"))
        .order_by(pi_table.c.item_index)
    ).all()
    pi_by_index = {r._mapping["item_index"]: r._mapping["id"] for r in rows}
    return [
        {
            "item_index": slot.get("item_index"),
            "plan_item_id": pi_by_index.get(slot.get("item_index")),
            "knowledge_card_id": slot.get("card_id"),
            "question_type": slot.get("question_type"),
            "score": slot.get("score"),
            "difficulty": slot.get("difficulty"),
            "cognitive_level": slot.get("cognitive_level"),
            "exam_point_id": slot.get("exam_point_id"),
            "unit_id": slot.get("unit_id"),
            "coverage_atom": slot.get("coverage_atom"),
            "answer_boundary": slot.get("answer_boundary"),
            "quality": {"status": "blocker", "message": "批返回缺失该题"},
            "needs_review": True,
        }
        for slot in slots
    ]


def test_all_placeholder_questions_fail_the_run_without_paper_version(session):
    gr_id, _pi_by_index = _setup_pipeline(session)

    task_id = enqueue_generation(session, course_id="c1", project_id="ep1")
    tr_row = session.execute(
        select(task_runs).where(task_runs.c.id == task_id)
    ).one()

    with pytest.raises(GenerationRunnerError, match="模型未产出可用题目"):
        execute_generation_task_handler(
            session,
            tr_row,
            graph_invoke=_placeholder_graph,
            write_paper_version=True,
        )

    gr = session.execute(
        select(generation_runs).where(generation_runs.c.id == gr_id)
    ).one()
    assert gr._mapping["status"] == "failed"
    assert gr._mapping["error_message"]
    # 关键：不创建任何试卷版本，否则教师会看到一张全是空题的"成功"卷子
    pv_cnt = session.execute(
        select(func.count()).select_from(paper_versions)
        .where(paper_versions.c.generation_run_id == gr_id)
    ).scalar_one()
    assert pv_cnt == 0
    # generated_questions 仍落库（供排查），但不落 paper_items
    pi_cnt = session.execute(
        select(func.count()).select_from(paper_items).where(
            paper_items.c.paper_version_id.in_(
                select(paper_versions.c.id).where(paper_versions.c.generation_run_id == gr_id)
            )
        )
    ).scalar_one()
    assert pi_cnt == 0


def test_partial_usable_paper_below_threshold_also_fails(session, monkeypatch):
    from app.services import generation_runner_service as runner

    monkeypatch.setattr(runner.settings, "generation_min_usable_ratio", 0.9)
    gr_id, _pi_by_index = _setup_pipeline(session)

    task_id = enqueue_generation(session, course_id="c1", project_id="ep1")
    tr_row = session.execute(
        select(task_runs).where(task_runs.c.id == task_id)
    ).one()

    def mostly_placeholder_graph(session, gr, snap, progress=None):
        out = _placeholder_graph(session, gr, snap, progress)
        out[0]["stem"] = "完整的题干"
        out[0]["answer"] = "A"
        return out

    with pytest.raises(GenerationRunnerError, match="模型未产出可用题目"):
        execute_generation_task_handler(
            session,
            tr_row,
            graph_invoke=mostly_placeholder_graph,
            write_paper_version=True,
        )

    pv_cnt = session.execute(
        select(func.count()).select_from(paper_versions)
        .where(paper_versions.c.generation_run_id == gr_id)
    ).scalar_one()
    assert pv_cnt == 0


def test_healthy_generation_still_succeeds_with_ratio(session):
    gr_id, _pi_by_index = _setup_pipeline(session)
    task_id = enqueue_generation(session, course_id="c1", project_id="ep1")
    tr_row = session.execute(
        select(task_runs).where(task_runs.c.id == task_id)
    ).one()

    result = execute_generation_task_handler(
        session,
        tr_row,
        graph_invoke=_make_mock_37_graph(37),
        write_paper_version=True,
    )
    assert result["generated_questions"] == 37
    assert result["dropped_questions"] == 0
    assert result["usable_ratio"] == 1.0

    pv_cnt = session.execute(
        select(func.count()).select_from(paper_versions)
        .where(paper_versions.c.generation_run_id == gr_id)
    ).scalar_one()
    assert pv_cnt == 1
