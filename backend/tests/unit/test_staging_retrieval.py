from __future__ import annotations

import pytest
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from app.adapters.retrieval.bm25 import BM25Retriever
from app.adapters.retrieval.errors import RetrievalConfigurationError
from app.adapters.retrieval.fusion import WeightedRRFRetriever, weighted_reciprocal_rank
from app.adapters.retrieval.multi_query import MultiVariantRetriever
from app.adapters.retrieval.parent import NeighborhoodExpansionRetriever
from app.adapters.retrieval.tokenization import (
    dedup_key,
    query_token_coverage,
    tokenize,
)
from app.adapters.retrieval.vector_store import (
    PaperPactVectorStore,
    cosine_similarity,
)
from app.domain.framework.exam_points import (
    ExamPoint,
    OperationalDetailPolicy,
    WeightSource,
)
from app.domain.knowledge.relevance import StagingChunk
from app.services.staging_retrieval_service import HybridStagingRetriever


def _exam_point(*, retrieval_intent: str = "检索链路、召回偏差及诊断依据") -> ExamPoint:
    return ExamPoint(
        code="EP-1",
        anchor_key="retrieval-diagnosis",
        title="检索偏差诊断",
        assessment_requirement="能够分析检索遗漏的原因并提出诊断依据",
        weight_value=10,
        weight_source=WeightSource.ASSESSMENT_SYLLABUS,
        weight_group_id="retrieval",
        operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
        retrieval_intent=retrieval_intent,
    )


class StaticEmbedder:
    """固定向量桩：``vectors`` 是嵌入服务返回的批量向量。"""

    def __init__(self, vectors: list[list[float]]):
        self.vectors = vectors
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return self.vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)

    def embed_query(self, text: str) -> list[float]:
        self.calls.append([text])
        return self.vectors[0] if self.vectors else []

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)


class FailingEmbedder(StaticEmbedder):
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding service unavailable")


def _chunk(chunk_id: str, content: str, *, vector=None, page_index=0) -> StagingChunk:
    return StagingChunk(
        id=chunk_id,
        material_version_id="MV-1",
        content=content,
        locator={
            "material_id": "MAT-1",
            "material_name": "课件",
            "page_index": page_index,
            "heading_path": ["第一章", "检索"],
        },
        embedding=vector,
    )


# ---- 分词与重叠度量 ------------------------------------------------------
def test_tokenize_splits_ascii_words_and_chinese_ngrams():
    tokens = tokenize("LangChain 检索链路")

    assert "langchain" in tokens
    assert "检索" in tokens
    assert "链路" in tokens
    assert "检索链" in tokens


def test_query_token_coverage_ignores_content_length():
    short = query_token_coverage("检索链路", "检索链路")
    long_body = query_token_coverage("检索链路", "检索链路" + "无关内容" * 200)

    assert short == 1.0
    assert long_body == 1.0


def test_dedup_key_normalizes_blank_and_form_underscores():
    assert dedup_key("教学班：____") == dedup_key("教学班：__  __")


def test_cosine_similarity_clips_negative_values():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == 0.0


# ---- BM25 ---------------------------------------------------------------
def test_bm25_retriever_ranks_by_query_term_frequency():
    documents = [
        Document(page_content="检索链路召回偏差诊断", metadata={"chunk_id": "a"}),
        Document(page_content="数据库连接池配置", metadata={"chunk_id": "b"}),
    ]
    retriever = BM25Retriever(documents=documents, k=2)

    hits = retriever.invoke("检索链路")

    assert [hit.metadata["chunk_id"] for hit in hits] == ["a"]
    assert hits[0].metadata["lexical_score"] == 1.0


# ---- 加权 RRF -----------------------------------------------------------
def test_weighted_rrf_fuses_channels_by_rank_and_is_deterministic():
    vector_channel = [
        Document(page_content="A", metadata={"chunk_id": "a"}),
        Document(page_content="B", metadata={"chunk_id": "b"}),
    ]
    lexical_channel = [
        Document(page_content="B", metadata={"chunk_id": "b"}),
        Document(page_content="C", metadata={"chunk_id": "c"}),
    ]

    first = weighted_reciprocal_rank(
        [vector_channel, lexical_channel], weights=[0.65, 0.35], top_k=3
    )
    second = weighted_reciprocal_rank(
        [list(reversed(vector_channel)), list(reversed(lexical_channel))],
        weights=[0.65, 0.35],
        top_k=3,
    )

    assert [document.metadata["chunk_id"] for document in first] == ["b", "a", "c"]
    # 通道内部顺序反过来后 a/c 名次变化，b 仍在两通道都靠前
    assert first[0].metadata["chunk_id"] == "b"
    assert second[0].metadata["chunk_id"] == "b"
    assert first[0].metadata["fusion"] == "rrf"


