# 统一首页 AI 助手 P0 核对与保护

> 日期：2026-11-13。状态：P0 核对完成；未新增 `/home`、未合并消息表、未改变课程或知识域的数据归属。
> 对应方案：[UNIFIED_HOME_ASSISTANT_DESIGN.md](./UNIFIED_HOME_ASSISTANT_DESIGN.md) 第 10 节 P0。
> 本文记录现状与本轮补齐项，不代表 P1 统一界面已经实施。

## 1. P0 结论

1. 课程助手与共享库问答可以复用同一套对话外观，但必须保留两套原生会话、授权和业务端点。
2. 课程助手的全部写操作最终仍转发既有业务 API，助手自身不直接写课程数据。
3. 共享库回答当前是**抽取式资料摘录**（`channel=extractive`），不是模型综合回答；统一界面必须按真实能力标注。
4. 本轮补齐了知识域“部分范围失效仍继续回答”的问题：创建会话和提问都会完整校验所选空间，任一失效即阻断。
5. 已存在的课程授权隔离、知识会话本人隔离、撤权后整条答案隐藏均可复用，不需要在 Adapter 重写权限。

## 2. 功能映射

| 领域 | 现有入口 | 主要前端实现 | 后端能力 | 统一后的接入方式 |
| --- | --- | --- | --- | --- |
| 课程助手 | `/courses/:courseId/assistant`（课程侧栏 AI 助手） | `pages/assistant/index.tsx`、`stores/assistantStore.ts`、`lib/assistantProposalExecution.ts` | `/api/v1/courses/{course_id}/assistant/**` | Course Adapter 复用原页面卡片、SSE、停止、接力与执行队列 |
| 课程资料问答 | 课程助手内 `answer_material_content` 工具 | 课程助手来源卡 | 课程 RAG 检索与回答 | 作为课程助手的一种只读能力保留 |
| 共享单库问答 | 空间页「问答」页签 | `pages/knowledge-center/SpacePage.tsx` | `/api/v1/knowledge/sessions/**` | Knowledge Adapter 复用原会话与来源结构 |
| 跨库问答 | 知识中心「跨库问答」弹窗（最多 5 个空间） | `pages/knowledge-center/index.tsx` | 同上，`selected_space_ids` 固定集合 | 范围选择器迁移后复用同一会话创建/提问接口 |
| 共享资料选用 | 课程资料页「从共享库导入」、共享空间「选用到课程」 | `pages/materials/index.tsx`、`pages/knowledge-center/SpacePage.tsx` | `/api/v1/knowledge/spaces/{space_id}/documents/{document_id}/versions/{version_id}/import-to-course` | 来源卡跳既有选用面板，固定版本确认后进入课程助手 |

能力边界：

- 课程助手有课程状态查询、资料问答、提案卡、教师确认、自动执行、出卷接力、生成进度、会话新建/重命名/删除。
- 共享问答有会话新建/恢复、提问、来源卡、反馈；当前没有重命名和删除端点，也没有模型综合回答。
- 首版不做同一轮混合课程与共享库检索，不跨多门课程批量执行。

## 3. 真实端点清单

