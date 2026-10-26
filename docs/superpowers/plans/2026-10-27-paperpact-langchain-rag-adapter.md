# PaperPact LangChain RAG 适配层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变 PaperPact 现有检索结果契约、课程隔离、降级策略和 LangGraph 工作流的前提下，把课程资料 RAG 接入 LangChain 的 `Embeddings`、`Document` 和自定义 `Retriever` 抽象。

**Architecture:** 保留现有 PostgreSQL JSON 向量、SQL 语义分下推、关键词覆盖率、混合评分、多查询合并、资料范围过滤和邻域扩展。新增一层 LangChain 适配器：Embedding 网关适配为 LangChain `Embeddings`，`StagingChunk` 适配为 `Document`，自定义 Retriever 委托现有确定性检索核心。助手 RAG 先切换到适配器，知识目录检索通过兼容入口逐步接入；`LangGraph`、LLM 网关、流式输出和领域工作流保持不变。

**Tech Stack:** Python 3.12、FastAPI、LangChain Core、LangGraph、SQLAlchemy 2.0、PostgreSQL、Redis、Celery、pytest、uv。

**Spec:** 本文即实现规格；仓库工程约束以 `AGENTS.md` 为最高优先级，RAG 现有行为以 `backend/app/services/staging_retrieval_service.py`、`backend/app/services/content_index_service.py` 和对应测试为基线。

## Global Constraints

- 不修改 `backend/app/workflows/framework_graph.py`、`organization_graph.py`、`generation_graph.py` 的领域逻辑；这些文件属于已验证封存代码。
- 不迁移现有 `content_blocks.embedding` JSON 存储，不引入 pgvector、Milvus 或新的向量数据库。
- 不替换现有 `OpenAICompatibleEmbeddingGateway`、`LLMJsonClient`、SSE 流式输出和 Celery 异步边界。
- 不把 LangChain 的默认切分器用于替换 MinerU 解析块；现有 `heading_path`、`page_index` 和 `block_index` 是来源定位的权威数据。
- 所有数据库查询继续携带 `course_id`；`material_ids` 过滤和资料可见性规则不能由模型或检索器自行放宽。
- 保留没有 Embedding 配置、向量缺失、Embedding 请求失败和 PostgreSQL 语义下推失败时的纯词面降级。
- 默认保留 `custom` 后端作为回滚路径；只有通过等价性测试后，才把 `langchain` 后端设为默认。
- 不因为接入 LangChain 而把检索结果改成“模型认为相关”的自由选择；排序、阈值、去重和来源元数据仍由程序决定。
- 后端依赖只使用 `uv`，提交 `backend/pyproject.toml` 与同步后的 `backend/uv.lock`。

---

## 现状与目标边界

### 现有能力必须保留

1. `content_index_service.ensure_embedded` 对解析块进行清洗、批量 Embedding、模型名和清洗版本记录。
2. `load_content_chunks` 返回 `StagingChunk`，携带正文、资料版本和定位器。
3. `load_semantic_scores` 在 PostgreSQL 内计算余弦相似度，避免将全部向量传到 Python。
4. `staging_retrieval_service` 使用查询侧词面覆盖率和向量语义分进行混合排序。
5. `retrieve_multi_for_question` 支持多个查询变体、同文折叠和确定性排序。
6. `assistant_service` 负责 RAG 路由、来源卡、邻域扩展、无结果拒答和流式回答。
7. `organization_graph` 通过既有检索入口获取考点证据，不能因适配器改造破坏知识目录构建。

### 改造后的目标调用关系

```text
OpenAICompatibleEmbeddingGateway
        │
        ▼
PaperPactEmbeddings (LangChain Embeddings adapter)
        │
        ├── ensure_embedded / query embedding
        │
StagingChunk ──► Document + metadata
        │
        ▼
PaperPactHybridRetriever (custom BaseRetriever)
        │
        ├── existing lexical coverage
        ├── existing semantic cosine / SQL pushdown
        ├── existing multi-query merge
        └── existing material scope + neighborhood expansion
        │
        ├── assistant RAG
        └── knowledge organization compatibility entry
```

## File Map

### Create

- `backend/app/adapters/model/langchain_embeddings.py`
  - LangChain `Embeddings` 适配器，只负责把既有 Embedding 网关转换为 `embed_documents` 和 `embed_query`。
