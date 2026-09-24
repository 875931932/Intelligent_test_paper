"""_merged_evidence_blocks 的长度上限与超长块切分（P1 粒度修复）。"""

from app.services.knowledge_publish_service import (
    _EVIDENCE_MERGE_MAX_CHARS,
    _EVIDENCE_MERGE_MIN_CHARS,
    _merged_evidence_blocks,
)


def _block(text: str, *, heading: str | None = None, index: int = 0) -> dict:
    return {
        "id": f"block-{index}",
        "text": text,
        "heading_path": [heading] if heading else [],
        "page_index": index,
        "bbox": [0, 0, 1, 1],
        "reading_order": index,
        "block_type": "text",
        "content_hash": "h" * 64,
    }


def test_small_fragments_still_merge_into_one_bounded_chunk():
    blocks = [_block(f"碎块内容{i}", index=i) for i in range(10)]

    merged = _merged_evidence_blocks(blocks)

    assert len(merged) == 1
    merged_block, sources = merged[0]
    assert len(sources) == 10
    assert all(len(text) <= _EVIDENCE_MERGE_MAX_CHARS for text in [merged_block["text"]])


def test_standalone_block_under_limit_keeps_original_text():
    text = "长内容" * ((_EVIDENCE_MERGE_MIN_CHARS + 10) // 4)

    merged = _merged_evidence_blocks([_block(text)])

    assert len(merged) == 1
    merged_block, sources = merged[0]
    assert merged_block["text"] == text
    assert sources == [sources[0]]
    assert len(text) < _EVIDENCE_MERGE_MAX_CHARS


def test_oversized_block_is_split_on_sentence_boundaries():
    sentence = "这是一个用于测试超长块按句末标点切分的完整句子。"
    text = sentence * ((_EVIDENCE_MERGE_MAX_CHARS * 2) // len(sentence) + 1)

    merged = _merged_evidence_blocks([_block(text, heading="第一章", index=7)])

    assert len(merged) >= 2
    texts = [merged_block["text"] for merged_block, _ in merged]
    assert all(0 < len(item) <= _EVIDENCE_MERGE_MAX_CHARS for item in texts)
    # 除最后一段外都在句末标点处断开
    assert all(item.endswith("。") for item in texts[:-1])
    # 溯源信息按块保留
    for merged_block, sources in merged:
        assert merged_block["heading_path"] == ["第一章"]
        assert merged_block["page_index"] == 7
        assert len(sources) == 1


def test_oversized_line_without_delimiters_hard_splits_without_loss():
    text = "x" * (_EVIDENCE_MERGE_MAX_CHARS * 2 + 100)

    merged = _merged_evidence_blocks([_block(text)])

    texts = [merged_block["text"] for merged_block, _ in merged]
    assert len(texts) == 3
    assert all(len(item) <= _EVIDENCE_MERGE_MAX_CHARS for item in texts)
    assert "".join(texts) == text


def test_oversized_block_does_not_swallow_following_fragments():
    text = "超长内容。" * (_EVIDENCE_MERGE_MAX_CHARS // 5 + 10)
    giant = _block(text, index=0)
    tail = _block("尾随碎块", index=1)

    merged = _merged_evidence_blocks([giant, tail])

    texts = [merged_block["text"] for merged_block, _ in merged]
    assert all(len(item) <= _EVIDENCE_MERGE_MAX_CHARS for item in texts)
    assert texts[-1] == "尾随碎块"
