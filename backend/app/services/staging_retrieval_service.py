"""Strict hybrid retrieval over explicitly supplied staging chunks."""

from __future__ import annotations

import math
import re
from numbers import Real
from typing import Protocol

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
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (
        left_norm * right_norm
    )


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
        if expected_dimension is None:
            expected_dimension = len(vector)
        elif len(vector) != expected_dimension:
            raise RetrievalConfigurationError("嵌入向量维度不一致")
        vectors.append(vector)
    return vectors


def retrieve_for_exam_point(
    point: ExamPoint,
    chunks: list[StagingChunk],
    embedder: EmbeddingClient,
    *,
    top_k: int,
    minimum_score: float,
) -> list[RankedChunk]:
    """Rank only the provided staging snapshot; invalid embeddings fail closed."""

    _validate_configuration(top_k=top_k, minimum_score=minimum_score)
    if not chunks:
        return []

    texts = [point.retrieval_intent, *(chunk.content for chunk in chunks)]
    try:
        raw_vectors = embedder.embed(texts)
    except RetrievalConfigurationError:
        raise
    except Exception as exc:
        raise RetrievalConfigurationError("嵌入服务调用失败，暂存检索已中止") from exc
    vectors = _validated_vectors(raw_vectors, expected_count=len(texts))
    query_vector, chunk_vectors = vectors[0], vectors[1:]

    ranked: list[RankedChunk] = []
    for chunk, vector in zip(chunks, chunk_vectors, strict=True):
        lexical_score = lexical_overlap(point.retrieval_intent, chunk.content)
        semantic_score = cosine_similarity(query_vector, vector)
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

    ranked.sort(key=lambda item: item.score, reverse=True)
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

    def retrieve(
        self,
        exam_point: ExamPoint,
        chunks: list[StagingChunk],
    ) -> list[RankedChunk]:
        return retrieve_for_exam_point(
            exam_point,
            chunks,
            self.embedder,
            top_k=self.top_k,
            minimum_score=self.minimum_score,
        )
