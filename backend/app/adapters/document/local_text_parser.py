"""Deterministic parser for plain text and Markdown materials."""

from __future__ import annotations

import hashlib

from app.adapters.document.protocol import ContentBlock, DocumentProtocolError


def parse_local_text(content: bytes, *, markdown: bool) -> list[ContentBlock]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentProtocolError("text file must be UTF-8") from exc
    blocks: list[ContentBlock] = []
    headings: list[str] = []
    for raw in text.splitlines():
        value = raw.strip()
        if not value:
            continue
        block_type = "paragraph"
        if markdown and value.startswith("#"):
            level = len(value) - len(value.lstrip("#"))
            title = value[level:].strip()
            if title:
                headings[level - 1 :] = [title]
                value = title
                block_type = "title"
        blocks.append(
            ContentBlock(
                block_index=len(blocks),
                block_type=block_type,
                text=value,
                markdown=raw if markdown else None,
                heading_path=list(headings),
                reading_order=len(blocks),
                content_hash=hashlib.sha256(value.encode("utf-8")).hexdigest(),
            )
        )
    return blocks
