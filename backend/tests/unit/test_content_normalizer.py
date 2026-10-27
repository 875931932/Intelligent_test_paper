from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.adapters.document.local_text_parser import parse_local_text
from app.adapters.document.protocol import DocumentProtocolError
from app.services.document_processing_service import normalize_content_list, read_mineru_zip


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return buffer.getvalue()


def test_normalizer_preserves_structure_and_heading_path():
    blocks = normalize_content_list([
        {"type": "title", "text": "第一章", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "核心概念", "page_idx": 1, "bbox": [1, 2, 3, 4]},
        {"type": "formula", "latex": "x^2", "page_idx": 1},
    ])
    assert [block.block_type for block in blocks] == ["title", "paragraph", "equation"]
    assert blocks[1].heading_path == ["第一章"]
    assert blocks[1].page_index == 1
    assert len(blocks[1].content_hash) == 64


def test_zip_requires_content_list_and_returns_artifacts():
    payload = json.dumps([{"type": "text", "text": "知识"}], ensure_ascii=False).encode()
    parsed = read_mineru_zip(_zip({"result/content_list.json": payload, "result/full.md": b"# title"}))
    assert parsed.blocks[0].text == "知识"
    assert "result/full.md" in parsed.artifacts


@pytest.mark.parametrize("name", ["../escape.txt", "/absolute.txt", "safe/../../escape.txt", "C:\\escape.txt"])
def test_zip_rejects_path_traversal(name):
    with pytest.raises(DocumentProtocolError):
        read_mineru_zip(_zip({name: b"bad", "content_list.json": b"[]"}))


def test_local_markdown_parser_is_deterministic():
    first = parse_local_text("# 标题\n正文".encode(), markdown=True)
    second = parse_local_text("# 标题\n正文".encode(), markdown=True)
    assert first == second


def test_local_markdown_aggregates_section_and_keeps_headings_in_body():
    """同一章节聚合成块：标题保留在正文（参与嵌入），heading_path 记层级。"""

    blocks = parse_local_text(
        "# 第一章 绪论\n\n检索增强生成把检索与生成结合。\n\n## 1.1 背景\n正文\n".encode(),
        markdown=True,
    )

    assert [block.heading_path for block in blocks] == [["第一章 绪论"], ["第一章 绪论", "1.1 背景"]]
    # 标题与其正文同块：不再有只含标题的独立块
    assert blocks[0].text == "# 第一章 绪论  \n检索增强生成把检索与生成结合。"
    assert blocks[1].text == "## 1.1 背景\n正文"
    assert [block.block_type for block in blocks] == ["paragraph", "paragraph"]
    assert [block.block_index for block in blocks] == [0, 1]
    assert [block.reading_order for block in blocks] == [0, 1]
    assert blocks[1].markdown == blocks[1].text
    assert len(blocks[1].content_hash) == 64


def test_local_markdown_tracks_full_heading_hierarchy():
    """# ~ ###### 逐级进 heading_path；同级回退时截断更深层级。

    注意：只有标题、没有正文的章节不会被切分成块（空章节无检索价值），
    所以这里每级标题都带一行正文。
    """

    blocks = parse_local_text(
        "# L1\n\n一\n\n## L2\n\n二\n\n### L3\n\n三\n\n"
        "#### L4\n\n四\n\n##### L5\n\n五\n\n###### L6\n\n六\n\n# L1b\n\n七\n".encode(),
        markdown=True,
    )

    assert [block.heading_path for block in blocks] == [
        ["L1"],
        ["L1", "L2"],
        ["L1", "L2", "L3"],
        ["L1", "L2", "L3", "L4"],
        ["L1", "L2", "L3", "L4", "L5"],
        ["L1", "L2", "L3", "L4", "L5", "L6"],
        ["L1b"],
    ]


def test_local_markdown_keeps_heading_only_sections_as_blocks():
    """只有标题的章节仍产一个块（正文即标题行），标题本身可参与嵌入。"""

    blocks = parse_local_text("# 空章节\n\n# 有正文\n正文\n".encode(), markdown=True)

    assert [(block.heading_path, block.text) for block in blocks] == [
        (["空章节"], "# 空章节"),
        (["有正文"], "# 有正文\n正文"),
    ]


def test_local_markdown_merges_consecutive_headings_into_deepest_section():
    """连续标题之间没有正文时合并为一块，heading_path 取最深层级。"""

    blocks = parse_local_text("# L1\n\n## L2\n\n### L3\n\n# L1b\n\n正文\n".encode(), markdown=True)

    assert [(block.heading_path, block.text) for block in blocks] == [
        (["L1", "L2", "L3"], "# L1  \n## L2  \n### L3"),
        (["L1b"], "# L1b  \n正文"),
    ]


def test_local_markdown_splits_long_section_and_keeps_metadata():
    """超长章节按长度切块，各块继承章节标题元数据。"""

    body = "句子。" * 900
    blocks = parse_local_text(f"# 长章节\n{body}\n".encode(), markdown=True)

    assert len(blocks) > 1
    assert [block.block_index for block in blocks] == list(range(len(blocks)))
    assert {tuple(block.heading_path) for block in blocks} == {("长章节",)}
    # 单块不超过切分粒度，且首块带标题、后续块是同一章节的续写
    assert all(len(block.text) <= 1600 for block in blocks)
    assert blocks[0].text.startswith("# 长章节")


def test_local_markdown_without_headings_stays_single_block():
    blocks = parse_local_text("没有标题的一段话".encode(), markdown=True)

    assert len(blocks) == 1
    assert blocks[0].heading_path == []
    assert blocks[0].text == "没有标题的一段话"


def test_local_plain_text_behavior_unchanged():
    """TXT 行为不变：一行一块、不设 markdown、无 heading_path、忽略空行。"""

    blocks = parse_local_text("纯文本笔记\n\n第二行\n".encode(), markdown=False)

    assert [(b.block_index, b.block_type, b.text, b.markdown, b.heading_path) for b in blocks] == [
        (0, "paragraph", "纯文本笔记", None, []),
        (1, "paragraph", "第二行", None, []),
    ]


def test_local_parser_rejects_non_utf8():
    with pytest.raises(DocumentProtocolError):
        parse_local_text(b"\xff\xfe\x00bad", markdown=True)