- `backend/app/adapters/retrieval/langchain_documents.py`
  - `StagingChunk` / `RankedChunk` 到 LangChain `Document` 的转换，统一来源元数据字段。
- `backend/app/services/langchain_retrieval.py`
  - PaperPact 自定义 Retriever，调用现有确定性检索核心，不复制混合评分算法。
- `backend/tests/unit/test_langchain_embeddings.py`
  - Embedding 适配器的批量、单查询、空输入和错误传播测试。
- `backend/tests/unit/test_langchain_documents.py`
  - Document 内容和元数据映射测试。
- `backend/tests/unit/test_langchain_retrieval.py`
  - Retriever 与现有检索函数的等价性、过滤和降级测试。

### Modify

- `backend/pyproject.toml`
  - 增加 LangChain Core 直接依赖；不引入与当前模型供应商无关的 provider 包。
- `backend/uv.lock`
  - 运行 `uv lock` 后提交依赖锁定结果。
- `backend/app/config.py`
  - 增加 RAG 后端选择配置，默认值保持 `custom`。
- `backend/app/services/staging_retrieval_service.py`
  - 抽取或复用现有排序核心，为兼容入口和 LangChain Retriever 提供统一的 `RankedChunk` 结果；让 `retrieve_for_exam_point` 通过共享核心提供兼容结果，不得直接修改封存的 `organization_graph.py`。
- `backend/app/services/assistant_service.py`
  - 在特性开关打开时通过 Retriever 获取 `Document`，保留现有来源卡、邻域扩展和流式回答契约。
- `backend/tests/unit/test_staging_retrieval.py`
  - 补充自定义后端与 LangChain 后端的结果对照测试。
- `backend/tests/unit/test_assistant_service.py`
  - 保留并扩展助手 RAG 的 LangChain 后端分支、降级和来源元数据测试。
- `backend/tests/integration/test_assistant_api.py`
  - 验证真实 API 仍能返回流式正文和来源卡。
- `backend/tests/workflow/test_organization_graph.py`
  - 验证知识组织流程的证据 ID、证据分数和覆盖判定不变。
- `README.md`
  - 仅在代码和测试完成后，把技术栈写为 `LangChain Core（Document/Embeddings/自定义 Retriever）`，避免写成使用现成向量数据库。
- `docs/backend-api.md`
  - 更新 RAG 实现说明，写清 LangChain 只负责抽象层，检索排序和课程隔离仍由 PaperPact 服务控制。

## Task 1: 固化改造前基线

**Files:**
- Test: `backend/tests/unit/test_staging_retrieval.py`
- Test: `backend/tests/unit/test_assistant_service.py`
- Test: `backend/tests/workflow/test_organization_graph.py`
- Reference: `backend/app/services/staging_retrieval_service.py`
- Reference: `backend/app/services/content_index_service.py`

**Interfaces:**
- Consumes: 当前 `StagingChunk`、`RankedChunk`、`retrieve_for_question`、`retrieve_multi_for_question` 和助手来源卡结构。
- Produces: 改造前的可复现 top-k ID、排序、分数、来源定位和降级行为记录，供后续等价性测试使用。

- [ ] **Step 1: 运行现有检索和助手 RAG 门禁**

```powershell
Set-Location F:\比赛项目\阅卷出题功能\backend
uv run pytest -q tests/unit/test_staging_retrieval.py tests/unit/test_assistant_service.py tests/workflow/test_organization_graph.py
```

Expected: 当前测试全部通过；记录测试数量和失败列表，不在本任务中修改生产代码。

- [ ] **Step 2: 固定最小回归样例**

在测试 fixture 中至少保留以下场景：

```text
1. 关键词命中专业术语，但正文较长；
2. 语义相似但词面没有完全重合；
3. 多查询变体命中同一正文；
4. 同一模板片段跨资料重复；
5. 向量缺失或 Embedding 服务失败；
6. material_ids 过滤后不能返回其他资料；
7. 无页码标题命中后能够扩展同章节正文；
8. 所有候选低于阈值时返回空结果。
```

- [ ] **Step 3: 提交基线**

```bash
git add backend/tests
git commit -m "test: lock PaperPact RAG retrieval baseline"
```

## Task 2: 增加 LangChain Embeddings 适配器

