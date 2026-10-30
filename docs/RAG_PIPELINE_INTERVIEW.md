# PaperPact RAG 链路详解（面试版）

> 本文所有代码片段均摘自仓库现文件，所有「实测输出」均来自真实运行
> （脚本见 `docs/demos/rag_pipeline_demo.py`）。
> 凡是**引用代码注释中记录的生产实测数字**，都会显式标注来源，不做二次声称。
>
> **注释约定**：代码块中凡以 `# ←` 开头的注释，都是本文为讲解补充的注解；
> 其余行（均为源码原文摘录，未做改动）。
> 每个数据结构、组件之后都会给出「逐项作用」，说明它在链路里到底承担什么。

---

## 0. 60 秒速览

| 环节 | 做了什么 | 关键设计 | 代码位置 |
|---|---|---|---|
| ① 切块 | PDF/PPT 走 MinerU；TXT/Markdown 走本地解析 | Markdown 按标题聚合章节，标题留在正文里参与嵌入 | `backend/app/adapters/document/local_text_parser.py` |
| ② 索引 | 块正文 → 向量，落 `content_blocks` | content_hash 去重缓存、embedding_model 校验、文本口径版本失效；有 pgvector 时同步写原生向量列 | `backend/app/services/content_index_service.py` |
| ③ 装载 | 按课程 + 白名单取语料 | 向量在哪算，由向量后端决定（pgvector / SQL 下推 / 内存） | 同上 `load_content_chunks()` / `ready_runs()` |
| ④ 检索 | LangChain 检索链：VectorStore + BM25 → 加权 RRF → 多查询 → 邻域扩展 | 融合只看排名（RRF），分项分数仅作观测；阈值只作用于向量通道 cosine | `backend/app/adapters/retrieval/` + `services/langchain_retrieval.py` |
| ⑤ 生成 | top-k 片段 + 来源卡 + SSE 流式 | 来源卡可回溯原文；prompt 硬约束「资料里没有找到」才准说不知道 | `backend/app/services/assistant_service.py` |

一句话定位：**这是一条标准 LangChain 形状的混合检索链，但打分/融合/降级是被自己接管、可复现的**——
组件全部继承自 `langchain_core`（`VectorStore`、`BaseRetriever`），装配在
`services/langchain_retrieval.py`，算法实现留在 `adapters/retrieval/`。

---

## 1. 定位：两个消费端，共用一套检索链

```text
知识组织阶段                     教师助手问答
考点 ExamPoint                  自然语言问题
      │                               │
      └──────────┬────────────────────┘
                 ▼
        LangChain 检索链（VectorStore + BM25 → 加权 RRF → 多查询 → 邻域扩展）
                 ▼
         RankedChunk 列表
        ├─→ 组织图：作为「证据块」喂给知识卡生成
        └─→ 助手：来源卡 + 段2 流式回答
```

装配后的链路（两个消费端同构）：

```text
query ─┬─ PaperPactVectorStore.similarity_search  ─┐
       └─ BM25Retriever                            ┴─ WeightedRRFRetriever ─ MultiVariantRetriever ─ NeighborhoodExpansionRetriever ─ top_k
```

组件分工（这才是「用了 LangChain」的具体含义）：

| 组件 | 继承/使用 | 文件 | 作用 |
|---|---|---|---|
| `Embeddings` | 实现 | `adapters/model/langchain_embeddings.py` | 入库与查询统一接口，含自定义批量 `embed_queries` |
| `VectorStore` | 继承 | `adapters/retrieval/vector_store.py` | 三后端（pgvector / SQL 下推 / 内存）统一为 `similarity_search_with_score()` |
| `BaseRetriever` | 继承 | `adapters/retrieval/bm25.py` | 零依赖 BM25 词面通道 |
| `BaseRetriever` | 继承 | `adapters/retrieval/fusion.py` | 加权 RRF 融合器（`WeightedRRFRetriever`） |
| `BaseRetriever` | 继承 | `adapters/retrieval/multi_query.py` | 多查询变体召回 + 同文折叠 |
| `BaseRetriever` | 继承 | `adapters/retrieval/parent.py` | 邻域扩展（ParentDocument 思路的手工实现） |
| 装配 | — | `services/langchain_retrieval.py` | 选后端、配权重/阈值、`Document` ↔ `StagingChunk` 互转 |
| 门面 | — | `services/staging_retrieval_service.py` | 考点检索入口 `HybridStagingRetriever`（封存工作流只依赖它） |

两个消费端的入口：

