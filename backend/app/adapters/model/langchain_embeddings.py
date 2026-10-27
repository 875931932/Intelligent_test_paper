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
        """入库批量嵌入（资料块 / 证据块）。"""

        if not texts:
            return []
        return self.gateway.embed(texts)

    def embed_query(self, text: str) -> list[float]:
        """单条查询嵌入。"""

        return self.gateway.embed([text])[0]

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """多条查询嵌入：一次批量请求，返回顺序与入参一致。

        LangChain 标准接口只有单条 embed_query，多查询逐条调用会让多路检索
        从一次批量请求退化成 N 次往返；本方法保留既有网关的批量语义。
        """

        if not texts:
            return []
        return self.gateway.embed(texts)
