from __future__ import annotations

import re

from app.domain.generation.difficulty_standard import check_difficulty_fit


def _compact_text(value) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(value or "")).lower()


def answer_option_keys(answer, options) -> set[str]:
    """把答案解析成选项字母集合，同时兼容三种实际形态：

    1. 字母形式 —— 模型经常无视"答案须与选项完全一致"的约定，直接返回 'B' / 'ABD'；
    2. 选项原文 —— schema 规定的形态（generation_graph 的 _answer_hits_boundary 按此判分）；
    3. 多个原文并列 —— 教师手写多选答案时常见的 '甲、丙'。

    只有整串都是选项字母时才按字母解析，否则按原文匹配，避免把 'LoRA' 里的
    L/O/R/A 误当成选项字母；两者都不命中时返回空集合，由调用方判为 blocker。
    """
    text = str(answer or "").strip()
    if not text:
        return set()
    opts = [str(o) for o in (options or [])]
    keys = {chr(65 + i) for i in range(len(opts))}

    def resolve_one(part: str) -> set[str]:
        part = part.strip()
        if not part:
            return set()
        upper = part.upper()
        if all(c in keys for c in upper):
            return set(upper)
        return {chr(65 + i) for i, o in enumerate(opts) if o == part}

    compact = re.sub(r"[,，、；;\s]+", "", text.upper())
    if compact and all(c in keys for c in compact):
        return set(compact)
    parts = [p for p in re.split(r"[,，、；;]+", text) if p.strip()]
    if len(parts) > 1:
        resolved: set[str] = set()
        for part in parts:
            resolved |= resolve_one(part)
        return resolved
    return resolve_one(text)


def validate_generated_question(question: dict, atom_text: str = "") -> dict:
    qtype = question.get("question_type")
    stem = str(question.get("stem", "")).strip()
    if not stem:
        return {"status": "blocker", "code": "stem_missing", "message": "题目缺少题干"}
    if re.search(r"根据(课件|资料)|第\s*\d+\s*(页|章|讲)|实验\s*\d+", stem, re.IGNORECASE):
        return {"status": "blocker", "code": "source_language", "message": "题目包含来源话术"}
    # 难度标准校验（v2 报告式：枚举严格校验 + 任务表达识别 + 风险/未验证分层，
    # 标准定义见 domain/generation/difficulty_standard.py）。难度/认知层次由合同
    # 槽位盖章后传入（generation_graph 先 _stamp_question 再校验）。blocker 与
    # 最终 pass 都携带 checks/risks/unverified——随 question["quality"] 持久化。
    fit = check_difficulty_fit(question, atom_text=atom_text)
    if fit["status"] != "pass":
        return fit
    if qtype == "single_choice":
        opts = question.get("options") or []
        answer_raw = question.get("answer")
        picked = answer_option_keys(answer_raw, opts)
        if len(opts) != 4 or not answer_raw:
            return {"status": "blocker", "code": "single_choice_schema", "message": "单选题必须有四个选项和答案"}
        # 只查"有答案"不够：模型会给单选题返回 "AB"，于是单选题里出现多个正确项
        if len(picked) != 1:
            return {"status": "blocker", "code": "single_choice_answer",
                    "message": "单选题答案必须唯一对应一个选项（写选项字母或选项原文），不能多选"}
    if qtype == "multiple_choice":
        opts = question.get("options") or []
        answer_raw = question.get("answer")
        picked = answer_option_keys(answer_raw, opts)
        # 任务卡规定四个互斥选项；校验与任务卡同口径，避免"提示词说四、校验放过三"
        if len(opts) != 4 or not answer_raw:
            return {"status": "blocker", "code": "multiple_choice_schema", "message": "多选题必须有四个选项和答案"}
        if len(picked) < 2:
            return {"status": "blocker", "code": "multiple_choice_answer", "message": "多选题答案必须对应两个及以上选项（写选项字母或选项原文）"}
    if qtype == "true_false" and not isinstance(question.get("answer"), bool):
        return {"status": "blocker", "code": "true_false_schema", "message": "判断题答案必须为布尔值"}
    if qtype in {"fill_blank", "short_answer", "comprehensive", "essay"} and not str(question.get("answer", "")).strip():
        return {"status": "blocker", "code": "answer_missing", "message": "题目缺少答案"}
    if qtype == "fill_blank":
        # 与任务卡同口径：连续下划线不少于 4 个（任务卡原话）。2-3 个下划线
        # 只是排版噪声，放过去会产出和任务卡不符的题。
        blank_runs = re.findall(r"_{4,}", stem)
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
        subquestions = question.get("subquestions") or []
        if not subquestions:
            return {"status": "blocker", "code": "subquestions_missing", "message": "综合题必须包含相互关联的分问"}
        # schema 要求每个分问都带全字段且分值和等于本题总分，校验器必须照此查，
        # 否则"prompt 要求、校验放行"会产出残缺分问，教师端看到缺胳膊少腿的综合题。
        required = ("action", "prompt", "answer_boundary", "answer", "rubric", "score")
        sub_total = 0.0
        for i, sub in enumerate(subquestions, start=1):
            if not isinstance(sub, dict):
                return {"status": "blocker", "code": "subquestion_schema", "message": f"第 {i} 个分问不是对象"}
            missing = [k for k in required if not sub.get(k) and sub.get(k) != 0]
            if missing:
                return {
                    "status": "blocker",
                    "code": "subquestion_schema",
                    "message": f"第 {i} 个分问缺少字段：{','.join(missing)}",
                }
            try:
                sub_total += float(sub.get("score") or 0)
            except (TypeError, ValueError):
                return {"status": "blocker", "code": "subquestion_schema", "message": f"第 {i} 个分问分值不是数字"}
        total = float(question.get("score") or 0)
        if total > 0 and abs(sub_total - total) > 0.01:
            return {
                "status": "blocker",
                "code": "subquestion_score_sum",
                "message": f"各分问分值之和（{sub_total:g}）应等于本题总分（{total:g}）",
            }
        if question.get("comprehensive_archetype") == "code_completion_scenario":
            numbered_blanks = re.findall(r"_+\(\d+\)_+", stem)
            if len(numbered_blanks) < 4:
                return {
                    "status": "blocker",
                    "code": "code_blanks_missing",
                    "message": f"代码填空综合题的题干须含至少 4 处编号挖空 ____________(1)__________（当前 {len(numbered_blanks)} 处）",
                }
            if len(subquestions) != 2:
                return {
                    "status": "blocker",
                    "code": "code_scenario_subquestions",
                    "message": "代码填空综合题固定两个分问：补全代码与问题分析",
                }
    return {
        "status": "pass", "code": "ok", "message": "通过基础质量检查",
        # 难度标准 v2 报告透传：实际检查了什么（checks）、疑似不匹配（risks）、
        # 无法自动判定的维度（unverified）——不是"没报错就是通过"。
        "checks": fit.get("checks", []),
        "risks": fit.get("risks", []),
        "unverified": fit.get("unverified", ()),
    }


