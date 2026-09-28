# AI 助手对话页设计（v1）

> 状态：设计待评审
> 日期：2026-09-28
> 范围：全栈功能设计——课程内对话式 AI 助手页，聚合查询、提案与异步任务触发。

## 1. 背景与目标

教师在出卷流程中需要在资料库、命题框架、知识目录、蓝图/合同、试卷等多个页面之间来回切换。
本设计新增一个对话式「AI 助手」页，把**状态查询、低危操作提案、异步任务触发**聚合到一个入口：

- 用户用自然语言表达需求（「看下我上传的资料」「帮我建一门新课程」「改一下合同槽位」）；
- 助手解析意图 → 只读查询直接返回结构化结果卡 → 写操作生成提案卡 → 用户确认后由既有业务 API 执行。

**核心原则：助手页不新增任何业务写路径。** 执行者永远是各页面同款的既有 API 与确定性算法，
助手只负责意图解析、结果呈现与提案生成——这是「AI 只产提案/只读报告，不绕确认流」红线的落地形态。

## 2. 范围

### 2.1 v1 做

- 课程内单时间线对话页（`/courses/:courseId/assistant`），SSE 流式回复；
- 只读查询（结果卡 + 页面跳转）：课程全阶段概览、资料清单与解析状态、框架/蓝图/合同/试卷状态、项目列表；
- 写操作提案（提案卡，确认后调既有 API）：新建/修改课程、修订合同、触发资料解析、触发蓝图 AI 建议；
- 纯问答（系统使用说明、状态解释）；
- 历史恢复与在途轮次断线续读。

### 2.2 v2 展望（明确不在 v1）

- **资料内容问答**（「总结这份教学资料」）：需新建 RAG 检索链路（向量检索 + 资料内容进上下文），
  底子是既有 `staging_retrieval_service`/`EmbeddingClient`，独立迭代；
- 多会话管理（新建/切换/重命名会话）；
- 跨课程聚合视图与操作；
- 生成中「停止」按钮（v1 断开 SSE 不取消任务，回复照常落库）。

### 2.3 明确拒绝（回复并引导到对应页面）

| 用户想做 | 处置 |
|---|---|
| 出题/改题内容 | 引导试卷页（AI 修改/创建题目既有入口） |
| 蓝图确认、试卷定稿与导出（里程碑冻结类操作） | 引导对应页面人工操作 |
| 删除资料等危险删除 | 引导对应页面 |
| 在线考试/阅卷 | 范围外，指向 docs 范围声明 |
| 操作其他课程的对象 | 拒绝（course_id 隔离硬约束） |

## 3. 交互设计

### 3.1 页面与入口

- 路由：`App.tsx` 在 `/courses/:courseId` 子路由中新增 `assistant`；
- 入口：`Sidebar.tsx` 的 `navItems` 追加 `{ to: `${base}/assistant`, icon: Bot, label: 'AI 助手' }`；
- 版式：垂直消息时间线 + 底部输入框，复用全局设计变量（`--space-*`、玻璃卡样式），页头沿用
  `page-header/page-title` 工具类。

### 3.2 消息与卡片

| 角色/类型 | 说明 |
|---|---|
| `user` | 用户输入文本 |
| `assistant` 文本 | Markdown 回复（流式或整段） |
| 结果卡 | 结构化数据（清单/状态表）+「打开 XX 页」跳转按钮，由确定性查询结果渲染，不依赖模型文本 |
| 提案卡 | 操作名 + 参数预览 + 影响说明 + [确认执行] [取消]；确认后调既有 API，成功回写状态并显示回执 |
| `system` 回执 | 提案执行成功/失败、任务触发结果，追加在时间线 |

刷新恢复：挂载时 `GET messages` 拉历史；若最近轮次仍在途（task_run 非终态）则重连 SSE 续读
（模式参照蓝图 AI 建议面板的恢复 effect：防重复拉取、在途续轮询）。

## 4. 架构：SSE 流式链路

红线约束：LLM 调用禁止进入请求线程。因此 **SSE 端点只是事件转发器，不调用模型**；
流式生成发生在 Celery worker，经 Redis Stream 中继：

```text
POST /courses/{id}/assistant/turns {message}
  → create_task_run(task_type="assistant_turn") + outbox 派发 → 202 {task_run_id}
  → 前端立即 fetch 打开 GET .../turns/{task_run_id}/stream（SSE，带 Authorization）

Worker（register_task_handler("assistant_turn", ...)）
  ① 确定性上下文快照：课程各阶段状态 + 对象清单（名称↔id 白名单）+ 最近 N 条消息
  ② 意图解析·段1（非流式 request_json，温度 0）→ {reply, action:{tool,args}|null}
  ③ 确定性路由：
       action=null            → 纯问答，进入 ④
       只读工具               → 执行 DB 查询，结果组装为结果卡
       提案工具               → 参数白名单校验，组装提案卡（status=proposed），不执行
       拒绝类                 → 固定引导文案
  ④ 纯问答 → 意图解析·段2 流式生成正文，每个 delta XADD 进 Stream；
     查询/提案 → 段1 的 reply 整段 + 卡片事件一次性 XADD（省一次调用）
  ⑤ 终态：写 assistant_messages 落库 → XADD done/error → task_runs 置 succeeded/failed

SSE 端点（FastAPI StreamingResponse，async）
  XREAD BLOCK 读 assistant:turn:{task_run_id} → yield "event: delta|card|done|error"
  task 终态推送 done 后关闭连接；不触碰 LLM
```