### 3.1 课程助手（均挂 `require_owned_course`）

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/v1/courses/{course_id}/assistant/sessions` | 本人该课程会话列表 |
| POST | `/api/v1/courses/{course_id}/assistant/sessions` | 新建会话 |
| PATCH | `/api/v1/courses/{course_id}/assistant/sessions/{session_id}` | 重命名会话 |
| DELETE | `/api/v1/courses/{course_id}/assistant/sessions/{session_id}` | 删除会话（在途轮次按既有 409 规则） |
| POST | `/api/v1/courses/{course_id}/assistant/turns` | 发起一轮提问，创建任务 |
| GET | `/api/v1/courses/{course_id}/assistant/turns/{task_run_id}/stream` | SSE 流式回答 |
| POST | `/api/v1/courses/{course_id}/assistant/turns/{task_run_id}/cancel` | 停止当前回答 |
| POST | `/api/v1/courses/{course_id}/assistant/relays` | 出卷接力，不写教师消息 |
| POST | `/api/v1/courses/{course_id}/assistant/generation-reports` | 生成结束后由后端落助手消息 |
| GET | `/api/v1/courses/{course_id}/assistant/messages` | 恢复会话时间线 |
| PATCH | `/api/v1/courses/{course_id}/assistant/messages/{message_id}` | 回写提案卡状态 |

实现：[assistant.py](../backend/app/api/v1/assistant.py)。

### 3.2 共享库问答

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/api/v1/knowledge/search` | 授权检索，返回固定发布版本片段 |
| GET | `/api/v1/knowledge/sessions` | 本人知识会话列表 |
| POST | `/api/v1/knowledge/sessions` | 用选定空间集合建会话 |
| GET | `/api/v1/knowledge/sessions/{session_id}` | 读取会话，依赖重新授权后隐藏失效答案 |
| POST | `/api/v1/knowledge/sessions/{session_id}/ask` | 提问并生成抽取式回答 |
| POST | `/api/v1/knowledge/sessions/{session_id}/messages/{message_id}/feedback` | 本人反馈 |

相邻业务端点：

- `GET /api/v1/knowledge/course-materials`：可共享的课程资料与已共享版本。
- `POST /api/v1/knowledge/spaces/{space_id}/import-from-course`：课程资料共享到知识库。
- `POST /api/v1/knowledge/spaces/{space_id}/documents/{document_id}/versions/{version_id}/import-to-course`：固定版本选用到本人课程。

实现：[knowledge_base.py](../backend/app/api/v1/knowledge_base.py)、[knowledge_answer_service.py](../backend/app/services/knowledge_answer_service.py)、[knowledge_course_service.py](../backend/app/services/knowledge_course_service.py)。

## 4. 原工具执行策略

注册表：[assistant_tools.py](../backend/app/services/assistant_tools.py)。后端对模型参数做白名单硬校验，非法参数不会成为业务写操作。

| 策略 | 工具 | 行为 |
| --- | --- | --- |
| 只读 | `course_overview`、`usage_guide` | 读取当前课程快照，不写数据 |
| 课程 RAG | `answer_material_content` | 只用当前课程已解析资料，回答按检索片段生成 |
| 教师确认 | `create_course`、`update_course`、`start_parse`、`confirm_blueprint`、`confirm_contract` | 卡片显示「确认执行」，教师点击后才调用既有 API |
| 自动执行并接力 | `create_exam_project`、`update_exam_rules`、`create_blueprint`、`enqueue_blueprint_suggest`、`start_generation`、`update_question_type_format` | 卡片 `status=auto`，前端串行执行既有 API，成功后追问下一张卡 |
| 自动执行但不接力 | `enqueue_paper_review`、`enqueue_framework_review` | 只读评审任务，不入出卷接力链 |

目标绑定与幂等：

- `update_course` 的目标由后端固定为当前上下文 `course_id`，不取模型输出。
- 其余提案 payload 只含业务对象 id；前端执行时使用当前路由绑定的 `courseId`。
- 自动执行前先把卡片 claim 为 `executing` 落库，重复挂载/双击不会重复执行；失败停在 `executing` 供教师手动重试。
- 切换课程时有 `get().courseId !== courseId` 守卫，迟到结果不回写当前课程视图。
- `start_generation` 不在执行后立即接力，等生成结束再由 `generation-reports` 落收尾消息。

## 5. 旧链接兼容清单

