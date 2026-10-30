r"""PaperPact RAG 链路演示：真实调用仓库代码，输出可直接引用为面试演示产物。

运行：cd backend && $env:PYTHONPATH='.'; uv run python docs/demos/rag_pipeline_demo.py
输出同时写入 C:\Users\Gnahz\AppData\Local\Temp\rag_demo_output.txt（UTF-8）

链路（与线上一致）：
    VectorStore(pgvector / JSON 下推 / 内存) ┐
                                            ├─ 加权 RRF ─ 多查询变体 ─ 邻域扩展 ─ top_k
    BM25Retriever（零依赖，中英混排）        ┘
"""

from __future__ import annotations

import io
import json
import sys

from app.adapters.document.local_text_parser import parse_local_text
from app.adapters.model.langchain_embeddings import PaperPactEmbeddings
from app.adapters.retrieval.bm25 import BM25Retriever
from app.adapters.retrieval.errors import RetrievalConfigurationError
from app.adapters.retrieval.fusion import weighted_reciprocal_rank
from app.adapters.retrieval.langchain_documents import (
    documents_to_ranked,
    staging_chunk_to_document,
)
from app.adapters.retrieval.tokenization import lexical_overlap, query_token_coverage
from app.domain.knowledge.relevance import StagingChunk
from app.services.langchain_retrieval import (
    VECTOR_WEIGHT,
    LEXICAL_WEIGHT,
    HybridRetrievalChain,
    build_assistant_retriever,
    build_memory_vector_store,
)
from app.services.staging_retrieval_service import HybridStagingRetriever

BUFFER = io.StringIO()


def emit(line: str = "") -> None:
    print(line)
    BUFFER.write(line + "\n")


def rule(title: str) -> None:
    emit()
    emit("=" * 76)
    emit(title)
    emit("=" * 76)


# ---------------------------------------------------------------------------
# 确定性向量网关：向量手工指定，便于逐项核对分数
# ---------------------------------------------------------------------------
VECTORS: dict[str, list[float]] = {
    # queries
    "教学大纲里这门课的考核方式是什么": [1.0, 0.98, 0.02, 0.0],
    "教学大纲里这门课的考核方式": [0.99, 0.99, 0.02, 0.0],
    "查询抖动B": [1.0, 0.98, 0.02, 1e-9],
    # docs
    "考核正文": [1.0, 0.95, 0.10, 0.05],
    "教材参考书": [0.90, 0.80, 0.20, 0.10],
    "教学安排": [0.30, 0.20, 0.90, 0.70],
    "表头碎片": [0.95, 0.90, 0.05, 0.05],
    "噪声": [0.05, 0.10, 0.20, 0.90],
}


class FixedGateway:
    """替身网关：记录每次批量请求的入参，返回手工指定向量。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [VECTORS[t] for t in texts]


class KeyedEmbeddings:
    """按『文本 → 向量键』映射的 Embeddings（演示用，语义等价于真实模型）。"""

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.query_calls: list[str] = []

    def embed_documents(self, texts):
        return [VECTORS[self.mapping[t]] for t in texts]

    def embed_query(self, text: str):
        self.query_calls.append(text)
        return VECTORS[self.mapping[text]]

    def embed_queries(self, texts):
        return [VECTORS[self.mapping[t]] for t in texts]


# ===========================================================================
rule("演示 1｜切块：Markdown 按标题聚合章节（heading_path 记层级）")
# ===========================================================================
MARKDOWN = """# 第一章 课程概述

本课程是计算机专业的核心课，先修课程为《线性代数》与《Python 程序设计》。

## 1.1 课程目标

掌握监督学习与无监督学习的基本方法，能够独立完成小型项目的数据建模。

## 1.2 考核方式

本课程考核方式由平时作业（30%）、期中测验（20%）与期末闭卷考试（50%）三部分组成。

# 第二章 教学安排

