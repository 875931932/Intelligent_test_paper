"""资料库「试卷」文件夹 P2c：快照编辑 → 存回试卷区 → 四件套导出。

三块行为各有各的不变量：

1. **编辑只动副本**：改名/改题/删题/重排都写 paper_archives.snapshot，原试卷
   （paper_versions / paper_items / generated_questions）一个字节都不碰，且
   档案列 item_count / total_score 必须与快照同口径（列表页只读档案列）。
2. **存回建真卷**：每题落一条 generated_questions（generation_run_id /
   plan_item_id 置空，题面进 payload），paper_items 照常引用——既有读写、导出、
   待审核链路无需特判；同时切 active 指针、项目回 review、走「只留 3 份」策略。
3. **导出同构**：归档快照能拼出与 get_paper_version 同构的 pv dict 直接喂渲染器，
   不回查 paper_versions（归档本就独立于版本表）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import Session

from app.db.schema import (
    Base,
    Course,
    User,
    exam_projects,
    generated_questions,
    paper_archives,
    paper_items,
    paper_versions,
)
from app.services.paper_archive_service import (
    PaperArchiveError,
    archive_paper_version,
    delete_archive_question,
    get_paper_archive,
    load_archive_export_paper,
    rename_paper_archive,
    reorder_archive_questions,
    restore_archive_to_paper,
    update_archive_question,
)
from app.services.paper_version_service import (
    export_answer_detail_json,
    get_paper_version,
)

_Q1 = {
    "item_index": 1,
    "stem": "关于梯度同步，下列说法正确的是？",
    "options": {"A": "需要通信", "B": "不需要"},
    "answer": "A",
    "explanation": "同步梯度需要通信。",
    "question_type": "single_choice",
    "difficulty": "medium",
    "score": 2.0,
}
_Q2 = {
    "item_index": 2,
    "stem": "简述参数服务器的作用。",
    "answer": "聚合并分发全局参数。",
    "rubric": "答出聚合与分发各得一半分。",
    "question_type": "short_answer",
    "difficulty": "hard",
    "score": 3.0,
}


def _snapshot() -> dict:
    return {
        "version_no": 1,
        "status": "candidate",
        "total_score": 5.0,
        "questions": [dict(_Q1), dict(_Q2)],
    }


@pytest.fixture
def session(tmp_path):
    """最小库：一个项目 + 一版卷 + 一份 2 题的归档快照。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'archive_restore.db'}")
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(User(id="u1", display_name="T1", role="teacher"))
        s.flush()
        s.add(Course(id="c1", owner_id="u1", slug="cs101", name="CS101"))
        s.commit()
        with s.begin():
            s.execute(exam_projects.insert().values(
                id="proj1", course_id="c1", name="Midterm", status="review",
            ))
            s.execute(paper_versions.insert().values(
                id="pv1", course_id="c1", exam_project_id="proj1",
                version_no=1, status="candidate",
            ))
            s.execute(exam_projects.update().where(exam_projects.c.id == "proj1")
                      .values(active_paper_version_id="pv1"))
            s.execute(paper_archives.insert().values(
                id="a1", course_id="c1", exam_project_id="proj1",
                source_paper_version_id="pv1", source_version_no=1,
                name="期末归档", item_count=2, total_score=5.0,
                snapshot=_snapshot(), created_by="u1",
            ))
        yield s
    engine.dispose()


# ─── 存回试卷区 ──────────────────────────────────────────────────────


