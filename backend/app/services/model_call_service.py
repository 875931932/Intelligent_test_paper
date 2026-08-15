"""Durable, redacted recording for final model-call outcomes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.db.schema import model_calls
from app.domain.model_calls import ModelCallContext


class DatabaseModelCallRecorder:
    """Write one row for the final success or failure of a logical model call."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        if not callable(session_factory):
            raise TypeError("DatabaseModelCallRecorder requires an owned session factory")
        self.session_factory = session_factory

    def record(
        self,
        *,
        context: ModelCallContext,
        provider: str,
        model: str,
        status: str,
        prompt_hash: str,
        input_tokens: int | None,
        output_tokens: int | None,
        duration_ms: int,
        error_code: str | None,
        error_message: str | None,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        session = self.session_factory()
        try:
            session.execute(
                model_calls.insert().values(
                    id=uuid4().hex,
                    course_id=context.course_id,
                    generation_attempt_id=context.generation_attempt_id,
                    framework_build_run_id=context.framework_build_run_id,
                    organization_run_id=context.organization_run_id,
                    stage=context.stage,
                    provider=provider,
                    model=model,
                    status=status,
                    request_id=request_id,
                    prompt_hash=prompt_hash,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=duration_ms,
                    error_code=error_code,
                    error_message=error_message,
                    details=details or {},
                )
            )
            session.commit()
        finally:
            session.close()
