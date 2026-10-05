"""课程内 AI 助手：意图解析 → 确定性路由 → 结果卡/提案卡 + SSE 流式事件。

链路：POST .../assistant/turns 写 user 消息并建 task_runs（assistant_turn）→
worker 调 ``execute_turn_task``：装配确定性上下文快照 → 段1（非流式 JSON）
解析意图 → 白名单校验与确定性路由 → 只读查询出结果卡 / 写操作出提案卡 /
纯问答走段2流式正文 → 助手消息落库；事件经 TurnEventSink（生产 Redis Stream）
推给 SSE 端点，前端边生成边显示。

红线（本模块的落地方式）：
- 写操作只出提案卡（status=proposed），执行由前端确认后调既有业务 API——
  本模块不碰任何业务写路径（消息表与 task_runs 除外）；
- LLM 调用只发生在 worker（本模块被 handler 调用），不进请求线程；
- 模型回传的 id 必须命中上下文白名单、参数过显式校验（带反馈重试一次）；
- prompt 不含出题比例/难度/去重的约束规则本身（教师的比例要求只作为蓝图建议指令或考核规则提案转交确定性机制，助手不换算、不承诺结果）；
- 消息读写全部带 course_id。

结构镜像 exam_rules_ai_service 的既定套路（上下文装配 / prompt 纯函数 /
确定性收口 / 带反馈纠错一次 / 幂等入队 / worker 入口）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.db.schema import (
    Course,
    assistant_messages,
    assistant_sessions,
    blueprint_versions,
    exam_points,
    exam_projects,
    framework_versions,
    knowledge_cards,
    knowledge_catalog_versions,
    paper_items,
    paper_versions,
    plan_items,
    task_runs,
)
from app.domain.course.category_profiles import available_categories, normalize_category
from app.domain.blueprint.models import ASSESSMENT_MODES
from app.domain.framework.exam_rules import canonical_question_type
from app.domain.generation.archetypes import ARCHETYPE_CONTRACTS
from app.domain.generation.question_formats import QUESTION_TEMPLATES
from app.domain.knowledge.relevance import StagingChunk
from app.domain.model_calls import ModelCallContext
from app.infrastructure.tasks.models import TERMINAL_TASK_STATUSES, create_task_run
from app.services import (
    exam_project_service,
    framework_service,
    material_service,
    parse_service,
)
# 工具注册表：枚举/提示词/卡片元数据的唯一来源（本模块只做路由与收口）
from app.services.assistant_tools import (
    PROPOSAL_TOOLS,
    RAG_TOOL,
    READ_TOOLS,
    proposal_card_meta,
    render_section,
    tool_names,
)
# 复用既有判定（模块级 import 优于复制第二份）
from app.services.ai_revise_service import llm_configured
# RAG 检索与语料索引（助手 v2 资料内容问答）
from app.services.content_index_service import (
    build_embedder,
    embedding_configured,
    ensure_embedded,
    load_content_chunks,
    load_semantic_scores,
    supports_semantic_pushdown,
)
from app.services.staging_retrieval_service import (
    RankedChunk,
    lexical_rank_for_question,
    retrieve_multi_for_question,
)

logger = logging.getLogger("services.assistant")

TASK_TYPE = "assistant_turn"
_INPUT_VERSION = "assistant_turn_v1"
_TASK_LEASE_SECONDS = 300  # 与 worker.py 的 _LEASE_SECONDS_BY_TYPE 保持一致
_CLIENT_TIMEOUT_SECONDS = 90.0
_CLIENT_MAX_ATTEMPTS = 2

# SSE 事件通道（worker 与 SSE 端点共享的唯一契约）
STREAM_KEY_PREFIX = "assistant:turn:"
STREAM_MAXLEN = 5000
STREAM_TTL_SECONDS = 600

_HISTORY_LIMIT = 10
_HISTORY_CHAR_LIMIT = 500
_MATERIAL_LIMIT = 50
_MESSAGE_MAX_CHARS = 4000

# 工具清单（只读 / 提案 / 问答）与卡片元数据全部来自注册表 assistant_tools，
# 提示词里的工具说明与参数口径也由它渲染——一处定义，不再各维护一份。
# 双保险：即使模型给出这些 tool，路由层也按确定性文案拒绝（prompt 另有指示）。
# 蓝图确认与发起生成已移入提案（教师卡上确认即教师确认），定稿/导出仍拒绝。
REFUSED_TOOLS = frozenset(
    {
        "delete_material",
        "remove_material",
        "finalize_paper",
        "export_paper",
        "read_material_content",  # 全文照抄/朗读仍拒绝；问答与总结走 RAG_TOOL（v2）
        "online_exam",
        "grading",
    }
)
REFUSED_REPLY = (
    "这类操作需要你亲自到对应页面完成：出题/改题去「试卷」页，试卷定稿与导出"
    "是里程碑操作不代劳，删除资料去「资料库」页，在线考试与阅卷不在本系统范围内。"
)

# 资料内容问答（RAG，助手 v2）：第 4 类路由——语料检索 + 段2 流式作答 + 来源引用卡
_RAG_TOP_K = 6
_RAG_HYBRID_MIN_SCORE = 0.15
_RAG_LEXICAL_MIN_SCORE = 0.2
_RAG_SNIPPET_CHARS = 160        # 来源卡摘要长度
_RAG_EXPAND_MIN_CHARS = 60      # 邻域扩展的正文下限（低于此为又一个标题）
_RAG_EXPAND_PER_HIT = 2         # 每个命中最多带几个邻域正文块
_RAG_EXPAND_MAX_TOTAL = 6       # 单轮邻域扩展总块数封顶（生成上下文可控）
_RAG_BLOCK_PROMPT_CHARS = 1500  # 单块进段2 prompt 的上限
_RAG_FALLBACK_REPLY = "已检索到相关资料，回答见下："

# 段1 的输出通道：function calling（而非让模型自由吐 JSON）。
# 线上实测（step-5-preview/step-3.7-flash）：模型会把 history 里"消息 content +
# 卡片动作 {kind,tool,status}"拼成一个扁平对象，或直接吐 `{}`，自由 JSON 的
# 外层契约时好时坏。改用工具后由 provider 保证函数调用信封在场，参数严格按
# 下发的 schema 填充；tool_choice 未下发（StepFun 档案不支持，见 model_profiles），
# 故仍保留 content-JSON 兜底与带反馈重试。枚举与白名单同源，模型不可能"发明"工具名。
# 形状遵循网关 tool 参数口径（只给 function 体，{"type":"function","function":…}
# 的外层包装由 _build_body 补），与 model_profiles 的既有约定一致。
_INTENT_TOOL: dict = {
    "name": "submit_reply",
    "description": (
        "向教师提交一条回复，并可附带一个要执行的动作。闲聊/解释状态时只给 reply。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "description": "给教师的中文回复，1~2 句"},
            "action": {
                "type": "object",
                "description": "要执行的动作；纯问答或闲聊时省略本字段",
                "properties": {
                    "tool": {
                        # 枚举与注册表同源（含只读/提案/问答），模型无从"发明"工具
                        "type": "string",
                        "enum": tool_names(),
                    },
                    "args": {"type": "object", "description": "动作参数；无参数时给空对象"},
                },
                "required": ["tool"],
            },
        },
        "required": ["reply"],
    },
}

# 提案卡状态机（单向迁移）：
#   proposed （待教师确认）→ executed | dismissed
#   auto     （待自动执行）→ executing
#   executing（已开始，结果未知）→ executed | dismissed
# executing 是关键的安全停态：自动执行中途刷新/切页时卡片停在 executing，
# **不再自动重跑**——建项目/建蓝图/发建议都不是幂等操作，重放会重复建实体，
# 由卡片上的「重试执行」交给教师决定是否再来一次。
_PROPOSAL_TRANSITIONS = {"executed", "dismissed", "executing"}
# 注：executing → executing 刻意**不**允许——认领必须是独占的（两个标签页同时
# 看到 auto 卡时，只有先落库那次能 claim 成功，另一次被拒后不再执行业务写）。
# 长步骤的执行进度不走状态机，由前端用本地进度态呈现。
_ALLOWED_STATUS_MOVES = {
    "proposed": {"executed", "dismissed"},
    "auto": {"executing"},
    "executing": {"executed", "dismissed"},
}


class AssistantError(Exception):
    """助手业务错误（消息可直接面向 API 层映射）。"""


# ---------------------------------------------------------------------------
# 消息事件通道（窄接口：生产 Redis Stream，测试/降级内存实现）
# ---------------------------------------------------------------------------


class TurnEventSink:
    """一轮对话的事件出口（delta/card/done/error）。"""

    def publish(self, event: str, data: dict) -> None:  # pragma: no cover - 接口
        raise NotImplementedError


class MemoryTurnEventSink(TurnEventSink):
    """进程内事件收集：单测与无 Redis 环境的降级实现。"""

    registry: dict[str, list[dict]] = {}

    def __init__(self, task_run_id: str) -> None:
        self.task_run_id = task_run_id
        self.registry.setdefault(task_run_id, [])

    def publish(self, event: str, data: dict) -> None:
        self.registry[self.task_run_id].append({"event": event, "data": data})


class RedisTurnEventSink(TurnEventSink):
    """生产实现：XADD 进 Stream，SSE 端点 XREAD 转发。

    发布失败只记日志不抛出——流式是尽力而为，消息落库才是权威；
    Redis 整体不可用时由 SSE 端点的 DB 兜底收尾。
    """

    def __init__(self, task_run_id: str, client) -> None:
        self.task_run_id = task_run_id
        self.client = client

    def publish(self, event: str, data: dict) -> None:
        key = f"{STREAM_KEY_PREFIX}{self.task_run_id}"
        try:
            self.client.xadd(
                key,
                {"event": event, "data": json.dumps(data, ensure_ascii=False)},
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )
            self.client.expire(key, STREAM_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("assistant 事件发布失败 task_run_id=%s: %s", self.task_run_id, exc)


def build_event_sink(task_run_id: str) -> TurnEventSink:
    """Redis 可用则用 Stream，否则回退内存（SSE 端点相应走 DB 兜底）。"""
    try:
        import redis

        from app.config import settings

        if not settings.redis_url:
            raise RuntimeError("redis_url not configured")
        client = redis.Redis.from_url(
            settings.redis_url, socket_timeout=2.0, socket_connect_timeout=2.0
        )
        client.ping()
        return RedisTurnEventSink(task_run_id, client)
    except Exception:  # noqa: BLE001
        return MemoryTurnEventSink(task_run_id)


# ---------------------------------------------------------------------------
# 上下文装配（确定性查询，全部带 course_id）
# ---------------------------------------------------------------------------


def _material_rows(session: Session, course_id: str) -> list[dict]:
    items = material_service.list_materials(session, course_id=course_id, include_deleted=False)
    rows: list[dict] = []
    for item in items[:_MATERIAL_LIMIT]:
        version = item.get("latest_version")
        parse_status = None
        if version is not None:
            parsed = parse_service.latest_parse_status(
                session, course_id=course_id, material_version_id=version["id"]
            )
            if parsed:
                parse_status = parsed.get("status")
        rows.append(
            {
                "id": str(item.get("id")),
                "name": str(item.get("logical_name") or item.get("name") or ""),
                "type": str(item.get("material_type") or ""),
                "status": str(item.get("status") or ""),
                "parse_status": parse_status,
            }
        )
    return rows


def _framework_summary(session: Session, course_id: str) -> dict | None:
    try:
        current = framework_service.get_current_framework(session, course_id=course_id)
    except framework_service.FrameworkNotFoundError:
        return None
    rules = current.get("exam_rules") or {}
    return {
        "version_no": current.get("version_no"),
        "status": current.get("status"),
        "exam_rules": {
            "exam_form": rules.get("exam_form"),
            "duration_minutes": rules.get("duration_minutes"),
            "total_score": rules.get("total_score"),
            "question_type_ratios": rules.get("question_type_ratios") or [],
            # 章节命题权重与考试侧重点：update_exam_rules 是整份替换语义，
            # 提案合并必须以快照现值兜底，未改动的字段不能被抹掉
            "chapter_weights": rules.get("chapter_weights") or [],
            "assessment_focus": rules.get("assessment_focus") or [],
            # 已设置的题型格式覆盖（教师/AI 助手此前提案落库的现值，供追问与对比）
            "type_formats": rules.get("type_formats") or {},
        },
    }


def _catalog_summary(session: Session, course_id: str) -> dict | None:
    row = session.execute(
        select(
            knowledge_catalog_versions.c.version_no,
            knowledge_catalog_versions.c.status,
        )
        .where(
            knowledge_catalog_versions.c.course_id == course_id,
            knowledge_catalog_versions.c.status == "published",
        )
        .order_by(knowledge_catalog_versions.c.version_no.desc())
        .limit(1)
    ).one_or_none()
    if row is None:
        return None
    return {"version_no": row[0], "status": row[1]}


def _blueprint_summary(
    session: Session, *, course_id: str, project: dict
) -> dict | None:
    """项目当前蓝图（active → 最新）+ 题位统计（题型/难度分布）。"""
    bv_id = project.get("active_blueprint_version_id")
    if not bv_id:
        latest = session.execute(
            select(blueprint_versions.c.id)
            .where(
                blueprint_versions.c.course_id == course_id,
                blueprint_versions.c.exam_project_id == project["id"],
            )
            .order_by(blueprint_versions.c.version_no.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest is None:
            return None
        bv_id = latest
    row = session.execute(
        select(
            blueprint_versions.c.version_no,
            blueprint_versions.c.status,
            blueprint_versions.c.confirmed_at,
        ).where(
            blueprint_versions.c.id == bv_id,
            blueprint_versions.c.course_id == course_id,
        )
    ).one_or_none()
    if row is None:
        return None
    by_type: dict[str, dict] = {}
    stats = session.execute(
        select(
            plan_items.c.question_type,
            plan_items.c.difficulty,
            func.count(),
            func.coalesce(func.sum(plan_items.c.score), 0),
        )
        .where(
            plan_items.c.course_id == course_id,
            plan_items.c.blueprint_version_id == bv_id,
        )
        .group_by(plan_items.c.question_type, plan_items.c.difficulty)
    ).all()
    for question_type, difficulty, count, score in stats:
        bucket = by_type.setdefault(question_type, {"count": 0, "score": 0.0, "difficulty": {}})
        bucket["count"] += int(count)
        bucket["score"] += float(score)
        bucket["difficulty"][difficulty] = bucket["difficulty"].get(difficulty, 0) + int(count)
    return {
        "blueprint_version_id": bv_id,
        "version_no": row[0],
        "status": row[1],
        "confirmed": row[2] is not None,
        "item_count": sum(v["count"] for v in by_type.values()),
        "by_type": by_type,
    }


def _contract_summary(session: Session, *, course_id: str, project_id: str) -> dict:
    snapshot = exam_project_service.get_current_contract_snapshot(
        session, course_id=course_id, project_id=project_id
    )
    if snapshot is None:
        return {"exists": False, "confirmed": False}
    slots = snapshot.get("slots") if isinstance(snapshot, dict) else None
    return {
        "exists": True,
        "confirmed": True,
        "slot_count": len(slots) if isinstance(slots, list) else None,
    }


def _paper_summary(session: Session, *, course_id: str, project: dict) -> dict:
    """项目当前试卷摘要：优先 active_paper_version_id（教师在试卷页看到的那份
    卷——历史版本被重新激活时与最新版本号不同，评审/引导都以它为准），
    无激活记录或指向失效时回退最新版本。"""
    stmt = select(
        paper_versions.c.id,
        paper_versions.c.version_no,
        paper_versions.c.status,
    ).where(
        paper_versions.c.course_id == course_id,
        paper_versions.c.exam_project_id == project["id"],
    )
    row = None
    active_id = project.get("active_paper_version_id")
    if active_id:
        row = session.execute(
            stmt.where(paper_versions.c.id == active_id)
        ).one_or_none()
    if row is None:
        row = session.execute(
            stmt.order_by(paper_versions.c.version_no.desc()).limit(1)
        ).one_or_none()
    if row is None:
        return {"exists": False}
    needs_review = session.execute(
        select(func.count())
        .where(
            paper_items.c.course_id == course_id,
            paper_items.c.paper_version_id == row[0],
            paper_items.c.needs_review.is_(True),
        )
    ).scalar_one()
    return {
        "exists": True,
        "paper_version_id": row[0],
        "version_no": row[1],
        "status": row[2],
        "needs_review_count": int(needs_review),
    }


def _project_rows(session: Session, course_id: str) -> list[dict]:
    rows = session.execute(
        select(
            exam_projects.c.id,
            exam_projects.c.name,
            exam_projects.c.status,
            exam_projects.c.active_blueprint_version_id,
            exam_projects.c.active_paper_version_id,
        ).where(exam_projects.c.course_id == course_id)
    ).all()
    return [
        {
            "id": r[0],
            "name": r[1],
            "status": r[2],
            "active_blueprint_version_id": r[3],
            "active_paper_version_id": r[4],
        }
        for r in rows
    ]


def _history_rows(session: Session, course_id: str, session_id: str | None = None) -> list[dict]:
    stmt = select(
        assistant_messages.c.role,
        assistant_messages.c.content,
        assistant_messages.c.action,
    ).where(assistant_messages.c.course_id == course_id)
    if session_id:
        # 会话是记忆边界：切换会话即切换上下文（v3 多会话）
        stmt = stmt.where(assistant_messages.c.session_id == session_id)
    rows = session.execute(
        stmt.order_by(assistant_messages.c.created_at.desc(), assistant_messages.c.id.desc())
        .limit(_HISTORY_LIMIT)
    ).all()
    history = []
    for role, content, action in reversed(rows):
        action = action or {}
        history.append(
            {
                "role": role,
                "content": (content or "")[:_HISTORY_CHAR_LIMIT],
                # 只给一句「上一步走到哪」的人话，不下发结构化 action
                "step": _step_note(action),
            }
        )
    return history


def _step_note(action: dict) -> str:
    """历史里「上一步做了什么」的中性文本（非结构化，不给模型模仿的形状）。

    原先下发 ``{kind, tool, status}``，模型会照抄这个形状去拼自己的输出——线上
    实测出现 ``{"content":…, "kind":"proposal", "status":"pending", "tool":…}``
    这种扁平对象（21:53 那次"继续"失败的第一根因）。改成一句人话，既保住「哪一步
    已走过」的信息（阶梯据此不重复发起），又不提供可模仿的输出模板。
    """
    if not action or action.get("kind") != "proposal":
        return ""
    tool = str(action.get("tool") or "")
    label = proposal_card_meta(tool).get("label") or tool
    status = str(action.get("status") or "")
    if status in {"executed", "auto", "executing"}:
        return f"已执行：{label}"
    if status == "dismissed":
        return f"教师取消：{label}"
    return f"待教师确认：{label}"


def load_turn_context(
    session: Session, *, course_id: str, session_id: str | None = None
) -> dict:
    """装配一轮对话的确定性上下文：业务快照 + 白名单 + 历史。

    session_id 给定时历史只取该会话（会话是记忆边界）；缺省取全课程
    （兼容 payload 不带会话的旧任务）。
    """
    course = session.execute(
        select(Course.name).where(Course.id == course_id)
    ).scalar_one_or_none()
    if course is None:
        raise AssistantError("课程不存在")

    materials = _material_rows(session, course_id)
    projects = _project_rows(session, course_id)
    project_details = []
    for project in projects:
        project_details.append(
            {
                "id": project["id"],
                "name": project["name"],
                "status": project["status"],
                "blueprint": _blueprint_summary(session, course_id=course_id, project=project),
                "contract": _contract_summary(
                    session, course_id=course_id, project_id=project["id"]
                ),
                "paper": _paper_summary(session, course_id=course_id, project=project),
            }
        )
    # 生成任务真实状态（复用项目摘要的归并）：status=generating 只是阶段标记，
    # 模型判断「是否真在生成 / 从未发起 / 已失败」全靠这个字段（generation_task_status）
    project_details = exam_project_service.with_generation_task_status(
        session, course_id, project_details
    )

    return {
        "course_id": course_id,
        "course_name": course,
        "materials": materials,
        "framework": _framework_summary(session, course_id),
        "catalog": _catalog_summary(session, course_id),
        "projects": project_details,
        "history": _history_rows(session, course_id, session_id),
        # 白名单：模型回传的 id 必须命中这些集合
        "allowed_ids": {
            "material_ids": [m["id"] for m in materials],
            "project_ids": [p["id"] for p in project_details],
        },
    }


# ---------------------------------------------------------------------------
# 段1：意图解析（非流式 JSON，温度 0）
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """你是高校课程工作台内的 AI 助手。教师在「{course_name}」课程空间里用自然语言向你提需求，你输出一句回复与一个可选的动作（action）。动作分两类：只读查询（后端直接执行并展示结果卡）、写操作提案（只生成提案卡，教师点「确认」才由既有接口执行——你永远不直接执行写操作）。

