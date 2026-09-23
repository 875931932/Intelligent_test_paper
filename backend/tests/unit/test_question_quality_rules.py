from app.services.generation_service import validate_generated_question


def test_single_choice_requires_four_options_and_one_answer():
    result = validate_generated_question({"question_type": "single_choice", "stem": "问题", "options": ["A", "B"], "answer": "A"})
    assert result["status"] == "blocker"


def test_quality_blocks_source_language_and_accepts_complete_short_answer():
    blocked = validate_generated_question({"question_type": "short_answer", "stem": "根据课件第1页回答", "answer": "答案", "explanation": "解析", "rubric": [{"point": "核心", "score": 2}]})
    assert blocked["status"] == "blocker"
    accepted = validate_generated_question({"question_type": "short_answer", "stem": "解释RAG流程", "answer": "检索后生成", "explanation": "解析", "rubric": [{"point": "说明检索", "score": 2}]})
    assert accepted["status"] == "pass"


def _comprehensive(subquestions, score=10):
    return {
        "question_type": "comprehensive",
        "stem": "分析一个部署方案",
        "answer": "答案",
        "explanation": "解析",
        "rubric": [{"point": "方案", "score": 10}],
        "subquestions": subquestions,
        "score": score,
    }


def _sub(action="分析", answer="分问答案"):
    """schema 要求的完整分问对象（见 schemas/generation.py comprehensive output_schema）。"""
    return {
        "action": action,
        "prompt": "请分析该方案",
        "answer_boundary": "边界",
        "answer": answer,
        "rubric": [{"point": "要点", "score": 5}],
        "score": 5,
    }


def test_comprehensive_question_requires_subquestions_answer_and_rubric():
    # 缺分问 → blocker
    result = validate_generated_question(_comprehensive([]))
    assert result["status"] == "blocker" and result["code"] == "subquestions_missing"

    # 分问字段不全 → blocker
    partial = validate_generated_question(_comprehensive([{"action": "分析"}]))
    assert partial["status"] == "blocker" and partial["code"] == "subquestion_schema"

    # 分问分值之和 != 本题总分 → blocker（两个 5 分分问 = 10，本题声明 12）
    mismatched = validate_generated_question(_comprehensive([_sub(), _sub()], score=12))
    assert mismatched["status"] == "blocker" and mismatched["code"] == "subquestion_score_sum"

    # 完整且分值闭合 → pass
    complete = validate_generated_question(_comprehensive([_sub(), _sub()], score=10))
    assert complete["status"] == "pass"


def test_legitimate_model_weight_filename_wording_is_not_source_leakage():
    result = validate_generated_question({"question_type": "true_false", "stem": "模型服务名不必与权重文件名相同。", "answer": True})
    assert result["status"] == "pass"


def test_low_difficulty_keyword_in_examined_term_is_exempted():
    # "指标比较"中的"比较"是被考查的术语本身（出现在合同原子原文中），
    # 不是对学生的认知要求 → 豁免
    question = {
        "question_type": "single_choice",
        "difficulty": "low",
        "stem": "在公式识别模型的评估中，关于指标比较，下列说法正确的是？",
        "options": ["甲", "乙", "丙", "丁"], "answer": "甲",
    }
    atom = "模型评估中指标比较可通过对比不同模型版本在公式识别任务上的表现来完成。"
    assert validate_generated_question(question, atom_text=atom)["status"] == "pass"
    # 原子不含该关键词时（真认知要求）仍拦截
    assert validate_generated_question(question, atom_text="模型评估的基本流程")["status"] == "blocker"
