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

    def request_json(self, *, system_prompt, payload, temperature,
                     call_context=None, response_validator=None):
        self.payloads.append(payload)
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


def test_classify_file_rejects_incomplete_coverage():
    response = {
        "file_decisions": [
            _file_item("EP1", chunk_ids=("c1", "c2")),
            _file_item("EP2", chunk_ids=("c1",)),
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