| 入口 | 入参 | 返回 | 谁调用 |
|---|---|---|---|
| `HybridStagingRetriever.retrieve` | 考点 + 语料 + 可选 `query_vector` / `query_text` | `RankedChunk[]` | 知识组织图（考点取证） |
| `HybridStagingRetriever.retrieve_multi` | 考点 + 语料 + 已批量嵌入的 `query_vectors` | `RankedChunk[]` | 知识组织图（三路查询合并） |
| `HybridStagingRetriever.embed_queries` | 查询串列表 | 向量列表 | 组织图：一次批量嵌入，避免逐查询调用 |
| `_rank_rag_chunks`（助手内部） | 问题串 + 语料 + 会话 | `("hybrid"\|"lexical", RankedChunk[])` | 助手 RAG |

`minimum_score` 现在只作用于**向量通道的 cosine**（不再是对融合总分的阈值）：
RRF 只看排名，融合分是"排名分的归一化产物"，给它设阈值没有语义。

---

## 2. 数据模型：两个贯穿全链路的载体

整条链路只传两种对象：**进检索的 `StagingChunk`** 和 **出检索的 `RankedChunk`**。

### 2.1 `StagingChunk` —— 检索的输入单位

```python
# backend/app/domain/knowledge/relevance.py:16-23
class StagingChunk(BaseModel):
    """A parsed material chunk that has not yet been published to the knowledge base."""

    id: str                                             # ← 块主键（content_blocks.id），排序决胜与来源卡都靠它
    material_version_id: str                            # ← 所属资料版本：白名单过滤的最小单位
    content: str = Field(min_length=1)                   # ← 块正文：词面打分与向量嵌入的唯一文本源
    locator: dict[str, Any] = Field(default_factory=dict) # ← 来源定位：来源卡展示 + 邻域扩展策略
    embedding: list[float] | None = None                # ← 内存后端的块向量；SQL 后端为 None（向量不出库）
```

| 字段 | 具体作用 | 谁在用 |
|---|---|---|
| `id` | 块唯一标识：排序决胜键、来源卡 `block_id`、`documents_to_ranked` 回捞键 | RRF / 来源卡 / Document 边界 |
| `material_version_id` | 标定"哪份资料的哪个版本"，解析版本白名单靠它过滤 | 装载层、组织图 |
| `content` | **同一份文本同时喂给 BM25 与嵌入**——两套口径若分开清洗，会出现"词面命中但语义没命中"的不可解释分歧 | BM25 / Embeddings |
| `locator` | `material_id`、`material_name`、`page_index`、`heading_path`。**决定来源卡内容**，也决定邻域扩展走"有页码 → 同页±1"还是"无页码 → 同节正文" | 来源卡、邻域扩展 |
| `embedding` | 只在**内存后端**（SQLite 测试库、按考点裁剪的陈述块）随对象传递；SQL 后端在库内算 cosine，向量不出库 | `build_memory_vector_store` |

### 2.2 `RankedChunk` —— 检索的输出单位

```python
# backend/app/domain/knowledge/relevance.py:26-36
class RankedChunk(BaseModel):
    chunk: StagingChunk
    score: float          # ← 归一化 RRF 融合分（0~1）：排序权威
    lexical_score: float  # ← BM25 分（已按通道内最高分归一）：观测/调参，不参与排序
    semantic_score: float # ← cosine 相似度：观测/调参，不参与排序
```

三个分数都留着是为了**可调试性**：召回异常时能立刻分辨是词面没命中（术语写法不同）
还是语义没命中（嵌入模型不解这个话题），不必重跑打日志。
排序只认 `score`（RRF），分项分只进来源卡与演示输出。

---

## 3. 环节一：切块

### 3.1 Markdown：按标题聚合章节

```python
# backend/app/adapters/document/local_text_parser.py:11-16
# Markdown 标题层级：# ~ ###### 依次映射到块元数据 h1 ~ h6
_MARKDOWN_HEADERS = [("#" * level, f"h{level}") for level in range(1, 7)]

# 过长章节的切块粒度（字符数）。1600/200 是初始试验值，按实际资料的检索效果再调。
_MARKDOWN_CHUNK_SIZE = 1600
_MARKDOWN_CHUNK_OVERLAP = 200
```

```python
# backend/app/adapters/document/local_text_parser.py:53-92
def _parse_markdown(text: str) -> list[ContentBlock]:
    """Markdown：先按标题聚合章节，再切分过长章节。

    strip_headers=False 是有意设置的——索引取块正文生成向量，标题必须留在正文里，
    否则只有 heading_path 元数据、标题却没有参与嵌入。
    """

    # 第一步：按 # ~ ###### 把文档切成「章节」，每个 section 的 metadata 带 h1..h6
    sections = MarkdownHeaderTextSplitter(
        headers_to_split_on=_MARKDOWN_HEADERS,  # ← 标题级别 → 元数据键的映射表
        return_each_line=False,                 # ← 不按行切开，整节聚成一个 Document
        strip_headers=False,                    # ← 标题保留在 page_content 里 → 参与嵌入
    ).split_text(text)

    # 第二步：只对超过 1600 字的章节做长度切分，重叠 200 字防语义被切断
    chunks = RecursiveCharacterTextSplitter(
        chunk_size=_MARKDOWN_CHUNK_SIZE,
        chunk_overlap=_MARKDOWN_CHUNK_OVERLAP,
    ).split_documents(sections)                 # ← 切分后各块继续携带章节 metadata
    ...
```

