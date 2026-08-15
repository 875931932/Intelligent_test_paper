"""Shared metadata for redacted model-call observability."""

from __future__ import annotations

from pydantic import BaseModel


class ModelCallContext(BaseModel):
    course_id: str
    framework_build_run_id: str | None = None
    organization_run_id: str | None = None
    generation_attempt_id: str | None = None
    stage: str
