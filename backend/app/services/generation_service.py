from __future__ import annotations

import re


def _compact_text(value) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(value or "")).lower()


def _bigram_features(text: str) -> set[str]:
    """提取文本的 bigram 特征用于语义相似度检测。"""
    cleaned = _compact_text(text)
    features: set[str] = set()
    for i in range(len(cleaned) - 1):
        features.add(cleaned[i : i + 2])
    return features


def _jaccard_similarity(left: set, right: set) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


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
        # 只统计中文全角括号解释；ASCII 括号属于代码、编号挖空等正常用法。
        parenthetical_pairs = stem.count("（")
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
            # 同考点语义聚类检测：同一 exam_point 下多个题目考查高度相似的子主题
            if left.get("exam_point_id") and left.get("exam_point_id") == right.get("exam_point_id"):
                left_stem = _compact_text(left.get("stem", ""))
                right_stem = _compact_text(right.get("stem", ""))
                left_features = _bigram_features(left.get("coverage_atom", "") or "")
                right_features = _bigram_features(right.get("coverage_atom", "") or "")
                atom_similarity = _jaccard_similarity(left_features, right_features)
                if atom_similarity > 0.5:
                    conflicts.append(
                        {
                            "code": "semantic_clustering",
                            "item_indexes": indexes,
                            "repair_item_index": right.get("item_index"),
                            "message": f"同考点（{left.get('exam_point_id')}）下题目语义高度重叠，bigram相似度={atom_similarity:.2f}",
                        }
                    )
    return conflicts


def validate_generated_question(question: dict) -> dict:
    qtype = question.get("question_type")
    stem = str(question.get("stem", "")).strip()
    difficulty = question.get("difficulty", "medium")
    if not stem or re.search(r"根据(课件|资料)|第\s*\d+\s*(页|章|讲)|实验\s*\d+", stem, re.IGNORECASE):
        return {"status": "blocker", "code": "source_language", "message": "题目包含来源话术"}
    # 难度合理性检查：低难度题不应出现"分析、评价、设计"等高级认知关键词
    if difficulty == "low":
        high_order_hints = [
            "分析", "评价", "评估", "设计", "创造", "比较", "对比",
            "综合", "判断并说明", "论证", "批判", "优化",
        ]
        if any(hint in stem for hint in high_order_hints):
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