要点：

- **两段式意图解析**：段1 保证结构化意图可确定性校验（不做多步 agent 循环、不做流上 JSON 解析）；
  段2 仅纯问答需要流式正文，查询/提案类用段1 的 reply 整段返回，省一次调用；
- **事件通道可替换**：事件发布器定义为窄接口 `TurnEventSink`（XADD 语义），
  生产实现 Redis Stream，测试与 inline runner 用内存实现——沿用 outbox "narrow publisher seam" 风格；
- **Redis Stream 键**：`assistant:turn:{task_run_id}`，字段 `event`+`data(JSON)`，
  `MAXLEN ~5000`、`EXPIRE 600s`；断线重连从 `$` 之外的 last-id XREAD 补齐，最终以落库消息为准；
- **降级路径**：若所配型号/网关暂不支持流式输出，段2 退化为整段返回、SSE 一次性推 done
  （链路不变，仅无打字机效果），实现时以 `model_profiles` 实际能力定。

### 4.1 SSE 事件协议

| event | data | 时机 |
|---|---|---|
| `card` | `{kind: result|proposal, tool, payload}` | 段1 路由完成后（与 reply 同批） |
| `delta` | `{text}` | 纯问答段2 的文本增量 |
| `done` | `{message_id, task_run_id}` | 消息落库、任务终态 |
| `error` | `{message}` | 任务失败 |

前端用 `fetch` + `ReadableStream` 读取（`EventSource` 无法带 Authorization 头）。

## 5. 后端设计

### 5.1 端点

| 端点 | 语义 |
|---|---|
| `POST /api/v1/courses/{course_id}/assistant/turns` | 入新一轮：建 `assistant_messages(user)` + `task_run`，202 返回 `{task_run_id, user_message_id}` |
| `GET /api/v1/courses/{course_id}/assistant/turns/{task_run_id}/stream` | SSE 事件流（只读转发） |
| `GET /api/v1/courses/{course_id}/assistant/messages` | 历史恢复（时间序，含卡片与回执） |
| `PATCH /api/v1/courses/{course_id}/assistant/messages/{message_id}` | 仅回写 `action.status`（`proposed→executed/dismissed` 单向），不执行任何业务 |

提案确认不经过助手端点：**前端收到确认后直接调既有业务 API**（`POST /courses`、
`POST .../contracts/confirm`、`POST .../materials/{id}/parse`、既有 ai-suggest enqueue 等），
成功后 PATCH 回写卡片状态。助手的写能力上限 = 既有 API 能力，无第二套写路径。

路由注册归属：新增第 8 个 router `app/api/v1/assistant.py`（业务概念「AI 助手」独立成 router，
router 只做协议转换，意图解析与路由逻辑进 `app/services/assistant_service.py`）。

### 5.2 v1 工具清单

**只读（直接执行 → 结果卡）**

| tool | 数据来源 |
|---|---|
| `course_overview` | 课程 + 资料/框架/目录/蓝图/合同/试卷各阶段状态聚合 |
| `list_materials` | 既有 `material_service.list_materials` + 解析状态 |
| `framework_status` | 既有 `framework_service` 当前框架版本概览 |
| `blueprint_status` | 既有蓝图题位统计（题型/难度/分值分布） |
| `contract_status` | 既有 `GET .../contracts/current` 同源数据 |
| `paper_status` | 试卷版本/待复核题数 |
| `list_exam_projects` | 既有项目列表同源数据 |

> **点名定位**（教师问某个项目时卡片不罗列其它项目）：项目级只读工具
> （`course_overview`/`blueprint_status`/`contract_status`/`paper_status`/`list_exam_projects`）
> 收可选 `args={project_id}`，与提案共用 id 白名单硬校验（非法带反馈重试一次）；未点名
> 呈现全部项目。前端结果卡只含单项目时，CTA 深链 `?project={id}` 直达该项目试卷。

**提案（组装提案卡 → 确认调既有 API）**

| tool | 确认时调用 |
|---|---|
| `create_course` | `POST /api/v1/courses` |
| `update_course` | `PATCH /api/v1/courses/{id}` |
| `confirm_contract` | `POST .../contracts/confirm`（唯一落库动作：确定性分配 + 冻结，仅限未确认合同） |
| `start_parse` | `POST .../materials/{material_id}/parse` |
| `enqueue_blueprint_suggest` | 既有蓝图 AI 建议 enqueue（复用 task_runs 链路） |

### 5.3 意图解析的确定性约束

