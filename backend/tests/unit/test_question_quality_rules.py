from app.services.generation_service import validate_generated_question


def test_single_choice_requires_four_options_and_one_answer():
    result = validate_generated_question({"question_type": "single_choice", "stem": "问题", "options": ["A", "B"], "answer": "A"})
    assert result["status"] == "blocker"


def test_quality_blocks_source_language_and_accepts_complete_short_answer():
    blocked = validate_generated_question({"question_type": "short_answer", "stem": "根据课件第1页回答", "answer": "答案", "explanation": "解析", "rubric": [{"point": "核心", "score": 2}]})
    assert blocked["status"] == "blocker"
    accepted = validate_generated_question({"question_type": "short_answer", "stem": "解释RAG流程", "answer": "检索后生成", "explanation": "解析", "rubric": [{"point": "说明检索", "score": 2}]})
    assert accepted["status"] == "pass"
