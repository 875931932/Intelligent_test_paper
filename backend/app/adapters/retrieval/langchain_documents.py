"""One-way mapping from PaperPact retrieval results to LangChain ``Document``.

正文、资料定位与检索分数是来源卡和 prompt 的权威数据；向量不进入 metadata，
避免来源卡与生成上下文携带大字段。
"""

from __future__ import annotations

from typing import Any

from langchain_core.documents import Document

from app.domain.knowledge.relevance import StagingChunk
from app.services.staging_retrieval_service import RankedChunk


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


def ranked_chunk_to_document(item: RankedChunk) -> Document:
    metadata = _chunk_metadata(item.chunk)
    metadata.update(
        {
            "score": item.score,
            "lexical_score": item.lexical_score,
            "semantic_score": item.semantic_score,
        }
    )
    return Document(page_content=item.chunk.content, metadata=metadata)


def document_metadata(document: Document) -> dict[str, Any]:
    return dict(document.metadata)


def documents_to_ranked(
    documents: list[Document], chunks: list[StagingChunk]
) -> list[RankedChunk]:
    """把 Retriever 返回的 Document 还原成领域内部结果（供来源卡与邻域扩展使用）。"""

    by_id = {chunk.id: chunk for chunk in chunks}
    return [
        RankedChunk(
            chunk=by_id[metadata["chunk_id"]],
            score=float(metadata["score"]),
            lexical_score=float(metadata["lexical_score"]),
            semantic_score=float(metadata["semantic_score"]),
        )
        for metadata in (document_metadata(document) for document in documents)
    ]
