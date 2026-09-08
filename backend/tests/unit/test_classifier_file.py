from __future__ import annotations

import pytest

from app.adapters.model.deepseek_semantic_extractors import (
    DeepSeekExamPointEvidenceClassifier,
    DeepSeekModelError,
)
from app.domain.framework.exam_points import (
    ExamPoint,
    OperationalDetailPolicy,
    WeightSource,
)
from app.domain.knowledge.relevance import StagingChunk


class FakeJsonClient:
    def __init__(self, response):
        self.response = response
        self.payloads = []
        self.system_prompts = []

    def request_json(self, *, system_prompt, payload, temperature,
                     call_context=None, response_validator=None):
        self.payloads.append(payload)
        self.system_prompts.append(system_prompt)
        if response_validator:
            response_validator(self.response)
        return self.response


def _point(code="EP1") -> ExamPoint:
    return ExamPoint(
        code=code,
        anchor_key="rag",
        title=f"考点{code}",
        assessment_requirement=f"理解并应用{code}",
        weight_value=50,
        weight_source=WeightSource.ASSESSMENT_SYLLABUS,
        weight_group_id="rag",
        operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
        retrieval_intent=f"检索{code}的定义、原理和评分依据",
    )


def _chunk(cid, material="M1") -> StagingChunk:
    return StagingChunk(id=cid, material_version_id=material, content=f"{cid}内容")


def _background(point_code, chunk_id) -> dict:
    return {
        "exam_point_code": point_code,
        "evidence_chunk_id": chunk_id,
        "relevance_class": "background",
        "support_claim": "与主题相关的背景说明",
        "content_kind": "background",
        "confidence": 80,
    }


def _file_item(point_code, material="M1", chunk_ids=("c1", "c2")) -> dict:
    return {
        "exam_point_code": point_code,
        "material_version_id": material,
        "decisions": [_background(point_code, chunk_id) for chunk_id in chunk_ids],
    }


def test_classify_file_returns_decisions_for_all_points():
    response = {
        "file_decisions": [
            _file_item("EP1", chunk_ids=("c1", "c2")),
            _file_item("EP2", chunk_ids=("c1", "c2")),
        ]
    }
    client = FakeJsonClient(response)
    classifier = DeepSeekExamPointEvidenceClassifier(client)

    decisions = classifier.classify_file(
        exam_points=[_point("EP1"), _point("EP2")],
        material_version_id="M1",
        chunks=[_chunk("c1"), _chunk("c2")],
        call_context=None,
    )

    assert {item.exam_point_code for item in decisions} == {"EP1", "EP2"}
    assert all(len(item.decisions) == 2 for item in decisions)
    assert {item.material_version_id for item in decisions} == {"M1"}
    payload = client.payloads[0]
    assert [point["code"] for point in payload["exam_points"]] == ["EP1", "EP2"]
    assert payload["material_version_id"] == "M1"
    assert {chunk["evidence_chunk_id"] for chunk in payload["chunks"]} == {"c1", "c2"}


def test_classify_file_completes_missing_pairs_as_out_of_scope():
    response = {
        "file_decisions": [
            _file_item("EP1", chunk_ids=("c1", "c2")),
            _file_item("EP2", chunk_ids=("c1",)),
        ]
    }
    classifier = DeepSeekExamPointEvidenceClassifier(FakeJsonClient(response))

    decisions = classifier.classify_file(
        exam_points=[_point("EP1"), _point("EP2")],
        material_version_id="M1",
        chunks=[_chunk("c1"), _chunk("c2")],
    )

    by_point = {item.exam_point_code: item for item in decisions}
    assert {d.evidence_chunk_id for d in by_point["EP2"].decisions} == {"c1", "c2"}
    completed = {d.evidence_chunk_id: d for d in by_point["EP2"].decisions}["c2"]
    assert completed.relevance_class.value == "out_of_scope"


def test_classify_file_rejects_unknown_point():
    response = {
        "file_decisions": [
            _file_item("EP1", chunk_ids=("c1", "c2")),
            _file_item("EP3", chunk_ids=("c1", "c2")),
        ]
    }
    classifier = DeepSeekExamPointEvidenceClassifier(FakeJsonClient(response))

    with pytest.raises(DeepSeekModelError) as caught:
        classifier.classify_file(
            exam_points=[_point("EP1"), _point("EP2")],
            material_version_id="M1",
            chunks=[_chunk("c1"), _chunk("c2")],
        )

    assert caught.value.error_code == "model_output_scope_violation"


