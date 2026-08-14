import json
from collections import Counter

from app.domain.blueprint.models import PlanItem
from app.workflows.generation_graph import build_generation_graph


class CoordinatingGateway:
    def __init__(self, *, leak_once: bool = False, semantic_conflict_once: bool = False):
        self.planning_payloads = []
        self.generation_payloads = []
        self.calls_by_atom = Counter()
        self.leak_once = leak_once
        self.semantic_conflict_once = semantic_conflict_once
        self.audit_calls = 0

    def plan_coverage(self, payload):
        self.planning_payloads.append(payload)
        return {
            "directives": [
                {
                    "item_index": 1,
                    "coverage_atom": "向量化的基本定义",
                    "answer_boundary": "向量化",
                    "preferred_terms": ["向量化"],
                    "cognitive_level": "remember",
                    "novelty_contract": "只考查基本定义",
                },
                {
                    "item_index": 2,
                    "coverage_atom": "训练数据的基本作用",
                    "answer_boundary": "为模型提供学习样本",
                    "preferred_terms": ["训练数据"],
                    "cognitive_level": "understand",
                    "novelty_contract": "只考查训练数据作用",
                },
            ]
        }

    def generate(self, payload):
        self.generation_payloads.append(payload)
        self.calls_by_atom[payload.coverage_atom] += 1
        if payload.question_type == "fill_blank":
            return {"stem": "将文本表示为向量的过程称为____。", "answer": "向量化"}
        if self.leak_once and self.calls_by_atom[payload.coverage_atom] == 1:
            return {"stem": "向量化能够把文本转为向量表示。", "answer": True}
        return {"stem": "训练数据用于为模型提供学习样本。", "answer": True}

    def audit_paper(self, payload):
        self.audit_calls += 1
        if self.semantic_conflict_once and self.audit_calls == 1:
            return {"conflicts": [{"item_indexes": [1, 2], "repair_item_index": 2, "code": "semantic_overlap", "message": "两题换一种说法后仍考查相同知识"}]}
        return {"conflicts": []}


def _state():
    return {
        "plan_items": [
            PlanItem(item_index=1, question_type="fill_blank", score=2, anchor_key="rag", unit_id="u1", card_id="c1").model_dump(),
            PlanItem(item_index=2, question_type="true_false", score=2, anchor_key="rag", unit_id="u1", card_id="c1").model_dump(),
        ],
        "knowledge_cards": {
            "c1": {
                "name": "课程基础概念",
                "performance_statement": "能够解释向量化和训练数据的基本作用",
                "assessable_content": ["向量化的定义", "训练数据的作用"],
                "preferred_terms": ["向量化", "训练数据"],
                "scope_boundary": {},
                "cognitive_targets": ["understand"],
                "allowed_question_types": ["fill_blank", "true_false"],
            }
        },
    }


def test_generation_graph_plans_the_whole_paper_before_question_generation():
    gateway = CoordinatingGateway()

    result = build_generation_graph(gateway).invoke(_state())

    assert len(gateway.planning_payloads) == 1
    planning_text = json.dumps(gateway.planning_payloads[0].model_dump(), ensure_ascii=False)
    assert "card_id" not in planning_text
    assert "evidence" not in planning_text.lower()
    assert [payload.coverage_atom for payload in gateway.generation_payloads] == ["向量化的基本定义", "训练数据的基本作用"]
    assert [question["item_index"] for question in result["questions"]] == [1, 2]
    assert result["conflicts"] == []
    assert result["questions"][0]["quality"]["status"] == "pass"


def test_generation_graph_repairs_only_the_question_that_leaks_another_answer():
    gateway = CoordinatingGateway(leak_once=True)

    result = build_generation_graph(gateway, max_repair_attempts=2).invoke(_state())

    assert gateway.calls_by_atom["向量化的基本定义"] == 1
    assert gateway.calls_by_atom["训练数据的基本作用"] == 2
    repaired_payload = [payload for payload in gateway.generation_payloads if payload.coverage_atom == "训练数据的基本作用"][-1]
    assert "答案泄漏" in repaired_payload.teacher_revision_instruction
    assert result["conflicts"] == []
    assert all(question["quality"]["status"] == "pass" for question in result["questions"])


def test_generation_graph_uses_one_compact_semantic_audit_and_repairs_only_flagged_item():
    gateway = CoordinatingGateway(semantic_conflict_once=True)

    result = build_generation_graph(gateway, max_repair_attempts=2).invoke(_state())

    assert gateway.audit_calls == 2
    assert gateway.calls_by_atom["向量化的基本定义"] == 1
    assert gateway.calls_by_atom["训练数据的基本作用"] == 2
    assert result["conflicts"] == []