| 组件 | 在链路里的作用 | 关键参数为什么这么设 |
|---|---|---|
| `MarkdownHeaderTextSplitter` | 把长文档切成**语义章节**，并把标题层级抽进 metadata | `return_each_line=False`：默认会把内容按行打碎，章节语义就没了；`strip_headers=False`：标题要留在正文里参与嵌入 |
| `RecursiveCharacterTextSplitter` | 只对**过长的章节**做二次切分，保证单块不超过 1600 字 | `chunk_overlap=200`：防止一句话/一个论证被拦腰截断；1600/200 是试验起点，由检索效果回调 |

**`ContentBlock` 是什么**（`adapters/document/protocol.py`，`@dataclass(frozen=True)` 不可变）：
`block_index` / `block_type` / `text` / `markdown` / `heading_path` / `reading_order` / `content_hash`
本地解析全部填齐；`latex` / `page_index` / `bbox` / `asset_reference` 留空（无版面信息，由 MinerU 路径填）。

三个刻意的决定：

- **`strip_headers=False`**：标题要参与嵌入。
- **不再为每个标题单独造块**：避免"只含标题的低价值向量块"挤占检索槽位。
- **TXT 路径完全不变**：按行切块、`markdown=None`、无 `heading_path`。

### 3.2 实测输出

```text
输入：一份 Markdown 教学大纲（4 个标题、4 个段落）

#  heading_path                      字符    正文预览
----------------------------------------------------------------------------
0  ['第一章 课程概述']                      53    # 第一章 课程概述   / 本课程是计算机专业的核心课，先修课
1  ['第一章 课程概述', '1.1 课程目标']          48    ## 1.1 课程目标   / 掌握监督学习与无监督学习的基本方
2  ['第一章 课程概述', '1.2 考核方式']          59    ## 1.2 考核方式   / 本课程考核方式由平时作业（30%
3  ['第二章 教学安排']                      45    # 第二章 教学安排   / 第二章安排 16 学时讲授监督学习

超长章节（2246 字符）→ 切成 3 块，各块 heading_path 均为 ['长章节']，单块字符数 [5, 1599, 841]
```

> 已知边界：`"# 长章节\n正文"`（标题与正文之间**没有空行**）会多出一个只含标题的块
> （上面 `[5, 1599, 841]` 中的 5）。它仍是合法可检索单元，只是多占一个槽位。

---

## 4. 环节二：向量索引

`backend/app/services/content_index_service.py` 里的三个防坑设计，全部来自生产教训：

```python
# backend/app/services/content_index_service.py:39-46
TASK_TYPE = "material_index"        # ← 任务类型标识：写入 task_run，用于后台监控按用途归类
_INPUT_VERSION = "material_index_v1" # ← 该任务的输入口径版本：口径变了就 bump，旧 task_run 不被复用
# 嵌入输入清洗版本：清洗逻辑（_embedding_text）每次变更必须 bump——版本落后于
# 常量的向量由 ensure_embedded 自动重嵌（与换模型自动重嵌同款机制）。
EMBEDDING_TEXT_VERSION = 1
```

| 机制 | 做法 | 不这么做会怎样 |
|---|---|---|
| `content_hash` 去重缓存 | 同 `content_hash` 的文本直接复用已算向量 | 同一课程里十几份同文副本 → 重复调用嵌入 API |
| `embedding_model` 列校验 | 复用缓存时必须 `embedding_model == settings.embedding_model` | 换模型后即使维度相同，新旧向量也互不可比，旧块永久隐身 |
| `EMBEDDING_TEXT_VERSION` | 文本清洗口径变了就升版本，按版本失效重嵌 | 改了口径但复用旧向量 → 检索分数与文本不一致 |

**清洗环节**：嵌入输入不是原始块正文，而是 `_embedding_text(content)`——
MinerU 对表格产出 HTML，`<td>` 本身无语义，但单元格里的表头与数值有，
所以要把标签翻译成可读分隔符而不是直接删掉（`</td>` → `" | "`，`</tr>`/`</p>` → 换行，
`<字母` 开头的才当标签剥，保住 `a < b`）。**只用于嵌入输入**，来源卡与生成上下文仍用原始正文。

