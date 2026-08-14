"""Provider-neutral document parsing contract."""

from __future__ import annotations

from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class ParseState(StrEnum):
    WAITING_FILE = "waiting_file"
    PENDING = "pending"
    RUNNING = "running"
    CONVERTING = "converting"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class ParseRequest:
    material_version_id: str
    filename: str
    content_type: str
    content_factory: Callable[[], AsyncIterable[bytes]]
    model_version: str = "vlm"


@dataclass(frozen=True)
class ParseSubmission:
    provider_batch_id: str


@dataclass(frozen=True)
class ParseProgress:
    state: ParseState
    data_id: str | None = None
    result_url: str | None = None
    error_summary: str | None = None
    trace_id: str | None = None
    raw_status: str | None = None


@dataclass(frozen=True)
class ParseArtifact:
    provider_batch_id: str
    content: bytes
    content_type: str | None = None


@dataclass(frozen=True)
class ContentBlock:
    block_index: int
    block_type: str
    text: str = ""
    markdown: str | None = None
    latex: str | None = None
    page_index: int | None = None
    bbox: list[float] | None = None
    heading_path: list[str] = field(default_factory=list)
    asset_reference: str | None = None
    reading_order: int | None = None
    content_hash: str = ""


class DocumentParser(Protocol):
    async def submit(self, request: ParseRequest) -> ParseSubmission: ...

    async def poll(self, provider_batch_id: str) -> ParseProgress: ...

    async def fetch(self, provider_batch_id: str) -> ParseArtifact: ...


class DocumentParserError(Exception):
    pass


class DocumentNetworkError(DocumentParserError):
    pass


class DocumentProtocolError(DocumentParserError):
    pass


class DocumentProviderError(DocumentParserError):
    pass
