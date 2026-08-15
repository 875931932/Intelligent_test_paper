import json
from collections import Counter

from app.domain.blueprint.models import PlanItem
from app.domain.generation.structure_signature import build_structure_signature
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


class ComprehensiveGateway:
    def __init__(self):
        self.planning_payloads = []
        self.generation_payloads = []

    def plan_coverage(self, payload):
        self.planning_payloads.append(payload)
        return {
            "directives": [
                {
                    "item_index": 1,
                    "coverage_atom": "根据异常表现定位原因",
                    "answer_boundary": "定位依据、原因和修正",
                    "cognitive_level": "analyze",
                    "comprehensive_archetype": "fault_diagnosis",
                    "material_form": "symptom_list",
                    "cognitive_sequence": ["analyze", "apply"],
                    "subquestion_count_range": [2, 3],
                },
                {
                    "item_index": 2,
                    "coverage_atom": "根据约束比较候选方案",
                    "answer_boundary": "比较依据、选择和理由",
                    "cognitive_level": "evaluate",
                    "comprehensive_archetype": "comparative_decision",
                    "material_form": "constraint_table",
                    "cognitive_sequence": ["analyze", "evaluate"],
                    "subquestion_count_range": [2, 4],
                },
                {
                    "item_index": 3,
                    "coverage_atom": "解释复合现象的因果链",
                    "answer_boundary": "关键环节和因果关系",
                    "cognitive_level": "analyze",
                    "comprehensive_archetype": "integrated_explanation",
                    "material_form": "causal_chain",
                    "cognitive_sequence": ["understand", "analyze"],
                    "subquestion_count_range": [2, 3],
                },
            ]
        }

    def generate(self, payload):
        self.generation_payloads.append(payload)
        return {
            "stem": payload.prompt_material[0],
            "subquestions": [
                {
                    "action": payload.cognitive_sequence[0],
                    "prompt": "完成指定任务",
                    "answer_boundary": payload.answer_boundary,
                    "answer": payload.answer_boundary,
                    "rubric": ["依据充分"],
                },
                {
                    "action": payload.cognitive_sequence[-1],
                    "prompt": "说明结论",
                    "answer_boundary": payload.answer_boundary,
                    "answer": payload.answer_boundary,
                    "rubric": ["结论明确"],
                },
            ],
            "answer": payload.answer_boundary,
            "explanation": "按合同完成",
            "rubric": ["结构完整"],
        }

    def audit_paper(self, payload):
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


def _comprehensive_state():
    archetype_material = [
        ("诊断任务", "异常表现：召回结果持续偏离问题"),
        ("决策任务", "约束表：质量优先且资源有限"),
        ("解释任务", "因果链：输入变化引发多阶段响应"),
    ]
    return {
        "plan_items": [
            PlanItem(
                item_index=index,
                question_type="comprehensive",
                score=10,
                anchor_key=f"anchor-{index}",
                unit_id=f"u{index}",
                card_id=f"c{index}",
                cognitive_level="analyze",
                assessment_mode="problem_solving" if index < 3 else "application",
            ).model_dump()
            for index in (1, 2, 3)
        ],
        "knowledge_cards": {
            f"c{index}": {
                "performance_statement": f"能够完成{task}",
                "assessable_content": [task],
                "prompt_material": material,
                "scope_boundary": {},
            }
            for index, (task, material) in enumerate(archetype_material, start=1)
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
    assert all("structure_signature" not in question for question in result["questions"])


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


def test_generation_graph_carries_three_distinct_comprehensive_contracts_without_sources():
    gateway = ComprehensiveGateway()

    result = build_generation_graph(gateway).invoke(_comprehensive_state())

    assert [payload.comprehensive_archetype for payload in gateway.generation_payloads] == [
        "fault_diagnosis",
        "comparative_decision",
        "integrated_explanation",
    ]
    assert len({payload.question_template for payload in gateway.generation_payloads}) == 3
    planning_text = json.dumps(gateway.planning_payloads[0].model_dump(), ensure_ascii=False)
    generation_text = json.dumps([payload.model_dump() for payload in gateway.generation_payloads], ensure_ascii=False)
    assert "prompt_material" in planning_text
    assert "assessment_mode" in planning_text
    for forbidden in ("card_id", "exam_point_id", "filename", "page", "evidence", "material_version_id"):
        assert forbidden not in generation_text.lower()
    assert [question["item_index"] for question in result["questions"]] == [1, 2, 3]
    assert all(question["quality"]["status"] == "pass" for question in result["questions"])
    assert [question["comprehensive_archetype"] for question in result["questions"]] == [
        "fault_diagnosis",
        "comparative_decision",
        "integrated_explanation",
    ]
    assert all(question["material_form"] for question in result["questions"])
    assert all(question["cognitive_sequence"] for question in result["questions"])
    assert all(question["structure_signature"]["signature_hash"] for question in result["questions"])


def test_generation_graph_retries_recent_structure_conflict_with_safe_revision_instruction():
    recent = build_structure_signature(
        archetype="fault_diagnosis",
        material_form="symptom_list",
        cognitive_sequence=["analyze", "apply"],
        subquestion_actions=["analyze", "apply"],
        answer_boundaries=["定位依据、原因和修正", "定位依据、原因和修正"],
    )

    class RetryingGateway(ComprehensiveGateway):
        def plan_coverage(self, payload):
            self.planning_payloads.append(payload)
            material_form = "symptom_list" if len(self.planning_payloads) == 1 else "error_process"
            return {
                "directives": [
                    {
                        "item_index": 1,
                        "coverage_atom": "根据异常表现定位原因",
                        "answer_boundary": "定位依据、原因和修正",
                        "cognitive_level": "analyze",
                        "comprehensive_archetype": "fault_diagnosis",
                        "material_form": material_form,
                        "cognitive_sequence": ["analyze", "apply"],
                        "subquestion_count_range": [2, 3],
                    }
                ]
            }

    state = _comprehensive_state()
    state["plan_items"] = state["plan_items"][:1]
    state["knowledge_cards"] = {"c1": state["knowledge_cards"]["c1"]}
    state["recent_structure_signatures"] = [recent.model_dump()]
    gateway = RetryingGateway()

    result = build_generation_graph(gateway).invoke(state)

    assert len(gateway.planning_payloads) == 2
    revision = gateway.planning_payloads[1].global_policy["revision_instruction"]
    assert "综合题结构与近期试卷重复" in revision
    assert recent.structure_key in revision
    for forbidden in ("old stem", "old answer", "filename", "page", "evidence"):
        assert forbidden not in revision.lower()
    assert result["questions"][0]["material_form"] == "error_process"
