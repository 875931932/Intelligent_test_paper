"""一键补证据（auto_supplement_direct_evidence）分流单元测试（含 C 类排除）。"""

from __future__ import annotations

import pytest

from app.adapters.model.deepseek_gateway import DeepSeekModelError
from app.api.v1.knowledge import _apply_auto_supplement
from app.domain.knowledge.models import KnowledgeTreeConfirmation, TreeOperation

_POINT_FIELDS = {
    "anchor_key": "rag",
    "title": "检索效果诊断",
    "assessment_requirement": "能够诊断召回偏差",
    "weight_value": 100,
    "weight_source": "assessment_syllabus",
    "weight_group_id": "rag",
    "priority": "normal",
    "cognitive_targets": ["analyze"],
    "assessment_orientations": ["diagnostic"],
    "allowed_question_types": [],
    "operational_detail_policy": "supporting_only",
    "scope_boundary": {},
    "required_evidence_roles": [],
    "retrieval_intent": "检索偏差及诊断依据",
    "assessment_anchor_keys": ["rag"],
    "teaching_anchor_keys": ["rag-teaching"],
    "status": "confirmed",
}


def _point_row(code: str) -> dict:
    return {**_POINT_FIELDS, "id": f"id-{code}", "code": code}


class _FakeSession:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def execute(self, statement):
        return self

    def mappings(self):
        return iter(self.rows)


def _candidate(code: str, chunk_id: str, relevance_class: str = "supporting") -> dict:
    return {
        "exam_point_code": code,
        "evidence_chunk_id": chunk_id,
        "relevance_class": relevance_class,
        "support_claim": "KV Cache 缓存注意力键值以复用计算",
        "confidence": 70,
        "content": "KV Cache 用于缓存注意力历史键值以复用计算。",
    }


def _coverage(code: str, status: str, reasons: list[str]) -> dict:
    return {"exam_point_code": code, "status": status, "reasons": reasons}


def _tree_with_card(code: str) -> list[dict]:
    return [
        {
            "status": "active",
            "units": [
                {
                    "status": "active",
                    "exam_point_code": code,
                    "cards": [{"status": "active"}],
                }
            ],
        }
    ]


class _Recommender:
    def __init__(self, accepted: dict[str, list[str]], failing: set[str] | None = None) -> None:
        self.accepted = accepted
        self.failing = failing or set()
        self.called: list[str] = []

    def recommend(self, *, exam_point, candidates, call_context=None):
        self.called.append(exam_point.code)
        if exam_point.code in self.failing:
            raise DeepSeekModelError("model_error", "boom")
        chunks = self.accepted.get(exam_point.code, [])
        return [
            {
                "evidence_chunk_id": chunk_id,
                "relevance_class": "supporting",
                "support_claim": "claim",
                "confidence": 70,
                "content": "content",
                "model_reason": "承载考点知识",
            }
            for chunk_id in chunks
        ]


def _payload(coverage: list[dict], sources: list[dict], topics: list[dict] | None = None) -> dict:
    return {"coverage": coverage, "evidence_sources": sources, "topics": topics or []}


def test_auto_flag_false_returns_confirmation_untouched():
    recommender = _Recommender({})
    confirmation = KnowledgeTreeConfirmation(
        operations=[],
        reviewed_topic_codes=[],
        reviewed_exam_point_codes=[],
        teacher_exclusions=[],
    )
    result = _apply_auto_supplement(
        course_id="course",
        run_id="run",
        candidate={"payload": _payload([], [])},
        confirmation=confirmation,
        session=_FakeSession([]),
        recommender=recommender,
    )
    assert result is confirmation
    assert recommender.called == []


