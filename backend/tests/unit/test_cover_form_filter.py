"""封面/表单/声明块过滤的离线单元测试。

知识目录构建与补证据候选都应排除实验报告封面、学生信息等非知识块，
同时不得误杀封面之后的真实正文（实验目的/步骤/结果）。
"""

from __future__ import annotations

from app.services.knowledge_publish_service import (
    _COVER_FORM_UNDERSCORE_PATTERN,
    _drop_cover_form_blocks,
    _is_cover_form_block,
)


def test_cover_form_block_detection():
    assert _is_cover_form_block("**实验报告封面**")
    assert _is_cover_form_block("<strong>课程名称：</strong> ... 课程代码")
    assert _is_cover_form_block("学生姓名：___  学号：____ 教学班：____")
    assert _is_cover_form_block("我申明，本报告内的实验已按要求完成")
    assert _is_cover_form_block("**实验报告评语与评分：**  **评阅老师签名：**")
    # 真实正文不应命中强信号
    assert not _is_cover_form_block("人类反馈强化学习通过奖励模型优化策略网络")


def test_cover_zone_underscore_signal():
    # 下划线填空占位是封面表单区的辅助信号（配合强信号定位后一并剔除）
    assert _COVER_FORM_UNDERSCORE_PATTERN.search("授课教师：_____________________")
    assert not _COVER_FORM_UNDERSCORE_PATTERN.search("def foo(a, b=10): return a+b")


def test_drop_cover_form_blocks_keeps_body():
    blocks = [
        {"text": "**实验报告封面**"},
        {"text": "<strong>课程名称：</strong> <strong><u>__大模型__</u></strong>"},
        {"text": "学生姓名：_____________________"},
        {"text": "学号：_____________________"},
        {"text": "递交日期：______________________"},
        {"text": "我申明，本报告内的实验已按要求完成，并没有抄袭行为。"},
        {"text": "申明人(签名)：_______________________"},
        {"text": "**实验报告评语与评分：**"},
        {"text": "**评阅老师签名：**"},
        {"text": "**一、实验名称：** 开源大模型本地部署、量化与推理基础"},
        {"text": "（1）了解开源大语言模型本地部署与推理的基本概念。"},
        {"text": "（2）掌握 Python、Transformers 等实验环境的安装与配置方法。"},
    ]
    filtered = _drop_cover_form_blocks(blocks)
    assert filtered == blocks[9:]
    assert "实验报告封面" not in "".join(b["text"] for b in filtered)
    assert "学生姓名" not in "".join(b["text"] for b in filtered)
    # 真实正文被完整保留
    assert filtered[0]["text"] == "**一、实验名称：** 开源大模型本地部署、量化与推理基础"
    assert len(filtered) == 3


def test_drop_cover_form_no_cover_keeps_all():
    blocks = [
        {"text": "向量数据库将文档切块并嵌入为向量"},
        {"text": "相似度检索返回最相关的知识块"},
    ]
    assert _drop_cover_form_blocks(blocks) == blocks


def test_drop_cover_form_stops_at_first_body_block():
    # 封面区中间若有正文信号，应停止剔除（防止把后续正文误删）
    blocks = [
        {"text": "**实验报告封面**"},
        {"text": "课程名称：____"},
        {"text": "**三、实验目的：**"},  # 强信号区已过，此为正文/模板过渡
        {"text": "（1）了解 RAG 基本概念和原理。"},
    ]
    filtered = _drop_cover_form_blocks(blocks)
    # 前 2 块被剔除，实验目的及其后正文保留
    assert filtered[0]["text"] == "**三、实验目的：**"
    assert len(filtered) == 2