# AI 期末试卷系统核心一期开发设计

> 状态：已确认的开发设计基线  
> 版本：0.3
> 更新日期：2026-08-14  
> 上游产品设计：`docs/superpowers/specs/2026-08-12-ai-final-exam-paper-design.md`  
> 本版范围：核心一期的系统架构、领域对象、MinerU 文档解析、知识树、API、后台任务和 LangGraph 工作流

## 1. 文档目的

本文将已经确认的产品设计转化为可用于正式开发的技术基线。正式系统不继续扩展当前 `prototype/server.py`，而是新建生产工程；原型只作为模型效果、提示词和回归样例的参考。

核心一期优先打通：

```text
多课程空间
  → 上传教学大纲、考核大纲和教学资料
  → MinerU 文档解析
  → 大纲命题框架确认
  → 大纲约束下的资料提炼
  → 候选知识树确认并发布
  → 考试蓝图确认
  → 按题型生成候选试题
  → 质量检查与局部重生成
  → 结构化候选卷审核
```

核心一期完成后，教师应能在不同课程空间内独立完成上述流程，且出题模型只接收当前题位对应的纯净知识卡，不接收文件名、页码、原始证据、来源关系或业务 ID。

## 2. 已确认范围

### 2.1 核心一期包含

- 单教师运行，使用固定内置教师身份；
- 支持创建多门课程，所有业务数据通过 `course_id` 隔离；
- 大纲区与教学资料区分开上传；
- 上传文件先进入暂存区，不自动调用 MinerU、DeepSeek 或构建索引；
- 教学大纲、考核大纲独立解析并生成教师确认的命题框架；
- 教学资料按已确认框架逐文件解析、提炼和映射；
- 建立并确认“考核章节—知识主题—考核单元—纯净知识卡”知识树；
- 发布版本化知识目录和检索索引；
- 基础表单生成版本化蓝图和逐题 `PlanItem`；
- 使用 DeepSeek 生成选择、判断、填空、简答等候选题；
- 确定性检查、语义审校、最多两次局部修复；
- 结构化查看、修改、采用、拒绝、排序和单题重生成；
- 模型调用、解析任务、错误和版本可追踪。

### 2.2 核心一期暂缓

- 登录、注册、角色、管理员后台和复杂权限；
- 个性化对话出题入口；
- 正式富文本编辑器；
- Word/PDF 学生卷、答卷和答案评分细则导出；
- 历史试卷复用、复杂题库管理和跨试卷语义查重；
- 自动生成图片、图表或题面插图；
- 将图片视觉语义直接作为自动命题事实；
- 微服务拆分和 Kubernetes 部署。

### 2.3 不可破坏的业务约束

1. 考核大纲决定期末考试范围、章节权重、能力和题型规则。
2. 教学大纲用于验证已教内容和教学深度，不能单独扩大考试范围。
3. 教学资料只提供本课程实际知识、条件、案例和评分依据。
4. 无教师确认答案的作业只能提供覆盖、题型、场景、常见错误和难度信号。
5. 上传不触发解析；教师主动创建框架构建或资料整理运行后才调用 MinerU 和模型。
6. 候选数据未经教师确认不得进入当前可出题知识库。
7. 生成运行必须冻结框架版本、知识目录版本、索引版本、蓝图版本和模板版本。
8. 纯净知识卡与来源关系分开保存。
9. 出题模型不得接收任何来源元数据、原始证据或业务 ID。
10. 单个文件或单道题失败不能无条件中断整个批次，但硬约束错误必须在调用模型前阻断。

## 3. 总体架构

### 3.1 架构选择

采用“核心垂直闭环 + 模块化单体”。FastAPI 应用、后台 Worker 和 React 教师端可独立部署，但共享同一套领域模型和 PostgreSQL 数据库。核心一期不拆微服务。

```mermaid
flowchart LR
  WEB["React 教师端"] --> API["FastAPI 应用"]
  API --> DB[("PostgreSQL + pgvector")]
  API --> OBJ[("MinIO 对象存储")]
  API --> REDIS[("Redis")]

  REDIS --> WORKER["Celery Worker"]
  WORKER --> PARSER["文档解析适配器"]
  PARSER --> MINERU["MinerU 精准解析 API"]
  WORKER --> GRAPH["LangGraph 工作流"]
  GRAPH --> LLM["DeepSeek 模型网关"]

  WORKER --> DB
  WORKER --> OBJ
```

### 3.2 推荐技术组合

| 层级 | 技术 |
|---|---|
| 前端 | React、TypeScript、Vite；具体组件库在前端设计章节确定 |
| API | Python 3.12、FastAPI、Pydantic v2 |
| ORM/数据库初始化 | SQLAlchemy 2；核心一期使用空库初始化脚本，后续结构演进再引入 Alembic |
| 业务数据库 | PostgreSQL 16 |
| 向量索引 | pgvector |
| 对象存储 | MinIO；生产可替换为 S3 兼容服务 |
| 异步任务 | Redis、Celery |
| 工作流 | LangGraph，使用持久化 checkpointer |
| 文档解析 | MinerU 精准解析 API；TXT/Markdown 使用本地确定性解析器 |
| 生成模型 | OpenAI 兼容模型网关，核心一期默认 `deepseek-v4-flash` |
| 测试 | pytest、pytest-asyncio、httpx、testcontainers 或 Docker Compose |

### 3.3 逻辑模块

| 模块 | 责任 | 明确禁止 |
|---|---|---|
| `course` | 课程空间、课程状态、固定教师归属 | 不处理文件解析和生成 |
| `material` | 上传会话、文件版本、删除状态、对象存储 | 上传完成后不得自动调用模型 |
| `document_processing` | MinerU、本地解析器、产物归一化 | 不判断考试范围，不生成知识点 |
| `framework` | 双大纲抽取、合并、冲突、教师确认、版本失效 | 不读取教学资料补全大纲缺失 |
| `knowledge` | 资料映射、知识树、知识卡、来源关系、索引发布 | 不创建考核大纲未准入的考试范围 |
| `blueprint` | 题型、分值、章节权重、逐题计划 | 不直接生成题目 |
| `generation` | 模型输入编译、题目生成、质检和局部修复 | 不向模型发送来源字段 |
| `paper` | 候选卷、题目采用、编辑、排序、版本 | 不把 HTML 作为唯一事实来源 |
| `model_gateway` | DeepSeek 调用、结构化输出、重试、诊断、用量 | 不包含课程业务判断 |
| `tasking` | 数据库任务、Outbox、幂等、租约、取消 | Redis 消息不能成为任务事实来源 |