def test_auto_only_targets_no_direct_evidence_points_with_candidates():
    recommender = _Recommender(
        accepted={"p1": ["c1"], "p3": ["c3"]},
        failing={"p2"},
    )
    coverage = [
        _coverage("p1", "insufficient", ["no_direct_evidence"]),
        _coverage("p2", "insufficient", ["no_direct_evidence", "consolidation_failed"]),
        _coverage("p3", "sufficient", []),
        _coverage("p4", "insufficient", ["no_direct_evidence", "no_recalled_evidence"]),
        _coverage("p5", "conflicting", ["conflicting_direct_claims"]),
    ]
    sources = [
        _candidate("p1", "c1"),
        _candidate("p2", "c2"),
        _candidate("p4", "c4"),
    ]
    confirmation = KnowledgeTreeConfirmation(
        operations=[
            TreeOperation(operation="exclude_topic", target_code="t1", value="t1")
        ],
        reviewed_topic_codes=["t1"],
        reviewed_exam_point_codes=["p3"],
        teacher_exclusions=[],
        auto_supplement_direct_evidence=True,
    )
    result = _apply_auto_supplement(
        course_id="course",
        run_id="run",
        candidate={"payload": _payload(coverage, sources)},
        confirmation=confirmation,
        session=_FakeSession([_point_row("p1")]),
        recommender=recommender,
    )
    # A 类只 p1 被补证据：p3 已充足、p4 无候选、p5 冲突。
    assert recommender.called == ["p1"]
    supplement_ops = [
        op for op in result.operations if op.operation == "supplement_direct_evidence"
    ]
    assert [op.value for op in supplement_ops] == ["c1"]
    # C 类 p2（failed 且无活跃卡链）被自动排除
    assert "p2" in result.teacher_exclusions
    # 教师原有操作保留
    assert any(
        op.operation == "exclude_topic" and op.target_code == "t1"
        for op in result.operations
    )
    # 自动补充/排除的考点并入已审阅集合
    assert "p1" in result.reviewed_exam_point_codes
    assert "p2" in result.reviewed_exam_point_codes
    assert "p3" in result.reviewed_exam_point_codes


def test_auto_merges_recommendations_across_points_and_dedupes():
    recommender = _Recommender(accepted={"p1": ["c1", "c2"], "p2": ["c3"]})
    coverage = [
        _coverage("p1", "insufficient", ["no_direct_evidence"]),
        _coverage("p2", "insufficient", ["no_direct_evidence"]),
    ]
    sources = [
        _candidate("p1", "c1"),
        _candidate("p1", "c2"),
        _candidate("p2", "c3"),
    ]
    confirmation = KnowledgeTreeConfirmation(
        operations=[],
        reviewed_topic_codes=[],
        reviewed_exam_point_codes=[],
        teacher_exclusions=[],
        auto_supplement_direct_evidence=True,
    )
    result = _apply_auto_supplement(
        course_id="course",
        run_id="run",
        candidate={"payload": _payload(coverage, sources)},
        confirmation=confirmation,
        session=_FakeSession([_point_row("p1"), _point_row("p2")]),
        recommender=recommender,
    )
    ops = [
        (op.target_code, op.value)
        for op in result.operations
        if op.operation == "supplement_direct_evidence"
    ]
    assert ops == [("p1", "c1"), ("p1", "c2"), ("p2", "c3")]
    assert set(result.reviewed_exam_point_codes) == {"p1", "p2"}


def test_auto_no_recommendations_returns_confirmation_unchanged():
    recommender = _Recommender(accepted={"p1": []})
    coverage = [_coverage("p1", "insufficient", ["no_direct_evidence"])]
    sources = [_candidate("p1", "c1")]
    confirmation = KnowledgeTreeConfirmation(
        operations=[],
        reviewed_topic_codes=[],
        reviewed_exam_point_codes=[],
        teacher_exclusions=[],
        auto_supplement_direct_evidence=True,
    )
    result = _apply_auto_supplement(
        course_id="course",
        run_id="run",
        candidate={"payload": _payload(coverage, sources)},
        confirmation=confirmation,
        session=_FakeSession([_point_row("p1")]),
        recommender=recommender,
    )
    assert result.operations == []
    assert result.reviewed_exam_point_codes == []


def test_auto_failed_but_with_active_card_chain_is_rescued_not_excluded():
    """B 类：failed 残因 + 已有活跃卡链 → 不排除（交由发布端救回）。"""
    recommender = _Recommender(accepted={})
    coverage = [_coverage("p1", "insufficient", ["classification_failed"])]
    # 树内含 p1 活跃卡链
    topics = _tree_with_card("p1")
    confirmation = KnowledgeTreeConfirmation(
        operations=[],
        reviewed_topic_codes=[],
        reviewed_exam_point_codes=[],
        teacher_exclusions=[],
        auto_supplement_direct_evidence=True,
    )
    result = _apply_auto_supplement(
        course_id="course",
        run_id="run",
        candidate={"payload": _payload(coverage, [], topics)},
        confirmation=confirmation,
        session=_FakeSession([]),
        recommender=recommender,
    )
    assert recommender.called == []
    # 有活跃卡链的 failed 考点不进入 teacher_exclusions，交由发布端救回
    assert "p1" not in result.teacher_exclusions
    assert "p1" not in result.reviewed_exam_point_codes


