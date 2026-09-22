"""试卷版本服务单元测试 (T5)。

复用 generation 管道创建一个 candidate 试卷版本（37题，3个needs_review），
然后测试覆写、确认、回滚等行为。
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
from app.services import exam_project_service
from app.services.blueprint_persistence_service import (
    confirm_blueprint,
    create_draft_blueprint,
)
from app.services.contract_execution_service import revise_and_confirm
from app.services.generation_runner_service import (
    enqueue_generation,
    execute_generation_task_handler,
)
from app.services.paper_version_service import (
    Conflict,
    PaperVersionError,
    PendingNeedsReview,
    confirm_paper_version,
    get_paper_version,
    list_needs_review,
    resolve_current_paper_version_id,
    revert_to_candidate,
    update_paper_item,
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
    engine = create_engine(f"sqlite:///{tmp_path / 'pv.db'}")
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
            s.execute(exam_points.insert(), [
                _ep("au1", "c1", "fv1", "A1", "EP1", "考点1", "掌握A", 30, "A1", "围绕A检索"),
                _ep("au2", "c1", "fv1", "A2", "EP2", "考点2", "掌握B", 40, "A2", "围绕B检索"),
                _ep("au3", "c1", "fv1", "A3", "EP3", "考点3", "掌握C", 30, "A3", "围绕C检索"),
            ])
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
                        "assessable_content": [f"{letter}{idx}-原子1定义", f"{letter}{idx}-原子2应用"],
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


def _full_pipeline_to_candidate_paper(session, *, fail_indices=None):
    """走完整条 pipeline 直到 candidate paper_version 返回 (pv_id, gr_id)。

    与 TR-4 的 mock 一样：第 13/27/31 道（index 12/26/30 → 题号 13,27,31）标记
    needs_review。"""
    type_rules = {
        "single_choice": {"count": 20, "score": 2},
        "true_false": {"count": 10, "score": 1},
        "fill_blank": {"count": 5, "score": 2},
        "short_answer": {"count": 2, "score": 20},
    }
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

    bv_id, _plan = create_draft_blueprint(
        session,
        course_id="c1", project_id="ep1",
        framework_version_id="fv1", catalog_version_id="cv1",
        type_rules=type_rules, chapter_weights=chapter_weights,
        units_payload=units_payload,
        card_semantic_profiles=sem, card_question_types=qtypes,
    )
    confirm_blueprint(session, course_id="c1", project_id="ep1", blueprint_version_id=bv_id)
    r = revise_and_confirm(
        session, course_id="c1", project_id="ep1", blueprint_version_id=bv_id,
        slot_revisions=[],
    )
    gr_id = r["generation_run_id"]

    # 入队并执行（mock）
    task_id = enqueue_generation(session, course_id="c1", project_id="ep1")
    tr_row = session.execute(select(task_runs).where(task_runs.c.id == task_id)).one()

    def mock_graph(session, gr, snap, progress=None):
        from app.db.schema import plan_items as pi_t
        rows = session.execute(
            select(pi_t.c.id, pi_t.c.item_index, pi_t.c.knowledge_card_id,
                   pi_t.c.question_type, pi_t.c.difficulty, pi_t.c.cognitive_level)
            .where(pi_t.c.blueprint_version_id == gr.get("blueprint_version_id"))
            .order_by(pi_t.c.item_index)
        ).all()
        items = [dict(r._mapping) for r in rows]
        out = []
        bad = fail_indices if fail_indices is not None else {12, 26, 30}
        for n in range(len(items)):
            item = items[n]
            needs_fail = n in bad
            check_pass = {"check_type": "schema", "status": "pass", "details": {"message": "OK"}}
            check_fail = {"check_type": "contract_compliance", "status": "warn",
                          "details": {"message": f"第{n+1}题警告"}}
            out.append({
                "plan_item_id": item["id"],
                "knowledge_card_id": item.get("knowledge_card_id"),
                "stem": f"原始题干 n={n+1}",
                "options": [f"A{n+1}", f"B{n+1}", f"C{n+1}", f"D{n+1}"],
                "answer": f"A{n+1}",
                "explanation": f"解析 n={n+1}",
                "question_type": item["question_type"],
                "difficulty": item["difficulty"],
                "cognitive_level": item["cognitive_level"],
                "quality": {
                    "needs_review": needs_fail,
                    "message": "质量警告" if needs_fail else "通过",
                    "quality_checks": [check_pass, check_fail] if needs_fail else [check_pass],
                },
            })
        return out

    execute_generation_task_handler(
        session, tr_row, graph_invoke=mock_graph, write_paper_version=True
    )

    # 找 paper_version id
    pv = session.execute(
        select(paper_versions.c.id).where(
            paper_versions.c.generation_run_id == gr_id,
            paper_versions.c.course_id == "c1",
        )
    ).one()
    return pv._mapping["id"], gr_id


# --- TR-5.1 ---

def test_update_rejected_after_finalize(session):
    pv_id, _ = _full_pipeline_to_candidate_paper(session)
    # 强制确认（忽略 needs_review）
    confirm_paper_version(session, course_id="c1", paper_version_id=pv_id,
                          force_ignore_needs_review=True)
    # 再更新 item_index=1 应抛 Conflict
    with pytest.raises(Conflict, match=r"finalized"):
        update_paper_item(
            session,
            course_id="c1", paper_version_id=pv_id, item_index=1,
            teacher_override_patch={"stem": "新题干"},
        )


# --- TR-5.2 ---

def test_confirm_rejects_pending_needs_review_unless_forced(session):
    pv_id, _ = _full_pipeline_to_candidate_paper(session)
    # 默认 confirm 应有 3 个待处理，抛出含 count 的错误
    with pytest.raises(PendingNeedsReview) as exc_info:
        confirm_paper_version(session, course_id="c1", paper_version_id=pv_id,
                              force_ignore_needs_review=False)
    assert "3" in exc_info.value.args[0]
    # item_indices 是 3 个：mock_graph 中 n in {12,26,30} → display_order 13,27,31
    indices = exc_info.value.item_indices
    assert len(indices) == 3
    for i in (13, 27, 31):
        assert i in indices

    # force=true 成功
    result = confirm_paper_version(
        session, course_id="c1", paper_version_id=pv_id, force_ignore_needs_review=True
    )
    assert result["status"] == "finalized"
    # 项目状态导出
    proj = session.execute(select(exam_projects).where(exam_projects.c.id == "ep1")).one()
    assert proj._mapping["status"] == "exported"
    assert proj._mapping["active_paper_version_id"] == pv_id


# --- TR-5.3 ---

def test_list_needs_review_sorted_by_item_index_and_correct_length(session):
    pv_id, _ = _full_pipeline_to_candidate_paper(session)
    review = list_needs_review(session, pv_id, course_id="c1")
    assert len(review) == 3
    indices = [r["item_index"] for r in review]
    # 升序
    assert indices == sorted(indices)
    assert indices == [13, 27, 31]
    # 每条都有 question_type, needs_review_reason
    for r in review:
        assert isinstance(r.get("question_type"), str)
        assert "needs_review_reason" in r


# --- TR-5.4 ---

def test_override_stem_only_finalized_text_uses_override(session):
    pv_id, _ = _full_pipeline_to_candidate_paper(session)
    # item_index = 5（n=4 → needs_review=False，正常修改）
    new_stem = "教师人工修改的全新题干内容XYZ"
    updated = update_paper_item(
        session,
        course_id="c1", paper_version_id=pv_id, item_index=5,
        teacher_override_patch={"stem": new_stem},
    )
    finalized = updated.get("finalized_text") or {}
    assert finalized["stem"] == new_stem

    # 再读取完整 paper_version，比较 stem 与 payload.stem
    pv = get_paper_version(session, pv_id, course_id="c1")
    q5 = next(q for q in pv["questions"] if q["item_index"] == 5)
    assert q5["stem"] == new_stem
    # payload 原始 stem 仍然是旧的
    # 注：get_paper_version 返回 stem 是 override? override : payload
    # 这里断言 override != payload：用 finalized_text 间接确认
    # 查 gq payload
    gq_payload_q = None
    gq_id_row = session.execute(
        select(generated_questions.c.payload)
        .select_from(paper_items)
        .join(generated_questions, generated_questions.c.id == paper_items.c.generated_question_id)
        .where(
            paper_items.c.paper_version_id == pv_id,
            paper_items.c.display_order == 5,
        )
    ).one()
    orig_stem = (gq_id_row._mapping["payload"] or {}).get("stem", "")
    assert orig_stem != new_stem
    assert orig_stem.startswith("原始题干 n=")


# --- revert happy path ---

def test_revert_finalized_moves_back_to_candidate(session):
    pv_id, _ = _full_pipeline_to_candidate_paper(session)
    confirm_paper_version(session, course_id="c1", paper_version_id=pv_id,
                          force_ignore_needs_review=True)
    # 验证 finalized
    pv = session.execute(
        select(paper_versions.c.status).where(paper_versions.c.id == pv_id)
    ).one()
    assert pv._mapping["status"] == "finalized"

    revert_to_candidate(session, course_id="c1", paper_version_id=pv_id)
    pv2 = session.execute(
        select(paper_versions.c.status, paper_versions.c.finalized_at)
        .where(paper_versions.c.id == pv_id)
    ).one()
    assert pv2._mapping["status"] == "candidate"
    assert pv2._mapping["finalized_at"] is None

    proj = session.execute(select(exam_projects).where(exam_projects.c.id == "ep1")).one()
    assert proj._mapping["status"] == "review"
    assert proj._mapping["active_paper_version_id"] is None


# --- 版本解析：项目“当前正在处理”的试卷版本 ---

def test_resolve_current_raises_when_no_paper_version(session):
    """项目尚无任何试卷版本时应抛 PaperVersionError（API 层映射 404）。"""
    with pytest.raises(PaperVersionError):
        resolve_current_paper_version_id(session, course_id="c1", project_id="ep1")


def test_resolve_current_returns_candidate_when_no_active(session):
    """生成完成但未确认：current 必须指向刚生成的 candidate。

    这是审核流的正常路径：active_paper_version_id 只在确认定稿时回写，
    若解析规则依赖它，生成成功后前端永远拿不到可审核的版本。
    """
    pv_id, _ = _full_pipeline_to_candidate_paper(session)
    resolved = resolve_current_paper_version_id(session, course_id="c1", project_id="ep1")
    assert resolved == pv_id


def test_resolve_current_falls_back_to_active_when_no_candidate(session):
    """确认定稿后（无待审 candidate）：current 仍指向已定稿版本，供导出使用。"""
    pv_id, _ = _full_pipeline_to_candidate_paper(session)
    confirm_paper_version(session, course_id="c1", paper_version_id=pv_id,
                          force_ignore_needs_review=True)
    resolved = resolve_current_paper_version_id(session, course_id="c1", project_id="ep1")
    assert resolved == pv_id


def test_resolve_current_prefers_fresh_candidate_over_finalized(session):
    """确认后回到生成阶段重新生成：current 应指向新 candidate 而非陈旧已定稿版本。

    旧 v1 已 finalized 且 active_paper_version_id 仍指向它，若解析规则优先
    active，审核页会看到旧卷、确认旧卷必然 409 Conflict。
    """
    pv_v1, _ = _full_pipeline_to_candidate_paper(session)
    confirm_paper_version(session, course_id="c1", paper_version_id=pv_v1,
                          force_ignore_needs_review=True)
    proj = session.execute(select(exam_projects).where(exam_projects.c.id == "ep1")).one()
    assert proj._mapping["active_paper_version_id"] == pv_v1
    assert proj._mapping["status"] == "exported"

    pv_v2, _ = _full_pipeline_to_candidate_paper(session)
    assert pv_v2 != pv_v1
    v2_row = session.execute(
        select(paper_versions.c.version_no, paper_versions.c.status)
        .where(paper_versions.c.id == pv_v2)
    ).one()
    assert v2_row._mapping["version_no"] == 2
    assert v2_row._mapping["status"] == "candidate"

    resolved = resolve_current_paper_version_id(session, course_id="c1", project_id="ep1")
    assert resolved == pv_v2


# --- get_paper_version 汇总分值 ---

def test_get_paper_version_reports_total_score(session):
    """get_paper_version 应按 plan_items 分值汇总出 total_score。"""
    pv_id, _ = _full_pipeline_to_candidate_paper(session)
    expected = session.execute(
        select(func.coalesce(func.sum(plan_items.c.score), 0.0))
        .select_from(paper_items)
        .join(generated_questions, generated_questions.c.id == paper_items.c.generated_question_id)
        .join(plan_items, plan_items.c.id == generated_questions.c.plan_item_id)
        .where(paper_items.c.paper_version_id == pv_id)
    ).scalar_one()
    assert expected > 0

    pv = get_paper_version(session, pv_id, course_id="c1")
    assert pv["total_score"] == pytest.approx(expected)
    assert len(pv["questions"]) > 0


# --- 项目摘要：total_score / item_count ---

def test_get_project_reports_paper_summary_after_generation(session):
    """生成候选卷后，项目摘要必须带上 total_score/item_count/版本指针。

    exam_projects 表没有分值、题数列，后端曾从不填充这两个字段，前端据此
    恒显示“尚未生成试卷”——即使生成已经成功、审核页就在看这一版卷子。
    """
    pv_id, _ = _full_pipeline_to_candidate_paper(session)
    project = exam_project_service.get_project(session, "c1", "ep1")
    assert project["paper_version_id"] == pv_id
    assert project["paper_version_status"] == "candidate"
    assert project["paper_version_no"] == 1
    assert project["item_count"] > 0
    assert project["total_score"] > 0
    assert project["status"] == "review"


def test_list_projects_reports_paper_summary(session):
    """课程首页列表同样要带分值/题数，否则卡片恒显示“待生成”。"""
    _full_pipeline_to_candidate_paper(session)
    projects = exam_project_service.list_projects(session, "c1")
    assert len(projects) == 1
    assert projects[0]["paper_version_id"] is not None
    assert projects[0]["item_count"] > 0
    assert projects[0]["total_score"] > 0


def test_project_summary_without_paper_version_is_none(session):
    """尚未生成过试卷的项目：字段补 None，前端据此显示“尚未生成试卷”。"""
    project = exam_project_service.get_project(session, "c1", "ep1")
    assert project["paper_version_id"] is None
    assert project["paper_version_status"] is None
    assert project["total_score"] is None
    assert project["item_count"] is None


# --- get_paper_version 逐题展示字段 ---

def test_get_paper_version_exposes_display_fields(session):
    """审核页要逐题展示分值/考点/解析，这些必须由后端随题目下发。

    分值以 plan_items 为准（合同同口径，与 total_score 不会互相矛盾）；
    考点、解析随生成载荷透传。缺了这些字段，前端只能渲染出空白卡片——
    生成成功了用户却仍然“看不到试卷”。

    注意口径：试卷版本 API 的 item_index 是 paper_items.display_order
    （题号，从 1 起），PATCH /items/{item_index} 用的也是它；plan_items
    自己的 item_index 同样从 1 起，两者在正常链路相等，这里直接对齐。
    """
    pv_id, _ = _full_pipeline_to_candidate_paper(session)
    plan = {
        r["item_index"]: r
        for r in session.execute(
            select(plan_items.c.item_index, plan_items.c.score, plan_items.c.exam_point_id)
            .order_by(plan_items.c.item_index)
        ).mappings()
    }
    assert len(plan) > 0

    pv = get_paper_version(session, pv_id, course_id="c1")
    questions = pv["questions"]
    assert len(questions) == len(plan)
    for q in questions:
        slot = plan[q["item_index"]]
        assert q["score"] == pytest.approx(slot["score"])
        assert q["exam_point_id"] == slot["exam_point_id"]
        assert q["exam_point_id"]
        assert q["explanation"] == f"解析 n={q['item_index']}"
    # 逐题分值之和必须等于总分，前端“总分 N 分”与题目行“N 分”同源
    assert sum(q["score"] for q in questions) == pytest.approx(pv["total_score"])

    # 待审题目必须带上人可读的理由串，前端据此提示“需审核：……”
    flagged = [q for q in questions if q["needs_review"]]
    assert {q["item_index"] for q in flagged} == {13, 27, 31}
    assert all(q["needs_review_reason"] for q in flagged)