**入库是幂等的**：

```python
# backend/app/services/content_index_service.py:ensure_embedded
def ensure_embedded(
    session: Session,
    *,
    course_id: str,                        # ← 多租户隔离
    material_ids: list[str] | None = None,
    run_ids: list[str] | None = None,
) -> int:
    """把缺当前模型向量或清洗版本落后的解析块嵌入落库（幂等），返回本次新嵌入的块数。

    - 未配置 EMBEDDING_* → 返回 0（检索层据 NULL 向量退化词面，不算错误）；
    - 嵌入调用失败 → 记日志返回已成数量：向量是派生数据，故障不打断整轮对话；
    - 过滤条件 embedding IS NULL OR embedding_model != 当前模型 OR
      embedding_text_version < EMBEDDING_TEXT_VERSION。
    """
```

写入时如果检测到 pgvector 后端，**同时**写原生向量列（下一节）：

```python
# backend/app/services/content_index_service.py（写库片段）
rows = [
    {"block_id": ..., "embedding": vector, ..., "embedding_vec": vector_literal(vector)}
    for ...
]
session.execute(update(content_blocks)..., rows)
if detect_vector_backend(session) == "pgvector":
    session.execute(
        text("UPDATE content_blocks SET embedding_vec = CAST(:embedding_vec AS vector) WHERE id = :block_id"),
        [...],
    )
```

### 4.1 原生向量列（pgvector，可选）

`content_blocks.embedding_vec vector(n)` + HNSW（`vector_cosine_ops`）由
`init_db` 的 `_migrate_content_block_vector_column` 幂等维护：

| 情况 | 行为 |
|---|---|
| 非 PostgreSQL / 无 `vector` 扩展 | 直接跳过，检索自动落在 SQL 下推后端 |
| 列不存在 | `ADD COLUMN vector(n)`，n 取自既有 JSON 向量的实际长度 |
| 维度与当前模型不符 | 索引删掉、列改型并清空（`USING NULL`），交给 `ensure_embedded` 重嵌 |
| HNSW 建索引失败 | 只告警：退化为顺序扫描，结果仍正确 |

历史数据靠 `_backfill_content_block_vectors` 就地回填（`embedding::text::vector`，
纯库内转换、不调嵌入 API）——否则"已有 JSON 向量但没有原生列"的存量块会在
pgvector 后端下静默漏召回。

> 当前开发库实测：`pg_available_extensions` 里没有 `vector`，`detect_vector_backend`
> 返回 `json`，检索走库内余弦下推；自相似性校验（拿真实块自身向量作查询）top1 = 自身，
> score=1.0。装上扩展后无需改代码即切到 HNSW。

---

## 5. 环节三：语料装载 + 三种向量后端

### 5.1 隔离在 SQL 里，不靠应用层自觉

```python
# backend/app/adapters/retrieval/vector_store.py（pgvector 后端）
SELECT id,
       GREATEST(0.0, LEAST(1.0, 1.0 - (embedding_vec <=> CAST(:query AS vector)))) AS similarity
FROM content_blocks
WHERE course_id = :course_id                    -- ← ① 多租户隔离：底线，不是优化
  AND document_parse_run_id IN :run_ids          -- ← ② 只取白名单资料 + ready 解析版本
  AND embedding_model = :embedding_model         -- ← ③ 只取当前嵌入模型的向量
  AND embedding_vec IS NOT NULL                  -- ← ④ 缺向量的块不参与
ORDER BY embedding_vec <=> CAST(:query AS vector), id
LIMIT :k
```

| 过滤条件 | 挡的是什么 | 漏掉会怎样 |
|---|---|---|
| `course_id` | 跨课程数据泄漏 | 直接违反多租户底线（38 张课程级表无一例外） |
| `document_parse_run_id IN :run_ids` | 未就绪/已废弃解析版本的块 | 教师删掉的资料还会被检索到 |
| `embedding_model` | 旧模型算的向量 | 新旧向量空间不可比，旧块语义分≈0 且永远隐身 |
| `embedding_vec IS NOT NULL` | 尚未嵌入/回填的块 | NULL 参与距离运算，结果不可预期 |

### 5.2 三后端：同一抽象，按库能力自动选择

```python
# backend/app/adapters/retrieval/vector_store.py:detect_vector_backend
bind = session.get_bind()
if bind.dialect.name != "postgresql":
    return "memory"        # ← SQLite 测试库：Python 余弦
has_type = ...              # ← pg_type 里有 vector 吗
has_column = ...            # ← content_blocks 有 embedding_vec 吗
return "pgvector" if has_type and has_column else "json"
```

