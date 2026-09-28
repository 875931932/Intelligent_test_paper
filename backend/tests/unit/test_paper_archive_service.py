"""资料库「试卷」文件夹：快照归档（存/列/详/删）。

最关键的一条：**源卷被保留策略物理删除后，归档必须还在**——归档与
paper_versions 无外键牵连，存进去的是题目解析后的完整快照。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, func, select, update
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
    generation_runs,
    knowledge_cards,
    knowledge_catalog_versions,
    paper_archives,
    paper_items,
    paper_versions,
    plan_items,
)
from app.services.paper_archive_service import (
    PaperArchiveError,
    archive_paper_version,
    delete_paper_archive,
    get_paper_archive,
    list_paper_archives,
)
from app.services.paper_version_service import prune_paper_version_history

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


def _archive_ids(session: Session) -> set[str]:
    return set(
        session.execute(
            select(paper_archives.c.id).where(paper_archives.c.course_id == "c1")
        ).scalars()
    )


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'paper_archive.db'}")
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
            s.execute(exam_projects.insert().values(
                id="proj1", course_id="c1", name="Midterm", status="review",
            ))
            s.execute(exam_projects.insert().values(
                id="proj2", course_id="c1", name="另一项目", status="draft",
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
            s.execute(generation_runs.insert().values(
                id="gr1", course_id="c1", framework_version_id="fv1",
                catalog_version_id="cv1", blueprint_version_id="bv1",
                prompt_template_version="v1", run_type="full", status="completed",
            ))
            s.execute(generated_questions.insert().values(
                id="gq1", course_id="c1", generation_run_id="gr1", plan_item_id="pi1",
                knowledge_card_id="kc1", revision_no=1, status="candidate",
                payload=dict(_PAYLOAD),
            ))
            s.execute(paper_versions.insert().values(
                id="pv1", course_id="c1", exam_project_id="proj1",
                generation_run_id="gr1", version_no=1, status="candidate",
            ))
            s.execute(paper_items.insert().values(
                id="item1", course_id="c1", paper_version_id="pv1",
                generated_question_id="gq1", display_order=1,
            ))
            # 另一个项目自己的卷（无生成 run、无题目），供列表/跨项目用例
            s.execute(paper_versions.insert().values(
                id="pv_other", course_id="c1", exam_project_id="proj2",
                version_no=1, status="candidate",
            ))
            s.execute(exam_projects.update().where(exam_projects.c.id == "proj1")
                      .values(active_paper_version_id="pv1"))
        yield s
    engine.dispose()


def test_archive_captures_resolved_snapshot(session):
    out = archive_paper_version(
        session, course_id="c1", project_id="proj1", created_by="u1",
    )

    assert out["id"] in _archive_ids(session)
    assert out["name"] == "Midterm v1"          # 不给名字用「项目名 v几」
    assert out["item_count"] == 1
    assert out["total_score"] == 2.0
    assert out["source_version_no"] == 1
    assert out["source_paper_version_id"] == "pv1"
    assert out["created_by"] == "u1"
    # 快照里是解析后的题目（题干/答案/分值都在，够后面编辑与导出用）
    q = out["questions"][0]
    assert q["stem"] == _PAYLOAD["stem"]
    assert q["answer"] == "A"
    assert q["score"] == 2.0
    assert q["has_override"] is False


def test_archive_accepts_explicit_source_and_name(session):
    out = archive_paper_version(
        session, course_id="c1", project_id="proj1",
        source_paper_version_id="pv1", name="  期末归档卷  ",
    )
    assert out["name"] == "期末归档卷"
    assert out["source_paper_version_id"] == "pv1"


def test_archive_rejects_version_from_another_project(session):
    with pytest.raises(PaperArchiveError, match="不属于该项目"):
        archive_paper_version(
            session, course_id="c1", project_id="proj2",
            source_paper_version_id="pv1",
        )
    with pytest.raises(PaperArchiveError):
        archive_paper_version(session, course_id="c1", project_id="nope")
    session.rollback()


def test_archive_survives_source_version_pruning(session):
    """核心不变量：源卷被保留策略物理删除后，归档仍在且题目完整。"""
    # 造出第 2~4 份卷（无题目），让 pv1 成为最旧的一份
    with session.begin():
        for n in (2, 3, 4):
            session.execute(paper_versions.insert().values(
                id=f"pv{n}", course_id="c1", exam_project_id="proj1",
                version_no=n, status="candidate",
            ))
        session.execute(exam_projects.update().where(exam_projects.c.id == "proj1")
                        .values(active_paper_version_id="pv4"))

    archived = archive_paper_version(
        session, course_id="c1", project_id="proj1", source_paper_version_id="pv1",
    )

    pruned = prune_paper_version_history(session, course_id="c1", project_id="proj1")
    session.commit()
    assert pruned == ["pv1"]

    # 源卷连题面一起没了……
    assert session.execute(
        select(paper_versions.c.id).where(paper_versions.c.id == "pv1")
    ).one_or_none() is None
    assert session.execute(
        select(paper_items.c.id).where(paper_items.c.paper_version_id == "pv1")
    ).one_or_none() is None
    assert session.execute(
        select(generated_questions.c.id).where(generated_questions.c.id == "gq1")
    ).one_or_none() is None
    # ……归档还活着，题目内容一字不少
    assert archived["id"] in _archive_ids(session)
    detail = get_paper_archive(
        session, course_id="c1", archive_id=archived["id"],
    )
    assert detail["item_count"] == 1
    assert detail["questions"][0]["stem"] == _PAYLOAD["stem"]
    assert detail["snapshot"]["version_no"] == 1


def test_list_archives_is_newest_first_with_project_name(session):
    first = archive_paper_version(session, course_id="c1", project_id="proj1", name="卷一")
    second = archive_paper_version(session, course_id="c1", project_id="proj2", name="卷二")
    # SQLite 的 CURRENT_TIMESTAMP 粒度是秒，两笔归档同秒时排序没有意义，
    # 显式把第一条设早一分钟，确定性地验证「新 → 旧」
    session.execute(
        update(paper_archives)
        .where(paper_archives.c.id == first["id"])
        .values(created_at=func.datetime("now", "-60 seconds"))
    )
    session.commit()

    listed = list_paper_archives(session, course_id="c1")
    assert [r["id"] for r in listed] == [second["id"], first["id"]]
    assert [r["name"] for r in listed] == ["卷二", "卷一"]
    assert [r["project_name"] for r in listed] == ["另一项目", "Midterm"]
    # 列表只带档案字段，不塞题目内容
    assert all("snapshot" not in r and "questions" not in r for r in listed)


def test_delete_archive_removes_row_and_detail_404(session):
    out = archive_paper_version(session, course_id="c1", project_id="proj1", name="待删")
    delete_paper_archive(session, course_id="c1", archive_id=out["id"])

    assert _archive_ids(session) == set()
    with pytest.raises(PaperArchiveError, match="不存在"):
        get_paper_archive(session, course_id="c1", archive_id=out["id"])
    with pytest.raises(PaperArchiveError, match="不存在"):
        delete_paper_archive(session, course_id="c1", archive_id=out["id"])
    session.rollback()
