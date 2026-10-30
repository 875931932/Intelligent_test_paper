"""解析块（content_blocks）的向量索引与 RAG 语料装载——助手 v2 资料内容问答底座。

职责：
- ensure_embedded：把缺当前模型向量或清洗版本落后的解析块嵌入落库
 （幂等，换模型 / bump EMBEDDING_TEXT_VERSION 自动重嵌），同时写 pgvector 原生
  ``embedding_vec`` 列（有扩展时）；
- load_content_chunks：装载可检索语料（staged 资料最新版本的最新 ready run）；
- enqueue_index_task：解析转 ready 时入队 material_index 任务（transactional outbox）。

语义打分不再在这里做：已迁到 ``adapters/retrieval/vector_store.py`` 的
``PaperPactVectorStore``（pgvector / JSON 下推 / 内存三后端）。

红线：ensure_embedded 内部调嵌入 API，只允许在 Celery worker 内执行——HTTP 端点
只负责 enqueue_index_task 入队，请求线程绝不做嵌入。
"""

from __future__ import annotations

import html
import logging
import re
from typing import TYPE_CHECKING

from sqlalchemy import bindparam, or_, select, text, update
from sqlalchemy.orm import Session

from app.adapters.retrieval.vector_store import detect_vector_backend, vector_literal
from app.config import settings
from app.db.schema import content_blocks, document_parse_runs, task_runs
from app.domain.knowledge.relevance import StagingChunk
from app.infrastructure.tasks.models import create_task_run
from app.services import material_service

if TYPE_CHECKING:
    from app.adapters.model.langchain_embeddings import PaperPactEmbeddings

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


def build_embedder() -> PaperPactEmbeddings:
    """构造嵌入客户端（唯一出口：外部系统只走 adapters/）。

    对外统一是 LangChain ``Embeddings``（PaperPactEmbeddings），内部仍是既有
    ``OpenAICompatibleEmbeddingGateway``：批量限制、响应校验与错误语义不变。
    """

    from app.adapters.model.embedding_gateway import OpenAICompatibleEmbeddingGateway
    from app.adapters.model.langchain_embeddings import PaperPactEmbeddings

    return PaperPactEmbeddings(
        gateway=OpenAICompatibleEmbeddingGateway(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
            model=settings.embedding_model,
            api_format=settings.embedding_api_format,
        )
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


def ready_runs(
    session: Session, *, course_id: str, material_ids: list[str] | None = None
) -> list[dict]:
    """当前可检索语料对应的 ready 解析 run（供 VectorStore 装配 SQL 过滤条件）。"""

    return _ready_version_rows(session, course_id=course_id, material_ids=material_ids)


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
        embedder = build_embedder()
        vectors = embedder.embed_documents([content for _, content in pairs])
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
    rows = [
        {
            "block_id": block_id,
            "embedding": vector,
            "embedding_model": settings.embedding_model,
            "embedding_text_version": EMBEDDING_TEXT_VERSION,
            "embedding_vec": vector_literal(vector),
        }
        for (block_id, _), vector in zip(pairs, vectors, strict=True)
    ]
    session.execute(
        update(content_blocks)
        .where(content_blocks.c.id == bindparam("block_id"))
        .values(
            embedding=bindparam("embedding"),
            embedding_model=bindparam("embedding_model"),
            embedding_text_version=bindparam("embedding_text_version"),
        ),
        rows,
    )
    # 有 pgvector 扩展时同步写原生向量列（HNSW 索引据此走最近邻算子）；
    # 无扩展的库该列不存在，detect_vector_backend 会返回 json/memory，跳过。
    if detect_vector_backend(session) == "pgvector":
        session.execute(
            text(
                "UPDATE content_blocks "
                "SET embedding_vec = CAST(:embedding_vec AS vector) "
                "WHERE id = :block_id"
            ),
            [{"block_id": row["block_id"], "embedding_vec": row["embedding_vec"]} for row in rows],
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
    或缺失 → embedding=None；混合检索的向量通道由 ``PaperPactVectorStore`` 负责，
    它按后端（pgvector / JSON 下推 / 内存）自行取向量。

    include_embedding=False 不选向量列（embedding 恒为 None）：SQL 后端（pgvector /
    JSON 下推）在库内算分，1024 维向量 JSON 不必过网络（59MB → ~4MB 文本）。

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


def get_material_content(
    session: Session,
    *,
    course_id: str,
    material_id: str,
    block_id: str | None = None,
    offset: int = 0,
    limit: int = 30,
) -> dict:
    """资料解析内容预览：与助手 RAG 同语料同口径（load_content_chunks 直接复用）。

    两种模式：
    - ``block_id`` 定位模式：返回该块 ± 上下文窗口（助手来源卡「查看原文」），
      focus.index=null 表示旧引用已不在当前解析结果中（资料重新解析过）；
    - 分页模式：按解析顺序切片（资料库「查看」浏览整份资料）。

    未解析（无 ready run）→ 空列表：前端据实提示先解析，不是错误。
    """

    chunks = load_content_chunks(
        session, course_id=course_id, material_ids=[material_id], include_embedding=False
    )
    total = len(chunks)

    def _block(chunk) -> dict:
        return {
            "id": chunk.id,
            "page_index": chunk.locator.get("page_index"),
            "heading_path": list(chunk.locator.get("heading_path") or []),
            "content": chunk.content,
        }

    if block_id is not None:
        idx = next((i for i, c in enumerate(chunks) if c.id == block_id), None)
        if idx is None:
            return {"total": total, "offset": 0, "returned": 0, "blocks": [],
                    "focus": {"id": block_id, "index": None}}
        start = max(0, idx - 2)
        window = chunks[start: idx + 4]
        return {
            "total": total,
            "offset": start,
            "returned": len(window),
            "blocks": [_block(c) for c in window],
            "focus": {"id": block_id, "index": idx},
        }

    start = max(0, offset)
    size = min(max(1, limit), 100)
    window = chunks[start: start + size]
    return {
        "total": total,
        "offset": start,
        "returned": len(window),
        "blocks": [_block(c) for c in window],
        "focus": None,
    }


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