def test_weighted_rrf_retriever_wraps_channels():
    retriever = WeightedRRFRetriever(
        retrievers=[
            _StaticDocs(
                documents=[Document(page_content="A", metadata={"chunk_id": "a"})]
            )
        ],
        weights=[0.35],
        top_k=1,
    )

    hits = retriever.invoke("任意查询")

    assert hits[0].metadata["chunk_id"] == "a"
    assert hits[0].metadata["score"] == 1.0


# ---- 多查询 -------------------------------------------------------------
def test_multi_variant_retriever_collapses_template_duplicates_and_marks_variants():
    class _Echo(BaseRetriever):
        def _get_relevant_documents(
            self, query: str, *, run_manager: CallbackManagerForRetrieverRun | None = None
        ) -> list[Document]:
            return [
                Document(page_content="教学班：____", metadata={"chunk_id": "t1"}),
                Document(page_content=f"正文 {query}", metadata={"chunk_id": f"c-{query}"}),
            ]

    retriever = MultiVariantRetriever(
        retriever=_Echo(),
        variant_fn=lambda question: [question, "核心"],
        top_k=3,
    )

    hits = retriever.invoke("问题")

    assert hits[0].metadata["chunk_id"] == "t1"
    assert {hit.metadata["chunk_id"] for hit in hits} == {"t1", "c-问题", "c-核心"}
    assert hits[0].metadata["query_variants"] == 2


# ---- 邻域扩展 -----------------------------------------------------------
def _document(chunk_id: str, content: str, *, page_index=None, heading=None) -> Document:
    metadata = {
        "chunk_id": chunk_id,
        "material_id": "MAT-1",
        "page_index": page_index,
        "heading_path": heading or [],
    }
    return Document(page_content=content, metadata=metadata)


def test_neighborhood_expansion_attaches_same_page_body():
    anchor = _document("a", "检索偏差诊断要点", page_index=3)
    documents = [
        anchor,
        _document("b", "同页正文" * 30, page_index=3),
        _document("c", "邻页正文" * 30, page_index=4),
        _document("d", "远页正文" * 30, page_index=9),
    ]

    retriever = NeighborhoodExpansionRetriever(
        retriever=_StaticDocs(documents=[anchor]), documents=documents, per_hit=1, max_total=1
    )
    expanded = retriever.expand([anchor])

    assert [document.metadata["chunk_id"] for document in expanded] == ["a", "b"]
    assert expanded[1].metadata["expanded_from"] == "a"
    # 扩展块继承锚点分数，不改变排序权威
    assert expanded[1].metadata["score"] == anchor.metadata.get("score", 0.0)


def test_neighborhood_expansion_heading_anchor_pulls_section_bodies_under_budget():
    heading = _document("h", "检索", heading=["第一章", "检索"])
    documents = [
        heading,
        _document("b1", "正文一" * 100, heading=["第一章", "检索"]),
        _document("b2", "正文二" * 100, heading=["第一章", "检索"]),
    ]

    retriever = NeighborhoodExpansionRetriever(
        retriever=_StaticDocs(documents=[heading]),
        documents=documents,
        section_per_hit_chars=300,
    )
    expanded = retriever.expand([heading])

    assert [document.metadata["chunk_id"] for document in expanded] == ["h", "b1"]


def test_neighborhood_expansion_body_anchor_prefers_following_neighbor():
    anchor = _document("b1", "正文命中" * 20, heading=["第一章"])
    documents = [
        _document("b0", "前邻正文" * 20, heading=["第一章"]),
        anchor,
        _document("b2", "后邻正文" * 20, heading=["第一章"]),
    ]

    retriever = NeighborhoodExpansionRetriever(
        retriever=_StaticDocs(documents=[anchor]), documents=documents, per_hit=1, max_total=1
    )
    expanded = retriever.expand([anchor])

    assert [document.metadata["chunk_id"] for document in expanded] == ["b1", "b2"]


def test_neighborhood_expansion_dedups_and_handles_empty_input():
    anchor = _document("a", "命中正文" * 20, page_index=1)
    documents = [anchor, _document("shared", "共享正文" * 20, page_index=1)]

    retriever = NeighborhoodExpansionRetriever(
        retriever=_StaticDocs(documents=[anchor]), documents=documents, per_hit=2, max_total=2
    )

    assert retriever.expand([]) == []
    first = retriever.expand([anchor])
    assert [document.metadata["chunk_id"] for document in first] == ["a", "shared"]


class _StaticDocs(BaseRetriever):
    documents: list[Document] = []

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun | None = None
    ) -> list[Document]:
        return list(self.documents)


