"""加权 RRF 融合：把多个 Retriever 的结果按排名倒数求和合并。

选 RRF 而不是"加权分数相加"的原因：
1. 各通道分数量纲不可比——余弦相似度天然落在 [0,1]，BM25 得分是无界对数；
   分数相加必须逐通道归一，而归一系数会随语料变化漂移；
2. RRF 只看排名不看绝对分值，语料规模、长尾词频变化都不影响融合结果的
   稳定性，是与线上回归最不冲突的融合方式（TREC 长期结论）。

归一化：``score / max_possible``，``max_possible = Σw / (1 + c)``——即"每个通道
都把它排第 1"的满分。这样 ``score`` 仍落在 (0, 1]，与旧的 ``retrieval_score``
观测口径（0~1 分布）兼容，便于沿用既有的分数分布看板。
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict


class WeightedRRFRetriever(BaseRetriever):
    """``Σ weight_i / (rank_i + rank_constant)`` 的倒数排名融合。"""

    retrievers: list[BaseRetriever]
    weights: list[float]
    id_key: str = "chunk_id"
    top_k: int = 8
    rank_constant: int = 60

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def model_post_init(self, __context: Any) -> None:
        if len(self.retrievers) != len(self.weights):
            raise ValueError("retrievers 与 weights 数量必须一致")
        if any(weight < 0 for weight in self.weights):
            raise ValueError("weights 不能为负")

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun | None = None
    ) -> list[Document]:
        return weighted_reciprocal_rank(
            [retriever.invoke(query) for retriever in self.retrievers],
            weights=self.weights,
            top_k=self.top_k,
            id_key=self.id_key,
            rank_constant=self.rank_constant,
        )


def weighted_reciprocal_rank(
    channel_results: list[list[Document]],
    *,
    weights: list[float],
    top_k: int,
    id_key: str = "chunk_id",
    rank_constant: int = 60,
    score_budget: float | None = None,
) -> list[Document]:
    """融合核心（模块级函数，便于在已预计算向量/结果的场景直接复用）。

    ``score_budget`` 是归一化分母的权重预算，默认取 ``sum(weights)``。混合检索
    必须传**配置权重之和**（而不是"本次有命中的通道权重和"）：否则某次查询只有
    向量通道有命中时，它会被归一成满分 1.0，与"双通道都排第 1"的完美命中打平，
    多查询合并时主题块反而被 tie-break 挤掉。
    """

    fused: dict[str, float] = {}
    documents: dict[str, Document] = {}
    for channel, weight in zip(channel_results, weights, strict=False):
        if weight <= 0.0:
            continue
        for rank, document in enumerate(channel, start=1):
            doc_id = _document_id(document, id_key, rank)
            fused[doc_id] = fused.get(doc_id, 0.0) + weight / (rank + rank_constant)
            # 首次出现者承载更"干净"的原文与分项信号（后出现的可能已被
            # 上一段融合改写 metadata，但正文一致，不影响来源卡）。
            documents.setdefault(doc_id, document)

    budget = score_budget if score_budget is not None else sum(weights)
    max_possible = budget / (1.0 + rank_constant)
    ranked = sorted(fused.items(), key=lambda item: (-item[1], item[0]))
    results: list[Document] = []
    for doc_id, score in ranked[:top_k]:
        document = documents[doc_id]
        metadata = dict(document.metadata)
        metadata["score"] = score / max_possible if max_possible else 0.0
        metadata["fusion"] = "rrf"
        results.append(Document(page_content=document.page_content, metadata=metadata))
    return results


def _document_id(document: Document, id_key: str, rank: int) -> str:
    value = document.metadata.get(id_key)
    if isinstance(value, str) and value:
        return value
    return f"{rank}:{hash(document.page_content)}"
