"""LangChain ``Embeddings`` adapter over the existing embedding gateway.

只做一层转发：批量限制、响应校验和错误语义仍由
``OpenAICompatibleEmbeddingGateway`` 负责，适配层不读配置、不新建 HTTP 客户端。
"""

from __future__ import annotations

from typing import Protocol

from langchain_core.embeddings import Embeddings


class EmbeddingGateway(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class PaperPactEmbeddings(Embeddings):
    """把 PaperPact 既有嵌入网关暴露为 LangChain Embeddings 接口。"""

    def __init__(self, *, gateway: EmbeddingGateway) -> None:
        self.gateway = gateway

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self.gateway.embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self.gateway.embed([text])[0]
