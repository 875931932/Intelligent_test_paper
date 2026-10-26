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


def test_empty_document_batch_does_not_call_gateway():
    gateway = FakeGateway(vectors=[])

    assert PaperPactEmbeddings(gateway=gateway).embed_documents([]) == []
    assert gateway.calls == []


def test_gateway_failure_is_not_swallowed():
    class FailingGateway:
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise EmbeddingGatewayError("embedding request failed")

    adapter = PaperPactEmbeddings(gateway=FailingGateway())

    with pytest.raises(EmbeddingGatewayError):
        adapter.embed_query("query")
    with pytest.raises(EmbeddingGatewayError):
        adapter.embed_documents(["a"])
