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
    blueprint_versions,
    exam_projects,
    framework_versions,
    knowledge_catalog_versions,
    paper_items,
    paper_versions,
    plan_items,
    task_runs,
)
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
        "read_material_content",
        "answer_material_content",
        "online_exam",
        "grading",
    }
)
REFUSED_REPLY = (
    "这类操作需要你亲自到对应页面完成：出题/改题去「试卷」页，蓝图确认与试卷定稿导出"
    "是里程碑操作不代劳，删除资料去「资料库」页，在线考试与阅卷不在本系统范围内。"
)

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


def _history_rows(session: Session, course_id: str) -> list[dict]:
    rows = session.execute(
        select(
            assistant_messages.c.role,
            assistant_messages.c.content,
            assistant_messages.c.action,
        )
        .where(assistant_messages.c.course_id == course_id)
        .order_by(assistant_messages.c.created_at.desc(), assistant_messages.c.id.desc())
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


def load_turn_context(session: Session, *, course_id: str) -> dict:
    """装配一轮对话的确定性上下文：业务快照 + 白名单 + 历史。"""
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
        "history": _history_rows(session, course_id),
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

可用只读工具（action.tool 取其一，args 留 {} 即可）：
- course_overview：课程全阶段状态概览（资料/框架/目录/蓝图/合同/试卷/项目）
- list_materials：上传资料清单与解析状态
- framework_status：命题框架与考核规则状态
- blueprint_status：各试卷项目蓝图的题位统计
- contract_status：各项目合同状态
- paper_status：各试卷版本与待复核题数
- list_exam_projects：试卷项目列表

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
6. 总结/问答资料内容 → 「资料内容的问答与总结暂不支持，当前可查看资料清单与解析状态。」

规则：
- 需要具体数据且命中上述工具时才给 action；闲聊、询问用法、解释状态含义时 action 置 null，直接回答。
- 回复用中文，面向教师，简洁自然；查询/提案类回复 1~2 句引出下卡即可。
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


def _read_course_overview(session: Session, context: dict) -> dict:
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
        "projects": context["projects"],
    }


def execute_read_tool(session: Session, *, context: dict, tool: str) -> dict:
    if tool == "course_overview":
        return _read_course_overview(session, context)
    if tool == "list_materials":
        return {"materials": context["materials"]}
    if tool == "framework_status":
        if context["framework"] is None:
            raise AssistantError("尚未构建命题框架")
        return {"framework": context["framework"], "catalog": context["catalog"]}
    if tool == "blueprint_status":
        return {"projects": [{"id": p["id"], "name": p["name"], "blueprint": p["blueprint"]} for p in context["projects"]]}
    if tool == "contract_status":
        return {"projects": [{"id": p["id"], "name": p["name"], "contract": p["contract"]} for p in context["projects"]]}
    if tool == "paper_status":
        return {"projects": [{"id": p["id"], "name": p["name"], "paper": p["paper"]} for p in context["projects"]]}
    if tool == "list_exam_projects":
        return {
            "projects": [
                {"id": p["id"], "name": p["name"], "status": p["status"]}
                for p in context["projects"]
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


def route_intent(intent: dict, *, session: Session, context: dict) -> dict:
    """意图 → {kind: chat|result|proposal, reply, action?, payload?}。

    AssistantError 表示意图/参数问题（上层带反馈重试一次）；工具白名单外的
    tool 名同样按 AssistantError 走重试，最终落确定性失败文案。
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
        payload = execute_read_tool(session, context=context, tool=tool)
        reply = intent.get("reply") or _DEFAULT_READ_REPLIES.get(tool, "查询结果见下表：")
        return {
            "kind": "result",
            "reply": reply,
            "action": {"kind": "result", "tool": tool, "args": args, "status": "completed"},
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
# 段2：流式正文（仅纯问答）
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
) -> str:
    new_id = message_id or uuid4().hex
    session.execute(
        assistant_messages.insert().values(
            id=new_id,
            course_id=course_id,
            task_run_id=task_run_id,
            role=role,
            content=content,
            action=action or {},
            stream_status=stream_status,
            # 显式带微秒的时间戳：server_default 在 SQLite 无微秒，同秒的
            # user/assistant 消息会排序错乱（历史踩过同秒字符串比较坑）。
            created_at=datetime.now(timezone.utc),
        )
    )
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


def list_messages(session: Session, *, course_id: str, limit: int = 200) -> list[dict]:
    rows = session.execute(
        select(assistant_messages)
        .where(assistant_messages.c.course_id == course_id)
        .order_by(assistant_messages.c.created_at.asc(), assistant_messages.c.id.asc())
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
# 主执行
# ---------------------------------------------------------------------------


def run_turn(session: Session, *, payload: dict, client, sink: TurnEventSink) -> dict:
    """一轮对话的完整执行：意图 → 路由 → 落库 → 事件。只写 assistant_messages。"""
    course_id = str(payload["course_id"])
    task_run_id = str(payload.get("task_run_id") or "")
    message = str(payload.get("message") or "")

    # 幂等：worker 租约过期重领时，消息已落库就不重跑（防重复烧模型与重复消息）
    existing = _assistant_reply_exists(session, task_run_id)
    if existing is not None:
        sink.publish("done", {"message_id": existing["id"], "task_run_id": task_run_id})
        return {"message_id": existing["id"], "duplicate": True}

    context = load_turn_context(session, course_id=course_id)
    call_context = ModelCallContext(course_id=course_id, stage=TASK_TYPE)

    intent = parse_intent(client, context, message, call_context=call_context)
    try:
        routed = route_intent(intent, session=session, context=context)
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
            routed = route_intent(intent, session=session, context=context)
        except AssistantError as second_error:
            routed = {
                "kind": "chat",
                "stream": False,  # 确定性失败文案，不再问模型
                "reply": f"我没能处理这个请求（{second_error}）。请换个说法，或到对应页面操作。",
            }

    stream_status = "complete"
    if routed["kind"] == "chat":
        if routed.get("stream"):
            buffer = _DeltaBuffer(sink)
            try:
                content = stream_answer(
                    client, context, message, on_delta=buffer.add, call_context=call_context
                )
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

    message_id = _insert_message(
        session,
        course_id=course_id,
        task_run_id=task_run_id,
        role="assistant",
        content=content,
        action=action,
        stream_status=stream_status,
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
# 任务入队
# ---------------------------------------------------------------------------


def _task_key(course_id: str, message: str) -> str:
    return hashlib.sha256(f"turn:{course_id}:{message}".encode()).hexdigest()[:24]


def enqueue_turn(session: Session, *, course_id: str, message: str) -> dict:
    """写 user 消息并创建 assistant_turn 任务；调用方负责 commit 与 outbox 派发。

    幂等语义（对齐 enqueue_propose）：同文本的**在途**任务复用（双击/重发不
    重复烧模型），已到终态的任务换一把新键。
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

    base_key = _task_key(course_id, message)
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
    )
    return {"task_run_id": turn_id, "user_message_id": user_message_id}
