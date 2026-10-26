"""PaperPact 自定义 LangChain Retriever：委托既有确定性混合检索核心。

排序、阈值、去重、SQL 语义下推与资料范围过滤全部沿用
``staging_retrieval_service`` 的实现；本模块只负责把入参映射到 LangChain
``Document`` 边界，不复制打分算法，也不在检索器内做邻域扩展。
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks.manager import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict

from app.adapters.retrieval.langchain_documents import ranked_chunk_to_document
from app.domain.framework.exam_points import ExamPoint
from app.domain.knowledge.relevance import StagingChunk
from app.services.staging_retrieval_service import (
    RankedChunk,
    retrieve_for_exam_point,
    retrieve_for_question,
    retrieve_multi_for_question,
)


class PaperPactHybridRetriever(BaseRetriever):
    chunks: list[StagingChunk]
    # EmbeddingClient 是未 runtime_checkable 的 Protocol，pydantic 无法做实例校验；
    # 用 Any 保留原网关（批量限制/响应校验仍由网关负责）。
    embedder: Any = None
    top_k: int
    minimum_score: float
    semantic_scores: dict[str, float] | None = None
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: CallbackManagerForRetrieverRun,
    ) -> list[Document]:
        return [ranked_chunk_to_document(item) for item in self.retrieve_ranked(query)]

    def retrieve_ranked(self, question: str) -> list[RankedChunk]:
        return retrieve_for_question(
            question,
            self.chunks,
            self.embedder,
            top_k=self.top_k,
            minimum_score=self.minimum_score,
            semantic_scores=self.semantic_scores,
        )

    def retrieve_variants(
        self,
        question_variants: list[str],
        *,
        semantic_scores: list[dict[str, float]] | None = None,
    ) -> list[Document]:
        return [
            ranked_chunk_to_document(item)
            for item in self.retrieve_variants_ranked(
                question_variants, semantic_scores=semantic_scores
            )
        ]

    def retrieve_variants_ranked(
        self,
        question_variants: list[str],
        *,
        semantic_scores: list[dict[str, float]] | None = None,
    ) -> list[RankedChunk]:
        return retrieve_multi_for_question(
            question_variants,
            self.chunks,
            self.embedder,
            top_k=self.top_k,
            minimum_score=self.minimum_score,
            semantic_scores=semantic_scores,
        )

    def retrieve_exam_point(
        self,
        point: ExamPoint,
        chunks: list[StagingChunk],
        *,
        query_vector: list[float] | None = None,
        query_text: str | None = None,
    ) -> list[RankedChunk]:
        """知识组织考点检索入口：返回领域内部 RankedChunk，调用方无需改造。"""

        return retrieve_for_exam_point(
            point,
            chunks,
            self.embedder,
            top_k=self.top_k,
            minimum_score=self.minimum_score,
            query_vector=query_vector,
            query_text=query_text,
        )
