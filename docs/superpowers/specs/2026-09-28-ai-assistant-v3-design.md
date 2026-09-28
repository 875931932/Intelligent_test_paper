# AI 助手 v3：多会话管理 + 停止生成 设计

- 日期：2026-09-28（v1 见 `2026-09-28-ai-assistant-chat-page-design.md`，v2 见 `2026-09-28-ai-assistant-rag-design.md`）
- 状态：设计定稿，实施中
- 用户决策（本轮）：① 会话**包含删除**（UI 按钮 + 二次确认；助手本身仍拒绝删除类操作）；
  ② 停止**保留已生成的部分正文**并打 `stopped` 标记（允许为此改 `stream_status` 的 CHECK 约束）。

## 1. 背景与范围

v1/v2 每个课程只有一条对话时间线（`assistant_messages` 按 `course_id` 单轨），
v1 断开 SSE 只是前端关流、任务照跑（v1 spec §2.2 明确列 v3）。本轮做两件事：

**v3 做**：

1. **多会话**：新建 / 切换 / 重命名 / 删除（二次确认）；会话内消息隔离，段1 对话历史按会话取。
2. **停止生成**：在途轮次可取消——取消端点只改任务状态（零 LLM），worker 协作式检查点中止
   流式，**保留已流出的部分正文**，`stream_status='stopped'` + 前端「已停止」徽标。

**v3 不做**：多会话并发在途（单课程同一时刻只允许一个在途轮次，前端禁用发送）；
跨会话记忆；助手工具新增任何会话操作（删除会话等危险操作助手仍拒绝，引导 UI 手动）。

## 2. 现状底子（复用，不重造）

| 已有 | 位置 | v3 用法 |
|---|---|---|
| `task_runs.status='cancelled'` + `cancel_task()` 条件更新 | `infrastructure/tasks/models.py` | 取消端点直接调用（queued/running/waiting_external → cancelled，终态不可变） |
| SSE `_probe_turn_state` 已把 cancelled 当终态 | `api/v1/assistant.py` | 现状发 `error`，v3 改发 `done`（用户主动停止不是错误） |
| Celery 桥 `dispatch_task` 忽略 `execute_task` 返回值 | `celery_app.py` | 取消后 `claim_task` 失败返回 False，**不重试不报错** |
| `complete_task` 要求 running+租约 | `models.py` | 被取消的轮次完成时写不进去，任务保持 cancelled（预期行为） |
| `stream_text` 失败不重试；`on_delta` 抛非 httpx 异常直接穿透 | `adapters/model/llm_gateway.py` | 检查点中止异常可安全上抛，`with stream_cm` 关连接即停上游生成 |
| `_DeltaBuffer` 聚批发布、不保留全文 | `assistant_service.py` | 部分正文由检查点闭包自攒（不碰缓冲内部） |
| `load_turn_context` 取近 10 条历史（course 级） | `assistant_service.py` | 加 `session_id` 过滤 |
| 迁移函数先例 + `test_database_bootstrap.py` | `db/init_db.py` | 新迁移同款幂等写法与测试 |

## 3. 数据模型（schema.py + init_db，唯一入口；无 Alembic）

```python
# 新表：课程内会话（一条会话 = 一条时间线）
assistant_sessions = _course_table(
    "assistant_sessions",
    Column("title", String(80), nullable=False, default="新会话", server_default="新会话"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# assistant_messages 增一列（应用层恒写；历史任务 payload 缺 session_id 时允许 NULL）
Column("session_id", String(64), ForeignKey("assistant_sessions.id"), nullable=True),
# CHECK 约束：stream_status 增 'stopped'
Index("ix_assistant_messages_course_session",
      assistant_messages.c.course_id, assistant_messages.c.session_id, assistant_messages.c.created_at),
```

- `nullable=True` 的原因：部署瞬间在途的旧任务 payload 无 `session_id`，其失败消息写 NULL
  （孤儿行只出现在不带过滤的历史查询里，前端从不走该路径）；**新写入一律带值**。
- 不加 title 唯一约束（会话允许重名，避免 409 复杂度）。

**迁移 `_migrate_assistant_session(engine)`（幂等，注册进既有两处调用点）**：

1. 内省缺表 → `create_all` 已建；缺 `session_id` 列 → `ALTER TABLE assistant_messages ADD COLUMN session_id VARCHAR(64)`（双方言言）。
2. **回填**：对每个有消息但 `session_id IS NULL` 的课程，建一条会话
   （title = 该课程首条 user 消息前 24 字，无则「历史会话」），`UPDATE ... SET session_id`。