- **单步意图**：一轮一次路由决策，不做多步 agent 自主循环；
- **白名单硬校验**：模型回传的 id（course/material/project 等）必须命中段1 上下文注入的
  名称↔id 白名单，参数过显式 schema 校验；非法则带错误反馈重试一次，仍失败则落错误文案；
- **prompt 构成**：系统能力说明（工具清单与拒绝规则）+ 课程业务快照（名称与状态，
  不含出题比例/难度/去重规则——助手职责不涉及，禁止进入任何 prompt）+ 最近 10 条消息 + 用户输入；
- **注入防护**：资料名称等外部文本在 prompt 中标注为数据，指令优先级低于系统段；
  一切 id 以白名单校验为准（不信任模型输出）；
- **课程隔离**：上下文查询、消息读写、流端点全部带 `course_id` 过滤，跨课程对象一律拒绝。

## 6. 数据模型

### 6.1 新表 `assistant_messages`（schema.py + init_db 幂等迁移）

沿用 `_course_table` 公共列（`id`/`course_id`/`created_at`），业务列：

| 列 | 类型 | 说明 |
|---|---|---|
| `task_run_id` | String(64) FK task_runs.id | 产生本消息的轮次 |
| `role` | String(20) NOT NULL | `user` / `assistant` / `system` |
| `content` | Text NOT NULL default `''` | 文本正文（Markdown） |
| `action` | JSON | `{tool, args, payload, status}`，status ∈ `proposed/executed/dismissed/rejected` |
| `stream_status` | String(20) default `complete` | `streaming/complete/failed`（在途恢复判断） |

索引：`(course_id, created_at)`。`task_runs` 无需新列（payload/result JSON 已够）。

迁移验证：空库与既有库各执行一次 `uv run python -m app.db.init_db` 后 schema 一致（无 Alembic）。

## 7. 红线合规对照

| 红线 | 本设计的落点 |
|---|---|
| AI 只产提案/只读报告，不绕确认流 | 写操作全部提案卡，确认调既有 API；里程碑/删除类直接拒绝引导 |
| LLM 调用不进请求线程 | LLM 全部在 Celery worker（`assistant_turn` handler）；SSE 端点仅转发 Redis Stream |
| 外部系统只走 adapters/ | 流式与非流式模型调用都封装在 `adapters/model`（沿用 `JsonRequester` 与 model_profiles） |
| 课程隔离 course_id | 新表、全部端点、上下文快照、白名单校验均带 course_id |
| 不碰封存 graph | 不改 `framework/organization/generation_graph`；助手不参与出题链路 |
| 比例/难度/去重不进 prompt | 助手 prompt 不含任何出题约束规则 |
| 确定性算法保证约束 | 合同修订等写操作的业务语义仍由既有确定性算法执行，AI 只透传参数 |

## 8. 前端设计

| 文件 | 职责 |
|---|---|
| `src/pages/assistant/index.tsx` | 页面：消息流 + 输入框 + 卡片渲染器（结果卡/提案卡/回执） |
| `src/stores/assistantStore.ts` | Zustand：messages、sending、streamTaskId、恢复状态（一域一 store） |
| `src/api/domains/assistant.ts` | turns 创建、messages 拉取/回写、SSE fetch-stream 封装；注册进 `api/client.ts` |
| `src/types/` | 请求/消息/卡片类型手写镜像，禁 `any` |

SSE 读取封装：`fetch` + `ReadableStream` 按行解析 `event:/data:`，断线自动重连一次（从 last-id 续读）；
流结束后刷新对应消息状态。挂载恢复 effect 参照 `BlueprintSuggestPanel` 的防重复模式
（`restoredRef`/`startedRef` 同款思路）。

## 9. 测试策略与门禁

- **单测**（`tests/unit/test_assistant_service.py`）：
  意图路由三分支（纯问答/只读/提案）、id 白名单拒绝非法回传、重试一次后失败文案、
  提案组装不触碰业务写、`action.status` 单向回写校验、消息时间序恢复；
- **集成**（`tests/integration/test_assistant_api.py`）：
  turn 创建 → inline worker 执行 → 消息落库 → GET messages 恢复；
  SSE 端点事件顺序（注入内存 `TurnEventSink`）；跨课程访问 404；提案确认回写链路；
- **门禁**：`uv run pytest -q` + `--cov-fail-under=80`、`npm run build` 0 error、`npm run lint` warnings ≤ 10。

## 10. 已知取舍与风险

| 项 | 处置 |
|---|---|
| 型号网关流式能力未证实 | 实现首日验证 `model_profiles`；不支持则走 §4 降级路径（整段推），打字机延后 |
| 纯问答两段调用（成本×2、首字 ~2s） | 接受；查询/提案类单段即可。后续可评估合并为工具流式单段 |
| Redis Stream 测试依赖 | `TurnEventSink` 窄接口注入，测试用内存实现，不依赖真实 Redis |
| v1 不支持生成中停止 | 断开 SSE 不取消任务，回复照常落库；v2 加取消标记 |
| 单时间线无多会话 | v1 刻意收敛；v2 再评估 |