def test_restore_builds_version_with_planless_questions(session):
    out = restore_archive_to_paper(session, course_id="c1", archive_id="a1", created_by="u1")

    # 新版追加（v1 之上 → v2），candidate，元数据记下来源
    assert out["version_no"] == 2
    assert out["status"] == "candidate"
    assert out["metadata"]["created_from"] == "archive_restore"
    assert out["metadata"]["archive_id"] == "a1"
    assert out["generation_run_id"] is None
    assert out["total_score"] == 5.0

    # 题目按 display_order 1..2 落库，题面取自快照
    assert [q["item_index"] for q in out["questions"]] == [1, 2]
    assert [q["stem"] for q in out["questions"]] == [_Q1["stem"], _Q2["stem"]]
    assert [q["score"] for q in out["questions"]] == [2.0, 3.0]
    assert out["questions"][1]["rubric"] == _Q2["rubric"]
    # 存回的题没有蓝图槽位：plan_item_id 为空，分值退回 payload
    assert out["questions"][0]["plan_item_id"] is None

    # generated_questions 真实建行，两列置空（R8：既有 join 无需特判）
    gq_rows = session.execute(
        select(generated_questions).where(generated_questions.c.course_id == "c1")
    ).all()
    assert len(gq_rows) == 2
    for row in gq_rows:
        gq = row._mapping if hasattr(row, "_mapping") else row
        assert gq["generation_run_id"] is None
        assert gq["plan_item_id"] is None
        assert gq["payload"]["stem"] in (_Q1["stem"], _Q2["stem"])
        # 旧题号随删题/重排失真，payload 不带它——题号以 display_order 为准
        assert "item_index" not in gq["payload"]

    # 项目：切 active 指针 + 回待审核
    proj = session.execute(
        select(exam_projects.c.status, exam_projects.c.active_paper_version_id)
        .where(exam_projects.c.id == "proj1")
    ).one()
    assert proj._mapping["active_paper_version_id"] == out["id"]
    assert proj._mapping["status"] == "review"

    # 归档本身不动（它是独立副本）
    detail = get_paper_archive(session, course_id="c1", archive_id="a1")
    assert detail["item_count"] == 2
    assert detail["snapshot"]["version_no"] == 1


def test_restore_prunes_old_versions_but_archive_survives(session):
    """存回走「只留 3 份」：更早版本连题一起被物理删，归档毫发无损。"""
    with session.begin():
        for n in (2, 3):
            session.execute(paper_versions.insert().values(
                id=f"pv{n}", course_id="c1", exam_project_id="proj1",
                version_no=n, status="candidate",
            ))
        session.execute(exam_projects.update().where(exam_projects.c.id == "proj1")
                        .values(active_paper_version_id="pv3"))

    out = restore_archive_to_paper(session, course_id="c1", archive_id="a1")
    assert out["version_no"] == 4

    kept = set(session.execute(
        select(paper_versions.c.id).where(paper_versions.c.exam_project_id == "proj1")
    ).scalars())
    assert kept == {"pv2", "pv3", out["id"]}       # pv1 被修剪掉
    assert session.execute(
        select(paper_archives.c.id).where(paper_archives.c.id == "a1")
    ).one_or_none() is not None
    # 再存回一次仍然是追加语义，不会复用被删的版本号
    assert restore_archive_to_paper(session, course_id="c1", archive_id="a1")["version_no"] == 5


def test_restore_rejects_empty_snapshot_and_cross_course(session):
    with session.begin():
        session.execute(paper_archives.insert().values(
            id="a_empty", course_id="c1", exam_project_id="proj1",
            name="空卷", item_count=0, total_score=0.0,
            snapshot={"version_no": 1, "status": "candidate", "total_score": 0.0, "questions": []},
        ))
    with pytest.raises(PaperArchiveError, match="没有题目"):
        restore_archive_to_paper(session, course_id="c1", archive_id="a_empty")
    session.rollback()

    # 跨课程存回（课程隔离）：照旧查不到这份归档
    with pytest.raises(PaperArchiveError, match="不存在"):
        restore_archive_to_paper(session, course_id="c2", archive_id="a1")
    session.rollback()
    # 上面两次拒绝都没动过卷：项目仍指向 v1，没有新版本
    assert session.execute(
        select(exam_projects.c.active_paper_version_id).where(exam_projects.c.id == "proj1")
    ).scalar_one() == "pv1"
    assert session.execute(
        select(paper_versions.c.id).where(paper_versions.c.exam_project_id == "proj1")
    ).scalars().all() == ["pv1"]


# ─── 快照编辑 ────────────────────────────────────────────────────────


