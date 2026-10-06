"""Blueprint 持久化服务：草稿、计划项增删改查、确认发布。

只导入和调用 blueprint_service.allocate_plan_items 纯函数，不修改引擎模块。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.schema import (
    assessment_units,
    blueprint_sections,
    blueprint_versions,
    content_domains,
    exam_points,
    exam_projects,
    framework_versions,
    knowledge_cards,
    plan_items,
)
from app.domain.blueprint.models import (
    ASSESSMENT_MODES,
    BlueprintPlan,
    BlueprintRequest,
    CardSemanticProfile,
    UnitCoverage,
)
from app.domain.framework.exam_rules import (
    canonical_question_type,
    normalize_exam_rules,
    rules_have_type_ratios,
    type_rules_from_ratios,
)
from app.domain.generation.archetypes import ARCHETYPE_CONTRACTS
from app.domain.generation.contract import build_exam_point_pools
from app.services.blueprint_service import (
    BlueprintValidationError,
    allocate_plan_items,
)
from app.services.contract_service import _point_capacity


class BlueprintPersistenceError(Exception):
    """持久化蓝图时发生的 DB / 约束错误。"""


# 当客户端未下发 type_rules（前端蓝图阶段无题型配置输入）时，
# 依据已发布命题框架的允许题型推导出的默认题型分布，合计 100 分。
# 仅保留框架实际允许的题型，避免造出框架不支持的题位。
_DEFAULT_TYPE_RULES: dict[str, dict[str, int]] = {
    "single_choice": {"count": 15, "score": 2},  # 30
    "true_false": {"count": 10, "score": 1},     # 10
    "fill_blank": {"count": 10, "score": 2},     # 20
    "short_answer": {"count": 4, "score": 5},    # 20
    "comprehensive": {"count": 2, "score": 10},  # 20
}


def _framework_payload(
    session: Session,
    *,
    course_id: str,
    framework_version_id: str = "",
) -> dict | None:
    """取框架 payload，优先指定版本；该版本没有题型比例时回退到当前已发布版本。

    知识目录发布时会 pin 住一个 framework_version_id。教师之后重建框架（重新解析
    考纲、拿到题型比例），已发布的知识目录仍指向旧版本——若不回退，蓝图就会拿旧
    版本的 payload 推导题型分布，与命题框架页显示的考核规则对不上。
    """
    def load(version_id: str) -> dict | None:
        try:
            payload = session.execute(
                select(framework_versions.c.payload).where(
                    framework_versions.c.id == version_id,
                    framework_versions.c.course_id == course_id,
                )
            ).scalar_one_or_none()
        except SQLAlchemyError:
            return None
        return payload if isinstance(payload, dict) else None

    if framework_version_id:
        payload = load(framework_version_id)
        if payload is not None and rules_have_type_ratios(payload.get("final_exam_rules")):
            return payload

    try:
        published = session.execute(
            select(framework_versions.c.payload)
            .where(
                framework_versions.c.course_id == course_id,
                framework_versions.c.status == "published",
            )
            .order_by(framework_versions.c.version_no.desc())
            .limit(1)
        ).scalar_one_or_none()
    except SQLAlchemyError:
        published = None
    if isinstance(published, dict):
        return published
    return load(framework_version_id) if framework_version_id else None


def _atom_capacity(
    session: Session,
    *,
    course_id: str,
    catalog_version_id: str,
) -> int | None:
    """知识卡池实际可出的题位数（与合同分配同口径），蓝图题位数上限。

    合同分配按考点逐题位取原子：池子不够的题位会被静默丢弃，卷面比例按
    题号顺序失真——排在各章末尾的综合题先全灭（2026-10 实测 42 题位 →
    29 题、综合 0 道）。这里按与合同执行完全一致的构造（catalog 全量
    active 单元 + 全量卡 + is_core 兜底 + 默认核心度门槛）求"最多能出多少
    题"，供题型分布等比缩容；查不到单元/卡时返回 None（不设上限）。
    """
    try:
        unit_rows = session.execute(
            select(assessment_units.c.id, assessment_units.c.exam_point_id)
            .where(
                assessment_units.c.catalog_version_id == catalog_version_id,
                assessment_units.c.course_id == course_id,
                assessment_units.c.status == "active",
            )
        ).all()
        card_rows = session.execute(
            select(
                knowledge_cards.c.id,
                knowledge_cards.c.assessable_content,
                knowledge_cards.c.answer_proposition,
                knowledge_cards.c.concept_cluster,
                knowledge_cards.c.assessment_unit_id,
            ).where(
                knowledge_cards.c.catalog_version_id == catalog_version_id,
                knowledge_cards.c.course_id == course_id,
            )
        ).all()
    except SQLAlchemyError:
        return None
    cards_by_unit: dict[str, list] = {}
    for row in card_rows:
        c = row._mapping
        if c["assessment_unit_id"]:
            cards_by_unit.setdefault(str(c["assessment_unit_id"]), []).append(c)
    cards: dict[str, dict] = {}
    units: list[UnitCoverage] = []
    for row in unit_rows:
        u = row._mapping
        if not u["exam_point_id"]:
            continue
        card_ids: list[str] = []
        for c in cards_by_unit.get(str(u["id"]), []):
            cid = str(c["id"])
            raw_atoms = list(c["assessable_content"] or [])
            cards[cid] = {
                "assessable_content": raw_atoms or [f"{cid} 默认知识原子"],
                "answer_proposition": c["answer_proposition"] or "",
                "answer_boundary": c["answer_proposition"] or "",
                "concept_cluster": c["concept_cluster"] or "",
                # 与合同执行同一兜底：核心度恒过默认门槛（见 contract_execution_service）
                "is_core": True,
            }
            card_ids.append(cid)
        if not card_ids:
            # 单元无卡：合同执行会补占位卡，容量至少 +1
            cid = f"__placeholder_{u['id']}"
            cards[cid] = {
                "assessable_content": [f"{cid} 默认知识原子"],
                "answer_proposition": "",
                "answer_boundary": "",
                "concept_cluster": cid,
                "is_core": True,
            }
            card_ids = [cid]
        units.append(UnitCoverage(
            unit_id=str(u["id"]),
            exam_point_id=str(u["exam_point_id"]),
            anchor_key=str(u["id"]),
            card_ids=card_ids,
        ))
    if not units:
        return None
    pools = build_exam_point_pools(units, cards)
    return sum(_point_capacity(pool) for pool in pools.values())


def _default_type_rules(
    session: Session,
    *,
    course_id: str,
    framework_version_id: str,
    capacity: int | None = None,
) -> dict:
    """未下发 type_rules 时推导默认题型分布。

    优先采用考核大纲声明的题型比例（框架 payload 里的 exam_rules），那才是考纲
    的硬约束；缺失或无法闭合到总分时才回退内置默认分布。
    ``capacity``（知识卡池实际可出的题位数）小于推导题数时按题型分值份额等比
    缩容：总分与题型占比不变、单题分值上调（见 type_rules_from_ratios）。
    结果受考点"允许题型"约束：中文题型名也做归一化——模型在 allowed_question_types
    里写的是"单选题"，早先直接与英文字段名比较，过滤几乎永远落空。
    """
    allowed: set[str] = set()
    try:
        rows = session.execute(
            select(exam_points.c.allowed_question_types).where(
                exam_points.c.framework_version_id == framework_version_id,
                exam_points.c.course_id == course_id,
                exam_points.c.status == "confirmed",
            )
        ).all()
        for r in rows:
            raw = r._mapping.get("allowed_question_types")
            if isinstance(raw, list):
                for entry in raw:
                    canonical = canonical_question_type(entry)
                    if canonical:
                        allowed.add(canonical)
    except SQLAlchemyError:
        allowed = set()

    def restrict(rules: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        if not allowed:
            return rules
        kept = {t: dict(rule) for t, rule in rules.items() if t in allowed}
        return kept or dict(rules)

    payload = _framework_payload(
        session, course_id=course_id, framework_version_id=framework_version_id
    )
    if payload is not None:
        # payload 里字段名是领域模型的 final_exam_rules
        exam_rules = payload.get("final_exam_rules")
        if rules_have_type_ratios(exam_rules):
            from_syllabus = type_rules_from_ratios(
                exam_rules["question_type_ratios"], total_score=100, capacity=capacity
            )
            if from_syllabus:
                return restrict(from_syllabus)

    if capacity is not None and capacity > 0:
        # 无考纲比例时同样按原子容量缩容：把内置默认分布写成"分值占比"
        # （30/10/20/20/20，与默认题数×分值完全等价）交给同一缩容路径。
        from_defaults = type_rules_from_ratios(
            [
                {"question_type": qt, "ratio": rule["count"] * rule["score"]}
                for qt, rule in _DEFAULT_TYPE_RULES.items()
            ],
            total_score=100,
            capacity=capacity,
        )
        if from_defaults:
            return restrict(from_defaults)

    return restrict(dict(_DEFAULT_TYPE_RULES))


def _assessment_focus(
    session: Session,
    *,
    course_id: str,
    framework_version_id: str,
) -> dict[str, float]:
    """读取考核大纲声明的考试侧重点（归一到 100 的各考查方式权重）。

    与题型比例同一来源（框架 payload 的 final_exam_rules）：教师在考核规则卡
    改完侧重点，下一次出卷即按新侧重点分配；未声明时为空 dict，蓝图按题型
    默认分布出卷。
    """
    payload = _framework_payload(
        session, course_id=course_id, framework_version_id=framework_version_id
    )
    rules = payload.get("final_exam_rules") if isinstance(payload, dict) else None
    focus: dict[str, float] = {}
    for entry in normalize_exam_rules(rules).get("assessment_focus", []):
        focus[str(entry["assessment_mode"])] = float(entry["weight"])
    return focus


def _difficulty_ratio(
    session: Session,
    *,
    course_id: str,
    framework_version_id: str,
) -> dict[str, float]:
    """读取考核大纲声明的难度比例（归一到 100 的简单/中等/困难占比）。

    与考试侧重点同一来源（框架 payload 的 final_exam_rules）：教师在考核规则卡
    改完难度比例，下一次出卷即按新比例逐题型确定性分配槽位难度；未声明时为
    空 dict，蓝图保持既有缺省（全 medium），不凭空造难度。
    """
    payload = _framework_payload(
        session, course_id=course_id, framework_version_id=framework_version_id
    )
    rules = payload.get("final_exam_rules") if isinstance(payload, dict) else None
    raw = normalize_exam_rules(rules).get("difficulty_distribution") or {}
    return {str(key): float(value) for key, value in raw.items()}


def _apply_difficulty(
    type_rules: dict,
    *,
    distribution: dict[str, float],
) -> dict:
    """把考核规则卡的难度比例折算进各题型的槽位难度分布。

    规则（与 _apply_assessment_focus 同约定，显式下发的永远优先）：教师/脚本在
    type_rules 里显式写下的 difficulty 键不动；未声明的题型注入规则卡的比例。
    注入随 type_rules 持久化，确认阶段防御性重跑输入一致。蓝图引擎按
    largest-remainder 把比例落成逐题位难度（每题型各自达标）。
    """
    if not distribution:
        return type_rules
    out = dict(type_rules)
    for qt, rule in type_rules.items():
        if isinstance(rule, dict) and "difficulty_distribution" not in rule:
            out[qt] = {**rule, "difficulty_distribution": dict(distribution)}
    return out


def _enrich_units_with_policy(
    session: Session,
    units_payload: list[dict],
    *,
    course_id: str,
    framework_version_id: str,
) -> None:
    """按考点行回填单元的实操考核政策（缺失才填，显式值尊重）。

    前端创建蓝图只下发 unit_id/exam_point_id/anchor_key/card_ids，UnitCoverage
    的 operational_detail_policy 缺省 supporting_only、allowed_assessment_modes
    缺省不含 practical_operation——实操可考性在 UI 流程里永远为假。考点行才是
    政策的权威来源，推导口径与 scripts/build_real_material_demo.py 一致
    （policy == directly_assessable 才允许实操）。
    """
    keys = {
        str(u.get("exam_point_id") or "").strip()
        for u in units_payload
        if isinstance(u, dict) and not u.get("operational_detail_policy")
    }
    keys.discard("")
    if not keys:
        return
    rows = session.execute(
        select(
            exam_points.c.id,
            exam_points.c.code,
            exam_points.c.operational_detail_policy,
        ).where(
            exam_points.c.course_id == course_id,
            or_(
                # id 全局唯一，直接匹配；code 只在本框架版本内唯一，防跨版本撞码
                exam_points.c.id.in_(keys),
                and_(
                    exam_points.c.code.in_(keys),
                    exam_points.c.framework_version_id == framework_version_id,
                ),
            ),
        )
    ).all()
    policy_by_key: dict[str, str] = {}
    for r in rows:
        policy_by_key[str(r._mapping["id"])] = r._mapping["operational_detail_policy"]
        if r._mapping["code"]:
            policy_by_key[str(r._mapping["code"])] = r._mapping["operational_detail_policy"]
    base_modes = [m for m in ASSESSMENT_MODES if m != "practical_operation"]
    for u in units_payload:
        if not isinstance(u, dict) or u.get("operational_detail_policy"):
            continue
        policy = policy_by_key.get(str(u.get("exam_point_id") or "").strip())
        if not policy:
            continue
        u["operational_detail_policy"] = policy
        if policy == "directly_assessable" and not u.get("allowed_assessment_modes"):
            u["allowed_assessment_modes"] = [*base_modes, "practical_operation"]


def _mode_eligible(
    unit: UnitCoverage,
    *,
    mode: str,
    question_type: str,
    card_question_types: dict[str, list[str]],
) -> bool:
    """单元是否可考该（题型 × 考查方式）组合——与蓝图引擎 _eligible_units 同口径。"""
    if mode not in unit.allowed_assessment_modes:
        return False
    if mode == "practical_operation" and unit.operational_detail_policy != "directly_assessable":
        return False
    return any(
        not card_question_types.get(cid) or question_type in card_question_types[cid]
        for cid in unit.card_ids
    )


def _normalize_mode_weights(weights: dict[str, float]) -> dict[str, float]:
    """考查方式权重归一到 100（非正权重剔除；全零返回空 dict）。"""
    positive = {m: w for m, w in weights.items() if w > 0}
    total = sum(positive.values())
    if total <= 0:
        return {}
    return {m: round(w / total * 100.0, 4) for m, w in positive.items()}


def _cap_practical_weight(
    weights: dict[str, dict[str, float]],
    *,
    type_rules: dict,
    units: list[UnitCoverage],
    chapter_weights: dict[str, float],
    total_score: float,
) -> None:
    """实操占比以"含可直考单元的章节权重和"为上界，超出按比例收敛（就地归一）。

    实操槽位只能落进可直考章，其总容量即这些章的权重和；超出的部分确定性地
    等比缩回上界内（留 1% 余量吸收题数取整），保证"没有足够可直考的实操单元"
    时出卷不失败。
    """
    mode = "practical_operation"
    if not any(dist.get(mode, 0) > 0 for dist in weights.values()):
        return
    eligible_anchors = {
        unit.anchor_key
        for unit in units
        if mode in unit.allowed_assessment_modes
        and unit.operational_detail_policy == "directly_assessable"
    }
    cap = sum(float(w) for a, w in chapter_weights.items() if a in eligible_anchors)
    # 全部章都可直考（cap≈100）时无实际上界；其余情况留 1% 余量
    limit = cap if cap > 99.99 else cap * 0.99
    for _ in range(20):
        demand = sum(
            float(type_rules[qt].get("count", 0))
            * float(type_rules[qt].get("score", 0))
            * dist.get(mode, 0)
            / 100.0
            for qt, dist in weights.items()
        )
        demand_pct = demand / total_score * 100.0 if total_score > 0 else 0.0
        if demand_pct <= limit + 0.01:
            return
        factor = limit / demand_pct if demand_pct > 0 else 0.0
        for dist in weights.values():
            dist[mode] = dist.get(mode, 0) * factor
            scaled = _normalize_mode_weights(dist)
            dist.clear()
            dist.update(scaled)


def _apply_assessment_focus(
    type_rules: dict,
    *,
    focus: dict[str, float],
    units: list[UnitCoverage],
    chapter_weights: dict[str, float],
    card_question_types: dict[str, list[str]],
    total_score: float,
) -> tuple[dict, set[str]]:
    """把考试侧重点折算进各题型的考查方式分布，并确定性收敛到可考范围。

    规则（教师/脚本显式下发的 assessment_mode_distribution 永远优先）：
    1. 逐（题型 × 方式）过滤无任何可考单元的组合，过滤到空就不注入，交回
       引擎默认分布（默认不含实操，天然可考）；
    2. 实操占比按可直考章容量收敛归一——没有可直考单元时实操归 0，蓝图
       照常生成；题位表逐题可见考查方式，收敛结果教师看得见。
    返回 (新 type_rules, 注入了分布的题型集合)。
    """
    if not focus:
        return type_rules, set()
    weights: dict[str, dict[str, float]] = {}
    for qt, rule in type_rules.items():
        if not isinstance(rule, dict) or "assessment_mode_distribution" in rule:
            continue
        eligible = {
            mode: w
            for mode, w in focus.items()
            if w > 0
            and any(
                _mode_eligible(
                    unit,
                    mode=mode,
                    question_type=qt,
                    card_question_types=card_question_types,
                )
                for unit in units
            )
        }
        if eligible:
            weights[qt] = _normalize_mode_weights(eligible)
    if not weights:
        return type_rules, set()
    _cap_practical_weight(
        weights,
        type_rules=type_rules,
        units=units,
        chapter_weights=chapter_weights,
        total_score=total_score,
    )
    applied: set[str] = set()
    out = dict(type_rules)
    for qt, dist in weights.items():
        if dist:
            out[qt] = {**type_rules[qt], "assessment_mode_distribution": dist}
            applied.add(qt)
    return out, applied


def _without_practical(type_rules: dict, applied: set[str]) -> dict:
    """从注入的分布里剔除实操（其余权重归一）——分配阶梯的中间退路。"""
    out: dict = {}
    for qt, rule in type_rules.items():
        if qt not in applied:
            out[qt] = rule
            continue
        dist = rule.get("assessment_mode_distribution") or {}
        rest = _normalize_mode_weights(
            {m: w for m, w in dist.items() if m != "practical_operation"}
        )
        if rest:
            out[qt] = {**rule, "assessment_mode_distribution": rest}
        else:
            out[qt] = {k: v for k, v in rule.items() if k != "assessment_mode_distribution"}
    return out


def _nid() -> str:
    """生成短小写 UUID。"""
    return uuid.uuid4().hex[:16]


def _row_to_dict(row) -> dict[str, Any]:
    """把 SQLAlchemy Row (mapping 或 tuple) 转为普通 dict。"""
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    return dict(row._asdict()) if hasattr(row, "_asdict") else dict(row)


def create_draft_blueprint(
    session: Session,
    *,
    course_id: str,
    project_id: str,
    framework_version_id: str,
    catalog_version_id: str,
    type_rules: dict,
    chapter_weights: dict,
    units_payload: list[dict],
    card_semantic_profiles: dict[str, dict],
    card_question_types: dict[str, list[str]],
    comprehensive_archetypes: list[str] | None = None,
) -> tuple[str, BlueprintPlan]:
    """创建草稿蓝图版本：分配计划 → 持久化 blueprint_version + sections + plan_items。"""
    # 0. 实操考核政策按考点行回填：前端 units 载荷不含该字段，缺失会让实操
    #    在 UI 流程里永远不可考（与 demo 脚本的 policy→modes 推导同口径）。
    _enrich_units_with_policy(
        session,
        units_payload,
        course_id=course_id,
        framework_version_id=framework_version_id,
    )
    # 1. 构造请求对象
    units = [UnitCoverage(**u) for u in units_payload]
    profiles = {
        cid: CardSemanticProfile(**p) if isinstance(p, dict) else p
        for cid, p in card_semantic_profiles.items()
    }
    # 未下发 type_rules（空 dict）时，依据已发布命题框架自动推导默认题型分布，
    # 保证蓝图阶段零输入也能生成完整计划项。
    if not type_rules:
        type_rules = _default_type_rules(
            session,
            course_id=course_id,
            framework_version_id=framework_version_id,
            # 卡池可出的题位数低于考纲比例推导的题数时等比缩容，避免合同
            # 阶段静默丢题（题型比例失真、综合题全灭）。
            capacity=_atom_capacity(
                session, course_id=course_id, catalog_version_id=catalog_version_id
            ),
        )
    # 教师显式综合题原型按序列表（可重复=数量，教师要两道代码题就写两次）：
    # 合同分配读取的是 type_rules.comprehensive.archetypes，这里在推导结果
    # 上合并注入，非编程课程可排除 code_completion_scenario（非法名在此
    # 过滤，调用方——AI 助手提案——已按 ARCHETYPE_CONTRACTS 严格校验）。
    # 只合入已声明的综合题规则：注入无 count 的新键会让分配算法按
    # "题数必须为正"报错——题型构成归考核规则，这里不擅自补题型。
    if comprehensive_archetypes:
        pool = [name for name in comprehensive_archetypes if name in ARCHETYPE_CONTRACTS]
        if not pool:
            raise BlueprintValidationError("comprehensive_archetypes 无合法原型名")
        existing = type_rules.get("comprehensive")
        if not isinstance(existing, dict):
            raise BlueprintValidationError(
                "type_rules 未声明 comprehensive 题型（考核规则的题型比例未含综合题），"
                "无法设置综合题原型池"
            )
        type_rules = {
            **type_rules,
            "comprehensive": {**existing, "archetypes": pool},
        }
    # 难度比例（考核规则卡声明）→ 各题型槽位难度分布：显式下发的题型规则
    # 不动，未声明的题型注入比例；注入随 type_rules 持久化，确认阶段防御性
    # 重跑输入一致。未声明时保持蓝图既有缺省（全 medium）。
    type_rules = _apply_difficulty(
        type_rules,
        distribution=_difficulty_ratio(
            session, course_id=course_id, framework_version_id=framework_version_id
        ),
    )
    # 章节权重可能来自考核大纲的原始 weight_value，未必归一化到 100。
    # 蓝图引擎要求各章权重合计 100，这里统一缩放。
    if chapter_weights:
        weight_sum = sum(float(w) for w in chapter_weights.values())
        if weight_sum <= 0:
            raise BlueprintValidationError("chapter weights must have a positive total")
        if abs(weight_sum - 100) > 0.01:
            scale = 100.0 / weight_sum
            chapter_weights = {k: float(v) * scale for k, v in chapter_weights.items()}
    # 考试侧重点（考核规则卡声明）→ 各题型考查方式分布，先确定性收敛到
    # 可考范围；注入的分布随 type_rules 持久化，确认阶段防御性重跑输入一致。
    total_score = sum(
        float(r.get("count", 0)) * float(r.get("score", 0))
        for r in type_rules.values()
    )
    focused_rules, focus_applied = _apply_assessment_focus(
        type_rules,
        focus=_assessment_focus(
            session, course_id=course_id, framework_version_id=framework_version_id
        ),
        units=units,
        chapter_weights=chapter_weights,
        card_question_types=card_question_types,
        total_score=total_score,
    )
    # 2. 确定性分配阶梯：注入侧重点 → 剔除实操 → 回退原分布（引擎默认）。
    #    与合同分配的阈值回退同款：逐级降级，全部失败才把校验错误抛给教师。
    candidates = [focused_rules]
    if focus_applied:
        candidates.append(_without_practical(focused_rules, focus_applied))
        candidates.append(type_rules)

    plan: BlueprintPlan | None = None
    last_error: BlueprintValidationError | None = None
    for rules in candidates:
        request = BlueprintRequest(
            total_score=total_score,
            type_rules=rules,
            chapter_weights=chapter_weights,
            units=units,
            card_semantic_profiles=profiles,
            card_question_types=card_question_types,
        )
        try:
            plan = allocate_plan_items(request)
            type_rules = rules
            break
        except BlueprintValidationError as exc:
            last_error = exc
        except Exception as exc:  # pragma: no cover - 引擎外的异常
            raise BlueprintPersistenceError(f"蓝图分配失败: {exc}") from exc
    if plan is None:
        raise last_error or BlueprintPersistenceError("蓝图分配失败")

    try:
        # 3. 计算 version_no
        current_max = session.execute(
            select(func.max(blueprint_versions.c.version_no))
            .where(
                blueprint_versions.c.exam_project_id == project_id,
                blueprint_versions.c.course_id == course_id,
            )
        ).scalar_one_or_none() or 0
        version_no = int(current_max) + 1

        # 4. 插入 blueprint_version
        bv_id = _nid()
        session.execute(
            blueprint_versions.insert().values(
                id=bv_id,
                course_id=course_id,
                exam_project_id=project_id,
                framework_version_id=framework_version_id,
                catalog_version_id=catalog_version_id,
                version_no=version_no,
                status="draft",
                type_rules=type_rules,
                chapter_weights=chapter_weights,
            )
        )
        # 项目状态与蓝图草稿同步，前端才能区分“尚未生成”和“已生成待教师确认”。
        # 同时把项目指向该蓝图版本：前端用 active_blueprint_version_id 判断
        # “已有蓝图”并进入合同阶段，之前只改 status 不写该字段导致蓝图创建
        # 成功后界面仍显示创建表单、无法进入合同。
        session.execute(
            exam_projects.update()
            .where(
                exam_projects.c.id == project_id,
                exam_projects.c.course_id == course_id,
            )
            .values(
                status="blueprint",
                active_blueprint_version_id=bv_id,
            )
        )

        # 5. 插入 plan_items（当前 BlueprintPlan 不含 sections，不建立
        # section → plan_items 关联，blueprint_section_id 一律留空）
        # 构建 anchor_key → assessment_unit_id 查找：从当前 catalog 的 assessment_units
        unit_rows = session.execute(
            select(assessment_units.c.id, assessment_units.c.exam_point_id, assessment_units.c.code)
            .where(
                assessment_units.c.catalog_version_id == catalog_version_id,
                assessment_units.c.course_id == course_id,
            )
        ).all()
        # 按 unit.code 或 unit.id 匹配 UnitCoverage.unit_id
        unit_by_code = {r._mapping["code"]: r._mapping for r in unit_rows}
        unit_by_id = {r._mapping["id"]: r._mapping for r in unit_rows}

        plan_item_rows = []
        for item in plan.items:
            # 从 units_payload 找匹配
            unit_match = None
            for u in units:
                if u.unit_id == item.unit_id:
                    unit_match = u
                    break
            if unit_match is None:
                raise BlueprintPersistenceError(
                    f"计划项 {item.item_index} 引用的 unit_id={item.unit_id} 不在 units_payload 中"
                )
            # 查找 assessment_unit_id
            au_row = unit_by_id.get(unit_match.unit_id) or unit_by_code.get(unit_match.unit_id)
            if au_row is None:
                raise BlueprintPersistenceError(
                    f"计划项 {item.item_index} 无法找到 assessment_unit：unit_id={item.unit_id}"
                )
            assessment_unit_id = au_row["id"]

            plan_item_rows.append({
                "id": _nid(),
                "course_id": course_id,
                "blueprint_version_id": bv_id,
                "blueprint_section_id": None,
                "assessment_unit_id": assessment_unit_id,
                "question_type": item.question_type,
                "assessment_mode": item.assessment_mode,
                "item_index": item.item_index,
                "score": float(item.score),
                "difficulty": item.difficulty,
                "cognitive_level": item.cognitive_level,
                "exam_point_id": item.exam_point_id or None,
                "knowledge_card_id": item.card_id or None,
            })

        if plan_item_rows:
            session.execute(plan_items.insert(), plan_item_rows)

        session.commit()
        return bv_id, plan

    except BlueprintValidationError:
        session.rollback()
        raise
    except BlueprintPersistenceError:
        session.rollback()
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        raise BlueprintPersistenceError(f"数据库错误: {exc}") from exc


def list_plan_items(
    session: Session, blueprint_version_id: str, *, course_id: str
) -> list[dict]:
    """列出某蓝图版本的全部计划项，附带单元/卡片/section 元数据。

    考点名（exam_point_title）与章节（anchor_key）按蓝图**自己的**框架版本解析：
    目录/框架重建后当前已发布目录里查不到这些 id，但旧版 exam_points 行仍留在
    表里，按 id 精确取当年的名字；历史数据曾用 code 当 exam_point_id，故 id/code
    双匹配（限定本蓝图框架版本，framework 内 code 唯一，不会放大行数）。
    """
    stmt = (
        select(
            plan_items,
            assessment_units.c.title.label("assessment_unit_title"),
            knowledge_cards.c.name.label("knowledge_card_name"),
            blueprint_sections.c.section_index,
            exam_points.c.title.label("exam_point_title"),
            exam_points.c.code.label("exam_point_code"),
            exam_points.c.anchor_key,
        )
        .select_from(plan_items)
        .join(assessment_units, assessment_units.c.id == plan_items.c.assessment_unit_id, isouter=True)
        .join(knowledge_cards, knowledge_cards.c.id == plan_items.c.knowledge_card_id, isouter=True)
        .join(blueprint_sections, blueprint_sections.c.id == plan_items.c.blueprint_section_id, isouter=True)
        .join(
            blueprint_versions,
            blueprint_versions.c.id == plan_items.c.blueprint_version_id,
            isouter=True,
        )
        .join(
            exam_points,
            and_(
                exam_points.c.framework_version_id == blueprint_versions.c.framework_version_id,
                or_(
                    exam_points.c.id == plan_items.c.exam_point_id,
                    exam_points.c.code == plan_items.c.exam_point_id,
                ),
            ),
            isouter=True,
        )
        .where(
            plan_items.c.blueprint_version_id == blueprint_version_id,
            plan_items.c.course_id == course_id,
        )
        .order_by(plan_items.c.item_index)
    )
    return [_row_to_dict(r) for r in session.execute(stmt).all()]


def update_plan_item(
    session: Session, plan_item_id: str, changes: dict, *, course_id: str
) -> dict:
    """更新单个计划项，修改后轻量校验总分合理性；失败则回滚。"""
    allowed_keys = {
        "score", "question_type", "difficulty", "cognitive_level",
        "exam_point_id", "card_id", "assessment_mode",
    }
    unknown = set(changes.keys()) - allowed_keys
    if unknown:
        raise BlueprintValidationError(f"不支持的修改字段: {sorted(unknown)}")

    # card_id → knowledge_card_id
    db_changes = dict(changes)
    if "card_id" in db_changes:
        db_changes["knowledge_card_id"] = db_changes.pop("card_id")

    if "score" in db_changes:
        score = float(db_changes["score"])
        # 0.5 步长校验
        if abs(score * 2 - round(score * 2)) > 0.001:
            raise BlueprintValidationError(
                f"score 必须按 0.5 步进，当前值={score}"
            )
        db_changes["score"] = score

    # 考查方式值域：5 枚举之外拒绝（教师手动下拉/AI 建议同口径）。
    # 与单元 allowed_assessment_modes 的兼容性校验留给合同分配兜底
    # （归正到原型允许值），此处只保证值合法。
    if "assessment_mode" in db_changes:
        mode = db_changes["assessment_mode"]
        if mode not in ASSESSMENT_MODES:
            raise BlueprintValidationError(
                f"assessment_mode 非法: {mode!r}（可用：{', '.join(ASSESSMENT_MODES)}）"
            )

    # 题型 canonical 校验：中文别名归一到英文枚举，未知题型拒绝——
    # 落库的必须是 generation_graph 认识的 canonical 值。
    if "question_type" in db_changes:
        canonical = canonical_question_type(db_changes["question_type"])
        if canonical is None:
            raise BlueprintValidationError(
                f"question_type 非法: {db_changes['question_type']!r}"
            )
        db_changes["question_type"] = canonical

    try:
        # 找到所属 blueprint_version_id
        # plan_item_id 来自路径参数：归属必须用调用方的 course_id 过滤校验，
        # 不能从行里反推 course_id（否则跨课程传 id 即可改到别的课程的题位）。
        bv_row = session.execute(
            select(
                plan_items.c.blueprint_version_id,
                blueprint_versions.c.status,
            )
            .join(
                blueprint_versions,
                blueprint_versions.c.id == plan_items.c.blueprint_version_id,
            )
            .where(
                plan_items.c.id == plan_item_id,
                plan_items.c.course_id == course_id,
            )
        ).one_or_none()
        if bv_row is None:
            raise BlueprintPersistenceError(f"plan_item 不存在: {plan_item_id}")
        bv_id = bv_row._mapping["blueprint_version_id"]
        # 冻结纪律：只有 draft 可原地改。已确认/被取代的蓝图，其难度与分值已被
        # 合同槽位拷贝，原地改会让两层脱节——那种情况只能新建蓝图版本。
        if bv_row._mapping["status"] != "draft":
            raise BlueprintPersistenceError(
                f"蓝图版本 status={bv_row._mapping['status']}，不可原地修改；"
                "请创建新版蓝图后再调整"
            )

        session.execute(
            plan_items.update()
            .where(
                plan_items.c.id == plan_item_id,
                plan_items.c.course_id == course_id,
            )
            .values(**db_changes)
        )

        # 重新加载该 blueprint_version 的所有 plan_items 校验总分
        all_scores = session.execute(
            select(plan_items.c.score)
            .where(
                plan_items.c.blueprint_version_id == bv_id,
                plan_items.c.course_id == course_id,
            )
        ).all()
        total = sum(float(r._mapping["score"]) for r in all_scores)
        # 总分必须是 0.5 的整数倍（与 blueprint_service 的 half-point 规则一致）
        if abs(total * 2 - round(total * 2)) > 0.01:
            session.rollback()
            raise BlueprintValidationError(
                f"总分校验失败: sum(score)={total}, 不是 0.5 的整数倍"
            )

        session.commit()

        refreshed = session.execute(
            select(plan_items).where(
                plan_items.c.id == plan_item_id,
                plan_items.c.course_id == course_id,
            )
        ).one()
        return _row_to_dict(refreshed)

    except BlueprintValidationError:
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        raise BlueprintPersistenceError(f"数据库错误: {exc}") from exc


def confirm_blueprint(
    session: Session,
    *,
    course_id: str,
    project_id: str,
    blueprint_version_id: str,
) -> dict:
    """确认蓝图：重跑分配校验 → 标记已确认 → 关联项目，旧版本置为 superseded。

    项目状态只进不退：仅 draft/blueprint 推进到 contract，已越过合同阶段
    （generating/review/exported）的项目补记确认时保持现值。
    """
    try:
        # 1. 加载 blueprint_version，校验归属与状态
        bv = session.execute(
            select(blueprint_versions)
            .where(
                blueprint_versions.c.id == blueprint_version_id,
                blueprint_versions.c.course_id == course_id,
                blueprint_versions.c.exam_project_id == project_id,
            )
        ).one_or_none()
        if bv is None:
            raise BlueprintPersistenceError("蓝图版本不存在或不属于此项目")
        bv_data = bv._mapping
        if bv_data["status"] != "draft":
            raise BlueprintPersistenceError(
                f"只能确认 draft 状态的蓝图，当前 status={bv_data['status']}"
            )

        # 2. 加载 plan_items 重建 units_payload 并调用 allocate_plan_items 防御性校验
        stored_items = session.execute(
            select(
                plan_items.c.item_index,
                plan_items.c.question_type,
                plan_items.c.score,
                plan_items.c.assessment_mode,
                plan_items.c.difficulty,
                plan_items.c.cognitive_level,
                plan_items.c.exam_point_id,
                plan_items.c.knowledge_card_id,
                assessment_units.c.id.label("unit_db_id"),
                assessment_units.c.code.label("unit_code"),
                assessment_units.c.exam_point_id.label("au_exam_point_id"),
            )
            .select_from(plan_items)
            .join(assessment_units, assessment_units.c.id == plan_items.c.assessment_unit_id)
            .where(
                plan_items.c.blueprint_version_id == blueprint_version_id,
                plan_items.c.course_id == course_id,
            )
            .order_by(plan_items.c.item_index)
        ).all()

        catalog_version_id = bv_data["catalog_version_id"]

        # 从 catalog 加载全部 units 以重建 anchor_key
        all_units = session.execute(
            select(
                assessment_units.c.id,
                assessment_units.c.code,
                assessment_units.c.exam_point_id,
                content_domains.c.framework_anchor_key,
            )
            .select_from(assessment_units)
            .join(content_domains, content_domains.c.id == assessment_units.c.content_domain_id, isouter=True)
            .where(
                assessment_units.c.catalog_version_id == catalog_version_id,
                assessment_units.c.course_id == course_id,
            )
        ).all()
        unit_info = {r._mapping["id"]: r._mapping for r in all_units}
        # 构建 unit_by_code/id → anchor_key
        def _anchor(unit_id: str) -> str:
            info = unit_info.get(unit_id)
            if info and info.get("framework_anchor_key"):
                return info["framework_anchor_key"]
            return unit_id

        used_unit_ids: dict[str, set[str]] = {}  # unit_id → {card_id}
        for row in stored_items:
            d = row._mapping
            uid = d["unit_db_id"]
            cid = d["knowledge_card_id"] or ""
            used_unit_ids.setdefault(uid, set()).add(cid)

        rebuilt_units = []
        for uid, card_set in used_unit_ids.items():
            info = unit_info.get(uid)
            if info is None:
                raise BlueprintPersistenceError(f"单元 {uid} 不在当前 catalog 中")
            rebuilt_units.append({
                "unit_id": uid,
                # info 即该单元自身在 assessment_units 中的行；此前还回退到
                # 上一循环的残留变量 d（最后一行 stored_item 的考点），会把
                # 别的单元的 exam_point_id 张冠李戴，故只认 info。
                "exam_point_id": info.get("exam_point_id") or "",
                "anchor_key": _anchor(uid),
                "card_ids": sorted(card_set) if card_set else ["__placeholder__"],
            })

        # 实操考核政策按考点行回填：创建阶段注入过实操分布的蓝图，确认时的
        # 防御性重跑必须看到同样的可考性，否则 "has no eligible chapter"。
        _enrich_units_with_policy(
            session,
            rebuilt_units,
            course_id=course_id,
            framework_version_id=bv_data["framework_version_id"],
        )

        # 加载卡片语义画像（如存在，否则默认）
        card_rows = session.execute(
            select(knowledge_cards.c.id, knowledge_cards.c.concept_cluster, knowledge_cards.c.answer_proposition)
            .where(
                knowledge_cards.c.catalog_version_id == catalog_version_id,
                knowledge_cards.c.course_id == course_id,
            )
        ).all()
        sem_profiles: dict[str, dict] = {}
        card_qtypes: dict[str, list[str]] = {}
        for cr in card_rows:
            c = cr._mapping
            sem_profiles[c["id"]] = {
                "concept_cluster": c.get("concept_cluster") or c["id"],
                "answer_proposition": c.get("answer_proposition") or c["id"],
            }

        # 重跑分配（防御性：若仍可分配则 OK；BlueprintValidationError 会冒泡）
        type_rules = bv_data.get("type_rules") or {}
        chapter_weights = bv_data.get("chapter_weights") or {}
        try:
            request_check = BlueprintRequest(
                total_score=sum(
                    float(r.get("count", 0)) * float(r.get("score", 0))
                    for r in type_rules.values()
                ),
                type_rules=type_rules,
                chapter_weights=chapter_weights,
                units=[UnitCoverage(**u) for u in rebuilt_units],
                card_semantic_profiles={
                    k: CardSemanticProfile(**v) for k, v in sem_profiles.items()
                },
                card_question_types=card_qtypes,
            )
            allocate_plan_items(request_check)
        except BlueprintValidationError:
            session.rollback()
            raise

        # 3. 把其他已确认版本置为 superseded
        session.execute(
            blueprint_versions.update()
            .where(
                blueprint_versions.c.exam_project_id == project_id,
                blueprint_versions.c.course_id == course_id,
                blueprint_versions.c.status == "confirmed",
                blueprint_versions.c.id != blueprint_version_id,
            )
            .values(status="superseded")
        )

        # 4. 更新当前版本状态
        session.execute(
            blueprint_versions.update()
            .where(
                blueprint_versions.c.id == blueprint_version_id,
                blueprint_versions.c.course_id == course_id,
            )
            .values(status="confirmed", confirmed_at=func.now())
        )

        # 5. 更新 exam_projects: 设置 active_blueprint_version_id；状态只进不退——
        # 仅 draft/blueprint 阶段推进到 contract；项目已越过合同阶段（generating/
        # review/exported，如生成完成后才补记蓝图确认）时保持现值，否则确认动作会把
        # 已完成的项目打回合同阶段，助手阶梯随之倒发 start_generation 提案。
        current_status = session.execute(
            select(exam_projects.c.status)
            .where(
                exam_projects.c.id == project_id,
                exam_projects.c.course_id == course_id,
            )
        ).scalar_one()
        session.execute(
            exam_projects.update()
            .where(
                exam_projects.c.id == project_id,
                exam_projects.c.course_id == course_id,
            )
            .values(
                active_blueprint_version_id=blueprint_version_id,
                status=(
                    "contract"
                    if current_status in ("draft", "blueprint")
                    else current_status
                ),
            )
        )

        session.commit()
        return {"status": "confirmed", "blueprint_version_id": blueprint_version_id}

    except BlueprintValidationError:
        raise
    except BlueprintPersistenceError:
        session.rollback()
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        raise BlueprintPersistenceError(f"数据库错误: {exc}") from exc
