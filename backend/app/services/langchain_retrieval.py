"""LangChain 检索链装配：两条业务路径共用同一套组件。

装配后的链路（考点检索与助手 RAG 同构）：

    query ─┬─ VectorStore.similarity_search ─┐
           └─ BM25Retriever                  ┴─ weighted RRF ─ Optional(MultiVariant) ─ Optional(Neighborhood) ─ top_k

本模块只做"装配与边界转换"：
- ``VectorStore``/``BM25Retriever``/``WeightedRRFRetriever``/``MultiVariantRetriever``/
  ``NeighborhoodExpansionRetriever`` 各自在 ``adapters/retrieval`` 下独立实现；
- 这里负责选后端、配权重、配阈值、把 ``StagingChunk`` 与 ``Document`` 互转。

两条路径的差异只在"查询是否已批量嵌入"：
- 知识目录（``organization_graph``）按考点批量嵌入 3 条查询后传入向量，
  避免重复调用嵌入 API，因此用 ``HybridRetrievalChain.search`` 直接喂向量；
- 助手 RAG 只有一条自然语言问题，走 ``MultiVariantRetriever`` 全文检索。
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict

from app.adapters.retrieval.bm25 import BM25Retriever
from app.adapters.retrieval.embedding_contract import embed_query
from app.adapters.retrieval.fusion import weighted_reciprocal_rank
from app.adapters.retrieval.langchain_documents import staging_chunk_to_document
from app.adapters.retrieval.multi_query import MultiVariantRetriever
from app.adapters.retrieval.parent import NeighborhoodExpansionRetriever
from app.adapters.retrieval.vector_store import PaperPactVectorStore
from app.domain.knowledge.relevance import StagingChunk

# 融合权重沿用线上调参结论（语义 0.65 / 词面 0.35）。RRF 下权重是"通道影响力"，
# 不再是分数量纲，因此该比例可直接复用历史观测。
VECTOR_WEIGHT = 0.65
LEXICAL_WEIGHT = 0.35


def build_memory_vector_store(
    chunks: list[StagingChunk], embedder: Embeddings | None
) -> PaperPactVectorStore | None:
    """把按考点裁剪后的候选块装配成内存 VectorStore（块向量已在 state 里）。"""

    vectors = {
        chunk.id: [float(value) for value in chunk.embedding]
        for chunk in chunks
        if chunk.embedding
    }
    if not vectors:
        return None
    documents = {
        chunk.id: staging_chunk_to_document(chunk)
        for chunk in chunks
        if chunk.id in vectors
    }
    return PaperPactVectorStore(
        embedding=embedder,
        backend="memory",
        documents_by_id=documents,
        vectors=vectors,
    )


class HybridRetrievalChain:
    """向量 + BM25 → 加权 RRF 的混合检索（可喂预计算查询向量）。"""

    def __init__(
        self,
        *,
        documents: list[Document],
        vector_store: PaperPactVectorStore | None,
        top_k: int,
        score_threshold: float,
        embedder: Embeddings | None = None,
        id_key: str = "chunk_id",
    ) -> None:
        if top_k <= 0:
            raise ValueError("top_k 必须是正整数")
        self.documents = documents
        self.vector_store = vector_store
        self.embedder = embedder
        self.top_k = int(top_k)
        self.score_threshold = max(0.0, min(1.0, float(score_threshold)))
        self.id_key = id_key
        # 通道召回深度：融合只看排名，通道候选太少会让 RRF 退化成单通道；
        # 取 top_k 的 3 倍（至少 top_k+4），兼顾召回质量与请求成本。
        self.depth = max(self.top_k * 3, self.top_k + 4)
        self._bm25 = BM25Retriever(documents=documents, k=self.depth, id_key=id_key)

    def search(
        self,
        query_text: str,
        *,
        query_vector: list[float] | None = None,
        limit: int | None = None,
    ) -> list[Document]:
        channels: list[list[Document]] = []
        weights: list[float] = []

        if self.vector_store is not None:
            vector = query_vector
            if vector is None and self.embedder is not None:
                vector = embed_query(self.embedder, query_text)
            if vector is not None:
                vector_hits = [
                    document
                    for document, distance in self.vector_store.similarity_search_with_score_by_vector(
                        vector, self.depth
                    )
                    if 1.0 - distance >= self.score_threshold
                ]
                if vector_hits:
                    channels.append(vector_hits)
                    weights.append(VECTOR_WEIGHT)

        if query_text.strip():
            lexical_hits = self._bm25.invoke(query_text)
            if lexical_hits:
                channels.append(lexical_hits)
                weights.append(LEXICAL_WEIGHT)

        if not channels:
            return []
        return weighted_reciprocal_rank(
            channels,
            weights=weights,
            top_k=limit or self.top_k,
            id_key=self.id_key,
            score_budget=self._score_budget(),
        )

    def _score_budget(self) -> float:
        """归一化权重预算：按**配置**权重之和，缺失通道记 0 分而不抬高它者的分。"""

        budget = LEXICAL_WEIGHT
        if self.vector_store is not None:
            budget += VECTOR_WEIGHT
        return budget


def build_assistant_retriever(
    chain: HybridRetrievalChain,
    *,
    documents: list[Document],
    variant_fn: Callable[[str], list[str]],
    expansion: bool = True,
) -> BaseRetriever:
    """助手 RAG 链：混合检索 → 多查询变体融合 → 邻域扩展。"""

    inner: BaseRetriever = _ChainRetriever(chain=chain)
    multi = MultiVariantRetriever(
        retriever=inner,
        variant_fn=variant_fn,
        top_k=chain.top_k,
        id_key=chain.id_key,
    )
    if not expansion:
        return multi
    return NeighborhoodExpansionRetriever(
        retriever=multi,
        documents=documents,
        id_key=chain.id_key,
    )


class _ChainRetriever(BaseRetriever):
    """把 ``HybridRetrievalChain`` 包成标准 Retriever（供组合器复用）。"""

    chain: Any
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _get_relevant_documents(
        self, query: str, *, run_manager: Any = None
    ) -> list[Document]:
        return self.chain.search(query)
