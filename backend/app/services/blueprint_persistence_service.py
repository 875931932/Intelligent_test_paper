"""Blueprint 持久化服务：草稿、计划项增删改查、确认发布。

只导入和调用 blueprint_service.allocate_plan_items 纯函数，不修改引擎模块。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
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
    BlueprintPlan,
    BlueprintRequest,
    CardSemanticProfile,
    UnitCoverage,
)
from app.domain.framework.exam_rules import (
    canonical_question_type,
    rules_have_type_ratios,
    type_rules_from_ratios,
)
from app.services.blueprint_service import (
    BlueprintValidationError,
    allocate_plan_items,
)


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


def _default_type_rules(
    session: Session,
    *,
    course_id: str,
    framework_version_id: str,
) -> dict:
    """未下发 type_rules 时推导默认题型分布。

    优先采用考核大纲声明的题型比例（框架 payload 里的 exam_rules），那才是考纲
    的硬约束；缺失或无法闭合到总分时才回退内置默认分布。
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
                exam_rules["question_type_ratios"], total_score=100
            )
            if from_syllabus:
                return restrict(from_syllabus)

    return restrict(dict(_DEFAULT_TYPE_RULES))


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
) -> tuple[str, BlueprintPlan]:
    """创建草稿蓝图版本：分配计划 → 持久化 blueprint_version + sections + plan_items。"""
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
    request = BlueprintRequest(
        total_score=sum(
            float(r.get("count", 0)) * float(r.get("score", 0))
            for r in type_rules.values()
        ),
        type_rules=type_rules,
        chapter_weights=chapter_weights,
        units=units,
        card_semantic_profiles=profiles,
        card_question_types=card_question_types,
    )

    # 2. 调用引擎分配计划
    try:
        plan: BlueprintPlan = allocate_plan_items(request)
    except BlueprintValidationError:
        raise
    except Exception as exc:  # pragma: no cover - 引擎外的异常
        raise BlueprintPersistenceError(f"蓝图分配失败: {exc}") from exc

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


def list_plan_items(session: Session, blueprint_version_id: str) -> list[dict]:
    """列出某蓝图版本的全部计划项，附带单元/卡片/section元数据。"""
    stmt = (
        select(
            plan_items,
            assessment_units.c.title.label("assessment_unit_title"),
            knowledge_cards.c.name.label("knowledge_card_name"),
            blueprint_sections.c.section_index,
        )
        .select_from(plan_items)
        .join(assessment_units, assessment_units.c.id == plan_items.c.assessment_unit_id, isouter=True)
        .join(knowledge_cards, knowledge_cards.c.id == plan_items.c.knowledge_card_id, isouter=True)
        .join(blueprint_sections, blueprint_sections.c.id == plan_items.c.blueprint_section_id, isouter=True)
        .where(plan_items.c.blueprint_version_id == blueprint_version_id)
        .order_by(plan_items.c.item_index)
    )
    return [_row_to_dict(r) for r in session.execute(stmt).all()]


def update_plan_item(session: Session, plan_item_id: str, changes: dict) -> dict:
    """更新单个计划项，修改后轻量校验总分合理性；失败则回滚。"""
    allowed_keys = {
        "score", "question_type", "difficulty", "cognitive_level",
        "exam_point_id", "card_id",
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

    try:
        # 找到所属 blueprint_version_id
        bv_row = session.execute(
            select(plan_items.c.blueprint_version_id, plan_items.c.course_id)
            .where(plan_items.c.id == plan_item_id)
        ).one_or_none()
        if bv_row is None:
            raise BlueprintPersistenceError(f"plan_item 不存在: {plan_item_id}")
        bv_id = bv_row._mapping["blueprint_version_id"]
        course_id = bv_row._mapping["course_id"]

        session.execute(
            plan_items.update()
            .where(plan_items.c.id == plan_item_id)
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
            select(plan_items).where(plan_items.c.id == plan_item_id)
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
    """确认蓝图：重跑分配校验 → 标记已确认 → 关联项目，旧版本置为 superseded。"""
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
            .where(plan_items.c.blueprint_version_id == blueprint_version_id)
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

        # 5. 更新 exam_projects: 设置 active_blueprint_version_id, status='contract'
        session.execute(
            exam_projects.update()
            .where(
                exam_projects.c.id == project_id,
                exam_projects.c.course_id == course_id,
            )
            .values(
                active_blueprint_version_id=blueprint_version_id,
                status="contract",
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