## 4. 数据设计

### 4.1 关系概览

```mermaid
erDiagram
  COURSE ||--o{ MATERIAL_VERSION : contains
  COURSE ||--o{ FRAMEWORK_VERSION : owns
  FRAMEWORK_VERSION ||--o{ FRAMEWORK_ANCHOR : contains
  MATERIAL_VERSION ||--o{ CONTENT_BLOCK : parsed_into
  FRAMEWORK_VERSION ||--o{ KNOWLEDGE_CATALOG_VERSION : constrains
  KNOWLEDGE_CATALOG_VERSION ||--o{ CONTENT_DOMAIN : contains
  CONTENT_DOMAIN ||--o{ ASSESSMENT_UNIT : contains
  ASSESSMENT_UNIT ||--o{ KNOWLEDGE_CARD : described_by
  KNOWLEDGE_CARD ||--o{ KNOWLEDGE_EVIDENCE_LINK : traced_by
  CONTENT_BLOCK ||--o{ EVIDENCE_CHUNK : produces
  EVIDENCE_CHUNK ||--o{ KNOWLEDGE_EVIDENCE_LINK : supports
  KNOWLEDGE_CATALOG_VERSION ||--|| INDEX_VERSION : published_with
  COURSE ||--o{ EXAM_PROJECT : owns
  EXAM_PROJECT ||--o{ BLUEPRINT_VERSION : versions
  BLUEPRINT_VERSION ||--o{ PLAN_ITEM : allocates
  PLAN_ITEM ||--o{ GENERATED_QUESTION : generates
  EXAM_PROJECT ||--o{ PAPER_VERSION : versions
```

### 4.2 核心数据表

| 模块 | 数据表 |
|---|---|
| 课程 | `courses` |
| 文件 | `materials`、`material_versions`、`upload_sessions` |
| 解析 | `parser_profiles`、`document_parse_runs`、`document_artifacts`、`content_blocks` |
| 命题框架 | `framework_build_runs`、`framework_versions`、`framework_anchors`、`framework_conflicts` |
| 资料整理 | `organization_runs`、`evidence_chunks` |
| 知识树 | `knowledge_catalog_versions`、`content_domains`、`assessment_units`、`knowledge_cards`、`knowledge_evidence_links` |
| 索引 | `index_versions`、`index_memberships` |
| 蓝图 | `exam_projects`、`blueprint_versions`、`blueprint_sections`、`plan_items` |
| 生成 | `generation_runs`、`generation_attempts`、`generated_questions`、`quality_checks` |
| 试卷 | `paper_versions`、`paper_items` |
| 模型与任务 | `model_calls`、`task_runs`、`outbox_events` |

所有课程业务表都必须含 `course_id`。首期使用固定 `owner_id`，以后补充认证时不迁移业务主键。

### 4.3 纯净知识卡

`knowledge_cards` 是出题模型与知识库之间的边界：

```text
KnowledgeCard
  ├─ id                         后端 ID，不进入模型
  ├─ course_id
  ├─ catalog_version_id
  ├─ assessment_unit_id
  ├─ name
  ├─ performance_statement
  ├─ assessable_content         JSONB
  ├─ scope_boundary             JSONB
  ├─ cognitive_targets          JSONB
  ├─ allowed_question_types     JSONB
  ├─ importance
  ├─ content_hash
  ├─ status
  └─ version
```

来源关系保存到独立关联表：

```text
KnowledgeEvidenceLink
  ├─ knowledge_card_id
  ├─ evidence_chunk_id
  ├─ evidence_role
  ├─ confidence
  ├─ teacher_confirmed
  └─ lifecycle_status
```

生成模型只读取由 `KnowledgeCard` 编译出的白名单载荷。教师点击“查看依据”时，后端才联查 `KnowledgeEvidenceLink → EvidenceChunk → MaterialVersion`。

### 4.4 四层版本冻结

每次生成运行至少冻结：

```text
GenerationRun
  ├─ framework_version_id
  ├─ catalog_version_id
  ├─ index_version_id
  ├─ blueprint_version_id
  └─ prompt_template_version
```

运行开始后发布的新大纲、知识树、索引或蓝图只影响新运行，不得静默修改正在生成或已经生成的候选卷。

## 5. MinerU 文档解析设计

### 5.1 接入方式

MinerU 通过 `DocumentParser` 端口接入。业务流程只依赖统一解析契约，不能在 `framework` 或 `knowledge` 模块中直接请求 MinerU。

```python
class DocumentParser(Protocol):
    def submit(self, request: ParseRequest) -> ParseSubmission: ...
    def poll(self, provider_task_id: str) -> ParseProgress: ...
    def fetch(self, provider_task_id: str) -> ParseArtifact: ...
```

核心一期解析策略：

| 文件类型 | 解析器 | 默认模式 |
|---|---|---|
| PDF、DOC/DOCX、PPT/PPTX、XLS/XLSX、扫描件、图片 | MinerU 精准解析 API | `vlm`，允许配置为 `pipeline` |
| TXT、Markdown | 本地确定性解析器 | 本地文本解析 |
| HTML | 核心一期不接收 | 后续可使用 `MinerU-HTML` |

MinerU 官方接口限制通过 `parser_profile` 配置保存，不散落在业务代码中。当前设计基于官方文档中的 200 MB、200 页限制，以及批量申请上传链接每次最多 50 个文件的规则。

### 5.2 主动触发原则

上传完成后文件状态为 `staged`，不会自动调用 MinerU。以下业务动作会按需确保解析完成：

- 创建 `FrameworkBuildRun` 时解析选中的教学大纲和考核大纲；
- 创建 `OrganizationRun` 时解析选中的教学资料；
- 已存在相同文件哈希和相同 `parser_profile` 的成功产物时直接复用。

### 5.3 MinerU 调用流程

