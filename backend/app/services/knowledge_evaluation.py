"""知识库检索评测的纯函数：匹配、指标与汇总。

不依赖数据库或模型，便于单测；真实数据由脚本注入。指标只描述检索与引用
支持情况，不代表真实教师使用效果。
"""

from __future__ import annotations

import re
from typing import Any

_WS = re.compile(r"\s+")


def normalize(text: str | None) -> str:
    return _WS.sub("", (text or "").lower())


def _match_expected(hit: dict, expected: dict) -> bool:
    """文档 + 可选版本/页码/标题片段同时匹配才算命中。"""

    if expected.get("document_id") and hit.get("document_id") != expected["document_id"]:
        return False
    if expected.get("document_title") and expected["document_title"] not in (hit.get("document_title") or ""):
        return False
    if expected.get("version_no") is not None and int(hit.get("version_no") or 0) != int(expected["version_no"]):
        return False
    if expected.get("page_index") is not None and hit.get("page_index") != expected["page_index"]:
        return False
    if expected.get("block_index") is not None and hit.get("block_index") != expected["block_index"]:
        return False
    if expected.get("heading") and expected["heading"] not in " / ".join(hit.get("heading_path") or []):
        return False
    return True


def score_case(case: dict, hits: list[dict]) -> dict:
    """单个问题的证据召回、引用定位与拒答判定。

    ``evidence_recall`` = 命中的期望证据数 / 期望证据总数。``citation_support``
    需要答案级判断（本轮未做），恒为 ``None``；``quote_locatable`` 只说明原文
    片段能否在前 5 条候选中定位，不能替代答案声明是否有证据支持。
    """

    expected = case.get("expected") or []
    must_refuse = bool(case.get("must_refuse"))
    result: dict[str, Any] = {
        "id": case.get("id"),
        "question": case.get("question"),
        "tags": list(case.get("tags") or []),
        "retrieved": len(hits),
        "expected_count": len(expected),
        "evidence_recall": None,
        "reciprocal_rank": None,
        "citation_support": None,
        "quote_locatable": None,
        "refusal_correct": None,
    }

    if must_refuse:
        result["refusal_correct"] = 1.0 if not hits else 0.0
        return result
    if not expected:
        return result

    matched = sum(
        1 for item in expected if any(_match_expected(hit, item) for hit in hits)
    )
    result["evidence_recall"] = round(matched / len(expected), 4)

    first_rank = 0
    for rank, hit in enumerate(hits, start=1):
        if any(_match_expected(hit, item) for item in expected):
            first_rank = rank
            break
    result["reciprocal_rank"] = round(1.0 / first_rank, 4) if first_rank else 0.0

    quotes = [normalize(item.get("quote")) for item in expected if item.get("quote")]
    if quotes:
        corpus = [normalize(hit.get("text")) for hit in hits[:5]]
        result["quote_locatable"] = (
            1.0 if any(quote and quote in text for quote in quotes for text in corpus) else 0.0
        )
    return result


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return round(ordered[index], 2)


def summarize(results: list[dict], latencies_ms: list[float]) -> dict:
    """按适用样本求均值；不适用返回 None，避免用 0 稀释指标。"""

    def _mean(key: str) -> float | None:
        values = [row[key] for row in results if row.get(key) is not None]
        return round(sum(values) / len(values), 4) if values else None

    by_tag: dict[str, list[dict]] = {}
    for row in results:
        for tag in row.get("tags") or ["untagged"]:
            by_tag.setdefault(tag, []).append(row)

    tag_summary = {}
    for tag, rows in by_tag.items():
        values = [row["evidence_recall"] for row in rows if row.get("evidence_recall") is not None]
        tag_summary[tag] = {
            "cases": len(rows),
            "evidence_recall": round(sum(values) / len(values), 4) if values else None,
        }

    return {
        "cases": len(results),
        "evidence_recall": _mean("evidence_recall"),
        "mrr": _mean("reciprocal_rank"),
        "quote_locatable": _mean("quote_locatable"),
        "citation_support": None,
        "citation_support_note": "答案级引用支持尚未评估；quote_locatable 只表示原文片段可定位。",
        "refusal_accuracy": _mean("refusal_correct"),
        "latency_ms": {
            "p50": _percentile(latencies_ms, 0.5),
            "p95": _percentile(latencies_ms, 0.95),
            "max": round(max(latencies_ms), 2) if latencies_ms else 0.0,
        },
        "by_tag": tag_summary,
        "note": "仅衡量检索与引用定位；不代表真实教师使用效果。",
    }
