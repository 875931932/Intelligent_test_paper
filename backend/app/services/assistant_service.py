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
- prompt 不含出题比例/难度/去重规则（助手职责不涉及出题约束，禁止入 prompt）；
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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.schema import (
    Course,
    assistant_messages,
    assistant_sessions,
    blueprint_versions,
    exam_projects,
    framework_versions,
    knowledge_catalog_versions,
    paper_items,
    paper_versions,
    plan_items,
    task_runs,
)
from app.domain.knowledge.relevance import StagingChunk
from app.domain.model_calls import ModelCallContext
from app.infrastructure.tasks.models import TERMINAL_TASK_STATUSES, create_task_run
from app.services import (
    exam_project_service,
    framework_service,
    material_service,
    parse_service,
)
# 复用既有判定（模块级 import 优于复制第二份）
from app.services.ai_revise_service import llm_configured
# RAG 检索与语料索引（助手 v2 资料内容问答）
from app.services.content_index_service import build_embedder, embedding_configured, ensure_embedded, load_content_chunks
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

# 只读工具：后端确定性执行查询，结果卡不依赖模型文本
READ_TOOLS = (
    "course_overview",
    "list_materials",
    "framework_status",
    "blueprint_status",
    "contract_status",
    "paper_status",
    "list_exam_projects",
)
# 提案工具：只组装提案卡（执行契约在 payload），确认由前端调既有业务 API
PROPOSAL_TOOLS = (
    "create_course",
    "update_course",
    "start_parse",
    "enqueue_blueprint_suggest",
    "confirm_contract",
)
# 双保险：即使模型给出这些 tool，路由层也按确定性文案拒绝（prompt 另有指示）
REFUSED_TOOLS = frozenset(
    {
        "delete_material",
        "remove_material",
        "confirm_blueprint",
        "finalize_paper",
        "export_paper",
        "start_generation",
        "read_material_content",  # 全文照抄/朗读仍拒绝；问答与总结走 RAG_TOOL（v2）
        "online_exam",
        "grading",
    }
)
REFUSED_REPLY = (
    "这类操作需要你亲自到对应页面完成：出题/改题去「试卷」页，蓝图确认与试卷定稿导出"
    "是里程碑操作不代劳，删除资料去「资料库」页，在线考试与阅卷不在本系统范围内。"
)

# 资料内容问答（RAG，助手 v2）：第 4 类路由——语料检索 + 段2 流式作答 + 来源引用卡
RAG_TOOL = "answer_material_content"
_RAG_TOP_K = 6
_RAG_HYBRID_MIN_SCORE = 0.15
_RAG_LEXICAL_MIN_SCORE = 0.2
_RAG_SNIPPET_CHARS = 160        # 来源卡摘要长度
_RAG_BLOCK_PROMPT_CHARS = 1500  # 单块进段2 prompt 的上限
_RAG_FALLBACK_REPLY = "已检索到相关资料，回答见下："

# 提案卡状态只允许单向迁移（proposed → executed/dismissed）
_PROPOSAL_TRANSITIONS = {"executed", "dismissed"}


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
    row = session.execute(
        select(
            paper_versions.c.id,
            paper_versions.c.version_no,
            paper_versions.c.status,
        )
        .where(
            paper_versions.c.course_id == course_id,
            paper_versions.c.exam_project_id == project["id"],
        )
        .order_by(paper_versions.c.version_no.desc())
        .limit(1)
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
                "action": {
                    "kind": action.get("kind"),
                    "tool": action.get("tool"),
                    "status": action.get("status"),
                }
                if action
                else {},
            }
        )
    return history


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
- course_overview：课程全阶段状态概览（资料/框架/目录/蓝图/合同/试卷/项目）
- list_materials：上传资料清单与解析状态
- framework_status：命题框架与考核规则状态
- blueprint_status：蓝图题位统计（逐试卷项目）
- contract_status：合同状态（逐试卷项目）
- paper_status：试卷版本与待复核题数（逐试卷项目）
- list_exam_projects：试卷项目列表
只读工具 args 默认 {}（呈现全部）。教师**点名了某个试卷项目**时，course_overview/blueprint_status/contract_status/paper_status/list_exam_projects 必须传 args={project_id(取自 payload.ids.project_ids)}，结果卡只呈现该项目；没点名就不传。

