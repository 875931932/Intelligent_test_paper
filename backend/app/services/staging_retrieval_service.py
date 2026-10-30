"""考点检索门面：知识目录（``organization_graph``）的唯一检索入口。

实现已全部迁到 ``adapters/retrieval`` 的 LangChain 组件（VectorStore / BM25 /
加权 RRF）。本模块只保留两件事：
1. 入参校验（top_k / minimum_score）与嵌入契约（批量嵌入 + 向量校验）；
2. 把「多查询 × 混合检索」的结果按 chunk id 取最高分合并（任一查询召回的块
   都进入候选，避免长查询把词面分稀释后整体落选）。

对外签名保持不变，封存工作流无需改动。
"""

from __future__ import annotations

import math
from hashlib import sha256
from numbers import Real

from langchain_core.embeddings import Embeddings

from app.adapters.retrieval.embedding_contract import embed_queries, embed_query
from app.adapters.retrieval.errors import RetrievalConfigurationError
from app.adapters.retrieval.langchain_documents import (
    documents_to_ranked,
    staging_chunk_to_document,
)
from app.domain.framework.exam_points import ExamPoint
from app.domain.knowledge.relevance import RankedChunk, StagingChunk
from app.services.langchain_retrieval import (
    HybridRetrievalChain,
    build_memory_vector_store,
)


def _validate_configuration(*, top_k: int, minimum_score: float) -> None:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        raise RetrievalConfigurationError("top_k 必须是正整数")
    if (
        isinstance(minimum_score, bool)
        or not isinstance(minimum_score, Real)
        or not math.isfinite(float(minimum_score))
        or not 0 <= float(minimum_score) <= 1
    ):
        raise RetrievalConfigurationError("minimum_score 必须是 0 到 1 之间的有限数值")


def _chunk_key(chunk: StagingChunk) -> str:
    """确定性决胜键：优先 content_hash，退化为内容哈希。"""

    if getattr(chunk, "content_hash", None):
        return str(chunk.content_hash)
    return sha256(chunk.content.encode()).hexdigest()


class HybridStagingRetriever:
    """向量 + BM25 混合检索（加权 RRF），面向考点/问题两类查询。"""

    def __init__(
        self,
        *,
        embedder: Embeddings,
        top_k: int,
        minimum_score: float,
    ) -> None:
        _validate_configuration(top_k=top_k, minimum_score=minimum_score)
        self.embedder = embedder
        self.top_k = top_k
        self.minimum_score = float(minimum_score)

    def embed_query(self, exam_point: ExamPoint) -> list[float]:
        """考点检索的单查询嵌入（取 retrieval_intent 作文本）。"""

        return embed_query(self.embedder, exam_point.retrieval_intent)

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return embed_queries(self.embedder, texts)

    def _chain(self, chunks: list[StagingChunk]) -> HybridRetrievalChain:
        documents = [staging_chunk_to_document(chunk) for chunk in chunks]
        return HybridRetrievalChain(
            documents=documents,
            vector_store=build_memory_vector_store(chunks, self.embedder),
            top_k=self.top_k,
            score_threshold=self.minimum_score,
            embedder=self.embedder,
        )

    def retrieve(
        self,
        exam_point: ExamPoint,
        chunks: list[StagingChunk],
        *,
        query_vector: list[float] | None = None,
        query_text: str | None = None,
    ) -> list[RankedChunk]:
        if not chunks:
            return []
        documents = self._chain(chunks).search(
            query_text or exam_point.retrieval_intent,
            query_vector=query_vector,
        )
        return _finalize(documents_to_ranked(documents, chunks), self.top_k)

    def retrieve_multi(
        self,
        exam_point: ExamPoint,
        chunks: list[StagingChunk],
        *,
        query_vectors: list[list[float]],
        query_texts: list[str] | None = None,
    ) -> list[RankedChunk]:
        """多查询混合检索：各查询独立融合，按 chunk id 取最高分合并后截断 top_k。"""

        if not query_vectors or not chunks:
            return []
        if query_texts is None:
            query_texts = [exam_point.retrieval_intent] * len(query_vectors)
        chain = self._chain(chunks)
        merged: dict[str, RankedChunk] = {}
        for query_vector, query_text in zip(query_vectors, query_texts, strict=True):
            for item in documents_to_ranked(
                chain.search(query_text, query_vector=query_vector, limit=len(chunks)),
                chunks,
            ):
                existing = merged.get(item.chunk.id)
                if existing is None or item.score > existing.score:
                    merged[item.chunk.id] = item
        return _finalize(list(merged.values()), self.top_k)


def _finalize(items: list[RankedChunk], top_k: int) -> list[RankedChunk]:
    """分数量化 + 确定性排序 + 截断。

    RRF 只看排名，分数本身已不受嵌入尾级浮点噪声影响；量化到 3 位是为了让
    ``retrieval_score``（观测列）可读，且截断边界附近的顺序在 run 间稳定。
    """

    for item in items:
        item.score = round(item.score, 3)
    items.sort(
        key=lambda item: (-item.score, _chunk_key(item.chunk), item.chunk.id)
    )
    return items[:top_k]
