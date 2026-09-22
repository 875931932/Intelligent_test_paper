"""教师手动新增题目的最小约束：题干与答案必填。"""
import pytest

from app.services.generation_service import answer_option_keys, validate_generated_question
from app.services.paper_version_service import PaperVersionError, _validate_teacher_item


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


def test_answer_option_keys_accepts_letters_and_option_text():
    """模型有时返回字母，有时按 schema 约定返回选项原文，两种都要能解析。"""
    opts = ["只调低秩矩阵", "更新全部权重", "仅用于推理", "改变预训练权重"]
    assert answer_option_keys("B", opts) == {"B"}
    assert answer_option_keys("ABD", opts) == {"A", "B", "D"}
    assert answer_option_keys("A,B", opts) == {"A", "B"}
    assert answer_option_keys("更新全部权重", opts) == {"B"}
    # 非纯字母的串不能按字母解析，否则 'LoRA' 会误命中 L/O/R/A
    assert answer_option_keys("LoRA 微调", opts) == set()
    assert answer_option_keys("", opts) == set()
    assert answer_option_keys("Z", opts) == set()


def test_teacher_item_multiple_choice_accepts_letter_or_text_answer():
    opts = ["甲", "乙", "丙", "丁"]
    _validate_teacher_item(**_item(question_type="multiple_choice", options=opts, answer="AB"))
    _validate_teacher_item(**_item(question_type="multiple_choice", options=opts, answer="甲、丙"))
    with pytest.raises(PaperVersionError, match="两个及以上"):
        _validate_teacher_item(**_item(question_type="multiple_choice", options=opts, answer="A"))


def test_generated_single_choice_answer_must_be_exactly_one_option():
    """模型曾给单选题返回 "AB"，导致单选题里出现多个正确项。"""
    ok = validate_generated_question({
        "question_type": "single_choice", "stem": "下列关于 LoRA 的说法正确的是",
        "options": ["只调低秩矩阵", "更新全部权重", "仅用于推理", "改变预训练权重"], "answer": "B",
    })
    assert ok["status"] == "pass"

    for bad_answer in ("AB", "", "E", "B、C"):
        result = validate_generated_question({
            "question_type": "single_choice", "stem": "下列关于 LoRA 的说法正确的是",
            "options": ["只调低秩矩阵", "更新全部权重", "仅用于推理", "改变预训练权重"], "answer": bad_answer,
        })
        assert result["status"] == "blocker", bad_answer
        assert result["code"] in {"single_choice_schema", "single_choice_answer"}, bad_answer


def test_generated_essay_requires_answer():
    ok = validate_generated_question({"question_type": "essay", "stem": "论述题", "answer": "要点"})
    assert ok["status"] == "pass"
    bad = validate_generated_question({"question_type": "essay", "stem": "论述题", "answer": "  "})
    assert bad["status"] == "blocker" and bad["code"] == "answer_missing"
