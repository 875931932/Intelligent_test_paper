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
    assert first[1].heading_path == ["标题"]