def test_rename_updates_name_and_rejects_blank(session):
    out = rename_paper_archive(session, course_id="c1", archive_id="a1", name="  期末卷·定稿  ")
    assert out["name"] == "期末卷·定稿"

    with pytest.raises(PaperArchiveError, match="不能为空"):
        rename_paper_archive(session, course_id="c1", archive_id="a1", name="   ")
    with pytest.raises(PaperArchiveError, match="不存在"):
        rename_paper_archive(session, course_id="c1", archive_id="nope", name="x")
    session.rollback()


def test_patch_question_merges_and_syncs_archive_columns(session):
    out = update_archive_question(
        session, course_id="c1", archive_id="a1", item_index=2,
        patch={"answer": "聚合参数并分发给工作节点。", "score": 4.0},
    )

    q = next(q for q in out["questions"] if q["item_index"] == 2)
    assert q["answer"] == "聚合参数并分发给工作节点。"
    assert q["score"] == 4.0
    # 其余字段原样保留（浅合并）
    assert q["stem"] == _Q2["stem"]
    assert q["rubric"] == _Q2["rubric"]
    # 档案列与快照同口径：总分 2 + 4 = 6，题数不变
    assert out["total_score"] == 6.0
    assert out["item_count"] == 2
    # 改过的题打上「已修改」（快照题即成品，等价于试卷页的 teacher_override）
    assert out["questions"][1]["has_override"] is True
    # 只动副本：原试卷的卷面分值不受影响
    assert out["source_paper_version_id"] == "pv1"


def test_patch_question_can_clear_needs_review_flag(session):
    """编辑器勾选「已处理完毕」要真的把快照里的待审核标记清掉，且不写进题面。"""
    snap = _snapshot()
    snap["questions"][1]["needs_review"] = True
    snap["questions"][1]["needs_review_reason"] = "答案疑似缺失"
    snap["questions"][0]["needs_review"] = True
    snap["questions"][0]["needs_review_reason"] = "选项与答案不一致"
    session.execute(
        update(paper_archives).where(paper_archives.c.id == "a1").values(snapshot=snap)
    )
    session.commit()

    out = update_archive_question(
        session, course_id="c1", archive_id="a1", item_index=2,
        patch={"answer": "补充后的答案", "clear_needs_review": True},
    )
    q2 = out["questions"][1]
    assert q2["needs_review"] is False
    assert q2["needs_review_reason"] is None
    assert q2["has_override"] is True
    assert "clear_needs_review" not in q2      # 开关不是题面字段，不得留在快照里

    # 没勾选：标记原样保留，另一道题的改动也不串扰
    out2 = update_archive_question(
        session, course_id="c1", archive_id="a1", item_index=1, patch={"score": 4.0},
    )
    assert out2["questions"][0]["has_override"] is True
    assert out2["questions"][0]["needs_review_reason"] == "选项与答案不一致"
    assert out2["questions"][1]["needs_review_reason"] is None


def test_patch_question_rejects_blank_or_answer_off_options(session):
    # 答案空 → 会把空白答卷导出去，与试卷页同口径拒绝
    with pytest.raises(PaperArchiveError):
        update_archive_question(
            session, course_id="c1", archive_id="a1", item_index=1, patch={"answer": ""},
        )
    # 题干空同样拒绝
    with pytest.raises(PaperArchiveError):
        update_archive_question(
            session, course_id="c1", archive_id="a1", item_index=1, patch={"stem": "   "},
        )
    # 多选答案必须落在选项上（单选只查非空）
    with pytest.raises(PaperArchiveError):
        update_archive_question(
            session, course_id="c1", archive_id="a1", item_index=1,
            patch={"question_type": "multiple_choice", "answer": "Z"},
        )
    with pytest.raises(PaperArchiveError, match="不在该归档中"):
        update_archive_question(
            session, course_id="c1", archive_id="a1", item_index=9, patch={"stem": "x"},
        )
    with pytest.raises(PaperArchiveError, match="补丁不能为空"):
        update_archive_question(
            session, course_id="c1", archive_id="a1", item_index=1, patch={},
        )
    session.rollback()
    # 被拒的补丁不留痕
    assert get_paper_archive(session, course_id="c1", archive_id="a1")["questions"][0]["answer"] == "A"