资料内容问答工具：
- answer_material_content：基于已解析资料正文回答问题/做总结。args={material_id?}——教师点名某份资料时必须传 material_id（取自 payload.ids.material_ids）；问全课程资料时不传。仅对 snapshot.materials 中 parse_status=="ready" 的资料使用；没有已解析资料时不使用本工具，回复引导教师先到「资料库」解析。回答正文由系统按检索片段生成，你的 reply 只给一句引导（如「已检索到相关资料，回答如下：」），不要复述片段。

可用提案工具（action.args 只允许下述字段，id 必须取自 payload.ids 白名单）：
- create_course：新建课程。args={name(必填,1~200字), slug?(小写字母数字连字符), description?}
- update_course：修改当前课程。args={name?, slug?, description?}（至少一个）
- start_parse：启动某资料解析。args={material_id(取自 payload.ids.material_ids)}
- enqueue_blueprint_suggest：发起蓝图调整建议。args={project_id(取自 payload.ids.project_ids), instruction?(一句话要求)}
- confirm_contract：重新分配并确认合同。args={project_id(取自 payload.ids.project_ids)}（合同已确认冻结时不要选它）

拒绝并按标准话术回复（action 置 null，不要选任何工具）：
1. 出题、改题、新增题目 → 「题目内容的新增与修改请到『试卷』页操作（选中题目后可用 AI 修改/创建）。」
2. 蓝图确认、试卷定稿、导出 → 「这是需要你亲自确认的里程碑操作，请到『试卷』页完成。」
3. 删除资料 → 「删除资料请到『资料库』页操作。」
4. 在线考试、阅卷、评分 → 「在线考试与阅卷不在本系统范围内——本系统止于导出纸质试卷产物。」
5. 操作其它课程 → 「我只能操作当前课程空间内的数据。」
6. 要求原样输出/朗读整份资料全文 → 「全文照抄请到『资料库』页查看原文；针对资料内容的提问与总结可选用 answer_material_content 工具。」

规则：
- 需要具体数据且命中上述工具时才给 action；闲聊、询问用法、解释状态含义时 action 置 null，直接回答。
- 回复用中文，面向教师，简洁自然；查询/提案类回复 1~2 句：先给针对教师所问对象的结论，再引出卡片。
- 结果卡已结构化呈现数据：回复不要逐条复述卡内容，教师没点名的项目/资料不要罗列；状态以卡片标签为准，回复里不要自行转述另一套状态说法。
- 你给的 id 必须来自 payload.ids 白名单；不确定教师指哪份资料/项目时，action 置 null 并在回复里追问。
- 只依据 payload 中的真实数据回答，不臆造资料、项目、状态或数字。
- 不承诺任何出题比例/难度/去重的调整——这些由系统确定性算法保证，不归对话管。

