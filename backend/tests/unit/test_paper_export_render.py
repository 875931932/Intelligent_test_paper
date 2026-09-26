"""导出渲染：题号去重、答案规范化、分节与缺答案标注、综合题分问与答题卡。"""
import app.services.paper_version_service as pvs
from app.services.paper_version_service import (
    _answer_text,
    _difficulty_label,
    _render_question_html,
    _render_sections,
    _section_caption,
    _section_groups,
    _sections_table_html,
    _stem_html,
    _strip_stem_noise,
    _sub_prompt,
    export_answer_card_html,
    export_answer_key_html,
    export_student_paper_html,
)


class _FakeSession:
    """只支撑 _paper_meta 需要的 scalar()；其余 DB 交互都在测试前被 monkeypatch 掉。"""

    def scalar(self, statement):
        return "大模型调优与部署技术"


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


def _comprehensive(**over) -> dict:
    q = {
        "item_index": 7,
        "question_type": "comprehensive",
        "stem": (
            "补全下面的加载代码。\n"
            "```python\nloader = WebBaseLoader(url)\nraw = ______(1)______()\n```\n"
            "最后检索 4 个片段。"
        ),
        "answer": "load()",
        "score": 10,
        "difficulty": "hard",
        "options": [],
        "subquestions": [
            {"prompt": "1. 请在不改变整体结构的前提下补全代码", "score": 6, "answer": "raw = loader.load()"},
            {"prompt": "（2）可以从哪些方向优化？", "score": 4, "answer": "换 chunk_size"},
        ],
    }
    q.update(over)
    return q


def test_render_question_renders_subquestions_with_scores():
    """综合题分问此前整段被丢掉：必须渲染成（1）题面（6分）。"""
    html = _render_question_html(_comprehensive(), with_answer=False)
    assert '<span class="sub-no">（1）</span> 请在不改变整体结构的前提下补全代码' in html
    assert '<span class="sub-score">（6分）</span>' in html
    assert '<span class="sub-no">（2）</span> 可以从哪些方向优化？' in html
    assert '<span class="sub-score">（4分）</span>' in html
    # 学生卷不出答案，也不出难度元信息
    assert "【答案】" not in html
    assert "难度：" not in html


def test_render_question_answer_key_gives_each_sub_answer():
    html = _render_question_html(_comprehensive(), with_answer=True)
    assert "换 chunk_size" in html  # 逐问答案，只可能来自 subquestions
    assert "难度：困难" in html  # 难度元信息仅答卷保留


def test_stem_code_fence_becomes_monospace_block():
    html = _render_question_html(_comprehensive(), with_answer=False)
    assert '<pre class="code">' in html
    assert "loader = WebBaseLoader(url)" in html
    assert "```" not in html


def test_stem_pipe_table_becomes_real_table():
    """综合题的 GFM 管道表格必须渲染成 <table>，不再是一竖线排的纯文字。"""
    stem = (
        "特性约束如下表所示。\n"
        "| 评价指标 | TurboMind | PyTorchEngine |\n"
        "|:---|:---:|---:|\n"
        "| 初始化资源占用 | 高 | 低 |\n"
        "| 生成速度 | 快 | 较慢 |\n"
        "请结合上表回答。"
    )
    html = _render_question_html(
        {"item_index": 1, "question_type": "short_answer", "stem": stem, "answer": "x", "score": 5},
        with_answer=True,
    )
    assert '<table class="md-table">' in html
    assert "<thead><tr><th>评价指标</th><th>TurboMind</th><th>PyTorchEngine</th></tr></thead>" in html
    assert "<td>初始化资源占用</td>" in html and "<td>较慢</td>" in html
    # 表格之外的正文仍在，竖线原文不再裸露
    assert "特性约束如下表所示。" in html and "请结合上表回答。" in html
    assert "| 评价指标" not in html and "|---" not in html


def test_pipe_table_inside_code_fence_stays_code():
    """围栏里的竖线行是代码本体，不能被误判成表格。"""
    html = _stem_html("对比：\n```text\n| a | b |\n|---|---|\n```")
    assert '<pre class="code">' in html
    assert "md-table" not in html
    assert "| a | b |" in html


def test_ragged_table_rows_pad_and_truncate_to_header_width():
    html = _stem_html("| a | b | c |\n|---|---|---|\n| 1 | 2 |\n| 1 | 2 | 3 | 4 |")
    # 两行数据都归到 3 列：少的补齐、多的截齐，表格列数才不塌
    assert html.count("<td>") == 6


def test_student_paper_adds_answer_blank_and_section_hint():
    groups = _section_groups([
        {"item_index": 1, "question_type": "single_choice", "score": 2, "stem": "甲", "answer": "A",
         "options": ["a", "b", "c", "d"]},
        {"item_index": 2, "question_type": "true_false", "score": 2, "stem": "乙", "answer": True},
        {"item_index": 3, "question_type": "short_answer", "score": 5, "stem": "丙", "answer": "x"},
    ])
    html = _render_sections(groups, with_answer=False)
    assert '<span class="q-no">1.</span> 甲（  ）' in html
    assert '<span class="q-no">2.</span> 乙（ ）' in html
    assert '<span class="q-no">3.</span> 丙' in html and "丙（" not in html  # 主观题不补括号
    assert "（将答案写在答题纸上）" in html
    assert "对的打钩 √" in html
    assert "难度：" not in html