可用只读工具（action.tool 取其一）：
{read_tools}
只读工具 args 默认 {}（呈现全部）。教师**点名了某个试卷项目**时 course_overview 必须传 project_id（取自 payload.ids.project_ids），结果卡只呈现该项目；没点名就不传（usage_guide 与项目无关，恒 args={}）。

资料内容问答工具：
{rag_tools}

产品能力地图（回答「这个网站能做什么/怎么操作/流程是什么」时的依据；结构化步骤与跳转按钮由 usage_guide 引导卡呈现）：
- 页面模块：课程概览（全阶段状态）、资料库（上传/解析/索引资料，四分区展示）、命题框架（双大纲→考点与考核规则，确认后冻结）、知识目录（分类→事实→画像→知识卡）、试卷（试卷项目工作区）。
- 出卷主线：上传并解析资料 → 构建并冻结命题框架 → 生成知识目录 → 创建试卷项目并确认蓝图 → 确认合同（系统逐题位确定性分配）→ AI 生成 → 审核编辑（可发起整卷 AI 质量评审）→ 定稿导出学生卷/答卷/答题卡/答案细则四份产物。
- 助手边界：只读查询、资料内容问答与总结、写操作提案（教师点确认后由既有接口执行）可由我代劳——出卷主线的创建项目/修改考核规则/创建与确认蓝图/确认合同/发起生成与整卷质量评审均可提案代劳；出题改题、试卷定稿导出、删除资料需引导教师到对应页面亲自完成；在线考试与阅卷不在本系统范围内。

可用提案工具（action.args 只允许下述字段，id 必须取自 payload.ids 白名单）：
{proposal_tools}

出卷主线推进（教师要出卷、继续出卷、或直接给出出卷要求时，先看 snapshot.projects 状态选**下一步**的提案，一次一张卡；回复里说明整体计划。**除 confirmed 的确认类提案外，其余提案卡片由前端自动执行并回报执**；执行成功后系统会以内部的「继续」指令推进下一级——它**不是教师说的话**（不会出现在对话时间线里），收到这类追问就按本阶梯推进，**不要**在回复里要求教师手动输入「继续」。教师带着具体要求（难度/侧重/综合题形态）要新卷时，第一张卡仍只是 create_exam_project——回复里点明这些要求各自落到后续哪一步（各工具的参数说明里写明了要求该填进哪个字段），**不要**塞进本卡 args。发起 AI 生成后系统**不再追问**：出卷要跑几分钟，生成结束后由系统直接发一条完成通报，你不需要在这期间回复任何东西）：
1. 没有试卷项目，或教师点名要另出一份新卷（「生成一张新试卷」「再出一份」）→ create_exam_project（name 从教师原话取；课程可有多个试卷项目，既有项目保持冻结不受影响，新项目按本阶梯从头走）
2. 项目没有蓝图（blueprint 为 null）→ 先把教师的规则要求落成提案（偏理论/题型比例/章节权重 → update_exam_rules；综合题原型偏好 → 并进 create_blueprint 的 args），再 create_blueprint
3. 蓝图已有但未确认（blueprint.confirmed=false）→ 需要调整题位或难度分布 → enqueue_blueprint_suggest（指令带上教师原话的比例要求，建议由前端自动应用）；不需调整 → confirm_blueprint
4. 合同未确认（contract.confirmed=false）→ confirm_contract
5. 合同已确认且该项目**从未发起生成**（快照里 generation_task_status 为 null）→ start_generation（status=generating 只是合同确认时置上的**阶段标记**，不等于任务已在跑）
每一步只推进一级：历史里已经执行过的那一步不要重复发起（例如已发起过 enqueue_blueprint_suggest 就不再发起，蓝图未确认时直接 confirm_blueprint）。

接力停点——以下情况**不出提案卡**（action 置 null），用一两句话说明现状与教师接下来要做什么，然后停下等教师回复。**先按项目 status 判定，命中即停、不再往下看**：
1. 项目 status=review 或 exported → 出卷主线已完成。固定话术：先一句现状（试卷已生成、待审核），再引导「请到『试卷』页审核编辑，定稿与导出也在该页完成」；不列举导出格式、不把导出/发布摆成选项让教师点单，教师点名定稿/导出按下方拒绝清单回复。例外两类（本停点只拦**同一项目**的后续推进）：① 教师明确要求检查/评审试卷 → 照发 enqueue_paper_review（只读报告不改数据）；② 教师明确要另出一份新卷（「生成一张新试卷」「再出一份」）→ 照发 create_exam_project，新项目按阶梯从头走，不受本项目 review 状态牵连。
2. 项目 status=generating 且 generation_task_status 为 queued/running（确有生成任务在跑）→ 引导到试卷页看进度，不要重复发起生成。status=generating 但 generation_task_status 为 null（合同刚确认、生成尚未发起）**不是停点**——按阶梯第 5 步照发 start_generation；generation_task_status 为 failed/cancelled → 不要重复发起，引导教师到『试卷』页点「重新生成」。

拒绝并按标准话术回复（action 置 null，不要选任何工具）：
1. 出题、改题、新增题目 → 「题目内容的新增与修改请到『试卷』页操作（选中题目后可用 AI 修改/创建）。」
2. 试卷定稿、导出 → 「这是需要你亲自确认的里程碑操作，请到『试卷』页完成。」（蓝图确认与发起生成**不再**拒绝——用 confirm_blueprint / start_generation 提案）
3. 删除资料 → 「删除资料请到『资料库』页操作。」
4. 在线考试、阅卷、评分 → 「在线考试与阅卷不在本系统范围内——本系统止于导出纸质试卷产物。」
5. 操作其它课程 → 「我只能操作当前课程空间内的数据。」
6. 要求原样输出/朗读整份资料全文 → 「全文照抄请到『资料库』页查看原文；针对资料内容的提问与总结可选用 answer_material_content 工具。」

