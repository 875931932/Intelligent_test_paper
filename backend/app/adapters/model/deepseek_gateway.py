from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from app.domain.model_calls import ModelCallContext


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
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-v4-flash",
        timeout: float = 90.0,
        max_attempts: int = 4,
        client: httpx.Client | None = None,
        recorder: ModelCallRecorder | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DeepSeek API key is required")
        if not base_url.strip():
            raise ValueError("DeepSeek base URL is required")
        if not model.strip():
            raise ValueError("DeepSeek model is required")
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

        for attempt in range(1, self.max_attempts + 1):
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
            except httpx.HTTPStatusError as exc:
                last_error = DeepSeekModelError(
                    "deepseek_http_error",
                    f"DeepSeek request failed with HTTP status {exc.response.status_code}",
                )
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
                last_retry_error_code = exc.error_code
                attempts.append(
                    {
                        "attempt": attempt,
                        "http_status": final_http_status,
                        "error_code": exc.error_code,
                    }
                )

            if attempt < self.max_attempts:
                time.sleep(min(2 ** (attempt - 1), 8))

        assert last_error is not None
        duration_ms = round((time.perf_counter() - started) * 1000)
        details = {
            "attempt_count": self.max_attempts,
            "retry_count": self.max_attempts - 1,
            "final_http_status": final_http_status,
            "last_error_code": last_error.error_code,
            "attempts": attempts,
            **last_error.details,
        }
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
        self.recorder.record(
            context=context,
            provider="deepseek",
            model=self.model,
            status=status,
            prompt_hash=prompt_hash,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            error_code=error.error_code if error else None,
            error_message=str(error) if error else None,
            request_id=request_id,
            details=details,
        )


class DeepSeekGateway:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-v4-flash",
        timeout: float = 90.0,
        max_attempts: int = 4,
        client: httpx.Client | None = None,
        recorder: ModelCallRecorder | None = None,
    ) -> None:
        self.json_client = DeepSeekJsonClient(
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
    ) -> dict:
        return self.json_client.request_json(
            system_prompt=system_prompt,
            payload=payload,
            temperature=temperature,
            call_context=call_context,
        )

    def plan_coverage(self, payload) -> dict:
        return self._request_json(
            payload,
            temperature=0.1,
            system_prompt=(
                "你是高校期末考试的全卷命题规划主脑。只能依据给定的纯净知识内容和题位蓝图，"
                "为每个题位分配唯一、最小且可评分的考查原子和答案边界。同一知识卡可以复用，"
                "但考查原子、认知层次和答案核心不得重叠；任何题目的答案核心都不得成为另一题的提示。"
                "填空题只分配术语、定义、条件或核心结论等理论任务。优先采用课程材料中的常用术语，"
                "避免不必要的括号解释。必须返回 JSON 对象，顶层字段为 directives。"
            ),
        )

    def generate(self, payload) -> dict:
        return self._request_json(
            payload,
            temperature=0.2,
            system_prompt=(
                "你是高校期末考试命题教师，只能依据给定纯净知识内容和指定考查原子出题，必须返回JSON。"
                "严格遵守答案边界和题型任务，不延伸考查其他知识原子。优先使用 preferred_terms 中的常用术语，"
                "表达直接清楚；除符号、缩写或必要消歧外不要使用括号解释。若有修订指令，必须针对该问题局部改写。"
            ),
        )

    def audit_paper(self, payload) -> dict:
        return self._request_json(
            payload,
            temperature=0.0,
            system_prompt=(
                "你是高校期末考试的全卷语义审查员。输入只包含纯净题目摘要，不包含资料来源。"
                "检查不同题型是否换一种说法重复考查同一知识、某题是否泄漏另一题答案、答案核心是否高度重叠。"
                "不要评价排版，不要重写题目。必须返回 JSON 对象，格式为 "
                '{"conflicts":[{"item_indexes":[1,2],"repair_item_index":2,"code":"semantic_overlap","message":"原因"}]}。'
                "没有冲突时返回空数组。"
            ),
        )


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
