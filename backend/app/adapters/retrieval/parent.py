"""邻域扩展检索器：命中块 → 命中块 + 同资料邻近正文（ParentDocument 思路的手工实现）。

为什么不用 ``ParentDocumentRetriever``：它要求「子块 ↔ 父块」两套 id 与两套存储，
而本项目的父块恰好就是同一张 ``content_blocks`` 表的相邻行——父子关系由
``page_index`` / ``heading_path`` / 解析顺序天然给出，不需要额外的 docstore。
把扩展规则留在检索链末尾（而不是塞回打分函数）有两个好处：
1. 排序权威仍在上游融合器，扩展块只"附着"不改序；
2. 扩展参数（每命中带几块、字数预算）是纯检索策略，可独立配置与测试。

规则（沿用线上验证过的口径）：
- 有页码（PDF）：同资料、页码差 ≤1 的正文块，同页优先、长块优先；
- 无页码（md/txt）：标题锚点（正文 == heading_path 末级）→ 同节正文按解析序
  装入，单锚点 600 字/全局 1000 字预算；非标题锚点 → 前后邻块，后邻优先；
- 只带正文（≥60 字，排除"又一个标题"），同块只带一次，命中本身不重复。
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict


def _metadata(document: Document, key: str) -> Any:
    return document.metadata.get(key)


def _heading(document: Document) -> list[str]:
    return [str(value) for value in (_metadata(document, "heading_path") or [])]


def _is_heading_block(document: Document) -> bool:
    """标题块判据：正文恰好等于 heading_path 末级（解析器把标题单独成块）。"""

    heading = _heading(document)
    return bool(heading) and document.page_content.strip() == heading[-1]


class NeighborhoodExpansionRetriever(BaseRetriever):
    """在命中结果后追加同资料邻近正文块（排序不变，扩展按附着顺序追加）。"""

    retriever: BaseRetriever
    documents: list[Document]
    id_key: str = "chunk_id"
    min_chars: int = 60
    per_hit: int = 2
    max_total: int = 6
    section_per_hit_chars: int = 600
    section_max_chars: int = 1000

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun | None = None
    ) -> list[Document]:
        return self.expand(self.retriever.invoke(query))

    # ---- 扩展主体 ---------------------------------------------------------
    def expand(self, anchors: list[Document]) -> list[Document]:
        if not anchors:
            return anchors
        positions = {
            str(_metadata(document, self.id_key)): index
            for index, document in enumerate(self.documents)
        }
        picked = {self._doc_id(anchor) for anchor in anchors}

        neighbor_lists: list[tuple[Document, list[Document]]] = []
        section_lists: list[tuple[Document, list[Document]]] = []
        for anchor in anchors:
            page_index = _metadata(anchor, "page_index")
            if page_index is not None:
                neighbor_lists.append((anchor, self._page_neighbors(anchor, picked)))
                continue
            if _is_heading_block(anchor):
                section_lists.append((anchor, self._section_bodies(anchor, picked)))
            else:
                neighbor_lists.append(
                    (anchor, self._ordered_neighbors(anchor, positions, picked))
                )

        expanded: list[Document] = []
        for _round in range(self.per_hit):
            if len(expanded) >= self.max_total:
                break
            for anchor, candidates in neighbor_lists:
                if len(expanded) >= self.max_total:
                    break
                target = next(
                    (item for item in candidates if self._doc_id(item) not in picked),
                    None,
                )
                if target is None:
                    continue
                picked.add(self._doc_id(target))
                expanded.append(self._attach(anchor, target))

        budget = self.section_max_chars
        for anchor, bodies in section_lists:
            if budget <= 0:
                break
            taken = 0
            for target in bodies:
                if taken >= self.section_per_hit_chars or budget <= 0:
                    break
                doc_id = self._doc_id(target)
                if doc_id in picked:
                    continue
                picked.add(doc_id)
                expanded.append(self._attach(anchor, target))
                taken += len(target.page_content)
                budget -= len(target.page_content)
        return anchors + expanded

    # ---- 候选构造 ---------------------------------------------------------
    def _page_neighbors(self, anchor: Document, picked: set[str]) -> list[Document]:
        anchor_page = _metadata(anchor, "page_index")
        material_id = _metadata(anchor, "material_id")
        candidates = [
            document
            for document in self.documents
            if self._doc_id(document) not in picked
            and _metadata(document, "material_id") == material_id
            and _metadata(document, "page_index") is not None
            and abs(_metadata(document, "page_index") - anchor_page) <= 1
            and len(document.page_content) >= self.min_chars
        ]
        candidates.sort(
            key=lambda document: (
                abs(_metadata(document, "page_index") - anchor_page),
                -len(document.page_content),
            )
        )
        return candidates

    def _section_bodies(self, anchor: Document, picked: set[str]) -> list[Document]:
        path = _heading(anchor)
        if not path:
            return []
        material_id = _metadata(anchor, "material_id")
        level = len(path)
        anchor_id = self._doc_id(anchor)
        return [
            document
            for document in self.documents
            if self._doc_id(document) not in picked
            and _metadata(document, "material_id") == material_id
            and _heading(document)[:level] == path
            and self._doc_id(document) != anchor_id
            and len(document.page_content) >= self.min_chars
        ]

    def _ordered_neighbors(
        self, anchor: Document, positions: dict[str, int], picked: set[str]
    ) -> list[Document]:
        anchor_position = positions.get(self._doc_id(anchor))
        if anchor_position is None:
            return []
        material_id = _metadata(anchor, "material_id")
        candidates = [
            document
            for document in self.documents
            if self._doc_id(document) not in picked
            and self._doc_id(document) in positions
            and _metadata(document, "material_id") == material_id
            and len(document.page_content) >= self.min_chars
        ]
        candidates.sort(
            key=lambda document: (
                abs(positions[self._doc_id(document)] - anchor_position),
                positions[self._doc_id(document)] < anchor_position,
                -len(document.page_content),
            )
        )
        return candidates

    # ---- 工具 -------------------------------------------------------------
    def _attach(self, anchor: Document, target: Document) -> Document:
        metadata = dict(target.metadata)
        metadata["score"] = anchor.metadata.get("score", 0.0)
        metadata["expanded_from"] = self._doc_id(anchor)
        return Document(page_content=target.page_content, metadata=metadata)

    def _doc_id(self, document: Document) -> str:
        value = document.metadata.get(self.id_key)
        return str(value) if value is not None else ""
