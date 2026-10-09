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
    # "指标比较""评估中"的局部窗口与合同原子共现 → 位置级术语豁免（v2），
    # 不是对学生的认知要求 → 干净通过
    question = {
        "question_type": "single_choice",
        "difficulty": "low",
        "stem": "在公式识别模型的评估中，关于指标比较，下列说法正确的是？",
        "options": ["甲", "乙", "丙", "丁"], "answer": "甲",
    }
    atom = "模型评估中指标比较可通过对比不同模型版本在公式识别任务上的表现来完成。"
    clean = validate_generated_question(question, atom_text=atom)
    assert clean["status"] == "pass"
    assert clean["risks"] == []
    # 原子不含这些术语、题干也非任务表达（"…下列说法正确的是"是名词用法）
    # → v2 降级为风险提示，交教师复核，不再断言难度不符（改进方案 1.5）
    risky = validate_generated_question(question, atom_text="模型评估的基本流程")
    assert risky["status"] == "pass"
    assert risky["risks"]


def test_missing_stem_reports_missing_not_source_language():
    # 空题干曾与来源话术共用一个 code/message：quality_checks 与日志一律显示
    # "题目包含来源话术"，把"模型压根没给题干"完全盖住，排查时会被带偏。
    result = validate_generated_question(
        {"question_type": "single_choice", "stem": "", "options": [], "answer": ""}
    )
    assert result["status"] == "blocker"
    assert result["code"] == "stem_missing"
    assert result["message"] == "题目缺少题干"
