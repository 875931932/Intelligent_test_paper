"""零依赖 BM25 检索器（中英混排，确定性）。

为什么不用 ``langchain_community.retrievers.BM25Retriever``：
1. 它会拖入 ``rank_bm25`` + ``langchain-community`` 两个依赖，与本项目
   "适配器层只留必要依赖"的约束冲突；
2. 中文语料仍需自定义 ``preprocess_func``（其默认按空格切分，对中文完全失效），
   所谓"标准组件"实际只剩一层薄壳。

因此这里直接继承 ``BaseRetriever`` 实现 Okapi BM25：上层拿到的仍是标准
``Retriever``（可塞进 ``EnsembleRetriever`` 式的融合链、可挂 callback、可 ``invoke``），
只是打分核是自己写的。``b=0.5``（低于经典 0.75）是为了照顾"长正文块"——
本项目的块常常是整段教材正文，过强的长度惩罚会把正文挤出召回。
"""

from __future__ import annotations

import math
from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict, PrivateAttr

from app.adapters.retrieval.tokenization import tokenize


class BM25Retriever(BaseRetriever):
    """对给定 ``documents`` 建立 BM25 索引的 Retriever。"""

    documents: list[Document]
    k: int = 4
    k1: float = 1.5
    b: float = 0.5
    id_key: str = "chunk_id"

    model_config = ConfigDict(arbitrary_types_allowed=True)

    _term_freqs: list[dict[str, int]] = PrivateAttr(default_factory=list)
    _doc_lengths: list[int] = PrivateAttr(default_factory=list)
    _idf: dict[str, float] = PrivateAttr(default_factory=dict)
    _average_length: float = PrivateAttr(default=0.0)
    _doc_ids: list[str] = PrivateAttr(default_factory=list)
    _position: dict[str, int] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        self._build_index()

    def _build_index(self) -> None:
        document_frequency: dict[str, int] = {}
        for position, document in enumerate(self.documents):
            tokens = tokenize(document.page_content)
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            self._term_freqs.append(counts)
            self._doc_lengths.append(len(tokens))
            doc_id = str(document.metadata.get(self.id_key, position))
            self._doc_ids.append(doc_id)
            self._position.setdefault(doc_id, position)
            for token in counts:
                document_frequency[token] = document_frequency.get(token, 0) + 1

        total = len(self.documents)
        self._average_length = (
            sum(self._doc_lengths) / total if total else 0.0
        )
        self._idf = {
            token: math.log(1.0 + (total - freq + 0.5) / (freq + 0.5))
            for token, freq in document_frequency.items()
        }

    def _score(self, position: int, query_tokens: list[str]) -> float:
        length = self._doc_lengths[position]
        if length == 0 or self._average_length == 0.0:
            return 0.0
        normalization = self.k1 * (
            1.0 - self.b + self.b * length / self._average_length
        )
        counts = self._term_freqs[position]
        score = 0.0
        for token in query_tokens:
            frequency = counts.get(token)
            idf = self._idf.get(token)
            if not frequency or idf is None:
                continue
            score += idf * frequency * (self.k1 + 1.0) / (frequency + normalization)
        return score

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun | None = None
    ) -> list[Document]:
        query_tokens = list(dict.fromkeys(tokenize(query)))
        if not query_tokens or not self.documents:
            return []
        scored: list[tuple[float, int]] = []
        for position in range(len(self.documents)):
            score = self._score(position, query_tokens)
            if score > 0.0:
                scored.append((score, position))
        scored.sort(key=lambda item: (-item[0], self._doc_ids[item[1]]))
        selected = scored[: self.k]
        if not selected:
            return []
        best = selected[0][0]
        documents: list[Document] = []
        for score, position in selected:
            document = self.documents[position]
            metadata = dict(document.metadata)
            metadata["lexical_score"] = score / best if best else 0.0
            metadata.setdefault("score", metadata["lexical_score"])
            documents.append(
                Document(page_content=document.page_content, metadata=metadata)
            )
        return documents
