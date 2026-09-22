"""教师手动新增题目的最小约束：题干与答案必填。"""
import pytest

from app.services.paper_version_service import PaperVersionError, _validate_teacher_item
from app.services.generation_service import validate_generated_question


def _item(**over):
    base = {"stem": "题干", "question_type": "short_answer", "options": None, "answer": "答案"}
    base.update(over)
    return base


def test_teacher_item_requires_non_empty_answer():
    with pytest.raises(PaperVersionError, match="答案不能为空"):
        _validate_teacher_item(**_item(answer=""))
    with pytest.raises(PaperVersionError, match="答案不能为空"):
        _validate_teacher_item(**_item(answer="   "))


def test_teacher_item_requires_stem():
    with pytest.raises(PaperVersionError, match="题干不能为空"):
        _validate_teacher_item(**_item(stem="  "))


def test_teacher_item_true_false_false_answer_is_valid():
    """判断题答案 false 是合法答案，不能因" falsy "被当成缺答案。"""
    _validate_teacher_item(**_item(question_type="true_false", answer=False))


def test_teacher_item_multiple_choice_answer_must_be_option_keys():
    ok = _item(question_type="multiple_choice", options=["甲", "乙", "丙", "丁"], answer="AB")
    _validate_teacher_item(**ok)
    with pytest.raises(PaperVersionError, match="选项字母"):
        _validate_teacher_item(**_item(question_type="multiple_choice", options=["甲", "乙"], answer="Z"))
    with pytest.raises(PaperVersionError, match="至少需要两个选项"):
        _validate_teacher_item(**_item(question_type="multiple_choice", options=["甲"], answer="A"))
    with pytest.raises(PaperVersionError, match="不能为空"):
        _validate_teacher_item(**_item(question_type="multiple_choice", options=["甲", "乙"], answer=""))


def test_generated_multiple_choice_requires_answer_subset():
    ok = validate_generated_question({
        "question_type": "multiple_choice", "stem": "下列属于参数高效微调的有",
        "options": ["LoRA", "QLoRA", "SFT", "Prompt"], "answer": "AB",
    })
    assert ok["status"] == "pass"

    missing = validate_generated_question({
        "question_type": "multiple_choice", "stem": "下列属于参数高效微调的有",
        "options": ["LoRA", "QLoRA", "SFT", "Prompt"], "answer": "",
    })
    assert missing["status"] == "blocker" and missing["code"] == "multiple_choice_schema"

    single = validate_generated_question({
        "question_type": "multiple_choice", "stem": "下列属于参数高效微调的有",
        "options": ["LoRA", "QLoRA", "SFT", "Prompt"], "answer": "A",
    })
    assert single["status"] == "blocker" and single["code"] == "multiple_choice_answer"


def test_generated_essay_requires_answer():
    ok = validate_generated_question({"question_type": "essay", "stem": "论述题", "answer": "要点"})
    assert ok["status"] == "pass"
    bad = validate_generated_question({"question_type": "essay", "stem": "论述题", "answer": "  "})
    assert bad["status"] == "blocker" and bad["code"] == "answer_missing"