规则：
- 需要具体数据且命中上述工具时才给 action；闲聊、解释状态含义时 action 置 null，直接回答。询问网站能做什么、怎么操作、出卷流程、下一步做什么 → 给 usage_guide 引导卡，reply 结合 payload.snapshot（资料解析/框架/目录/项目的现有状态）推断教师当前所处步骤，给下一步建议（1~3 句），精确进度与跳转以卡片为准，不要逐条复述步骤。
- 回复用中文，面向教师，简洁自然；查询/提案类回复 1~2 句：先给针对教师所问对象的结论，再引出卡片。
- 结果卡已结构化呈现数据：回复不要逐条复述卡内容，教师没点名的项目/资料不要罗列；状态以卡片标签为准，回复里不要自行转述另一套状态说法。
- 你给的 id 必须来自 payload.ids 白名单；不确定教师指哪份资料/项目时，action 置 null 并在回复里追问。
- 参数能定下来就**直接发卡**（不建议、不商量）：出卷主线上的写操作卡片由前端自动执行，只有确认类提案卡才需要教师点「确认执行」——所以更不该先反问「是否现在发起」「要我帮你吗」，那只会把对话停在没有下一步的口头承诺上；仅当目标真的指不清（多份资料/项目没点名）才按上一条追问。
- 只依据 payload 中的真实数据回答，不臆造资料、项目、状态或数字。
- 难度要求的处理：比例/难度/去重的**结果**由系统确定性算法保证，你可以把教师的比例要求转成蓝图建议指令或考核规则提案，但不自己做换算、不承诺达标结果；题型的出题格式要求可用 update_question_type_format 提案修改。