**Files:**
- Create: `backend/app/adapters/model/langchain_embeddings.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Test: `backend/tests/unit/test_langchain_embeddings.py`

**Interfaces:**
- Consumes: `app.adapters.model.embedding_gateway.OpenAICompatibleEmbeddingGateway`。
- Produces:

```python
class PaperPactEmbeddings(Embeddings):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
```

- [ ] **Step 1: 写适配器失败测试**

测试必须验证：

```python
def test_embed_documents_delegates_batch():
    gateway = FakeGateway(vectors=[[0.1, 0.2], [0.3, 0.4]])
    adapter = PaperPactEmbeddings(gateway=gateway)
    assert adapter.embed_documents(["a", "b"]) == [[0.1, 0.2], [0.3, 0.4]]
    assert gateway.calls == [["a", "b"]]

def test_embed_query_returns_single_vector():
    gateway = FakeGateway(vectors=[[0.1, 0.2]])
    adapter = PaperPactEmbeddings(gateway=gateway)
    assert adapter.embed_query("query") == [0.1, 0.2]

def test_empty_document_batch_does_not_call_gateway():
    gateway = FakeGateway(vectors=[])
    assert PaperPactEmbeddings(gateway=gateway).embed_documents([]) == []
    assert gateway.calls == []
```

- [ ] **Step 2: 增加直接依赖并锁定版本**

在 `backend/pyproject.toml` 增加 `langchain-core`，不要因为适配器需求引入完整 provider SDK。运行：

```powershell
Set-Location F:\比赛项目\阅卷出题功能\backend
uv lock
uv sync
```

- [ ] **Step 3: 实现最小适配器**

约束：

1. `embed_documents` 必须一次把文本列表交给现有 gateway，保留 gateway 的批量限制和响应校验。
2. `embed_query` 必须复用同一 gateway，不得新增第二个 HTTP 客户端。
3. 不在适配器中读取 `.env`，配置继续由 `settings` 和 `content_index_service.build_embedder` 负责。
4. 不吞掉 `EmbeddingGatewayError`；错误应继续触发现有检索降级或测试可观察的异常。

- [ ] **Step 4: 运行适配器测试并提交**

```powershell
uv run pytest -q tests/unit/test_langchain_embeddings.py
```

```bash
git add backend/app/adapters/model/langchain_embeddings.py backend/pyproject.toml backend/uv.lock backend/tests/unit/test_langchain_embeddings.py
git commit -m "feat: add LangChain embeddings adapter"
```

## Task 3: 建立 StagingChunk 到 Document 的单向适配

**Files:**
- Create: `backend/app/adapters/retrieval/langchain_documents.py`
- Test: `backend/tests/unit/test_langchain_documents.py`

**Interfaces:**

```python
def ranked_chunk_to_document(item: RankedChunk) -> Document: ...
def staging_chunk_to_document(chunk: StagingChunk) -> Document: ...
def document_metadata(document: Document) -> dict: ...
```

- [ ] **Step 1: 写字段映射测试**

`Document.page_content` 必须等于原始 `chunk.content`；`Document.metadata` 至少包含：

```python
{
    "chunk_id": chunk.id,
    "material_version_id": chunk.material_version_id,
    "material_id": chunk.locator["material_id"],
    "material_name": chunk.locator["material_name"],
    "page_index": chunk.locator["page_index"],
    "heading_path": chunk.locator["heading_path"],
}
```

`RankedChunk` 转换时额外包含：

```python
{
    "score": item.score,
    "lexical_score": item.lexical_score,
    "semantic_score": item.semantic_score,
}
```

- [ ] **Step 2: 实现转换函数**

约束：

1. 不把向量放进 `Document.metadata`，避免来源卡和 prompt 携带大字段。
2. `heading_path` 必须复制为新列表，避免后续扩展修改原始定位器。
3. `None` 页码保留为 `None`，不能转成字符串 `"None"`。
4. `chunk_id` 必须可用于来源卡的“查看原文”定位。

- [ ] **Step 3: 运行测试并提交**

```powershell
uv run pytest -q tests/unit/test_langchain_documents.py
```

```bash
git add backend/app/adapters/retrieval/langchain_documents.py backend/tests/unit/test_langchain_documents.py
git commit -m "feat: map PaperPact chunks to LangChain documents"
```

## Task 4: 实现 PaperPactHybridRetriever

**Files:**
- Create: `backend/app/services/langchain_retrieval.py`
- Modify: `backend/app/services/staging_retrieval_service.py`
- Test: `backend/tests/unit/test_langchain_retrieval.py`

**Interfaces:**

```python
from pydantic import ConfigDict