```mermaid
sequenceDiagram
  participant W as Worker
  participant O as MinIO
  participant M as MinerU
  participant D as PostgreSQL

  W->>D: 创建 DocumentParseRun
  W->>M: POST /api/v4/file-urls/batch
  M-->>W: batch_id + file_urls
  W->>O: 流式读取原文件
  W->>M: PUT 临时上传地址
  loop 轮询
    W->>M: GET /api/v4/extract-results/batch/{batch_id}
    M-->>W: waiting-file/pending/running/converting/done/failed
  end
  W->>M: 下载 full_zip_url
  W->>O: 保存 ZIP 和解析产物
  W->>D: 归一化 ContentBlock 并标记 ready
```

请求使用清理后的文件名，并将 `material_version_id` 作为 MinerU `data_id`。Token 只存在于服务端密钥配置中。

### 5.4 解析产物

完成后优先读取：

1. `content_list.json`：结构化内容的主要输入；
2. `full.md`：教师预览和辅助降级读取；
3. `middle.json`：版面定位和解析诊断；
4. 图片、表格截图及其他资源：保存为内部对象，但不自动成为知识事实。

归一化结果：

```text
ContentBlock
  ├─ id
  ├─ course_id
  ├─ material_version_id
  ├─ parser_profile_id
  ├─ block_type: title | paragraph | list | table | equation | code | image | metadata
  ├─ text
  ├─ markdown
  ├─ latex
  ├─ page_index
  ├─ bbox
  ├─ heading_path
  ├─ asset_reference
  ├─ reading_order
  └─ content_hash
```

### 5.5 安全和失败边界

- MinerU Token、临时上传 URL 和结果 URL 不进入前端或普通日志；
- `full_zip_url` 下载后只保存内部对象存储地址；
- ZIP 解压检查路径穿越、文件数量、解压后总大小和压缩比；
- 网络错误最多自动重试 3 次；
- 相同幂等键不得创建重复解析记录；
- `failed` 结果显示提供方错误摘要和 `trace_id`，不自动伪造文本；
- MinerU 成功只表示取得结构化内容，不能绕过知识准入和教师确认；
- 云端解析会把课程文件发送给外部服务，首次启用时必须向教师说明。

## 6. 知识树设计

### 6.1 树的业务含义

知识树不是文件目录，也不是把所有标题排列成树。它是蓝图和生成之间的稳定课程考核目录：

```text
L0 课程
└─ L1 考核章节/考试内容域
   └─ L2 知识主题
      └─ L3 考核单元
         └─ L4 纯净知识卡
```

| 层级 | 来源和作用 |
|---|---|
| L0 | 当前课程空间 |
| L1 | 考核大纲的期末考试章节、范围和权重 |
| L2 | 考核大纲骨架经教学大纲校验和补充后的主题结构 |
| L3 | 学生应完成的可评分表现，是蓝图题位的主要分配对象 |
| L4 | 支撑具体题目、答案和评分点的纯净课程事实 |

证据不是树节点。文件名、页码、内容块 ID 和原文通过 `KnowledgeEvidenceLink` 挂在知识卡侧面。

### 6.2 构建流程

```mermaid
flowchart TD
  A["考核大纲结构化结果"] --> B["建立 L1 考核章节骨架"]
  C["教学大纲结构化结果"] --> D["校验并补充 L2 教学主题"]
  B --> E["形成框架知识树骨架"]
  D --> E
  E --> F["逐文件提取 L2/L3/L4 候选"]
  F --> G["术语标准化与噪声过滤"]
  G --> H["按考核锚点分组"]
  H --> I["组内聚类、去重和小批归并"]
  I --> J["跨文件同义候选合并"]
  J --> K["建立父子关系"]
  K --> L["结构与证据准入检查"]
  L --> M["候选树与版本差异"]
  M --> N["教师确认"]
  N --> O["发布 KnowledgeCatalogVersion"]
  O --> P["同步发布 IndexVersion"]
```

### 6.3 大纲骨架

考核大纲先建立 L1，至少包含：章节名称、考试范围、章节权重、能力要求、允许题型、排除项和考核大纲锚点。

教学大纲可以：

- 对应到现有 L1；
- 补充 L2 主题；
- 标明已讲内容、重点难点和教学深度；
- 产生与考核大纲的冲突报告。

教学大纲、课件和作业均不能静默创建新的 L1 考试章节。

### 6.4 逐文件候选提取

每个文件独立调用语义模型，输出小型结构化候选：

```json
{
  "framework_anchor": "已匹配的考核范围",
  "content_domain_candidate": "知识主题",
  "assessment_unit_candidate": {
    "name": "在给定约束下比较两种方法",
    "performance_statement": "学生能够根据条件选择方法并解释原因",
    "cognitive_level": "analyze"
  },
  "knowledge_cards": [
    {
      "name": "纯净知识点名称",
      "assessable_content": ["课程事实、条件、关系或评分边界"]
    }
  ],
  "source_links": []
}
```

文件名、页码和内容块 ID 只允许进入 `source_links`，不得进入候选节点名称或 `assessable_content`。

### 6.5 聚类和归并

不能把所有文件正文交给一个全局模型。归并流程为：

1. 按考核大纲锚点分组；
2. 使用术语词典、关键词、内容哈希和向量相似度形成候选簇；
3. 每个候选簇只向归并模型发送候选名称、表现声明和纯净内容摘要；
4. 模型判断同义、包含、拆分、素材或冲突关系；
5. 确定性代码创建 L2-L3-L4 父子关系；
6. 对跨批次结果执行标准名称、内容哈希和语义相似度二次去重；
7. 结构校验不通过的节点进入教师待审，不自动发布。

### 6.6 节点准入

每个 L3 考核单元必须：

- 关联至少一个考核大纲范围锚点；
- 包含明确认知动作和可评分表现；
- 保存可命题与不可命题边界；
- 至少包含一张有效知识卡。

每张 L4 知识卡必须：

- 至少关联一条允许作为事实或评分依据的当前课程资料；
- 内容能够独立判断、解释、应用或评分；
- 不包含文件名、页码、实验编号和来源描述；
- 不以安装、下载、截图、提交等操作指令作为唯一内容；
- 不使用模型参数知识补全课程中不存在的事实。

不能准入的内容分类为：

```text
material_only
style_reference
unmatched
needs_teacher_review
excluded
```

### 6.7 教师确认与发布

首次发布要求教师审核完整树，支持合并、拆分、改名、同一考核章节内移动、标重点、标不考、降级为素材和查看来源。