输出方式：调用 submit_reply 工具提交，参数即下面的对象（reply 必填；不需要动作时省略 action 或置空）。
{"reply": "给教师的回复文本", "action": {"tool": "course_overview", "args": {"section": "materials"}}}
不需要动作时：{"reply": "..."}"""


def build_intent_prompt(
    context: dict, message: str, *, previous_error: str = ""
) -> tuple[str, dict]:
    """组装段1的 (system_prompt, payload)。纯函数，便于断言真实数据进了 prompt。"""
    payload: dict = {
        "course": {"id": context["course_id"], "name": context["course_name"]},
        # 课程类别清单（create_course 的 category 取值；只给 key/label，控制提示词体积）
        "course_categories": [
            {"key": item["key"], "label": item["label"]}
            for item in available_categories()
        ],
        "user_message": message,
        "history": context.get("history") or [],
        "snapshot": {
            "materials": context.get("materials") or [],
            "framework": context.get("framework"),
            "catalog": context.get("catalog"),
            "projects": context.get("projects") or [],
        },
        "ids": context.get("allowed_ids") or {},
    }
    if previous_error:
        payload["previous_validation_error"] = previous_error
    # replace 而非 format：系统提示里含 JSON 示例与 args={...}，花括号会被
    # format 当占位符解析而炸（KeyError）。工具清单与参数口径由注册表渲染。
    system_prompt = (
        _SYSTEM_PROMPT.replace("{course_name}", context["course_name"])
        .replace("{read_tools}", render_section("read"))
        .replace("{rag_tools}", render_section("rag"))
        .replace("{proposal_tools}", render_section("proposal"))
    )
    return system_prompt, payload


def _normalize_intent(raw) -> dict:
    """把模型输出收敛为 {reply, action|null}；结构非法抛 AssistantError（上层重试一次）。"""
    if not isinstance(raw, dict):
        raise AssistantError("模型未返回 JSON 对象")
    # content 与 reply 同义：模型若漏调工具、退回 content 通道，常按 history 里
    # 消息的形状用 content 当正文（线上 21:53 实测），此处一并接受。
    reply = str(raw.get("reply") or raw.get("content") or "").strip()
    action = raw.get("action")
    if action in (None, "", {}) and "tool" in raw:
        # 模型偶发漏掉 {reply, action} 外层包装、直接吐 action 形状
        # （线上 21:26/21:27 连续实测），形状无歧义——确定性归一后
        # 走下面同一套 tool/args 校验，避免白白烧一次带反馈重试。
        action = {"tool": raw.get("tool"), "args": raw.get("args")}
    if action in (None, "", {}):
        # 既无正文也无动作 ≠合法纯问答：模型 reasoning 正确但 content 通道
        # 输出退化 JSON（`{}`/破损串）时会落成这里——放行会静默降级成
        # 「口头承诺却不出卡、无下文」。抛错走上层带反馈重试一次。
        if not reply:
            raise AssistantError("输出既无 reply 也无 action（空响应或 JSON 破损）")
        return {"reply": reply, "action": None}
    if not isinstance(action, dict):
        raise AssistantError("action 必须是对象或 null")
    tool = str(action.get("tool") or "").strip()
    if not tool:
        raise AssistantError("action.tool 缺失")
    args = action.get("args")
    if args in (None, ""):
        args = {}
    if not isinstance(args, dict):
        raise AssistantError("action.args 必须是对象")
    return {"reply": reply, "action": {"tool": tool, "args": args}}


def parse_intent(
    client,
    context: dict,
    message: str,
    *,
    call_context: ModelCallContext,
    previous_error: str = "",
    on_think=None,
) -> dict:
    system_prompt, payload = build_intent_prompt(context, message, previous_error=previous_error)
    # stream=True：意图解析也走 SSE——推理增量在等待期实时回调 on_think
    # （前端思考块先动起来），function calling 的 arguments 增量由网关
    # 按 index 拼装成与非流式同构的 message 后，交 _normalize_intent 统一收口。
    raw = client.request_json(
        system_prompt=system_prompt,
        payload=payload,
        temperature=0.0,
        tool=_INTENT_TOOL,
        call_context=call_context,
        on_think=on_think,
        stream=True,
    )
    return _normalize_intent(raw)


# ---------------------------------------------------------------------------
# 只读工具执行（确定性查询）
# ---------------------------------------------------------------------------


# 结果卡的视图标记：一个查询工具按 section 只呈现教师问的那一部分
_OVERVIEW_SECTIONS = (
    "overview",
    "materials",
    "framework",
    "blueprint",
    "contract",
    "paper",
    "projects",
)


def _read_course_overview(context: dict, *, projects: list[dict], section: str) -> dict:
    """课程状态查询的唯一载荷：同一份快照 + 本次要呈现的 section。

    原先是六七个各查一片的工具，现在一次装配全量返回、由 section 决定卡片视图，
    避免模型在「框架状态/蓝图状态/合同状态…」之间挑错工具。
    """
    return {
        "section": section,
        "course_name": context["course_name"],
        "materials": context["materials"],
        "framework": context["framework"],
        "catalog": context["catalog"],
        "projects": projects,
    }


def _target_projects(context: dict, args: dict | None) -> list[dict]:
    """读工具的可选项目定位：未传 → 全部；传了 → 必须命中白名单。

    非法 id 抛 AssistantError，由上层带反馈重试一次（与提案工具同一套白名单语义）。
    """
    project_id = str((args or {}).get("project_id") or "").strip()
    if not project_id:
        return context["projects"]
    allowed = context.get("allowed_ids") or {}
    if project_id not in (allowed.get("project_ids") or []):
        raise AssistantError("project_id 不在当前课程项目白名单内")
    return [p for p in context["projects"] if p["id"] == project_id]


# 出卷主线步骤与页面导航（usage_guide 引导卡的确定性内容）
_GUIDE_STEPS = (
    {
        "key": "materials",
        "label": "上传并解析资料",
        "nav": "materials",
        "hint": "资料库上传大纲/讲义，解析后建立语料索引（问答与生成的依据）",
    },
    {
        "key": "framework",
        "label": "构建命题框架",
        "nav": "framework",
        "hint": "双大纲生成考点与考核规则，教师确认后冻结",
    },
    {
        "key": "knowledge",
        "label": "生成知识目录",
        "nav": "knowledge",
        "hint": "分类→事实→画像→知识卡，发布后供合同分配引用",
    },
    {
        "key": "blueprint",
        "label": "创建项目并确认蓝图",
        "nav": "paper",
        "hint": "可提案创建项目与蓝图，AI 给蓝图调整建议，教师逐条确认题位",
    },
    {
        "key": "contract_generate",
        "label": "确认合同并生成试卷",
        "nav": "paper",
        "hint": "确定性分配逐题位原子并冻结合同，随后 AI 分批生成",
    },
    {
        "key": "review_export",
        "label": "审核定稿并导出",
        "nav": "paper",
        "hint": "逐题审核编辑 → 定稿 → 导出学生卷/答卷/答题卡/答案细则",
    },
)
_GUIDE_PAGES = (
    {"label": "课程概览", "nav": "", "desc": "全阶段状态总览"},
    {"label": "资料库", "nav": "materials", "desc": "上传/解析/索引资料"},
    {"label": "命题框架", "nav": "framework", "desc": "考点与考核规则（冻结）"},
    {"label": "知识目录", "nav": "knowledge", "desc": "知识卡与证据链"},
    {"label": "试卷", "nav": "paper", "desc": "蓝图→合同→生成→审核→导出"},
)


def _usage_guide_payload(context: dict) -> dict:
    """使用引导卡载荷：出卷主线步骤（done/current/todo）+ 页面导航。

    完成信号取自本轮 snapshot 的既有字段；状态按前缀推导——第一个未完成
    步骤为「进行中」，其后全部「未开始」，即使个别信号非线性，卡片呈现
    的仍是一条从头开始的流程。全部完成时 current_step 为 None。
    """
    projects = context.get("projects") or []
    done_flags = [
        any(m.get("parse_status") == "ready" for m in context.get("materials") or []),
        context.get("framework") is not None,
        context.get("catalog") is not None,
        any((p.get("blueprint") or {}).get("confirmed") for p in projects),
        any((p.get("paper") or {}).get("exists") for p in projects),
        any(
            p.get("status") == "exported"
            or (p.get("paper") or {}).get("status") == "finalized"
            for p in projects
        ),
    ]
    current = next((i for i, done in enumerate(done_flags) if not done), len(done_flags))
    steps = [
        {**step, "status": "done" if i < current else ("current" if i == current else "todo")}
        for i, step in enumerate(_GUIDE_STEPS)
    ]
    return {
        "steps": steps,
        "pages": list(_GUIDE_PAGES),
        "current_step": _GUIDE_STEPS[current]["key"] if current < len(_GUIDE_STEPS) else None,
    }


def execute_read_tool(
    session: Session, *, context: dict, tool: str, args: dict | None = None
) -> dict:
    if tool == "course_overview":
        section = str((args or {}).get("section") or "").strip() or "overview"
        if section not in _OVERVIEW_SECTIONS:
            raise AssistantError(
                f"未知 section {section}（可用：{', '.join(_OVERVIEW_SECTIONS)}）"
            )
        return _read_course_overview(
            context, projects=_target_projects(context, args), section=section
        )
    if tool == "usage_guide":
        # 不触库：能力地图 + 步骤状态全由本轮 snapshot 推导
        return _usage_guide_payload(context)
    raise AssistantError(f"未知只读工具 {tool}")


# ---------------------------------------------------------------------------
# 提案载荷（执行契约：前端确认后按 tool 调既有业务 API）
# ---------------------------------------------------------------------------


def _require_str(args: dict, key: str, *, max_len: int, allow_empty: bool = False) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        if allow_empty:
            return ""
        raise AssistantError(f"{key} 不能为空")
    if len(text) > max_len:
        raise AssistantError(f"{key} 超长（≤{max_len} 字）")
    return text


def _validate_slug(slug: str | None) -> str | None:
    if slug is None:
        return None
    import re

    # 与 CourseUpdate.slug 的 pattern 保持一致
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise AssistantError("slug 只能包含小写字母、数字与连字符")
    return slug


def _target_project(allowed: dict, context: dict, args: dict) -> dict:
    """提案的项目定位：给定必须命中白名单；缺省且仅一个项目时自动取之。"""
    project_id = str(args.get("project_id") or "").strip()
    ids = allowed["project_ids"]
    if not project_id:
        if not ids:
            raise AssistantError("当前课程还没有试卷项目，请先用 create_exam_project 创建")
        if len(ids) > 1:
            raise AssistantError("需要指定 project_id（当前课程有多个试卷项目）")
        project_id = ids[0]
    elif project_id not in ids:
        raise AssistantError("project_id 不在当前课程项目白名单内")
    project = next((p for p in context["projects"] if p["id"] == project_id), None)
    if project is None:
        raise AssistantError("项目不存在")
    return project


def _validate_overrides(args: dict, key: str, *, entries) -> list | None:
    """比例/权重类覆盖列表的统一校验：非空列表、逐项过 entries 校验器。

    entries(item) 返回归一化后的条目；抛 AssistantError 即带反馈重试。
    返回 None 表示教师没给这个字段（保持现值）。
    """
    raw = args.get(key)
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise AssistantError(f"{key} 需要非空列表")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise AssistantError(f"{key} 每项需为对象")
        out.append(entries(item))
    return out


def build_proposal_payload(tool: str, args: dict, *, context: dict) -> dict:
    """白名单硬校验并组装提案执行契约；非法参数抛 AssistantError（上层重试一次）。"""
    allowed = context["allowed_ids"]

    if tool == "create_course":
        name = _require_str(args, "name", max_len=200)
        if not name:
            raise AssistantError("新建课程缺少 name")
        slug = _validate_slug(_require_str(args, "slug", max_len=120, allow_empty=True) or None)
        description = _require_str(args, "description", max_len=10_000, allow_empty=True)
        body = {"name": name}
        if slug:
            body["slug"] = slug
        if description:
            body["description"] = description
        category = str(args.get("category") or "").strip()
        if category:
            # 未知类别走带反馈重试（而非静默回退），让模型改用 payload.course_categories 里的 key
            if normalize_category(category) != category:
                raise AssistantError(f"未知课程类别 {category}（可用 key 见 payload.course_categories）")
            body["category"] = category
        return {"body": body}

    if tool == "update_course":
        # 只允许修改当前课程：目标 id 不取自模型，直接钉死上下文课程
        name = _require_str(args, "name", max_len=200)
        slug = _validate_slug(_require_str(args, "slug", max_len=120, allow_empty=True) or None)
        description = _require_str(args, "description", max_len=10_000, allow_empty=True)
        body = {k: v for k, v in (("name", name), ("slug", slug), ("description", description)) if v}
        if not body:
            raise AssistantError("修改课程至少需要一个字段（name/slug/description）")
        return {"course_id": context["course_id"], "body": body}

    if tool == "start_parse":
        material_id = str(args.get("material_id") or "").strip()
        if material_id not in allowed["material_ids"]:
            raise AssistantError("material_id 不在当前课程资料白名单内")
        material = next(
            (m for m in context["materials"] if m["id"] == material_id), None
        )
        # material_name 供提案卡展示（与白名单同源快照，前端无需二次查询）
        return {
            "material_id": material_id,
            "material_name": material["name"] if material else "",
            "body": {},
        }

    if tool == "create_exam_project":
        name = _require_str(args, "name", max_len=200)
        if not name:
            raise AssistantError("创建试卷项目缺少 name")
        return {"body": {"name": name}}

    if tool == "update_exam_rules":
        # PATCH /rules 是整份替换语义：教师只给要改的字段，其余按本轮快照
        # 现值合并回填，未涉及的题型比例/章节权重不能被默认值抹掉。
        framework = context.get("framework")
        if framework is None:
            raise AssistantError("尚未构建命题框架，无法修改考核规则")
        current = framework.get("exam_rules") or {}

        def _ratio_entry(item: dict) -> dict:
            canonical = canonical_question_type(item.get("question_type"))
            if not canonical:
                raise AssistantError(f"未知题型：{item.get('question_type')!r}")
            value = item.get("ratio")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise AssistantError("题型比例 ratio 必须为正数")
            return {"question_type": canonical, "ratio": float(value)}

        def _weight_entry(item: dict) -> dict:
            anchor = str(item.get("anchor_key") or "").strip()
            if not anchor:
                raise AssistantError("章节权重缺少 anchor_key")
            value = item.get("weight")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise AssistantError("章节权重 weight 必须为正数")
            return {"anchor_key": anchor, "weight": float(value)}

        def _focus_entry(item: dict) -> dict:
            mode = str(item.get("assessment_mode") or "").strip()
            if mode not in ASSESSMENT_MODES:
                raise AssistantError(
                    f"未知考查方式 {mode}（可用：{', '.join(ASSESSMENT_MODES)}）"
                )
            value = item.get("weight")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise AssistantError("考试侧重点 weight 必须为正数")
            return {"assessment_mode": mode, "weight": float(value)}

        ratios = _validate_overrides(args, "question_type_ratios", entries=_ratio_entry)
        chapters = _validate_overrides(args, "chapter_weights", entries=_weight_entry)
        focus = _validate_overrides(args, "assessment_focus", entries=_focus_entry)

        def _difficulty_arg(raw: object) -> dict | None:
            """难度比例 {low, medium, high}：缺省键=0，全部缺省/非法即拒。"""
            if raw is None:
                return None
            if not isinstance(raw, dict):
                raise AssistantError("difficulty_distribution 需要 {low, medium, high} 对象")
            parsed: dict[str, float] = {}
            for key in ("low", "medium", "high"):
                value = raw.get(key)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                    raise AssistantError(f"难度比例 {key} 必须是非负数字")
                parsed[key] = float(value)
            if not parsed:
                raise AssistantError("difficulty_distribution 至少给一个非负档位")
            return parsed

        difficulty = _difficulty_arg(args.get("difficulty_distribution"))
        if ratios is None and chapters is None and focus is None and difficulty is None:
            raise AssistantError(
                "至少给一个要修改的字段"
                "（question_type_ratios/chapter_weights/assessment_focus/difficulty_distribution）"
            )
        body = {
            "exam_form": current.get("exam_form") or "",
            "duration_minutes": current.get("duration_minutes"),
            "total_score": current.get("total_score"),
            "question_type_ratios": (
                ratios if ratios is not None else current.get("question_type_ratios") or []
            ),
            "chapter_weights": (
                chapters if chapters is not None else current.get("chapter_weights") or []
            ),
            "assessment_focus": (
                focus if focus is not None else current.get("assessment_focus") or []
            ),
            # 未涉及难度时置 None：端点按「未提供即保留」回填现值，不抹掉已声明比例
            "difficulty_distribution": difficulty if difficulty is not None else None,
        }
        # before 供提案卡做「现值 → 新值」对比展示
        return {
            "body": body,
            "before": {
                "question_type_ratios": current.get("question_type_ratios") or [],
                "chapter_weights": current.get("chapter_weights") or [],
                "assessment_focus": current.get("assessment_focus") or [],
            },
        }

    if tool == "create_blueprint":
        project = _target_project(allowed, context, args)
        if context.get("framework") is None:
            raise AssistantError("尚未构建命题框架：创建蓝图前请先到命题框架页确认冻结")
        if context.get("catalog") is None:
            raise AssistantError("知识目录尚未发布：创建蓝图前请先发布知识目录")
        archetypes = args.get("comprehensive_archetypes")
        if archetypes is not None:
            if not isinstance(archetypes, list) or not archetypes:
                raise AssistantError(
                    "comprehensive_archetypes 需要非空列表（按序指派，重复=该原型的数量）"
                )
            pool: list[str] = []
            for raw in archetypes:
                key = str(raw or "").strip()
                if key not in ARCHETYPE_CONTRACTS:
                    raise AssistantError(
                        f"未知综合题原型 {key}（可用：{', '.join(ARCHETYPE_CONTRACTS)}）"
                    )
                # 不去重：重复项=数量（教师要「两道代码题」= 该原型写两次），
                # 合同按序硬指派，去重会把数量要求抹成一道
                pool.append(key)
            # 题型构成归考核规则：比例已声明却不含综合题时蓝图根本不会出
            # 综合题，先引导改比例而不是创建后再报错
            ratios = (context.get("framework") or {}).get("exam_rules", {}).get(
                "question_type_ratios"
            ) or []
            if ratios and not any(
                canonical_question_type(r.get("question_type")) == "comprehensive"
                for r in ratios
                if isinstance(r, dict)
            ):
                raise AssistantError(
                    "考核规则的题型比例未包含综合题，蓝图不会出综合题——"
                    "请先用 update_exam_rules 在 question_type_ratios 中加入综合题比例"
                )
            archetypes = pool
        # body 只带原型池（蓝图主体由前端确认时按考核规则+知识目录组装，
        # 与试卷页创建蓝图同一条组装路径，无第二套写入逻辑）
        # basis：生成依据（考核规则现值）——提案卡展示「蓝图将按什么确定性
        # 生成」，教师确认前看到规则全貌，而不是只有一行项目名。
        rules = (context.get("framework") or {}).get("exam_rules") or {}
        return {
            "project_id": project["id"],
            "project_name": project["name"],
            "body": {"comprehensive_archetypes": archetypes} if archetypes else {},
            "basis": {
                "question_type_ratios": rules.get("question_type_ratios") or [],
                "assessment_focus": rules.get("assessment_focus") or [],
                "chapter_weights": rules.get("chapter_weights") or [],
            },
        }

    if tool == "confirm_blueprint":
        project = _target_project(allowed, context, args)
        blueprint = project.get("blueprint")
        if blueprint is None:
            raise AssistantError("该项目还没有蓝图，先用 create_blueprint 创建")
        if blueprint.get("confirmed"):
            raise AssistantError("蓝图已确认，无需再次确认（要调整可发起蓝图建议或新建蓝图版本）")
        return {"project_id": project["id"], "project_name": project["name"], "body": {}}

    if tool == "enqueue_blueprint_suggest":
        project_id = str(args.get("project_id") or "").strip()
        if project_id not in allowed["project_ids"]:
            raise AssistantError("project_id 不在当前课程项目白名单内")
        instruction = _require_str(args, "instruction", max_len=500, allow_empty=True) or ""
        project = next((p for p in context["projects"] if p["id"] == project_id), None)
        return {
            "project_id": project_id,
            "project_name": project["name"] if project else "",
            "body": {"instruction": instruction},
        }

    if tool == "confirm_contract":
        project = _target_project(allowed, context, args)
        if project["contract"].get("confirmed"):
            raise AssistantError("该项目合同已确认冻结，不能重新分配（可新建试卷项目或蓝图版本）")
        return {
            "project_id": project["id"],
            "project_name": project["name"],
            "body": {},
        }

    if tool == "start_generation":
        project = _target_project(allowed, context, args)
        if not (project.get("contract") or {}).get("confirmed"):
            raise AssistantError("合同尚未确认：先用 confirm_contract 确认合同再发起生成")
        return {"project_id": project["id"], "project_name": project["name"], "body": {}}

    if tool == "enqueue_paper_review":
        project = _target_project(allowed, context, args)
        paper = project.get("paper") or {}
        if not paper.get("exists"):
            raise AssistantError("该项目还没有试卷：先确认合同并发起生成，试卷生成后才能发起评审")
        instruction = _require_str(args, "instruction", max_len=500, allow_empty=True) or ""
        pv_id = str(paper.get("paper_version_id") or "").strip()
        if not pv_id:
            raise AssistantError("该项目没有可评审的试卷版本")
        # paper_version_id 已按 active_paper_version_id 解析（与试卷页所见同源）
        return {
            "project_id": project["id"],
            "project_name": project["name"],
            "paper_version_id": pv_id,
            "paper_version_no": paper.get("version_no"),
            "body": {"instruction": instruction},
        }

    if tool == "update_question_type_format":
        raw_key = str(args.get("question_type") or "").strip()
        canonical = canonical_question_type(args.get("question_type"))
        if canonical not in QUESTION_TEMPLATES:
            # 综合题传原型 key：任务卡覆盖与标准题型同一张表，键=原型名；
            # 裸 comprehensive 不是任务卡，仍拒绝
            canonical = raw_key if raw_key in ARCHETYPE_CONTRACTS else None
        if canonical is None:
            raise AssistantError(
                f"未知题型：{args.get('question_type')!r}（可用："
                f"{', '.join(sorted(QUESTION_TEMPLATES))}；综合题原型："
                f"{', '.join(ARCHETYPE_CONTRACTS)}）"
            )
        template = _require_str(args, "template", max_len=2000, allow_empty=True)
        if template is None:
            raise AssistantError("template 缺失（设置格式传完整要求，恢复默认传空串）")
        exam_rules = (context.get("framework") or {}).get("exam_rules") or {}
        current = (exam_rules.get("type_formats") or {}).get(canonical, "")
        # body 即执行体：前端确认后原样 PATCH /rules/type-formats
        return {
            "body": {"question_type": canonical, "template": template},
            # 现值供卡片对比展示（从未设置过 = 空串 = 系统/类别默认）
            "current": current,
        }

    raise AssistantError(f"未知提案工具 {tool}")


# ---------------------------------------------------------------------------
# 路由（确定性收口）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 资料内容问答（RAG）执行
# ---------------------------------------------------------------------------


# 查询变体的确定性削词表：只削问句框架与句首泛化动词，原问题恒为首变体
_RAG_FRAME_WORDS = (
    "讲了什么", "说了什么", "写了什么", "做了什么", "提了什么", "讲什么",
    "什么内容", "有哪些内容", "有什么内容", "有哪些", "有什么", "是多少",
    "是什么", "怎么样", "为什么", "是不是", "如何", "哪些", "什么", "吗", "呢",
    "请问", "我想知道", "告诉我", "一下",
)
_RAG_LEAD_VERBS = (
    "总结", "概括", "简述", "概述", "描述", "介绍", "说明", "解释",
    "阐述", "梳理", "分析", "列举", "列出", "翻译", "讲解", "谈谈", "讲讲", "请",
)
_RAG_STRIP_PUNCT = "？?。！!，,、;；:：~～\"'（）()《》[]【】 \t"


def _rag_query_variants(question: str) -> list[str]:
    """确定性查询改写：[原问题, 主题核心串]（核心不同且 ≥2 字才追加）。

    不引入模型调用：削掉疑问框架（讲了什么/是什么/…）与句首泛化动词
    （总结/介绍/请/…），让「教学大纲」这类主题词单独成一条向量查询——
    原问题负责语义完整，核心串负责主题词的字面/语义直击。
    """

    core = question
    for char in _RAG_STRIP_PUNCT:
        core = core.replace(char, "")
    for frame in _RAG_FRAME_WORDS:
        core = core.replace(frame, "")
    while True:
        stripped = core.lstrip()
        for verb in _RAG_LEAD_VERBS:
            if stripped.startswith(verb):
                stripped = stripped[len(verb):]
                break
        else:
            break
        core = stripped
    core = core.strip("的了过是")
    variants = [question]
    if core and core != question and len(core) >= 2:
        variants.append(core)
    return variants


def _rank_rag_chunks(
    question: str,
    chunks: list[StagingChunk],
    *,
    semantic_scores: list[dict[str, float]] | None = None,
) -> tuple[str, list[RankedChunk]]:
    """多查询混合检索（原问题+主题核心双变体，合并期同文折叠）；嵌入不可用
    或混合无命中 → 纯词面（确定性降级，嵌入故障不断轮）。

    semantic_scores：SQL 下推预计算的逐变体语义分（与 _rag_query_variants 对齐，
    向量不出库）。给定且覆盖全部块 → 直接用预计算分打分；覆盖不全或缺省 → 回落
    「装载向量 + Python cosine」旧路径（未装载向量时即纯词面）。
    """

    if embedding_configured():
        try:
            variants = _rag_query_variants(question)
            scores_cover = (
                semantic_scores is not None
                and len(semantic_scores) == len(variants)
                and all(
                    chunk.id in scores for scores in semantic_scores for chunk in chunks
                )
            )
            if scores_cover:
                ranked = retrieve_multi_for_question(
                    variants,
                    chunks,
                    None,
                    top_k=_RAG_TOP_K,
                    minimum_score=_RAG_HYBRID_MIN_SCORE,
                    semantic_scores=semantic_scores,
                )
                if ranked:
                    return "hybrid", ranked
            elif all(chunk.embedding is not None for chunk in chunks):
                ranked = retrieve_multi_for_question(
                    variants,
                    chunks,
                    build_embedder(),
                    top_k=_RAG_TOP_K,
                    minimum_score=_RAG_HYBRID_MIN_SCORE,
                )
                if ranked:
                    return "hybrid", ranked
        except Exception as exc:  # noqa: BLE001 — 嵌入故障降级词面，不上抛
            logger.warning("RAG 混合检索失败，降级词面: %s", exc)
    return "lexical", lexical_rank_for_question(
        question, chunks, top_k=_RAG_TOP_K, minimum_score=_RAG_LEXICAL_MIN_SCORE
    )


def _expand_rag_neighborhood(
    ranked: list[RankedChunk], chunks: list[StagingChunk]
) -> list[RankedChunk]:
    """命中块邻域扩展：排序不变，每个命中附带同资料同/邻页的正文块。

    「总结/讲了什么」类查询与正文天然低相似（实测正文表 rank #291），而命中
    标题的同/邻页恰好是被短块洪泛淹没的正文表——确定性地把它们带回生成上下文
    与来源卡：
    - 同资料、页码差 ≤1、正文（len ≥ _RAG_EXPAND_MIN_CHARS，排除又一个标题）；
    - 同页优先于邻页，同页内正文越长信息量越大；同块只带一次（多命中共享
      邻域去重，命中本身永不重复带入）；
    - **轮转分配**：每轮每个命中取一个最优未选邻域（预算 _RAG_EXPAND_MAX_TOTAL
      内保证每个命中至少贡献一块，前位命中不吃光预算），轮数封顶
      _RAG_EXPAND_PER_HIT；
    - 扩展块分数继承锚点（分数只用于召回排序，扩展按附着顺序进入段2与来源卡）。
    """

    if not ranked:
        return ranked
    picked = {item.chunk.id for item in ranked}
    candidate_lists: list[tuple[RankedChunk, list[StagingChunk]]] = []
    for anchor in ranked:
        anchor_page = anchor.chunk.locator.get("page_index")
        if anchor_page is None:
            continue
        material_id = anchor.chunk.locator.get("material_id")
        candidates = [
            chunk
            for chunk in chunks
            if chunk.id not in picked
            and chunk.locator.get("material_id") == material_id
            and chunk.locator.get("page_index") is not None
            and abs(chunk.locator["page_index"] - anchor_page) <= 1
            and len(chunk.content) >= _RAG_EXPAND_MIN_CHARS
        ]
        candidates.sort(
            key=lambda chunk: (
                abs(chunk.locator["page_index"] - anchor_page),
                -len(chunk.content),
            )
        )
        candidate_lists.append((anchor, candidates))

    expanded: list[RankedChunk] = []
    for _round in range(_RAG_EXPAND_PER_HIT):
        if len(expanded) >= _RAG_EXPAND_MAX_TOTAL:
            break
        for anchor, candidates in candidate_lists:
            if len(expanded) >= _RAG_EXPAND_MAX_TOTAL:
                break
            target = next((c for c in candidates if c.id not in picked), None)
            if target is None:
                continue
            picked.add(target.id)
            expanded.append(
                RankedChunk(
                    chunk=target,
                    score=anchor.score,
                    lexical_score=anchor.lexical_score,
                    semantic_score=anchor.semantic_score,
                )
            )
    return ranked + expanded


def execute_rag(
    session: Session, *, context: dict, args: dict, question: str
) -> tuple[dict, dict]:
    """资料内容问答：白名单/解析状态校验 → 自愈索引 → 检索 → (来源卡 payload, 检索态)。

    嵌入调用只发生在 worker（本模块只被 handler 调用），不进请求线程；非法参数/
    未解析抛 AssistantError（上层带反馈重试一次）。
    """

    if not question.strip():
        raise AssistantError("问题不能为空")
    material_id = str(args.get("material_id") or "").strip() or None
    material_ids = None
    target_row = None
    if material_id is not None:
        if material_id not in (context["allowed_ids"].get("material_ids") or []):
            raise AssistantError("material_id 不在当前课程资料白名单内")
        target_row = next((m for m in context["materials"] if m["id"] == material_id), None)
        if target_row is None:
            raise AssistantError("material_id 不在当前课程资料白名单内")
        if target_row.get("parse_status") != "ready":
            raise AssistantError(
                f"资料「{target_row.get('name')}」尚未解析完成，先到资料库页完成解析"
            )
        material_ids = [material_id]
    elif not any(m.get("parse_status") == "ready" for m in context["materials"]):
        raise AssistantError("课程内还没有已解析的资料，请先到资料库完成解析")

    # 查询时自愈：历史数据/索引任务失败留下的缺向量块在 worker 内补嵌
    ensure_embedded(session, course_id=context["course_id"], material_ids=material_ids)
    # 语义打分下推 PG：向量 JSON 不出库（59MB 全量拉取 → ~4MB 仅文本），SQL 内算
    # cosine；非 PG 方言装载向量走旧路径，下推任一环失败降级词面——嵌入故障不断轮
    pushdown = supports_semantic_pushdown(session)
    chunks = load_content_chunks(
        session,
        course_id=context["course_id"],
        material_ids=material_ids,
        include_embedding=not pushdown,
    )
    if not chunks:
        raise AssistantError("该范围没有可检索的资料内容")

    semantic_scores = None
    if pushdown and embedding_configured():
        try:
            query_vectors = build_embedder().embed(_rag_query_variants(question))
            semantic_scores = load_semantic_scores(
                session,
                course_id=context["course_id"],
                material_ids=material_ids,
                query_vectors=query_vectors,
            )
        except Exception as exc:  # noqa: BLE001 — 下推失败降级词面，不上抛
            logger.warning("RAG 语义下推失败，降级词面: %s", exc)
            semantic_scores = None

    mode, ranked = _rank_rag_chunks(question, chunks, semantic_scores=semantic_scores)
    ranked = _expand_rag_neighborhood(ranked, chunks)
    payload = {
        "question": question,
        "material_id": material_id,
        "material_name": target_row.get("name") if target_row else None,
        "mode": mode,
        "sources": [
            {
                "block_id": item.chunk.id,
                "material_id": item.chunk.locator.get("material_id"),
                "material_name": item.chunk.locator.get("material_name"),
                "page_index": item.chunk.locator.get("page_index"),
                "heading_path": [
                    str(h) for h in list(item.chunk.locator.get("heading_path") or [])
                ][:6],
                "snippet": (
                    item.chunk.content[: _RAG_SNIPPET_CHARS] + "…"
                    if len(item.chunk.content) > _RAG_SNIPPET_CHARS
                    else item.chunk.content
                ),
            }
            for item in ranked
        ],
    }
    return payload, {"mode": mode, "ranked": ranked}


_DEFAULT_READ_REPLIES = {
    "course_overview": "课程当前各阶段状态见下表：",
    "usage_guide": "这个课程空间能做什么与出卷全流程见下卡，结合你当前进度的建议：",
}
# 合并后的查询工具按 section 给开场白（模型没给 reply 时的确定性兜底）
_SECTION_READ_REPLIES = {
    "materials": "这是你的资料清单与解析状态：",
    "framework": "命题框架与考核规则状态如下：",
    "blueprint": "各项目蓝图统计如下：",
    "contract": "合同状态如下：",
    "paper": "试卷状态如下：",
    "projects": "试卷项目列表如下：",
}


def _read_reply(tool: str, args: dict | None) -> str:
    if tool == "course_overview":
        section = str((args or {}).get("section") or "").strip()
        if section in _SECTION_READ_REPLIES:
            return _SECTION_READ_REPLIES[section]
    return _DEFAULT_READ_REPLIES.get(tool, "查询结果见下表：")


_DEFAULT_PROPOSAL_REPLIES = {
    "create_course": "已生成新建课程提案，确认后执行：",
    "update_course": "已生成课程信息修改提案，确认后执行：",
    "start_parse": "已生成解析启动提案，确认后执行：",
    "create_exam_project": "已生成试卷项目创建提案，确认后执行：",
    "update_exam_rules": "已生成考核规则修改提案，确认后写入（蓝图创建时按新规则确定性生成）：",
    "create_blueprint": "已生成蓝图创建提案，确认后按考核规则与知识目录生成草稿蓝图：",
    "confirm_blueprint": "已生成蓝图确认提案，确认后落库冻结题位计划，请核对参数：",
    "enqueue_blueprint_suggest": "已生成蓝图调整建议任务的发起提案，确认后执行：",
    "confirm_contract": "已生成合同重新分配提案（确认合同落库），请核对参数后执行：",
    "start_generation": "已生成 AI 生成任务的发起提案，确认后按已确认合同分批生成：",
    "enqueue_paper_review": "已生成整卷 AI 评审任务的发起提案，确认后执行（只读报告，不改数据）：",
    "update_question_type_format": "已生成题型格式修改提案，确认后写入考核规则：",
}


# ---------------------------------------------------------------------------
# 确认类提案的实物预览：让教师在气泡里看到「确认的到底是什么」
# ---------------------------------------------------------------------------

_PREVIEW_TOOLS = frozenset({"confirm_blueprint", "confirm_contract", "start_generation"})


def _active_blueprint_version_id(session: Session, *, course_id: str, project_id: str) -> str | None:
    """项目当前蓝图版本（active → 最新版本回退），与 _blueprint_summary 同口径。"""
    bv_id = session.execute(
        select(exam_projects.c.active_blueprint_version_id).where(
            exam_projects.c.id == project_id,
            exam_projects.c.course_id == course_id,
        )
    ).scalar_one_or_none()
    if bv_id:
        return bv_id
    return session.execute(
        select(blueprint_versions.c.id)
        .where(
            blueprint_versions.c.course_id == course_id,
            blueprint_versions.c.exam_project_id == project_id,
        )
        .order_by(blueprint_versions.c.version_no.desc())
        .limit(1)
    ).scalar_one_or_none()


def _preview_doc(source: str, items: list[dict], *, version_no: int | None = None) -> dict:
    difficulty: dict[str, int] = {}
    total = 0.0
    for item in items:
        key = item["difficulty"]
        difficulty[key] = difficulty.get(key, 0) + 1
        total += float(item["score"])
    return {
        "source": source,
        "version_no": version_no,
        "item_count": len(items),
        "total_score": round(total, 2),
        "difficulty": difficulty,
        "items": items,
    }


def _proposal_preview(session: Session, *, course_id: str, tool: str, payload: dict) -> dict:
    """确认/发起类提案卡的实物预览（只进卡片 payload，不进 LLM prompt）。

    - confirm_blueprint / confirm_contract：蓝图题位计划逐题明细。合同确认前
      尚无快照，展示的即是确定性分配算法将按此锁定的题位基础；
    - start_generation：已确认合同的真实槽位（缺快照时回退蓝图明细）。
    数据现势读库、course_id 隔离；项目无蓝图时返回空（提案本身不受影响）。
    """
    if tool not in _PREVIEW_TOOLS:
        return {}
    project_id = str(payload.get("project_id") or "")
    if not project_id:
        return {}

    if tool == "start_generation":
        snapshot = exam_project_service.get_current_contract_snapshot(
            session, course_id=course_id, project_id=project_id
        )
        slots = snapshot.get("slots") if isinstance(snapshot, dict) else None
        if isinstance(slots, list) and slots:
            items = [
                {
                    "item_index": int(s.get("item_index") or 0),
                    "question_type": str(s.get("question_type") or ""),
                    "score": float(s.get("score") or 0),
                    "difficulty": str(s.get("difficulty") or ""),
                    "exam_point": str(s.get("exam_point_title") or s.get("exam_point_id") or ""),
                    "knowledge_card": str(s.get("card_name") or s.get("card_id") or ""),
                }
                for s in slots
                if isinstance(s, dict)
            ]
            if items:
                return {"preview": _preview_doc("contract", items)}

    bv_id = _active_blueprint_version_id(session, course_id=course_id, project_id=project_id)
    if not bv_id:
        return {}
    version_no = session.execute(
        select(blueprint_versions.c.version_no).where(
            blueprint_versions.c.id == bv_id,
            blueprint_versions.c.course_id == course_id,
        )
    ).scalar_one_or_none()
    rows = session.execute(
        select(
            plan_items.c.item_index,
            plan_items.c.question_type,
            plan_items.c.score,
            plan_items.c.difficulty,
            plan_items.c.assessment_mode,
            exam_points.c.title,
            knowledge_cards.c.name,
        )
        .select_from(
            plan_items.outerjoin(
                exam_points,
                and_(
                    exam_points.c.id == plan_items.c.exam_point_id,
                    exam_points.c.course_id == course_id,
                ),
            ).outerjoin(
                knowledge_cards,
                and_(
                    knowledge_cards.c.id == plan_items.c.knowledge_card_id,
                    knowledge_cards.c.course_id == course_id,
                ),
            )
        )
        .where(
            plan_items.c.course_id == course_id,
            plan_items.c.blueprint_version_id == bv_id,
        )
        .order_by(plan_items.c.item_index)
    ).all()
    if not rows:
        return {}
    items = [
        {
            "item_index": int(row[0]),
            "question_type": str(row[1]),
            "score": float(row[2]),
            "difficulty": str(row[3]),
            "assessment_mode": str(row[4]),
            "exam_point": row[5] or "",
            "knowledge_card": row[6] or "",
        }
        for row in rows
    ]
    return {"preview": _preview_doc("blueprint", items, version_no=int(version_no) if version_no is not None else None)}


def route_intent(intent: dict, *, session: Session, context: dict, message: str = "") -> dict:
    """意图 → {kind: chat|result|proposal|rag, reply, action?, payload?}。

    AssistantError 表示意图/参数问题（上层带反馈重试一次）；工具白名单外的
    tool 名同样按 AssistantError 走重试，最终落确定性失败文案。message 为教师
    原话（RAG 的问题即原话，不经模型转述）。
    """
    action = intent.get("action")
    if action is None:
        # 纯问答：交段2流式生成正文（段1 reply 只作降级）
        return {"kind": "chat", "stream": True, "reply": intent.get("reply") or ""}

    tool = action["tool"]
    args = action.get("args") or {}

    if tool in REFUSED_TOOLS:
        # 拒绝必须用确定性文案：不得再进段2被模型改写（否则可能复述违规承诺）
        return {"kind": "chat", "stream": False, "reply": REFUSED_REPLY}

    if tool in READ_TOOLS:
        payload = execute_read_tool(session, context=context, tool=tool, args=args)
        reply = intent.get("reply") or _read_reply(tool, args)
        return {
            "kind": "result",
            "reply": reply,
            "action": {"kind": "result", "tool": tool, "args": args, "status": "completed"},
            "payload": payload,
        }

    if tool == RAG_TOOL:
        # 问题 = 教师原话：检索与段2 都用 message，不经模型转述（防改写失真）
        payload, retrieval = execute_rag(
            session, context=context, args=args, question=message
        )
        return {
            "kind": "rag",
            "stream": True,
            "reply": intent.get("reply") or _RAG_FALLBACK_REPLY,
            "retrieval": retrieval,
            "action": {"kind": "sources", "tool": RAG_TOOL, "args": args, "status": "completed"},
            "payload": payload,
        }

    if tool in PROPOSAL_TOOLS:
        payload = build_proposal_payload(tool, args, context=context)
        # 确认类提案附实物预览（蓝图题位/合同槽位）：教师点确认前能看到内容，
        # 而不是只有一行项目名。现势读库，任何缺失都不阻断提案本身。
        payload.update(
            _proposal_preview(session, course_id=context["course_id"], tool=tool, payload=payload)
        )
        # 卡片元数据与「要不要教师确认」同样来自注册表：前端据此决定渲染
        # 「确认执行」按钮还是自动执行并回报执（前后端不再各维护一份）。
        meta = proposal_card_meta(tool)
        payload.update(meta)
        reply = intent.get("reply") or _DEFAULT_PROPOSAL_REPLIES.get(tool, "已生成提案，确认后执行：")
        return {
            "kind": "proposal",
            "reply": reply,
            "action": {
                "kind": "proposal",
                "tool": tool,
                "args": args,
                # auto = 待自动执行（前端 claim 后转 executing）；proposed = 等教师确认
                "status": "auto" if meta.get("auto") else "proposed",
                "receipt": "",
            },
            "payload": payload,
        }

    raise AssistantError(f"未知工具 {tool}")


# ---------------------------------------------------------------------------
# 段2：流式正文（纯问答 / 资料内容问答）
# ---------------------------------------------------------------------------

_ANSWER_SYSTEM_PROMPT = """你是高校课程「{course_name}」工作台内的 AI 助手，正在与命题教师对话。

