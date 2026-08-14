from app.services.generation_service import audit_question_set


def test_audit_blocks_answer_core_leaked_into_another_stem():
    questions = [
        {"item_index": 1, "question_type": "fill_blank", "stem": "将文本表示为向量的过程称为____。", "answer": "向量化", "coverage_atom": "向量化的基本定义", "answer_boundary": "向量化", "quality": {"status": "pass"}},
        {"item_index": 2, "question_type": "true_false", "stem": "向量化可以把文本转换为可计算的向量表示。", "answer": True, "coverage_atom": "向量表示的作用", "answer_boundary": "文本转换为向量表示", "quality": {"status": "pass"}},
    ]

    conflicts = audit_question_set(questions)

    assert any(conflict["code"] == "cross_answer_leak" and conflict["repair_item_index"] == 2 for conflict in conflicts)


def test_audit_blocks_repeated_coverage_atom_across_question_types():
    questions = [
        {"item_index": 1, "question_type": "single_choice", "stem": "问题一", "options": ["A", "B", "C", "D"], "answer": "A", "coverage_atom": "低秩矩阵的作用", "answer_boundary": "减少可训练参数", "quality": {"status": "pass"}},
        {"item_index": 2, "question_type": "short_answer", "stem": "问题二", "answer": "减少可训练参数", "explanation": "解析", "rubric": [{"point": "核心", "score": 2}], "coverage_atom": "低秩矩阵的作用", "answer_boundary": "减少可训练参数", "quality": {"status": "pass"}},
    ]

    conflicts = audit_question_set(questions)

    assert any(conflict["code"] == "duplicate_coverage_atom" for conflict in conflicts)


def test_audit_marks_excessive_parenthetical_explanations_for_repair():
    questions = [
        {"item_index": 1, "question_type": "true_false", "stem": "训练数据（即监督信号）用于模型训练（也就是参数更新）。", "answer": True, "coverage_atom": "训练数据的作用", "answer_boundary": "用于模型训练", "quality": {"status": "pass"}}
    ]

    conflicts = audit_question_set(questions)

    assert conflicts[0]["code"] == "excessive_parentheses"
    assert conflicts[0]["repair_item_index"] == 1
