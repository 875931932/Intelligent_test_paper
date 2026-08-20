from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from app.domain.model_calls import ModelCallContext


_PERSISTED_ERROR_MESSAGES = {
    "deepseek_http_error": "DeepSeek request failed with an HTTP error",
    "deepseek_transport_error": "DeepSeek request failed",
    "model_empty_response": "model returned empty content",
    "model_invalid_envelope": "model response envelope is invalid",
    "model_non_json_response": "model returned content that is not valid JSON",
    "model_non_object_response": "model returned a non-object JSON value",
    "model_output_evidence_gap": "model response failed evidence validation",
    "model_output_scope_violation": "model response failed scope validation",
    "model_schema_validation_failed": "model JSON does not match the required schema",
}
_PERSISTED_VALIDATION_FIELDS = {
    "ability_requirements",
    "alignment_keys",
    "allowed_question_types",
    "anchors",
    "assessable_content",
    "assessment_anchor_keys",
    "assessment_orientations",
    "assessment_requirement",
    "assessment_units",
    "candidate_assessment_unit",
    "candidate_card_content",
    "cards",
    "code",
    "cognitive_targets",
    "confidence",
    "content_kind",
    "decisions",
    "depth",
    "evidence_chunk_id",
    "evidence_chunk_ids",
    "evidence_role",
    "exam_point_code",
    "exam_points",
    "exam_weight",
    "excluded_content",
    "final_exam_rules",
    "importance",
    "key",
    "material_version_id",
    "name",
    "operational_detail_policy",
    "performance_statement",
    "priority",
    "prompt_material",
    "relevance_class",
    "required_evidence_roles",
    "requirements",
    "retrieval_intent",
    "scope_boundary",
    "source_locations",
    "source_locator",
    "status",
    "support_claim",
    "teaching_anchor_keys",
    "teaching_topics",
    "title",
    "weight_group_id",
    "weight_source",
    "weight_value",
}


class ModelCallRecorder(Protocol):
    def record(self, **values: Any) -> None: ...