要求：
- 用中文自然回答，简洁直接，不臆造系统中不存在的数据；需要具体状态时引用 snapshot 中的真实值。
- 不承诺调整出题比例/难度/去重——这些由系统确定性算法保证。
- 涉及写操作只说明会生成提案由教师确认，不声称已执行。
- 与当前课程无关的问题礼貌拉回到课程工作台话题。"""


class _DeltaBuffer:
    """把流式 delta 聚批后再发布：避免每 token 一次 XADD。

    event 可换 "think"——思考增量与正文增量分通道聚批，语义与正文一致。
    """

    def __init__(
        self,
        sink: TurnEventSink,
        *,
        event: str = "delta",
        min_chars: int = 24,
        min_interval: float = 0.12,
    ) -> None:
        self.sink = sink
        self.event = event
        self.min_chars = min_chars
        self.min_interval = min_interval
        self._parts: list[str] = []
        self._size = 0
        self._last = time.monotonic()

    def add(self, text: str) -> None:
        self._parts.append(text)
        self._size += len(text)
        if self._size >= self.min_chars or (time.monotonic() - self._last) >= self.min_interval:
            self.flush()

    def flush(self) -> None:
        if not self._parts:
            return
        self.sink.publish(self.event, {"text": "".join(self._parts)})
        self._parts = []
        self._size = 0
        self._last = time.monotonic()


def stream_answer(
    client,
    context: dict,
    message: str,
    *,
    on_delta,
    on_think=None,
    call_context: ModelCallContext,
) -> str:
    """段2：流式生成纯问答正文（on_think 透传思考增量，与正文分通道）。"""
    system_prompt = _ANSWER_SYSTEM_PROMPT.replace("{course_name}", context["course_name"])
    payload = {
        "course": {"id": context["course_id"], "name": context["course_name"]},
        "history": context.get("history") or [],
        "snapshot": {
            "materials": context.get("materials") or [],
            "framework": context.get("framework"),
            "catalog": context.get("catalog"),
            "projects": context.get("projects") or [],
        },
        "user_message": message,
    }
    return client.stream_text(
        system_prompt=system_prompt,
        payload=payload,
        temperature=0.6,
        on_delta=on_delta,
        on_think=on_think,
        call_context=call_context,
    )


_RAG_ANSWER_SYSTEM_PROMPT = """你是高校课程「{course_name}」工作台内的 AI 助手，正在基于教师指定的资料回答问题。

