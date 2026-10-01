"""解析块（content_blocks）的向量索引与 RAG 语料装载——助手 v2 资料内容问答底座。

职责：
- ensure_embedded：把缺当前模型向量或清洗版本落后的解析块嵌入落库
 （幂等，换模型 / bump EMBEDDING_TEXT_VERSION 自动重嵌）；
- load_content_chunks：装载可检索语料（staged 资料最新版本的最新 ready run）；
- supports_semantic_pushdown / load_semantic_scores：PG 下语义打分下推到 SQL
 （逐查询向量与库内向量在库内算 cosine，embedding JSON 不出库）；
- enqueue_index_task：解析转 ready 时入队 material_index 任务（transactional outbox）。

红线：ensure_embedded 内部调嵌入 API，只允许在 Celery worker 内执行——HTTP 端点
只负责 enqueue_index_task 入队，请求线程绝不做嵌入。
"""

from __future__ import annotations

import html
import logging
import math
import re

from sqlalchemy import bindparam, or_, select, text, update
from sqlalchemy.orm import Session

from app.config import settings
from app.db.schema import content_blocks, document_parse_runs, task_runs
from app.domain.knowledge.relevance import StagingChunk
from app.infrastructure.tasks.models import create_task_run
from app.services import material_service

logger = logging.getLogger("services.content_index")

TASK_TYPE = "material_index"
_INPUT_VERSION = "material_index_v1"

# 嵌入输入清洗版本：清洗逻辑（_embedding_text）每次变更必须 bump——版本落后于
# 常量的向量由 ensure_embedded 自动重嵌（与换模型自动重嵌同款机制）。
EMBEDDING_TEXT_VERSION = 1

_CELL_END_RE = re.compile(r"(?i)</\s*(td|th)\s*>")
_BLOCK_END_RE = re.compile(r"(?i)</\s*(tr|p|div|li|h[1-6])\s*>")
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")  # 只剥「<字母」开头的真标签，保住 a < b
_TRAIL_CELL_SEP_RE = re.compile(r"\s*\|\s*$")


def _embedding_text(content: str) -> str:
    """嵌入输入的正文提取：嵌套表格 HTML → 平铺可读文本，其余标签剥除。

    - `</td>`/`</th>` → 单元格以「 | 」分隔（表头与单元格值进入嵌入语义），
      `</tr>`/`</p>`/`</li>` → 换行保留结构；正则逐段替换天然处理任意嵌套深度；
    - 其余标签剥成空格，HTML 实体反转义，连续空白压成单空格、逐行去尾部残留分隔符；
    - 无标签纯文本恒等（仅空白规范化）。

    只用于嵌入输入：检索结果卡与生成上下文仍用原始块正文（表格 HTML 对 LLM 可读）。
    """

    text = content
    if "<" in text:
        text = _CELL_END_RE.sub(" | ", text)
        text = _BLOCK_END_RE.sub("\n", text)
        text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.split("\n")]
    lines = [_TRAIL_CELL_SEP_RE.sub("", line) for line in lines]
    return "\n".join(line for line in lines if line)


def embedding_configured() -> bool:
    """嵌入三元配置齐备与否（缺配置时检索层退化纯词面，不算错误）。"""

    return bool(
        settings.embedding_api_key.strip()
        and settings.embedding_base_url.strip()
        and settings.embedding_model.strip()
    )


def build_embedder():
    """构造嵌入网关（唯一出口：外部系统只走 adapters/）。"""

    from app.adapters.model.embedding_gateway import OpenAICompatibleEmbeddingGateway

    return OpenAICompatibleEmbeddingGateway(
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        api_format=settings.embedding_api_format,
    )


def _block_text(*, text: str | None, latex: str | None, markdown: str | None) -> str:
    """块的可检索正文：text 优先，公式/富文本块退到 latex/markdown（空文本块无检索价值）。"""

    return (text or "").strip() or (latex or "").strip() or (markdown or "").strip()


