"""一键补证据（auto_supplement_direct_evidence）单元测试。"""

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


def _payload(coverage: list[dict], sources: list[dict]) -> dict:
    return {"coverage": coverage, "evidence_sources": sources}


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
        # 缺直接证据且有候选：应被处理
        _coverage("p1", "insufficient", ["no_direct_evidence"]),
        # 归并失败：补证据无法重建链路，跳过
        _coverage("p2", "insufficient", ["no_direct_evidence", "consolidation_failed"]),
        # 已充足：跳过
        _coverage("p3", "sufficient", []),
        # 无任何间接证据候选：跳过
        _coverage("p4", "insufficient", ["no_direct_evidence", "no_recalled_evidence"]),
        # conflicting：已有 direct 证据，跳过
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
    # 只 p1 被处理：p2 因 *_failed 跳过、p3 已充足、p4 无候选、p5 冲突。
    assert recommender.called == ["p1"]
    supplement_ops = [
        op for op in result.operations if op.operation == "supplement_direct_evidence"
    ]
    assert [op.value for op in supplement_ops] == ["c1"]
    # 教师原有操作保留
    assert any(
        op.operation == "exclude_topic" and op.target_code == "t1"
        for op in result.operations
    )
    # 自动补充过的考点并入已审阅集合
    assert "p1" in result.reviewed_exam_point_codes
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
