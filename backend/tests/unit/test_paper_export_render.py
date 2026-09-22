from app.services.paper_version_service import _render_question_html


def test_true_false_boolean_answer_renders_as_chinese_judgment():
    """判断题答案在后端是布尔值，导出时不能渲染成 Python 的 True/False。"""
    yes = _render_question_html(
        {"item_index": 1, "question_type": "true_false", "stem": "全量微调会更新所有参数。", "answer": True},
        with_answer=True,
    )
    no = _render_question_html(
        {"item_index": 2, "question_type": "true_false", "stem": "LoRA 只调整低秩矩阵。", "answer": False},
        with_answer=True,
    )
    assert "正确" in yes and "True" not in yes
    assert "错误" in no and "False" not in no


def test_student_paper_hides_answer_but_keeps_options():
    html = _render_question_html(
        {
            "item_index": 1,
            "question_type": "single_choice",
            "stem": "下列关于 LoRA 的说法正确的是",
            "options": ["只调低秩矩阵", "更新全部权重", "仅用于推理", "改变预训练权重"],
            "answer": "A",
        },
        with_answer=False,
    )
    assert "【答案】" not in html
    assert "只调低秩矩阵" in html


def test_choice_answer_string_rendered_verbatim():
    html = _render_question_html(
        {
            "item_index": 1,
            "question_type": "single_choice",
            "stem": "题干",
            "options": ["甲", "乙", "丙", "丁"],
            "answer": "B",
        },
        with_answer=True,
    )
    assert "【答案】" in html and ">B<" in html