| 后端 | 向量在哪算 | 适用场景 | 代价 |
|---|---|---|---|
| `pgvector` | 数据库（`<=>` 算子，走 HNSW 索引） | 生产库装了扩展 | 需扩展 + 列 + 索引 |
| `json` | 数据库（`json_array_elements_text` + `unnest ... WITH ORDINALITY`，`GROUP BY` 求点积/范数） | 有 PG 但无扩展（**当前开发库**） | 顺序扫描，几千块量级可接受 |
| `memory` | Python（`math.fsum` 余弦） | SQLite 测试、按考点裁剪的几十条陈述 | 向量要随对象传 |

三个后端对外都是标准 `VectorStore`：`similarity_search_with_score()` 返回
**(Document, 距离)**，距离 = `1 - cosine`（与 LangChain 主流 store 口径一致），
`_select_relevance_score_fn` 复用 cosine 反解，保证上层拿到的相关度可比。

### 5.3 装载为什么有 `include_embedding` 开关

`load_content_chunks(..., include_embedding=backend == "memory")`：
SQL 后端在库内算 cosine，1024 维向量 JSON 不必过网络（历史实测 59MB → ~4MB 文本，
数字来自 `load_semantic_scores` 时代的代码注释）；内存后端才需要把向量带出来。

结构残片（`<details>` 之类清洗后无正文的块）不进语料——它们不可嵌入，
混入会让"全部块带向量"的判据永远为假、整轮被打回词面。

---

## 6. 环节四：检索链

### 6.1 查询改写：确定性削词，不调模型

```python
# backend/app/services/assistant_service.py:1287-1316（两张词表，改词表即改改写行为）
_RAG_FRAME_WORDS = (...)   # ← 疑问框架词：讲了什么 / 是什么 / 有哪些 / 怎么做 …
_RAG_PRONOUN_WORDS = (...) # ← 指代词：这门课 / 本课程 / 它 …

def _rag_query_variants(question: str) -> list[str]:
    """原问题 + 削掉疑问框架的主题串（幂等、去重、空串不追加）。"""
```

| 决定 | 理由 |
|---|---|
| 不用 `MultiQueryRetriever` 的 LLM 改写 | 改写要"可复现 + 零模型成本 + 不引入幻觉查询"；多一次模型调用还会把延迟翻倍 |
| 但保留 MultiQuery 的**骨架** | 多路召回 + 排名融合的收益仍在：`MultiVariantRetriever` 只是把"变体从哪来"抽成可注入的 `variant_fn` |
| 削词而不是扩词 | 扩词要靠模型；削词是确定性字符串规则，失败也只退化为原问题 |

### 6.2 查询嵌入：一次批量请求 + 契约校验

```python
# backend/app/adapters/retrieval/embedding_contract.py
def validate_vectors(raw_vectors, *, expected_count): ...
def embed_query(embedder, text): ...
def embed_queries(embedder, texts): ...
```

校验四件事：数量与文本数一致、逐维有限数、非零范数、各向量同维度。
**只有嵌入调用本身失败才抛 `RetrievalConfigurationError`**，上层据此降级词面，不会静默返回空。

知识组织阶段三路查询（考点名 / `retrieval_intent` / 「考点名：考核要求」）走
`HybridStagingRetriever.embed_queries()` 一次批量嵌入，再传入
`retrieve_multi(query_vectors=...)`——避免每个查询各调一次 API。

### 6.3 词面通道：零依赖 BM25

```python
# backend/app/adapters/retrieval/bm25.py
class BM25Retriever(BaseRetriever):
    documents: list[Document]
    k: int = 4
    k1: float = 1.5
    b: float = 0.5        # ← 低于经典 0.75：本项目的块常是整段教材正文，过强的长度惩罚会把正文挤出召回
```

打分是标准 Okapi BM25：

```text
idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
score(d) = Σ_t idf(t) · tf(t,d)(k1+1) / (tf(t,d) + k1(1 - b + b·|d|/avgdl))
```

分词（`adapters/retrieval/tokenization.py`）刻意不引 jieba：

| 文本 | token |
|---|---|
| ASCII | `[A-Za-z0-9_]+` 整词小写（代码标识符、英文术语保持完整） |
| 中文 | 连续汉字串切 **2-gram + 3-gram** |

不引分词器的理由：无词典、无新词歧义、跨环境完全确定；2/3-gram 对"考点名 → 陈述正文"
这类短查询的召回已足够（旧实现实测覆盖率区分度 0.18）。

**为什么不用 `langchain_community` 的 `BM25Retriever`**：它会拖入
`rank_bm25` + `langchain-community` 两个依赖，而中文仍需自定义 `preprocess_func`——
所谓"标准组件"实际只剩一层薄壳。继承 `BaseRetriever` 自己实现，上层拿到的仍是标准
Retriever（可组合、可挂 callback、可 `invoke`）。