def _ready_version_rows(
    session: Session, *, course_id: str, material_ids: list[str] | None = None
) -> list[dict]:
    """staged 资料最新版本的最新 ready 解析 run（语料与索引的唯一来源）。

    最新 ready run 按 completed_at DESC、id DESC 决胜（与 knowledge_publish 的
    ready run 选取同款语义）；latest_version 复用 material_service 的版本口径。
    """

    items = material_service.list_materials(session, course_id=course_id, include_deleted=False)
    wanted = set(material_ids) if material_ids is not None else None
    version_to_material: dict[str, dict] = {}
    for item in items:
        material_id = str(item.get("id"))
        if wanted is not None and material_id not in wanted:
            continue
        version = item.get("latest_version")
        if not version:
            continue
        version_to_material[str(version["id"])] = {
            "material_id": material_id,
            "material_name": str(item.get("logical_name") or item.get("name") or ""),
        }
    if not version_to_material:
        return []
    run_rows = session.execute(
        select(
            document_parse_runs.c.id,
            document_parse_runs.c.material_version_id,
        )
        .where(
            document_parse_runs.c.course_id == course_id,
            document_parse_runs.c.material_version_id.in_(version_to_material),
            document_parse_runs.c.status == "ready",
        )
        .order_by(
            document_parse_runs.c.material_version_id,
            document_parse_runs.c.completed_at.desc(),
            document_parse_runs.c.id.desc(),
        )
    ).all()
    # 每版本只取第一个（排序后首行即最新 ready run）
    picked: dict[str, str] = {}
    for run_id, version_id in run_rows:
        picked.setdefault(str(version_id), str(run_id))
    return [
        {"run_id": run_id, "material_version_id": version_id, **version_to_material[version_id]}
        for version_id, run_id in picked.items()
    ]


def ensure_embedded(
    session: Session,
    *,
    course_id: str,
    material_ids: list[str] | None = None,
    run_ids: list[str] | None = None,
) -> int:
    """把缺当前模型向量或清洗版本落后的解析块嵌入落库（幂等），返回本次新嵌入的块数。

    - 未配置 EMBEDDING_* → 返回 0（检索层据 NULL 向量退化纯词面，不算错误）；
    - 嵌入调用失败 → 记日志返回已成数量：索引是尽力而为的派生数据，故障不打断
      整轮对话，剩余 NULL 向量由检索层判定降级；
    - 过滤条件 embedding IS NULL OR embedding_model != 当前模型 OR
      embedding_text_version < EMBEDDING_TEXT_VERSION：换模型旧向量不可比、
      清洗逻辑 bump 后旧输入的向量作废，命中即重嵌（与 evidence_chunks 同语义）。
      嵌入输入走 _embedding_text 清洗（嵌套表格 HTML → 平铺文本）。本函数自行 commit。
    """

    if not embedding_configured():
        return 0
    if run_ids is None:
        run_ids = [
            row["run_id"]
            for row in _ready_version_rows(session, course_id=course_id, material_ids=material_ids)
        ]
    if not run_ids:
        return 0
    missing = session.execute(
        select(
            content_blocks.c.id,
            content_blocks.c.text,
            content_blocks.c.latex,
            content_blocks.c.markdown,
        )
        .where(
            content_blocks.c.course_id == course_id,
            content_blocks.c.document_parse_run_id.in_(run_ids),
            or_(
                content_blocks.c.embedding.is_(None),
                content_blocks.c.embedding_model != settings.embedding_model,
                content_blocks.c.embedding_text_version.is_(None),
                content_blocks.c.embedding_text_version < EMBEDDING_TEXT_VERSION,
            ),
        )
        .order_by(content_blocks.c.document_parse_run_id, content_blocks.c.block_index)
    ).all()
    pairs = [
        (
            str(row[0]),
            _embedding_text(_block_text(text=row[1], latex=row[2], markdown=row[3])),
        )
        for row in missing
    ]
    pairs = [(block_id, content) for block_id, content in pairs if content]
    if not pairs:
        return 0

    try:
        gateway = build_embedder()
        vectors = gateway.embed([content for _, content in pairs])
    except Exception as exc:  # noqa: BLE001 — 索引故障降级词面，不上抛打断调用方
        logger.warning(
            "content_blocks 嵌入失败（course=%s，%d 块）: %s", course_id, len(pairs), exc
        )
        return 0
    if len(vectors) != len(pairs):
        logger.warning(
            "content_blocks 嵌入数量不符（course=%s，期望 %d 实际 %d），跳过落库",
            course_id,
            len(pairs),
            len(vectors),
        )
        return 0
    # 只写派生向量列，不碰教师数据；executemany 一次往返
    session.execute(
        update(content_blocks)
        .where(content_blocks.c.id == bindparam("block_id"))
        .values(
            embedding=bindparam("embedding"),
            embedding_model=bindparam("embedding_model"),
            embedding_text_version=bindparam("embedding_text_version"),
        ),
        [
            {
                "block_id": block_id,
                "embedding": vector,
                "embedding_model": settings.embedding_model,
                "embedding_text_version": EMBEDDING_TEXT_VERSION,
            }
            for (block_id, _), vector in zip(pairs, vectors, strict=True)
        ],
    )
    session.commit()
    logger.info("content_blocks 新嵌入 %d 块（course=%s）", len(pairs), course_id)
    return len(pairs)