第二章安排 16 学时讲授监督学习，8 学时实验，2 学时复习。
"""

emit("输入：一份 Markdown 教学大纲（4 个标题、4 个段落）")
emit()
emit(f"{'#':<3}{'heading_path':<34}{'字符':<6}正文预览")
emit("-" * 76)
for block in parse_local_text(MARKDOWN.encode(), markdown=True):
    preview = block.text.replace("\n", " / ")[:32]
    emit(
        f"{block.block_index:<3}{str(block.heading_path):<34}"
        f"{len(block.text):<6}{preview}"
    )
emit()
emit("观察点：")
emit("  · 标题与其正文同块（strip_headers=False）→ 标题参与嵌入，不是只存元数据；")
emit("  · heading_path 保存完整层级，供来源卡展示与同节邻域扩展使用；")
emit("  · 只含标题的章节仍产出一块，块正文即标题行，不会被丢弃。")

LONG_SECTION = "# 长章节\n" + "本课程考核方式由平时作业、期中测验与期末考试三部分组成。" * 80
long_blocks = parse_local_text(LONG_SECTION.encode(), markdown=True)
emit()
emit(
    f"超长章节（{len(LONG_SECTION)} 字符）→ 切成 {len(long_blocks)} 块，"
    f"各块 heading_path 均为 {long_blocks[0].heading_path}，"
    f"单块字符数 {[len(b.text) for b in long_blocks]}"
)

TXT = "纯文本笔记第一行\n\n纯文本笔记第二行\n"
emit()
emit("TXT 对照（行为不变：一行一块、markdown=None、无 heading_path）：")
for block in parse_local_text(TXT.encode(), markdown=False):
    emit(f"  block_index={block.block_index} markdown={block.markdown} "
         f"heading_path={block.heading_path} text={block.text!r}")


# ===========================================================================
rule("演示 2｜词面口径：Jaccard 会把长正文挤出阈值，查询覆盖率不会")
# ===========================================================================
QUERY = "考核方式"
CANDIDATES = {
    "长正文（信息量最大）": "本课程考核方式由平时作业、期中测验与期末闭卷考试三部分组成，"
                        "缺勤超过三次取消考试资格，缓考需提前两周申请并附医院证明。",
    "短碎片（只有关键词）": "考核方式",
    "无关正文": "本课程先修课程为《线性代数》与《Python 程序设计》，实验环境为 A100 集群。",
}
emit(f"查询：{QUERY!r}")
emit()
emit(f"{'候选块':<22}{'Jaccard（并集分母）':<20}{'查询覆盖率（查询分母）':<22}")
emit("-" * 76)
for name, text in CANDIDATES.items():
    jac = lexical_overlap(QUERY, text)
    cov = query_token_coverage(QUERY, text)
    emit(f"{name:<22}{jac:<20.3f}{cov:<22.3f}")
emit()
emit("结论：分母不同决定行为——Jaccard 分母是并集，块越长分越低（长正文被系统性挤出）；")
emit("      覆盖率分母是查询集，与查询共享术语的长正文拿到真实加成。")
emit("      覆盖率现在只作 BM25 之外的降级排序口径，融合排序由 RRF 负责。")


# ===========================================================================
rule("演示 3｜LangChain 检索链：VectorStore + BM25 → 加权 RRF")
# ===========================================================================
CHUNKS = [
    StagingChunk(
        id="cb-1001", material_version_id="mv-1",
        content="本课程考核方式由平时作业（30%）、期中测验（20%）与期末闭卷考试（50%）三部分组成。",
        locator={"material_id": "m-1", "material_name": "教学大纲.md",
                 "heading_path": ["第一章 课程概述", "1.2 考核方式"]},
        embedding=VECTORS["考核正文"],
    ),
    StagingChunk(
        id="cb-1002", material_version_id="mv-1",
        content="教材：《机器学习》周志华著；参考书：《统计学习方法》李航著。考核方式细则见课程平台公告。",
        locator={"material_id": "m-1", "material_name": "教学大纲.md",
                 "heading_path": ["第一章 课程概述", "1.1 课程目标"]},
        embedding=VECTORS["教材参考书"],
    ),
    StagingChunk(
        id="cb-1003", material_version_id="mv-1",
        content="第二章安排 16 学时讲授监督学习，8 学时实验，2 学时复习。",
        locator={"material_id": "m-1", "material_name": "教学大纲.md",
                 "heading_path": ["第二章 教学安排"]},
        embedding=VECTORS["教学安排"],
    ),
    StagingChunk(
        id="cb-1007", material_version_id="mv-2",
        content="实验环境：Python 3.11 + PyTorch 2.4，显卡 A100，实验楼 3 层机房。",
        locator={"material_id": "m-2", "material_name": "实验手册.md",
                 "heading_path": ["实验环境"]},
        embedding=VECTORS["噪声"],
    ),
]

embedder = KeyedEmbeddings({
    "教学大纲里这门课的考核方式是什么": "教学大纲里这门课的考核方式是什么",
    "教学大纲里这门课的考核方式": "教学大纲里这门课的考核方式",
})

QUESTION = "教学大纲里这门课的考核方式是什么"
store = build_memory_vector_store(CHUNKS, embedder)
assert store is not None
chain = HybridRetrievalChain(
    documents=[staging_chunk_to_document(c) for c in CHUNKS],
    vector_store=store,
    top_k=6,
    score_threshold=0.15,
    embedder=embedder,
)
ranked_docs = chain.search(QUESTION)
ranked = documents_to_ranked(ranked_docs, CHUNKS)

emit(f"查询：{QUESTION!r}   top_k=6   cosine 阈值=0.15")
emit(f"通道权重：vector={VECTOR_WEIGHT} / lexical={LEXICAL_WEIGHT}   rank_constant=60")
emit()
emit(f"{'rank':<5}{'block_id':<10}{'RRF':<9}{'词面':<9}{'语义':<9}heading_path / 正文预览")
emit("-" * 76)
for index, item in enumerate(ranked, start=1):
    path = " > ".join(item.chunk.locator.get("heading_path") or []) or "(无标题)"
    emit(
        f"{index:<5}{item.chunk.id:<10}{item.score:<9.3f}"
        f"{item.lexical_score:<9.3f}{item.semantic_score:<9.3f}"
        f"{path} / {item.chunk.content[:22]}"
    )
emit()
emit(f"向量通道被 cosine 阈值剔除的块："
     f"{[c.id for c in CHUNKS if c.id not in {i.chunk.id for i in ranked}]}")

# --- 通道明细：解释 RRF 为什么只看排名 -------------------------------------
query_vector = embedder.embed_query(QUESTION)
vector_hits, lexical_hits = [], []
if store is not None:
    vector_hits = store.similarity_search_with_score_by_vector(query_vector, 8)
bm25 = BM25Retriever(
    documents=[staging_chunk_to_document(c) for c in CHUNKS],
    k=8,
)
lexical_hits = bm25.invoke(QUESTION)
emit()
emit("两个通道各自的原始分（量纲完全不同，直接相加需要逐通道归一）：")
emit(f"  向量：{[(doc.metadata['chunk_id'], round(score, 3)) for doc, score in vector_hits]}")
emit(f"  BM25：{[(doc.metadata['chunk_id'], round(doc.metadata['lexical_score'], 3)) for doc in lexical_hits]}")
fused = weighted_reciprocal_rank(
    [[doc for doc, _ in vector_hits], lexical_hits],
    weights=[VECTOR_WEIGHT, LEXICAL_WEIGHT],
    top_k=4,
    score_budget=VECTOR_WEIGHT + LEXICAL_WEIGHT,
)
emit(f"RRF 融合：{[(doc.metadata['chunk_id'], round(doc.metadata['score'], 3)) for doc in fused]}")
emit("说明：RRF = Σ weight/(rank+60)，只看排名；缺通道记 0 分，不会把单通道命中抬成满分。")


# ===========================================================================
rule("演示 3b｜确定性：查询向量加 1e-9 抖动，召回顺序不变")
# ===========================================================================
jitter_embedder = KeyedEmbeddings({QUESTION: "查询抖动B"})
jitter_chain = HybridRetrievalChain(
    documents=chain.documents,
    vector_store=store,
    top_k=6,
    score_threshold=0.15,
    embedder=jitter_embedder,
)
order_a = [doc.metadata["chunk_id"] for doc in chain.search(QUESTION)]
order_b = [doc.metadata["chunk_id"] for doc in jitter_chain.search(QUESTION)]
emit(f"原始查询向量：{order_a}")
emit(f"抖动后查询向量：{order_b}")
emit(f"顺序一致：{order_a == order_b}（排序键：-score → chunk_id，排名分不含浮点尾级）")


# ===========================================================================
rule("演示 4｜多查询变体 + 同文折叠：把模板碎片挤出 top-k")
# ===========================================================================
DUPLICATE_TEXT = "教学班：________________ 任课教师：__________ 考核方式：__________"
CORPUS = [
    *CHUNKS,
    StagingChunk(id="cb-2001", material_version_id="mv-3", content=DUPLICATE_TEXT,
                 locator={"material_id": "m-3", "material_name": "A班大纲.md"},
                 embedding=VECTORS["表头碎片"]),
    StagingChunk(id="cb-2002", material_version_id="mv-4", content=DUPLICATE_TEXT,
                 locator={"material_id": "m-4", "material_name": "B班大纲.md"},
                 embedding=VECTORS["表头碎片"]),
    StagingChunk(id="cb-2003", material_version_id="mv-5", content=DUPLICATE_TEXT,
                 locator={"material_id": "m-5", "material_name": "C班大纲.md"},
                 embedding=VECTORS["表头碎片"]),
]
corpus_docs = [staging_chunk_to_document(c) for c in CORPUS]
corpus_chain = HybridRetrievalChain(
    documents=corpus_docs,
    vector_store=build_memory_vector_store(CORPUS, embedder),
    top_k=6,
    score_threshold=0.15,
    embedder=embedder,
)
single = documents_to_ranked(corpus_chain.search(QUESTION, limit=8), CORPUS)
emit("① 单查询、不做变体：同一份表头在 3 份大纲里各出现一次，占据多个槽位")
for index, item in enumerate(single, start=1):
    emit(f"   {index}. {item.chunk.id}  RRF={item.score:.3f}  "
         f"{item.chunk.locator.get('material_name')}  {item.chunk.content[:24]}")

from app.services.assistant_service import _rag_query_variants

variants = _rag_query_variants(QUESTION)
retriever = build_assistant_retriever(
    corpus_chain, documents=corpus_docs, variant_fn=_rag_query_variants, expansion=False
)
multi_docs = retriever.invoke(QUESTION)
multi = documents_to_ranked(multi_docs, CORPUS)
emit()
emit(f"② 多查询变体 {variants} 经 MultiVariantRetriever 合并 + 同文折叠：")
for index, item in enumerate(multi, start=1):
    emit(f"   {index}. {item.chunk.id}  RRF={item.score:.3f}  "
         f"{item.chunk.locator.get('material_name')}  {item.chunk.content[:24]}")
kept = [i.chunk.id for i in multi if i.chunk.id.startswith("cb-2")]
emit()
emit(f"折叠前表头碎片占 {len([i for i in single if i.chunk.id.startswith('cb-2')])} 个槽位，"
     f"折叠后只保留 {len(kept)} 个（{kept}）")


# ===========================================================================
rule("演示 5｜邻域扩展：命中标题 → 带回同节正文（ParentDocument 的手工版）")
# ===========================================================================
HEAD = StagingChunk(
    id="md-h", material_version_id="mv-6", content="1.2 考核方式",
    locator={"material_id": "m-6", "material_name": "讲义.md",
             "heading_path": ["第一章 课程概述", "1.2 考核方式"]},
    embedding=VECTORS["考核正文"],
)
SECTION_BODY = StagingChunk(
    id="md-b", material_version_id="mv-6",
    content="本课程考核方式由平时作业（30%）、期中测验（20%）与期末闭卷考试（50%）三部分组成。"
            "缺勤超过三次取消考试资格，缓考需提前两周提交申请并附医院证明。",
    locator={"material_id": "m-6", "material_name": "讲义.md",
             "heading_path": ["第一章 课程概述", "1.2 考核方式"]},
    embedding=VECTORS["噪声"],  # 刻意给低相似度：正文往往不是语义 top-1
)
OTHER = StagingChunk(
    id="md-x", material_version_id="mv-6", content="第二章安排 16 学时讲授监督学习。",
    locator={"material_id": "m-6", "material_name": "讲义.md",
             "heading_path": ["第二章 教学安排"]},
    embedding=VECTORS["教学安排"],
)
md_corpus = [HEAD, SECTION_BODY, OTHER]
md_docs = [staging_chunk_to_document(c) for c in md_corpus]
md_chain = HybridRetrievalChain(
    documents=md_docs,
    vector_store=build_memory_vector_store(md_corpus, embedder),
    top_k=1,
    score_threshold=0.15,
    embedder=embedder,
)
plain = documents_to_ranked(md_chain.search(QUESTION), md_corpus)
expand_retriever = build_assistant_retriever(
    md_chain, documents=md_docs, variant_fn=_rag_query_variants, expansion=True
)
expanded = documents_to_ranked(expand_retriever.invoke(QUESTION), md_corpus)
emit(f"仅召回：{[i.chunk.id for i in plain]}")
emit(f"邻域扩展后：{[i.chunk.id for i in expanded]}")
emit("说明：命中标题块时，同节正文按 heading_path 前缀匹配带回（单锚点 600 字预算），")
emit("      扩展块继承锚点分数、不改变排序；md/txt 无页码场景正是靠这条链路救回正文。")


# ===========================================================================
rule("演示 6｜来源卡：检索结果 → 前端可回溯的引用结构")
# ===========================================================================
_RAG_SNIPPET_CHARS = 160
payload = [
    {
        "block_id": item.chunk.id,
        "material_id": item.chunk.locator.get("material_id"),
        "material_name": item.chunk.locator.get("material_name"),
        "page_index": item.chunk.locator.get("page_index"),
        "heading_path": [str(h) for h in list(item.chunk.locator.get("heading_path") or [])][:6],
        "snippet": (
            item.chunk.content[:_RAG_SNIPPET_CHARS] + "…"
            if len(item.chunk.content) > _RAG_SNIPPET_CHARS
            else item.chunk.content
        ),
    }
    for item in multi
]
emit(json.dumps(payload, ensure_ascii=False, indent=2))


# ===========================================================================
rule("演示 7｜考点检索（知识目录路径）：批量嵌入 + 多查询合并")
# ===========================================================================
from app.domain.framework.exam_points import (
    ExamPoint,
    OperationalDetailPolicy,
    WeightSource,
)

POINT = ExamPoint(
    code="EP-1",
    anchor_key="assessment",
    title="考核方式与成绩构成",
    assessment_requirement="能够说明课程考核方式与各部分权重",
    weight_value=10,
    weight_source=WeightSource.ASSESSMENT_SYLLABUS,
    weight_group_id="course",
    operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
    retrieval_intent="考核方式、成绩构成与比例",
)
point_texts = [POINT.title, POINT.retrieval_intent, f"{POINT.title}：{POINT.assessment_requirement}"]
# 考点查询串不在预置向量表里：统一映射到「考核方式」查询向量（演示用替身）
point_embedder = KeyedEmbeddings(
    {text: "教学大纲里这门课的考核方式是什么" for text in point_texts}
)
point_retriever = HybridStagingRetriever(embedder=point_embedder, top_k=2, minimum_score=0.25)
vectors = point_retriever.embed_queries(point_texts)
hits = point_retriever.retrieve_multi(
    POINT, CHUNKS, query_vectors=vectors, query_texts=point_texts
)
emit(f"查询集（确定性装配）：{point_texts}")
emit(f"命中：{[(i.chunk.id, i.score) for i in hits]}")
emit("观察点：三路查询一次批量嵌入 → 各查询独立 RRF → 按 chunk 取最高分合并 → top_k 封顶。")


# ===========================================================================
rule("演示 8｜批量嵌入与降级：嵌入故障不断轮")
# ===========================================================================
gateway = FixedGateway()
adapter = PaperPactEmbeddings(gateway=gateway)
vectors = adapter.embed_queries(["教学大纲里这门课的考核方式是什么", "教学大纲里这门课的考核方式"])
emit(f"embed_queries([2 条查询]) → 返回 {len(vectors)} 个向量，"
     f"网关实际调用次数 = {len(gateway.calls)}，入参 = {gateway.calls[0]}")
emit(f"embed_documents([]) → {adapter.embed_documents([])}"
     f"（空输入不触发网关，调用次数仍为 {len(gateway.calls)}）")


class BrokenEmbeddings:
    def embed_documents(self, texts):
        raise RuntimeError("connection reset by peer")

    def embed_query(self, text):
        raise RuntimeError("connection reset by peer")

    def embed_queries(self, texts):
        raise RuntimeError("connection reset by peer")


emit()
emit("① 嵌入服务报错 → 检索抛 RetrievalConfigurationError，由调用方降级（不是静默返回空）：")
try:
    broken = HybridStagingRetriever(
        embedder=BrokenEmbeddings(), top_k=2, minimum_score=0.15
    )
    broken.embed_queries([QUESTION])
except RetrievalConfigurationError as exc:
    emit(f"   RetrievalConfigurationError: {exc}")

lexical_chain = HybridRetrievalChain(
    documents=corpus_docs, vector_store=None, top_k=4, score_threshold=0.0
)
lexical = documents_to_ranked(lexical_chain.search(QUESTION), CORPUS)
emit()
emit("② 纯词面降级（BM25 通道 + 查询覆盖率预筛）：")
for index, item in enumerate(lexical, start=1):
    emit(f"   {index}. {item.chunk.id}  RRF={item.score:.3f}  {item.chunk.content[:24]}")
emit("   排序键在无命中时退化为 chunk_id，结果仍然确定性。")


# ===========================================================================
rule("演示 9｜查询改写：确定性削词，不调模型")
# ===========================================================================
for question in [
    "教学大纲里这门课的考核方式是什么",
    "总结一下第二章讲了什么",
    "实验环境的显卡型号是多少",
]:
    emit(f"{question!r}  →  {_rag_query_variants(question)}")

with open(r"C:\Users\Gnahz\AppData\Local\Temp\rag_demo_output.txt", "w", encoding="utf-8") as handle:
    handle.write(BUFFER.getvalue())
sys.stderr.write("\n[demo] 输出已写入 rag_demo_output.txt\n")
