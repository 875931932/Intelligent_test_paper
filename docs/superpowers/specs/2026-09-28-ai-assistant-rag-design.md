# AI 助手 v2：资料内容问答（RAG）设计

- 日期：2026-09-28（v1 见 `2026-09-28-ai-assistant-chat-page-design.md`）
- 状态：✅ 已实现（门禁：pytest 1242 passed / cov 85.54%、build 0 error、lint 10 warnings ≤ 基线）

## 1. 背景与范围

v1 助手的回复只基于课程业务快照（状态、名称、计数），不读资料正文；v1 spec §2.2
把「资料内容问答」（"总结这份教学资料"）列为 v2 头条。本轮**只做 RAG 资料问答**，
多会话管理与「停止」按钮留 v3（用户决策）。

**v2 做**：

- 新工具 `answer_material_content`：基于已解析资料正文回答问题/做总结，
  检索片段驱动的流式作答 + **来源引用卡**；
- 解析完成后**自动建索引**（Celery 后台把 `content_blocks` 逐块嵌入落库，
  用户决策），辅以查询时自愈兜底（历史数据/索引任务失败时不空转）。

**v2 不做**：多会话、停止按钮、跨课程聚合、资料全文照抄/朗读（仍拒绝）。

## 2. 架构与数据流

```
教师：「总结教学大纲第3章」
→ 段1 意图解析（worker 内 LLM）
   action = {tool: "answer_material_content", args: {material_id?}}
→ route_intent 第4类路由 kind="rag"（确定性）：
   1. material_id 白名单校验（payload.ids.material_ids，非法 → AssistantError 带反馈重试一次）
   2. 资料存在且最新解析 run 为 ready（否则 AssistantError「尚未解析完成」）
   3. ensure_embedded：语料中缺当前模型向量的块 → 嵌入并落库（幂等，失败仅告警）
   4. load_content_chunks：装载语料（course_id 过滤，仅 staged 资料最新 ready run）
   5. 检索：hybrid（0.35 词面 + 0.65 语义）；嵌入不可用 → 纯词面降级（确定性）
→ 段2 流式作答（RAG 专用 grounding prompt + 检索片段）
→ 落库 action {kind: "sources", tool, args, status: "completed", payload{...sources}}
   SSE card {kind:"sources", tool, payload} → 前端来源卡
```

写能力边界不变：RAG 全链路只读业务数据，只写 `assistant_messages`；索引任务写
`content_blocks` 的两列向量（技术性派生数据，与 evidence_chunks 向量先例同语义，
不改变教师可见内容）。

## 3. 工具与消息契约

### 3.1 意图（段1）

- `RAG_TOOL = "answer_material_content"`，从 `REFUSED_TOOLS` 移除
  （`read_material_content` 全文照抄类仍拒绝）；
- args：`{material_id?: string}`——教师点名资料必传（白名单校验），
  问全课程资料不传；**问题就是教师原话**，不单独抽取 `question` 参数；
- prompt 新增工具说明：仅对 `parse_status == "ready"` 的资料使用；没有已解析
  资料时不使用，回复引导先解析。

### 3.2 路由（第4类 kind）

```python
{"kind": "rag", "stream": True,
 "retrieval": {"mode": "hybrid"|"lexical", "sources": [...]},
 "reply": "已检索到相关资料，回答见下：" }   # 段1 reply 仅作段2失败降级
```

### 3.3 落库/SSE（sources 卡）

```json
action = {"kind": "sources", "tool": "answer_material_content",
          "args": {...}, "status": "completed", "payload": {...}}
```

payload：

```json
{
  "question": "教师原话",
  "material_id": "点名时的 id，否则 null",
  "material_name": "点名时的资料名，否则 null",
  "mode": "hybrid | lexical",
  "sources": [
    {"block_id": "...", "material_id": "...", "material_name": "...",
     "page_index": 2, "heading_path": ["第3章", "3.1"], "snippet": "前160字"}
  ]
}
```

- `sources` 为空（无命中）：不发 card、不落 sources 动作（action 回落普通 chat
  形态），正文由段2 说明「没找到」；
- score 不进 payload（前端不展示相关度，避免误导）。

## 4. 语料与索引

### 4.1 schema（唯一结构变更）

`content_blocks` 增列（镜像 `evidence_chunks` 语义）：

- `embedding` JSON NULL——块向量；
- `embedding_model` VARCHAR(64) NULL——生成模型溯源，**换模型后旧向量不可比**：
  嵌入前的过滤条件是 `embedding IS NULL OR embedding_model != settings.embedding_model`。

`app/db/schema.py` + `init_db.py` 幂等迁移（ALTER ADD，缺列才加）；无新表，
AGENTS/CODE_WIKI 表数 38 不变。

### 4.2 索引服务 `app/services/content_index_service.py`（新）

- `ensure_embedded(session, *, course_id, material_ids=None) -> int`：
  按上述过滤条件选块 → `OpenAICompatibleEmbeddingGateway.embed()`（网关自带分批）
  → 逐块 UPDATE 两列。未配置 `EMBEDDING_*` → 返回 0（不抛错，检索层走词面降级）；
  嵌入失败 → log warning 返回已成数量（查询层据剩余 NULL 向量自动降级）。
  幂等：模型一致且无缺失 → 空转返回 0。
- `load_content_chunks(session, *, course_id, material_ids=None) -> list[dict]`：
  仅 staged 资料最新版本的**最新 ready run**（复用 `latest_parse_status` 的
  排序语义），course_id 强过滤；返回块内容/locator/向量。
- `enqueue_index_task(session, *, course_id, run_id) -> bool`：
  `create_task_run(task_type="material_index", idempotency_key=f"material_index:{run_id}")`
  ——幂等键恒为 run_id，重复触发返回既有任务，不重复入队；调用方负责 commit +
  `dispatch_pending_events`（与 assistant/提案同款 outbox 模式）。

