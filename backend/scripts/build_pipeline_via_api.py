"""构建完整 7 段式 pipeline.json 快照（完全通过 FastAPI TestClient + HTTP API）。

与 build_real_material_demo.py 同级，但：
- 不依赖任何外部模型网关（MinerU / DeepSeek），使用合成种子数据；
- 通过 TestClient 调用 Task 6 全链路 API（create-project → blueprints → plan-items
  → confirm blueprint → allocate contract → confirm contract → generate → poll
  → paper-version → confirm）；
- 输出同构的 7 段式 pipeline.json 到 ``frontend/public/demo/pipeline.json``，
  供 demo-viewer 回归对比（HANDOVER §9 双链路同步）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 路径：允许在 backend 目录外（repo root）也能直接 ``python backend/scripts/...``
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.schema import (
    Base,
    Course,
    User,
    assessment_units,
    content_domains,
    exam_points,
    framework_versions,
    knowledge_cards,
    knowledge_catalog_versions,
)
from app.db.session import get_session, get_session_factory

OUTPUT_DIR = ROOT / "frontend" / "public" / "demo"
OUTPUT_FILE = OUTPUT_DIR / "pipeline.json"

COURSE_ID = "c1"
PREFIX = f"/api/v1/courses/{COURSE_ID}"

# ---------------------------------------------------------------------------
# 种子配置
# ---------------------------------------------------------------------------

# 10 个考点（其中 6 个参与章节权重 × 题目分配；后 4 个仅用于满足 "exam_points × 10"）
EXAM_POINT_SPECS = [
    # id, anchor, code, title, requirement, weight, group, intent, allowed_types
    ("ep1", "CH1", "EP1", "数据结构基础", "掌握线性表、栈、队列的定义与典型应用",
     30, "CH1", "围绕线性表、栈、队列结构检索定义与场景",
     ["single_choice", "true_false", "fill_blank", "short_answer", "comprehensive"]),
    ("ep2", "CH2", "EP2", "树与二叉树", "掌握二叉树遍历、平衡树与典型场景",
     20, "CH2", "围绕树结构检索遍历规则与平衡条件",
     ["single_choice", "true_false", "fill_blank", "short_answer", "comprehensive"]),
    ("ep3", "CH3", "EP3", "图论基础", "掌握图的存储、遍历与最短路径算法",
     15, "CH3", "围绕图论检索存储结构与遍历/路径算法",
     ["single_choice", "true_false", "fill_blank", "short_answer", "comprehensive"]),
    ("ep4", "CH4", "EP4", "排序算法", "掌握比较类排序的原理、复杂度与稳定性",
     15, "CH4", "围绕排序检索复杂度分析与稳定性判断",
     ["single_choice", "true_false", "fill_blank", "short_answer", "comprehensive"]),
    ("ep5", "CH5", "EP5", "查找算法", "掌握二分查找、哈希表与 B 树族检索",
     10, "CH5", "围绕查找算法检索平均/最坏复杂度",
     ["single_choice", "true_false", "fill_blank", "short_answer", "comprehensive"]),
    ("ep6", "CH6", "EP6", "算法设计策略", "掌握分治、贪心、动态规划三种范式",
     10, "CH6", "围绕算法范式检索策略边界与典型案例",
     ["single_choice", "true_false", "fill_blank", "short_answer", "comprehensive"]),
    ("ep7", "EX7", "EP7", "算法复杂度分析", "会估算时间/空间复杂度阶",
     0, "EX7", "复杂度大 O 记号分析",
     ["single_choice", "true_false", "fill_blank"]),
    ("ep8", "EX8", "EP8", "字符串匹配", "掌握朴素匹配与 KMP 算法",
     0, "EX8", "字符串模式匹配算法检索",
     ["single_choice", "true_false", "fill_blank"]),
    ("ep9", "EX9", "EP9", "并查集", "掌握并查集的路径压缩与按秩合并",
     0, "EX9", "不相交集合操作检索",
     ["single_choice", "true_false", "fill_blank"]),
    ("ep10", "EX10", "EP10", "堆与优先队列", "掌握堆结构与优先队列应用",
     0, "EX10", "堆性质与优先队列检索",
     ["single_choice", "true_false", "fill_blank"]),
]

# 内容域：2 个 L1 × 3 个 L2
L1_DOMAINS = [
    ("cd-p1", None, 1, "P1", "P1", "第一部分 基础数据结构"),
    ("cd-p2", None, 1, "P2", "P2", "第二部分 算法与应用"),
]

L2_DOMAINS = [
    # id, parent_id, level, anchor_key, code, name
    ("cd-ch1", "cd-p1", 2, "CH1", "CH1", "第一章 线性表、栈与队列"),
    ("cd-ch2", "cd-p1", 2, "CH2", "CH2", "第二章 树与二叉树"),
    ("cd-ch3", "cd-p1", 2, "CH3", "CH3", "第三章 图论基础"),
    ("cd-ch4", "cd-p2", 2, "CH4", "CH4", "第四章 排序算法"),
    ("cd-ch5", "cd-p2", 2, "CH5", "CH5", "第五章 查找算法"),
    ("cd-ch6", "cd-p2", 2, "CH6", "CH6", "第六章 算法设计策略"),
]

# 6 个 assessment unit（1 个 L2 域 × 对应 1 个 EP1~6）
UNIT_SPECS = [
    ("au-ch1", "cd-ch1", "ep1", "U-CH1", "线性结构单元", "学生能在具体场景下应用线性结构", 30),
    ("au-ch2", "cd-ch2", "ep2", "U-CH2", "树结构单元", "学生能分析二叉树遍历与平衡树", 20),
    ("au-ch3", "cd-ch3", "ep3", "U-CH3", "图结构单元", "学生能应用图遍历与最短路径算法", 15),
    ("au-ch4", "cd-ch4", "ep4", "U-CH4", "排序单元", "学生能比较各排序算法并判定稳定性", 15),
    ("au-ch5", "cd-ch5", "ep5", "U-CH5", "查找单元", "学生能分析各查找结构的平均/最坏复杂度", 10),
    ("au-ch6", "cd-ch6", "ep6", "U-CH6", "策略单元", "学生能区分分治/贪心/动态规划范式边界", 10),
]

# 每 unit 2 张卡：6 × 2 = 12 张
CARD_LETTERS = {
    "au-ch1": ("LA", "线性结构族"),
    "au-ch2": ("LB", "树结构族"),
    "au-ch3": ("LC", "图结构族"),
    "au-ch4": ("LD", "排序算法族"),
    "au-ch5": ("LE", "查找结构族"),
    "au-ch6": ("LF", "算法策略族"),
}

# 题型规则：15 单选 @2 + 10 判断 @1 + 5 填空 @2 + 4 简答 @5 + 3 综合 @10
#   → 总分 30+10+10+20+30 = 100；题数 15+10+5+4+3 = 37
TYPE_RULES: dict[str, dict[str, Any]] = {
    "single_choice": {"count": 15, "score": 2},
    "true_false": {"count": 10, "score": 1},
    "fill_blank": {"count": 5, "score": 2},
    "short_answer": {"count": 4, "score": 5},
    "comprehensive": {"count": 3, "score": 10},
}

# 章节权：CH1~CH6 = 30/20/15/15/10/10，合计 100
CHAPTER_WEIGHTS: dict[str, int] = {
    "CH1": 30, "CH2": 20, "CH3": 15, "CH4": 15, "CH5": 10, "CH6": 10,
}


# ---------------------------------------------------------------------------
# 种子数据库
# ---------------------------------------------------------------------------


def _card_hash(card_id: str, idx: int) -> str:
    return hashlib.sha256(f"{card_id}|{idx}".encode()).hexdigest()


def build_seeded_factory(tmp_path: Path) -> tuple[Any, sessionmaker[Session]]:
    """创建 SQLite engine + sessionmaker，建表并写入合成种子。"""
    db_path = tmp_path / "pipeline_api.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

    with factory() as s:
        s.add(User(id="u1", display_name="T-Demo", role="teacher"))
        s.flush()
        s.add(Course(id=COURSE_ID, owner_id="u1", slug="data-struct", name="数据结构与算法"))
        s.commit()

        with s.begin():
            # framework version (published)
            s.execute(framework_versions.insert().values(
                id="fv-demo", course_id=COURSE_ID, version_no=1, status="published",
                payload={
                    "anchors": [
                        {"key": f"CH{i}", "title": f"第{i}章锚点"} for i in range(1, 7)
                    ] + [
                        {"key": f"EX{i}", "title": f"扩展锚点{i}"} for i in range(7, 11)
                    ],
                    "final_exam_rules": {
                        "format": "闭卷笔试",
                        "duration_minutes": 120,
                        "total_score": 100,
                    },
                },
            ))
            # catalog version (published)
            s.execute(knowledge_catalog_versions.insert().values(
                id="cv-demo", course_id=COURSE_ID, framework_version_id="fv-demo",
                version_no=1, status="published",
                payload={"construction_mode": "synthetic_seed"},
            ))
            # exam_points × 10
            rows = []
            for ep_id, anchor, code, title, req, w, group, intent, allowed in EXAM_POINT_SPECS:
                rows.append({
                    "id": ep_id, "course_id": COURSE_ID,
                    "framework_version_id": "fv-demo", "anchor_key": anchor,
                    "code": code, "title": title, "assessment_requirement": req,
                    "weight_value": w, "weight_source": "teacher_confirmed",
                    "weight_group_id": group, "priority": "normal",
                    "cognitive_targets": ["记忆", "理解", "应用", "分析"],
                    "assessment_orientations": ["conceptual", "application"],
                    "allowed_question_types": allowed,
                    "operational_detail_policy": "supporting_only",
                    "scope_boundary": {"chapter": anchor},
                    "required_evidence_roles": [],
                    "retrieval_intent": intent,
                    "teaching_anchor_keys": [],
                    "status": "active",
                })
            s.execute(exam_points.insert(), rows)

            # content_domains: L1 先插（parent=None），L2 再插（引用 L1）
            l1_rows = []
            for dom_id, parent, level, anchor, code, name in L1_DOMAINS:
                l1_rows.append({
                    "id": dom_id, "course_id": COURSE_ID, "catalog_version_id": "cv-demo",
                    "parent_domain_id": parent, "level": level,
                    "framework_anchor_key": anchor, "code": code, "name": name,
                    "status": "active",
                })
            s.execute(content_domains.insert(), l1_rows)

            l2_rows = []
            for dom_id, parent, level, anchor, code, name in L2_DOMAINS:
                l2_rows.append({
                    "id": dom_id, "course_id": COURSE_ID, "catalog_version_id": "cv-demo",
                    "parent_domain_id": parent, "level": level,
                    "framework_anchor_key": anchor, "code": code, "name": name,
                    "status": "active",
                })
            s.execute(content_domains.insert(), l2_rows)

            # assessment_units：6 个（对应 L2 域 × EP1~EP6）
            au_rows = []
            for au_id, domain_id, ep_id, code, title, perf, weight in UNIT_SPECS:
                au_rows.append({
                    "id": au_id, "course_id": COURSE_ID, "catalog_version_id": "cv-demo",
                    "content_domain_id": domain_id, "exam_point_id": ep_id,
                    "code": code, "title": title, "performance_statement": perf,
                    "scope_boundary": {"chapter": code},
                    "weight": weight, "status": "active",
                })
            s.execute(assessment_units.insert(), au_rows)

            # knowledge_cards：每 unit 2 张，共 12 张
            cards: list[dict[str, Any]] = []
            for au_id, _dom_id, _epid, code, _title, perf, _w in UNIT_SPECS:
                letter, cluster = CARD_LETTERS[au_id]
                for idx in (1, 2):
                    cid = f"c{letter.lower()}{idx}"
                    assessable = [
                        f"{letter}{idx}-原子1：{cluster}的核心定义 #{idx}",
                        f"{letter}{idx}-原子2：{cluster}的典型应用场景 #{idx}",
                        f"{letter}{idx}-原子3：{cluster}的易混辨析点 #{idx}",
                    ]
                    cards.append({
                        "id": cid, "course_id": COURSE_ID,
                        "catalog_version_id": "cv-demo", "assessment_unit_id": au_id,
                        "name": f"卡{letter}{idx}",
                        "performance_statement": perf,
                        "assessable_content": assessable,
                        "scope_boundary": {"unit_code": code},
                        "cognitive_targets": ["记忆", "理解", "应用", "分析"],
                        "allowed_question_types": [
                            "single_choice", "true_false", "fill_blank",
                            "short_answer", "comprehensive",
                        ],
                        "importance": 1,
                        "concept_cluster": cluster,
                        "answer_proposition": f"{letter}{idx}的答案边界表述：{cluster}子命题#{idx}",
                        "prompt_material": [f"{letter}{idx}提示语：{cluster}相关示例 #{idx}"],
                        "relation_edges": [],
                        "content_hash": _card_hash(cid, idx),
                        "status": "active",
                        "version": 1,
                    })
            s.execute(knowledge_cards.insert(), cards)

    return engine, factory


# ---------------------------------------------------------------------------
# TestClient 构建 + mock graph_invoke
# ---------------------------------------------------------------------------


def _make_units_and_cards_payload() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, list[str]]]:
    """构造 blueprint 创建所需的 units / semantic_profiles / card_question_types。"""
    # unit_id → (anchor_key, exam_point_id, card_ids)
    unit_map = {
        "au-ch1": ("CH1", "ep1", ["cla1", "cla2"]),
        "au-ch2": ("CH2", "ep2", ["clb1", "clb2"]),
        "au-ch3": ("CH3", "ep3", ["clc1", "clc2"]),
        "au-ch4": ("CH4", "ep4", ["cld1", "cld2"]),
        "au-ch5": ("CH5", "ep5", ["cle1", "cle2"]),
        "au-ch6": ("CH6", "ep6", ["clf1", "clf2"]),
    }
    units: list[dict[str, Any]] = []
    for au_id, (anchor, epid, card_ids) in unit_map.items():
        units.append({
            "unit_id": au_id, "exam_point_id": epid,
            "anchor_key": anchor, "card_ids": card_ids,
        })

    # 卡 → (cluster, answer_prop) 与题型
    card_profile_map: dict[str, tuple[str, str]] = {}
    for au_id, _d, _e, _c, _t, _perf, _w in UNIT_SPECS:
        letter, cluster = CARD_LETTERS[au_id]
        for idx in (1, 2):
            cid = f"c{letter.lower()}{idx}"
            card_profile_map[cid] = (
                cluster,
                f"{letter}{idx}的答案边界表述：{cluster}子命题#{idx}",
            )

    profiles: dict[str, dict[str, Any]] = {}
    qtypes: dict[str, list[str]] = {}
    ALL_TYPES = ["single_choice", "true_false", "fill_blank", "short_answer", "comprehensive"]
    for cid, (cluster, ans_prop) in card_profile_map.items():
        profiles[cid] = {
            "concept_cluster": cluster,
            "answer_proposition": ans_prop,
        }
        qtypes[cid] = list(ALL_TYPES)
    return units, profiles, qtypes


def make_client(engine: Any, factory: sessionmaker[Session]) -> TestClient:
    """构造带 dependency_overrides + mock_graph_invoke 的 TestClient。"""
    from app.main import app

    def _override_session():
        sess = factory()
        try:
            yield sess
        finally:
            sess.close()

    def _override_factory():
        return factory

    get_session_factory.cache_clear()
    from app.db.session import get_engine
    get_engine.cache_clear()

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_session_factory] = _override_factory

    # Mock graph_invoke：全部题 needs_review=False（避免 force=true 触发副作用），
    # 但为了让 "force=true" 代码路径确实走通一次，我们把第 1 道标记为 needs_review=True。
    def _mock_graph_invoke(session: Session, generation_run: dict, contract_snapshot: dict):
        from app.db.schema import plan_items as _pi
        bv_id = generation_run.get("blueprint_version_id")
        course_id = generation_run.get("course_id")
        rows = session.execute(
            select(_pi.c.id, _pi.c.question_type, _pi.c.score, _pi.c.difficulty,
                   _pi.c.cognitive_level, _pi.c.knowledge_card_id, _pi.c.item_index)
            .where(_pi.c.blueprint_version_id == bv_id, _pi.c.course_id == course_id)
            .order_by(_pi.c.item_index)
        ).all()
        out: list[dict[str, Any]] = []
        for i, r in enumerate(rows):
            qtype = r._mapping["question_type"] or "single_choice"
            # 第 1 道标 needs_review，其余全过
            needs_review = (i == 0)
            if qtype == "single_choice":
                options = [f"选项A-{i}", f"选项B-{i}", f"选项C-{i}", f"选项D-{i}"]
                answer = "A"
            elif qtype == "true_false":
                options = []
                answer = "true" if (i % 2 == 0) else "false"
            elif qtype == "fill_blank":
                options = []
                answer = f"填空答案_{i}"
            elif qtype == "short_answer":
                options = []
                answer = f"简答题参考要点_{i}：①要点一 ②要点二 ③要点三"
            else:  # comprehensive
                options = []
                answer = (
                    f"综合题解答_{i}：\n"
                    f"步骤1：分析问题边界\n"
                    f"步骤2：建立数学模型\n"
                    f"步骤3：设计算法并给出复杂度"
                )
            quality_checks = [
                {"check_type": "semantic", "status": "pass", "details": {"score": 0.91}},
                {"check_type": "answerable", "status": "pass", "details": {"score": 0.90}},
            ]
            if needs_review:
                quality_checks.append({
                    "check_type": "diversity",
                    "status": "warn",
                    "details": {"message": "与同组其他题相似度偏高，请教师复核", "score": 0.42},
                })
            out.append({
                "plan_item_id": r._mapping["id"],
                "knowledge_card_id": r._mapping["knowledge_card_id"],
                "stem": f"[API mock 题干 #{i + 1}] 类型={qtype}，分值={r._mapping['score']}",
                "options": options,
                "answer": answer,
                "question_type": qtype,
                "difficulty": r._mapping["difficulty"] or "medium",
                "cognitive_level": r._mapping["cognitive_level"] or "understand",
                "score": float(r._mapping["score"] or 0),
                "quality": {
                    "needs_review": needs_review,
                    "message": ("teacher check required" if needs_review else "ok"),
                    "quality_checks": quality_checks,
                },
            })
        return out

    app.state.mock_graph_invoke = _mock_graph_invoke

    client = TestClient(app)
    # 记录 cleanup（显式注册，稍后在调用方统一清理）
    client.__dict__["_pipeline_cleanup"] = _CleanupHandle(app)
    return client


class _CleanupHandle:
    def __init__(self, app):
        self._app = app

    def __call__(self):
        self._app.dependency_overrides.pop(get_session, None)
        self._app.dependency_overrides.pop(get_session_factory, None)
        if hasattr(self._app.state, "mock_graph_invoke"):
            delattr(self._app.state, "mock_graph_invoke")


# ---------------------------------------------------------------------------
# 轮询 task run
# ---------------------------------------------------------------------------


def _wait_for_task(client: TestClient, task_run_id: str,
                   max_attempts: int = 30, interval: float = 0.05) -> dict:
    for _ in range(max_attempts):
        r = client.get(f"{PREFIX}/exam-projects/task-runs/{task_run_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in ("succeeded", "failed", "cancelled"):
            return body
        time.sleep(interval)
    return body


# ---------------------------------------------------------------------------
# 核心：运行全链路
# ---------------------------------------------------------------------------


def run_pipeline_via_api(session_factory: sessionmaker[Session],
                         engine: Any,
                         write_path: Path | None = None,
                         ) -> dict[str, Any]:
    """通过 TestClient 跑完整条 Task 6 管线，返回 7 段式 pipeline 字典。

    Parameters
    ----------
    session_factory :
        已 seeding 的 sessionmaker（由 build_seeded_factory 产出）。
    engine :
        对应的 SQLAlchemy engine（用于最终 dispose）。
    write_path :
        若不为 None，则在收集完成后将 JSON 原子写入此路径。
    """
    pipeline: dict[str, Any] = {"status": "in_progress"}

    # framework / knowledge_tree 段：从 DB 直接读取原始种子记录，
    # 保持与 build_real_material_demo.py 输出结构同构。
    with session_factory() as s:
        fv = s.execute(select(framework_versions).where(
            framework_versions.c.id == "fv-demo",
            framework_versions.c.course_id == COURSE_ID,
        )).one()
        eps = s.execute(select(exam_points).where(
            exam_points.c.framework_version_id == "fv-demo",
            exam_points.c.course_id == COURSE_ID,
        ).order_by(exam_points.c.code)).all()
        cv = s.execute(select(knowledge_catalog_versions).where(
            knowledge_catalog_versions.c.id == "cv-demo",
            knowledge_catalog_versions.c.course_id == COURSE_ID,
        )).one()
        domains = s.execute(select(content_domains).where(
            content_domains.c.catalog_version_id == "cv-demo",
            content_domains.c.course_id == COURSE_ID,
        ).order_by(content_domains.c.level, content_domains.c.code)).all()
        aus = s.execute(select(assessment_units).where(
            assessment_units.c.catalog_version_id == "cv-demo",
            assessment_units.c.course_id == COURSE_ID,
        ).order_by(assessment_units.c.code)).all()
        cards = s.execute(select(knowledge_cards).where(
            knowledge_cards.c.catalog_version_id == "cv-demo",
            knowledge_cards.c.course_id == COURSE_ID,
        ).order_by(knowledge_cards.c.id)).all()

    framework_payload = dict(fv._mapping)
    framework_payload["exam_points"] = [dict(r._mapping) for r in eps]

    tree_payload = dict(cv._mapping)
    # 重建 tree：L1 → L2 → units → cards
    l1_by_id: dict[str, dict[str, Any]] = {}
    l2_by_parent: dict[str, list[dict[str, Any]]] = {}
    for r in domains:
        d = dict(r._mapping)
        if d["level"] == 1:
            d["sub_domains"] = []
            l1_by_id[d["id"]] = d
        else:
            l2_by_parent.setdefault(d["parent_domain_id"] or "", []).append(d)
    for lid, l1 in l1_by_id.items():
        l1["sub_domains"] = sorted(
            l2_by_parent.get(lid, []), key=lambda x: x["code"]
        )
    tree_payload["content_domains"] = sorted(
        l1_by_id.values(), key=lambda x: x["code"]
    )
    # 按 l2 域分组 units
    au_by_domain: dict[str, list[dict[str, Any]]] = {}
    card_by_unit: dict[str, list[dict[str, Any]]] = {}
    for r in aus:
        a = dict(r._mapping)
        a["cards"] = []
        au_by_domain.setdefault(a["content_domain_id"] or "", []).append(a)
    for r in cards:
        c = dict(r._mapping)
        card_by_unit.setdefault(c["assessment_unit_id"] or "", []).append(c)
    # 把 units 挂到 L2，cards 挂到 unit
    for l1 in tree_payload["content_domains"]:
        for l2 in l1["sub_domains"]:
            domain_units = sorted(
                au_by_domain.get(l2["id"], []), key=lambda x: x["code"]
            )
            for a in domain_units:
                a["cards"] = sorted(
                    card_by_unit.get(a["id"], []), key=lambda x: x["name"]
                )
            l2["assessment_units"] = domain_units

    client = None
    try:
        client = make_client(engine, session_factory)

        # 1) 创建 exam_project
        r = client.post(f"{PREFIX}/exam-projects", json={"name": "PipelineAPI-Project"})
        assert r.status_code == 201, f"create project: {r.status_code} {r.text}"
        proj = r.json()
        project_id = proj["id"]
        assert project_id

        # 2) POST blueprints
        units_payload, card_profiles, card_qtypes = _make_units_and_cards_payload()
        r = client.post(
            f"{PREFIX}/exam-projects/{project_id}/blueprints",
            json={
                "framework_version_id": "fv-demo",
                "catalog_version_id": "cv-demo",
                "type_rules": TYPE_RULES,
                "chapter_weights": CHAPTER_WEIGHTS,
                "units": units_payload,
                "card_semantic_profiles": card_profiles,
                "card_question_types": card_qtypes,
            },
        )
        assert r.status_code == 201, f"create blueprint: {r.status_code} {r.text}"
        bp_resp = r.json()
        blueprint_version_id = bp_resp["blueprint_version_id"]
        plan_from_create: list[dict[str, Any]] = list(bp_resp["plan"] or [])

        # 3) GET plan-items（单独 GET 一次，确保 API 端到端）
        r = client.get(
            f"{PREFIX}/exam-projects/{project_id}/blueprints/current/plan-items"
        )
        assert r.status_code == 200, f"get plan-items: {r.status_code} {r.text}"
        plan_items = r.json()
        assert isinstance(plan_items, list)

        # 4) POST confirm blueprint
        r = client.post(
            f"{PREFIX}/exam-projects/{project_id}/blueprints/current/confirm",
            json={},
        )
        assert r.status_code == 200, f"confirm blueprint: {r.status_code} {r.text}"
        bp_confirmed = r.json()
        assert bp_confirmed.get("status") == "confirmed"

        # 5) POST contracts/allocate
        r = client.post(
            f"{PREFIX}/exam-projects/{project_id}/contracts/allocate",
            json={},
        )
        assert r.status_code == 200, f"allocate contract: {r.status_code} {r.text}"
        alloc = r.json()
        used_threshold = alloc.get("used_threshold")
        snap = alloc.get("contract_snapshot") or {}
        slots = snap.get("slots") or []
        conflicts_history = alloc.get("conflicts_history") or []

        # 6) POST contracts/confirm（无修订）
        r = client.post(
            f"{PREFIX}/exam-projects/{project_id}/contracts/confirm",
            json={"slot_revisions": []},
        )
        assert r.status_code == 201, f"confirm contract: {r.status_code} {r.text}"
        conf_result = r.json()
        generation_run_id = conf_result.get("generation_run_id")
        assert generation_run_id

        # 7) POST generate → 202 + task_run_id
        r = client.post(
            f"{PREFIX}/exam-projects/{project_id}/generate",
            json={"mock_graph": True},
        )
        assert r.status_code == 202, f"generate: {r.status_code} {r.text}"
        task_run_id = r.json().get("task_run_id")
        assert task_run_id

        # 8) GET task-runs 轮询 → succeeded
        task_status = _wait_for_task(client, task_run_id)
        if task_status["status"] != "succeeded":
            raise RuntimeError(
                f"task failed status={task_status['status']} "
                f"err={task_status.get('error_message')}"
            )

        # 9) GET paper-versions/current
        r = client.get(
            f"{PREFIX}/exam-projects/{project_id}/paper-versions/current"
        )
        assert r.status_code == 200, f"get paper-version: {r.status_code} {r.text}"
        pv = r.json()
        pv_id = pv["id"]
        pv_status = pv.get("status")
        questions = pv.get("questions") or []

        # 10) needs-review + confirm(force=true 兜底)
        r = client.get(f"{PREFIX}/paper-versions/{pv_id}/needs-review")
        assert r.status_code == 200, f"needs-review: {r.status_code} {r.text}"
        needs = r.json() or []

        r = client.post(
            f"{PREFIX}/paper-versions/{pv_id}/confirm",
            json={"force_ignore_needs_review": False},
        )
        if r.status_code == 409:
            # 存在 needs_review：再走一次 force=true
            r2 = client.post(
                f"{PREFIX}/paper-versions/{pv_id}/confirm",
                json={"force_ignore_needs_review": True},
            )
            assert r2.status_code == 200, f"force confirm: {r2.status_code} {r2.text}"
        else:
            assert r.status_code == 200, f"confirm: {r.status_code} {r.text}"

        # 11) 取最终 finalized paper-version
        r = client.get(
            f"{PREFIX}/exam-projects/{project_id}/paper-versions/current"
        )
        assert r.status_code == 200
        pv_final = r.json()
        assert pv_final.get("status") == "finalized", (
            f"expected finalized, got {pv_final.get('status')}"
        )
        final_questions = pv_final.get("questions") or []

        # ------------------------------------------------------------------
        # 组装 7 段式输出
        # ------------------------------------------------------------------

        # blueprint 段
        blueprint_sec = {
            "blueprint_version_id": blueprint_version_id,
            "type_rules": TYPE_RULES,
            "chapter_weights": CHAPTER_WEIGHTS,
            "plan_items": plan_items,
        }

        # contract 段
        contract_sec = {
            "used_threshold": used_threshold,
            "slots": slots,
            "slot_revisions_applied": [],
            "conflicts_history": conflicts_history,
        }

        # paper 段：统一字段
        # 注意：paper_version 服务端 questions 输出中未包含 score 字段，
        # 需通过 plan_item_id → blueprint plan_items 的 score 映射回填。
        # needs_review 来自 paper_items.c.needs_review 列（q['needs_review'] 直取）。
        pi_score_map: dict[str, float] = {
            str(pi.get("id") or ""): float(pi.get("score") or 0)
            for pi in plan_items
        }

        def _paper_item(q: dict[str, Any]) -> dict[str, Any]:
            override = q.get("teacher_override") or {}
            pi_id = str(q.get("plan_item_id") or "")
            score_from_api = q.get("score")
            if score_from_api not in (None, 0, 0.0, "0", "0.0", ""):
                item_score = float(score_from_api)
            else:
                item_score = pi_score_map.get(pi_id, 0.0)
            # needs_review 优先取列值；否则看 quality_audit 的缓存
            nr_from_col = q.get("needs_review")
            if isinstance(nr_from_col, bool):
                item_nr = nr_from_col
            else:
                qa = q.get("quality_audit") or {}
                item_nr = bool((qa.get("needs_review")) if isinstance(qa, dict) else False)
            return {
                "item_index": int(q.get("item_index") or 0),
                "question_type": q.get("question_type") or "",
                "stem": q.get("stem") or "",
                "options": list(q.get("options") or []),
                "answer": q.get("answer") or "",
                "score": float(item_score),
                "teacher_override": override if isinstance(override, dict) else {},
                "needs_review": bool(item_nr),
            }

        paper_items = [_paper_item(q) for q in final_questions]
        paper_sec = {
            "paper_version_id": pv_id,
            "status": pv_final.get("status") or "",
            "items": paper_items,
        }

        # final_check
        total_score = round(sum(float(it["score"]) for it in paper_items), 2)
        item_count = len(paper_items)
        needs_review_count = sum(1 for it in paper_items if it["needs_review"])
        final_check_sec = {
            "total_score": total_score,
            "item_count": item_count,
            "needs_review_count": needs_review_count,
        }

        pipeline = {
            "framework": framework_payload,
            "knowledge_tree": tree_payload,
            "blueprint": blueprint_sec,
            "contract": contract_sec,
            "paper": paper_sec,
            "final_check": final_check_sec,
            "status": "completed",
        }
    finally:
        if client is not None:
            cleanup = client.__dict__.get("_pipeline_cleanup")
            if cleanup is not None:
                cleanup()
            client.close()
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass

    # 写文件（可选）——失败仅警告，不抛异常
    if write_path is not None:
        try:
            write_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = write_path.with_name(
                f"{write_path.name}.{os.getpid()}.tmp"
            )
            tmp_file.write_text(
                json.dumps(pipeline, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            for attempt in range(5):
                try:
                    tmp_file.replace(write_path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (2 ** attempt))
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] 写入 pipeline.json 失败：{exc}", file=sys.stderr)

    return pipeline


# ---------------------------------------------------------------------------
# 辅助 / CLI
# ---------------------------------------------------------------------------


def print_summary(pipeline: dict[str, Any]) -> None:
    fw_eps = len((pipeline.get("framework") or {}).get("exam_points") or [])
    tree = pipeline.get("knowledge_tree") or {}
    domains_all = []

    def _walk(doms):
        for d in doms:
            domains_all.append(d)
            subs = d.get("sub_domains") or []
            _walk(subs)

    _walk(tree.get("content_domains") or [])
    units_total = sum(
        len(d.get("assessment_units") or []) for d in domains_all
        if d.get("level") == 2
    )
    cards_total = sum(
        len(u.get("cards") or [])
        for d in domains_all if d.get("level") == 2
        for u in (d.get("assessment_units") or [])
    )
    plan_len = len((pipeline.get("blueprint") or {}).get("plan_items") or [])
    slots_len = len((pipeline.get("contract") or {}).get("slots") or [])
    items_len = len((pipeline.get("paper") or {}).get("items") or [])
    fc = pipeline.get("final_check") or {}

    print("=" * 56)
    print("pipeline_via_api 七段式快照摘要")
    print("=" * 56)
    print(f"  [framework]   exam_points count  : {fw_eps}")
    print(f"  [knowledge_tree]")
    print(f"                  content_domains  : {len(domains_all)} "
          f"(L1={sum(1 for d in domains_all if d.get('level')==1)}, "
          f"L2={sum(1 for d in domains_all if d.get('level')==2)})")
    print(f"                  assessment_units : {units_total}")
    print(f"                  knowledge_cards  : {cards_total}")
    print(f"  [blueprint]   plan_items count   : {plan_len}")
    print(f"  [contract]    slots count        : {slots_len}")
    print(f"  [paper]       items count        : {items_len}")
    print(f"  [final_check] total_score        : {fc.get('total_score')}")
    print(f"                item_count         : {fc.get('item_count')}")
    print(f"                needs_review_count : {fc.get('needs_review_count')}")
    print(f"  [status]                          : {pipeline.get('status')}")
    print("=" * 56)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="通过 TestClient 全 API 构建 pipeline.json 七段式快照"
    )
    parser.add_argument(
        "--no-write-file", action="store_true",
        help="只跑管线并打印摘要，不写 frontend/public/demo/pipeline.json",
    )
    parser.add_argument(
        "--stdout", action="store_true",
        help="完成后把 pipeline JSON 以美化格式打印到 stdout",
    )
    args = parser.parse_args(argv)

    write_path: Path | None = None
    if not args.no_write_file:
        write_path = OUTPUT_FILE

    # 在临时目录创建 SQLite DB（退出即清理）
    with tempfile.TemporaryDirectory(prefix="pipeline_api_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        engine, factory = build_seeded_factory(tmp_path)
        pipeline = run_pipeline_via_api(factory, engine, write_path=write_path)

    if args.stdout:
        print(json.dumps(pipeline, ensure_ascii=False, indent=2, default=str))

    print_summary(pipeline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