class PaperPactHybridRetriever(BaseRetriever):
    chunks: list[StagingChunk]
    embedder: EmbeddingClient | None
    top_k: int
    minimum_score: float
    semantic_scores: dict[str, float] | None = None
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: CallbackManagerForRetrieverRun,
    ) -> list[Document]: ...

    def retrieve_variants(
        self,
        question_variants: list[str],
        *,
        semantic_scores: list[dict[str, float]] | None = None,
    ) -> list[Document]: ...

    def retrieve_ranked(
        self,
        question: str,
    ) -> list[RankedChunk]: ...
```

- [ ] **Step 1: 写等价性测试**

对于同一批 `StagingChunk`、同一组查询、同一阈值，要求：

```python
legacy = retrieve_multi_for_question(
    variants,
    chunks,
    embedder,
    top_k=6,
    minimum_score=0.15,
)
modern = retriever.retrieve_variants(variants)

assert [doc.metadata["chunk_id"] for doc in modern] == [
    item.chunk.id for item in legacy
]
assert [doc.metadata["score"] for doc in modern] == [
    item.score for item in legacy
]
```

同时覆盖：

- 空 chunks 不调用 Embedding；
- 空白查询变体被过滤；
- 语义分已由 SQL 预计算时不调用 Embedding；
- Embedding 失败时仍走现有纯词面降级；
- `material_ids` 过滤后的候选不会越界；
- `minimum_score` 不合法时仍抛出 `RetrievalConfigurationError`。

- [ ] **Step 2: 抽取共享检索核心**

把现有混合评分和确定性排序保留在一个内部实现中。`PaperPactHybridRetriever` 只能调用这个核心，不能复制一份新的：

```python
ranked = _rank_hybrid(
    intent=query,
    chunks=chunks,
    query_vector=query_vector,
    top_k=top_k,
    minimum_score=minimum_score,
    semantic_scores=semantic_scores,
)
```

`RankedChunk` 仍是领域内部结果；只有 Retriever 边界把它转换成 `Document`。

- [ ] **Step 3: 实现单查询与多查询入口**

规则：

1. `invoke("问题")` 调用单查询逻辑，行为等价于 `retrieve_for_question`。
2. `retrieve_variants([...])` 调用多查询逻辑，行为等价于 `retrieve_multi_for_question`。
3. 多查询合并仍按现有归一化文本折叠和确定性决胜键排序。
4. `Document.metadata` 保留检索分数和来源定位，供助手来源卡使用。
5. 不在 Retriever 内实现邻域扩展；邻域扩展仍由助手服务按当前课程资料快照执行。

- [ ] **Step 4: 运行检索适配器测试和原有检索测试**

```powershell
uv run pytest -q tests/unit/test_langchain_retrieval.py tests/unit/test_staging_retrieval.py
```

```bash
git add backend/app/services/langchain_retrieval.py backend/app/services/staging_retrieval_service.py backend/tests/unit/test_langchain_retrieval.py
git commit -m "feat: add PaperPact hybrid LangChain retriever"
```

## Task 5: 以特性开关接入助手 RAG

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/services/assistant_service.py`
- Modify: `backend/tests/unit/test_assistant_service.py`
- Modify: `backend/tests/integration/test_assistant_api.py`

**Interfaces:**

```python
rag_retriever_backend: Literal["custom", "langchain"] = "custom"
```

`assistant_service` 的检索装配必须继续返回当前来源卡需要的结构：

```python
{
    "chunk_id": str,
    "content": str,
    "locator": dict,
    "score": float,
    "lexical_score": float,
    "semantic_score": float,
}
```

- [ ] **Step 1: 写 feature flag 分支测试**

测试同一查询在 `custom` 和 `langchain` 两个后端下：

1. 来源卡的 chunk ID 顺序一致；
2. 来源卡内容和定位器一致；
3. 空结果仍走“不编造”路径；
4. 向量失败仍降级词面；
5. 流式回答 prompt 仍包含来源片段；
6. `material_id` 白名单仍然有效。

- [ ] **Step 2: 增加配置字段**

默认配置必须是：

```python
rag_retriever_backend: str = "custom"
```

配置值只允许 `custom` 和 `langchain`；其他值在配置读取或检索装配时明确报错，不静默切换。

- [ ] **Step 3: 在助手服务中接入 Retriever**

