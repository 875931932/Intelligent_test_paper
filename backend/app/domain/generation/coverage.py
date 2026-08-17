"""综合题合同验证与文本归一化工具（供合同链路 contract.py 复用）。"""
from __future__ import annotations

import re

from app.domain.generation.archetypes import ARCHETYPE_CONTRACTS


class CoveragePlanError(ValueError):
    def __init__(self, message: str, *, structure_key: str | None = None):
        super().__init__(message)
        self.structure_key = structure_key


_COGNITIVE_ACTIONS = {"remember", "understand", "apply", "analyze", "evaluate", "create"}


def _validate_comprehensive_contract(
    *,
    question_type: str,
    assessment_mode: str,
    comprehensive_archetype: str | None,
    material_form: str | None,
    cognitive_sequence: list[str] | None,
    subquestion_count_range: list[int] | None,
    subquestion_actions: list[str] | None,
    answer_boundaries: list[str] | None,
) -> None:
    if question_type != "comprehensive":
        if (
            comprehensive_archetype is not None
            or material_form is not None
            or cognitive_sequence
            or subquestion_count_range is not None
            or subquestion_actions
            or answer_boundaries
        ):
            raise CoveragePlanError("非综合题不能携带综合题合同字段")
        return
    if comprehensive_archetype is None or material_form is None or cognitive_sequence is None or subquestion_count_range is None:
        raise CoveragePlanError("综合题必须携带完整的原型、材料形式、认知序列和分问范围")
    contract = ARCHETYPE_CONTRACTS.get(comprehensive_archetype)
    if contract is None:
        raise CoveragePlanError(f"未知综合题原型：{comprehensive_archetype}")
    if assessment_mode not in contract.allowed_modes:
        raise CoveragePlanError(f"综合题原型与考查方式不兼容：{comprehensive_archetype}/{assessment_mode}")
    if material_form not in contract.material_forms:
        raise CoveragePlanError(f"综合题原型与材料形式不兼容：{comprehensive_archetype}/{material_form}")
    if not cognitive_sequence or len(cognitive_sequence) > 4 or any(action not in _COGNITIVE_ACTIONS for action in cognitive_sequence):
        raise CoveragePlanError("综合题认知序列必须包含 1 至 4 个合法认知动作")
    if (
        len(subquestion_count_range) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in subquestion_count_range)
        or not 2 <= subquestion_count_range[0] <= subquestion_count_range[1] <= 4
    ):
        raise CoveragePlanError("综合题分问范围上下界必须在 2 至 4 之间且下界不大于上界")
    if comprehensive_archetype == "code_completion_scenario" and list(subquestion_count_range) != [2, 2]:
        raise CoveragePlanError("code_completion_scenario 原型固定两个分问：补全代码与问题分析")
    if not subquestion_actions or not answer_boundaries or len(subquestion_actions) != len(answer_boundaries):
        raise CoveragePlanError("综合题必须规划数量一致的分问动作和答案边界")
    if not subquestion_count_range[0] <= len(subquestion_actions) <= subquestion_count_range[1]:
        raise CoveragePlanError("综合题规划分问数量必须落在分问范围内")


def _normalized(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", value).lower()