3. `CREATE INDEX IF NOT EXISTS ix_assistant_messages_course_session`（双方言）。
4. CHECK 约束加 `'stopped'`：
   - **PostgreSQL**：`DROP CONSTRAINT ck_assistant_messages_stream_status` + `ADD CONSTRAINT ... IN ('streaming','complete','failed','stopped')`（先例：`_migrate_evidence_link_fk`）。
   - **SQLite**：**跳过**（先例同款注释：约束不可原地改；测试全为新建库自动带新 CHECK，
     开发库为 PostgreSQL）。老 SQLite 库若需继续用需重建，文档注明。

## 4. API 契约（前缀同 v1，全部带登录 + course_id 隔离）

| 端点 | 请求 → 响应 | 说明 |
|---|---|---|
| `GET /assistant/sessions` | → `[{id, title, created_at, updated_at}]` | `updated_at DESC`（消息插入与改名时 bump） |
| `POST /assistant/sessions` | `{title?}` → **201** session | 缺省「新会话」；title ≤40 字（服务端截断） |
| `PATCH /assistant/sessions/{sid}` | `{title}` → **200** session | 非空 ≤40 字，否则 422；404 不存在/跨课程 |
| `DELETE /assistant/sessions/{sid}` | → **204** | 级联删该会话消息；**会话内有在途 assistant_turn → 409**（防 worker 写入已删会话撞 FK）；404 跨课程 |
| `POST /assistant/turns` | `{message, session_id?}` → **202** `{task_run_id, user_message_id}` | `session_id` 缺省 → 取课程最近会话、无则建「新会话」（兜底，保旧客户端/测试）；跨课程 session → 404 |
| `GET /assistant/messages` | `?session_id=` → 时间序 | **缺省仍返回全课程**（向后兼容）；带则只返回该会话 |
| `POST /assistant/turns/{task_run_id}/cancel` | → **200** `{task_run_id, status}` | 幂等：已终态原样返回现态（不再更新）；404 不存在/跨课程。**纯 DB 更新，零 LLM** |
| `GET .../turns/{id}/stream` | SSE | DB 兜底探测：`cancelled` → 发 **`done`**（`message_id` 可为 null）而非 `error`；failed/missing 不变 |

删除会话的在途判定：`task_runs` 中 `task_type='assistant_turn'`、`status NOT IN 终态`、
`payload['session_id'] == sid`（SQLAlchemy JSON 取值双方言可译）→ 409「会话内仍有在途对话，
请先停止后再删除」。

## 5. 停止机制：三检查点 + 协作式中止

```python
class TurnCancelled(Exception):   # 携带已流出的部分正文
    def __init__(self, partial: str = "") -> None: ...

def _turn_cancelled(session, *, course_id, task_id) -> bool:
    """读 task_runs.status == 'cancelled'；带 course_id；探测异常按未取消处理（不阻断轮次）。"""

class _CancelProbe:               # 节流：默认 0.5s 一次 DB 探测（流式高频回调不打爆库）
    def cancelled(self) -> bool: ...
```

探测用 **worker 自己的 session**（run_turn 已持有）：Postgres READ COMMITTED 每条语句取新
快照、SQLite SELECT 自动提交，均能读到取消端点已提交的更新；不新建连接、不需要 Redis。

| 检查点 | 位置 | 命中行为 |
|---|---|---|
| **A 轮次开始** | `run_turn` 幂等检查之后、`parse_intent` 之前 | **直接返回** `{cancelled: True}`：不调模型、不落消息（与「取消先于领取」一致——还没产出就不留痕），SSE 由 DB 兜底发 done |
| **B 流式中** | `on_delta` 包装（chat 与 RAG 两处）：闭包自攒 `partial`，节流探测 → `raise TurnCancelled(partial)` | 异常穿透 `stream_text`（非 httpx 异常不被吞、不重试），在 `run_turn` 的 `except TurnCancelled`（**必须排在既有 `except Exception` 降级之前**）接住：`content=partial`、`stream_status='stopped'`，`buffer.flush()` 后走既有落库/发卡/done 流程 |
| **C 落库前** | `_insert_message` 之前一处 | `stream_status='stopped'`（覆盖段1 已完成但流式未开始/已结束的窗口：非流式回复、结果卡、提案卡照常落，只改标记） |

