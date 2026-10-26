"""PaperPact 自定义 Retriever：与既有确定性检索核心的等价性、降级与过滤。"""

from __future__ import annotations

import pytest

from app.domain.framework.exam_points import (
    ExamPoint,
    OperationalDetailPolicy,
    WeightSource,
)
from app.domain.knowledge.relevance import StagingChunk
from app.services.langchain_retrieval import PaperPactHybridRetriever
from app.services.staging_retrieval_service import (
    RetrievalConfigurationError,
    retrieve_for_exam_point,
    retrieve_for_question,
    retrieve_multi_for_question,
)


class StaticEmbedder:
    def __init__(self, vectors: list[list[float]]):
        self.vectors = vectors
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return self.vectors


def _chunks() -> list[StagingChunk]:
    return [
        StagingChunk(
            id="a",
            material_version_id="v1",
            content="监督学习分为分类与回归两大任务。",
            embedding=[1.0, 0.0],
            locator={"material_id": "m1", "material_name": "教学大纲", "page_index": 2},
        ),
        StagingChunk(
            id="b",
            material_version_id="v1",
            content="聚类与降维属于无监督学习方法。",
            embedding=[0.6, 0.8],
            locator={"material_id": "m1", "material_name": "教学大纲", "page_index": 3},
        ),
        StagingChunk(
            id="dup1",
            material_version_id="v1",
            content="教学班：____________",
            embedding=[0.9, 0.9],
            locator={"material_id": "m1", "page_index": 1},
        ),
        StagingChunk(
            id="dup2",
            material_version_id="v2",
            content="教学班：__________",
            embedding=[0.9, 0.9],
            locator={"material_id": "m2", "page_index": 1},
        ),
    ]


def _exam_point() -> ExamPoint:
    return ExamPoint(
        code="EP-1",
        anchor_key="supervised-learning",
        title="监督学习",
        assessment_requirement="能够区分监督学习与无监督学习",
        weight_value=10,
        weight_source=WeightSource.ASSESSMENT_SYLLABUS,
        weight_group_id="ml",
        operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
        retrieval_intent="监督学习的任务类型",
    )


def test_retrieve_variants_matches_legacy_multi_query():
    variants = ["总结监督学习讲了什么", "监督学习"]
    embedder = StaticEmbedder([[1.0, 0.0], [0.6, 0.8]])
    chunks = _chunks()

    legacy = retrieve_multi_for_question(
        variants, chunks, embedder, top_k=6, minimum_score=0.05
    )
    retriever = PaperPactHybridRetriever(
        chunks=chunks, embedder=embedder, top_k=6, minimum_score=0.05
    )
    modern = retriever.retrieve_variants(variants)

    assert [doc.metadata["chunk_id"] for doc in modern] == [
        item.chunk.id for item in legacy
    ]
    assert [doc.metadata["score"] for doc in modern] == [
        item.score for item in legacy
    ]
    assert [doc.page_content for doc in modern] == [
        item.chunk.content for item in legacy
    ]


def test_retrieve_ranked_matches_legacy_single_query():
    embedder = StaticEmbedder([[1.0, 0.0]])
    chunks = _chunks()

    legacy = retrieve_for_question(
        "监督学习有哪些任务", chunks, embedder, top_k=2, minimum_score=0.05
    )
    retriever = PaperPactHybridRetriever(
        chunks=chunks, embedder=embedder, top_k=2, minimum_score=0.05
    )

    assert retriever.retrieve_ranked("监督学习有哪些任务") == legacy


def test_empty_chunks_do_not_call_embedder():
    embedder = StaticEmbedder([])
    retriever = PaperPactHybridRetriever(
        chunks=[], embedder=embedder, top_k=3, minimum_score=0.1
    )

    assert retriever.invoke("任意问题") == []
    assert retriever.retrieve_variants(["问题", "题"]) == []
    assert embedder.calls == []


def test_blank_query_variants_are_filtered():
    embedder = StaticEmbedder([])
    retriever = PaperPactHybridRetriever(
        chunks=_chunks(), embedder=embedder, top_k=3, minimum_score=0.1
    )

    assert retriever.retrieve_variants(["", "   "]) == []
    assert embedder.calls == []


def test_precomputed_semantic_scores_skip_embedding():
    class ExplodingEmbedder:
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise AssertionError("预计算语义分路径不得调用嵌入服务")

    chunks = _chunks()
    scores = [{"a": 1.0, "b": 0.6, "dup1": 0.9, "dup2": 0.9}] * 2
    retriever = PaperPactHybridRetriever(
        chunks=chunks, embedder=ExplodingEmbedder(), top_k=4, minimum_score=0.0
    )

    documents = retriever.retrieve_variants(
        ["监督学习有哪些任务", "监督学习"], semantic_scores=scores
    )

    assert documents
    assert documents[0].metadata["chunk_id"] == "a"


def test_embedding_failure_propagates_for_upper_layer_degradation():
    class FailingEmbedder:
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("network down")

    retriever = PaperPactHybridRetriever(
        chunks=_chunks(), embedder=FailingEmbedder(), top_k=3, minimum_score=0.1
    )

    with pytest.raises(RetrievalConfigurationError, match="检索已中止"):
        retriever.invoke("监督学习")


def test_invalid_configuration_still_raises_retrieval_error():
    retriever = PaperPactHybridRetriever(
        chunks=_chunks(), embedder=StaticEmbedder([]), top_k=0, minimum_score=0.1
    )

    with pytest.raises(RetrievalConfigurationError):
        retriever.retrieve_ranked("监督学习")

    bad_threshold = PaperPactHybridRetriever(
        chunks=_chunks(), embedder=StaticEmbedder([]), top_k=3, minimum_score=1.5
    )
    with pytest.raises(RetrievalConfigurationError):
        bad_threshold.retrieve_variants(["监督学习", "学习"])


def test_retrieval_only_returns_supplied_course_snapshot():
    chunks = _chunks()[:1]  # material_ids 过滤后的快照
    embedder = StaticEmbedder([[1.0, 0.0], [1.0, 0.0]])
    retriever = PaperPactHybridRetriever(
        chunks=chunks, embedder=embedder, top_k=10, minimum_score=0.0
    )

    documents = retriever.retrieve_variants(["监督学习", "监督学习分类"])

    assert [doc.metadata["chunk_id"] for doc in documents] == ["a"]
    assert embedder.calls == [["监督学习", "监督学习分类"]]


def test_exam_point_retrieval_matches_legacy_core():
    point = _exam_point()
    chunks = _chunks()
    embedder = StaticEmbedder([[1.0, 0.0]])
    retriever = PaperPactHybridRetriever(
        chunks=[], embedder=embedder, top_k=4, minimum_score=0.0
    )

    legacy = retrieve_for_exam_point(
        point, chunks, embedder, top_k=4, minimum_score=0.0
    )
    modern = retriever.retrieve_exam_point(point, chunks)

    assert [item.chunk.id for item in modern] == [item.chunk.id for item in legacy]
    assert [
        (item.score, item.lexical_score, item.semantic_score) for item in modern
    ] == pytest.approx(
        [(item.score, item.lexical_score, item.semantic_score) for item in legacy]
    )