| 链接/入口 | 现状 | 统一后要求 |
| --- | --- | --- |
| `/courses/:courseId/assistant` | 课程助手的正式深链 | 继续可用；切默认首页后保留重定向或直接打开统一界面并带课程范围 |
| `/knowledge` | 共享库首页，登录后默认兜底 | 继续可用；统一入口上线后仍可打开资料与知识中心 |
| `/knowledge/:spaceId` | 空间详情，含问答页签 | 保留；问答页签可跳统一界面并带该空间范围 |
| `/courses/:courseId/materials` | 课程资料与「从共享库导入」 | 保留；来源卡可跳到该面板完成选用 |
| `/courses/:courseId/knowledge` | 课程知识目录 | 保留，与共享库概念区分不变 |
| `ShareCourseMaterialModal` 的「前往预览与发布」 | `/knowledge/{spaceId}?fromCourse=&document=&sourceVersion=` | 保留 query 语义，统一界面需要能恢复目标空间与文档 |
| `*` 兜底 | `Navigate to="/knowledge"` | 切换默认首页时改为 `/home`，旧路径仍可解析 |

## 6. 授权用例与现状

### 6.1 课程域

- 所有课程助手端点统一挂 `require_owned_course`；他人课程与不存在统一 404，匿名 401。
- `admin` 身份不自动获得他人课程内容。
- 会话列表、消息读取、取消、SSE 均带 `course_id` 条件，不能靠 `session_id` 跨课程读取。
- 课程删除/切换后的在途轮次有服务端状态与前端 `courseId` 守卫。

已被 `tests/integration/test_course_access_matrix.py` 覆盖，本次 23 项通过。

### 6.2 知识域

- 会话按 `school_id + owner_id + status=active` 读取；空间 owner 与管理员不因此获得他人私人问答。
- 历史答案按 `dependency_versions` 重新授权；任一版本撤回、空间失权或时间窗失效时，整条答案置为 `hidden`，正文与引用一起消失。
- 来源打开前由详情接口再次授权，不返回私有源课程 id 或链接。
- 会话创建与提问都重新校验空间读权限；无任何可用空间时建会话仍返回 404，不暴露空间是否存在。

### 6.3 本轮补齐：范围变化阻断

原实现中，会话选定的部分空间被撤权后，`ask` 会静默过滤掉失权空间继续回答，教师看到的仍是“完整回答”，但检索范围已经缩小。这违反方案第 8.4 节。

现在 [knowledge_answer_service.py](../backend/app/services/knowledge_answer_service.py) 的 `_require_full_scope`：

- 创建会话：全部空间无权 → 404（沿用不暴露存在性的规则）；部分无权 → 409 `SCOPE_CHANGED`。
- 已存在会话提问：任一空间无权 → 409 `SCOPE_CHANGED`，且在写入用户消息之前阻断。
- 前端直接展示后端错误信息，不再出现静默缩小范围。

新增用例：

- `test_cross_space_qa_uses_only_authorized_spaces`：混合无权空间建会话返回 409。
- `test_revoked_space_blocks_ask_instead_of_shrinking_scope`：读者被移除后提问返回 409，历史不新增未回答的用户消息。

## 7. P0 复用清单

| 类别 | 复用内容 | 备注 |
| --- | --- | --- |
| 页面 | 课程助手页、知识中心、空间详情 | P1 只抽对话壳，不重写页面业务 |
| 输入 | 课程助手输入框：多行、Enter 发送、失败回填 | 统一输入框沿用同一交互 |
| 卡片 | 课程提案卡、结果卡、来源卡、引导卡 | 保留原始 message id 与领域 |
| 执行 | `assistantProposalExecution.ts`、自动执行队列 | 不在 Adapter 复制写路径 |
| 历史 | 课程会话 CRUD、知识会话列表/详情 | 摘要聚合另建只读端点 |
| 移动端 | 现有 Layout 与页面滚动 | P1 再做历史抽屉；本轮未单独验收移动端 |

## 8. 已知缺口（留给 P1/P2）

1. 知识会话重命名/删除端点已补齐；统一历史入口的管理 UI 仍待实施。
2. 共享回答仍是资料摘录；统一界面不得标注为模型综合回答，模型回答按 P2 单独建设。
3. 范围变化目前只有错误提示，没有统一的重新选范围面板与“用剩余范围新建会话”动作。
4. 本人历史摘要端点已补齐；统一首页尚未接入，历史列表 UI 仍待实施。
5. 提案 payload 已固化 `course_id`；统一首页接入时仍需保留卡片目标并在执行前校验。
6. `/home` 统一入口、范围选择器、历史抽屉和移动端布局均未实施。

