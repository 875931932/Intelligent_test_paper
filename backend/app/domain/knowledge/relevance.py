"""Small contracts shared by staging retrieval and later relevance analysis."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class StagingChunk(BaseModel):
    """A parsed material chunk that has not yet been published to the knowledge base."""

    id: str
    material_version_id: str
    content: str = Field(min_length=1)
    locator: dict[str, Any] = Field(default_factory=dict)