# ---- 向量存储（内存后端） -----------------------------------------------
def test_memory_vector_store_returns_distance_and_semantic_signal():
    documents = {
        "a": Document(page_content="检索链路", metadata={"chunk_id": "a"}),
        "b": Document(page_content="数据库", metadata={"chunk_id": "b"}),
    }
    store = PaperPactVectorStore(
        embedding=StaticEmbedder([]),
        backend="memory",
        documents_by_id=documents,
        vectors={"a": [1.0, 0.0], "b": [0.0, 1.0]},
    )

    hits = store.similarity_search_with_score_by_vector([1.0, 0.0], k=2)

    assert hits[0][0].metadata["chunk_id"] == "a"
    assert hits[0][1] == 0.0
    assert hits[0][0].metadata["semantic_score"] == 1.0


def test_memory_vector_store_rejects_dimension_mismatch():
    store = PaperPactVectorStore(
        embedding=StaticEmbedder([]),
        backend="memory",
        documents_by_id={"a": Document(page_content="A", metadata={"chunk_id": "a"})},
        vectors={"a": [1.0, 0.0]},
    )

    with pytest.raises(RetrievalConfigurationError):
        store.similarity_search_with_score_by_vector([1.0, 0.0, 0.0], k=1)


def test_vector_store_requires_session_for_sql_backends():
    with pytest.raises(RetrievalConfigurationError):
        PaperPactVectorStore(
            embedding=StaticEmbedder([]),
            backend="pgvector",
            documents_by_id={},
        )


# ---- 考点检索门面 --------------------------------------------------------
def _retrieval_chunks() -> list[StagingChunk]:
    return [
        _chunk("c1", "检索链路召回偏差的定位方法", vector=[1.0, 0.0]),
        _chunk("c2", "数据库连接池与事务隔离", vector=[0.0, 1.0]),
    ]


def test_retrieve_returns_only_supplied_snapshot_chunks_and_is_deterministic():
    chunks = _retrieval_chunks()
    retriever = HybridStagingRetriever(
        embedder=StaticEmbedder([[1.0, 0.0]]), top_k=1, minimum_score=0.2
    )

    first = retriever.retrieve(_exam_point(), chunks)
    second = retriever.retrieve(_exam_point(), chunks)

    assert [item.chunk.id for item in first] == ["c1"]
    assert first == second
    assert all(item.chunk.id in {"c1", "c2"} for item in first)


def test_retrieve_skips_embedder_for_empty_snapshot():
    embedder = StaticEmbedder([[1.0, 0.0]])
    retriever = HybridStagingRetriever(embedder=embedder, top_k=3, minimum_score=0.0)

    assert retriever.retrieve(_exam_point(), []) == []
    assert embedder.calls == []


def test_retrieve_wraps_embedder_failure():
    retriever = HybridStagingRetriever(
        embedder=FailingEmbedder([]), top_k=2, minimum_score=0.1
    )

    with pytest.raises(RetrievalConfigurationError):
        retriever.retrieve(_exam_point(), _retrieval_chunks())


def test_retrieve_multi_merges_queries_by_highest_score():
    chunks = _retrieval_chunks()
    retriever = HybridStagingRetriever(
        embedder=StaticEmbedder([]), top_k=2, minimum_score=0.0
    )

    ranked = retriever.retrieve_multi(
        _exam_point(),
        chunks,
        query_vectors=[[1.0, 0.0], [0.0, 1.0]],
        query_texts=["检索链路", "数据库"],
    )

    assert {item.chunk.id for item in ranked} == {"c1", "c2"}
    assert [item.chunk.id for item in ranked] == [
        item.chunk.id
        for item in retriever.retrieve_multi(
            _exam_point(),
            chunks,
            query_vectors=[[1.0, 0.0], [0.0, 1.0]],
            query_texts=["检索链路", "数据库"],
        )
    ]


def test_retriever_rejects_unsafe_configuration():
    with pytest.raises(RetrievalConfigurationError):
        HybridStagingRetriever(embedder=StaticEmbedder([]), top_k=0, minimum_score=0.1)
    with pytest.raises(RetrievalConfigurationError):
        HybridStagingRetriever(embedder=StaticEmbedder([]), top_k=1, minimum_score=1.5)


def test_embed_queries_returns_vectors_aligned_with_input():
    retriever = HybridStagingRetriever(
        embedder=StaticEmbedder([[1.0, 0.0], [0.0, 1.0]]), top_k=1, minimum_score=0.0
    )

    assert retriever.embed_queries(["检索", "数据库"]) == [[1.0, 0.0], [0.0, 1.0]]
