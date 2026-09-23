"""DeepSeekSupplementRecommender 单元测试。"""

import pytest

from app.adapters.model.deepseek_gateway import DeepSeekModelError
from app.adapters.model.deepseek_semantic_extractors import (
    DeepSeekSupplementRecommender,
)
from tests.unit.test_deepseek_semantic_extractors import _point


def _candidate(chunk_id: str, claim: str = "KV Cache 缓存注意力键值以复用计算") -> dict:
    return {
        "evidence_chunk_id": chunk_id,
        "relevance_class": "supporting",
        "support_claim": claim,
        "confidence": 70,
        "content": "KV Cache 用于缓存注意力历史键值以复用计算。",
    }


def test_recommend_returns_model_accepted_items_with_reason():
    point = _point()

    class Client:
        def request_json(self, **kwargs):
            payload = kwargs["payload"]
            assert payload["exam_point"]["code"] == "rag-diagnosis"
            assert len(payload["candidates"]) == 2
            kwargs["response_validator"](
                {
                    "exam_point_code": "rag-diagnosis",
                    "recommendations": [
                        {
                            "evidence_chunk_id": "c1",
                            "recommend": True,
                            "reason": "原文陈述 KV Cache 缓存注意力键值，属于机制知识",
                        },
                        {
                            "evidence_chunk_id": "c2",
                            "recommend": False,
                            "reason": "纯操作指令，不含知识陈述",
                        },
                    ],
                }
            )
            return {}

    items = DeepSeekSupplementRecommender(Client()).recommend(
        exam_point=point, candidates=[_candidate("c1"), _candidate("c2")]
    )
    assert [item["evidence_chunk_id"] for item in items] == ["c1"]
    assert "KV Cache" in items[0]["model_reason"]
    # 合并回原始字段
    assert items[0]["relevance_class"] == "supporting"


def test_recommend_rejects_unknown_chunk_id():
    point = _point()

    class Client:
        def request_json(self, **kwargs):
            kwargs["response_validator"](
                {
                    "exam_point_code": "rag-diagnosis",
                    "recommendations": [
                        {
                            "evidence_chunk_id": "hallucinated-id",
                            "recommend": True,
                            "reason": "不在候选中",
                        }
                    ],
                }
            )
            return {}

    with pytest.raises(DeepSeekModelError, match="unknown chunk"):
        DeepSeekSupplementRecommender(Client()).recommend(
            exam_point=point, candidates=[_candidate("c1")]
        )


def test_recommend_rejects_duplicate_chunk_id():
    point = _point()

    class Client:
        def request_json(self, **kwargs):
            kwargs["response_validator"](
                {
                    "exam_point_code": "rag-diagnosis",
                    "recommendations": [
                        {"evidence_chunk_id": "c1", "recommend": True, "reason": "a"},
                        {"evidence_chunk_id": "c1", "recommend": False, "reason": "b"},
                    ],
                }
            )
            return {}

    with pytest.raises(DeepSeekModelError, match="duplicate"):
        DeepSeekSupplementRecommender(Client()).recommend(
            exam_point=point, candidates=[_candidate("c1")]
        )


def test_recommend_rejects_other_exam_point_response():
    point = _point()

    class Client:
        def request_json(self, **kwargs):
            kwargs["response_validator"](
                {
                    "exam_point_code": "EP-999",
                    "recommendations": [],
                }
            )
            return {}

    with pytest.raises(DeepSeekModelError, match="another exam point"):
        DeepSeekSupplementRecommender(Client()).recommend(
            exam_point=point, candidates=[_candidate("c1")]
        )


def test_recommend_empty_candidates_short_circuits_without_model_call():
    point = _point()

    class Client:
        def request_json(self, **kwargs):  # pragma: no cover
            raise AssertionError("must not call the model for empty candidates")

    items = DeepSeekSupplementRecommender(Client()).recommend(
        exam_point=point, candidates=[]
    )
    assert items == []


def test_recommend_truncates_long_content():
    point = _point()
    long_content = "长" * 5000
    captured: dict = {}

    class Client:
        def request_json(self, **kwargs):
            captured["candidates"] = kwargs["payload"]["candidates"]
            # 覆盖性校验要求对每个候选给出恰好一条 recommendation
            kwargs["response_validator"](
                {
                    "exam_point_code": "rag-diagnosis",
                    "recommendations": [
                        {"evidence_chunk_id": "c1", "recommend": True, "reason": "承载考点知识"}
                    ],
                }
            )
            return {}

    DeepSeekSupplementRecommender(Client()).recommend(
        exam_point=point,
        candidates=[{**_candidate("c1"), "content": long_content}],
    )
    assert len(captured["candidates"][0]["content"]) == 1200