- 落库后 `sink.publish("done")` 照旧 → 前端收口刷新即见「已停止」徽标。
- 任务行保持 `cancelled`（`complete_task` 写不进 → `execute_task` 返回 False → 桥不重试）。
- **竞态接受**：检查点 C 与取消的毫秒级窗口内可能落成 `complete`（良性）；取消后立刻删会话
  可能让 worker 落库撞 FK——落库异常已有兜底（`execute_turn_task` 捕获 + `_persist_failed_message`
  自带告警回滚），删会话本身已要求任务终态，窗口极小，不加锁。
- 段1 `request_json`（非流式、有超时）期间的取消不中断该次调用，由检查点 B/C 接住。

## 6. 会话语义（写路径）

- `enqueue_turn(..., session_id=None)`：
  1. 解析会话：给定 → 校验属课程（否则 404）；缺省 → 课程最近会话，无则建（兜底路径）。
  2. **幂等键 `_task_key(course_id, session_id, message)`**：同一句话在两个会话是两个任务。
  3. 写 user 消息带 `session_id`；若会话 title 仍是「新会话」→ 自动改为该消息前 24 字；
     bump 会话 `updated_at`。
- `_insert_message` 增 `session_id` 参数；assistant 回复沿用 payload 里的 `session_id`，
  并 bump 会话 `updated_at`（列表按最近活跃排序）。
- `load_turn_context(..., session_id=None)`：段1 历史 `.where(session_id == X)`（缺省不过滤，
  兼容旧 payload）——**会话是记忆边界**：切换会话 = 切换上下文。
- `_persist_failed_message`：从 payload 取 `session_id`（无则 NULL，见 §3）。
- 删除会话：单事务 `DELETE messages WHERE session_id+course_id` → `DELETE session`，
  course_id 双条件（隔离红线）。

## 7. 前端

**类型**（`types/api.ts`）：`AssistantSession {id,title,created_at,updated_at}`；
`AssistantStreamStatus` 增 `'stopped'`；`AssistantTurnRequest` 无变化（send 参数在 api 客户端）。

**API 客户端**（`api/domains/assistant.ts`）：`listSessions/createSession/renameSession/
deleteSession/cancelTurn`；`listMessages(courseId, sessionId?)`；`createTurn` 带 `session_id?`。

**Store**（`assistantStore.ts`，一域一 store）：

- 新状态：`sessions`、`activeSessionId`（course 维度持久化 `localStorage:
  assistant:{courseId}:session`，切课 reset 清空）、`streamSessionId`（在途轮次归属）。
- `restore(courseId)`：拉会话 → 解析 active（localStorage 有效则用之，否则最近会话，否则 null）
  → 拉 active 消息 → 末条 user 且属 active 则重连 SSE。
- `switchSession(sid)`：换 active → 拉该会话消息（失败**回滚**原会话与原时间线快照，
  否则停在空时间线且同 id 点击被早退拦掉、无法重试）→ `attachSessionStream`。
  ⚠️ 实现定稿（对初稿的偏离，根因：**切会话时关流会丢失 `done` 事件 → `sending`
  永久卡死**）：在途流**不关**——流式通道是全局的（只有一条，单在途），页面按
  `streamSessionId === activeSessionId` 归属决定是否显示流式区（POST 瞬态
  `streamSessionId` 为空也显示）；续流只看通道是否断开（页面重挂载 `stop()` 关过），
  不看会话归属。滚到底部的 effect 同样按归属门控，防止他会话在途时不断拽动视口。
- `createSession()`：建并置 active、清时间线、持久化；`renameSession`/`deleteSession`
  （删后若删的是 active → 置最近会话或 null）。
- `send(text)`：无 active 会话先 `createSession` 再发（单路径，不设禁用态）；
  携带 `session_id`；收口时 `streamSessionId = turn.session_id`。
- **新 `cancelTurn()`**（与 v1 `stop()`「只关流」分离）：POST cancel → 立即 toast
  （「已停止生成（已生成的内容会保留）」）；**不立刻关流刷新**（初稿的「关流 → finishTurn」
  会抢在 worker 检查点 B/C 落库（≤0.5s 节流）之前刷新，丢掉部分正文的可见性）——
  流开着则由 worker 落库后发的 `done` 自然收口（`finishTurn` 拉当前 active 会话消息，
  见部分正文 +「已停止」徽标）；流已关（罕见）则延迟 700ms 手动 `finishTurn()`。
  响应 `status !== 'cancelled'`（已终态/已跑完）→ 立即 `finishTurn()` + toast「未受影响」。