### 6.4 融合：加权 RRF，而不是分数相加

```python
# backend/app/adapters/retrieval/fusion.py
def weighted_reciprocal_rank(channel_results, *, weights, top_k,
                             id_key="chunk_id", rank_constant=60, score_budget=None):
    for channel, weight in zip(channel_results, weights):
        for rank, document in enumerate(channel, start=1):
            fused[doc_id] += weight / (rank + rank_constant)
    ...
    budget = score_budget if score_budget is not None else sum(weights)
    metadata["score"] = score / (budget / (1 + rank_constant))   # ← 归一化到 (0, 1]
```

**为什么换 RRF 而不是"0.35 词面 + 0.65 语义"相加**：

1. 两个通道量纲不可比——余弦天然落在 [0,1]，BM25 是无界对数分；
   分数相加必须逐通道归一，而归一系数会随语料长度分布漂移；
2. RRF 只看排名，语料规模、长尾词频变化都不影响融合稳定性。

**权重仍用 0.65 / 0.35**：RRF 下权重是"通道影响力"而非分数量纲，历史调参结论可直接复用。

**`score_budget` 这个参数是踩坑后加的**：归一化分母必须用**配置权重之和**，
不能用"本次有命中的通道权重和"。否则某次查询只有向量通道有命中，它会被归一成满分 1.0，
与"双通道都排第 1"的完美命中打平——多查询合并时主题块反而被 tie-break 挤掉
（实测复现于组织图用例：`m1-topic` 被 `m1-noise` 顶掉）。

通道召回深度取 `max(3·top_k, top_k+4)`：融合只看排名，通道候选太少会让 RRF 退化成单通道。

### 6.5 确定性收口

| 机制 | 做法 | 保证 |
|---|---|---|
| 融合分 | `weight/(rank+60)` 归一化，不含原始余弦的浮点尾级 | 查询向量加 1e-9 抖动，顺序不变（演示 3b） |
| 通道内排序 | `(-score, chunk_id)` | 同分时按主键，跨库跨进程一致 |
| 多查询合并 | 每个 (query, chunk) 取最高 RRF 分，再按 `(-score, content_hash, id)` 截断 | 合并结果不依赖字典迭代顺序 |
| 同文折叠 | `dedup_key` 去掉空白/下划线串后再判重 | 跨文档模板碎片只保留得分最高的一份 |

### 6.6 多查询与同文折叠

```python
# backend/app/adapters/retrieval/multi_query.py
class MultiVariantRetriever(BaseRetriever):
    retriever: BaseRetriever
    variant_fn: Callable[[str], list[str]]
    top_k: int = 6
    rank_constant: int = 60
```

每个变体独立召回 → 按 `1/(rank+60)` 融合 → 按归一正文折叠 → 截断 top_k。
**同文折叠是刚需**：同一课程里十几份大纲的"教学班：____"表头正文完全相同，
不折叠时 3 份副本就能占满 6 个槽位（演示 4：折叠前占 3 槽，折叠后 1 槽）。

### 6.7 邻域扩展：把被短块洪泛淹没的正文带回来

```python
# backend/app/adapters/retrieval/parent.py
class NeighborhoodExpansionRetriever(BaseRetriever):
    retriever: BaseRetriever
    documents: list[Document]
    min_chars: int = 60            # ← 正文下限：低于此为"又一个标题"
    per_hit: int = 2               # ← 每个命中最多带几个邻域块
    max_total: int = 6             # ← 单轮扩展总块数封顶（生成上下文可控）
```

为什么不用 `ParentDocumentRetriever`：它要求"子块 ↔ 父块"两套 id 与两套存储，
而本项目的父块就是同一张 `content_blocks` 的相邻行——父子关系由
`page_index` / `heading_path` / 解析顺序天然给出，不需要额外 docstore。

| 命中块形态 | 扩展策略 |
|---|---|
| 有 `page_index`（PDF） | 同资料、页码差 ≤1 的正文，同页优先、长块优先 |
| 无页码 + 标题块（正文 == `heading_path` 末级） | 同节正文按解析顺序装入；单锚点 600 字、全局 1000 字预算 |
| 无页码 + 正文块 | 前后邻块，距离近者先，同距离**后邻优先** |

扩展块**继承锚点分数**（`expanded_from` 记来源），只附着、不改排序。
md/txt 不产 `page_index`，这条链路正是"命中标题 → 救回本节正文"的唯一通道
（实测：`仅召回 ['md-h']` → `扩展后 ['md-h', 'md-b']`）。

### 6.8 三条降级路径

