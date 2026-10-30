"""查询嵌入的契约校验（与模型网关解耦，便于单测替换 Embeddings）。"""

from __future__ import annotations

import math

from langchain_core.embeddings import Embeddings

from app.adapters.retrieval.errors import RetrievalConfigurationError


def validate_vectors(raw_vectors: object, *, expected_count: int) -> list[list[float]]:
    """校验嵌入返回：数量对齐、逐维有限数、非零范数、各向量同维度。"""

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


def embed_query(embedder: Embeddings, text: str) -> list[float]:
    """单条查询嵌入；只有嵌入调用本身的失败才转成检索中止错误。"""

    try:
        raw_vector = embedder.embed_query(text)
    except RetrievalConfigurationError:
        raise
    except Exception as exc:
        raise RetrievalConfigurationError("嵌入服务调用失败，检索已中止") from exc
    return validate_vectors([raw_vector], expected_count=1)[0]


def embed_queries(embedder: Embeddings, texts: list[str]) -> list[list[float]]:
    """多查询批量嵌入（顺序与入参一致）。"""

    if not texts:
        return []
    try:
        raw_vectors = embedder.embed_queries(texts)
    except RetrievalConfigurationError:
        raise
    except Exception as exc:
        raise RetrievalConfigurationError("嵌入服务调用失败，检索已中止") from exc
    return validate_vectors(raw_vectors, expected_count=len(texts))
