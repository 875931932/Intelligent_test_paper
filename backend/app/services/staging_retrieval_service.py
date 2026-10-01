"""Strict hybrid retrieval over explicitly supplied staging chunks."""

from __future__ import annotations

import math
import re
from hashlib import sha256
from numbers import Real
from typing import Callable, Protocol

from pydantic import BaseModel

from app.domain.framework.exam_points import ExamPoint
from app.domain.knowledge.relevance import StagingChunk


class RankedChunk(BaseModel):
    chunk: StagingChunk
    score: float
    lexical_score: float
    semantic_score: float


class EmbeddingClient(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class RetrievalConfigurationError(RuntimeError):
    """Raised when retrieval cannot safely rank the supplied staging snapshot."""


MINIMUM_SEMANTIC_SCORE = 0.0


def _validate_configuration(*, top_k: int, minimum_score: float) -> None:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        raise RetrievalConfigurationError("top_k 必须是正整数")
    if (
        isinstance(minimum_score, bool)
        or not isinstance(minimum_score, Real)
        or not math.isfinite(float(minimum_score))
        or not 0 <= float(minimum_score) <= 1
    ):
        raise RetrievalConfigurationError("minimum_score 必须是 0 到 1 之间的有限数值")


def _lexical_tokens(text: str) -> set[str]:
    tokens = {match.group(0).lower() for match in re.finditer(r"[A-Za-z0-9_]+", text)}
    for sequence in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", text):
        for width in (2, 3):
            tokens.update(
                sequence[index : index + width]
                for index in range(len(sequence) - width + 1)
            )
    return tokens


def lexical_overlap(left: str, right: str) -> float:
    """Return language-agnostic Jaccard overlap for Chinese n-grams and ASCII tokens."""

    left_tokens = _lexical_tokens(left)
    right_tokens = _lexical_tokens(right)
    union = left_tokens | right_tokens
    if not union:
        return 0.0
    return len(left_tokens & right_tokens) / len(union)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    left_norm = math.hypot(*left)
    right_norm = math.hypot(*right)
    similarity = math.fsum(
        (a / left_norm) * (b / right_norm) for a, b in zip(left, right, strict=True)
    )
    return max(-1.0, min(1.0, similarity))


def _validated_vectors(
    raw_vectors: object, *, expected_count: int
) -> list[list[float]]:
    if not isinstance(raw_vectors, list) or len(raw_vectors) != expected_count:
        raise RetrievalConfigurationError("嵌入向量数量与检索文本数量不一致")

    vectors: list[list[float]] = []
    expected_dimension: int | None = None
    for raw_vector in raw_vectors:
        if not isinstance(raw_vector, list) or not raw_vector:
            raise RetrievalConfigurationError("嵌入服务返回了空向量")
        try:
            vector = [float(value) for value in raw_vector]
        except (TypeError, ValueError) as exc:
            raise RetrievalConfigurationError("嵌入向量必须只包含数值") from exc
        if not all(math.isfinite(value) for value in vector):
            raise RetrievalConfigurationError("嵌入向量必须只包含有限数值")
        norm = math.hypot(*vector)
        if norm == 0:
            raise RetrievalConfigurationError("嵌入向量不能是零范数向量")
        if not math.isfinite(norm):
            raise RetrievalConfigurationError("嵌入向量必须具有有限范数")
        if expected_dimension is None:
            expected_dimension = len(vector)
        elif len(vector) != expected_dimension:
            raise RetrievalConfigurationError("嵌入向量维度不一致")
        vectors.append(vector)
    return vectors


def _chunk_content_hash(chunk: StagingChunk) -> str:
    """排序决胜键：优先 content_hash，退化为内容哈希。"""

    if getattr(chunk, "content_hash", None):
        return str(chunk.content_hash)
    return sha256(chunk.content.encode()).hexdigest()


def _dedup_key(text: str) -> str:
    """同文折叠键：忽略空白与表单下划线串，跨文档模板碎片（表头/填空线）归一。"""

    return re.sub(r"[\s_＿—－\-]+", "", text)


def _merge_ranked_groups(
    ranked_groups: list[list[RankedChunk]],
    *,
    top_k: int,
    key: Callable[[RankedChunk], str],
) -> list[RankedChunk]:
    """多查询结果合并：按 key 归并同项取最高分，确定性排序后截断 top_k。"""

    by_key: dict[str, RankedChunk] = {}
    for ranked in ranked_groups:
        for item in ranked:
            k = key(item)
            existing = by_key.get(k)
            if existing is None or item.score > existing.score:
                by_key[k] = item
    merged = sorted(
        by_key.values(),
        key=lambda item: (-item.score, _chunk_content_hash(item.chunk), item.chunk.id),
    )
    return merged[:top_k]


def retrieve_for_exam_point(
    point: ExamPoint,
    chunks: list[StagingChunk],
    embedder: EmbeddingClient,
    *,
    top_k: int,
    minimum_score: float,
    query_vector: list[float] | None = None,
) -> list[RankedChunk]:
    """Rank the supplied snapshot using its persisted chunk embeddings."""

    _validate_configuration(top_k=top_k, minimum_score=minimum_score)
    if not chunks:
        return []

    if query_vector is None:
        try:
            raw_query_vectors = embedder.embed([point.retrieval_intent])
        except RetrievalConfigurationError:
            raise
        except Exception as exc:
            raise RetrievalConfigurationError("嵌入服务调用失败，暂存检索已中止") from exc
        query_vector = _validated_vectors(raw_query_vectors, expected_count=1)[0]
    return _rank_hybrid(
        intent=point.retrieval_intent,
        chunks=chunks,
        query_vector=query_vector,
        top_k=top_k,
        minimum_score=minimum_score,
    )


def retrieve_for_question(
    question: str,
    chunks: list[StagingChunk],
    embedder: EmbeddingClient | None,
    *,
    top_k: int,
    minimum_score: float,
    query_vector: list[float] | None = None,
    semantic_scores: dict[str, float] | None = None,
) -> list[RankedChunk]:
    """资料内容问答（助手 RAG）的混合检索：与 exam point 同款打分/量化/决胜，入参为裸问题串。

    semantic_scores 给定（SQL 下推预计算）→ 不调嵌入、不带查询向量直接打分；
    缺省维持原路径（嵌入查询向量 + 库内块向量）。
    """

    _validate_configuration(top_k=top_k, minimum_score=minimum_score)
    if not chunks:
        return []

    if query_vector is None and semantic_scores is None:
        try:
            raw_query_vectors = embedder.embed([question])
        except RetrievalConfigurationError:
            raise
        except Exception as exc:
            raise RetrievalConfigurationError("嵌入服务调用失败，检索已中止") from exc
        query_vector = _validated_vectors(raw_query_vectors, expected_count=1)[0]
    return _rank_hybrid(
        intent=question,
        chunks=chunks,
        query_vector=query_vector,
        top_k=top_k,
        minimum_score=minimum_score,
        semantic_scores=semantic_scores,
    )


def retrieve_multi_for_question(
    question_variants: list[str],
    chunks: list[StagingChunk],
    embedder: EmbeddingClient | None,
    *,
    top_k: int,
    minimum_score: float,
    semantic_scores: list[dict[str, float]] | None = None,
) -> list[RankedChunk]:
    """多查询资料问答检索：变体一次批量嵌入，各查询独立打分后同文折叠合并。

    变体为确定性改写（通常 [原问题, 去问句框架的主题串]），词面/语义各按自身
    文本打分；合并期按归一文本折叠近重复——跨文档模板碎片（表头「教学班：____」
    之类，同一课程里可有十几份同文副本）只保留得分最高的一份，正文块才可能
    进 top_k。单变体直接走单查询路径（行为与既有完全一致）。

    semantic_scores 给定（SQL 下推预计算，按去重后变体对齐）→ 跳过批量嵌入，
    直接用预计算语义分打分；缺省维持原路径（批量嵌入 + 库内块向量）。
    """

    _validate_configuration(top_k=top_k, minimum_score=minimum_score)
    if not chunks:
        return []
    variants = [v for v in dict.fromkeys(question_variants) if v.strip()]
    if not variants:
        return []
    if semantic_scores is not None and len(semantic_scores) != len(variants):
        raise RetrievalConfigurationError("预计算语义分与查询变体数量不一致")
    if len(variants) == 1:
        return retrieve_for_question(
            variants[0],
            chunks,
            embedder,
            top_k=top_k,
            minimum_score=minimum_score,
            semantic_scores=semantic_scores[0] if semantic_scores else None,
        )
    if semantic_scores is None:
        try:
            raw_vectors = embedder.embed(variants)
        except RetrievalConfigurationError:
            raise
        except Exception as exc:
            raise RetrievalConfigurationError("嵌入服务调用失败，检索已中止") from exc
        query_vectors = _validated_vectors(raw_vectors, expected_count=len(variants))
        score_maps: list[dict[str, float] | None] = [None] * len(variants)
    else:
        query_vectors = [None] * len(variants)
        score_maps = list(semantic_scores)
    ranked_groups = [
        retrieve_for_question(
            variant,
            chunks,
            embedder,
            top_k=len(chunks),
            minimum_score=minimum_score,
            query_vector=vector,
            semantic_scores=scores,
        )
        for variant, vector, scores in zip(variants, query_vectors, score_maps)
    ]
    return _merge_ranked_groups(
        ranked_groups, top_k=top_k, key=lambda item: _dedup_key(item.chunk.content)
    )


def lexical_rank_for_question(
    question: str,
    chunks: list[StagingChunk],
    *,
    top_k: int,
    minimum_score: float,
) -> list[RankedChunk]:
    """纯词面降级：嵌入不可用（未配置/调用失败/向量缺失）时按词面单独打分。

    semantic=0、score=词面分，同款 3 位量化与 content_hash 决胜，排序依旧确定。
    """

    _validate_configuration(top_k=top_k, minimum_score=minimum_score)
    if not chunks:
        return []
    ranked: list[RankedChunk] = []
    for chunk in chunks:
        lexical_score = lexical_overlap(question, chunk.content)
        if lexical_score >= float(minimum_score):
            ranked.append(
                RankedChunk(
                    chunk=chunk,
                    score=lexical_score,
                    lexical_score=lexical_score,
                    semantic_score=0.0,
                )
            )
    for item in ranked:
        item.score = round(item.score, 3)
    ranked.sort(
        key=lambda item: (
            -item.score,
            _chunk_content_hash(item.chunk),
            item.chunk.id,
        )
    )
    return ranked[:top_k]


def _rank_hybrid(
    *,
    intent: str,
    chunks: list[StagingChunk],
    query_vector: list[float] | None = None,
    top_k: int,
    minimum_score: float,
    semantic_scores: dict[str, float] | None = None,
) -> list[RankedChunk]:
    """0.35 词面 + 0.65 语义混合打分。

    semantic_scores 给定（SQL 下推预计算的 {block_id: cosine}，向量不出库）→
    直接取分，不再校验库内向量；缺省 → 校验全部块向量后 Python cosine
    （维度不齐/零范数直接抛配置错误）。两条路径的打分/量化/决胜完全同款。
    """

    if semantic_scores is None:
        if query_vector is None:
            raise RetrievalConfigurationError("缺少查询向量或预计算语义分")
        vectors = _validated_vectors(
            [query_vector, *(chunk.embedding for chunk in chunks)],
            expected_count=len(chunks) + 1,
        )
        query_vector, chunk_vectors = vectors[0], vectors[1:]
    else:
        chunk_vectors = None

    ranked: list[RankedChunk] = []
    for index, chunk in enumerate(chunks):
        lexical_score = lexical_overlap(intent, chunk.content)
        if semantic_scores is not None:
            semantic_score = semantic_scores[chunk.id]
        else:
            semantic_score = cosine_similarity(query_vector, chunk_vectors[index])
        if semantic_score <= MINIMUM_SEMANTIC_SCORE:
            continue
        score = 0.35 * lexical_score + 0.65 * semantic_score
        if score >= float(minimum_score):
            ranked.append(
                RankedChunk(
                    chunk=chunk,
                    score=score,
                    lexical_score=lexical_score,
                    semantic_score=semantic_score,
                )
            )

    # 分数量化到 3 位小数：嵌入 API 对同文本返回的向量存在尾级浮点微扰，
    # 若直接按 float 排序，截断边界附近的 chunk 顺序会在 run 间抖动，导致
    # 分类 prompt 变化、响应缓存永不命中。量化后配合 content_hash 决胜，
    # 同一资料快照的召回集合与顺序完全确定。
    for item in ranked:
        item.score = round(item.score, 3)
    ranked.sort(
        key=lambda item: (
            -item.score,
            _chunk_content_hash(item.chunk),
            item.chunk.id,
        )
    )
    return ranked[:top_k]


class HybridStagingRetriever:
    def __init__(
        self,
        *,
        embedder: EmbeddingClient,
        top_k: int,
        minimum_score: float,
    ) -> None:
        _validate_configuration(top_k=top_k, minimum_score=minimum_score)
        self.embedder = embedder
        self.top_k = top_k
        self.minimum_score = float(minimum_score)

    def embed_query(self, exam_point: ExamPoint) -> list[float]:
        try:
            raw_vectors = self.embedder.embed([exam_point.retrieval_intent])
        except RetrievalConfigurationError:
            raise
        except Exception as exc:
            raise RetrievalConfigurationError("嵌入服务调用失败，暂存检索已中止") from exc
        return _validated_vectors(raw_vectors, expected_count=1)[0]

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            raw_vectors = self.embedder.embed(texts)
        except RetrievalConfigurationError:
            raise
        except Exception as exc:
            raise RetrievalConfigurationError("嵌入服务调用失败，暂存检索已中止") from exc
        return _validated_vectors(raw_vectors, expected_count=len(texts))

    def retrieve(
        self,
        exam_point: ExamPoint,
        chunks: list[StagingChunk],
        *,
        query_vector: list[float] | None = None,
    ) -> list[RankedChunk]:
        return retrieve_for_exam_point(
            exam_point,
            chunks,
            self.embedder,
            top_k=self.top_k,
            minimum_score=self.minimum_score,
            query_vector=query_vector,
        )

    def retrieve_multi(
        self,
        exam_point: ExamPoint,
        chunks: list[StagingChunk],
        *,
        query_vectors: list[list[float]],
    ) -> list[RankedChunk]:
        """多查询混合检索：各查询独立打分，按 chunk 取最高分合并去重后截断 top_k。

        每个查询先过 minimum_score 过滤，再并集合并，保证任一查询能召回的块
        都进入候选，避免长查询词法分数被稀释而整体落选。
        """
        if not query_vectors:
            return []
        ranked_groups = [
            retrieve_for_exam_point(
                exam_point,
                chunks,
                self.embedder,
                top_k=len(chunks),
                minimum_score=self.minimum_score,
                query_vector=query_vector,
            )
            for query_vector in query_vectors
        ]
        return _merge_ranked_groups(
            ranked_groups, top_k=self.top_k, key=lambda item: item.chunk.id
        )