## 9. 验证记录

- `uv run pytest tests/integration/test_knowledge_api.py tests/integration/test_knowledge_spaces_api.py tests/integration/test_knowledge_lifecycle.py tests/integration/test_knowledge_course_bridge.py -q`：46 passed。
- `uv run pytest tests/integration/test_course_access_matrix.py -q`：23 passed。
- 前端本轮未改代码，无需重新构建。

## 10. P1 进展（2026-11-13）

已完成的后端基础：

1. 知识会话补齐 `PATCH /api/v1/knowledge/sessions/{id}` 与 `DELETE /api/v1/knowledge/sessions/{id}`，仅本人可操作；删除为 `status=deleted` 软删除，详情、提问、反馈均不可访问。
2. 提问在检索结束后、写助手消息前重新锁定会话；检索期间被删除的会话不会因迟到结果复活，也不返回答案。
3. 课程提案 payload 固化 `course_id`（新建课程除外）；前端执行前校验卡片目标与当前课程一致，不一致直接拒绝。
4. 新增 `GET /api/v1/assistant/sessions`：聚合本人课程会话与知识会话，返回标题、领域、目标名称、原生会话引用与更新时间，不返回消息正文；支持领域/课程筛选和游标分页，失权空间不返回名称。
5. 新增 `/home` 统一首页：左侧本人历史（课程/共享筛选、重命名、删除、加载更多），右侧范围栏与对话；课程范围内嵌原课程助手（SSE、卡片、停止、接力全部保留），共享范围使用资料摘录问答、来源卡与反馈；范围变化时阻断并提示重新选择，切换范围自动进入新会话；移动端历史收进抽屉。
6. 默认登录入口与兜底路由改为 `/home`；课程侧栏、课程空间、知识中心均保留 AI 助手入口，原 `/knowledge`、`/courses/:id/assistant` 继续可用。

P2 模型综合回答已实现，默认关闭（`KNOWLEDGE_MODEL_ANSWER_ENABLED=false`）；未在真实模型与人工评测集上验收。

## 11. P2 模型综合回答（2026-11-13）

实现方式：

1. 开关与 LLM 配置同时可用时，`ask` 只写一条 `state=pending` 的助手消息，并在同一事务写入 `answer_question` 任务与 outbox；不占用同步请求线程。
2. knowledge worker 领取任务后：重新加载本人会话 → 完整校验选定空间 → 检索 → 调用模型 → 逐字校验引用 → 写入最终答案；任一空间失权、会话删除或租约丢失都不写答案。
3. 模型输出要求严格 JSON：`answered` 或 `insufficient_evidence`；引用必须逐字出现在对应片段，否则整条判定无效并降级为资料摘录，标记 `degraded=true`。
4. 前端轮询 `pending` 消息并在完成后停止；气泡标注「模型综合回答」或「资料摘录（模型不可用，已降级）」。统一首页与空间问答页均已接入。
5. 任务状态、fencing token、租约与 outbox 复用既有 knowledge 任务链路；单账号同时最多 2 个回答任务。

配置：`.env.example` 已补 `KNOWLEDGE_MODEL_ANSWER_ENABLED`、`KNOWLEDGE_MODEL_ANSWER_TIMEOUT_SECONDS`、`KNOWLEDGE_MODEL_ANSWER_MAX_CITATIONS`、`KNOWLEDGE_MODEL_ANSWER_MAX_CHARS`。

验证：新增 3 个集成用例（模型成功、模型失败降级、写入前撤权阻断）；知识域 53 项测试通过，前端构建与 lint 通过。尚未完成真实模型质量、引用准确率与人工评测集验收，开启前应先跑评测。
