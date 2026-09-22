"""导出渲染：题号去重、答案规范化、分节与缺答案标注。"""
from app.services.paper_version_service import (
    _answer_text,
    _difficulty_label,
    _render_question_html,
    _render_sections,
    _section_caption,
    _section_groups,
    _sections_table_html,
    _strip_stem_noise,
)


def test_stem_own_numbering_and_score_prefix_stripped():
    """题干自带编号/分值时必须剥掉，否则与导出题号叠成「1.1.」。"""
    assert _strip_stem_noise("1.1. 下列关于 LoRA 的说法") == "下列关于 LoRA 的说法"
    assert _strip_stem_noise("（10分）请补全代码") == "请补全代码"
    assert _strip_stem_noise("1. 题干一") == "题干一"
    assert _strip_stem_noise("2、题干二") == "题干二"
    assert _strip_stem_noise("1.1 题干三") == "题干三"
    assert _strip_stem_noise("  7.  题干五") == "题干五"


def test_stem_numbering_strip_does_not_eat_decimal_or_subquestion():
    """小数与题内分问编号不能误剥。"""
    assert _strip_stem_noise("3.14是圆周率近似值") == "3.14是圆周率近似值"
    assert _strip_stem_noise("（1）请补全代码") == "（1）请补全代码"
    assert _strip_stem_noise("RAG 可以缓解幻觉____") == "RAG 可以缓解幻觉____"


def test_answer_text_normalizes_boolean_and_list():
    assert _answer_text(True) == "正确"
    assert _answer_text(False) == "错误"
    assert _answer_text(None) == ""
    assert _answer_text(["x", "y"]) == "x、y"
    assert _answer_text("A") == "A"


def test_difficulty_label_accepts_string_and_int_enums():
    assert _difficulty_label("medium") == "中等"
    assert _difficulty_label("hard") == "困难"
    assert _difficulty_label("easy") == "容易"
    assert _difficulty_label(3) == "中等"
    assert _difficulty_label(None) == "未知"


def test_section_caption_omits_per_score_when_scores_differ():
    uniform = _section_groups([
        {"item_index": 1, "question_type": "single_choice", "score": 2, "stem": "甲", "answer": "A"},
        {"item_index": 2, "question_type": "single_choice", "score": 2, "stem": "乙", "answer": "B"},
    ])
    mixed = _section_groups([
        {"item_index": 3, "question_type": "short_answer", "score": 5, "stem": "丙", "answer": ""},
        {"item_index": 4, "question_type": "short_answer", "score": 8, "stem": "丁", "answer": ""},
    ])
    assert _section_caption(uniform[0]) == "一、单选题（共2题，每题2分，共4分）"
    assert _section_caption(mixed[0]) == "一、简答题（共2题，共13分）"


def test_render_sections_has_single_continuous_numbering():
    groups = _section_groups([
        {"item_index": 1, "question_type": "single_choice", "score": 2, "stem": "1. 题干一", "answer": "A",
         "options": ["甲", "乙", "丙", "丁"]},
        {"item_index": 2, "question_type": "true_false", "score": 2, "stem": "2. 题干二", "answer": True},
    ])
    html = _render_sections(groups, with_answer=False)
    # 题号只出现一次，且题干里的自带编号已被剥离
    assert '<span class="q-no">1.</span> 题干一' in html
    assert '<span class="q-no">2.</span> 题干二' in html
    assert "1. 1." not in html and "2. 2." not in html
    # 分节标题
    assert "一、单选题" in html and "二、判断题" in html


def test_render_sections_marks_missing_answer_and_true_false():
    groups = _section_groups([
        {"item_index": 1, "question_type": "true_false", "score": 2, "stem": "陈述句", "answer": False},
        {"item_index": 2, "question_type": "short_answer", "score": 5, "stem": "简述题", "answer": ""},
    ])
    html = _render_sections(groups, with_answer=True)
    assert '<span class="ans-label">【答案】</span>错误' in html
    assert "【缺答案·需人工补充】" in html
    # 不应泄漏 Python 的布尔字面量
    assert "True" not in html and "False" not in html


def test_sections_table_lays_out_section_scores():
    groups = _section_groups([
        {"item_index": 1, "question_type": "single_choice", "score": 2, "stem": "甲", "answer": "A"},
        {"item_index": 2, "question_type": "true_false", "score": 2, "stem": "乙", "answer": True},
    ])
    table = _sections_table_html(groups)
    assert "<th>题次</th>" in table
    assert "<th>一</th>" in table and "<th>二</th>" in table
    assert "<th>总分</th>" in table and "<th>评卷人</th>" in table
    assert "<td>4</td>" in table


def test_render_question_escapes_html_in_stem():
    html = _render_question_html(
        {"item_index": 1, "question_type": "short_answer", "stem": "<script>alert(1)</script>", "answer": "x", "score": 5},
        with_answer=True,
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
