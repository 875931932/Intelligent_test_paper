"""PaperPact 领域对象 ↔ LangChain ``Document`` 的双向边界转换。

正文、资料定位与检索分数是来源卡和 prompt 的权威数据；向量不进入 metadata，
避免来源卡与生成上下文携带大字段。
"""

from __future__ import annotations

from typing import Any

from langchain_core.documents import Document

from app.domain.knowledge.relevance import RankedChunk, StagingChunk


def _chunk_metadata(chunk: StagingChunk) -> dict[str, Any]:
    return {
        "chunk_id": chunk.id,
        "material_version_id": chunk.material_version_id,
        "material_id": chunk.locator.get("material_id"),
        "material_name": chunk.locator.get("material_name"),
        # 无页码定位（md/txt）保留 None，不能变成字符串 "None"。
        "page_index": chunk.locator.get("page_index"),
        "heading_path": [str(h) for h in (chunk.locator.get("heading_path") or [])],
    }


def staging_chunk_to_document(chunk: StagingChunk) -> Document:
    return Document(page_content=chunk.content, metadata=_chunk_metadata(chunk))


def documents_to_ranked(
    documents: list[Document], chunks: list[StagingChunk]
) -> list[RankedChunk]:
    """把 Retriever 返回的 Document 还原成领域内部结果（供来源卡与邻域扩展使用）。

    分项信号（词面/语义）可能只由单一通道产生，缺失时记 0.0；排序权威始终是
    融合分 ``score``。
    """

    by_id = {chunk.id: chunk for chunk in chunks}
    results: list[RankedChunk] = []
    for document in documents:
        metadata = dict(document.metadata)
        chunk = by_id.get(str(metadata.get("chunk_id") or ""))
        if chunk is None:
            continue
        results.append(
            RankedChunk(
                chunk=chunk,
                score=float(metadata.get("score") or 0.0),
                lexical_score=float(metadata.get("lexical_score") or 0.0),
                semantic_score=float(metadata.get("semantic_score") or 0.0),
            )
        )
    return results
