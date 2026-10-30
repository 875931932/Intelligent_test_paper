"""PaperPact 的 LangChain ``VectorStore``：同一抽象，三种后端。

LangChain 自带 ``PGVector`` 需要 ``langchain-postgres`` + ``pgvector`` Python 包 +
数据库扩展三件套齐备；本项目要求"不新增依赖"，因此把"块向量存在哪里、cosine
怎么算"收敛进一个自带实现：

- ``pgvector``：``content_blocks.embedding_vec``（``vector(1024)`` 列 + HNSW
  ``vector_cosine_ops`` 索引），``ORDER BY embedding_vec <=> :q`` 由数据库算距离、
  走索引，向量不出库；
- ``json``：无扩展的 PostgreSQL 兜底——``json_array_elements_text`` + ``unnest``
  在库内做余弦，向量同样不出库（旧实现的等价路径，现收进 VectorStore）；
- ``memory``：SQLite 测试库与知识目录的「按考点裁剪后的几十条陈述」场景，
  Python 余弦，无需数据库能力。

约定：``similarity_search_with_score`` 返回 **距离**（``1 - cosine``，越小越近），
与 LangChain 主流 store 口径一致；``_select_relevance_score_fn`` 复用官方
``_cosine_relevance_score_fn``，把距离换算回 [0,1] 的相关度。
"""

from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Literal, Sequence

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import VectorStore
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.adapters.retrieval.embedding_contract import embed_query
from app.adapters.retrieval.errors import RetrievalConfigurationError

VectorBackend = Literal["pgvector", "json", "memory"]

_EMBEDDING_TABLE = "content_blocks"


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """余弦相似度，裁剪到 [0, 1]（本项目嵌入非负方向的近似，负值视为不相关）。"""

    left_norm = math.hypot(*left)
    right_norm = math.hypot(*right)
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    similarity = math.fsum(
        (a / left_norm) * (b / right_norm)
        for a, b in zip(left, right, strict=True)
    )
    return max(0.0, min(1.0, similarity))


def detect_vector_backend(session: Session) -> VectorBackend:
    """探测当前库能走哪条后端（同一进程内每会话一次，代价是一次系统表查询）。"""

    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return "memory"
    has_type = session.execute(
        text("SELECT 1 FROM pg_type WHERE typname = 'vector' LIMIT 1")
    ).first()
    has_column = session.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = 'embedding_vec' LIMIT 1"
        ),
        {"table": _EMBEDDING_TABLE},
    ).first()
    return "pgvector" if has_type and has_column else "json"


