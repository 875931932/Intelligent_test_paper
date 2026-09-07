"""Probe whether the MiMo endpoint (api.xiaomimimo.com) reliably supports
function calling as a structured-output channel.

Diagnostic only — no database/workflow access. Answers one question: when we
hand MiMo a single tool whose `parameters` is a JSON Schema, does it return
`tool_calls` under tool_choice auto/required/forced, or drift back to content?

Usage (from backend/):
    python scripts\\probe_mimo_function_calling.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_env() -> dict[str, str]:
    env: dict[str, str] = dict(os.environ)
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value:
                env.setdefault(key, value)
    return env


_SAMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "exam_point_code": {"type": "string"},
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "performance_statement": {"type": "string"},
                    "assessable_content": {"type": "array", "items": {"type": "string"}},
                    "evidence_chunk_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "performance_statement", "assessable_content", "evidence_chunk_ids"],
            },
        },
    },
    "required": ["exam_point_code", "cards"],
}

_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_knowledge_cards",
        "description": "提交一个考点的归并知识卡结果",
        "parameters": _SAMPLE_SCHEMA,
    },
}

_SYSTEM = (
    "你是知识目录归并器。给出考点 EP-01 的两张知识卡，然后调用 submit_knowledge_cards 工具提交。"
    "考点：混合数据集与数据失衡。"
)
_USER = "请输出 EP-01 的知识卡。"


def _post(base_url: str, api_key: str, model: str, extra: dict) -> dict:
    body = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _USER},
        ],
        "tools": [_TOOL],
        "thinking": {"type": "disabled"},
    }
    body.update(extra)
    resp = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=60.0,
    )
    status = resp.status_code
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text[:300]}
    return {"status": status, "body": data}


def _render(result: dict) -> str:
    status = result["status"]
    body = result["body"]
    if status != 200:
        return f"HTTP {status}: {json.dumps(body, ensure_ascii=False)[:400]}"
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return f"HTTP 200 but unexpected envelope: {json.dumps(body, ensure_ascii=False)[:400]}"
    tool_calls = message.get("tool_calls") or []
    content = message.get("content")
    if not tool_calls:
        return f"NO tool_calls; content={repr((content or '')[:120])}"
    parts = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        try:
            parsed = json.loads(args) if isinstance(args, str) else args
        except Exception:
            parsed = f"<unparsable args: {(str(args) or '')[:120]}>"
        parts.append(f"{fn.get('name')} -> {json.dumps(parsed, ensure_ascii=False)[:300]}")
    return "tool_calls: " + " | ".join(parts)


def main() -> int:
    env = load_env()
    api_key = env.get("DEEPSEEK_API_KEY", "")
    base_url = env.get("DEEPSEEK_BASE_URL", "https://api.xiaomimimo.com/v1")
    model = env.get("DEEPSEEK_MODEL", "mimo-v2.5-pro")
    if not api_key:
        print("[FAIL] DEEPSEEK_API_KEY missing in .env")
        return 2
    print(f"base_url={base_url} model={model}")

    # 1) tool_choice auto — 连发 5 次，看稳定率
    print('\n=== [1] tool_choice="auto" x5 ===')
    auto_ok = 0
    for i in range(1, 6):
        result = _post(base_url, api_key, model, {"tool_choice": "auto"})
        rendered = _render(result)
        ok = result["status"] == 200 and rendered.startswith("tool_calls:")
        auto_ok += int(ok)
        print(f"  #{i}: {rendered}")
    print(f"  -> tool_call rate: {auto_ok}/5")

    # 2) tool_choice required
    print('\n=== [2] tool_choice="required" ===')
    print(f"  {_render(_post(base_url, api_key, model, {'tool_choice': 'required'}))}")

    # 3) 强制指定单一函数
    print("\n=== [3] tool_choice=forced function ===")
    print(
        f"  {_render(_post(base_url, api_key, model, {'tool_choice': {'type': 'function', 'function': {'name': 'submit_knowledge_cards'}}}))}"
    )

    # 4) response_format json_schema 是否被拒绝
    print("\n=== [4] response_format=json_schema (expected unsupported) ===")
    resp = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0.0,
            "max_tokens": 512,
            "messages": [{"role": "user", "content": '输出 {"exam_point_code":"EP-01"}'}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "out", "schema": _SAMPLE_SCHEMA},
            },
            "thinking": {"type": "disabled"},
        },
        timeout=60.0,
    )
    if resp.status_code != 200:
        print(f"  HTTP {resp.status_code}: {resp.text[:300]}")
    else:
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except Exception:
            content = f"<unexpected: {resp.text[:300]}>"
        print(f"  HTTP 200, content={repr((content or '')[:200])}")

    print(f"\n[DONE] auto tool_call rate {auto_ok}/5")
    return 0


if __name__ == "__main__":
    sys.exit(main())