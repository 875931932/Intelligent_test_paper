from __future__ import annotations

import hashlib
import json
import re

from pydantic import BaseModel, ConfigDict


ACTION_ALIASES = {
    "定位原因": "diagnose",
    "分析成因": "diagnose",
    "提出修正": "repair",
    "给出改进": "repair",
    "比较方案": "compare",
    "作出选择": "decide",
    "解释现象": "explain",
    "设计方案": "design",
}
_CANONICAL_ACTIONS = set(ACTION_ALIASES.values())
_BOUNDARY_ROLES = (
    ("diagnose", ("根因", "原因", "成因", "cause")),
    ("repair", ("修正", "改进", "修复", "纠正", "优化", "repair")),
    ("compare", ("比较", "对比", "差异", "权衡", "compare")),
    ("decide", ("选择", "决策", "取舍", "结论", "decide")),
    ("explain", ("解释", "说明", "因果", "explain")),
    ("design", ("设计", "方案", "步骤", "design")),
)


def _compact(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(value or "")).lower()


def normalize_action(value: str) -> str:
    compact = _compact(value)
    if compact in _CANONICAL_ACTIONS:
        return compact
    for alias, canonical in ACTION_ALIASES.items():
        if _compact(alias) in compact:
            return canonical
    return "other"


def normalize_answer_boundary(value: str) -> str:
    compact = _compact(value)
    matched = [role for role, keywords in _BOUNDARY_ROLES if any(keyword in compact for keyword in keywords)]
    if "diagnose" in matched:
        return "diagnose"
    if "repair" in matched:
        return "repair"
    return "+".join(matched) if matched else "bounded_answer"


class QuestionStructureSignature(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    archetype: str
    material_form: str
    cognitive_sequence: list[str]
    subquestion_actions: list[str]
    answer_boundaries: list[str]
    structure_key: str
    signature_hash: str


def build_structure_signature(
    *,
    archetype: str,
    material_form: str,
    cognitive_sequence: list[str],
    subquestion_actions: list[str],
    answer_boundaries: list[str],
) -> QuestionStructureSignature:
    normalized = {
        "archetype": _compact(archetype),
        "material_form": _compact(material_form),
        "cognitive_sequence": [_compact(action) for action in cognitive_sequence],
        "subquestion_actions": [normalize_action(action) for action in subquestion_actions],
        "answer_boundaries": [normalize_answer_boundary(boundary) for boundary in answer_boundaries],
    }
    stable_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return QuestionStructureSignature(
        **normalized,
        structure_key=stable_json,
        signature_hash=hashlib.sha256(stable_json.encode("utf-8")).hexdigest(),
    )
