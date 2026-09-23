"""可交付内容判定：题干 + 答案，宁缺勿滥。"""
from app.services.paper_version_service import _has_usable_content


def test_usable_content_requires_both_stem_and_answer():
    assert _has_usable_content({"stem": "题干", "answer": "答案"}) is True
    assert _has_usable_content({"stem": "题干", "answer": "  "}) is False
    assert _has_usable_content({"stem": "  ", "answer": "答案"}) is False
    assert _has_usable_content({}) is False
    assert _has_usable_content({"stem": "题干"}) is False
    assert _has_usable_content({"answer": "答案"}) is False


def test_true_false_boolean_false_is_a_valid_answer():
    """判断题答案是布尔值，false 表示"错误"，不能当成缺答案。"""
    assert _has_usable_content({"stem": "SFT 会更新全部参数。", "answer": True}) is True
    assert _has_usable_content({"stem": "SFT 会更新全部参数。", "answer": False}) is True


def test_list_and_number_answers():
    assert _has_usable_content({"stem": "题干", "answer": ["甲", "乙"]}) is True
    assert _has_usable_content({"stem": "题干", "answer": []}) is False
    assert _has_usable_content({"stem": "题干", "answer": 0}) is True
    assert _has_usable_content({"stem": "题干", "answer": None}) is False


def test_missing_question_placeholder_is_not_usable():
    """生成侧三道防线失守留下的占位题：只有元数据，没有题干/答案。"""
    placeholder = {
        "item_index": 18,
        "question_type": "true_false",
        "score": 2.0,
        "difficulty": "medium",
        "coverage_atom": "某个原子",
        "answer_boundary": "某个答案域",
        "exam_point_id": "ep1",
        "unit_id": "u1",
        "card_id": "c1",
        "quality": {"status": "blocker", "message": "生成失败"},
        "needs_review": True,
    }
    assert _has_usable_content(placeholder) is False
