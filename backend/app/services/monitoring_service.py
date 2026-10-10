"""模型调用监控聚合（设计 §6）：零改造复用 model_calls，只读聚合 + 脱敏投影。

范围纪律（设计 §6.4，管理面只读聚合红线例外）：
- 平台管理员看全局（owner_ids=None 不过滤）；
- org_admin 只看「本单位 subtree 用户拥有的课程」产生的调用（course 级 join，
  业务代码不得模仿该跨课程查询模式）。
时间纪律：调用方传入的区间已由 router 校验（≤92 天）；本模块只做绑定归一
（SQLite 存 naive UTC 字符串、PG 存 timestamptz，见 _bind_range）。
查询纪律：所有聚合直接在 model_calls 上组装（不引用子查询里的列，避免
FROM 被隐式扩成笛卡尔积）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, case, func, select
from sqlalchemy.orm import Session

from app.db.schema import Course, User, model_calls

MAX_RANGE_DAYS = 92
DEFAULT_RANGE_DAYS = 7

# group_by 白名单（设计 §6.2）；error_code 只统计失败行（错误分布语义）
_GROUP_COLUMNS = {
    "model": model_calls.c.model,
    "stage": model_calls.c.stage,
    "status": model_calls.c.status,
    "error_code": model_calls.c.error_code,
}
VALID_GROUP_BY = tuple(_GROUP_COLUMNS)
VALID_BUCKETS = ("hour", "day")

# details 脱敏白名单（设计 §10.4）：response / raw_response_snapshot 永不出后端。
_DETAIL_WHITELIST = ("attempt_count", "retry_count", "final_http_status", "validation", "message_shape")


class RangeError(ValueError):
    """时间区间不合法（from>=to 或跨度超上限）。"""


def parse_range(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    """解析 from/to：缺省 to=now、from=to-7d；naive 按 UTC 解释；跨度 ≤92 天。"""

    now = datetime.now(UTC)
    end = _aware(end or now)
    start = _aware(start) if start else end - timedelta(days=DEFAULT_RANGE_DAYS)
    if start >= end:
        raise RangeError("from 必须早于 to")
    if end - start > timedelta(days=MAX_RANGE_DAYS):
        raise RangeError(f"时间范围不得超过 {MAX_RANGE_DAYS} 天")
    return start, end


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _bind_range(session: Session, start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """绑定归一：SQLite 时间列以 naive UTC 字符串存储（字节序=时序），绑定前去 tz；
    PG 是 timestamptz，保留 aware 免受 session TimeZone 影响。"""

    if session.get_bind().dialect.name == "sqlite":
        return start.replace(tzinfo=None), end.replace(tzinfo=None)
    return start, end


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def scope_owner_ids(session: Session, org_ids: set[str] | None) -> list[str] | None:
    """org_admin → 单位内用户的 id 列表（其课程即其调用范围）；平台管理员 None=全局。"""

    if org_ids is None:
        return None
    return list(session.scalars(select(User.id).where(User.organization_id.in_(org_ids))).all())


def _conds(session: Session, start: datetime, end: datetime) -> list:
    """时间区间条件（绑定层已按时区归一）。"""

    lo, hi = _bind_range(session, start, end)
    return [model_calls.c.created_at >= lo, model_calls.c.created_at < hi]


def _scope(stmt: Select, owner_ids: list[str] | None) -> Select:
    """org_admin 范围：经 courses.owner_id 收敛到本单位产生的调用。"""

    if owner_ids is None:
        return stmt
    return stmt.join(Course, Course.id == model_calls.c.course_id).where(
        Course.owner_id.in_(owner_ids)
    )


def _price_map(session: Session) -> dict[str, tuple[float | None, float | None]]:
    """型号 → (输入单价, 输出单价) 元/百万 tokens。同名型号多供应商时后者覆盖（从简）。"""

    from app.db.schema import LlmModel

    rows = session.execute(
        select(LlmModel.model_id, LlmModel.price_input_per_mtk, LlmModel.price_output_per_mtk)
    ).all()
    return {row.model_id: (row.price_input_per_mtk, row.price_output_per_mtk) for row in rows}


def _cost(model: str, input_tokens: int, output_tokens: int, prices: dict) -> float:
    """成本估算：单价空则该型号不计价（设计 §6.2——两价齐备才计，保守口径）。"""

    pair = prices.get(model)
    if not pair or pair[0] is None or pair[1] is None:
        return 0.0
    return input_tokens / 1_000_000 * pair[0] + output_tokens / 1_000_000 * pair[1]


# ── 概览 ─────────────────────────────────────────────────────────────────────


def overview(session: Session, start: datetime, end: datetime, owner_ids: list[str] | None) -> dict:
    is_pg = session.get_bind().dialect.name == "postgresql"
    columns = [
        func.count().label("total"),
        func.coalesce(func.sum(case((model_calls.c.status == "succeeded", 1), else_=0)), 0).label("succeeded"),
        func.coalesce(func.sum(case((model_calls.c.status == "failed", 1), else_=0)), 0).label("failed"),
        func.coalesce(func.sum(model_calls.c.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(model_calls.c.output_tokens), 0).label("output_tokens"),
        func.avg(model_calls.c.duration_ms).label("avg_duration"),
    ]
    if is_pg:
        # SQLite 无分位数函数（设计 §6.2：方言分支，测试不依赖分位数）
        columns.append(func.percentile_cont(0.5).within_group(model_calls.c.duration_ms).label("p50"))
        columns.append(func.percentile_cont(0.95).within_group(model_calls.c.duration_ms).label("p95"))
    stmt = select(*columns).select_from(model_calls).where(*_conds(session, start, end))
    stmt = _scope(stmt, owner_ids)
    row = session.execute(stmt).one()

    total = int(row.total)
    succeeded = int(row.succeeded)
    input_tokens = int(row.input_tokens)
    output_tokens = int(row.output_tokens)

    # 成本按 model 分组后逐型号折算（不同型号单价不同）
    cost = 0.0
    per_model = (
        select(
            model_calls.c.model,
            func.coalesce(func.sum(model_calls.c.input_tokens), 0),
            func.coalesce(func.sum(model_calls.c.output_tokens), 0),
        )
        .select_from(model_calls)
        .where(*_conds(session, start, end))
        .group_by(model_calls.c.model)
    )
    per_model = _scope(per_model, owner_ids)
    prices = _price_map(session)
    for r in session.execute(per_model).all():
        cost += _cost(r[0], int(r[1]), int(r[2]), prices)

    return {
        "range": {"from": _iso(start), "to": _iso(end)},
        "total_calls": total,
        "succeeded": succeeded,
        "failed": int(row.failed),
        "success_rate": round(succeeded / total, 4) if total else 0.0,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_estimate": round(cost, 4),
        "avg_duration_ms": round(float(row.avg_duration)) if row.avg_duration is not None else None,
        "p50_duration_ms": round(float(row.p50)) if is_pg and row.p50 is not None else None,
        "p95_duration_ms": round(float(row.p95)) if is_pg and row.p95 is not None else None,
    }


# ── 趋势 ─────────────────────────────────────────────────────────────────────


def _bucket_expr(bucket: str, session: Session):
    if session.get_bind().dialect.name == "postgresql":
        return func.date_trunc(bucket, model_calls.c.created_at)
    fmt = "%Y-%m-%d %H:00:00" if bucket == "hour" else "%Y-%m-%d 00:00:00"
    return func.strftime(fmt, model_calls.c.created_at)


def _bucket_label(value) -> str:
    if isinstance(value, str):
        return value.replace(" ", "T")
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.replace(tzinfo=None).isoformat()


def timeseries(
    session: Session, start: datetime, end: datetime, owner_ids: list[str] | None, bucket: str
) -> list[dict]:
    expr = _bucket_expr(bucket, session)
    stmt = (
        select(
            expr.label("bucket"),
            func.count().label("calls"),
            func.coalesce(func.sum(case((model_calls.c.status == "succeeded", 1), else_=0)), 0).label("succeeded"),
            func.coalesce(func.sum(case((model_calls.c.status == "failed", 1), else_=0)), 0).label("failed"),
            func.coalesce(func.sum(model_calls.c.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(model_calls.c.output_tokens), 0).label("output_tokens"),
        )
        .select_from(model_calls)
        .where(*_conds(session, start, end))
        .group_by(expr)
        .order_by(expr)
    )
    stmt = _scope(stmt, owner_ids)
    out = []
    for row in session.execute(stmt).all():
        calls = int(row.calls)
        succeeded = int(row.succeeded)
        out.append(
            {
                "bucket": _bucket_label(row.bucket),
                "calls": calls,
                "succeeded": succeeded,
                "failed": int(row.failed),
                "success_rate": round(succeeded / calls, 4) if calls else 0.0,
                "input_tokens": int(row.input_tokens),
                "output_tokens": int(row.output_tokens),
            }
        )
    return out


# ── 分组统计 ─────────────────────────────────────────────────────────────────


def breakdown(
    session: Session, start: datetime, end: datetime, owner_ids: list[str] | None, group_by: str
) -> list[dict]:
    col = _GROUP_COLUMNS[group_by]
    conds = _conds(session, start, end)
    if group_by == "error_code":
        conds.append(model_calls.c.error_code.is_not(None))
    stmt = (
        select(
            col.label("key"),
            model_calls.c.model,
            func.count().label("calls"),
            func.coalesce(func.sum(case((model_calls.c.status == "succeeded", 1), else_=0)), 0).label("succeeded"),
            func.coalesce(func.sum(model_calls.c.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(model_calls.c.output_tokens), 0).label("output_tokens"),
            func.avg(model_calls.c.duration_ms).label("avg_duration"),
        )
        .select_from(model_calls)
        .where(*conds)
        .group_by(col, model_calls.c.model)
    )
    stmt = _scope(stmt, owner_ids)
    prices = _price_map(session)

    # 先按 (分组, model) 折算成本，再折叠回分组（avg 用加权平均保持精确）
    folded: dict[str, dict] = {}
    for row in session.execute(stmt).all():
        key = row.key or ""
        cell = folded.setdefault(
            key,
            {"calls": 0, "succeeded": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0, "_dur_sum": 0.0},
        )
        calls = int(row.calls)
        cell["calls"] += calls
        cell["succeeded"] += int(row.succeeded)
        cell["input_tokens"] += int(row.input_tokens)
        cell["output_tokens"] += int(row.output_tokens)
        cell["cost"] += _cost(row.model, int(row.input_tokens), int(row.output_tokens), prices)
        if row.avg_duration is not None:
            cell["_dur_sum"] += float(row.avg_duration) * calls

    out = []
    for key, cell in folded.items():
        calls = cell["calls"]
        out.append(
            {
                "key": key,
                "calls": calls,
                "succeeded": cell["succeeded"],
                "success_rate": round(cell["succeeded"] / calls, 4) if calls else 0.0,
                "input_tokens": cell["input_tokens"],
                "output_tokens": cell["output_tokens"],
                "cost_estimate": round(cell["cost"], 4),
                "avg_duration_ms": round(cell["_dur_sum"] / calls) if calls and cell["_dur_sum"] else None,
            }
        )
    out.sort(key=lambda item: item["calls"], reverse=True)
    return out


# ── 明细 ─────────────────────────────────────────────────────────────────────


def _call_row(row) -> dict:
    return {
        "id": row.id,
        "stage": row.stage,
        "provider": row.provider,
        "model": row.model,
        "status": row.status,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "duration_ms": row.duration_ms,
        "course_id": row.course_id,
        "created_at": _iso(row.created_at),
    }


def _filtered(
    session: Session,
    start: datetime,
    end: datetime,
    owner_ids: list[str] | None,
    *,
    model: str | None = None,
    stage: str | None = None,
    status: str | None = None,
    error_code: str | None = None,
) -> Select:
    conds = _conds(session, start, end)
    for column, value in (
        (model_calls.c.model, model),
        (model_calls.c.stage, stage),
        (model_calls.c.status, status),
        (model_calls.c.error_code, error_code),
    ):
        if value:
            conds.append(column == value)
    stmt = select(model_calls).where(*conds)
    return _scope(stmt, owner_ids)


def list_calls(
    session: Session,
    start: datetime,
    end: datetime,
    owner_ids: list[str] | None,
    *,
    page: int = 1,
    page_size: int = 20,
    model: str | None = None,
    stage: str | None = None,
    status: str | None = None,
    error_code: str | None = None,
) -> dict:
    base = _filtered(
        session,
        start,
        end,
        owner_ids,
        model=model,
        stage=stage,
        status=status,
        error_code=error_code,
    )
    total = int(session.execute(select(func.count()).select_from(base.subquery())).scalar_one())
    rows = session.execute(
        base.order_by(model_calls.c.created_at.desc(), model_calls.c.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [_call_row(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def project_details(details: dict | None) -> dict:
    """§10.4 脱敏投影：只回白名单键；error_code 改名 last_error_code；
    response / raw_response_snapshot 等敏感键永不出后端。"""

    if not isinstance(details, dict):
        return {}
    projected = {k: details[k] for k in _DETAIL_WHITELIST if k in details}
    if "error_code" in details:
        projected["last_error_code"] = details["error_code"]
    return projected


def get_call(session: Session, owner_ids: list[str] | None, call_id: str) -> dict:
    stmt = select(model_calls).where(model_calls.c.id == call_id)
    stmt = _scope(stmt, owner_ids)
    row = session.execute(stmt).first()
    if row is None:
        # 不存在与越权统一 404：不泄露他人单位的调用是否存在
        raise LookupError("调用记录不存在")
    payload = _call_row(row)
    payload["request_id"] = row.request_id
    payload["prompt_hash"] = row.prompt_hash
    payload["details"] = project_details(row.details)
    return payload
