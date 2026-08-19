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

    def graph(session, gr, snap):
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

    def exploding_graph(session, gr, snap):
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
