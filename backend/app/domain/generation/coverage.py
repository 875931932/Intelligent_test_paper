from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.domain.blueprint.models import PlanItem


class CoveragePlanError(ValueError):
    pass


class CoveragePlanningSlot(BaseModel):
    item_index: int
    question_type: str
    score: float
    difficulty: str
    requested_cognitive_level: str
    performance_statement: str
    assessable_content: list[str]
    preferred_terms: list[str] = Field(default_factory=list)
    scope_boundary: dict = Field(default_factory=dict)


class CoveragePlanningPayload(BaseModel):
    slots: list[CoveragePlanningSlot]
    global_policy: dict


class CoverageDirective(BaseModel):
    item_index: int
    question_type: str
    score: float
    difficulty: str
    cognitive_level: str
    card_id: str
    performance_statement: str
    assessable_content: list[str]
    scope_boundary: dict = Field(default_factory=dict)
    coverage_atom: str
    answer_boundary: str
    preferred_terms: list[str] = Field(default_factory=list)
    novelty_contract: str
    generation_policy: dict = Field(default_factory=dict)


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
                performance_statement=card["performance_statement"],
                assessable_content=card["assessable_content"],
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
            "required_output_schema": {
                "directives": [
                    {
                        "item_index": "integer",
                        "coverage_atom": "short string",
                        "answer_boundary": "short semantic answer core",
                        "preferred_terms": ["string"],
                        "cognitive_level": "string",
                        "novelty_contract": "string",
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
    for item in plan_items:
        row = by_index[item.item_index]
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
        directives.append(
            CoverageDirective(
                item_index=item.item_index,
                question_type=item.question_type,
                score=item.score,
                difficulty=item.difficulty,
                cognitive_level=cognitive_level,
                card_id=item.card_id,
                performance_statement=card["performance_statement"],
                assessable_content=card["assessable_content"],
                scope_boundary=card.get("scope_boundary", {}),
                coverage_atom=atom,
                answer_boundary=answer_boundary,
                preferred_terms=list(row.get("preferred_terms") or card.get("preferred_terms", [])),
                novelty_contract=str(row.get("novelty_contract") or row.get("uniqueness_notes") or "只考查指定知识原子，不覆盖其他题目的答案核心"),
                generation_policy=generation_policy,
            )
        )
    return directives