教师不能把节点移动到考核大纲范围外。确需扩大考试范围时，必须先创建并确认新的命题框架版本。

后续资料整理默认展示版本差异：新增节点、内容变化、证据变化、失效节点、建议合并和受删除资料影响。发布时在同一事务中写入：

```text
KnowledgeCatalogVersion
+ ContentDomain
+ AssessmentUnit
+ KnowledgeCard
+ KnowledgeEvidenceLink
+ IndexVersion
```

## 7. API 设计

所有 API 使用 `/api/v1/courses/{course_id}` 作为课程边界。核心一期由服务端依赖注入固定教师身份，仍执行课程归属检查。

### 7.1 课程和文件

```http
POST   /api/v1/courses
GET    /api/v1/courses
GET    /api/v1/courses/{course_id}
PATCH  /api/v1/courses/{course_id}

POST   /api/v1/courses/{course_id}/upload-sessions
POST   /api/v1/courses/{course_id}/upload-sessions/{session_id}/complete
GET    /api/v1/courses/{course_id}/materials
GET    /api/v1/courses/{course_id}/materials/{material_id}
DELETE /api/v1/courses/{course_id}/materials/{material_id}
```

浏览器通过预签名地址直传 MinIO，`complete` 接口校验大小、哈希和类型后创建 `MaterialVersion(staged)`。

### 7.2 命题框架

```http
POST /api/v1/courses/{course_id}/framework-runs
GET  /api/v1/courses/{course_id}/framework-runs/{run_id}
GET  /api/v1/courses/{course_id}/framework-runs/{run_id}/candidate
POST /api/v1/courses/{course_id}/framework-runs/{run_id}/confirm
POST /api/v1/courses/{course_id}/framework-runs/{run_id}/reject
GET  /api/v1/courses/{course_id}/framework-versions/current
```

### 7.3 资料整理和知识树

```http
POST /api/v1/courses/{course_id}/organization-runs
GET  /api/v1/courses/{course_id}/organization-runs/{run_id}
GET  /api/v1/courses/{course_id}/organization-runs/{run_id}/candidate
GET  /api/v1/courses/{course_id}/organization-runs/{run_id}/knowledge-tree
POST /api/v1/courses/{course_id}/organization-runs/{run_id}/knowledge-tree/operations
POST /api/v1/courses/{course_id}/organization-runs/{run_id}/publish
POST /api/v1/courses/{course_id}/organization-runs/{run_id}/cancel

GET  /api/v1/courses/{course_id}/knowledge-catalogs/current
GET  /api/v1/courses/{course_id}/knowledge-catalogs/{version_id}
GET  /api/v1/courses/{course_id}/knowledge-catalogs/{version_id}/diff
GET  /api/v1/courses/{course_id}/knowledge-cards/{card_id}/evidence
```

树编辑使用结构化操作命令，不能直接覆盖整棵树 JSON。

### 7.4 蓝图和题位

```http
POST /api/v1/courses/{course_id}/exam-projects
GET  /api/v1/courses/{course_id}/exam-projects/{project_id}
POST /api/v1/courses/{course_id}/exam-projects/{project_id}/blueprints
GET  /api/v1/courses/{course_id}/blueprints/{blueprint_id}
POST /api/v1/courses/{course_id}/blueprints/{blueprint_id}/validate
POST /api/v1/courses/{course_id}/blueprints/{blueprint_id}/confirm
```

蓝图必须保存逐题 `PlanItem`，而不是只保存章节百分比。

### 7.5 生成和候选卷

```http
POST  /api/v1/courses/{course_id}/exam-projects/{project_id}/generation-runs
GET   /api/v1/courses/{course_id}/generation-runs/{run_id}
POST  /api/v1/courses/{course_id}/generation-runs/{run_id}/cancel

GET   /api/v1/courses/{course_id}/papers/{paper_id}
PATCH /api/v1/courses/{course_id}/papers/{paper_id}/questions/{question_id}
POST  /api/v1/courses/{course_id}/papers/{paper_id}/questions/{question_id}/accept
POST  /api/v1/courses/{course_id}/papers/{paper_id}/questions/{question_id}/reject
POST  /api/v1/courses/{course_id}/papers/{paper_id}/questions/{question_id}/regenerate
POST  /api/v1/courses/{course_id}/papers/{paper_id}/reorder
POST  /api/v1/courses/{course_id}/papers/{paper_id}/validate
```

单题重生成允许教师给出简短修改要求，但不能修改题型、分值、考核单元或已确认蓝图硬约束。

## 8. 后台任务和一致性

### 8.1 任务事实来源

PostgreSQL 的 `task_runs` 是任务事实来源，Redis/Celery 只负责传递消息。

```text
TaskRun
  ├─ id
  ├─ course_id
  ├─ task_type
  ├─ input_version
  ├─ idempotency_key
  ├─ status
  ├─ current_stage
  ├─ progress_current / progress_total
  ├─ retry_count
  ├─ lease_until
  ├─ error_code / error_summary
  └─ created_at / started_at / finished_at
```

API 在一个事务中写入业务运行记录和 `outbox_events`。Dispatcher 投递 Redis；Worker 使用数据库租约领取任务。消息丢失或 Worker 崩溃时，扫描器重新投递未完成任务。

### 8.2 客户端进度

核心一期使用短轮询：所有耗时 API 返回 `202 Accepted` 和 `run_id`，前端每 2—5 秒查询运行状态。刷新页面后根据 `run_id` 恢复，不依赖浏览器内存。WebSocket/SSE 不进入核心一期。

### 8.3 幂等与取消

- 创建运行要求 `Idempotency-Key`；
- 幂等键由业务类型、输入版本和教师操作组成；
- 重试不得创建重复框架、知识目录、题目或试卷版本；
- 取消后停止投递新子任务；
- 已经发出的外部请求可以结束，但写回前必须检查运行是否仍有效；
- 过期或已取消运行的结果只能记录诊断，不能覆盖业务数据。

## 9. LangGraph 工作流

LangGraph 只保存协调状态和稳定 ID，不保存原始文件、完整正文、完整证据、密钥或临时 URL。

### 9.1 FrameworkGraph