def load_content_chunks(
    session: Session,
    *,
    course_id: str,
    material_ids: list[str] | None = None,
    include_embedding: bool = True,
) -> list[StagingChunk]:
    """装载 RAG 语料：块正文 + 定位器（资料/页/章节）+ 当前模型向量。

    向量只在与 settings.embedding_model 一致时带出（换模型旧向量不可比），不一致
    或缺失 → embedding=None，检索层据「存在 NULL 向量」判定走词面降级。

    include_embedding=False 不选向量列（embedding 恒为 None）：PG 下语义分由
    load_semantic_scores 在库内算，55MB 向量 JSON 不必过网络（59MB → ~4MB 文本）。

    结构残片（<details> 之类纯标签，_embedding_text 清洗后无正文）不进语料：
    它们不可嵌入（ensure_embedded 同判据跳过），混入会让「全部块带向量」的混合
    检索门永远为假、整轮打回词面——可召回语料与可嵌入语料必须同源。
    """

    runs = _ready_version_rows(session, course_id=course_id, material_ids=material_ids)
    if not runs:
        return []
    by_run = {row["run_id"]: row for row in runs}
    columns = [
        content_blocks.c.id,
        content_blocks.c.document_parse_run_id,
        content_blocks.c.material_version_id,
        content_blocks.c.page_index,
        content_blocks.c.heading_path,
        content_blocks.c.text,
        content_blocks.c.latex,
        content_blocks.c.markdown,
    ]
    if include_embedding:
        columns += [content_blocks.c.embedding, content_blocks.c.embedding_model]
    rows = session.execute(
        select(*columns)
        .where(
            content_blocks.c.course_id == course_id,
            content_blocks.c.document_parse_run_id.in_(by_run),
        )
        .order_by(content_blocks.c.document_parse_run_id, content_blocks.c.block_index)
    ).all()
    model = settings.embedding_model.strip()
    chunks: list[StagingChunk] = []
    for row in rows:
        content = _block_text(text=row.text, latex=row.latex, markdown=row.markdown)
        if not content or not _embedding_text(content):
            continue
        run = by_run[str(row.document_parse_run_id)]
        embedding = None
        if include_embedding and model and row.embedding and (row.embedding_model or "") == model:
            embedding = row.embedding
        chunks.append(
            StagingChunk(
                id=str(row.id),
                material_version_id=str(row.material_version_id),
                content=content,
                locator={
                    "material_id": run["material_id"],
                    "material_name": run["material_name"],
                    "page_index": row.page_index,
                    "heading_path": list(row.heading_path or []),
                },
                embedding=embedding,
            )
        )
    return chunks


def supports_semantic_pushdown(session: Session) -> bool:
    """当前库方言是否支持语义打分下推（load_semantic_scores 的 PG 专用 SQL）。"""

    return session.get_bind().dialect.name == "postgresql"