def test_auto_exists_nothing_when_only_b_rescue_candidates():
    """仅 B 类（有卡 failed）时，本函数不应改变 confirmation。"""
    recommender = _Recommender(accepted={})
    coverage = [_coverage("p1", "insufficient", ["classification_failed"])]
    topics = _tree_with_card("p1")
    confirmation = KnowledgeTreeConfirmation(
        operations=[],
        reviewed_topic_codes=[],
        reviewed_exam_point_codes=[],
        teacher_exclusions=[],
        auto_supplement_direct_evidence=True,
    )
    result = _apply_auto_supplement(
        course_id="course",
        run_id="run",
        candidate={"payload": _payload(coverage, [], topics)},
        confirmation=confirmation,
        session=_FakeSession([]),
        recommender=recommender,
    )
    assert result is confirmation


def test_auto_filters_out_background_candidates():
    """A 类候选仅保留 supporting：background 的 support_claim 是无意义占位符，
    改判后卡片无支撑事实会触发发布质量闸，不得作为补证据候选下发推荐器。"""
    recommender = _Recommender(accepted={"p1": ["c2"]})
    coverage = [_coverage("p1", "insufficient", ["no_direct_evidence"])]
    sources = [
        _candidate("p1", "c1", relevance_class="background"),
        _candidate("p1", "c2", relevance_class="supporting"),
    ]
    confirmation = KnowledgeTreeConfirmation(
        operations=[],
        reviewed_topic_codes=[],
        reviewed_exam_point_codes=[],
        teacher_exclusions=[],
        auto_supplement_direct_evidence=True,
    )
    result = _apply_auto_supplement(
        course_id="course",
        run_id="run",
        candidate={"payload": _payload(coverage, sources)},
        confirmation=confirmation,
        session=_FakeSession([_point_row("p1")]),
        recommender=recommender,
    )
    ops = [
        (op.target_code, op.value)
        for op in result.operations
        if op.operation == "supplement_direct_evidence"
    ]
    # 只可能改判 supporting 的 c2；background 的 c1 不被采纳
    assert ops == [("p1", "c2")]


def test_auto_excludes_no_direct_evidence_with_only_background_candidates():
    """仅有 background 候选的 no_direct_evidence 考点：不算 A 类（无 supporting
    可改判）、无 failed 残因，此前掉进缝隙既不补也不排除拦发布；现在自动排除。"""
    recommender = _Recommender(accepted={})
    coverage = [_coverage("p1", "insufficient", ["no_direct_evidence"])]
    sources = [_candidate("p1", "c1", relevance_class="background")]
    confirmation = KnowledgeTreeConfirmation(
        operations=[],
        reviewed_topic_codes=[],
        reviewed_exam_point_codes=[],
        teacher_exclusions=[],
        auto_supplement_direct_evidence=True,
    )
    result = _apply_auto_supplement(
        course_id="course",
        run_id="run",
        candidate={"payload": _payload(coverage, sources)},
        confirmation=confirmation,
        session=_FakeSession([_point_row("p1")]),
        recommender=recommender,
    )
    assert recommender.called == []
    assert "p1" in result.teacher_exclusions
    assert not any(
        op.operation == "supplement_direct_evidence" for op in result.operations
    )


def test_auto_excludes_no_direct_evidence_without_any_candidates():
    """完全无候选的 no_direct_evidence 考点（材料无对应知识）：自动排除，
    避免覆盖缺口考点永久拦死一键发布。"""
    recommender = _Recommender(accepted={})
    coverage = [_coverage("p1", "insufficient", ["no_direct_evidence"])]
    confirmation = KnowledgeTreeConfirmation(
        operations=[],
        reviewed_topic_codes=[],
        reviewed_exam_point_codes=[],
        teacher_exclusions=[],
        auto_supplement_direct_evidence=True,
    )
    result = _apply_auto_supplement(
        course_id="course",
        run_id="run",
        candidate={"payload": _payload(coverage, [])},
        confirmation=confirmation,
        session=_FakeSession([_point_row("p1")]),
        recommender=recommender,
    )
    assert recommender.called == []
    assert "p1" in result.teacher_exclusions
    assert "p1" in result.reviewed_exam_point_codes