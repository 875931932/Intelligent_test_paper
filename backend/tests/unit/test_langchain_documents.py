"""StagingChunk / RankedChunk 与 LangChain Document 的边界转换。"""

from __future__ import annotations

import pytest

from app.adapters.retrieval.langchain_documents import (
    documents_to_ranked,
    staging_chunk_to_document,
)
from app.domain.knowledge.relevance import RankedChunk, StagingChunk


def _chunk() -> StagingChunk:
    return StagingChunk(
        id="blk-1",
        material_version_id="v1",
        content="监督学习分为分类与回归。",
        locator={
            "material_id": "m1",
            "material_name": "教学大纲",
            "page_index": 2,
            "heading_path": ["第3章", "3.1 监督学习"],
        },
        embedding=[1.0, 0.0],
    )


def test_staging_chunk_maps_content_and_locator_metadata():
    chunk = _chunk()

    document = staging_chunk_to_document(chunk)

    assert document.page_content == chunk.content
    assert document.metadata == {
        "chunk_id": "blk-1",
        "material_version_id": "v1",
        "material_id": "m1",
        "material_name": "教学大纲",
        "page_index": 2,
        "heading_path": ["第3章", "3.1 监督学习"],
    }
    assert "embedding" not in document.metadata


def test_heading_path_is_copied_not_shared():
    chunk = _chunk()

    document = staging_chunk_to_document(chunk)
    document.metadata["heading_path"].append("3.1.1")

    assert chunk.locator["heading_path"] == ["第3章", "3.1 监督学习"]


def test_page_index_none_is_preserved_not_stringified():
    chunk = StagingChunk(
        id="blk-2",
        material_version_id="v2",
        content="无页码正文",
        locator={"material_id": "m2", "heading_path": None},
    )

    document = staging_chunk_to_document(chunk)

    assert document.metadata["page_index"] is None
    assert document.metadata["heading_path"] == []


def test_documents_round_trip_to_ranked_chunks():
    chunk = _chunk()
    ranked = RankedChunk(chunk=chunk, score=0.88, lexical_score=0.25, semantic_score=0.9)
    document = staging_chunk_to_document(chunk)
    document.metadata.update(
        {"score": ranked.score, "lexical_score": ranked.lexical_score,
         "semantic_score": ranked.semantic_score}
    )

    assert documents_to_ranked([document], [chunk]) == [ranked]


def test_non_numeric_scores_are_normalized_to_float():
    """metadata 经过 JSON/回调层后可能是字符串，还原时统一成 float。"""

    chunk = _chunk()
    document = staging_chunk_to_document(chunk)
    document.metadata.update(
        {"score": "0.5", "lexical_score": "0.2", "semantic_score": "0.6"}
    )

    restored = documents_to_ranked([document], [chunk])

    assert restored[0].score == pytest.approx(0.5)
    assert restored[0].lexical_score == pytest.approx(0.2)
    assert restored[0].semantic_score == pytest.approx(0.6)


def test_unknown_chunk_id_is_skipped():
    """SQL 检索可能返回已被装载层过滤掉的块 id——回捞不到就丢弃，不造空块。"""

    document = staging_chunk_to_document(_chunk())
    document.metadata["chunk_id"] = "ghost"

    assert documents_to_ranked([document], [_chunk()]) == []