def load_semantic_scores(
    session: Session,
    *,
    course_id: str,
    material_ids: list[str] | None,
    query_vectors: list[list[float]],
) -> list[dict[str, float]] | None:
    """SQL 内算逐块语义分（cosine），embedding JSON 不出库——RAG 传输 59MB → ~4MB。

    返回按 query_vectors 对齐的 {block_id: cosine} 列表；非 PG 方言、查询向量
    非法（零范数/维度不齐/非有限）、无 ready run、无当前模型向量 → None，调用方
    降级词面（embedding 故障不断轮）。缺向量块不出现在结果里，由调用方按覆盖
    不全判定整体降级（与装载层「存在 NULL 向量走词面」同语义）。

    单次扫描出全部查询列：unnest(库向量, q0, q1, …) 成对展开，GROUP BY 一次
    算 dot/norm，模拟 6964×1024 实测 ~6s；调用方只在 worker 内调用。
    """

    if not supports_semantic_pushdown(session) or not query_vectors:
        return None
    dims = {len(vector) for vector in query_vectors}
    if len(dims) != 1:
        return None
    norm = math.hypot(*query_vectors[0])
    if not math.isfinite(norm) or norm == 0:
        return None
    runs = _ready_version_rows(session, course_id=course_id, material_ids=material_ids)
    if not runs:
        return None
    model = settings.embedding_model.strip()
    if not model:
        return None

    count = len(query_vectors)
    sim_cols = ", ".join(
        f"sum(x * y{i}) / (sqrt(sum(x * x)) * :qn{i}) AS sim{i}" for i in range(count)
    )
    unnest_args = ", ".join(
        ["e.emb", *(f"CAST(:q{i} AS float8[])" for i in range(count))]
    )
    ucols = ", ".join(["x", *(f"y{i}" for i in range(count))])
    sql = text(
        f"""
        WITH e AS (
          SELECT cb.id,
                 ARRAY(SELECT (json_array_elements_text(cb.embedding))::float8) AS emb
          FROM content_blocks cb
          WHERE cb.course_id = :course_id
            AND cb.document_parse_run_id IN :run_ids
            AND cb.embedding_model = :model
            AND cb.embedding IS NOT NULL
        )
        SELECT id, {sim_cols}
        FROM e, unnest({unnest_args}) AS u({ucols})
        GROUP BY id
        """
    ).bindparams(bindparam("run_ids", expanding=True))
    params: dict = {
        "course_id": course_id,
        "run_ids": [row["run_id"] for row in runs],
        "model": model,
    }
    for i, vector in enumerate(query_vectors):
        vector_norm = math.hypot(*vector)
        if not math.isfinite(vector_norm) or vector_norm == 0:
            return None
        params[f"q{i}"] = list(vector)
        params[f"qn{i}"] = vector_norm

    scores: list[dict[str, float]] = [{} for _ in range(count)]
    rows = session.execute(sql, params).all()
    for row in rows:
        for i in range(count):
            value = row[1 + i]
            if value is not None and math.isfinite(float(value)):
                scores[i][str(row[0])] = float(value)
    if not any(scores):
        return None
    return scores


def enqueue_index_task(session: Session, *, course_id: str, run_id: str) -> str | None:
    """解析转 ready 时入队 material_index 任务；幂等键恒为 run_id，已存在不重复入队。

    调用方负责 commit + dispatch_pending_events（与 assistant/提案同款 outbox 模式）。
    返回新建任务 id；任务已存在返回 None（无需再次派发）。
    """

    key = f"material_index:{run_id}"
    existing = session.execute(
        select(task_runs.c.id).where(
            task_runs.c.course_id == course_id,
            task_runs.c.idempotency_key == key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None
    return create_task_run(
        session,
        course_id=course_id,
        task_type=TASK_TYPE,
        idempotency_key=key,
        input_version=_INPUT_VERSION,
        payload={"course_id": course_id, "run_id": run_id},
    )
