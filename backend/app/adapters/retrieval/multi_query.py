"""多查询检索（Multi-Query 的确定性版本）。

LangChain 的 ``MultiQueryRetriever`` 用 LLM 生成查询变体；本项目的变体是**确定性
改写**（原问题 + 剥掉疑问框架的主题串），不额外烧一次模型调用，也不引入
LLM 输出的不确定性。因此这里保留 ``MultiQueryRetriever`` 的"多路召回 + 排名融合"
骨架，把"变体从哪来"抽成可注入的 ``variant_fn``。

合并期按归一文本折叠近重复：跨文档模板碎片（表头「教学班：____」之类，同一
课程里可有十几份同文副本）只保留得分最高的一份，正文块才有机会进 top_k。
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict

from app.adapters.retrieval.tokenization import dedup_key


class MultiVariantRetriever(BaseRetriever):
    """对同一问题展开多个确定性变体，各自召回后做 RRF 合并与同文折叠。"""

    retriever: BaseRetriever
    variant_fn: Callable[[str], list[str]]
    top_k: int = 6
    id_key: str = "chunk_id"
    rank_constant: int = 60

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun | None = None
    ) -> list[Document]:
        variants = [variant for variant in dict.fromkeys(self.variant_fn(query)) if variant.strip()]
        if not variants:
            return []

        fused: dict[str, float] = {}
        documents: dict[str, Document] = {}
        for variant in variants:
            for rank, document in enumerate(self.retriever.invoke(variant), start=1):
                doc_id = str(document.metadata.get(self.id_key) or f"{rank}:{hash(document.page_content)}")
                fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (rank + self.rank_constant)
                documents.setdefault(doc_id, document)

        max_possible = len(variants) / (1.0 + self.rank_constant)
        ranked = sorted(fused.items(), key=lambda item: (-item[1], item[0]))

        results: list[Document] = []
        seen_contents: set[str] = set()
        for doc_id, score in ranked:
            document = documents[doc_id]
            content_key = dedup_key(document.page_content)
            if content_key in seen_contents:
                continue
            seen_contents.add(content_key)
            metadata = dict(document.metadata)
            metadata["score"] = score / max_possible if max_possible else 0.0
            metadata["query_variants"] = len(variants)
            results.append(Document(page_content=document.page_content, metadata=metadata))
            if len(results) >= self.top_k:
                break
        return results
