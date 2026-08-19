from __future__ import annotations

import re


def _compact_text(value) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(value or "")).lower()


def validate_generated_question(question: dict, atom_text: str = "") -> dict:
    qtype = question.get("question_type")
    stem = str(question.get("stem", "")).strip()
    difficulty = question.get("difficulty", "medium")
    if not stem or re.search(r"根据(课件|资料)|第\s*\d+\s*(页|章|讲)|实验\s*\d+", stem, re.IGNORECASE):
        return {"status": "blocker", "code": "source_language", "message": "题目包含来源话术"}
    # 难度合理性检查：低难度题不应出现"分析、评价、设计"等高级认知关键词。
    # 豁免：关键词同时出现在合同原子原文中时，它是被考查的术语本身
    # （如原子"指标比较可通过……完成"中的"比较"），不是对学生的认知要求。
    if difficulty == "low":
        high_order_hints = [
            "分析", "评价", "评估", "设计", "创造", "比较", "对比",
            "综合", "判断并说明", "论证", "批判", "优化",
        ]
        if any(hint in stem for hint in high_order_hints if hint not in atom_text):
            return {
                "status": "blocker",
                "code": "difficulty_mismatch",
                "message": "低难度题目题干包含高级认知要求关键词，与指定难度不匹配",
            }
    if qtype == "single_choice" and (len(question.get("options", [])) != 4 or not question.get("answer")):
        return {"status": "blocker", "code": "single_choice_schema", "message": "单选题必须有四个选项和答案"}
    if qtype == "true_false" and not isinstance(question.get("answer"), bool):
        return {"status": "blocker", "code": "true_false_schema", "message": "判断题答案必须为布尔值"}
    if qtype in {"fill_blank", "short_answer", "comprehensive"} and not str(question.get("answer", "")).strip():
        return {"status": "blocker", "code": "answer_missing", "message": "题目缺少答案"}
    if qtype == "fill_blank":
        blank_runs = re.findall(r"_{2,}", stem)
        if len(blank_runs) != 1:
            return {
                "status": "blocker",
                "code": "fill_blank_count",
                "message": f"填空题必须恰好包含 1 个空（当前 {len(blank_runs)} 个），分值与空数一一对应",
            }
        # 填空题答案应该简短（不超过20个汉字或40个字符）
        answer_text = str(question.get("answer", "")).strip()
        if len(answer_text) > 40 or len(re.findall(r"[\u4e00-\u9fff]", answer_text)) > 20:
            return {
                "status": "blocker",
                "code": "fill_blank_answer_length",
                "message": "填空题答案过长，应简短唯一（不超过20个汉字或40个字符）",
            }
        # 填空题答案不应包含完整句子
        if re.search(r"[。！？；，、]", answer_text):
            return {
                "status": "blocker",
                "code": "fill_blank_sentence",
                "message": "填空题答案不应包含标点，应为简短术语或数值",
            }
    if qtype in {"short_answer", "comprehensive"} and (not question.get("explanation") or not question.get("rubric")):
        return {"status": "blocker", "code": "rubric_missing", "message": "主观题必须有解析和评分细则"}
    if qtype == "comprehensive":
        if not question.get("subquestions"):
            return {"status": "blocker", "code": "subquestions_missing", "message": "综合题必须包含相互关联的分问"}
        if question.get("comprehensive_archetype") == "code_completion_scenario":
            numbered_blanks = re.findall(r"_+\(\d+\)_+", stem)
            if len(numbered_blanks) < 4:
                return {
                    "status": "blocker",
                    "code": "code_blanks_missing",
                    "message": f"代码填空综合题的题干须含至少 4 处编号挖空 ____________(1)__________（当前 {len(numbered_blanks)} 处）",
                }
            subquestions = question.get("subquestions") or []
            if len(subquestions) != 2:
                return {
                    "status": "blocker",
                    "code": "code_scenario_subquestions",
                    "message": "代码填空综合题固定两个分问：补全代码与问题分析",
                }
    return {"status": "pass", "code": "ok", "message": "通过基础质量检查"}


def audit_paper_against_contract(slots, questions) -> dict:
    """合同终检：配额一致、原子唯一、答案互斥、溯源完整、needs_review 清零。"""
    from app.domain.generation.contract import boundaries_overlap

    checks: list[dict] = []
    slot_counts: dict[str, int] = {}
    question_counts: dict[str, int] = {}
    for slot in slots:
        slot_counts[slot.exam_point_id] = slot_counts.get(slot.exam_point_id, 0) + 1
    for question in questions:
        ep = question.get("exam_point_id", "")
        question_counts[ep] = question_counts.get(ep, 0) + 1
    checks.append({
        "code": "quota_match", "passed": slot_counts == question_counts,
        "detail": {"contract": slot_counts, "paper": question_counts},
    })

    atoms = [_compact_text(q.get("coverage_atom")) for q in questions]
    checks.append({
        "code": "atom_uniqueness",
        "passed": len(atoms) == len(set(atoms)),
        "detail": {"total": len(atoms), "unique": len(set(atoms))},
    })

    ordered = sorted(questions, key=lambda q: q.get("item_index", 0))
    collisions = []
    for i, left in enumerate(ordered):
        for right in ordered[i + 1:]:
            if boundaries_overlap(str(left.get("answer_boundary", "")), str(right.get("answer_boundary", ""))):
                collisions.append([left.get("item_index"), right.get("item_index")])
    checks.append({"code": "answer_mutex", "passed": not collisions, "detail": {"collisions": collisions}})

    missing = [
        q.get("item_index") for q in questions
        if not all(q.get(f) for f in ("exam_point_id", "unit_id", "card_id", "coverage_atom"))
    ]
    checks.append({"code": "traceability", "passed": not missing, "detail": {"missing": missing}})

    review_count = sum(1 for q in questions if q.get("needs_review"))
    checks.append({"code": "needs_review", "passed": review_count == 0, "detail": {"count": review_count}})
    return {"passed": all(c["passed"] for c in checks), "checks": checks}
