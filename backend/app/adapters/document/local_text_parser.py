"""Deterministic parser for plain text and Markdown materials."""

from __future__ import annotations

import hashlib

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from app.adapters.document.protocol import ContentBlock, DocumentProtocolError

# Markdown 标题层级：# ~ ###### 依次映射到块元数据 h1 ~ h6
_MARKDOWN_HEADERS = [("#" * level, f"h{level}") for level in range(1, 7)]

# 过长章节的切块粒度（字符数）。1600/200 是初始试验值，按实际资料的检索效果再调。
_MARKDOWN_CHUNK_SIZE = 1600
_MARKDOWN_CHUNK_OVERLAP = 200


def parse_local_text(content: bytes, *, markdown: bool) -> list[ContentBlock]:
    """本地文本链路唯一入口：Markdown 与纯文本分成两条内部路径。"""

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentProtocolError("text file must be UTF-8") from exc
    if markdown:
        return _parse_markdown(text)
    return _parse_plain_text(text)


def _parse_plain_text(text: str) -> list[ContentBlock]:
    """TXT：沿用原有按行解析，一行一个段落块，无标题层级。"""

    blocks: list[ContentBlock] = []
    for raw in text.splitlines():
        value = raw.strip()
        if not value:
            continue
        blocks.append(
            ContentBlock(
                block_index=len(blocks),
                block_type="paragraph",
                text=value,
                markdown=None,
                heading_path=[],
                reading_order=len(blocks),
                content_hash=hashlib.sha256(value.encode("utf-8")).hexdigest(),
            )
        )
    return blocks


def _parse_markdown(text: str) -> list[ContentBlock]:
    """Markdown：先按标题聚合章节，再切分过长章节。

    strip_headers=False 是有意设置的——索引取块正文生成向量，标题必须留在正文里，
    否则只有 heading_path 元数据、标题却没有参与嵌入。标题与其正文同块落为
    paragraph，heading_path 仍保存完整层级；不再为每个标题单独创建只含标题的向量块。
    """

    sections = MarkdownHeaderTextSplitter(
        headers_to_split_on=_MARKDOWN_HEADERS,
        return_each_line=False,
        strip_headers=False,
    ).split_text(text)
    chunks = RecursiveCharacterTextSplitter(
        chunk_size=_MARKDOWN_CHUNK_SIZE,
        chunk_overlap=_MARKDOWN_CHUNK_OVERLAP,
    ).split_documents(sections)

    blocks: list[ContentBlock] = []
    for document in chunks:
        value = document.page_content.strip()
        if not value:
            continue
        heading_path = [
            document.metadata[key]
            for key in (f"h{level}" for level in range(1, 7))
            if document.metadata.get(key)
        ]
        blocks.append(
            ContentBlock(
                block_index=len(blocks),
                block_type="paragraph",
                text=value,
                markdown=value,
                heading_path=heading_path,
                reading_order=len(blocks),
                content_hash=hashlib.sha256(value.encode("utf-8")).hexdigest(),
            )
        )
    return blocks