from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from app.domain.model_calls import ModelCallContext

logger = logging.getLogger("model.gateway")

# 并发洪泛是分类/归并阶段 HTTP 429 限流的根源：organization_max_workers=16
# 的并行线程同时轰击模型 API，网关虽带退避重试但退避窗口太短，无法等限流恢复。
# 用进程级信号量把所有模型调用（分类/归并/抽取/框架抽取共用同一网关）的并发数
# 收敛到很小的常数，让 DeepSeek 端始终处于可控负载；再用 429 长退避兜底
# RPM 触顶。metagain 单租户工具，进程内所有 client 共享同一信号量协调并发。
_LLM_MAX_CONCURRENCY = 2
_LLM_SEMAPHORE = threading.BoundedSemaphore(_LLM_MAX_CONCURRENCY)


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
        # 默认值与 DeepSeekGateway / settings 保持一致（StepFun step-3.7-flash）。
        # 历史上这里误留 MiMo 默认值，任何未显式传 model/base_url 的构造路径
        # 都会静默回落到 MiMo 端点，造成"配置改了却还在用旧模型"的排查盲区。
        base_url: str = "https://api.stepfun.com/v1",
        model: str = "step-3.7-flash",
        timeout: float = 90.0,
        max_attempts: int = 4,
        # 大 prompt（如知识目录分类批）失败重试非常昂贵：单次输入即数万 token，
        # 校验失败（模型漏判某对）时整批重发只会等额再烧一遍。超过该字符数的
        # 请求把重试上限收紧到 large_prompt_max_attempts。
        large_prompt_max_attempts: int = 2,
        large_prompt_threshold_chars: int = 60_000,
        disable_thinking: bool = True,
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
        self.large_prompt_max_attempts = max(1, large_prompt_max_attempts)
        self.large_prompt_threshold_chars = large_prompt_threshold_chars
        self.disable_thinking = disable_thinking
        self.client = client or httpx.Client(
            trust_env=False,
            # 连接 15s / 读写 self.timeout：连接阶段卡死（历史上曾单请求挂
            # 3600s）应快速失败进入重试，而非占住 worker 线程无限等待。
            timeout=httpx.Timeout(timeout, connect=15.0),
        )
        self.recorder = recorder

    def request_json(
        self,
        *,
        system_prompt: str,
        payload: Any,
        temperature: float,
        call_context: ModelCallContext | None = None,
        response_validator: Callable[[dict], None] | None = None,
        tool: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> dict:
        prompt = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else dict(payload)
        canonical_prompt = json.dumps(prompt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        prompt_hash = hashlib.sha256(f"{system_prompt}\n{canonical_prompt}".encode()).hexdigest()
        # temperature=0 的确定性调用（知识目录分类/归并/提取等）可安全复用
        # 历史成功响应：同 (model, prompt) 的重建（如资料未变的重复构建）不再
        # 触发真实模型调用，模型消耗趋近于零。出题类 temperature>0 调用不缓存。
        if temperature <= 0 and self.recorder is not None:
            lookup = getattr(self.recorder, "lookup_response", None)
            if lookup is not None:
                try:
                    cached = lookup(model=self.model, prompt_hash=prompt_hash)
                except Exception:
                    cached = None
                if cached is not None:
                    try:
                        if response_validator is not None:
                            response_validator(cached)
                    except Exception:
                        cached = None
                if cached is not None:
                    self._record(
                        context=call_context,
                        status="succeeded",
                        prompt_hash=prompt_hash,
                        input_tokens=0,
                        output_tokens=0,
                        duration_ms=0,
                        error=None,
                        request_id=None,
                        details={"cache_hit": True},
                    )
                    logger.info(
                        "模型调用命中缓存 stage=%s model=%s prompt_hash=%s",
                        _stage_of(call_context),
                        self.model,
                        prompt_hash[:12],
                    )
                    return cached
        started = time.perf_counter()
        attempts: list[dict[str, Any]] = []
        last_error: DeepSeekModelError | None = None
        request_id: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        final_http_status: int | None = None
        last_retry_error_code: str | None = None
        attempt_count = 0
        raw_snapshot: str | None = None
        # 大 prompt 收紧重试：避免数万 token 的批在多次重试中重复计费。
        effective_max_attempts = self.max_attempts
        if len(canonical_prompt) > self.large_prompt_threshold_chars:
            effective_max_attempts = min(
                self.max_attempts, self.large_prompt_max_attempts
            )

        for attempt in range(1, effective_max_attempts + 1):
            attempt_count = attempt
            request_id = None
            input_tokens = None
            output_tokens = None
            final_http_status = None
            raw_snapshot = None
            should_retry = True
            try:
                with _LLM_SEMAPHORE:
                    response = self._post(system_prompt, canonical_prompt, temperature, tool, max_tokens, reasoning_effort)
                headers = getattr(response, "headers", {})
                request_id = headers.get("x-request-id") if hasattr(headers, "get") else None
                status_code = getattr(response, "status_code", None)
                final_http_status = status_code if isinstance(status_code, int) else 200
                response.raise_for_status()
                raw_snapshot = response.content.decode("utf-8", errors="replace")[:2000]
                body = response.json()
                if not isinstance(body, dict):
                    raise DeepSeekModelError("model_invalid_envelope", "model response envelope is invalid")
                request_id = request_id or _optional_text(body.get("id"))
                usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
                input_tokens = _optional_int(usage.get("prompt_tokens"))
                output_tokens = _optional_int(usage.get("completion_tokens"))
                try:
                    message = body["choices"][0]["message"]
                except (KeyError, IndexError, TypeError):
                    raise DeepSeekModelError(
                        "model_invalid_envelope",
                        "model response is missing message content",
                    ) from None
                if not isinstance(message, dict):
                    raise DeepSeekModelError(
                        "model_invalid_envelope", "model message is invalid"
                    )
                if tool is not None:
                    result = _extract_tool_arguments(message)
                else:
                    content = message.get("content")
                    if not isinstance(content, str) or not content.strip():
                        raise DeepSeekModelError(
                            "model_empty_response", "model returned empty content"
                        )
                    result = _extract_json_object(content)
                    if result is None:
                        raise DeepSeekModelError(
                            "model_non_json_response",
                            "model returned content that is not valid JSON",
                        )
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
                        "response": result,
                    },
                )
                # 请求级 INFO：线上排查"日志看不到模型调用"的核心埋点。
                # 只带 stage/model/耗时/次数/token，不带 prompt 与 api_key。
                logger.info(
                    "模型调用成功 stage=%s model=%s status=succeeded "
                    "request_id=%s duration_ms=%d attempts=%d "
                    "input_tokens=%s output_tokens=%s",
                    _stage_of(call_context),
                    self.model,
                    request_id,
                    duration_ms,
                    attempt,
                    input_tokens,
                    output_tokens,
                )
                return result

            if not should_retry:
                break
            if attempt < effective_max_attempts:
                # 429 是显式限流信号，退避窗口远比网络抖动长：给足时间等限流恢复
                #（常见窗口约 1 分钟），避免在并发信号量保护下仍因 RPM 触顶重试耗尽。
                if final_http_status == 429:
                    time.sleep(min(45, 15 * attempt))
                else:
                    time.sleep(min(2 ** (attempt - 1), 8))

        assert last_error is not None
        duration_ms = round((time.perf_counter() - started) * 1000)
        # 已知错误码（evidence_gap 等）的具体原因落库便于定位；validator 自定义
        # 错误码的原始 message 可能含敏感内容，统一用脱敏文案。
        persisted_code, persisted_message = _persistence_error(last_error)
        details = {
            "attempt_count": attempt_count,
            "retry_count": attempt_count - 1,
            "effective_max_attempts": effective_max_attempts,
            "final_http_status": final_http_status,
            "last_error_code": persisted_code,
            "error_message": (
                str(last_error)
                if last_error.error_code in _PERSISTED_ERROR_MESSAGES
                else persisted_message
            ),
            "attempts": attempts,
        }
        validation_details = _sanitized_validation_details(last_error)
        if validation_details is not None:
            details["validation"] = validation_details
        message_shape = last_error.details.get("message_shape") if last_error.details else None
        if message_shape is not None:
            details["message_shape"] = message_shape
        if raw_snapshot:
            details["raw_response_snapshot"] = raw_snapshot
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
        # 请求级 WARNING：与成功日志同构，确保失败调用在日志里可见
        # （此前只有落库无日志，排查时只能翻数据库）。
        logger.warning(
            "模型调用失败 stage=%s model=%s status=failed duration_ms=%d "
            "attempts=%d error_code=%s http_status=%s request_id=%s",
            _stage_of(call_context),
            self.model,
            duration_ms,
            attempt_count,
            persisted_code,
            final_http_status,
            request_id,
        )
        raise DeepSeekGatewayError(last_error.error_code, str(last_error), details=details) from last_error

    def _post(
        self,
        system_prompt: str,
        canonical_prompt: str,
        temperature: float,
        tool: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> httpx.Response:
        # StepFun（api.stepfun.com/.ai）未文档化 thinking/tool_choice 参数，
        # MiMo（api.xiaomimimo.com）两者均支持：按 base_url 分流避免未知参数
        # 触发 400。tool_choice 缺省时模型若不触发 tool_calls，由
        # _extract_tool_arguments 兜底解析 content JSON。
        #
        # StepFun step-3.7-flash 是推理型模型，可用 reasoning_effort 三档控制
        # 思考强度（low/medium/high）。信息抽取用 low 最省预算、避免思考占满
        # 输出额度导致 content 为空/截断非 JSON。调用方可显式传入档位；越过
        # reasoning_effort 时沿用 disable_thinking=True 的旧行为（stepfun=low，
        # mimo=关闭思考）。
        is_stepfun = "stepfun" in self.base_url
        json_body: dict[str, Any] = {
            "model": self.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": canonical_prompt},
            ],
        }
        if max_tokens is not None:
            json_body["max_tokens"] = max_tokens
        if tool is not None:
            json_body["tools"] = [{"type": "function", "function": tool}]
            if not is_stepfun:
                json_body["tool_choice"] = "required"
        else:
            json_body["response_format"] = {"type": "json_object"}
        if is_stepfun:
            effort = reasoning_effort or ("low" if self.disable_thinking else None)
            if effort:
                json_body["reasoning_effort"] = effort
        elif self.disable_thinking:
            json_body["thinking"] = {"type": "disabled"}
        request = {
            "headers": {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            "json": json_body,
            # 连接与读取超时分离：读取超时即 self.timeout（默认 240s，覆盖
            # 慢速生成），连接超时固定 15s——历史上出现单请求挂 3600s 的
            # transport_error，多为连接阶段阻塞，及时失败重试优于无限等待。
            "timeout": httpx.Timeout(self.timeout, connect=15.0),
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
        base_url: str = "https://api.stepfun.com/v1",
        model: str = "step-3.7-flash",
        timeout: float = 90.0,
        max_attempts: int = 4,
        disable_thinking: bool = True,
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
            disable_thinking=disable_thinking,
            client=client,
            recorder=recorder,
        )
        # 打印生效配置：排查"配置改了却还在用旧模型"时，第一眼即可确认
        # 进程实际使用的 base_url/model；api_key 绝不进日志。
        logger.info(
            "DeepSeekGateway 生效配置 base_url=%s model=%s timeout=%.1fs "
            "max_attempts=%d disable_thinking=%s",
            base_url, model, timeout, max_attempts, disable_thinking,
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


def _stage_of(context: ModelCallContext | None) -> str:
    """日志用的调用方标识：缺省 context 时用 "-" 占位，绝不让日志缺失。"""
    return context.stage if context is not None else "-"


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _extract_tool_arguments(message: dict[str, Any]) -> dict:
    """Parse the first function-tool call's JSON arguments out of a chat message.

    MiMo 在 tool_choice=required 下偶发不触发 tool_calls 而直接回 content JSON，
    此时兜底解析 content，避免一次抖动导致整批重试烧 token。
    """

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        first = tool_calls[0]
        if isinstance(first, dict):
            function = first.get("function")
            if isinstance(function, dict):
                arguments = function.get("arguments")
                # MiMo 偶尔返回已解析的 JSON 对象，而非字符串：两者都接受。
                if isinstance(arguments, dict):
                    return arguments
                if isinstance(arguments, str) and arguments.strip():
                    try:
                        return json.loads(arguments)
                    except json.JSONDecodeError:
                        raise DeepSeekModelError(
                            "model_non_json_response",
                            "tool arguments are not valid JSON",
                        ) from None
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        parsed = _extract_json_object(content)
        if parsed is not None:
            return parsed
    # 采样真实的 message 结构，帮助定位 MiMo 到底回了什么（只取键名，不取长内容）。
    shape: dict[str, object] = {}
    for key in ("role", "content", "tool_calls", "refusal"):
        if key in message:
            value = message[key]
            if isinstance(value, list):
                shape[key] = {  # noqa: SIM118
                    "count": len(value),
                    "first_keys": sorted(
                        value[0].keys() if value and isinstance(value[0], dict) else []
                    ),
                }
            elif isinstance(value, dict):
                shape[key] = {"keys": sorted(value.keys())}
            else:
                shape[key] = (
                    (value[:80] + "...") if isinstance(value, str) and len(value) > 80 else value
                )
    raise DeepSeekModelError(
        "model_invalid_envelope",
        "model returned no tool call or JSON content",
        details={"message_shape": shape},
    )


def _extract_json_object(content: str) -> Any | None:
    """Parse a top-level JSON value, tolerating surrounding prose/code fences.

    模型偶发把 JSON 包在 markdown 代码块或说明文字里（`````json ... `````、
    "结果如下：{...}"）。先按原样解析（合法值原样返回，含非对象的 list/标量，
    由调用方 `isinstance(result, dict)` 判定对象类型错误码）；失败则取首个 {
    到末个 } 的连续段再试，只接受对象。与 tool 通道 _extract_tool_arguments
    的兜底口径一致。
    """

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    first = content.find("{")
    last = content.rfind("}")
    if 0 <= first < last:
        try:
            embedded = content[first : last + 1]
            parsed = json.loads(embedded)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


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
