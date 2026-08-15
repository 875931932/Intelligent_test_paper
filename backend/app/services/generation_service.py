from __future__ import annotations

import re


def _compact_text(value) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(value or "")).lower()


def _question_surface(question: dict) -> str:
    options = question.get("options", [])
    if isinstance(options, dict):
        options = list(options.values())
    return _compact_text([question.get("stem", ""), *options])


def _similar_core(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) >= 4 and (left in right or right in left):
        return True
    return False


def audit_question_set(questions: list[dict]) -> list[dict]:
    conflicts: list[dict] = []
    ordered = sorted(questions, key=lambda row: int(row.get("item_index", 0)))

    seen_comprehensive_structures: dict[tuple[str, str], dict] = {}
    for question in ordered:
        if question.get("question_type") != "comprehensive":
            continue
        signature = question.get("structure_signature")
        if not isinstance(signature, dict):
            continue
        structure_key = str(signature.get("structure_key") or "").strip()
        signature_hash = str(signature.get("signature_hash") or "").strip()
        identity = ("structure_key", structure_key) if structure_key else ("signature_hash", signature_hash)
        if not identity[1]:
            continue
        first = seen_comprehensive_structures.get(identity)
        if first is None:
            seen_comprehensive_structures[identity] = question
            continue
        conflicts.append(
            {
                "code": "duplicate_comprehensive_structure",
                "item_indexes": [first.get("item_index"), question.get("item_index")],
                "repair_item_index": question.get("item_index"),
                "message": "同卷综合题结构重复",
            }
        )

    for question in ordered:
        stem = str(question.get("stem", ""))
        parenthetical_pairs = stem.count("（") + stem.count("(")
        if parenthetical_pairs > 1:
            conflicts.append(
                {
                    "code": "excessive_parentheses",
                    "item_indexes": [question.get("item_index")],
                    "repair_item_index": question.get("item_index"),
                    "message": "题干使用了过多括号解释，应改为直接、常用的课程表达",
                }
            )

    for left_index, left in enumerate(ordered):
        left_atom = _compact_text(left.get("coverage_atom"))
        left_answer = _compact_text(left.get("answer_boundary") or left.get("answer"))
        for right in ordered[left_index + 1 :]:
            right_atom = _compact_text(right.get("coverage_atom"))
            right_answer = _compact_text(right.get("answer_boundary") or right.get("answer"))
            indexes = [left.get("item_index"), right.get("item_index")]
            if left_atom and left_atom == right_atom:
                conflicts.append(
                    {
                        "code": "duplicate_coverage_atom",
                        "item_indexes": indexes,
                        "repair_item_index": right.get("item_index"),
                        "message": "不同题位重复考查同一知识原子",
                    }
                )
                continue
            if _similar_core(left_answer, right_answer):
                conflicts.append(
                    {
                        "code": "duplicate_answer_core",
                        "item_indexes": indexes,
                        "repair_item_index": right.get("item_index"),
                        "message": "不同题位的答案核心重叠",
                    }
                )
                continue
            if left_answer and len(left_answer) >= 2 and left_answer in _question_surface(right):
                conflicts.append(
                    {
                        "code": "cross_answer_leak",
                        "item_indexes": indexes,
                        "repair_item_index": right.get("item_index"),
                        "message": "前一题答案核心出现在后一题题干或选项中",
                    }
                )
            elif right_answer and len(right_answer) >= 2 and right_answer in _question_surface(left):
                conflicts.append(
                    {
                        "code": "cross_answer_leak",
                        "item_indexes": indexes,
                        "repair_item_index": right.get("item_index"),
                        "message": "后一题答案核心出现在前一题题干或选项中",
                    }
                )
    return conflicts


def validate_generated_question(question: dict) -> dict:
    qtype = question.get("question_type")
    stem = str(question.get("stem", "")).strip()
    if not stem or re.search(r"根据(课件|资料)|第\s*\d+\s*(页|章|讲)|实验\s*\d+", stem, re.IGNORECASE):
        return {"status": "blocker", "code": "source_language", "message": "题目包含来源话术"}
    if qtype == "single_choice" and (len(question.get("options", [])) != 4 or not question.get("answer")):
        return {"status": "blocker", "code": "single_choice_schema", "message": "单选题必须有四个选项和答案"}
    if qtype == "true_false" and not isinstance(question.get("answer"), bool):
        return {"status": "blocker", "code": "true_false_schema", "message": "判断题答案必须为布尔值"}
    if qtype in {"fill_blank", "short_answer", "comprehensive"} and not str(question.get("answer", "")).strip():
        return {"status": "blocker", "code": "answer_missing", "message": "题目缺少答案"}
    if qtype in {"short_answer", "comprehensive"} and (not question.get("explanation") or not question.get("rubric")):
        return {"status": "blocker", "code": "rubric_missing", "message": "主观题必须有解析和评分细则"}
    if qtype == "comprehensive" and not question.get("subquestions"):
        return {"status": "blocker", "code": "subquestions_missing", "message": "综合题必须包含相互关联的分问"}
    return {"status": "pass", "code": "ok", "message": "通过基础质量检查"}