def test_delete_question_renumbers_and_syncs(session):
    out = delete_archive_question(session, course_id="c1", archive_id="a1", item_index=1)

    assert [q["item_index"] for q in out["questions"]] == [1]
    assert out["questions"][0]["stem"] == _Q2["stem"]     # 剩下的题前移一号
    assert out["item_count"] == 1
    assert out["total_score"] == 3.0

    with pytest.raises(PaperArchiveError, match="不在该归档中"):
        delete_archive_question(session, course_id="c1", archive_id="a1", item_index=5)
    session.rollback()


def test_reorder_renumbers_and_requires_exact_cover(session):
    out = reorder_archive_questions(
        session, course_id="c1", archive_id="a1", ordered_indices=[2, 1],
    )
    assert [q["item_index"] for q in out["questions"]] == [1, 2]
    assert out["questions"][0]["stem"] == _Q2["stem"]     # 原第 2 题排到最前
    # 顺序换了，题号仍从 1 连续编号
    assert [q["score"] for q in out["questions"]] == [3.0, 2.0]

    for bad in ([1, 1], [1], [1, 3]):
        with pytest.raises(PaperArchiveError, match="ordered_indices"):
            reorder_archive_questions(
                session, course_id="c1", archive_id="a1", ordered_indices=bad
            )
    session.rollback()


# ─── 导出：快照 → 同构 pv dict ──────────────────────────────────────


def test_export_paper_is_isomorphic_to_get_paper_version(session):
    # 先把 pv1 装上一道真题，好拿真卷的题目键集合做对照
    with session.begin():
        session.execute(generated_questions.insert().values(
            id="gq1", course_id="c1", generation_run_id=None, plan_item_id=None,
            revision_no=1, status="candidate", payload=dict(_Q1),
        ))
        session.execute(paper_items.insert().values(
            id="item1", course_id="c1", paper_version_id="pv1",
            generated_question_id="gq1", display_order=1,
        ))
    real = get_paper_version(session, "pv1", course_id="c1")
    archived = archive_paper_version(
        session, course_id="c1", project_id="proj1", name="对照卷",
    )

    pv = load_archive_export_paper(session, course_id="c1", archive_id=archived["id"])

    # 渲染器只认这几个键（_paper_meta / _section_groups 同口径）
    assert pv["exam_project_id"] == "proj1"
    assert pv["project_name"] == "Midterm"
    assert pv["version_no"] == real["version_no"]
    assert pv["status"] == real["status"]
    # 快照的总分来自真卷（按题解析后求和），不是档案列抄来的
    assert pv["total_score"] == real["total_score"] == 2.0
    assert len(pv["questions"]) == len(real["questions"]) == 1
    # 与真卷同构：同一套题面键，导出链路无需分支
    assert set(pv["questions"][0]) == set(real["questions"][0])
    assert pv["questions"][0]["stem"] == real["questions"][0]["stem"]

    # 归档 id 不在 paper_versions 里——导出全靠预加载的 pv，不回查版本表
    assert session.execute(
        select(paper_versions.c.id).where(paper_versions.c.id == archived["id"])
    ).one_or_none() is None


def test_export_answer_detail_json_renders_from_snapshot_alone(session):
    """归档不存在于 paper_versions：JSON 必须靠预加载的 pv 出，不回查版本表。"""
    pv = load_archive_export_paper(session, course_id="c1", archive_id="a1")
    data = export_answer_detail_json(session, "a1", course_id="c1", pv=pv)

    assert data["version_no"] == 1
    assert data["paper_version_id"] == "a1"
    assert data["total_questions"] == 2
    assert [q["item_index"] for q in data["questions"]] == [1, 2]
    stems = [q["stem"] for q in data["questions"]]
    assert _Q1["stem"] in stems and _Q2["stem"] in stems
    # 评分细则随卷下发（阅卷端直接输入）
    assert data["questions"][1]["rubric"] == _Q2["rubric"]