def test_student_paper_does_not_double_answer_blank():
    groups = _section_groups([
        {"item_index": 1, "question_type": "single_choice", "score": 2, "stem": "甲（  ）", "answer": "A",
         "options": ["a", "b", "c", "d"]},
    ])
    assert _render_sections(groups, with_answer=False).count("（  ）") == 1


def test_subquestion_prompt_drops_own_index_but_keeps_year():
    """分问编号由导出自己编，prompt 自带的要剥；年份开头的题面不能误伤。"""
    assert _sub_prompt({"prompt": "（2）可以从哪些方向优化？"}) == "可以从哪些方向优化？"
    assert _sub_prompt({"prompt": "1. 请在不改变整体结构的前提下补全代码"}) == "请在不改变整体结构的前提下补全代码"
    assert _sub_prompt({"prompt": "（2024）真题：请说明原因"}) == "（2024）真题：请说明原因"
    assert _sub_prompt("纯字符串分问") == "纯字符串分问"


_SAMPLE_PV = {
    "id": "pv1",
    "exam_project_id": "p1",
    "project_name": "第一学期",
    "version_no": 6,
    "total_score": 4,
    "status": "candidate",
    "questions": [
        {"item_index": 1, "question_type": "single_choice", "stem": "1. 题干一", "answer": "A", "score": 2,
         "options": ["甲", "乙", "丙", "丁"], "difficulty": "medium"},
        {"item_index": 2, "question_type": "true_false", "stem": "2. 题干二", "answer": False, "score": 2,
         "options": [], "difficulty": "medium"},
    ],
}


def test_export_end_to_end_builds_formal_document(monkeypatch):
    """端到端走导出入口：曾经因 _paper_meta 引用不存在的 courses 表而 500。"""
    monkeypatch.setattr(pvs, "get_paper_version", lambda *a, **k: dict(_SAMPLE_PV))
    html = export_student_paper_html(_FakeSession(), "pv1", course_id="c1")
    assert "考试卷" in html
    assert "大模型调优与部署技术" in html
    assert '<span class="q-no">1.</span> 题干一' in html
    assert "<th>题次</th>" in html and "<th>评卷人</th>" in html
    assert "一、单选题" in html and "二、判断题" in html
    # 学生卷不含答案
    assert "【答案】" not in html


def test_export_answer_key_marks_answers_and_missing(monkeypatch):
    pv = dict(_SAMPLE_PV)
    pv["questions"] = _SAMPLE_PV["questions"] + [
        {"item_index": 3, "question_type": "short_answer", "stem": "简述题", "answer": "", "score": 5,
         "options": [], "difficulty": "medium"},
    ]
    monkeypatch.setattr(pvs, "get_paper_version", lambda *a, **k: pv)
    html = export_answer_key_html(_FakeSession(), "pv1", course_id="c1")
    assert "答卷（含答案）" in html
    assert "装订线" in html
    assert '<span class="ans-label">【答案】</span>错误' in html
    assert "【缺答案·需人工补充】" in html
    assert "1 题缺答案" in html


def test_answer_card_renders_grid_lines_and_boxes(monkeypatch):
    """答题卡按范本：客观题格子表、填空一题一线、主观题矩形大框，全卷不出题面与答案。"""
    pv = dict(_SAMPLE_PV)
    pv["questions"] = _SAMPLE_PV["questions"] + [
        {"item_index": 3, "question_type": "fill_blank", "stem": "填空题干", "answer": "LLM", "score": 2,
         "options": [], "difficulty": "medium"},
        {"item_index": 4, "question_type": "short_answer", "stem": "简述题", "answer": "要点", "score": 5,
         "options": [], "difficulty": "medium"},
    ]
    monkeypatch.setattr(pvs, "get_paper_version", lambda *a, **k: pv)
    html = export_answer_card_html(_FakeSession(), "pv1", course_id="c1")
    assert "答题卡" in html
    # 客观题 → 空白格子表
    assert "<th>题号</th>" in html and "<th>1</th>" in html and "<th>2</th>" in html
    assert '<td class="blank-cell"></td>' in html
    # 填空题一题一线；简答题一个矩形大框（不按分值铺横线）
    assert html.count('class="fill-line"') == 1
    assert html.count('<div class="answer-box"></div>') == 1
    # 每节标题右侧带得分/评卷人小框（4 节）
    assert html.count('<table class="mark-box">') == 4
    # 考生信息栏在底部学号栏不重复
    assert '<div class="id-row">' in html
    assert '<div class="sign-row">' not in html
    # 不含题面与答案
    assert "题干一" not in html and "简述题" not in html and "【答案】" not in html


def test_answer_card_comprehensive_gets_full_page_boxes(monkeypatch):
    """综合题按范本给整页空白大框：除首题外逐题换页，不出分问文字与题面。"""
    pv = dict(_SAMPLE_PV)
    pv["questions"] = [_comprehensive(), _comprehensive(item_index=8)]
    monkeypatch.setattr(pvs, "get_paper_version", lambda *a, **k: pv)
    html = export_answer_card_html(_FakeSession(), "pv1", course_id="c1")
    assert html.count('class="answer-box answer-box--page"') == 2
    # 首题随节标题同块不换页，第二题起逐题换页
    assert html.count('class="card-box-item card-box-item--page"') == 1
    # 不出题面，也不出分问题面/分值（结构在学生卷上）
    assert "补全下面的加载代码" not in html
    assert "请在不改变整体结构" not in html and "可以从哪些方向优化" not in html
    assert "（6分）" not in html and "（4分）" not in html