- `settledTaskIds`：每次收口记账 `streamTaskId`（内存态，截断 50）——「只剩提问」的
  已收口轮次（停止于产出前，如检查点 A）在进入会话时不再被重连，避免反复
  resume→done→刷新 的闪烁循环。
- **单在途约束**：`sending === true` 时输入框禁用（跨会话同禁）——store 只有一条流式通道，
  v3 不做并发在途（§1 不做项）。

**页面**（`pages/assistant/index.tsx`）：

- 页头右侧**会话选择器**：下拉列出会话（标题 + 相对时间），行内 ✏️ 改名（行内输入框，
  Enter/失焦提交、Esc 取消）、🗑 删除（行内二次确认「确认删除？」是/否，不引新组件）；
  顶部「＋ 新对话」。
- `activeSessionId === null`（无会话）：空状态只给「开始新对话」主按钮。
- **停止按钮**：`sending` 时输入框 Send 旁显示 Stop（lucide `Square`），onClick `cancelTurn()`。
- **已停止徽标**：`stream_status === 'stopped'` 的 assistant 消息显示「已停止」徽标
  （与既有 failed 徽标并列判定，任何 kind）。

## 8. 测试计划（pytest；前端 build+lint）

| 层 | 用例要点 |
|---|---|
| unit `test_assistant_service.py` | 会话 CRUD 服务（建/改名/删/在途 409/跨课程 404/级联删）；enqueue 带会话（幂等键分会话、自动标题、兜底建会话）；`load_turn_context` 历史按会话过滤；检查点 A（task_runs 置 cancelled → 不调模型不落消息）；检查点 B（流式中置 cancelled → partial 落库 `stopped` + done 事件 + 不走 failed 降级）；检查点 C（流后置 cancelled → `stopped`）；未取消路径回归（既有 35 测） |
| unit `test_database_bootstrap.py` | `_migrate_assistant_session`：缺列补列、回填建会话、幂等重跑、索引存在（SQLite 可测部分；CHECK 换 Postgres-only 以注释标注） |
| integration `test_assistant_api.py` | sessions 端点全生命周期 + 404/409/422；turns 带/不带 session_id；`messages?session_id` 过滤与缺省全量；cancel：404、终态幂等、取消后 `execute_task` 返回 False 且不落消息；SSE 兜底 cancelled→done（可直测 `_event_stream`/探测分支）；停后重发同文本换新键 |
| 既有回归 | 助手全量、任务全量、`pytest -q` 全绿 + cov ≥80 |

## 9. 红线自查

1. **零 LLM 进请求线程**：cancel/sessions/turns 端点全部纯 DB 或入队；模型调用仍只在 worker
   （新检查点只读状态）。
2. **course_id 隔离**：`assistant_sessions` 走 `_course_table`；消息/会话查询双条件；
   取消探测带 course_id；删除级联双条件。
3. **AI 只产提案/只读不绕确认流**：停止与会话是教师 UI 直操作，不进助手工具表；
   助手删除类拒绝话术不变。
4. **封存 graph 不碰**；`比例/难度/去重` 不进 prompt（本轮不涉 prompt 改动，仅历史取数范围变化）。
5. **schema.py + init_db 唯一入口**：新表 + 加列 + 索引 + CHECK 迁移均幂等，空库/老库各跑一致；
   无 Alembic。
6. 外部系统不新增（纯 DB；Redis 既有通道不改协议，只改 DB 兜底映射）。

## 10. 风险与取舍

| 风险 | 处置 |
|---|---|
| 老 SQLite 库 CHECK 不可原地改 | 按先例跳过；测试全为新建库；文档注明需重建（生产为 PostgreSQL，有 DROP/ADD 先例） |
| 流式高频探测压库 | `_CancelProbe` 0.5s 节流；一轮至多数次探测 |
| 取消/删除竞态致 worker 落库撞 FK | 已分析为良性（删会话要求任务终态 + 落库异常自带兜底告警），不加锁 |
| 单在途限制（跨会话互斥发送） | 显式接受并写进 §1；store 单流通道，做并发要重构 SSE 多路复用，留待以后 |
| 检查点 A 后 `message_id=null` 的 done | 前端 `finishTurn` 以 GET messages 为权威，天然兼容 |
| 部署瞬间旧 payload 无 session_id | 列可空 + 失败消息允许 NULL 孤儿行（前端不渲染该路径） |