```mermaid
flowchart TD
  A["校验两份大纲"] --> B["冻结文件版本"]
  B --> C["确保 MinerU 解析完成"]
  C --> D1["教学大纲结构化抽取"]
  C --> D2["考核大纲结构化抽取"]
  D1 --> E["考核大纲主导合并"]
  D2 --> E
  E --> F["冲突、缺失项和权重校验"]
  F --> G["保存候选框架"]
  G --> H["interrupt：等待教师确认"]
  H --> I["应用教师修订"]
  I --> J["发布 FrameworkVersion"]
```

两份大纲独立调用模型。考核大纲主导范围、权重、能力和题型规则；教学大纲只校验教学覆盖与深度。

实现时必须继续满足：

- 教学大纲抽取与考核大纲抽取是两个独立图分支，可以并行执行，任何一个分支都不能接收另一份大纲正文；
- 同一 `framework_build_run_id` 的候选持久化必须幂等，Worker 重试不能生成多个框架候选；
- 所有开放冲突必须逐项提交非空处理决定，未知冲突键不得混入确认载荷；
- API 确认路径不得绕过 Graph 中的冲突、权重、锚点唯一性和结构化排除项校验；
- 发布新框架版本时，旧的当前版本原子变为 `superseded`，一门课程最多只有一个 `published` 框架版本。

Task 6 实现补充约束：L1 由已发布框架锚点完整建立，资料只能补充 L2/L3/L4；每个文件单独调用资料整理模型；未匹配范围、封面、文件名、提交要求和无证据候选进入待审或 `unmatched`，不得写入可发布树；知识目录、知识卡、证据关系和检索索引必须在一次发布事务中完成。

Task 7—8 实现补充约束：蓝图先按题型数量和分值确定题位，再按考纲章节权重分配到 L3/知识卡，禁止按知识点数量轮询出题；生成模型只接收 `QuestionGenerationPayload`，题型结构不合格或出现来源话术的题目进入局部重试，最多两次，仍失败则保留 blocker 状态。

### 9.2 OrganizationGraph

```mermaid
flowchart TD
  A["校验命题框架"] --> B["加载大纲知识树骨架"]
  B --> C["冻结选中资料"]
  C --> D["MinerU 按文件解析"]
  D --> E["确定性噪声清洗"]
  E --> F["逐文件提取 L2/L3/L4 候选"]
  F --> G["术语标准化"]
  G --> H["按考核锚点分组"]
  H --> I["候选聚类和小批归并"]
  I --> J["KnowledgeCatalogBuildSubgraph"]
  J --> K["证据准入与覆盖检查"]
  K --> L["生成树版本差异"]
  L --> M["interrupt：教师确认知识树"]
  M --> N["发布知识目录版本"]
  N --> O["发布检索索引版本"]
```

一个模型请求不得混合多个文件原文。树归并模型只接收同一考核锚点下的小批候选摘要，不接收全课程正文。

### 9.3 GenerationGraph

```mermaid
flowchart TD
  A["加载冻结蓝图"] --> B["校验五类版本"]
  B --> C["编译逐题纯净模型载荷"]
  C --> D["按题型小批生成"]
  D --> E["JSON Schema 校验"]
  E --> F["确定性质量检查"]
  F --> G["独立语义审校"]
  G --> H{"是否合格"}
  H -->|否，未超过2次| I["局部修复"]
  I --> E
  H -->|否，达到上限| J["needs_review"]
  H -->|是| K["后端回链证据"]
  J --> L["汇总候选卷"]
  K --> L
  L --> M["整卷重复、分值和蓝图检查"]
  M --> N["生成 PaperVersion"]
```

### 9.4 出题模型载荷

生成模块必须维护两个不同的对象，使用不同的 Pydantic Schema 和组装器：

```text
EvidenceTracePack（仅后端）
  ├─ course_id
  ├─ framework_version_id
  ├─ catalog_version_id
  ├─ index_version_id
  ├─ plan_item_id
  ├─ knowledge_card_ids[]
  ├─ evidence_ids[]
  └─ filter_trace

QuestionGenerationPayload（唯一允许发送给模型）
  ├─ question_type
  ├─ score
  ├─ difficulty
  ├─ cognitive_level
  ├─ performance_statement
  ├─ scope_boundary
  ├─ assessable_content
  ├─ question_template
  ├─ output_schema
  └─ teacher_revision_instruction
```

`EvidenceTracePack` 用于生成前检查来源是否仍有效、生成后回链教师查看依据、资料删除影响分析和审计，不得被序列化到模型请求。`QuestionGenerationPayload` 必须由字段白名单重新构造，不能通过排除几个字段的方式从数据库对象直接序列化。

模型载荷允许字段：

```text
question_type
score
difficulty
cognitive_level
performance_statement
scope_boundary
assessable_content
question_template
output_schema
teacher_revision_instruction
```

禁止字段：

```text
course_id
framework_anchor_id
assessment_unit_id
knowledge_card_id
evidence_id
文件名、页码、章节原始标签
原始证据正文和来源关系
```

后端在生成成功后，根据计划题绑定的知识卡查询有效来源关系并回链到 `GeneratedQuestion`。

## 10. 错误和重试策略

| 失败类型 | 处理 |
|---|---|
| MinerU 临时网络错误 | 最多自动重试 3 次 |
| MinerU 内容解析失败 | 文件标记失败，展示错误，不伪造解析结果 |
| DeepSeek 网络或临时 HTTP 错误 | 最多重试 2 次 |
| 模型返回空内容或协议错误 | 记录提供方请求 ID、响应元数据和脱敏摘要，按策略重试 |
| 模型 JSON/Schema 错误 | 使用结构纠错请求重试 1 次 |
| 单题质量检查失败 | 最多局部修复 2 次，之后 `needs_review` |
| 单个资料文件失败 | `OrganizationRun` 可进入 `partial_failed`，其他文件继续 |
| 总分、题数、权重或版本冲突 | 调用模型前直接阻断 |
| 教师取消 | 停止新任务，丢弃迟到结果 |

运行失败必须区分：`network_error`、`http_error`、`protocol_error`、`model_output_error`、`schema_error`、`quality_error`、`version_conflict` 和 `cancelled`。前端显示可操作的中文说明，日志保存机器可读错误码。

## 11. 核心架构基线检查

本开发设计基线满足以下条件：