def test_classify_file_rejects_foreign_material():
    response = {
        "file_decisions": [
            _file_item("EP1", material="M2", chunk_ids=("c1", "c2")),
        ]
    }
    classifier = DeepSeekExamPointEvidenceClassifier(FakeJsonClient(response))

    with pytest.raises(DeepSeekModelError) as caught:
        classifier.classify_file(
            exam_points=[_point("EP1")],
            material_version_id="M1",
            chunks=[_chunk("c1"), _chunk("c2")],
        )

    assert caught.value.error_code == "model_output_scope_violation"


def test_classify_file_rejects_chunks_from_another_material():
    classifier = DeepSeekExamPointEvidenceClassifier(FakeJsonClient({}))

    with pytest.raises(DeepSeekModelError) as caught:
        classifier.classify_file(
            exam_points=[_point("EP1")],
            material_version_id="M1",
            chunks=[_chunk("c1"), _chunk("c2", material="M2")],
        )

    assert caught.value.error_code == "model_input_scope_violation"


def _supporting(chunk_id) -> dict:
    return {
        "evidence_chunk_id": chunk_id,
        "relevance_class": "supporting",
        "support_claim": "设问语境支撑",
        "content_kind": "fact",
        "confidence": 70,
    }


def test_classify_file_expands_compact_class_arrays():
    response = {
        "file_decisions": [
            {
                "exam_point_code": "EP1",
                "material_version_id": "M1",
                "decisions": [_supporting("c1")],
                "background_chunk_ids": ["c2"],
                "out_of_scope_chunk_ids": ["c3"],
            },
            {
                "exam_point_code": "EP2",
                "material_version_id": "M1",
                "background_chunk_ids": ["c1", "c2"],
                "out_of_scope_chunk_ids": ["c3"],
            },
        ]
    }
    client = FakeJsonClient(response)
    classifier = DeepSeekExamPointEvidenceClassifier(client)

    decisions = classifier.classify_file(
        exam_points=[_point("EP1"), _point("EP2")],
        material_version_id="M1",
        chunks=[_chunk("c1"), _chunk("c2"), _chunk("c3")],
    )

    by_point = {item.exam_point_code: item for item in decisions}
    ep1 = {d.evidence_chunk_id: d for d in by_point["EP1"].decisions}
    assert ep1["c1"].relevance_class.value == "supporting"
    assert ep1["c1"].exam_point_code == "EP1"
    assert ep1["c2"].relevance_class.value == "background"
    assert ep1["c2"].support_claim
    assert ep1["c2"].confidence == 100
    assert ep1["c3"].relevance_class.value == "out_of_scope"
    ep2 = {d.evidence_chunk_id: d for d in by_point["EP2"].decisions}
    assert set(ep2) == {"c1", "c2", "c3"}
    assert all(d.relevance_class.value in {"background", "out_of_scope"} for d in ep2.values())
    system = client.system_prompts[0]
    assert "background_chunk_ids" in system
    assert "out_of_scope_chunk_ids" in system


def test_classify_file_rejects_compact_array_duplication():
    response = {
        "file_decisions": [
            {
                "exam_point_code": "EP1",
                "material_version_id": "M1",
                "decisions": [_supporting("c1"), _background("EP1", "c1")],
                "out_of_scope_chunk_ids": ["c2"],
            }
        ]
    }
    classifier = DeepSeekExamPointEvidenceClassifier(FakeJsonClient(response))

    with pytest.raises(DeepSeekModelError) as caught:
        classifier.classify_file(
            exam_points=[_point("EP1")],
            material_version_id="M1",
            chunks=[_chunk("c1"), _chunk("c2")],
        )

    assert caught.value.error_code == "model_output_scope_violation"


def test_classify_file_completes_compact_array_missing_chunk():
    response = {
        "file_decisions": [
            {
                "exam_point_code": "EP1",
                "material_version_id": "M1",
                "decisions": [_supporting("c1")],
                "out_of_scope_chunk_ids": ["c2"],
            }
        ]
    }
    classifier = DeepSeekExamPointEvidenceClassifier(FakeJsonClient(response))

    decisions = classifier.classify_file(
        exam_points=[_point("EP1")],
        material_version_id="M1",
        chunks=[_chunk("c1"), _chunk("c2"), _chunk("c3")],
    )

    by_id = {d.evidence_chunk_id: d for d in decisions[0].decisions}
    assert set(by_id) == {"c1", "c2", "c3"}
    assert by_id["c3"].relevance_class.value == "out_of_scope"
