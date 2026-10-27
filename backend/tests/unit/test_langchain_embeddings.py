"""LangChain Embeddings 适配器：批量委托、单查询、空输入和错误传播。"""

from __future__ import annotations

import pytest

from app.adapters.model.embedding_gateway import EmbeddingGatewayError
from app.adapters.model.langchain_embeddings import PaperPactEmbeddings


class FakeGateway:
    def __init__(self, vectors: list[list[float]]):
        self.vectors = vectors
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return self.vectors


def test_embed_documents_delegates_batch():
    gateway = FakeGateway(vectors=[[0.1, 0.2], [0.3, 0.4]])
    adapter = PaperPactEmbeddings(gateway=gateway)

    assert adapter.embed_documents(["a", "b"]) == [[0.1, 0.2], [0.3, 0.4]]
    assert gateway.calls == [["a", "b"]]


def test_embed_query_returns_single_vector():
    gateway = FakeGateway(vectors=[[0.1, 0.2]])
    adapter = PaperPactEmbeddings(gateway=gateway)

    assert adapter.embed_query("query") == [0.1, 0.2]
    assert gateway.calls == [["query"]]


def test_embed_queries_keeps_single_batch_request_and_input_order():
    """多查询：一次批量请求，返回顺序与入参一致（不退化成 N 次逐条往返）。"""
    gateway = FakeGateway(vectors=[[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]])
    adapter = PaperPactEmbeddings(gateway=gateway)

    assert adapter.embed_queries(["第一问", "第二问", "第三问"]) == [
        [1.0, 0.0],
        [0.0, 1.0],
        [0.5, 0.5],
    ]
    assert gateway.calls == [["第一问", "第二问", "第三问"]]


def test_embed_queries_delegates_batch_limit_to_gateway():
    """批量大小上限由网关裁决：适配层不切分、不逐条重试，错误原样上抛。"""

    class ChunkLimitGateway:
        def __init__(self):
            self.calls: list[list[str]] = []

        def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(list(texts))
            if len(texts) > 2:
                raise EmbeddingGatewayError("too many texts in one request")
            return [[0.0, 0.0] for _ in texts]

    gateway = ChunkLimitGateway()
    adapter = PaperPactEmbeddings(gateway=gateway)

    assert adapter.embed_queries(["a", "b"]) == [[0.0, 0.0], [0.0, 0.0]]
    with pytest.raises(EmbeddingGatewayError):
        adapter.embed_queries(["a", "b", "c"])
    assert gateway.calls == [["a", "b"], ["a", "b", "c"]]


def test_empty_document_batch_does_not_call_gateway():
    gateway = FakeGateway(vectors=[])

    assert PaperPactEmbeddings(gateway=gateway).embed_documents([]) == []
    assert PaperPactEmbeddings(gateway=gateway).embed_queries([]) == []
    assert gateway.calls == []


def test_embed_queries_failure_is_not_swallowed():
    class FailingGateway:
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise EmbeddingGatewayError("embedding request failed")

    with pytest.raises(EmbeddingGatewayError):
        PaperPactEmbeddings(gateway=FailingGateway()).embed_queries(["a", "b"])


def test_gateway_failure_is_not_swallowed():
    class FailingGateway:
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise EmbeddingGatewayError("embedding request failed")

    adapter = PaperPactEmbeddings(gateway=FailingGateway())

    with pytest.raises(EmbeddingGatewayError):
        adapter.embed_query("query")
    with pytest.raises(EmbeddingGatewayError):
        adapter.embed_documents(["a"])