```python
# backend/app/services/assistant_service.py:_rank_rag_chunks
if embedder is not None:
    try:
        store = _rag_vector_store(...)
        if store is not None:
            ... retriever.invoke(question) ...
            if documents_hit:
                return "hybrid", documents_to_ranked(documents_hit, chunks)
    except RetrievalConfigurationError as exc:
        logger.warning("RAG 混合检索失败，降级词面: %s", exc)

# 词面降级：先按多变体最大覆盖率预筛，再走纯 BM25 链
variants = _rag_query_variants(question)
covered = [d for d in documents
           if max(query_token_coverage(v, d.page_content) for v in variants)
           >= _RAG_LEXICAL_MIN_SCORE]
```

| 触发条件 | 行为 | 结果 |
|---|---|---|
| 嵌入未配置 / 调用失败 | `RetrievalConfigurationError` 被捕获 → 词面模式 | 不断轮，返回 `mode="lexical"` |
| 向量后端装配失败（无 ready run / 无模型名 / 维度不符） | 同上 | 同上 |
| 混合检索零命中 | 词面模式 | 覆盖率预筛后 BM25 |

边界：词面降级只在**嵌入/向量侧**失败时启用；BM25 通道永远参与，不单独成为降级条件。

---

## 7. 环节五：生成与来源卡

来源卡 payload，实测输出：

```json
[
  {
    "block_id": "cb-1001",
    "material_id": "m-1",
    "material_name": "教学大纲.md",
    "page_index": null,
    "heading_path": ["第一章 课程概述", "1.2 考核方式"],
    "snippet": "本课程考核方式由平时作业（30%）、期中测验（20%）与期末闭卷考试（50%）三部分组成。"
  }
]
```

| 字段 | 具体作用 | 前端拿它做什么 |
|---|---|---|
| `block_id` | 块唯一标识 | 点击来源卡 → 跳转原文对应块 |
| `material_id` / `material_name` | 资料身份 | 跳转资料详情，展示"依据来自哪份资料" |
| `page_index` | 页码（MinerU 产出；md/txt 为 `null`） | 有页码定位 PDF 页；`null` 时隐藏页码行 |
| `heading_path` | 标题层级数组（最多 6 级） | 展示"课程概述 > 1.2 考核方式"面包屑 |
| `snippet` | 正文截断 160 字 | 折叠态摘要，展开看全文 |

`snipet` 只截 160 字是因为**卡片给人扫、上下文给模型读**：进 prompt 的上限是 1500 字。

生成侧只投喂 top-k 块，`temperature=0.3`，SSE 流式：

```python
# backend/app/services/assistant_service.py
_RAG_ANSWER_SYSTEM_PROMPT = """你是高校课程「{course_name}」工作台内的 AI 助手，正在基于教师指定的资料回答问题。

要求：
- **只依据下方「资料片段」作答**：片段覆盖不足时明确说明「资料里没有找到」，不得编造片段之外的内容。
- 引用出处：结合片段的页码/章节标题指明依据位置；总结型请求先给结构化要点再展开。
- 用中文简洁自然回答；与资料无关的寒暄礼貌拉回资料话题。
- 不承诺出题比例/难度/去重——这些由系统确定性算法保证；写操作只说明会生成提案由教师确认。"""
```

两个细节：问题直接用教师原话（不经段1 模型转述，转述会失真，而检索对措辞敏感）；
`_RAG_FALLBACK_REPLY` 是段1 的降级文案，不是"无命中兜底"。

---

## 8. LangChain 用在哪、哪些自己实现

### 8.1 用了什么

| 能力 | 用法 | 为什么不自己造 |
|---|---|---|
| `VectorStore` 抽象 | 继承并实现 `similarity_search_with_score` | 上层 `as_retriever()`、组合器、callback 生态可直接复用；三后端切换对上层透明 |
| `BaseRetriever` 抽象 | BM25 / RRF / 多查询 / 邻域扩展各自继承 | 组合器只认 `BaseRetriever`，自研组件能自由拼装、独立测试 |
| `Document` 边界 | `staging_chunk_to_document` / `documents_to_ranked` | 组件生态的标准交换格式；回捞领域对象只靠 `chunk_id` |
| `Embeddings` 抽象 | `PaperPactEmbeddings` | 入库与查询统一接口，便于用确定性替身做离线演示与单测 |
| 切分器 | `MarkdownHeaderTextSplitter` + `RecursiveCharacterTextSplitter` | 章节切分是通用问题，自造只会更差 |

### 8.2 哪些刻意自己写

