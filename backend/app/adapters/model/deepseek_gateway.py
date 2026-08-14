from __future__ import annotations

import json
import time

import httpx


class DeepSeekGatewayError(Exception):
    pass


class DeepSeekGateway:
    def __init__(self, *, api_key: str, base_url: str = "https://api.deepseek.com/v1", model: str = "deepseek-v4-flash", timeout: float = 90.0, max_attempts: int = 4):
        if not api_key.strip():
            raise ValueError("DeepSeek API key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_attempts = max_attempts

    def _request_json(self, payload, *, system_prompt: str, temperature: float) -> dict:
        prompt = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else dict(payload)
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                response = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={
                        "model": self.model,
                        "temperature": temperature,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                        ],
                        "response_format": {"type": "json_object"},
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                body = response.json()
                content = body.get("choices", [{}])[0].get("message", {}).get("content")
                if not content:
                    raise DeepSeekGatewayError("model returned empty content")
                result = json.loads(content)
                if not isinstance(result, dict):
                    raise DeepSeekGatewayError("model returned a non-object JSON value")
                return result
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError, DeepSeekGatewayError) as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    time.sleep(min(2 ** attempt, 8))
        if isinstance(last_error, httpx.HTTPError):
            raise DeepSeekGatewayError(f"DeepSeek HTTP request failed after {self.max_attempts} attempts: {last_error}") from last_error
        raise DeepSeekGatewayError(f"DeepSeek returned invalid content after {self.max_attempts} attempts: {last_error}") from last_error

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