def audit_paper_against_contract(slots, questions) -> dict:
    """合同终检：配额一致、原子唯一、答案互斥、溯源完整、needs_review 清零。

    配额按**合同分配的考点**统计：题位可能因自愈回补改考到同章其他考点
    （question.backfilled_from 记录原考点），对合同而言该题位的产出已兑现，
    因此计回原考点；回补明细单独放在 backfilled_slots 供教师与审计查看。
    """
    from app.domain.generation.contract import boundaries_overlap

    checks: list[dict] = []
    slot_counts: dict[str, int] = {}
    question_counts: dict[str, int] = {}
    for slot in slots:
        slot_counts[slot.exam_point_id] = slot_counts.get(slot.exam_point_id, 0) + 1
    for question in questions:
        backfill = question.get("backfilled_from") or {}
        ep = backfill.get("from_exam_point_id") or question.get("exam_point_id", "")
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
    backfilled = [
        {
            "item_index": q.get("item_index"),
            "from_exam_point_id": (q.get("backfilled_from") or {}).get("from_exam_point_id"),
            "to_exam_point_id": (q.get("backfilled_from") or {}).get("to_exam_point_id"),
            "anchor_key": (q.get("backfilled_from") or {}).get("anchor_key"),
        }
        for q in questions if q.get("backfilled_from")
    ]
    checks.append({
        "code": "backfill_within_chapter",
        "passed": all(b["anchor_key"] for b in backfilled),
        "detail": {"backfilled_slots": backfilled, "count": len(backfilled)},
    })

    # 难度一致性汇总（信息层）：blocker 在单题门禁已拦，这里把"检查了什么、
    # 还有什么没验证"汇总进 final_check——AI 整卷评审与教师端据此可读到风险项
    # 与未验证项（情境陌生度/推理复杂度），而不是只见一个笼统的 passed。
    difficulty_risks: list[dict] = []
    unverified_codes: set[str] = set()
    difficulty_passed = True
    for q in questions:
        report = check_difficulty_fit(q, atom_text=str(q.get("coverage_atom") or ""))
        difficulty_passed = difficulty_passed and report["status"] == "pass"
        for risk in report.get("risks", []):
            difficulty_risks.append({"item_index": q.get("item_index"), "risk": risk})
        unverified_codes.update(report.get("unverified", ()))
    checks.append({
        "code": "difficulty_consistency",
        "passed": difficulty_passed,
        "detail": {
            "items_checked": len(questions),
            "risks": difficulty_risks,
            "unverified": sorted(unverified_codes),
        },
    })

    return {"passed": all(c["passed"] for c in checks), "checks": checks,
            "backfilled_slots": backfilled}