1. 已明确核心一期与暂缓范围；
2. 已确定模块化单体和生产技术组件；
3. 已确定多课程、单内置教师的过渡方案；
4. 已确定 MinerU 是主要文档解析适配器；
5. 已明确上传不自动触发外部调用；
6. 已建立版本化知识树流程；
7. 已区分知识卡和来源关系；
8. 已定义核心 API、异步任务、幂等和取消边界；
9. 已定义三套 LangGraph 及知识树构建子图；
10. 已确保生成模型不会接触来源元数据和业务 ID。

以上内容构成核心架构、数据和工作流的不可破坏基线；后续工程细化不得推翻这些边界。

## 12. 参考资料

- 产品设计基线：`docs/superpowers/specs/2026-08-12-ai-final-exam-paper-design.md`
- MinerU API 文档：[https://mineru.net/apiManage/docs](https://mineru.net/apiManage/docs)
- MinerU 输出文件说明：[https://opendatalab.github.io/MinerU/reference/output_files/](https://opendatalab.github.io/MinerU/reference/output_files/)

## 13. 正式工程目录

核心一期采用前后端分离、后端模块化单体。目录按业务职责拆分，不把所有逻辑继续堆到单个 `server.py`。

```text
project-root/
├─ backend/
│  ├─ app/
│  │  ├─ main.py                    # FastAPI 入口
│  │  ├─ config.py                  # 环境变量和配置校验
│  │  ├─ api/
│  │  │  └─ v1/                     # HTTP 路由和请求模型
│  │  ├─ domain/
│  │  │  ├─ course/
│  │  │  ├─ material/
│  │  │  ├─ framework/
│  │  │  ├─ knowledge/
│  │  │  ├─ blueprint/
│  │  │  ├─ generation/
│  │  │  └─ paper/
│  │  ├─ services/                  # 跨领域用例服务
│  │  ├─ workflows/                 # LangGraph 图和节点
│  │  ├─ adapters/
│  │  │  ├─ document/               # MinerU、本地解析器
│  │  │  ├─ model/                  # DeepSeek 网关
│  │  │  ├─ storage/                # MinIO 端口
│  │  │  └─ search/                 # PostgreSQL/pgvector
│  │  ├─ infrastructure/
│  │  │  ├─ db/                     # SQLAlchemy 会话、仓储和空库初始化
│  │  │  ├─ tasks/                  # Celery、Outbox、租约
│  │  │  └─ observability/          # 日志、指标、追踪
│  │  └─ schemas/                   # Pydantic API/领域 Schema
│  ├─ db/
│  │  ├─ schema.py                  # 初始表、约束和索引定义
│  │  └─ init_db.py                 # 空数据库初始化与开发种子
│  ├─ tests/
│  │  ├─ unit/
│  │  ├─ integration/
│  │  ├─ workflow/
│  │  ├─ contract/
│  │  └─ fixtures/
│  ├─ pyproject.toml
│  └─ Dockerfile
├─ frontend/
│  ├─ src/
│  │  ├─ app/                       # 路由、布局、QueryClient
│  │  ├─ pages/                     # 课程、上传、框架、知识树、蓝图、候选卷
│  │  ├─ features/
│  │  │  ├─ courses/
│  │  │  ├─ materials/
│  │  │  ├─ framework/
│  │  │  ├─ knowledge-tree/
│  │  │  ├─ blueprint/
│  │  │  └─ paper-review/
│  │  ├─ components/                # 通用表格、状态、确认、错误组件
│  │  ├─ api/                       # 类型化 API 客户端
│  │  └─ styles/
│  ├─ package.json
│  └─ Dockerfile
├─ docker-compose.dev.yml
├─ .env.example
└─ README.md
```

模块依赖方向固定为：

```text
api → services → domain
services → adapters / infrastructure
workflows → services
domain 不依赖 FastAPI、Celery、MinerU 或 React
```

领域模块通过端口接口访问数据库、对象存储和模型服务，便于使用 Fake 实现进行测试。

## 14. 核心前端页面和教师流程

### 15.1 页面结构

核心一期只实现结构化审核页面，不实现正式富文本排版。

| 页面 | 主要内容 | 核心操作 |
|---|---|---|
| 课程列表 | 课程名称、当前框架、知识库和最近试卷状态 | 创建课程、进入课程 |
| 课程工作台 | 大纲、资料、知识树、蓝图、候选卷五个阶段卡片 | 查看阶段状态和待处理任务 |
| 文件上传 | 大纲区、教学材料区、练习/题库区 | 多选上传、暂存、删除、选择整理 |
| 命题框架确认 | 教学大纲、考核大纲、范围、权重、冲突和排除项 | 确认、修改、拒绝 |
| 知识树确认 | L1-L4 树、来源、覆盖、差异和问题 | 合并、拆分、改名、排除、发布 |
| 基础蓝图 | 总分、题型、题数、分值、难度、考试范围 | 构建、校验、确认 |
| 蓝图审核 | 逐题题位、章节分布、认知层级、知识卡覆盖 | 调整计划、确认 |
| 候选卷审核 | 题干、选项、答案、解析、评分细则、质量报告、来源 | 采用、编辑、拒绝、单题重生成、排序 |

### 15.2 教师主流程

```text
选择课程
  → 上传文件到指定资料区
  → 选择大纲并点击“构建命题框架”
  → 检查范围、期末题型比例、章节权重和冲突
  → 确认命题框架
  → 选择教学资料并点击“整理选中资料”
  → 查看 MinerU 解析进度和资料清洗结果
  → 查看候选知识树和来源
  → 合并/拆分/排除后发布知识目录
  → 填写基础蓝图
  → 审核逐题题位
  → 点击生成候选卷
  → 逐题审核并局部修复
```

每个阶段都显示当前版本和待处理原因。页面刷新后根据 `run_id` 恢复运行，不依赖前端本地状态。

### 15.3 树审核界面约束

知识树页面采用左右分栏：

```text
左侧：树结构和节点状态
右侧：节点详情、纯净知识卡、覆盖信息、来源证据和变更记录
```

右侧展示来源是教师审核用途；复制到出题模型的载荷必须由后端重新编译，不能从前端展示对象直接提交。

### 15.4 候选卷审核界面约束

每道题固定显示：

- 题目内容；
- 分值、题型、认知层级和难度；
- 标准答案；
- 解析；
- 主观题评分细则；
- `blocker/warning/info` 质量检查；
- “查看依据”来源入口；
- 采用、拒绝、编辑、重生成操作。

候选卷审核页面不允许把教师编辑后的题目直接覆盖原始生成尝试；每次编辑创建新的 `PaperVersion` 或 `paper_item_revision`。

## 15. 配置和密钥管理

### 16.1 配置文件

仓库只提交 `.env.example`，不提交 `.env`、真实 Token 或任何模型请求日志。

```dotenv
APP_ENV=development
APP_HOST=0.0.0.0
APP_PORT=8000

DATABASE_URL=postgresql+psycopg://exam:exam@postgres:5432/exam
REDIS_URL=redis://redis:6379/0

S3_ENDPOINT=http://minio:9000
S3_ACCESS_KEY=exam-dev
S3_SECRET_KEY=change-me
S3_BUCKET=exam-materials
S3_REGION=us-east-1

MINERU_BASE_URL=https://mineru.net
MINERU_API_TOKEN=replace-me
MINERU_MODEL_VERSION=vlm
MINERU_MAX_FILES_PER_BATCH=50
MINERU_POLL_INTERVAL_SECONDS=10
MINERU_MAX_POLL_SECONDS=1800

DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_API_KEY=replace-me
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_ORGANIZE_CONCURRENCY=4
DEEPSEEK_GENERATE_CONCURRENCY=4
DEEPSEEK_MAX_REPAIR_ATTEMPTS=2

UPLOAD_MAX_BYTES=209715200
TASK_LEASE_SECONDS=120
LOG_LEVEL=INFO
```

### 16.2 配置校验

应用启动时按环境校验：

- development：允许 MinIO 默认密钥和本地固定教师；
- test：必须使用 Fake MinerU、Fake DeepSeek 和临时数据库；
- production：必须使用外部密钥服务或环境注入，拒绝 `replace-me`、默认 MinIO 密钥和固定教师身份。

MinerU 和 DeepSeek Token 只能被后端 Worker 读取。前端只能看到服务状态、模型名称和脱敏错误摘要。

### 16.3 日志和诊断

允许记录：提供方、模型/解析器版本、运行 ID、请求 ID、HTTP 状态、耗时、输入输出字符数、Token 用量、错误码和输出哈希。

禁止记录：API Token、完整文件正文、完整提示词、完整模型输出、临时上传 URL、学生个人信息。

## 16. 本地开发环境

### 17.1 Docker Compose 服务

```text
postgres   PostgreSQL + pgvector
redis      Celery broker/result transport
minio      原始文件和解析产物
api        FastAPI
worker     Celery Worker + LangGraph
frontend   Vite 开发服务器
```

### 17.2 启动顺序

```text
1. docker compose -f docker-compose.dev.yml up -d postgres redis minio
2. backend 执行空数据库初始化脚本（扩展、建表、索引和开发种子）
3. backend 创建固定开发教师和测试课程（幂等）
4. 启动 API
5. 启动 Worker
6. 启动前端
7. 浏览器进入课程列表
```

### 17.3 本地数据边界

- 原始文件和 MinerU 产物只写入 MinIO；
- PostgreSQL 只保存元数据、结构化内容和对象 Key；
- Worker 不能依赖浏览器上传临时目录；
- 测试重置只能删除开发 bucket、测试数据库和 Redis 队列，不允许使用工作区根目录递归删除。

## 17. 测试设计

### 18.1 单元测试

必须覆盖：

- 文件名、MIME、大小和哈希校验；
- MinerU 状态映射和 ZIP 安全检查；
- `content_list.json` 到 `ContentBlock` 的归一化；
- 封面、报告标题、文件名、提交要求和纯操作步骤清洗；
- 考核大纲锚点准入；
- 知识树节点合并、拆分、移动和禁止越界；
- 无答案作业不能生成事实证据；
- 蓝图题数、题型、分值、0.5 分倍数和章节权重；
- `QuestionGenerationPayload` 字段白名单；
- `EvidenceTracePack` 不得序列化进模型请求；
- 题目评分点分值闭合和质量等级。

### 18.2 集成测试

使用 Docker Compose 或 testcontainers 验证：

- PostgreSQL 空库初始化、扩展、约束和版本外键；
- MinIO 上传、完成、删除和恢复；
- Celery 任务租约、重复投递和 Worker 重启；
- Fake MinerU 的申请上传链接、PUT、轮询和 ZIP 下载；
- Fake DeepSeek 的 JSON、空内容、网络错误和超时；
- 运行取消后迟到结果不能覆盖新版本；
- 知识目录和索引版本原子发布。

### 18.3 工作流测试

每套 LangGraph 至少有：

1. 正常完成路径；
2. 节点失败后重试路径；
3. `interrupt/resume` 教师确认路径；
4. Worker 重启恢复路径；
5. 输入版本过期路径；
6. 单子任务失败但整体继续路径。

### 18.4 回归夹具

必须固化此前原型发现的回归夹具：

| 夹具 | 必须验证 |
|---|---|
| 教学大纲 + 考核大纲 | 考核大纲建立 L1，教学大纲只校验深度 |
| 带封面的实验报告 | 不产生“实验报告封面”等知识点 |
| 含 `config.json`、`model.safetensors` 的资料 | 文件名不成为考点 |
| 含安装、下载、提交和截图步骤的资料 | 操作指令被排除或降级为素材 |
| 同一知识点来自多个文件 | 树中归并，不重复生成一题/文件 |
| 考核大纲含期末考试权重 | 蓝图读取题型和章节权重，不按知识点轮询出题 |
| 生成模型收到来源元数据的风险测试 | 请求载荷中没有文件名、实验编号、页码和 ID |
| MinerU 空结果或失败 | 运行显示失败，不回退为标题候选 |
| 单题生成失败 | 其他题继续，失败题进入 `needs_review` |

### 18.5 人工验收

首个真实课程至少完成：

- 两份大纲各自检查 10 个锚点；
- 知识树每个 L1 章节抽查 5 张知识卡；
- 每种首期题型至少人工检查 5 道题；
- 所有主观题检查题干、答案和评分细则闭合；
- 抽查生成请求确认无来源字段；
- 抽查失败任务和重试诊断；
- 教师能够从题目返回知识卡，再查看具体来源页码。

任何答案错误、选择题多解、知识树超出考纲、来源无法追溯、题干出现来源话术、评分点不闭合或失败题被伪装成合格题，均属于发布阻断问题。

### 18.6 真实素材回归补充约束

2026-08-14 使用 2 份真实大纲和 17 份教学材料执行 MinerU、DeepSeek、知识树、蓝图和逐题生成全链路后，新增以下不可回退约束：

- MinerU 返回的阿里云 OSS 预签名上传地址未签入 `Content-Type` 时，客户端 PUT 不得自行添加该头，否则会触发 `SignatureDoesNotMatch`；必须保留真实接口契约测试。
- 章节权重按全卷**分值**分配，不能对每种题型分别按题目数量取整。内部以 0.5 分为整数单位进行分配；题型分值组合无法满足考纲目标时阻断蓝图，禁止静默归一化、丢弃小权重章节或把差额转移给其他章节。
- 蓝图必须覆盖考核大纲的每个非零权重锚点，并输出“考纲目标分值—实际分值”对照。真实回归夹具必须验证 `5/25/35/5/10/15/5` 等含小权重章节的组合。
- `PlanItem` 选择知识卡时必须校验 `allowed_question_types`；不能把仅适合简答或综合题的知识卡机械分配给判断题、填空题。
- DeepSeek 网络断连、空内容和坏 JSON 均只影响当前文件或当前题位；使用指数退避、幂等缓存和坏缓存淘汰。缓存结果只有通过当前题型结构质检后才能复用，局部修复不得反复读取同一坏结果。
- 来源话术检测不能机械禁止课程知识中合法出现的“文件名”等术语。来源隔离首先由生成载荷白名单保证，文本规则只拦截“根据课件/资料”“实验编号”“第 X 页/讲”等明确来源依赖表达。

## 18. 本版完成标准

本版在 0.1 基础上补充完成：

1. 正式工程目录和依赖方向；
2. 核心前端页面和教师操作路径；
3. MinerU、DeepSeek、数据库、对象存储和任务队列配置；
4. Docker Compose 本地开发拓扑；
5. 单元、集成、工作流和人工验收测试；
6. 针对历史原型问题的回归夹具。

## 19. 修订记录

| 日期 | 版本 | 内容 |
|---|---|---|
| 2026-08-14 | 0.1 | 固化核心一期范围、模块化单体架构、领域数据、MinerU 文档解析、知识树建立与确认、核心 API、后台任务、FrameworkGraph、OrganizationGraph、KnowledgeCatalogBuildSubgraph 和 GenerationGraph。 |
| 2026-08-14 | 0.2 | 补充正式工程目录、前端页面与教师流程、配置和密钥管理、本地 Docker Compose 开发环境、测试分层、历史问题回归夹具和人工验收标准。 |
| 2026-08-14 | 0.3 | 同步 FrameworkGraph 实现约束：双大纲并行分支、候选幂等、冲突决定完整性、确认路径一致校验和单一当前发布版本。 |
| 2026-08-14 | 0.4 | 补充蓝图分配、逐题 PlanItem、来源隔离生成载荷、DeepSeek 网关和候选题质量阻断规则。 |
| 2026-08-14 | 0.5 | 记录真实 19 文件全链路回归：MinerU OSS 签名头兼容、按全卷分值分配章节权重、非零锚点覆盖、题型与知识卡适配、模型网络重试与坏缓存淘汰，以及来源话术规则收窄。 |
| 2026-08-15 | 0.6 | 固化全卷级命题协调：主脑先生成考查原子分配，再按题型生成；新增跨题答案泄漏、考查原子重复、填空题认知越界、术语表达和括号过量的全卷审查与局部修复。 |

## 20. 全卷级命题协调与重复考点防护

### 20.1 已确认问题

旧原型将每个 `PlanItem` 独立交给模型，题目生成节点之间没有共享的全卷状态。单题质检只能检查字段完整性，无法发现不同题型之间的答案泄漏、同一考查原子重复或填空题生成应用型任务。这是链路设计问题，不能通过增加少量禁用词解决。

### 20.2 主脑规划

蓝图确认后增加 `CoveragePlanner` 节点。它只接收考核蓝图和已发布知识卡的纯净内容，不接收文件名、页码、证据 ID 或章节来源话术，输出每个题位的：

- 唯一 `coverage_atom`（本题要考的最小知识原子）；
- 认知层次与题型适配；
- 可接受的答案边界；
- 优先术语表达；
- 不得覆盖的相邻原子和答案核心。

同一知识卡可以复用，但只有在考查原子、认知层次和答案核心均不重叠时才允许复用。主脑在生成前建立全卷覆盖账本，优先阻断重复，而不是生成后整卷返工。

### 20.3 分题型生成

主脑生成任务后由 LangGraph 分发到题型节点。填空题固定为理论记忆、定义、条件和结论，不生成开放场景、方案设计或多步骤应用。综合题保留基础理解、灵活应用和实际场景的组合能力，并独立生成答案、解析和逐点评分细则。

生成载荷继续执行来源隔离白名单，只传递题型任务、纯净知识内容、考查原子和答案边界；来源信息留在教师追溯数据中，不进入命题模型。

### 20.4 全卷审查与局部重生成

所有题目生成后执行 `CrossQuestionAudit`：

1. 确定性检查答案与其他题干、选项的重合、覆盖原子重复、题型认知越界、填空答案边界和括号过量；
2. 仅在确定性检查无法判定时，进行一次精简的全卷语义审查；
3. 冲突只标记相关题位，并携带冲突原因和需要避开的答案核心进行局部重生成；
4. 重生成达到上限仍不合格时，题目状态为 `needs_review`，不得伪装为合格题。

### 20.5 通用表达规范

术语规范来自资料术语频率和教师确认的 `preferred_terms`，不按某门课程硬编码黑名单。模型优先使用课程中常用的表达，只有必须消歧、表示符号或缩写时使用括号；“（即……）”等冗余解释应在审查阶段局部改写。

### 20.6 回归夹具

- 两个题型命中同一答案核心时，审查必须标记冲突并只重生成冲突题；
- 同一知识卡的不同考查原子可以共存，完全相同的原子不得共存；
- 填空题不得携带 `apply`、`analyze` 或场景设计要求；
- 题干、选项、答案和解析中的答案核心不得泄漏到其他题；
- 优先术语应被写入生成载荷，来源元数据仍不得进入生成载荷；
- 括号过量的题目进入局部修复，不使用学科专属禁词表。
