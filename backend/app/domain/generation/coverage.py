from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.blueprint.models import AssessmentMode, PlanItem
from app.domain.generation.archetypes import ARCHETYPE_CONTRACTS, ComprehensiveArchetype, MaterialForm


class CoveragePlanError(ValueError):
    pass


class CoveragePlanningSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_index: int
    question_type: str
    score: float
    difficulty: str
    requested_cognitive_level: str
    assessment_mode: AssessmentMode
    performance_statement: str
    assessable_content: list[str]
    prompt_material: list[str] = Field(default_factory=list)
    preferred_terms: list[str] = Field(default_factory=list)
    scope_boundary: dict = Field(default_factory=dict)


class CoveragePlanningPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slots: list[CoveragePlanningSlot]
    global_policy: dict


class CoverageDirective(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_index: int
    question_type: str
    score: float
    difficulty: str
    cognitive_level: str
    assessment_mode: AssessmentMode = "conceptual"
    card_id: str
    performance_statement: str
    assessable_content: list[str]
    prompt_material: list[str] = Field(default_factory=list)
    scope_boundary: dict = Field(default_factory=dict)
    coverage_atom: str
    answer_boundary: str
    preferred_terms: list[str] = Field(default_factory=list)
    novelty_contract: str
    generation_policy: dict = Field(default_factory=dict)
    comprehensive_archetype: ComprehensiveArchetype | None = None
    material_form: MaterialForm | None = None
    cognitive_sequence: list[str] = Field(default_factory=list)
    subquestion_count_range: list[int] | None = None

    @model_validator(mode="after")
    def validate_comprehensive_contract(self):
        _validate_comprehensive_contract(
            question_type=self.question_type,
            assessment_mode=self.assessment_mode,
            comprehensive_archetype=self.comprehensive_archetype,
            material_form=self.material_form,
            cognitive_sequence=self.cognitive_sequence,
            subquestion_count_range=self.subquestion_count_range,
        )
        return self


_COMPREHENSIVE_FIELDS = {
    "comprehensive_archetype",
    "material_form",
    "cognitive_sequence",
    "subquestion_count_range",
}
_COGNITIVE_ACTIONS = {"remember", "understand", "apply", "analyze", "evaluate", "create"}


def _normalize_prompt_material(value: Any) -> list[str]:
    values = [value] if isinstance(value, str) else value if isinstance(value, (list, tuple)) else []
    return list(dict.fromkeys(text for item in values if (text := str(item).strip())))


def _validate_comprehensive_contract(
    *,
    question_type: str,
    assessment_mode: str,
    comprehensive_archetype: str | None,
    material_form: str | None,
    cognitive_sequence: list[str] | None,
    subquestion_count_range: list[int] | None,
) -> None:
    if question_type != "comprehensive":
        if comprehensive_archetype is not None or material_form is not None or cognitive_sequence or subquestion_count_range is not None:
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


def compile_coverage_planning_payload(plan_items: list[PlanItem], knowledge_cards: dict[str, dict]) -> CoveragePlanningPayload:
    slots = []
    for item in plan_items:
        card = knowledge_cards[item.card_id]
        slots.append(
            CoveragePlanningSlot(
                item_index=item.item_index,
                question_type=item.question_type,
                score=item.score,
                difficulty=item.difficulty,
                requested_cognitive_level=item.cognitive_level,
                assessment_mode=item.assessment_mode,
                performance_statement=card["performance_statement"],
                assessable_content=card["assessable_content"],
                prompt_material=_normalize_prompt_material(card.get("prompt_material")),
                preferred_terms=card.get("preferred_terms", []),
                scope_boundary=card.get("scope_boundary", {}),
            )
        )
    return CoveragePlanningPayload(
        slots=slots,
        global_policy={
            "unique_coverage_atoms": True,
            "non_overlapping_answer_boundaries": True,
            "same_card_reuse": "allowed_only_for_distinct_atoms_and_cognitive_levels",
            "fill_blank_cognitive_levels": ["remember", "understand"],
            "prefer_course_common_terms": True,
            "max_parenthetical_pairs": 1,
            "comprehensive_archetype_contracts": {
                name: {
                    "allowed_assessment_modes": sorted(contract.allowed_modes),
                    "material_forms": sorted(contract.material_forms),
                    "structure_requirements": list(contract.structure_requirements),
                }
                for name, contract in ARCHETYPE_CONTRACTS.items()
            },
            "comprehensive_structure_key": ["comprehensive_archetype", "material_form", "cognitive_sequence"],
            "comprehensive_structure_key_unique_within_paper": True,
            "comprehensive_subquestion_count_range": [2, 4],
            "comprehensive_template_policy": "先选择与题位 assessment_mode 兼容的原型；禁止固定套用某公司/某团队背景加三问的通用模板",
            "required_output_schema": {
                "directives": [
                    {
                        "item_index": "integer",
                        "coverage_atom": "short string",
                        "answer_boundary": "short semantic answer core",
                        "preferred_terms": ["string"],
                        "cognitive_level": "string",
                        "assessment_mode": "must equal the assigned slot assessment_mode",
                        "novelty_contract": "string",
                        "comprehensive_archetype": "required for comprehensive only",
                        "material_form": "required for comprehensive only",
                        "cognitive_sequence": ["recognized cognitive action"],
                        "subquestion_count_range": ["integer 2..4", "integer 2..4"],
                    }
                ]
            },
        },
    )


def _normalized(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", value).lower()


def build_coverage_directives(plan_items: list[PlanItem], knowledge_cards: dict[str, dict], raw_plan: dict) -> list[CoverageDirective]:
    rows = raw_plan.get("directives")
    if isinstance(rows, dict):
        rows = rows.get("slot_assignments") or rows.get("items")
    if rows is None:
        rows = raw_plan.get("slot_assignments")
    if not isinstance(rows, list):
        raise CoveragePlanError("覆盖计划 directives 必须是数组")
    by_index = {int(row.get("item_index", -1)): row for row in rows if isinstance(row, dict)}
    if set(by_index) != {item.item_index for item in plan_items}:
        raise CoveragePlanError("覆盖计划必须完整覆盖所有题位")

    directives: list[CoverageDirective] = []
    used_atoms: set[str] = set()
    used_answers: set[str] = set()
    used_levels_by_card: dict[str, set[str]] = {}
    used_comprehensive_structures: set[tuple[str, str, tuple[str, ...]]] = set()
    for item in plan_items:
        row = by_index[item.item_index]
        if item.question_type != "comprehensive" and _COMPREHENSIVE_FIELDS.intersection(row):
            raise CoveragePlanError("非综合题不能携带综合题合同字段")
        raw_assessment_mode = row.get("assessment_mode")
        if raw_assessment_mode is not None and raw_assessment_mode != item.assessment_mode:
            raise CoveragePlanError("覆盖计划不得改变题位已分配的考查方式")
        atom = str(row.get("coverage_atom") or row.get("assessable_content") or "").strip()
        raw_answer = str(row.get("answer") or "").strip()
        generic_answers = {"正确", "错误", "true", "false", "a", "b", "c", "d"}
        explicit_boundary = str(row.get("answer_boundary") or "").strip()
        if row.get("coverage_atom") and explicit_boundary and explicit_boundary.lower() not in generic_answers:
            answer_boundary = explicit_boundary
        elif raw_answer and raw_answer.lower() not in generic_answers:
            answer_boundary = raw_answer
        elif row.get("answer_core"):
            answer_boundary = str(row["answer_core"]).strip()
        elif atom:
            answer_boundary = atom
        else:
            answer_boundary = str(row.get("answer_boundary", "")).strip()
        if not atom or not answer_boundary:
            raise CoveragePlanError("每个题位都必须包含考查原子和答案边界")
        atom_key = _normalized(atom)
        answer_key = _normalized(answer_boundary)
        if atom_key in used_atoms:
            raise CoveragePlanError(f"重复考查原子：{atom}")
        if answer_key in used_answers:
            raise CoveragePlanError(f"重复答案边界：{answer_boundary}")
        used_atoms.add(atom_key)
        used_answers.add(answer_key)

        cognitive_level = str(row.get("cognitive_level") or item.cognitive_level)
        generation_policy: dict = {"max_parenthetical_pairs": 1, "prefer_common_terms": True}
        if item.question_type == "fill_blank":
            if cognitive_level not in {"remember", "understand"}:
                cognitive_level = "understand"
            generation_policy.update(
                {
                    "mode": "theory_recall",
                    "allowed_tasks": ["term", "definition", "condition", "core_conclusion"],
                    "disallowed_tasks": ["scenario_design", "open_analysis", "multi_step_solution"],
                }
            )

        card_levels = used_levels_by_card.setdefault(item.card_id, set())
        if cognitive_level in card_levels:
            raise CoveragePlanError(f"同一知识卡复用时认知层次重复：{cognitive_level}")
        card_levels.add(cognitive_level)

        card = knowledge_cards[item.card_id]
        comprehensive_archetype = row.get("comprehensive_archetype")
        material_form = row.get("material_form")
        cognitive_sequence = row.get("cognitive_sequence")
        subquestion_count_range = row.get("subquestion_count_range")
        _validate_comprehensive_contract(
            question_type=item.question_type,
            assessment_mode=item.assessment_mode,
            comprehensive_archetype=comprehensive_archetype,
            material_form=material_form,
            cognitive_sequence=cognitive_sequence,
            subquestion_count_range=subquestion_count_range,
        )
        if item.question_type == "comprehensive":
            structure_key = (str(comprehensive_archetype), str(material_form), tuple(cognitive_sequence))
            if structure_key in used_comprehensive_structures:
                raise CoveragePlanError("同卷综合题结构重复：原型、材料形式和认知序列必须形成唯一组合")
            used_comprehensive_structures.add(structure_key)
        directives.append(
            CoverageDirective(
                item_index=item.item_index,
                question_type=item.question_type,
                score=item.score,
                difficulty=item.difficulty,
                cognitive_level=cognitive_level,
                assessment_mode=item.assessment_mode,
                card_id=item.card_id,
                performance_statement=card["performance_statement"],
                assessable_content=card["assessable_content"],
                prompt_material=_normalize_prompt_material(card.get("prompt_material")),
                scope_boundary=card.get("scope_boundary", {}),
                coverage_atom=atom,
                answer_boundary=answer_boundary,
                preferred_terms=list(row.get("preferred_terms") or card.get("preferred_terms", [])),
                novelty_contract=str(row.get("novelty_contract") or row.get("uniqueness_notes") or "只考查指定知识原子，不覆盖其他题目的答案核心"),
                generation_policy=generation_policy,
                comprehensive_archetype=comprehensive_archetype,
                material_form=material_form,
                cognitive_sequence=list(cognitive_sequence or []),
                subquestion_count_range=list(subquestion_count_range) if subquestion_count_range is not None else None,
            )
        )
    return directives