class DeepSeekModelError(RuntimeError):
    """A safe model failure that can be persisted or returned to a workflow."""

    def __init__(self, error_code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


class DeepSeekGatewayError(DeepSeekModelError):
    """Backward-compatible gateway error name."""


class DeepSeekJsonClient:
    """OpenAI-compatible strict JSON client with final-outcome observability."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.xiaomimimo.com/v1",
        model: str = "mimo-v2.5-pro",
        timeout: float = 90.0,
        max_attempts: int = 4,
        client: httpx.Client | None = None,
        recorder: ModelCallRecorder | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("LLM API key is required")
        if not base_url.strip():
            raise ValueError("LLM base URL is required")
        if not model.strip():
            raise ValueError("LLM model is required")
        if max_attempts < 1:
            raise ValueError("DeepSeek max_attempts must be positive")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.client = client
        self.recorder = recorder

    def request_json(
        self,
        *,
        system_prompt: str,
        payload: Any,
        temperature: float,
        call_context: ModelCallContext | None = None,
        response_validator: Callable[[dict], None] | None = None,
    ) -> dict:
        prompt = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else dict(payload)
        canonical_prompt = json.dumps(prompt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        prompt_hash = hashlib.sha256(f"{system_prompt}\n{canonical_prompt}".encode()).hexdigest()
        started = time.perf_counter()
        attempts: list[dict[str, Any]] = []
        last_error: DeepSeekModelError | None = None
        request_id: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        final_http_status: int | None = None
        last_retry_error_code: str | None = None
        attempt_count = 0

        for attempt in range(1, self.max_attempts + 1):
            attempt_count = attempt
            request_id = None
            input_tokens = None
            output_tokens = None
            final_http_status = None
            should_retry = True
            try:
                response = self._post(system_prompt, canonical_prompt, temperature)
                headers = getattr(response, "headers", {})
                request_id = headers.get("x-request-id") if hasattr(headers, "get") else None
                status_code = getattr(response, "status_code", None)
                final_http_status = status_code if isinstance(status_code, int) else 200
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise DeepSeekModelError("model_invalid_envelope", "model response envelope is invalid")
                request_id = request_id or _optional_text(body.get("id"))
                usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
                input_tokens = _optional_int(usage.get("prompt_tokens"))
                output_tokens = _optional_int(usage.get("completion_tokens"))
                try:
                    content = body["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError):
                    raise DeepSeekModelError(
                        "model_invalid_envelope",
                        "model response is missing message content",
                    ) from None
                if not isinstance(content, str) or not content.strip():
                    raise DeepSeekModelError("model_empty_response", "model returned empty content")
                try:
                    result = json.loads(content)
                except json.JSONDecodeError:
                    raise DeepSeekModelError(
                        "model_non_json_response",
                        "model returned content that is not valid JSON",
                    ) from None
                if not isinstance(result, dict):
                    raise DeepSeekModelError(
                        "model_non_object_response",
                        "model returned a non-object JSON value",
                    )
                if response_validator is not None:
                    response_validator(result)
            except httpx.HTTPStatusError as exc:
                last_error = DeepSeekModelError(
                    "deepseek_http_error",
                    f"DeepSeek request failed with HTTP status {exc.response.status_code}",
                )
                should_retry = _is_retryable_http_status(exc.response.status_code)
                last_retry_error_code = last_error.error_code
                attempts.append(
                    {
                        "attempt": attempt,
                        "http_status": exc.response.status_code,
                        "error_code": last_error.error_code,
                    }
                )
            except httpx.HTTPError:
                last_error = DeepSeekModelError(
                    "deepseek_transport_error",
                    "DeepSeek request failed",
                )
                last_retry_error_code = last_error.error_code
                attempts.append({"attempt": attempt, "error_code": last_error.error_code})
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                last_error = DeepSeekModelError(
                    "model_invalid_envelope",
                    "model response envelope is invalid",
                )
                last_retry_error_code = last_error.error_code
                attempts.append({"attempt": attempt, "error_type": type(exc).__name__})
            except DeepSeekModelError as exc:
                last_error = exc
                persisted_error_code, _ = _persistence_error(exc)
                should_retry = True
                last_retry_error_code = persisted_error_code
                attempts.append(
                    {
                        "attempt": attempt,
                        "http_status": final_http_status,
                        "error_code": persisted_error_code,
                    }
                )
            else:
                duration_ms = round((time.perf_counter() - started) * 1000)
                self._record(
                    context=call_context,
                    status="succeeded",
                    prompt_hash=prompt_hash,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=duration_ms,
                    error=None,
                    request_id=request_id,
                    details={
                        "attempt_count": attempt,
                        "retry_count": attempt - 1,
                        "final_http_status": final_http_status,
                        "last_error_code": last_retry_error_code,
                        "attempts": attempts,
                    },
                )
                return result

            if not should_retry:
                break
            if attempt < self.max_attempts:
                time.sleep(min(2 ** (attempt - 1), 8))

        assert last_error is not None
        duration_ms = round((time.perf_counter() - started) * 1000)
        details = {
            "attempt_count": attempt_count,
            "retry_count": attempt_count - 1,
            "final_http_status": final_http_status,
            "last_error_code": _persistence_error(last_error)[0],
            "attempts": attempts,
        }
        validation_details = _sanitized_validation_details(last_error)
        if validation_details is not None:
            details["validation"] = validation_details
        self._record(
            context=call_context,
            status="failed",
            prompt_hash=prompt_hash,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            error=last_error,
            request_id=request_id,
            details=details,
        )
        raise DeepSeekGatewayError(last_error.error_code, str(last_error), details=details) from last_error

    def _post(self, system_prompt: str, canonical_prompt: str, temperature: float) -> httpx.Response:
        request = {
            "headers": {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            "json": {
                "model": self.model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": canonical_prompt},
                ],
                "response_format": {"type": "json_object"},
            },
            "timeout": self.timeout,
        }
        if self.client is not None:
            return self.client.post(f"{self.base_url}/chat/completions", **request)
        return httpx.post(f"{self.base_url}/chat/completions", **request)

    def _record(
        self,
        *,
        context: ModelCallContext | None,
        status: str,
        prompt_hash: str,
        input_tokens: int | None,
        output_tokens: int | None,
        duration_ms: int,
        error: DeepSeekModelError | None,
        request_id: str | None,
        details: dict[str, Any],
    ) -> None:
        if self.recorder is None or context is None:
            return
        error_code, error_message = _persistence_error(error) if error else (None, None)
        try:
            self.recorder.record(
                context=context,
                provider="deepseek",
                model=self.model,
                status=status,
                prompt_hash=prompt_hash,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=duration_ms,
                error_code=error_code,
                error_message=error_message,
                request_id=request_id,
                details=details,
            )
        except Exception:
            # Observability is deliberately best-effort and must never alter model-call semantics.
            return


class DeepSeekGateway:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.xiaomimimo.com/v1",
        model: str = "mimo-v2.5-pro",
        timeout: float = 90.0,
        max_attempts: int = 4,
        client: httpx.Client | None = None,
        json_client: DeepSeekJsonClient | None = None,
        recorder: ModelCallRecorder | None = None,
        call_context: ModelCallContext | None = None,
    ) -> None:
        self.call_context = call_context
        self.json_client = json_client or DeepSeekJsonClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=timeout,
            max_attempts=max_attempts,
            client=client,
            recorder=recorder,
        )

    def _request_json(
        self,
        payload: Any,
        *,
        system_prompt: str,
        temperature: float,
        call_context: ModelCallContext | None = None,
        response_validator: Callable[[Any], None] | None = None,
    ) -> dict:
        return self.json_client.request_json(
            system_prompt=system_prompt,
            payload=payload,
            temperature=temperature,
            call_context=call_context,
            response_validator=response_validator,
        )

    def generate_batch(self, payload) -> list[dict]:
        expected = [spec.item_index for spec in payload.questions]

        def validate_batch(result) -> None:
            questions = result.get("questions") if isinstance(result, dict) else None
            if not isinstance(questions, list):
                raise DeepSeekModelError(
                    "model_output_schema_violation", "批式生成必须返回包含 questions 数组的 JSON 对象"
                )
            indexes = [item.get("item_index") for item in questions if isinstance(item, dict)]
            if any(i is None for i in indexes) or len(indexes) != len(questions):
                raise DeepSeekModelError(
                    "model_output_schema_violation",
                    "批式生成每个元素必须包含 item_index",
                )
            if sorted(indexes) != sorted(expected):
                raise DeepSeekModelError(
                    "model_output_scope_violation",
                    f"批式生成 item_index 集合不符：期望 {sorted(expected)}，实际 {sorted(indexes)}",
                )

        response = self._request_json(
            payload,
            temperature=0.2,
            call_context=self.call_context,
            system_prompt=(
                "你是高校期末考试命题教师，一次为本批所有题位命题，必须返回 JSON 对象，"
                "顶层字段 questions 为数组，数组每个元素包含 item_index 及该题 output_schema 要求的全部字段。"
                "只能依据各题给定的纯净知识内容与指定考查原子出题，严格遵守答案边界和题型任务，"
                "不延伸考查其他知识原子。同批各题视角互补，不得互相提示或重复。"
                "forbidden_atoms 与 forbidden_answer_cores 中的内容不得出现在任何题干、选项或答案中。"
                "优先使用 preferred_terms 中的常用术语；除符号、缩写或必要消歧外不要使用括号解释。"
                "填空题题干恰好 1 个空（连续下划线表示），空内答案简短唯一。"
                "综合题逐项执行已分配的原型、材料形式、认知序列与分问范围："
                "code_completion_scenario 先给工程场景说明再给代码框架，"
                "关键处挖 ____________(编号)__________ 空（4至6处），分问固定为补全代码与问题分析，"
                "代码与参数只能来自给定材料。若有 teacher_revision_instruction，只针对其涉及的题目局部改写。"
            ),
            response_validator=validate_batch,
        )
        questions = response["questions"]
        return [item for item in questions if isinstance(item, dict)]


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code in {408, 429} or status_code >= 500


def _persistence_error(error: DeepSeekModelError) -> tuple[str, str]:
    message = _PERSISTED_ERROR_MESSAGES.get(error.error_code)
    if message is None:
        return "model_validation_failed", "model response validation failed"
    return error.error_code, message


def _sanitize_validation_path(parts: list[object]) -> str:
    safe_parts = [
        text if text.isdigit() or text in _PERSISTED_VALIDATION_FIELDS else "unexpected_field"
        for part in parts[:8]
        for text in [str(part)]
    ]
    return ".".join(safe_parts)


def _sanitized_validation_details(error: DeepSeekModelError) -> dict[str, object] | None:
    if error.error_code != "model_schema_validation_failed":
        return None
    invalid_fields = error.details.get("invalid_fields")
    if not isinstance(invalid_fields, list):
        return None
    safe_fields: list[str] = []
    for field in invalid_fields:
        if not isinstance(field, str) or not field:
            continue
        safe_fields.append(_sanitize_validation_path(field.split(".")))
        if len(safe_fields) == 20:
            break
    details: dict[str, object] = {"invalid_fields": safe_fields}
    invalid_inputs = error.details.get("invalid_inputs")
    if isinstance(invalid_inputs, dict):
        details["invalid_input_types"] = {
            _sanitize_validation_path(str(field).split(".")): str(
                value.get("type", "unknown")
            )
            for field, value in list(invalid_inputs.items())[:20]
            if isinstance(value, dict)
        }
    details["validation_messages"] = {
        _sanitize_validation_path(
            item.get("loc", []) if isinstance(item.get("loc"), list) else []
        ): str(item.get("msg", ""))[:160]
        for item in error.details.get("validation_errors", [])[:20]
        if isinstance(item, dict)
    }
    return details