仅替换“检索结果获取”这一段：

1. 继续使用现有 `_rag_query_variants`；
2. 继续调用 `ensure_embedded`、`load_content_chunks`、`load_semantic_scores`；
3. `custom` 分支保持旧调用；
4. `langchain` 分支构造 `PaperPactHybridRetriever` 并调用 `retrieve_variants`；
5. 把 `Document.metadata` 重新装配为来源卡结构；
6. 继续调用现有 `_expand_rag_neighborhood`；
7. 不改变 `_RAG_ANSWER_SYSTEM_PROMPT`、SSE 事件名和 `stream_rag_answer` 的输出协议。

- [ ] **Step 4: 运行助手单测和集成测试**

```powershell
uv run pytest -q tests/unit/test_assistant_service.py tests/integration/test_assistant_api.py
```

- [ ] **Step 5: 在默认配置仍为 custom 的情况下提交**

```bash
git add backend/app/config.py backend/app/services/assistant_service.py backend/tests/unit/test_assistant_service.py backend/tests/integration/test_assistant_api.py
git commit -m "feat: add LangChain retriever backend for assistant RAG"
```

## Task 6: 接入知识组织的考点检索

**Files:**
- Modify: `backend/app/services/staging_retrieval_service.py`
- Modify: 实际装配考点检索的 service 文件；不要直接修改封存的 `backend/app/workflows/organization_graph.py`
- Test: `backend/tests/workflow/test_organization_graph.py`
- Test: `backend/tests/integration/test_exam_point_pipeline.py`

**Interfaces:**
- Consumes: `retrieve_for_exam_point` 当前函数签名和 `RankedChunk` 返回结构。
- Produces: 在 `RAG_RETRIEVER_BACKEND=langchain` 时，考点检索通过共享的 Retriever 核心执行；调用方仍收到原 `RankedChunk`，不需要修改领域图。

- [ ] **Step 1: 增加考点检索等价性测试**

对于现有 `retrieve_for_exam_point` 的测试样例，比较两个后端的：

```python
[
    (item.chunk.id, item.score, item.lexical_score, item.semantic_score)
    for item in results
]
```

必须完全一致，或对浮点数使用 `pytest.approx(..., abs=1e-9)`。

- [ ] **Step 2: 通过兼容入口接入**

不要修改 `organization_graph` 的节点、边和 State。由 `retrieve_for_exam_point` 或其上层 service 根据配置选择：

```text
custom     -> 现有确定性检索核心
langchain  -> PaperPactHybridRetriever.retrieve_ranked(...)
```

`retrieve_ranked` 返回 `RankedChunk`，因此知识组织的证据覆盖、来源 ID 和分数逻辑不变。

- [ ] **Step 3: 运行工作流验证套件**

```powershell
uv run pytest -q tests/workflow/test_organization_graph.py tests/integration/test_exam_point_pipeline.py
```

由于触及知识组织检索入口，必须在合入前再运行完整后端门禁：

```powershell
uv run pytest -q
```

## Task 7: 回归、性能与可回滚验收

**Files:**
- Test: `backend/tests/unit/test_langchain_retrieval.py`
- Test: `backend/tests/unit/test_assistant_service.py`
- Test: `backend/tests/integration/test_assistant_api.py`
- Modify: `docs/backend-api.md`

- [ ] **Step 1: 对比两种后端的结果**

使用固定的真实资料快照和固定查询集，至少记录：

```text
query
material_ids
backend
returned_chunk_ids
scores
empty_result
fallback_mode
elapsed_ms
```

验收条件：

1. 相同输入下 top-k ID 和排序一致；
2. 资料范围过滤结果一致；
3. 没有 Embedding 时两边都能纯词面回答或明确返回无结果；
4. 向量服务失败时不会抛出未处理异常；
5. 来源卡能用 chunk ID 回到原文；
6. LangChain 后端没有明显增加单轮检索耗时；若增加，必须说明增加来自适配层而非重新请求 Embedding。

- [ ] **Step 2: 验证 SQL 语义下推没有被绕过**

在 PostgreSQL 测试环境中确认 `include_embedding=False` 和 `load_semantic_scores` 路径仍然生效；LangChain `Document` 不得携带全量向量。

- [ ] **Step 3: 验证回滚**

将 `RAG_RETRIEVER_BACKEND=custom` 后重新运行助手和知识组织测试，结果必须恢复到基线。不要删除旧实现，直到 LangChain 后端至少经过一轮真实资料人工验证。