| 组件 | 自研原因 |
|---|---|
| `BM25Retriever` | `langchain-community` 版要额外两个依赖，中文仍需自定义分词——薄壳不值得 |
| `WeightedRRFRetriever` | LangChain 的 `EnsembleRetriever` 融合分不可配置归一预算；本项目的 `score_budget` 是踩坑必需 |
| `MultiVariantRetriever` | `MultiQueryRetriever` 强制用 LLM 改写；这里变体是确定性的，要能注入 `variant_fn` |
| `NeighborhoodExpansionRetriever` | `ParentDocumentRetriever` 需要双 id 双存储；本项目的父子关系由解析顺序天然给出 |
| `PaperPactVectorStore` 的 SQL | 官方 `PGVector` 需 `langchain-postgres` + `pgvector` 包 + 扩展三件套；要求"不新增依赖"时，SQL 自己写 |

**一句话总结**：框架用来消除**接口层**的重复造轮子，打分与融合保持自持——
要的是「可复现 + 可解释 + 阈值可控」，不是「最少代码」。

---

## 9. 追问预演

**Q：为什么用 RRF 而不用分数加权或向量库默认的 L2？**
余弦落在 [0,1]、BM25 是无界对数，量纲不可比；分数相加必须逐通道归一且归一系数随语料漂移。
RRF 只看排名，语料规模变化不影响融合稳定性。代价是丢了"分差信息"——所以分项分保留在
`lexical_score` / `semantic_score` 里只做观测。

**Q：RRF 的 `score_budget` 是什么坑？**
归一化分母必须用配置权重之和。若用"本次命中通道权重和"，只有向量通道命中时会被归一成 1.0，
与双通道完美命中打平，多查询合并时被 tie-break 挤掉（组织图用例实测复现）。

**Q：中文没有分词，BM25 怎么算？**
ASCII 整词 + 中文 2/3-gram。不引 jieba 是刻意的：无词典、无新词歧义、跨环境完全确定。

**Q：为什么不用 pgvector？**
不是不用，是**按库能力自动选**：有扩展走 `<=>` + HNSW；没有扩展走库内 JSON 下推；
SQLite 测试走内存。当前开发库没有扩展，实测 `detect_vector_backend` 返回 `json`，
自相似 top1 = 自身（score 1.0）。装上扩展后不改代码即切 HNSW。

**Q：怎么保证结果可复现？**
五个来源：改写确定、RRF 只看排名、通道内 `(-score, chunk_id)`、合并期 `content_hash` 决胜、
无随机采样。查询向量加 1e-9 抖动，召回顺序不变（演示 3b）。

**Q：多租户隔离怎么做的？**
`course_id` 出现在每一次 SQL（含向量检索 SQL）、缓存键、对象存储路径；
再加资料白名单与解析版本白名单在装载层二次收口。检索 SQL 里没有裸 `SELECT`。

**Q：一次提问的成本与延迟？**
查询侧：每个变体一次嵌入（2 次，确定性改写所以是固定上界）+ 一条 SQL（SQL 后端）
或内存余弦；生成侧一次流式调用。入库侧一次批量嵌入，`content_hash` 命中时**零调用**。

**Q：邻域扩展为什么不算检索器职责？**
它依赖"来源卡 + prompt 预算"这类产品概念（600/1000 字预算、每命中 2 块）。
放在检索链末尾既不改排序，又能独立配置与测试；塞进打分函数反而会让排序不可解释。

---

## 10. 已知边界（主动说局限，比被问出来好）

1. **Markdown 标题与正文之间没有空行时**，会多出一个只含标题的块（实测见 3.2）。
2. **JSON 下推后端是顺序扫描**：几千块量级可接受，十万级需要扩展或专用向量库。
3. **`0.65/0.35` 权重、`0.15/0.2` 阈值、`rank_constant=60` 都是经验值**，
   没有做过离线评测集上的系统调参；可靠性依靠"确定性 + 阈值闸门 + 来源卡可回溯"。
4. **语义分数不可跨模型比较**：换嵌入模型后旧向量必须重嵌（`embedding_model` 校验强制），
   `embedding_vec` 维度变化时整列改型 + 清空，由 `ensure_embedded` 重嵌。
5. **邻域扩展的 60 字下限**会把很短的正文（如"缺勤三次取消资格。"）判成标题而不带入——
   实测踩到过一次，参数是权衡而非普适最优。

---

## 11. 复现方式

```powershell
cd backend
$env:PYTHONPATH='.'; $env:PYTHONUTF8='1'
uv run python ..\docs\demos\rag_pipeline_demo.py
# 输出同时写入 %TEMP%\rag_demo_output.txt（UTF-8）
```

脚本只调用仓库既有函数（`parse_local_text`、`staging_chunk_to_document`、
`HybridRetrievalChain`、`HybridStagingRetriever`、`BM25Retriever`、
`weighted_reciprocal_rank`、`build_assistant_retriever`、`_rag_query_variants`、
`PaperPactEmbeddings`），不修改任何生产代码；向量由手工指定的确定性映射提供，
便于逐项核对分数与排序。
