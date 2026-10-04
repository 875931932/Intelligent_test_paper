"""提示词/校验器一致性：题型任务卡、答案形态、禁用上下文下发。"""
import pytest

from app.adapters.model.llm_gateway import LLMGateway
from app.domain.generation.batching import QuestionBatch
from app.domain.generation.contract import ContractSlot, ForbiddenContext
from app.schemas.generation import (
    BatchGenerationPayload,
    BatchQuestionSpec,
    _comprehensive_template_and_schema,
    _template_and_schema_for,
    compile_batch_generation_payload,
)
from app.workflows.generation_graph import _answer_hits_boundary


def _slot(**over):
    base = {
        "item_index": 1,
        "question_type": "single_choice",
        "score": 2.0,
        "difficulty": "medium",
        "cognitive_level": "understand",
        "assessment_mode": "conceptual",
        "exam_point_id": "ep1",
        "anchor_key": "ch1",
        "unit_id": "u1",
        "card_id": "card1",
        "coverage_atom": "LoRA 只调整低秩矩阵参数",
        "answer_boundary": "低秩适配参数",
        "forbidden_context": ForbiddenContext(atoms=["全参数微调"], answer_cores=["更新全部权重"]),
    }
    base.update(over)
    return ContractSlot(**base)


def test_every_blueprint_question_type_has_a_template():
    """蓝图/考纲可能出现的题型都必须有任务卡，否则 compile 会 KeyError 崩节点。"""
    for qtype in ("single_choice", "multiple_choice", "true_false", "fill_blank", "short_answer", "essay"):
        template, schema = _template_and_schema_for(qtype)
        assert template and schema


def test_unknown_question_type_raises_readable_error():
    with pytest.raises(ValueError, match="没有任务卡"):
        _template_and_schema_for("calculation")


def test_compile_payload_carries_slot_forbidden_context():
    """slot 级禁用清单必须随题下发：模型看到的要和校验查的同一份。"""
    slot = _slot()
    batch = QuestionBatch(
        batch_id="b1", anchor_key="ch1", exam_point_ids=["ep1"], slots=[slot],
        forbidden_context=ForbiddenContext(atoms=["批级原子"], answer_cores=[]),
    )
    payload = compile_batch_generation_payload(batch, {"card1": {"name": "LoRA 知识卡"}})
    spec = payload.questions[0]
    assert spec.forbidden_atoms == ["全参数微调"]
    assert spec.forbidden_answer_cores == ["更新全部权重"]
    # 指令面同样指向"该题"的清单，不再是批级清单
    assert "该题" in payload.batch_instruction
    assert "答案解析" in payload.batch_instruction or "解析" in payload.batch_instruction


def test_compile_payload_multiple_choice_generates_five_option_free_spec():
    """多选/论述曾缺任务卡，compile 时直接 KeyError（在重试路径上会崩整个节点）。"""
    slot = _slot(question_type="multiple_choice", item_index=2)
    batch = QuestionBatch(
        batch_id="b1", anchor_key="ch1", exam_point_ids=["ep1"], slots=[slot],
        forbidden_context=ForbiddenContext(),
    )
    payload = compile_batch_generation_payload(batch, {})
    spec = payload.questions[0]
    assert spec.question_type == "multiple_choice"
    assert "两个或以上" in spec.question_template
    assert "至少两个" in spec.output_schema["answer"]

    essay_slot = _slot(question_type="essay", item_index=3)
    essay_batch = QuestionBatch(
        batch_id="b1", anchor_key="ch1", exam_point_ids=["ep1"], slots=[essay_slot],
        forbidden_context=ForbiddenContext(),
    )
    essay_payload = compile_batch_generation_payload(essay_batch, {})
    assert "论述" in essay_payload.questions[0].question_template


def test_answer_boundary_accepts_letter_and_text_answers():
    """模型按字母作答是合法的（导出也按字母渲染），不能判"未命中答案域"。"""
    options = ["只调整低秩矩阵参数", "需要更新全部模型权重", "仅适用于推理阶段", "会改变原始预训练权重"]
    boundary = "低秩适配参数"

    # 字母答案
    assert _answer_hits_boundary({"answer": "A", "options": options}, boundary) is True
    # 选项原文答案
    assert _answer_hits_boundary({"answer": options[0], "options": options}, boundary) is True
    # 多选字母组合
    assert _answer_hits_boundary({"answer": "AB", "options": options}, boundary) is True
    # 与边界共享连续片段
    assert _answer_hits_boundary({"answer": "低秩", "options": []}, boundary) is True
    # 完全无关仍不通过
    assert _answer_hits_boundary({"answer": "量子纠缠", "options": []}, boundary) is False


def test_answer_boundary_empty_answer_still_fails():
    assert _answer_hits_boundary({"answer": "", "options": ["甲", "乙"]}, "边界") is False
    # 无边界时不做答案域校验
    assert _answer_hits_boundary({"answer": "", "options": []}, "") is True


def test_gateway_prompt_delegates_question_type_formats_to_task_card():
    """批次提示词不得写死题型/原型格式：格式只从任务卡下发。

    双源必漂移（改档案不改提示词），且 code_completion 专属格式写死在批次
    提示词里会污染其他原型批次——这正是"生成链路过拟合当前学科题风"的形态。
    """
    captured = {}

    class _Client:
        def request_json(self, *, system_prompt, payload, temperature,
                         call_context=None, response_validator=None,
                         reasoning_effort=None):
            captured["system_prompt"] = system_prompt
            result = {"questions": [{"item_index": 1}]}
            if response_validator:
                response_validator(result)
            return result

    payload = BatchGenerationPayload(
        batch_id="B01",
        exam_point_ids=["EP1"],
        questions=[
            BatchQuestionSpec(
                item_index=1, question_type="fill_blank", score=2, difficulty="medium",
                cognitive_level="understand", coverage_atom="原子", answer_boundary="边界",
                question_template="任务卡模板", output_schema={},
            )
        ],
        batch_instruction="指令",
        output_schema={"type": "array"},
    )
    gateway = LLMGateway(api_key="k", json_client=_Client())
    gateway.generate_batch(payload)

    system_prompt = captured["system_prompt"]
    # 原三处硬编码格式残余
    assert "恰好 1 个空" not in system_prompt
    assert "code_completion_scenario" not in system_prompt
    assert "__________(编号)__________" not in system_prompt
    # 指引模型到任务卡里找格式定义
    assert "question_template" in system_prompt


def test_comprehensive_extra_format_rule_follows_archetype():
    """原型专属格式只注入该原型的模板，不外溢到其他原型。"""
    cc_template, _ = _comprehensive_template_and_schema("code_completion_scenario", [2, 2])
    assert "挖空格式要求" in cc_template
    case_template, _ = _comprehensive_template_and_schema("case_analysis", [2, 3])
    assert "挖空格式要求" not in case_template
