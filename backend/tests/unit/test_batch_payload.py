from app.domain.generation.batching import split_contract_into_batches
from app.domain.generation.contract import ContractSlot
from app.schemas.generation import compile_batch_generation_payload


def _slot(index: int, **overrides) -> ContractSlot:
    payload = dict(
        item_index=index, question_type="single_choice", score=2, difficulty="medium",
        cognitive_level="understand", assessment_mode="conceptual",
        exam_point_id="EP1", anchor_key="A1", unit_id="U1", card_id=f"C{index}",
        coverage_atom=f"原子{index}", answer_boundary=f"边界{index}",
        performance_statement="掌握某知识",
        prompt_material=[f"材料{index}"], scope_boundary={}, preferred_terms=["术语"],
    )
    payload.update(overrides)
    return ContractSlot(**payload)


def test_batch_payload_contains_all_slots_in_order():
    slots = [_slot(i) for i in (1, 2, 3)]
    batch = split_contract_into_batches(slots)[0]
    payload = compile_batch_generation_payload(batch, {})
    assert [q.item_index for q in payload.questions] == [1, 2, 3]
    assert payload.batch_id == "B01"
    assert payload.exam_point_ids == ["EP1"]


def test_batch_payload_carries_slot_content_and_card_fallback():
    slot = _slot(1)
    batch = split_contract_into_batches([slot])[0]
    payload = compile_batch_generation_payload(batch, {"C1": {
        "performance_statement": "卡级陈述", "prompt_material": ["卡级材料"],
        "preferred_terms": ["卡级术语"],
    }})
    spec = payload.questions[0]
    # slot 自带值优先
    assert spec.performance_statement == "掌握某知识"
    assert spec.prompt_material == ["材料1"]


def test_batch_payload_empty_slot_fields_fall_back_to_card():
    slot = _slot(1, performance_statement="", prompt_material=[], preferred_terms=[])
    batch = split_contract_into_batches([slot])[0]
    payload = compile_batch_generation_payload(batch, {"C1": {
        "performance_statement": "卡级陈述", "prompt_material": ["卡级材料"],
        "preferred_terms": ["卡级术语"],
    }})
    spec = payload.questions[0]
    assert spec.performance_statement == "卡级陈述"
    assert spec.prompt_material == ["卡级材料"]
    assert spec.preferred_terms == ["卡级术语"]


def test_batch_payload_forbidden_context_from_batch():
    slots = [_slot(i) for i in range(1, 9)]  # 拆两批
    batches = split_contract_into_batches(slots)
    first = sorted(batches, key=lambda b: b.slots[0].item_index)[0]
    payload = compile_batch_generation_payload(first, {})
    assert "原子7" in payload.forbidden_atoms
    assert "边界7" in payload.forbidden_answer_cores


def test_batch_instruction_mentions_complementarity_and_forbidden():
    batch = split_contract_into_batches([_slot(1), _slot(2)])[0]
    payload = compile_batch_generation_payload(batch, {})
    assert "互补" in payload.batch_instruction
    assert "forbidden_atoms" in payload.batch_instruction
    assert payload.output_schema["type"] == "array"


def test_comprehensive_slot_carries_template_and_contract_fields():
    comp = _slot(
        1, question_type="comprehensive", assessment_mode="application",
        comprehensive_archetype="case_analysis", material_form="case_text",
        cognitive_sequence=["understand", "apply"],
        subquestion_count_range=[2, 3], subquestion_actions=["提取事实", "解释因果"],
        answer_boundaries=["事实边界", "因果边界"],
    )
    batch = split_contract_into_batches([comp])[0]
    payload = compile_batch_generation_payload(batch, {})
    spec = payload.questions[0]
    assert spec.comprehensive_archetype == "case_analysis"
    assert spec.material_form == "case_text"
    assert spec.question_template  # 综合题模板非空
    assert "案例" in spec.question_template or "材料" in spec.question_template


def test_code_completion_archetype_gets_blank_format_rule():
    comp = _slot(
        1, question_type="comprehensive", assessment_mode="application",
        comprehensive_archetype="code_completion_scenario", material_form="code_skeleton",
        cognitive_sequence=["understand", "apply"],
        subquestion_count_range=[2, 2], subquestion_actions=["补全代码", "分析问题"],
        answer_boundaries=["代码补全", "问题分析"],
    )
    batch = split_contract_into_batches([comp])[0]
    payload = compile_batch_generation_payload(batch, {})
    assert "挖空" in payload.questions[0].question_template


def test_non_comprehensive_question_template_from_type_dict():
    batch = split_contract_into_batches([_slot(1)])[0]
    payload = compile_batch_generation_payload(batch, {})
    assert "四个互斥选项" in payload.questions[0].question_template or "选项" in payload.questions[0].question_template
    assert payload.questions[0].output_schema


def test_retry_revision_instruction_field_exists():
    batch = split_contract_into_batches([_slot(1)])[0]
    payload = compile_batch_generation_payload(batch, {})
    retried = payload.model_copy(update={"teacher_revision_instruction": "只修复题1"})
    assert retried.teacher_revision_instruction == "只修复题1"