class PaperPactVectorStore(VectorStore):
    """把 ``content_blocks`` 的向量检索包装成标准 LangChain VectorStore。"""

    def __init__(
        self,
        *,
        embedding: Embeddings | None,
        backend: VectorBackend,
        documents_by_id: dict[str, Document],
        vectors: dict[str, list[float]] | None = None,
        session: Session | None = None,
        course_id: str | None = None,
        run_ids: Sequence[str] | None = None,
        embedding_model: str = "",
    ) -> None:
        if backend in {"pgvector", "json"} and (session is None or not course_id or not run_ids):
            raise RetrievalConfigurationError("SQL 向量后端需要 session、course_id 与 run_ids")
        if backend == "memory" and vectors is None:
            raise RetrievalConfigurationError("内存向量后端需要 vectors")
        self._embedding = embedding
        self._backend: VectorBackend = backend
        self._documents_by_id = dict(documents_by_id)
        self._vectors = dict(vectors or {})
        self._session = session
        self._course_id = course_id
        self._run_ids = tuple(run_ids or ())
        self._embedding_model = embedding_model

    # ---- VectorStore 契约 -------------------------------------------------
    @property
    def embeddings(self) -> Embeddings | None:  # type: ignore[override]
        return self._embedding

    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: list[dict] | None = None,
        *,
        ids: list[str] | None = None,
        **kwargs: Any,
    ) -> list[str]:
        """只给内存后端追加（SQL 后端由嵌入流水线批量写，不在此处逐条更新）。"""

        if self._backend != "memory":
            raise NotImplementedError("SQL 向量后端不支持 add_texts，请走嵌入流水线")
        if self._embedding is None:
            raise RetrievalConfigurationError("内存后端追加向量需要 Embeddings")
        texts = list(texts)
        vectors = self._embedding.embed_documents(texts)
        assigned: list[str] = []
        for index, (content, vector) in enumerate(zip(texts, vectors, strict=True)):
            doc_id = (ids[index] if ids else None) or f"mem-{len(self._documents_by_id)}-{index}"
            metadata = dict(metadatas[index]) if metadatas else {}
            metadata.setdefault("chunk_id", doc_id)
            self._documents_by_id[doc_id] = Document(page_content=content, metadata=metadata)
            self._vectors[doc_id] = [float(value) for value in vector]
            assigned.append(doc_id)
        return assigned

    @classmethod
    def from_texts(  # type: ignore[override]
        cls,
        texts: list[str],
        embedding: Embeddings,
        metadatas: list[dict] | None = None,
        *,
        ids: list[str] | None = None,
        **kwargs: Any,
    ) -> "PaperPactVectorStore":
        store = cls(embedding=embedding, backend="memory", documents_by_id={}, vectors={})
        store.add_texts(texts, metadatas, ids=ids)
        return store

    def similarity_search(self, query: str, k: int = 4, **kwargs: Any) -> list[Document]:
        return [doc for doc, _distance in self.similarity_search_with_score(query, k, **kwargs)]

    def similarity_search_by_vector(
        self, embedding: list[float], k: int = 4, **kwargs: Any
    ) -> list[Document]:
        return [
            doc
            for doc, _distance in self.similarity_search_with_score_by_vector(
                embedding, k, **kwargs
            )
        ]

    def similarity_search_with_score(
        self, query: str, k: int = 4, **kwargs: Any
    ) -> list[tuple[Document, float]]:
        if self._embedding is None:
            raise RetrievalConfigurationError("未配置嵌入模型，无法做向量检索")
        vector = embed_query(self._embedding, query)
        return self.similarity_search_with_score_by_vector(vector, k, **kwargs)

    def similarity_search_with_score_by_vector(
        self, embedding: Sequence[float], k: int = 4, **kwargs: Any
    ) -> list[tuple[Document, float]]:
        """按查询向量检索，返回 (Document, 距离)，``semantic_score`` 落在 metadata。"""

        if k <= 0:
            return []
        hits = self._search_ids(embedding, k)
        results: list[tuple[Document, float]] = []
        for block_id, similarity in hits:
            document = self._documents_by_id.get(block_id)
            if document is None:
                continue
            metadata = dict(document.metadata)
            metadata["semantic_score"] = similarity
            metadata.setdefault("score", similarity)
            results.append(
                (Document(page_content=document.page_content, metadata=metadata), 1.0 - similarity)
            )
        return results

    @staticmethod
    def _cosine_relevance_score_fn(distance: float) -> float:
        return 1.0 - distance

    def _select_relevance_score_fn(self) -> Callable[[float], float]:
        return self._cosine_relevance_score_fn

    # ---- 后端分派 ---------------------------------------------------------
    def _search_ids(
        self, query_vector: Sequence[float], k: int
    ) -> list[tuple[str, float]]:
        if self._backend == "memory":
            return self._search_memory(query_vector, k)
        if self._backend == "pgvector":
            return self._search_pgvector(query_vector, k)
        return self._search_json_pushdown(query_vector, k)

    def _search_memory(
        self, query_vector: Sequence[float], k: int
    ) -> list[tuple[str, float]]:
        scored: list[tuple[str, float]] = []
        for block_id, vector in self._vectors.items():
            if block_id not in self._documents_by_id:
                continue
            if len(vector) != len(query_vector):
                raise RetrievalConfigurationError("嵌入向量维度不一致")
            scored.append((block_id, cosine_similarity(query_vector, vector)))
        # 距离优先、块 id 决胜：同一快照的召回顺序在 run 间完全确定。
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:k]

    def _search_pgvector(
        self, query_vector: Sequence[float], k: int
    ) -> list[tuple[str, float]]:
        statement = text(
            f"""
            SELECT id,
                   GREATEST(0.0, LEAST(1.0, 1.0 - (embedding_vec <=> CAST(:query AS vector)))) AS similarity
            FROM {_EMBEDDING_TABLE}
            WHERE course_id = :course_id
              AND document_parse_run_id IN :run_ids
              AND embedding_model = :embedding_model
              AND embedding_vec IS NOT NULL
            ORDER BY embedding_vec <=> CAST(:query AS vector), id
            LIMIT :k
            """
        ).bindparams(bindparam("run_ids", expanding=True))
        try:
            rows = self._session.execute(  # type: ignore[union-attr]
                statement,
                {
                    "query": vector_literal(query_vector),
                    "course_id": self._course_id,
                    "run_ids": list(self._run_ids),
                    "embedding_model": self._embedding_model,
                    "k": k,
                },
            ).all()
        except Exception as exc:  # 维度不一致/扩展失效 → 上层降级词面
            raise RetrievalConfigurationError("pgvector 检索失败") from exc
        return [(str(row[0]), float(row[1])) for row in rows]

    def _search_json_pushdown(
        self, query_vector: Sequence[float], k: int
    ) -> list[tuple[str, float]]:
        """无 pgvector 扩展时的库内余弦：JSON 数组展开后与查询数组逐维求和。"""

        statement = text(
            f"""
            WITH block_vectors AS (
                SELECT id, embedding
                FROM {_EMBEDDING_TABLE}
                WHERE course_id = :course_id
                  AND document_parse_run_id IN :run_ids
                  AND embedding_model = :embedding_model
                  AND embedding IS NOT NULL
            ),
            dims AS (
                SELECT b.id AS block_id,
                       t.ordinality::int AS idx,
                       CAST(t.value AS double precision) AS x
                FROM block_vectors b
                CROSS JOIN LATERAL json_array_elements_text(b.embedding)
                     WITH ORDINALITY AS t(value, ordinality)
            )
            SELECT d.block_id,
                   COALESCE(GREATEST(0.0, LEAST(1.0,
                       SUM(d.x * q.y)
                       / NULLIF(SQRT(SUM(d.x * d.x)) * NULLIF(:query_norm, 0.0), 0.0)
                   )), 0.0) AS similarity
            FROM dims d
            JOIN unnest(CAST(:query AS double precision[]))
                 WITH ORDINALITY AS q(y, idx) ON q.idx = d.idx
            GROUP BY d.block_id
            ORDER BY similarity DESC, d.block_id
            LIMIT :k
            """
        ).bindparams(bindparam("run_ids", expanding=True))
        try:
            rows = self._session.execute(  # type: ignore[union-attr]
                statement,
                {
                    "query": list(query_vector),
                    "query_norm": math.hypot(*query_vector),
                    "course_id": self._course_id,
                    "run_ids": list(self._run_ids),
                    "embedding_model": self._embedding_model,
                    "k": k,
                },
            ).all()
        except Exception as exc:
            raise RetrievalConfigurationError("JSON 向量下推检索失败") from exc
        return [(str(row[0]), float(row[1])) for row in rows]


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector 文本字面量：``[0.1,0.2,...]``（避免额外依赖 pgvector Python 包）。"""

    return "[" + ",".join(f"{float(value):.8g}" for value in vector) + "]"
