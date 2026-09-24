"""整卷 AI 质量评审服务单元测试。

镜像 tests/unit/test_contract_explain_service.py 的组织方式，试卷链路播种照抄
tests/unit/test_ai_create_service.py 的已验证 fixture。覆盖：上下文装配（题目
全量 / needs_review 清单 / 合同终检 checks / 蓝图配额、缺卷与空卷拒绝、finalized
放行、终检降级）、prompt 装配（真实数据进 payload + 「禁止学生答卷评分」与
「带 item_index」硬规则）、结果规整与校验收口、带反馈的一次纠错重试、任务入队
幂等。LLM 用同接口桩注入，不发真实请求。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, select, update
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
    blueprint_versions,
)
from app.services.paper_review_service import (
    TASK_TYPE,
    PaperReviewError,
    build_review_prompt,
    enqueue_review,
    load_review_context,
    normalize_result,
    run_review,
    validate_result,
)

_PAYLOAD = {
    "stem": "关于分布式训练中的梯度同步，下列说法正确的是？",
    "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
    "answer": "A",
    "explanation": "同步梯度需要通信，因此引入开销。",
    "question_type": "single_choice",
    "difficulty": "medium",
    "cognitive_level": "understand",
    "score": 2.0,
    "exam_point_id": "ep1",
    # 合同盖章的溯源字段（generation_runner 持久化口径），终检要读它们
    "unit_id": "au1",
    "card_id": "kc1",
    "coverage_atom": "A1-原子1定义",
    "answer_boundary": "A1-边界",
}

# 合同快照槽位（ContractSlot 必填字段全集），与 _PAYLOAD 的溯源字段同口径
_SLOT = {
    "item_index": 1,
    "question_type": "single_choice",
    "score": 2.0,
    "difficulty": "medium",
    "cognitive_level": "understand",
    "exam_point_id": "ep1",
    "anchor_key": "A1",
    "unit_id": "au1",
    "card_id": "kc1",
    "coverage_atom": "A1-原子1定义",
    "answer_boundary": "A1-边界",
}


class StubClient:
    """LLMJsonClient 同接口桩：按序返回预置响应并记录每次调用。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def request_json(self, *, system_prompt, payload, temperature, call_context, **kwargs):
        self.calls.append({"system_prompt": system_prompt, "payload": payload})
        return self._responses.pop(0)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'paper_review.db'}")
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
                assessable_content=["A1-原子1定义"], content_hash="hca1",
                status="active", concept_cluster="A", answer_proposition="A1-边界",
            ))
            s.execute(exam_projects.insert().values(
                id="proj1", course_id="c1", name="Midterm", status="draft",
            ))
            s.execute(blueprint_versions.insert().values(
                id="bv1", course_id="c1", exam_project_id="proj1",
                framework_version_id="fv1", catalog_version_id="cv1", version_no=1,
            ))
            s.execute(generation_runs.insert().values(
                id="gr1", course_id="c1", framework_version_id="fv1",
                catalog_version_id="cv1", blueprint_version_id="bv1",
                prompt_template_version="v1", run_type="full", status="completed",
                contract_snapshot={"total_score": 2.0, "slots": [dict(_SLOT)]},
            ))
            s.execute(plan_items.insert().values(
                id="pi1", course_id="c1", blueprint_version_id="bv1",
                assessment_unit_id="au1", question_type="single_choice", item_index=1,
                score=2.0, difficulty="medium", cognitive_level="understand",
                exam_point_id="ep1", knowledge_card_id="kc1",
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
        yield s
    engine.dispose()


def _ctx(session, *, instruction: str = "") -> dict:
    return load_review_context(
        session, course_id="c1", paper_version_id="pv1", instruction=instruction
    )


# ─── 上下文装配 ───


def test_load_context_includes_full_items_and_grounding(session):
    ctx = _ctx(session)
    # 题目全量：真实生效题面（override 优先口径）带题号/题型/难度/分值/答案/解析
    assert ctx["paper"]["item_count"] == 1
    assert ctx["paper"]["status"] == "candidate"
    assert len(ctx["items"]) == 1
    item = ctx["items"][0]
    assert item["item_index"] == 1
    assert item["question_type"] == "single_choice"
    assert item["stem"] == _PAYLOAD["stem"]
    assert item["answer"] == "A"
    assert item["explanation"] == _PAYLOAD["explanation"]
    assert item["difficulty"] == "medium"
    assert item["score"] == 2.0
    assert item["needs_review"] is False

    # 合同终检：调用既有 audit_paper_against_contract 的真实输出，不是伪造。
    # 本用例未标 needs_review，终检如实通过（needs_review count = 0）
    final_check = ctx["final_check"]
    assert final_check is not None
    codes = {c["code"] for c in final_check["checks"]}
    assert {"quota_match", "atom_uniqueness", "answer_mutex", "traceability",
            "needs_review", "backfill_within_chapter"} <= codes
    by_code = {c["code"]: c for c in final_check["checks"]}
    assert by_code["needs_review"]["passed"] is True
    assert by_code["needs_review"]["detail"]["count"] == 0
    assert by_code["traceability"]["passed"] is True

    # 蓝图配额（plan）与卷面实际计数（actual）都是确定性数据
    assert ctx["quotas"]["plan"]["question_type"] == {"single_choice": 1}
    assert ctx["quotas"]["plan"]["difficulty"] == {"medium": 1}
    assert ctx["quotas"]["actual"]["question_type"] == {"single_choice": 1}
    assert ctx["quotas"]["actual"]["difficulty"] == {"medium": 1}


def test_load_context_includes_needs_review_list(session):
    session.execute(
        update(paper_items)
        .where(paper_items.c.id == "item1")
        .values(needs_review=True, needs_review_reason="答案未命中答案域")
    )
    session.commit()
    ctx = _ctx(session)
    assert len(ctx["needs_review"]) == 1
    assert ctx["needs_review"][0]["item_index"] == 1
    assert ctx["needs_review"][0]["needs_review_reason"] == "答案未命中答案域"
    # 题目清单里的同步标记与 needs_review 清单同源
    assert ctx["items"][0]["needs_review"] is True


def test_load_context_degrades_final_check_without_snapshot(session):
    session.execute(
        update(generation_runs).where(generation_runs.c.id == "gr1")
        .values(contract_snapshot={})
    )
    session.commit()
    ctx = _ctx(session)
    assert ctx["final_check"] is None  # 如实降级，不伪造终检结果


def test_load_context_rejects_missing_paper(session):
    with pytest.raises(PaperReviewError, match="不存在"):
        load_review_context(
            session, course_id="c1", paper_version_id="pv-x", instruction=""
        )


def test_load_context_rejects_foreign_course(session):
    with pytest.raises(PaperReviewError, match="不存在"):
        load_review_context(
            session, course_id="other-course", paper_version_id="pv1", instruction=""
        )


def test_load_context_rejects_empty_paper(session):
    session.execute(paper_versions.insert().values(
        id="pv-empty", course_id="c1", exam_project_id="proj1", version_no=9, status="draft",
    ))
    session.commit()
    with pytest.raises(PaperReviewError, match="没有题目"):
        load_review_context(
            session, course_id="c1", paper_version_id="pv-empty", instruction=""
        )


def test_load_context_allows_finalized_paper(session):
    # 只读报告无状态禁令：定稿卷照样可评审（这正是它的价值）
    session.execute(
        update(paper_versions).where(paper_versions.c.id == "pv1").values(status="finalized")
    )
    session.commit()
    ctx = _ctx(session)
    assert ctx["paper"]["status"] == "finalized"


def test_load_context_quotas_plan_none_without_generation_run(session):
    # 无 generation_run 的试卷（教师纯手建链路）：plan 配额与终检都拿不到，跳过不硬凑
    session.execute(paper_versions.insert().values(
        id="pv2", course_id="c1", exam_project_id="proj1", version_no=2, status="candidate",
    ))
    session.execute(paper_items.insert().values(
        id="item2", course_id="c1", paper_version_id="pv2",
        generated_question_id="gq1", display_order=1,
    ))
    session.commit()
    ctx = load_review_context(
        session, course_id="c1", paper_version_id="pv2", instruction=""
    )
    assert ctx["final_check"] is None
    assert ctx["quotas"]["plan"] is None
    assert ctx["quotas"]["actual"]["question_type"] == {"single_choice": 1}


# ─── prompt 装配 ───


def test_build_prompt_grounded_in_real_data(session):
    session.execute(
        update(paper_items)
        .where(paper_items.c.id == "item1")
        .values(needs_review=True, needs_review_reason="缺解析")
    )
    session.commit()
    ctx = _ctx(session, instruction="重点关注难度分布")
    system, payload = build_review_prompt(ctx, "重点关注难度分布")

    assert payload["instruction"] == "重点关注难度分布"
    # 真实题目全量进 prompt
    assert payload["items"][0]["stem"] == _PAYLOAD["stem"]
    assert payload["items"][0]["answer"] == "A"
    assert payload["items"][0]["item_index"] == 1
    # needs_review 清单与终检信号（真实 checks）进 prompt
    assert payload["needs_review"][0]["item_index"] == 1
    assert payload["final_check"]["passed"] is False  # 第 1 题标了 needs_review，终检如实不过
    assert isinstance(payload["final_check"]["checks"], list)
    # 蓝图/卷面配额计数进 prompt（模型只解读不重算）
    assert payload["quotas"]["plan"]["difficulty"] == {"medium": 1}
    assert payload["quotas"]["actual"]["difficulty"] == {"medium": 1}

    # 系统提示词硬规则：禁学生答卷评分 / 带 item_index / 不编造 / 不绕过约束 / 5 维枚举
    assert "禁止学生答卷评分" in system
    assert "item_index" in system
    assert "不得编造" in system
    assert "绕过" in system
    for dimension in ("难度分布", "题面表述", "答案与解析一致性", "覆盖与配额", "风险题"):
        assert dimension in system
    assert "previous_validation_error" not in payload


def test_build_prompt_flags_final_check_unavailable(session):
    session.execute(
        update(generation_runs).where(generation_runs.c.id == "gr1")
        .values(contract_snapshot={})
    )
    session.commit()
    ctx = _ctx(session)
    _, payload = build_review_prompt(ctx, "")
    assert payload["final_check"] is None
    # 提示词要求模型对 null 如实标注「终检不可用」而非假装通过
    assert "合同终检不可用" in build_review_prompt(ctx, "")[0]


def test_build_prompt_carries_previous_error_on_retry(session):
    ctx = _ctx(session)
    _, payload = build_review_prompt(ctx, "", previous_error="总评为空或不足 10 字")
    assert payload["previous_validation_error"] == "总评为空或不足 10 字"


# ─── 结果规整 ───


def _context_stub() -> dict:
    return {"items": [{"item_index": 1}, {"item_index": 2}]}


def test_normalize_rejects_non_object():
    with pytest.raises(PaperReviewError):
        normalize_result(["not", "an", "object"], _context_stub())


def test_normalize_converges_malformed_raw():
    out = normalize_result(
        {"verdict": 42, "summary": 999, "sections": "oops"},
        _context_stub(),
    )
    assert out["verdict"] == "attention"  # 非法 verdict 收敛
    assert out["summary"] == "999"
    assert out["sections"] == []  # 非 list 置空


def test_normalize_drops_invalid_dimensions_and_entries():
    out = normalize_result(
        {
            "verdict": "pass",
            "summary": "整卷未发现实质问题，配额与难度均符合蓝图计划。",
            "sections": [
                "字符串项被丢弃",
                {"dimension": "学生得分评估", "severity": "info",
                 "finding": "维度非法被丢弃", "suggestion": "x", "item_indexes": []},
                {"dimension": "难度分布", "severity": "瞎写", "finding": "难度正常",
                 "suggestion": "无需调整", "item_indexes": []},
                {"dimension": "风险题", "severity": "warn", "finding": "第 1 题表述冗长",
                 "suggestion": "手动编辑精简题干", "item_indexes": [1]},
            ],
        },
        _context_stub(),
    )
    assert [s["dimension"] for s in out["sections"]] == ["难度分布", "风险题"]
    assert out["sections"][0]["severity"] == "warn"  # 非法 severity 收敛
    assert out["sections"][1]["item_indexes"] == [1]


def test_normalize_filters_out_of_range_item_indexes():
    out = normalize_result(
        {
            "verdict": "attention",
            "summary": "存在需要处理的问题。",
            "sections": [
                {"dimension": "风险题", "severity": "warn", "finding": "f",
                 "suggestion": "s", "item_indexes": [1, 999, True, "2", 1]},
                {"dimension": "覆盖与配额", "severity": "info", "finding": "f",
                 "suggestion": "s", "item_indexes": [77, 88]},  # 全部越界 → 置 []
            ],
        },
        _context_stub(),
    )
    assert out["sections"][0]["item_indexes"] == [1]  # 越界/布尔/字符串剔除且去重
    assert out["sections"][1]["item_indexes"] == []


# ─── 校验收口 ───


def _good_result() -> dict:
    return {
        "verdict": "pass",
        "summary": "整卷难度与配额和蓝图一致，题面与答案解析未发现实质问题。",
        "sections": [
            {"dimension": "难度分布", "severity": "info",
             "finding": "难度计数与蓝图一致", "suggestion": "无需调整", "item_indexes": []},
        ],
    }


def test_validate_blocks_empty_summary():
    result = validate_result({**_good_result(), "summary": ""})
    assert result["passed"] is False
    assert result["code"] == "summary_too_short"


def test_validate_blocks_short_summary():
    result = validate_result({**_good_result(), "summary": "还行"})
    assert result["passed"] is False
    assert result["code"] == "summary_too_short"


def test_validate_blocks_empty_sections():
    result = validate_result({**_good_result(), "sections": []})
    assert result["passed"] is False
    assert result["code"] == "sections_empty"


def test_validate_blocks_section_without_finding():
    good = _good_result()
    good["sections"] = [dict(good["sections"][0], finding="  ")]
    result = validate_result(good)
    assert result["passed"] is False
    assert result["code"] == "finding_empty"


def test_validate_blocks_invalid_verdict():
    result = validate_result({**_good_result(), "verdict": "ok"})
    assert result["passed"] is False
    assert result["code"] == "verdict_invalid"


def test_validate_passes_good_result():
    result = validate_result(_good_result())
    assert result["passed"] is True


# ─── 评审执行（桩客户端）───


def _raw_good() -> dict:
    return {
        "verdict": "pass",
        "summary": "整卷难度与配额和蓝图计划一致，题面表述与答案解析未发现实质问题，可以进入定稿流程。",
        "sections": [
            {"dimension": "难度分布", "severity": "info",
             "finding": "卷面难度 medium 1 题，与蓝图计划一致。",
             "suggestion": "保持现状，无需调整。", "item_indexes": []},
            {"dimension": "答案与解析一致性", "severity": "info",
             "finding": "第 1 题答案 A 与解析一致。",
             "suggestion": "无需处理。", "item_indexes": [1]},
        ],
    }


def _raw_bad() -> dict:
    return {"verdict": "ok", "summary": "短", "sections": []}


def test_run_review_retries_with_validation_feedback(session):
    client = StubClient([_raw_bad(), _raw_good()])
    result = run_review(
        session, course_id="c1", paper_version_id="pv1",
        instruction="重点看难度", client=client,
    )
    assert len(client.calls) == 2
    assert "previous_validation_error" in client.calls[1]["payload"]
    # 首次调用的 payload 必须带真实题目清单、needs_review、终检与配额
    first = client.calls[0]["payload"]
    assert first["items"][0]["item_index"] == 1
    assert first["items"][0]["stem"] == _PAYLOAD["stem"]
    assert isinstance(first["final_check"]["checks"], list)
    assert first["quotas"]["actual"]["difficulty"] == {"medium": 1}
    assert first["instruction"] == "重点看难度"
    # 结果载荷形状与 spec 一致
    assert set(result.keys()) == {
        "paper_version_id", "instruction", "verdict", "summary",
        "sections", "deterministic", "validated",
    }
    assert result["validated"] is True
    assert result["verdict"] == "pass"
    assert result["paper_version_id"] == "pv1"
    assert result["instruction"] == "重点看难度"
    # 终检可用（本用例未标 needs_review，终检整体通过）
    assert result["deterministic"] == {
        "needs_review_count": 0,
        "final_check_available": True,
    }
    assert client.calls[0]["payload"]["final_check"]["passed"] is True
    assert result["sections"][1]["item_indexes"] == [1]


def test_run_review_reports_failure_when_retry_still_blocks(session):
    client = StubClient([_raw_bad(), _raw_bad()])
    result = run_review(
        session, course_id="c1", paper_version_id="pv1",
        instruction="", client=client,
    )
    assert len(client.calls) == 2
    assert result["validated"] is False


def test_run_review_single_shot_when_first_passes(session):
    client = StubClient([_raw_good()])
    result = run_review(
        session, course_id="c1", paper_version_id="pv1",
        instruction="", client=client,
    )
    assert len(client.calls) == 1
    assert result["validated"] is True
    assert result["instruction"] == ""
    assert result["sections"][0]["dimension"] == "难度分布"


def test_run_review_reports_needs_review_count_and_unavailable_final_check(session):
    session.execute(
        update(paper_items)
        .where(paper_items.c.id == "item1")
        .values(needs_review=True, needs_review_reason="缺解析")
    )
    session.execute(
        update(generation_runs).where(generation_runs.c.id == "gr1")
        .values(contract_snapshot={})
    )
    session.commit()
    client = StubClient([_raw_good()])
    result = run_review(
        session, course_id="c1", paper_version_id="pv1",
        instruction="", client=client,
    )
    assert result["deterministic"] == {
        "needs_review_count": 1,
        "final_check_available": False,
    }


# ─── 任务入队 ───


def test_enqueue_creates_task_with_type_and_payload(session):
    task_id = enqueue_review(
        session, course_id="c1", paper_version_id="pv1", instruction="关注难度分布",
    )
    row = session.execute(
        select(task_runs.c.task_type, task_runs.c.status, task_runs.c.payload)
        .where(task_runs.c.id == task_id, task_runs.c.course_id == "c1")
    ).one()
    assert row.task_type == TASK_TYPE
    assert row.status == "queued"
    assert row.payload == {
        "course_id": "c1",
        "paper_version_id": "pv1",
        "instruction": "关注难度分布",
    }


def test_enqueue_dedupes_in_flight_and_mints_new_key_after_terminal(session):
    first = enqueue_review(
        session, course_id="c1", paper_version_id="pv1", instruction="标准评审",
    )
    again = enqueue_review(
        session, course_id="c1", paper_version_id="pv1", instruction="标准评审",
    )
    assert first == again  # 在途任务复用，不重复烧模型

    session.execute(
        update(task_runs).where(task_runs.c.id == first).values(status="succeeded")
    )
    session.commit()
    fresh = enqueue_review(
        session, course_id="c1", paper_version_id="pv1", instruction="标准评审",
    )
    assert fresh != first  # 已终态：同要求重发要拿到新任务


def test_enqueue_allows_empty_instruction(session):
    # instruction 可空：空关注点即标准评审，不拒绝
    task_id = enqueue_review(
        session, course_id="c1", paper_version_id="pv1", instruction="   ",
    )
    row = session.execute(
        select(task_runs.c.payload).where(task_runs.c.id == task_id)
    ).one()
    assert row.payload["instruction"] == ""


def test_enqueue_rejects_missing_paper(session):
    with pytest.raises(PaperReviewError, match="不存在"):
        enqueue_review(
            session, course_id="c1", paper_version_id="pv-x", instruction=""
        )


def test_enqueue_rejects_empty_paper(session):
    session.execute(paper_versions.insert().values(
        id="pv-empty", course_id="c1", exam_project_id="proj1", version_no=9, status="draft",
    ))
    session.commit()
    with pytest.raises(PaperReviewError, match="没有题目"):
        enqueue_review(
            session, course_id="c1", paper_version_id="pv-empty", instruction=""
        )


def test_enqueue_allows_finalized_paper(session):
    session.execute(
        update(paper_versions).where(paper_versions.c.id == "pv1").values(status="finalized")
    )
    session.commit()
    task_id = enqueue_review(
        session, course_id="c1", paper_version_id="pv1", instruction=""
    )
    assert task_id