### 4.3 触发点（解析转 ready）

`materials.py` 两个端点在返回 `status == "ready"` 时入队 + 派发（派发失败仅
rollback，事件保持 pending 不阻塞解析响应）：

- `POST .../parse`（同哈希复用直接 ready）；
- `POST .../parse/poll`（MinerU 完成落块转 ready）。

历史已 ready 的 run 不回填任务：由查询时 `ensure_embedded` 自愈
（首次提问该资料多等数秒，一次性代价）。

### 4.4 worker

- `worker.py`：`_LEASE_SECONDS_BY_TYPE["material_index"] = 300`、
  `_handle_material_index` → `ensure_embedded(payload.material_ids or None)`，
  `register_task_handler("material_index", ...)`。
- LLM/嵌入只发生在 worker：端点只入队，RAG 检索与作答在 assistant_turn 内
  （红线 ✓）。

## 5. 检索（`staging_retrieval_service.py` 扩展）

- `retrieve_for_question(question, chunks, embedder, *, top_k, minimum_score,
  query_vector=None)`：镜像 `retrieve_for_exam_point` 但入参为裸问题串；
  0.35 词面（`lexical_overlap` 复用）+ 0.65 语义（余弦复用），分数量化 3 位
  小数 + content_hash 决胜 → 确定性排序；
- `lexical_rank_for_question(question, chunks, *, top_k, minimum_score)`：
  纯词面降级路径（不碰嵌入），同款量化与决胜；
- 模式选择（确定性）：嵌入未配置 / 查询嵌入失败 / 任一块向量缺失 → lexical；
  默认 top_k=6（服务常量，可调）。

## 6. Prompt

### 6.1 段1（意图）

- 工具清单加 `answer_material_content` 条目与 args 规则（点名必传、白名单取 id）；
- 拒绝表第 6 条改写：资料**内容**问答 → 走 `answer_material_content`；
  「全文照抄/朗读整份资料」仍拒绝（token 爆炸且无必要）；
- 回复聚焦/不复述/状态词以卡片为准规则不变。

### 6.2 段2（RAG 专用 `_RAG_ANSWER_SYSTEM_PROMPT`）

- **只依据提供的资料片段作答**，片段覆盖不足就明确说「资料里没找到」，不臆造；
- 引用出处（页码/章节标题）；中文简洁，总结型先给结构化要点；
- 既有红线照旧（不承诺比例/难度/去重；写操作只提提案确认制）；
- payload：`{course, history, question, sources: [{material_name, page,
  heading_path, text}], user_message}`；
- 片段预算：每块进 prompt ≤1500 字、总量 ≤12000 字（超预算截断），
  每块 snippet 进 payload ≤160 字。

## 7. 前端

- `types/api.ts`：`AssistantAction.kind` 增 `'sources'`；新增
  `AssistantSourceCitation`；payload 增 `sources/question/material_id/material_name/mode`；
- `pages/assistant/index.tsx`：新增 `SourcesCard`（资料名 + 页/章节 + snippet，
  CTA「去资料库查看」跳资料页；资料页支持 `?material=` 深链则带上，
  实现时确认，不支持则裸跳列表页）；渲染分支加 `action.kind === 'sources'`；
  `sources` 空数组不渲染卡；
- SSE card kind `sources` 与既有 result 同形处理（store 若有 kind 白名单则同步）。

## 8. 测试计划

| 层 | 用例要点 |
|---|---|
| unit·索引 | 缺向量嵌入落库（stub gateway）；模型不一致重嵌；全就绪空转；未配置返回 0；course_id 隔离；enqueue 幂等（同 run_id 返回既有任务） |
| unit·检索 | `retrieve_for_question` hybrid 排序确定性（量化 + 决胜）；lexical 降级不碰嵌入；向量维度不齐 → 降级 |
| unit·路由 | 合法点名 → kind=rag + sources 组装；外来 material_id → AssistantError；未解析资料 → AssistantError；`answer_material_content` 不在 REFUSED；prompt 含工具说明与 grounding 规则 |
| unit·前端契约 | payload 字段与 types 镜像一致（人工核对 + tsc） |
| integration | worker 端到端 RAG 轮次（测试环境无嵌入配置 → lexical 路径天然可测）；poll 转 ready → 索引任务入队断言 |
| 门禁 | `uv run pytest --cov=app --cov-fail-under=80`；`npm run build`；`npm run lint` |

## 9. 红线自查

- LLM/嵌入不在请求线程：端点只入队，检索与作答全在 worker ✓
- 外部系统只走 `adapters/`（嵌入网关既有）✓
- 查询全带 `course_id`（块、语料、任务、SSE）✓
- 表结构只走 `schema.py` + `init_db` 幂等迁移，无 Alembic ✓
- 封存 graph 不动 ✓；比例/难度/去重不进任何助手 prompt ✓
- AI 只读不写业务：只写 `assistant_messages` + `content_blocks` 向量列
  （技术性派生数据）✓；写操作仍全部提案卡确认制 ✓
- 课程隔离：无课程专属禁词表，RAG prompt 为通用 grounding 规则 ✓

## 10. 风险与边界

- **未配置 `EMBEDDING_*`**：自动索引空转，RAG 退化纯词面检索（仍可用）；
- **大语料**：课程量级（几十份资料）Python 线性余弦可接受（evidence_chunks
  同款先例）；查询按 material 收敛天然减负，不做分片保持简单；
- **历史数据**：查询时 `ensure_embedded` 自愈，首查多等数秒（一次性）；
- **换嵌入模型**：`embedding_model` 不匹配自动重嵌（与 evidence_chunks 同语义）。
