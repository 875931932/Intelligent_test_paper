"""Durable, redacted recording for final model-call outcomes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from sqlalchemy import text
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

    def lookup_response(
        self,
        *,
        model: str,
        prompt_hash: str,
    ) -> dict[str, Any] | None:
        """按 (model, prompt_hash) 查找历史成功响应。

        仅用于 temperature=0 的确定性调用（分类/归并/提取等知识构建类），
        命中返回完整响应 dict；查询异常一律视为未命中，不阻断真实调用。
        命中记录的 details 中会写入 cache_hit=True，不作为后续缓存的源。
        """
        session = self.session_factory()
        try:
            row = session.execute(
                text(
                    """
                    SELECT details->>'response' AS response
                    FROM model_calls
                    WHERE model = :model
                      AND prompt_hash = :prompt_hash
                      AND status = 'succeeded'
                      AND details->>'response' IS NOT NULL
                      AND (details->>'cache_hit') IS NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"model": model, "prompt_hash": prompt_hash},
            ).scalar_one_or_none()
        except Exception:
            session.rollback()
            return None
        finally:
            session.close()
        if not isinstance(row, str):
            return None
        try:
            import json

            parsed = json.loads(row)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
