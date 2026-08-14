from __future__ import annotations

import json

import httpx


class DeepSeekGatewayError(Exception):
    pass


class DeepSeekGateway:
    def __init__(self, *, api_key: str, base_url: str = "https://api.deepseek.com/v1", model: str = "deepseek-v4-flash", timeout: float = 90.0):
        if not api_key.strip():
            raise ValueError("DeepSeek API key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def generate(self, payload) -> dict:
        prompt = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else dict(payload)
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "temperature": 0.2,
                    "messages": [
                        {"role": "system", "content": "你是高校期末考试命题教师，只能依据给定纯净知识内容出题，必须返回JSON。"},
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
            return json.loads(content)
        except httpx.HTTPError as exc:
            raise DeepSeekGatewayError(f"DeepSeek HTTP request failed: {exc}") from exc
        except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise DeepSeekGatewayError("DeepSeek returned invalid JSON") from exc