要求：
- **只依据下方「资料片段」作答**：片段覆盖不足时明确说明「资料里没有找到」，不得编造片段之外的内容。
- 引用出处：结合片段的页码/章节标题指明依据位置；总结型请求先给结构化要点再展开。
- 用中文简洁自然回答；与资料无关的寒暄礼貌拉回资料话题。
- 不承诺出题比例/难度/去重——这些由系统确定性算法保证；写操作只说明会生成提案由教师确认。"""


def stream_rag_answer(
    client,
    context: dict,
    message: str,
    *,
    retrieval: dict,
    on_delta,
    on_think=None,
    call_context: ModelCallContext,
) -> str:
    """段2（RAG）：以检索片段为依据流式生成回答。"""

    system_prompt = _RAG_ANSWER_SYSTEM_PROMPT.replace("{course_name}", context["course_name"])
    payload = {
        "course": {"id": context["course_id"], "name": context["course_name"]},
        "history": context.get("history") or [],
        "question": message,
        "sources": [
            {
                "material_name": item.chunk.locator.get("material_name"),
                "page_index": item.chunk.locator.get("page_index"),
                "heading_path": list(item.chunk.locator.get("heading_path") or []),
                "text": item.chunk.content[:_RAG_BLOCK_PROMPT_CHARS],
            }
            for item in retrieval["ranked"]
        ],
    }
    return client.stream_text(
        system_prompt=system_prompt,
        payload=payload,
        temperature=0.3,
        on_delta=on_delta,
        on_think=on_think,
        call_context=call_context,
    )


# ---------------------------------------------------------------------------
# 消息读写
# ---------------------------------------------------------------------------


def _insert_message(
    session: Session,
    *,
    course_id: str,
    task_run_id: str,
    role: str,
    content: str,
    action: dict | None = None,
    stream_status: str = "complete",
    message_id: str | None = None,
    session_id: str | None = None,
    thinking: str = "",
) -> str:
    new_id = message_id or uuid4().hex
    session.execute(
        assistant_messages.insert().values(
            id=new_id,
            course_id=course_id,
            task_run_id=task_run_id,
            # 会话归属：应用层恒写（v3 多会话）；旧任务 payload 缺失时为 NULL
            session_id=session_id,
            role=role,
            content=content,
            # 思考模型的推理全文：与 content 分离落列，绝不混进正式回复
            thinking=thinking,
            action=action or {},
            stream_status=stream_status,
            # 显式带微秒的时间戳：server_default 在 SQLite 无微秒，同秒的
            # user/assistant 消息会排序错乱（历史踩过同秒字符串比较坑）。
            created_at=datetime.now(timezone.utc),
        )
    )
    _touch_session(session, session_id=session_id)
    return new_id


def message_view(row) -> dict:
    return {
        "id": row["id"],
        "task_run_id": row["task_run_id"],
        "role": row["role"],
        "content": row["content"],
        # 思考全文（模型推理）：随消息持久化，前端渲染成独立思考区
        "thinking": row["thinking"] or "",
        "action": row["action"] or {},
        "stream_status": row["stream_status"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def list_messages(
    session: Session,
    *,
    course_id: str,
    session_id: str | None = None,
    limit: int = 200,
) -> list[dict]:
    stmt = select(assistant_messages).where(assistant_messages.c.course_id == course_id)
    if session_id:
        stmt = stmt.where(assistant_messages.c.session_id == session_id)
    rows = session.execute(
        stmt.order_by(assistant_messages.c.created_at.asc(), assistant_messages.c.id.asc())
        .limit(limit)
    ).mappings().all()
    return [message_view(row) for row in rows]


def patch_message_action(
    session: Session,
    *,
    course_id: str,
    message_id: str,
    action_status: str,
    receipt: str = "",
) -> dict:
    """提案卡状态回写：仅 proposed → executed/dismissed，单向，不执行任何业务。"""
    if action_status not in _PROPOSAL_TRANSITIONS:
        raise AssistantError("非法的提案状态")
    row = session.execute(
        select(assistant_messages).where(
            assistant_messages.c.id == message_id,
            assistant_messages.c.course_id == course_id,
        )
    ).mappings().one_or_none()
    if row is None:
        raise AssistantError("消息不存在")
    action = dict(row["action"] or {})
    if action.get("kind") != "proposal":
        raise AssistantError("只有提案卡可以回写状态")
    current_status = str(action.get("status") or "")
    if action_status not in _ALLOWED_STATUS_MOVES.get(current_status, set()):
        raise AssistantError(f"提案卡当前状态 {current_status or '未知'} 不能迁移到 {action_status}")
    action["status"] = action_status
    if receipt:
        action["receipt"] = str(receipt)[:500]
    session.execute(
        assistant_messages.update()
        .where(assistant_messages.c.id == message_id, assistant_messages.c.course_id == course_id)
        .values(action=action)
    )
    refreshed = session.execute(
        select(assistant_messages).where(
            assistant_messages.c.id == message_id,
            assistant_messages.c.course_id == course_id,
        )
    ).mappings().one()
    return message_view(refreshed)


_GENERATION_REPORT_TASK_TYPE = "assistant_notice"


def _dropped_slots_of_project(session: Session, *, course_id: str, project: dict) -> list[dict]:
    """读当前卷 metadata.dropped_slots（组卷宁缺勿滥剔除的题位），课程隔离。

    item_count/total_score 与 dropped_slots 必须出自同一份 current 卷
    （project.paper_version_id 即 _with_paper_summary 的选版结果），否则
    「蓝图计划数 = 成卷数 + 缺口数」的口径会对不上。
    """
    pv_id = project.get("paper_version_id")
    if not pv_id:
        return []
    row = session.execute(
        select(paper_versions.c["metadata"]).where(
            paper_versions.c.id == pv_id,
            paper_versions.c.course_id == course_id,
        )
    ).first()
    meta = row[0] if row else None
    dropped = meta.get("dropped_slots") if isinstance(meta, dict) else None
    if not isinstance(dropped, list):
        return []
    return [d for d in dropped if isinstance(d, dict)]


# 缺口播报的确定性文案映射：收尾通报不需要模型润色，原因与题型都翻成人话
_DROPPED_REASON_LABELS = {
    "missing_stem": "模型未产出可用题干",
    "missing_answer": "模型未产出可用答案",
    "missing_generated_question": "生成阶段未返回该题",
}
_DROPPED_TYPE_LABELS = {"comprehensive": "综合题"}


def _generation_gap_content(*, name: str, detail: str, item_count: int, dropped: list[dict]) -> str:
    """缺口播报文案：缺几道、缺哪些题位、为什么缺、怎么补——一并说清。

    成功分支原版只报「共 N 道题」，蓝图 42 成卷 40 时教师完全无感知
    （2026-10-04 实测：缺口消息照常发、缺口本身零提示）。
    """
    planned = item_count + len(dropped)
    items = "、".join(
        f"#{d['item_index']}" for d in dropped if d.get("item_index") is not None
    ) or "部分题位"
    types: list[str] = []
    for d in dropped:
        raw = str(d.get("question_type") or "").strip()
        label = _DROPPED_TYPE_LABELS.get(raw, raw)
        if label and label not in types:
            types.append(label)
    reasons: list[str] = []
    for d in dropped:
        raw = str(d.get("reason") or "").strip()
        label = _DROPPED_REASON_LABELS.get(raw) or raw or "未通过可用性校验"
        if label not in reasons:
            reasons.append(label)
    where = f"（{'、'.join(types)}）" if types else ""
    return (
        f"「{name}」的试卷已生成：{detail}。注意卷面有缺口——蓝图计划 {planned} 道题位，"
        f"成卷 {item_count} 道；题位 {items}{where} 因{'、'.join(reasons)}未入卷。"
        "到『试卷』页点「重新生成」即可补齐缺题，补齐前也可先审核现有题目。"
    )


def report_generation_complete(
    session: Session,
    *,
    course_id: str,
    session_id: str,
    project_id: str,
) -> dict:
    """生成结束后由**后端主动**播报一条助手消息（不写用户消息）。

    为什么不让前端发「继续」：出卷要跑几分钟，教师这段时间只是在等待、没有
    任何操作；若沿用自动追问，聊天里会凭空冒出一条教师自己发的「继续」气泡，
    很突兀。收尾通报本来就该由助手发出，所以这里只落 assistant 消息。

    文案是确定性的（状态通报不需要模型润色）：成功给「已生成 N 道题 + 去审核」，
    失败给原因与「重新生成」引导；成功但有题位被剔除（dropped_slots 非空）时
    额外把缺口说清（蓝图计划数/缺哪些题位/原因/补齐引导），缺口不再无声。

    幂等：按 (course, session, project, 当前生成 run) 建一条 assistant_notice
    任务的 idempotency_key，重复调用直接返回已播报结果（前端轮询可能多次触发）；
    带 run id 是为了让**重跑**成为新一轮收尾——旧口径会把重跑后的题数与缺口
    状态永久吞掉，教师停留在上一卷的旧消息上。
    """
    if _load_session_row(session, course_id=course_id, session_id=session_id) is None:
        raise AssistantError("会话不存在")
    try:
        project = exam_project_service.get_project(
            session, course_id=course_id, project_id=project_id
        )
    except exam_project_service.ExamProjectNotFoundError:
        raise AssistantError("试卷项目不存在")

    task_status = str(project.get("generation_task_status") or "")
    project_status = str(project.get("status") or "")
    settled = task_status in {"succeeded", "failed", "cancelled"} or project_status in {
        "review",
        "exported",
    }
    if not settled:
        # 还没跑完：不播报（前端会继续轮询，到终态再调）
        return {"reported": False, "reason": "not_settled"}

    run_id = str(project.get("active_generation_run_id") or "")
    key = hashlib.sha256(
        f"generation-report:{course_id}:{session_id}:{project_id}:{run_id}".encode()
    ).hexdigest()[:24]
    existing_task = session.execute(
        select(task_runs.c.id).where(
            task_runs.c.course_id == course_id,
            task_runs.c.idempotency_key == key,
        )
    ).scalar_one_or_none()
    task_id = existing_task or uuid4().hex
    existing_message = session.execute(
        select(assistant_messages.c.id)
        .where(assistant_messages.c.task_run_id == task_id)
        .limit(1)
    ).scalar_one_or_none()
    if existing_message is not None:
        return {"reported": False, "reason": "already_reported", "message_id": existing_message}

    name = str(project.get("name") or "该试卷")
    failed = task_status in {"failed", "cancelled"} and project_status not in {
        "review",
        "exported",
    }
    if failed:
        content = (
            f"「{name}」的试卷生成没有成功：{project.get('generation_error') or '未知原因'}。"
            "可到『试卷』页重新生成。"
        )
    else:
        detail = "，".join(
            [
                text
                for text in (
                    f"共 {project.get('item_count')} 道题" if project.get("item_count") else "",
                    f"总分 {project.get('total_score')}" if project.get("total_score") is not None else "",
                )
                if text
            ]
        ) or "试卷已就绪"
        dropped = _dropped_slots_of_project(session, course_id=course_id, project=project)
        if dropped:
            content = _generation_gap_content(
                name=name,
                detail=detail,
                item_count=int(project.get("item_count") or 0),
                dropped=dropped,
            )
        else:
            content = (
                f"「{name}」的试卷已生成：{detail}。可以进入审核了——"
                "到『试卷』页逐题查看与编辑，定稿与导出也在该页完成。"
            )

    if existing_task is None:
        create_task_run(
            session,
            course_id=course_id,
            task_type=_GENERATION_REPORT_TASK_TYPE,
            idempotency_key=key,
            input_version=_INPUT_VERSION,
            payload={
                "course_id": course_id,
                "session_id": session_id,
                "project_id": project_id,
                # 系统主动通报：不是教师轮次，前端不据此渲染流式区
                "system_notice": True,
            },
            task_id=task_id,
        )
    now = datetime.now(timezone.utc)
    session.execute(
        task_runs.update()
        .where(task_runs.c.id == task_id, task_runs.c.course_id == course_id)
        .values(
            status="succeeded",
            stage="completed",
            progress=100,
            result={"kind": "generation_report", "project_id": project_id},
            updated_at=now,
            completed_at=now,
        )
    )
    message_id = _insert_message(
        session,
        course_id=course_id,
        task_run_id=task_id,
        role="assistant",
        content=content,
        action={
            "kind": "generation_report",
            "tool": "start_generation",
            "project_id": project_id,
            "status": "completed",
            "receipt": "",
        },
        session_id=session_id,
    )
    return {
        "reported": True,
        "message_id": message_id,
        "task_run_id": task_id,
        "content": content,
    }


def _assistant_reply_exists(session: Session, task_run_id: str) -> dict | None:
    row = session.execute(
        select(assistant_messages)
        .where(
            assistant_messages.c.task_run_id == task_run_id,
            assistant_messages.c.role == "assistant",
        )
        .limit(1)
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# 停止生成（v3）：协作式取消——端点只改 task_runs 状态，worker 检查点中止
# ---------------------------------------------------------------------------


class TurnCancelled(Exception):
    """教师停止生成：携带已流出的部分正文（用于保留产出、标记 stopped）。"""

    def __init__(self, partial: str = "") -> None:
        super().__init__("assistant turn cancelled by teacher")
        self.partial = partial


def _turn_cancelled(session: Session, *, course_id: str, task_run_id: str) -> bool:
    """检查点探测：任务是否已被取消。探测异常按未取消处理（不阻断轮次）。

    用 worker 自己的 session 读（Postgres READ COMMITTED 每语句取新快照、
    SQLite SELECT 自动提交，都能看到取消端点已提交的更新）；检查点处均无
    未提交写入，rollback 恢复是安全的。
    """
    try:
        status = session.execute(
            select(task_runs.c.status).where(
                task_runs.c.id == task_run_id,
                task_runs.c.course_id == course_id,
            )
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "assistant 取消探测失败 task_run_id=%s: %s", task_run_id, exc
        )
        session.rollback()
        return False
    return status == "cancelled"


class _CancelProbe:
    """流式高频回调内的节流探测（默认 0.5s 一次，避免每 token 打库）。"""

    def __init__(
        self,
        session: Session,
        *,
        course_id: str,
        task_run_id: str,
        interval: float = 0.5,
    ) -> None:
        self._session = session
        self._course_id = course_id
        self._task_run_id = task_run_id
        self._interval = interval
        self._last = 0.0

    def cancelled(self) -> bool:
        now = time.monotonic()
        if now - self._last < self._interval:
            return False
        self._last = now
        return _turn_cancelled(
            self._session, course_id=self._course_id, task_run_id=self._task_run_id
        )


def _guarded_delta(buffer: _DeltaBuffer, probe: _CancelProbe, partial: list[str]):
    """检查点 B：攒正文 → 入缓冲（聚批发射）→ 节流探测 → 取消即中止。

    异常从 on_delta 穿透 stream_text（非 httpx 异常不被其 except 吞、本就不重试），
    `with stream_cm` 随之关闭连接即停上游生成。
    """

    def _on_delta(text: str) -> None:
        partial.append(text)
        buffer.add(text)
        if probe.cancelled():
            raise TurnCancelled("".join(partial))

    return _on_delta


def _guarded_think(buffer: _DeltaBuffer, probe: _CancelProbe, parts: list[str]):
    """思考增量：收集全文 → 入思考缓冲聚批发射 → 节流探测取消。

    与 _guarded_delta 同款穿透语义，但取消时不带正文 partial——思考阶段
    正文尚未开始，收口文案走「（已停止）」；已流出的思考照样随 parts 落库。
    """

    def _on_think(text: str) -> None:
        parts.append(text)
        buffer.add(text)
        if probe.cancelled():
            raise TurnCancelled("")

    return _on_think


# ---------------------------------------------------------------------------
# 主执行
# ---------------------------------------------------------------------------


def run_turn(session: Session, *, payload: dict, client, sink: TurnEventSink) -> dict:
    """一轮对话的完整执行：意图 → 路由 → 落库 → 事件。只写 assistant_messages。"""
    course_id = str(payload["course_id"])
    task_run_id = str(payload.get("task_run_id") or "")
    message = str(payload.get("message") or "")
    session_id = str(payload.get("session_id") or "") or None

    # 幂等：worker 租约过期重领时，消息已落库就不重跑（防重复烧模型与重复消息）
    existing = _assistant_reply_exists(session, task_run_id)
    if existing is not None:
        sink.publish("done", {"message_id": existing["id"], "task_run_id": task_run_id})
        return {"message_id": existing["id"], "duplicate": True}

    # 检查点 A：取消先于产出 → 不调模型不落消息（与「取消先于领取」一致——
    # 还没产出就不留痕），SSE 端由 DB 兜底按终态发 done。
    if _turn_cancelled(session, course_id=course_id, task_run_id=task_run_id):
        return {"task_run_id": task_run_id, "cancelled": True}

    context = load_turn_context(
        session, course_id=course_id, session_id=session_id
    )
    call_context = ModelCallContext(course_id=course_id, stage=TASK_TYPE)

    # 思考模型的推理内容：全程与正文通道分离——意图阶段（流式）增量聚批
    # 推、段2（流式）聚批推；全文收集后随消息落库，刷新/切会话仍可见。
    thinking_parts: list[str] = []
    # 意图阶段思考聚批：SSE 增量小而密，直接 publish 会每 token 一次 XADD；
    # 意图结束后 flush 收口，残余小增量不滞留（与段2 的 think_buffer 独立）。
    intent_think_buffer = _DeltaBuffer(sink, event="think")

    def _collect_think(text: str) -> None:
        thinking_parts.append(text)
        intent_think_buffer.add(text)

    # 带反馈纠错一次：解析与路由的 AssistantError 统一交回模型重试——
    # 破损 JSON（空响应/缺 reply 与 action）也在这里被拦下重试，
    # 不再静默降级成「只有口头承诺、不出卡」的纯聊天。
    try:
        intent = parse_intent(
            client, context, message, call_context=call_context, on_think=_collect_think
        )
        routed = route_intent(intent, session=session, context=context, message=message)
    except AssistantError as first_error:
        try:
            intent = parse_intent(
                client,
                context,
                message,
                call_context=call_context,
                previous_error=str(first_error),
                on_think=_collect_think,
            )
            routed = route_intent(intent, session=session, context=context, message=message)
        except AssistantError as second_error:
            routed = {
                "kind": "chat",
                "stream": False,  # 确定性失败文案，不再问模型
                "reply": f"我没能处理这个请求（{second_error}）。请换个说法，或到对应页面操作。",
            }

    # 意图阶段思考收口：聚批缓冲残余增量全部推出，再进入段2
    intent_think_buffer.flush()

    stream_status = "complete"
    probe = _CancelProbe(session, course_id=course_id, task_run_id=task_run_id)
    partial: list[str] = []
    if routed["kind"] == "chat":
        if routed.get("stream"):
            buffer = _DeltaBuffer(sink)
            think_buffer = _DeltaBuffer(sink, event="think")
            try:
                content = stream_answer(
                    client,
                    context,
                    message,
                    on_delta=_guarded_delta(buffer, probe, partial),
                    on_think=_guarded_think(think_buffer, probe, thinking_parts),
                    call_context=call_context,
                )
            except TurnCancelled as stopped:
                # 教师停止：保留已流出的部分正文，标记 stopped（不走失败降级）
                logger.info("assistant 轮次被教师停止（段2） task_run_id=%s", task_run_id)
                content = stopped.partial or "（已停止）"
                stream_status = "stopped"
            except Exception as exc:  # noqa: BLE001
                # 流式降级：段1 已有整段 reply，直接用它收口，不让一轮对话整体失败
                logger.warning("assistant 段2流式失败，降级段1回复 task_run_id=%s: %s", task_run_id, exc)
                content = routed.get("reply") or "（回答生成失败，请重试）"
                stream_status = "failed"
            buffer.flush()
            think_buffer.flush()
        else:
            # 拒绝/兜底等确定性文案：整段一次推，不再进模型改写
            content = routed["reply"]
            sink.publish("delta", {"text": content})
        action: dict = {}
    elif routed["kind"] == "rag":
        # 资料内容问答：检索片段驱动的流式正文 + 命中时来源引用卡
        buffer = _DeltaBuffer(sink)
        think_buffer = _DeltaBuffer(sink, event="think")
        try:
            content = stream_rag_answer(
                client,
                context,
                message,
                retrieval=routed["retrieval"],
                on_delta=_guarded_delta(buffer, probe, partial),
                on_think=_guarded_think(think_buffer, probe, thinking_parts),
                call_context=call_context,
            )
        except TurnCancelled as stopped:
            # 教师停止：保留已流出的部分正文，标记 stopped（不走失败降级）
            logger.info("assistant RAG轮次被教师停止 task_run_id=%s", task_run_id)
            content = stopped.partial or "（已停止）"
            stream_status = "stopped"
        except Exception as exc:  # noqa: BLE001
            # 流式降级：段1 已有引导语，用它收口，不让一轮对话整体失败
            logger.warning("assistant RAG段2流式失败，降级段1回复 task_run_id=%s: %s", task_run_id, exc)
            content = routed.get("reply") or "（回答生成失败，请重试）"
            stream_status = "failed"
        buffer.flush()
        think_buffer.flush()
        if routed["payload"].get("sources"):
            action = dict(routed["action"])
            action["payload"] = routed["payload"]
            sink.publish(
                "card",
                {"kind": "sources", "tool": RAG_TOOL, "payload": routed["payload"]},
            )
        else:
            action = {}  # 无命中：普通问答形态，不落卡（正文说明没找到）
    elif routed["kind"] == "result":
        content = routed["reply"]
        action = dict(routed["action"])
        action["payload"] = routed["payload"]
        sink.publish(
            "card",
            {"kind": "result", "tool": action["tool"], "payload": routed["payload"]},
        )
    else:  # proposal
        content = routed["reply"]
        action = dict(routed["action"])
        action["payload"] = routed["payload"]
        sink.publish(
            "card",
            {
                "kind": "proposal",
                "tool": action["tool"],
                "payload": routed["payload"],
                "action": {k: v for k, v in action.items() if k != "payload"},
            },
        )

    # 检查点 C：落库前确认取消（覆盖段1 完成后、无流式的窗口）→ 保留产出、标记 stopped
    if stream_status != "stopped" and _turn_cancelled(
        session, course_id=course_id, task_run_id=task_run_id
    ):
        stream_status = "stopped"

    message_id = _insert_message(
        session,
        course_id=course_id,
        task_run_id=task_run_id,
        role="assistant",
        content=content,
        action=action,
        stream_status=stream_status,
        session_id=session_id,
        thinking="".join(thinking_parts),
    )
    # 先落库再发 done：前端收到 done 立即拉 messages 必须可见（避免时序竞态）
    session.commit()
    sink.publish("done", {"message_id": message_id, "task_run_id": task_run_id})
    return {"message_id": message_id, "tool": action.get("tool"), "kind": routed["kind"]}


def build_client():
    """构造真实 LLM 客户端（worker 入口默认使用）。"""
    from app.adapters.model.llm_gateway import LLMJsonClient
    from app.config import settings
    from app.db.session import get_session_factory
    from app.services.model_call_service import DatabaseModelCallRecorder

    if not llm_configured():
        raise AssistantError("LLM model is not configured")
    return LLMJsonClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        disable_thinking=settings.llm_disable_thinking,
        timeout=_CLIENT_TIMEOUT_SECONDS,
        max_attempts=_CLIENT_MAX_ATTEMPTS,
        recorder=DatabaseModelCallRecorder(get_session_factory()),
    )


def _persist_failed_message(payload: dict, error: str) -> None:
    """失败消息用独立会话落库：当前 session 可能已脏，不能依赖它。"""
    task_run_id = str(payload.get("task_run_id") or "")
    course_id = str(payload.get("course_id") or "")
    if not task_run_id or not course_id:
        return
    from app.db.session import get_session_factory

    probe = get_session_factory()()
    try:
        if _assistant_reply_exists(probe, task_run_id) is not None:
            return
        _insert_message(
            probe,
            course_id=course_id,
            task_run_id=task_run_id,
            role="assistant",
            content=f"这一轮处理失败：{error}",
            action={},
            stream_status="failed",
            # 旧任务 payload 可能没有会话（部署瞬间的在途任务）→ NULL 孤儿行
            session_id=str(payload.get("session_id") or "") or None,
        )
        probe.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("assistant 失败消息落库失败 task_run_id=%s: %s", task_run_id, exc)
        probe.rollback()
    finally:
        probe.close()


def execute_turn_task(
    session: Session, *, payload: dict, client=None, sink: TurnEventSink | None = None
) -> dict:
    """worker 入口：默认构造 LLM 客户端与事件通道；失败发 error 事件并落失败消息。"""
    task_run_id = str(payload.get("task_run_id") or "")
    if sink is None:
        sink = build_event_sink(task_run_id)
    if client is None:
        client = build_client()
    try:
        return run_turn(session, payload=payload, client=client, sink=sink)
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        sink.publish("error", {"message": detail[:500], "task_run_id": task_run_id})
        _persist_failed_message(payload, detail)
        raise


# ---------------------------------------------------------------------------
# 会话管理（v3 多会话：新建/切换/重命名/删除；会话是消息时间线与记忆的边界）
# ---------------------------------------------------------------------------

_SESSION_TITLE_MAX = 40
_DEFAULT_SESSION_TITLE = "新会话"


def session_view(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def list_sessions(session: Session, *, course_id: str, limit: int = 100) -> list[dict]:
    rows = session.execute(
        select(assistant_sessions)
        .where(assistant_sessions.c.course_id == course_id)
        .order_by(
            assistant_sessions.c.updated_at.desc(), assistant_sessions.c.id.desc()
        )
        .limit(limit)
    ).all()
    return [session_view(r._mapping) for r in rows]


def create_session(
    session: Session, *, course_id: str, title: str = "", commit: bool = False
) -> dict:
    """新建会话（标题缺省「新会话」，超长截断）。commit 默认由路由负责。"""

    clean = str(title or "").strip()[:_SESSION_TITLE_MAX] or _DEFAULT_SESSION_TITLE
    now = datetime.now(timezone.utc)
    new_id = uuid4().hex
    session.execute(
        assistant_sessions.insert().values(
            id=new_id, course_id=course_id, title=clean, created_at=now, updated_at=now
        )
    )
    if commit:
        session.commit()
    row = session.execute(
        select(assistant_sessions).where(
            assistant_sessions.c.id == new_id,
            assistant_sessions.c.course_id == course_id,
        )
    ).one()
    return session_view(row._mapping)


def _load_session_row(session: Session, *, course_id: str, session_id: str):
    """按 (id, course_id) 取会话行——跨课程即视为不存在（隔离红线）。"""

    return session.execute(
        select(assistant_sessions).where(
            assistant_sessions.c.id == session_id,
            assistant_sessions.c.course_id == course_id,
        )
    ).one_or_none()


def rename_session(
    session: Session, *, course_id: str, session_id: str, title: str
) -> dict:
    clean = str(title or "").strip()
    if not clean:
        raise AssistantError("会话标题不能为空")
    row = _load_session_row(session, course_id=course_id, session_id=session_id)
    if row is None:
        raise AssistantError("会话不存在")
    session.execute(
        assistant_sessions.update()
        .where(assistant_sessions.c.id == session_id, assistant_sessions.c.course_id == course_id)
        .values(title=clean[:_SESSION_TITLE_MAX], updated_at=datetime.now(timezone.utc))
    )
    row = _load_session_row(session, course_id=course_id, session_id=session_id)
    return session_view(row._mapping)


def delete_session(session: Session, *, course_id: str, session_id: str) -> None:
    """删除会话及其全部消息；会话内有在途轮次先拒绝（防 worker 落库撞 FK）。"""

    if _load_session_row(session, course_id=course_id, session_id=session_id) is None:
        raise AssistantError("会话不存在")
    inflight = session.execute(
        select(task_runs.c.id)
        .where(
            task_runs.c.course_id == course_id,
            task_runs.c.task_type == TASK_TYPE,
            task_runs.c.status.notin_(TERMINAL_TASK_STATUSES),
            task_runs.c.payload["session_id"].as_string() == session_id,
        )
        .limit(1)
    ).scalar_one_or_none()
    if inflight is not None:
        raise AssistantError("会话内仍有在途对话，请先停止后再删除")
    session.execute(
        assistant_messages.delete().where(
            assistant_messages.c.course_id == course_id,
            assistant_messages.c.session_id == session_id,
        )
    )
    session.execute(
        assistant_sessions.delete().where(
            assistant_sessions.c.course_id == course_id,
            assistant_sessions.c.id == session_id,
        )
    )


def ensure_default_session(session: Session, *, course_id: str) -> str:
    """课程最近会话；无则新建「新会话」——turns 不带 session_id 的兜底路径。"""

    recent = session.execute(
        select(assistant_sessions.c.id)
        .where(assistant_sessions.c.course_id == course_id)
        .order_by(assistant_sessions.c.updated_at.desc(), assistant_sessions.c.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if recent is not None:
        return recent
    return create_session(session, course_id=course_id)["id"]


def _resolve_session_id(session: Session, *, course_id: str, session_id: str | None) -> str:
    """校验/解析本轮所属会话：给定必须属本课程，缺省走默认会话。"""

    if not session_id:
        return ensure_default_session(session, course_id=course_id)
    if _load_session_row(session, course_id=course_id, session_id=session_id) is None:
        raise AssistantError("会话不存在")
    return session_id


def _touch_session(session: Session, *, session_id: str | None) -> None:
    """消息落库后 bump 会话活跃时间（列表按最近活跃排序）。"""

    if not session_id:
        return
    session.execute(
        assistant_sessions.update()
        .where(assistant_sessions.c.id == session_id)
        .values(updated_at=datetime.now(timezone.utc))
    )


def _maybe_retitle_session(
    session: Session, *, session_id: str | None, first_message: str
) -> None:
    """首条用户消息把「新会话」自动改成消息摘要（教师不用手动改名）。"""

    if not session_id:
        return
    current = session.execute(
        select(assistant_sessions.c.title).where(assistant_sessions.c.id == session_id)
    ).scalar_one_or_none()
    if current is not None and current != _DEFAULT_SESSION_TITLE:
        return
    clean = str(first_message or "").replace("\n", " ").strip()[:_SESSION_TITLE_MAX]
    if not clean:
        return
    session.execute(
        assistant_sessions.update()
        .where(assistant_sessions.c.id == session_id)
        .values(title=clean)
    )


# ---------------------------------------------------------------------------
# 任务入队
# ---------------------------------------------------------------------------


def _task_key(course_id: str, session_id: str, message: str) -> str:
    # 会话参与幂等键：同一句话在两个会话是两个任务（v3 多会话）
    return hashlib.sha256(f"turn:{course_id}:{session_id}:{message}".encode()).hexdigest()[:24]


def enqueue_turn(
    session: Session,
    *,
    course_id: str,
    message: str,
    session_id: str | None = None,
) -> dict:
    """写 user 消息并创建 assistant_turn 任务；调用方负责 commit 与 outbox 派发。

    幂等语义（对齐 enqueue_propose）：同会话同文本的**在途**任务复用
    （双击/重发不重复烧模型），已到终态的任务换一把新键。
    session_id 缺省时用课程最近会话、无则新建（旧客户端/测试的兜底路径）。
    """
    message = str(message or "").strip()
    if not message:
        raise AssistantError("消息不能为空")
    if len(message) > _MESSAGE_MAX_CHARS:
        raise AssistantError(f"消息过长（≤{_MESSAGE_MAX_CHARS} 字）")

    course_exists = session.execute(
        select(Course.id).where(Course.id == course_id)
    ).scalar_one_or_none()
    if course_exists is None:
        raise AssistantError("课程不存在")

    resolved_session_id = _resolve_session_id(
        session, course_id=course_id, session_id=session_id
    )

    base_key = _task_key(course_id, resolved_session_id, message)
    existing = session.execute(
        select(task_runs.c.id, task_runs.c.status, task_runs.c.payload).where(
            task_runs.c.course_id == course_id,
            task_runs.c.idempotency_key == base_key,
        )
    ).one_or_none()
    if existing is not None and existing[1] not in TERMINAL_TASK_STATUSES:
        existing_payload = existing[2] or {}
        return {
            "task_run_id": existing[0],
            "user_message_id": existing_payload.get("user_message_id"),
            "session_id": resolved_session_id,
        }
    key = (
        hashlib.sha256(f"{base_key}:{uuid4().hex}".encode()).hexdigest()[:24]
        if existing is not None
        else base_key
    )

    user_message_id = uuid4().hex
    turn_id = uuid4().hex
    create_task_run(
        session,
        course_id=course_id,
        task_type=TASK_TYPE,
        idempotency_key=key,
        input_version=_INPUT_VERSION,
        payload={
            "course_id": course_id,
            "message": message,
            "task_run_id": turn_id,
            "user_message_id": user_message_id,
            "session_id": resolved_session_id,
        },
        task_id=turn_id,
    )
    _insert_message(
        session,
        course_id=course_id,
        task_run_id=turn_id,
        role="user",
        content=message,
        message_id=user_message_id,
        session_id=resolved_session_id,
    )
    # 首条消息把「新会话」自动改成消息摘要（在 touch 之后改标题即可）
    _maybe_retitle_session(
        session, session_id=resolved_session_id, first_message=message
    )
    return {
        "task_run_id": turn_id,
        "user_message_id": user_message_id,
        "session_id": resolved_session_id,
    }


# 内部接力指令：教师点完确认后由系统替流程推进，**不是**教师说的话，
# 因此不落 user 消息（聊天里不会平白多出一条「继续」气泡）。
_RELAY_INSTRUCTION = "继续"


def enqueue_relay(
    session: Session,
    *,
    course_id: str,
    session_id: str,
    after_message_id: str,
) -> dict:
    """确认/自动执行成功后推进下一张卡的**内部**轮次（不写用户消息）。

    与 enqueue_turn 的差别只有一处：不插 user 消息。为什么：教师在这一步只是
    点了「确认执行」（或卡片自动执行完），并没有在对话里说话；沿用「替他发一条
    继续」会在时间线上留下教师没打过的字，很突兀。改写 user 消息后，模型看到的
    历史是「上一步已执行」的卡片记录 + 这条内部指令，阶梯判断照样成立。

    幂等：以「在推进哪条消息之后」为键——同一条消息重复请求复用同一轮，
    不会把流程推两步（双击/重试安全）。
    """
    if _load_session_row(session, course_id=course_id, session_id=session_id) is None:
        raise AssistantError("会话不存在")
    row = session.execute(
        select(assistant_messages.c.id, assistant_messages.c.role, assistant_messages.c.content).where(
            assistant_messages.c.id == after_message_id,
            assistant_messages.c.course_id == course_id,
            assistant_messages.c.session_id == session_id,
        )
    ).one_or_none()
    if row is None:
        raise AssistantError("目标消息不存在")

    key = hashlib.sha256(
        f"relay:{course_id}:{session_id}:{after_message_id}".encode()
    ).hexdigest()[:24]
    existing = session.execute(
        select(task_runs.c.id, task_runs.c.status).where(
            task_runs.c.course_id == course_id,
            task_runs.c.idempotency_key == key,
        )
    ).one_or_none()
    if existing is not None:
        # 已推进过（含已完成）：原样返回，让调用方按状态决定续流还是刷新
        return {
            "task_run_id": existing[0],
            "session_id": session_id,
            "status": existing[1],
            "duplicate": True,
        }

    turn_id = uuid4().hex
    create_task_run(
        session,
        course_id=course_id,
        task_type=TASK_TYPE,
        idempotency_key=key,
        input_version=_INPUT_VERSION,
        payload={
            "course_id": course_id,
            "message": _RELAY_INSTRUCTION,
            "task_run_id": turn_id,
            "session_id": session_id,
            # 内部接力：没有教师消息，前端不据此渲染教师气泡
            "relay_after": after_message_id,
        },
        task_id=turn_id,
    )
    return {
        "task_run_id": turn_id,
        "session_id": session_id,
        "status": "queued",
        "duplicate": False,
    }
