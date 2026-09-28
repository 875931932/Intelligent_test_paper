"""试卷历史：每项目只保留最近 3 份，超出的连题目内容一起物理删除。

覆盖三件事：
1. 保留策略删得够深——paper_versions / paper_items / generated_questions /
   quality_checks / model_calls 全部消失，而 generation_runs、generation_attempts
   这两类运行日志仍在；
2. 豁免规则——被删卷引用的题目若还被保留卷挂着，题目不删；当前卷指针绝不悬空；
3. 切当前卷——只改 exam_projects 指针，已定稿的旧卷仍是 finalized。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.db.schema import (
    Base,
    Course,
    User,
    assessment_units,
    blueprint_versions,
    content_domains,
    exam_points,
    exam_projects,
    framework_versions,
    generated_questions,
    generation_attempts,
    generation_runs,
    knowledge_cards,
    knowledge_catalog_versions,
    model_calls,
    paper_items,
    paper_versions,
    plan_items,
    quality_checks,
)
from app.services.paper_version_service import (
    PaperVersionError,
    activate_paper_version,
    create_paper_version_from_generation,
    list_paper_versions,
    prune_paper_version_history,
)

_PAYLOAD = {
    "stem": "关于梯度同步，下列说法正确的是？",
    "options": {"A": "需要通信", "B": "不需要"},
    "answer": "A",
    "explanation": "同步梯度需要通信。",
    "question_type": "single_choice",
    "difficulty": "medium",
    "score": 2.0,
    "exam_point_id": "ep1",
}

# fixture 里预置的试卷版本数；保留策略恒留最新 3 份
PRESET_PAPER_COUNT = 4


def _ids(session: Session, table, **where) -> set[str]:
    stmt = select(table.c.id).where(table.c.course_id == "c1")
    for key, value in where.items():
        stmt = stmt.where(getattr(table.c, key) == value)
    return set(session.execute(stmt).scalars())


def _pv_ids(session: Session) -> set[str]:
    return _ids(session, paper_versions, exam_project_id="proj1")


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'paper_history.db'}")
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
                id="ep1", course_id="c1", framework_version_id="fv1", anchor_key="A1",
                code="EP1", title="考点1", assessment_requirement="掌握A", weight_value=30.0,
                weight_source="teacher_confirmed", weight_group_id="A1", priority="normal",
                cognitive_targets=[], assessment_orientations=[], allowed_question_types=[],
                operational_detail_policy="supporting_only", scope_boundary={},
                required_evidence_roles=[], retrieval_intent="围绕A检索",
                teaching_anchor_keys=[], status="active",
            ))
            s.execute(content_domains.insert().values(
                id="cd1", course_id="c1", catalog_version_id="cv1", parent_domain_id=None,
                level=1, framework_anchor_key="A1", code="A1", name="章1", status="active",
            ))
            s.execute(assessment_units.insert().values(
                id="au1", course_id="c1", catalog_version_id="cv1", content_domain_id="cd1",
                exam_point_id="ep1", code="U1", title="单元1", performance_statement="ps1",
                weight=30, status="active",
            ))
            s.execute(knowledge_cards.insert().values(
                id="kc1", course_id="c1", catalog_version_id="cv1", assessment_unit_id="au1",
                name="卡A1", performance_statement="掌握A1",
                assessable_content=["A1-原子1定义", "A1-原子2应用"],
                content_hash="hca1", status="active", concept_cluster="A",
                answer_proposition="A1-边界",
            ))
            # 指针先留空：exam_projects → paper_versions 是外键，卷还没建时不能指
            s.execute(exam_projects.insert().values(
                id="proj1", course_id="c1", name="Midterm", status="review",
            ))
            s.execute(blueprint_versions.insert().values(
                id="bv1", course_id="c1", exam_project_id="proj1",
                framework_version_id="fv1", catalog_version_id="cv1", version_no=1,
            ))
            s.execute(plan_items.insert().values(
                id="pi1", course_id="c1", blueprint_version_id="bv1",
                assessment_unit_id="au1", question_type="single_choice", item_index=1,
                score=2.0, difficulty="medium", cognitive_level="understand",
                exam_point_id="ep1", knowledge_card_id="kc1",
            ))
            # gr5 / gq5 先备好：建第 5 份卷的用例直接拿它走创建流程
            for n in range(1, 6):
                s.execute(generation_runs.insert().values(
                    id=f"gr{n}", course_id="c1", framework_version_id="fv1",
                    catalog_version_id="cv1", blueprint_version_id="bv1",
                    prompt_template_version="v1", run_type="full", status="completed",
                ))
                s.execute(generation_attempts.insert().values(
                    id=f"att{n}", course_id="c1", generation_run_id=f"gr{n}",
                    attempt_no=1, status="succeeded",
                ))
                s.execute(model_calls.insert().values(
                    id=f"mc{n}", course_id="c1", generation_attempt_id=f"att{n}",
                    stage="paper_generation", provider="test", model="stub",
                    status="succeeded", details={"response": {"stem": f"题面{n}"}},
                ))
                s.execute(generated_questions.insert().values(
                    id=f"gq{n}", course_id="c1", generation_run_id=f"gr{n}",
                    plan_item_id="pi1", knowledge_card_id="kc1", revision_no=1,
                    status="candidate", payload=dict(_PAYLOAD),
                ))
            for n in range(1, PRESET_PAPER_COUNT + 1):
                s.execute(paper_versions.insert().values(
                    id=f"pv{n}", course_id="c1", exam_project_id="proj1",
                    generation_run_id=f"gr{n}", version_no=n,
                    status="finalized" if n == 3 else "candidate",
                ))
                s.execute(paper_items.insert().values(
                    id=f"item{n}", course_id="c1", paper_version_id=f"pv{n}",
                    generated_question_id=f"gq{n}", display_order=1,
                ))
                s.execute(quality_checks.insert().values(
                    id=f"qc{n}", course_id="c1", generated_question_id=f"gq{n}",
                    check_type="schema", status="pass", details={},
                ))
            # 卷建齐后再把当前卷指针落到最新一份
            s.execute(exam_projects.update().where(exam_projects.c.id == "proj1")
                      .values(active_paper_version_id=f"pv{PRESET_PAPER_COUNT}"))
        yield s
    engine.dispose()


# ─── 保留策略：删得够深 ───


def test_prune_keeps_newest_three_and_purges_question_content(session):
    pruned = prune_paper_version_history(session, course_id="c1", project_id="proj1")
    session.commit()

    assert pruned == ["pv1"]
    # 4 份 → 留最新 3 份（pv4/pv3/pv2），最旧的 pv1 连内容一起消失
    assert _pv_ids(session) == {"pv4", "pv3", "pv2"}


def test_prune_removes_items_questions_checks_and_model_calls(session):
    prune_paper_version_history(session, course_id="c1", project_id="proj1")
    session.commit()

    # 只有 pv1 被删 → 它的题面、质检与模型响应一并消失
    assert _ids(session, paper_items) == {"item2", "item3", "item4"}
    assert _ids(session, generated_questions) == {"gq2", "gq3", "gq4", "gq5"}
    assert _ids(session, quality_checks) == {"qc2", "qc3", "qc4"}
    assert _ids(session, model_calls) == {"mc2", "mc3", "mc4", "mc5"}
    # 运行日志保留：只有状态与耗时，不含题面
    assert _ids(session, generation_runs) == {"gr1", "gr2", "gr3", "gr4", "gr5"}
    assert _ids(session, generation_attempts) == {"att1", "att2", "att3", "att4", "att5"}


def test_prune_is_noop_when_within_limit(session):
    # 已经只剩 3 份时再跑一次：返回空、库里什么都不动
    assert prune_paper_version_history(session, course_id="c1", project_id="proj1") == ["pv1"]
    session.commit()
    assert prune_paper_version_history(session, course_id="c1", project_id="proj1") == []
    session.commit()
    assert _pv_ids(session) == {"pv2", "pv3", "pv4"}
    assert "item2" in _ids(session, paper_items)
    assert "gq2" in _ids(session, generated_questions)


# ─── 豁免：指针不悬空、仍被引用的题不删 ───


def test_prune_repoints_active_pointer_when_it_would_be_pruned(session):
    session.execute(exam_projects.update().where(exam_projects.c.id == "proj1")
                    .values(active_paper_version_id="pv1"))
    session.commit()

    prune_paper_version_history(session, course_id="c1", project_id="proj1")
    session.commit()

    active = session.execute(
        select(exam_projects.c.active_paper_version_id).where(exam_projects.c.id == "proj1")
    ).scalar_one()
    # 指针落到最新保留卷（pv4），绝不指向已删行
    assert active == "pv4"


def test_prune_spares_question_still_referenced_by_kept_version(session):
    # pv3（保留）额外挂一道来自 gr1（将被删的那次生成）的题：题目必须活下来
    session.execute(paper_items.insert().values(
        id="item_shared", course_id="c1", paper_version_id="pv3",
        generated_question_id="gq1", display_order=2,
    ))
    session.commit()

    prune_paper_version_history(session, course_id="c1", project_id="proj1")
    session.commit()

    assert "pv1" not in _pv_ids(session)                # 旧卷实体已删
    assert "gq1" in _ids(session, generated_questions)  # 但仍被 pv3 挂着，题目保留
    assert "item_shared" in _ids(session, paper_items)
    # pv1 自己那条 paper_item 已随卷删除，pv3 两条都在
    rows = session.execute(
        select(paper_items.c.id).where(paper_items.c.paper_version_id == "pv3")
    ).scalars().all()
    assert set(rows) == {"item3", "item_shared"}


# ─── 生成新卷后自动修剪 ───


def test_new_paper_version_triggers_prune(session):
    pv_id = create_paper_version_from_generation(
        session,
        course_id="c1",
        project_id="proj1",
        generation_run_id="gr5",
        questions_list=[{"plan_item_id": "pi1", "quality": {"needs_review": False}}],
    )
    session.commit()

    assert pv_id  # 第 5 份卷建成
    # 5 份 → 只留最新 3 份（pv5/pv4/pv3），最旧两份连内容一起消失
    assert _pv_ids(session) == {pv_id, "pv4", "pv3"}
    assert "pv1" not in _ids(session, paper_items)
    assert "item1" not in _ids(session, paper_items)
    assert "gq1" not in _ids(session, generated_questions)
    assert "gq2" not in _ids(session, generated_questions)
    assert "mc1" not in _ids(session, model_calls)
    assert "qc1" not in _ids(session, quality_checks)
    # 运行日志仍在
    assert "gr1" in _ids(session, generation_runs)
    assert "att1" in _ids(session, generation_attempts)
    # 新卷即当前卷
    active = session.execute(
        select(exam_projects.c.active_paper_version_id).where(exam_projects.c.id == "proj1")
    ).scalar_one()
    assert active == pv_id


# ─── 历史列表 ───


def test_list_history_is_newest_first_and_marks_current(session):
    history = list_paper_versions(session, course_id="c1", project_id="proj1")

    assert [h["version_no"] for h in history] == [4, 3, 2]
    assert [h["id"] for h in history] == ["pv4", "pv3", "pv2"]
    assert history[0]["is_current"] is True
    assert all(not h["is_current"] for h in history[1:])
    assert history[0]["item_count"] == 1
    assert history[0]["status"] == "candidate"
    # 读取即执行保留策略：pv1 已被物理删除
    assert "pv1" not in _pv_ids(session)


def test_list_history_unknown_project_raises(session):
    with pytest.raises(PaperVersionError):
        list_paper_versions(session, course_id="c1", project_id="nope")


# ─── 切当前卷 ───


def test_activate_switches_pointer_without_touching_version(session):
    out = activate_paper_version(
        session, course_id="c1", project_id="proj1", paper_version_id="pv3",
    )

    active = session.execute(
        select(exam_projects.c.active_paper_version_id).where(exam_projects.c.id == "proj1")
    ).scalar_one()
    assert active == "pv3"
    assert out["id"] == "pv3"
    assert out["version_no"] == 3
    assert out["questions"][0]["stem"] == _PAYLOAD["stem"]
    # 已定稿的旧卷切回来仍是定稿：只动指针，冻结纪律不受影响
    assert out["status"] == "finalized"


def test_activate_rejects_version_from_another_project(session):
    with pytest.raises(PaperVersionError):
        activate_paper_version(
            session, course_id="c1", project_id="proj999", paper_version_id="pv3",
        )
    with pytest.raises(PaperVersionError):
        activate_paper_version(
            session, course_id="c1", project_id="proj1", paper_version_id="ghost",
        )
    session.rollback()