只返回严格 JSON 对象：
{"reply": "给教师的回复文本", "action": {"tool": "list_materials", "args": {}}}
不需要动作时：{"reply": "...", "action": null}"""


def build_intent_prompt(
    context: dict, message: str, *, previous_error: str = ""
) -> tuple[str, dict]:
    """组装段1的 (system_prompt, payload)。纯函数，便于断言真实数据进了 prompt。"""
    payload: dict = {
        "course": {"id": context["course_id"], "name": context["course_name"]},
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
    # format 当占位符解析而炸（KeyError）。
    return _SYSTEM_PROMPT.replace("{course_name}", context["course_name"]), payload


def _normalize_intent(raw) -> dict:
    """把模型输出收敛为 {reply, action|null}；结构非法抛 AssistantError（上层重试一次）。"""
    if not isinstance(raw, dict):
        raise AssistantError("模型未返回 JSON 对象")
    reply = str(raw.get("reply") or "").strip()
    action = raw.get("action")
    if action in (None, "", {}):
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
    client, context: dict, message: str, *, call_context: ModelCallContext, previous_error: str = ""
) -> dict:
    system_prompt, payload = build_intent_prompt(context, message, previous_error=previous_error)
    raw = client.request_json(
        system_prompt=system_prompt,
        payload=payload,
        temperature=0.0,
        call_context=call_context,
    )
    return _normalize_intent(raw)


# ---------------------------------------------------------------------------
# 只读工具执行（确定性查询）
# ---------------------------------------------------------------------------


def _read_course_overview(session: Session, context: dict, *, projects: list[dict]) -> dict:
    materials = context["materials"]
    return {
        "course_name": context["course_name"],
        "materials": {
            "count": len(materials),
            "parse_status": {
                status: sum(1 for m in materials if m["parse_status"] == status)
                for status in sorted({m["parse_status"] for m in materials if m["parse_status"]})
            },
        },
        "framework": context["framework"],
        "catalog": context["catalog"],
        "projects": projects,
    }


# 逐项目粒度的只读工具：教师点名项目时按 project_id 过滤（资料/框架是课程级，无此参数）
_PROJECT_READ_TOOLS = frozenset(
    {"course_overview", "blueprint_status", "contract_status", "paper_status", "list_exam_projects"}
)


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


def execute_read_tool(
    session: Session, *, context: dict, tool: str, args: dict | None = None
) -> dict:
    # 项目定位只对逐项目工具生效；非逐项目工具收到 project_id 时忽略（粒度不变）
    projects = (
        _target_projects(context, args) if tool in _PROJECT_READ_TOOLS else context["projects"]
    )
    if tool == "course_overview":
        return _read_course_overview(session, context, projects=projects)
    if tool == "list_materials":
        return {"materials": context["materials"]}
    if tool == "framework_status":
        if context["framework"] is None:
            raise AssistantError("尚未构建命题框架")
        return {"framework": context["framework"], "catalog": context["catalog"]}
    if tool == "blueprint_status":
        return {"projects": [{"id": p["id"], "name": p["name"], "blueprint": p["blueprint"]} for p in projects]}
    if tool == "contract_status":
        return {"projects": [{"id": p["id"], "name": p["name"], "contract": p["contract"]} for p in projects]}
    if tool == "paper_status":
        return {"projects": [{"id": p["id"], "name": p["name"], "paper": p["paper"]} for p in projects]}
    if tool == "list_exam_projects":
        return {
            "projects": [
                {"id": p["id"], "name": p["name"], "status": p["status"]}
                for p in projects
            ]
        }
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
        project_id = str(args.get("project_id") or "").strip()
        if not project_id:
            # 仅一个项目时允许省略
            ids = allowed["project_ids"]
            if len(ids) == 1:
                project_id = ids[0]
            else:
                raise AssistantError("需要指定 project_id（当前课程有多个试卷项目）")
        elif project_id not in allowed["project_ids"]:
            raise AssistantError("project_id 不在当前课程项目白名单内")
        project = next((p for p in context["projects"] if p["id"] == project_id), None)
        if project is None:
            raise AssistantError("项目不存在")
        if project["contract"].get("confirmed"):
            raise AssistantError("该项目合同已确认冻结，不能重新分配（可新建试卷项目或蓝图版本）")
        return {
            "project_id": project_id,
            "project_name": project["name"],
            "body": {},
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


def _rank_rag_chunks(question: str, chunks: list[StagingChunk]) -> tuple[str, list[RankedChunk]]:
    """多查询混合检索（原问题+主题核心双变体，合并期同文折叠）；嵌入不可用
    或混合无命中 → 纯词面（确定性降级，嵌入故障不断轮）。"""

    if embedding_configured() and all(chunk.embedding is not None for chunk in chunks):
        try:
            ranked = retrieve_multi_for_question(
                _rag_query_variants(question),
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
    chunks = load_content_chunks(
        session, course_id=context["course_id"], material_ids=material_ids
    )
    if not chunks:
        raise AssistantError("该范围没有可检索的资料内容")

    mode, ranked = _rank_rag_chunks(question, chunks)
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
    "list_materials": "这是你的资料清单与解析状态：",
    "framework_status": "命题框架与考核规则状态如下：",
    "blueprint_status": "各项目蓝图统计如下：",
    "contract_status": "合同状态如下：",
    "paper_status": "试卷状态如下：",
    "list_exam_projects": "试卷项目列表如下：",
}
_DEFAULT_PROPOSAL_REPLIES = {
    "create_course": "已生成新建课程提案，确认后执行：",
    "update_course": "已生成课程信息修改提案，确认后执行：",
    "start_parse": "已生成解析启动提案，确认后执行：",
    "enqueue_blueprint_suggest": "已生成蓝图调整建议任务的发起提案，确认后执行：",
    "confirm_contract": "已生成合同重新分配提案（确认合同落库），请核对参数后执行：",
}


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
        reply = intent.get("reply") or _DEFAULT_READ_REPLIES.get(tool, "查询结果见下表：")
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
        reply = intent.get("reply") or _DEFAULT_PROPOSAL_REPLIES.get(tool, "已生成提案，确认后执行：")
        return {
            "kind": "proposal",
            "reply": reply,
            "action": {"kind": "proposal", "tool": tool, "args": args, "status": "proposed", "receipt": ""},
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
    """把流式 delta 聚批后再发布：避免每 token 一次 XADD。"""

    def __init__(self, sink: TurnEventSink, *, min_chars: int = 24, min_interval: float = 0.12) -> None:
        self.sink = sink
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
        self.sink.publish("delta", {"text": "".join(self._parts)})
        self._parts = []
        self._size = 0
        self._last = time.monotonic()


def stream_answer(
    client,
    context: dict,
    message: str,
    *,
    on_delta,
    call_context: ModelCallContext,
) -> str:
    """段2：流式生成纯问答正文。"""
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
    if action.get("kind") != "proposal" or action.get("status") != "proposed":
        raise AssistantError("只有处于 proposed 状态的提案卡可以回写")
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

    intent = parse_intent(client, context, message, call_context=call_context)
    try:
        routed = route_intent(intent, session=session, context=context, message=message)
    except AssistantError as first_error:
        # 带反馈纠错一次：把校验失败原因交回模型重新解析
        intent = parse_intent(
            client,
            context,
            message,
            call_context=call_context,
            previous_error=str(first_error),
        )
        try:
            routed = route_intent(intent, session=session, context=context, message=message)
        except AssistantError as second_error:
            routed = {
                "kind": "chat",
                "stream": False,  # 确定性失败文案，不再问模型
                "reply": f"我没能处理这个请求（{second_error}）。请换个说法，或到对应页面操作。",
            }

    stream_status = "complete"
    probe = _CancelProbe(session, course_id=course_id, task_run_id=task_run_id)
    partial: list[str] = []
    if routed["kind"] == "chat":
        if routed.get("stream"):
            buffer = _DeltaBuffer(sink)
            try:
                content = stream_answer(
                    client,
                    context,
                    message,
                    on_delta=_guarded_delta(buffer, probe, partial),
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
        else:
            # 拒绝/兜底等确定性文案：整段一次推，不再进模型改写
            content = routed["reply"]
            sink.publish("delta", {"text": content})
        action: dict = {}
    elif routed["kind"] == "rag":
        # 资料内容问答：检索片段驱动的流式正文 + 命中时来源引用卡
        buffer = _DeltaBuffer(sink)
        try:
            content = stream_rag_answer(
                client,
                context,
                message,
                retrieval=routed["retrieval"],
                on_delta=_guarded_delta(buffer, probe, partial),
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
