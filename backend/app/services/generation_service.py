from __future__ import annotations

import re


def validate_generated_question(question: dict) -> dict:
    qtype = question.get("question_type")
    stem = str(question.get("stem", "")).strip()
    if not stem or re.search(r"根据(课件|资料)|第\s*\d+\s*(页|章|讲)|文件名|实验\s*\d+", stem, re.IGNORECASE):
        return {"status": "blocker", "code": "source_language", "message": "题目包含来源话术"}
    if qtype == "single_choice" and (len(question.get("options", [])) != 4 or not question.get("answer")):
        return {"status": "blocker", "code": "single_choice_schema", "message": "单选题必须有四个选项和答案"}
    if qtype == "true_false" and not isinstance(question.get("answer"), bool):
        return {"status": "blocker", "code": "true_false_schema", "message": "判断题答案必须为布尔值"}
    if qtype in {"fill_blank", "short_answer"} and not str(question.get("answer", "")).strip():
        return {"status": "blocker", "code": "answer_missing", "message": "题目缺少答案"}
    if qtype == "short_answer" and (not question.get("explanation") or not question.get("rubric")):
        return {"status": "blocker", "code": "rubric_missing", "message": "主观题必须有解析和评分细则"}
    return {"status": "pass", "code": "ok", "message": "通过基础质量检查"}