- [ ] **Step 4: 更新文档**

`docs/backend-api.md` 至少说明：

```text
RAG 的 LangChain 层负责 Embeddings、Document 和 Retriever 抽象；
向量持久化、课程隔离、混合评分、SQL 语义下推、邻域扩展和降级策略仍由 PaperPact 自定义服务负责；
RAG_RETRIEVER_BACKEND=custom|langchain，默认值和回滚方式；
无结果时不生成未经资料支持的答案。
```

- [ ] **Step 5: 通过最终门禁并提交**

```powershell
Set-Location F:\比赛项目\阅卷出题功能\backend
uv run pytest -q
uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=80
```

```bash
git add backend docs/backend-api.md
git commit -m "test: verify LangChain RAG parity and rollback"
```

## Task 8: 更新项目 README 和简历口径

**Files:**
- Modify: `README.md`
- Modify: `docs/backend-api.md`
- Modify: `F:\比赛项目\实习准备\04-简历正文打磨版-20261026.md`
- Modify: `F:\比赛项目\实习准备\_resume_build\build_resume.py`

- [ ] **Step 1: 更新 README 技术栈**

只有在 `RAG_RETRIEVER_BACKEND=langchain` 通过测试并完成人工验证后，才把后端技术栈写成：

```text
FastAPI + SQLAlchemy 2.0 + PostgreSQL + Redis + Celery
LangChain Core（Document、Embeddings、自定义 Retriever）
LangGraph 工作流
```

不要写成“使用 LangChain 向量数据库”或“使用 LangChain 完成全部检索算法”。

- [ ] **Step 2: 更新项目经历**

PaperPact 项目经历建议使用：

```text
接入 LangChain 的 Document、Embeddings 与自定义 Retriever 抽象，
保留项目自研的关键词覆盖率与向量相似度混合检索、资料范围过滤及同章节上下文扩展，
并通过 LangGraph 编排知识组织和试卷生成流程。
```

- [ ] **Step 3: 更新技能表述**

技能栏建议写成：

```text
LangChain 与 RAG：使用 LangChain 学习和实践 RAG 基础流程，
理解文档加载、切分、Embedding、检索与上下文组装；
具备基于 Python 自定义混合检索和 LangChain Retriever 适配的项目经验。
```

不要使用“精通 LangChain”“熟练掌握 LangChain 全套组件”等无法由当前项目和面试实操支撑的表述。

- [ ] **Step 4: 重新生成并检查简历**

```powershell
Set-Location F:\比赛项目\实习准备\_resume_build
uv run python build_resume.py
```

检查项目技术栈、Gitee 地址、页数、超链接和 PDF 视觉排版；简历只在代码代理完成并通过验收后更新，避免先写成果再补实现。

## 回滚与失败处理

1. 适配器单测失败：只修适配器，不修改现有检索核心。
2. top-k 结果不一致：默认继续使用 `custom`，定位差异是查询向量、过滤、排序还是 Document 转换。
3. SQL 语义下推失效：禁止用 Python 加载全部向量作为默认修复；恢复 `custom`，重新设计适配边界。
4. 助手来源卡字段丢失：先恢复来源卡使用 `RankedChunk` 的旧路径，再补充 metadata 映射。
5. 知识组织工作流测试失败：不得修改 `organization_graph.py` 的节点或边，撤回考点检索的 LangChain 分支。
6. 真实资料表现退化：保留 LangChain 适配层作为实验后端，不把它设为默认，也不在简历中写成已上线主链路。

## 完成标准

- [ ] `langchain-core` 是直接依赖，`uv.lock` 已同步。
- [ ] Embeddings、Document、Retriever 三个 LangChain 抽象均有真实代码和单元测试。
- [ ] 助手 RAG 在 `langchain` 后端下通过单测和 API 集成测试。
- [ ] 知识组织检索在 `langchain` 后端下通过工作流和集成测试。
- [ ] custom/langchain 两种后端在固定样例上的 top-k、来源和降级行为一致。
- [ ] PostgreSQL SQL 语义下推、课程隔离和邻域扩展仍然生效。
- [ ] `RAG_RETRIEVER_BACKEND=custom` 可以无损回滚。
- [ ] README、API 文档和简历表述与实际代码一致。
- [ ] 完整后端测试和覆盖率门禁通过。
