# AI 智能出卷系统 - Code Wiki

## 1. 项目概述

本项目是一个基于 AI 的智能试卷生成系统，采用「评估导向大纲 + 合同制命题」的架构设计。系统能够根据教师提供的教学大纲和考核大纲，自动构建课程知识框架，并通过多阶段流水线生成高质量的试卷。

**核心价值**：
- 评估导向的课程知识框架自动构建
- 基于知识图谱的试题原子化组织
- 合同驱动的确定性试卷生成（配额、难度、认知层级精确控制）
- 教师可干预的多阶段人工确认机制
- 考核大纲的考试规则（题型比例 / 章节命题权重）全程可查看、可修改，并真实作用于出卷

**产品形态**：一个「试卷」模块承载完整链路——出卷流水线（蓝图 → 合同 → 生成）与试卷的查看 / 编辑 / 定稿 / 导出是同一项目详情页的两个页签，不再分成两个入口。

**项目结构**：
```
f:\比赛项目\阅卷出题功能/
├── backend/                 # FastAPI 后端服务
├── frontend/                # React 前端应用
├── deploy/                  # 部署配置
├── docs/                    # 项目文档与素材
└── docker-compose.dev.yml   # 开发环境编排
```

---

## 2. 技术栈

### 2.1 后端技术栈

| 技术 | 版本/说明 | 用途 |
|------|----------|------|
| **FastAPI** | >=0.115 | Web 框架，提供 REST API |
| **Uvicorn** | >=0.30 | ASGI 服务器 |
| **SQLAlchemy** | >=2.0 | ORM，数据库操作 |
| **PostgreSQL** | - | 主数据库，课程隔离多租户架构 |
| **Psycopg** | >=3.2 | PostgreSQL 驱动 |
| **Redis** | >=5.0 | 缓存与 Celery broker |
| **Celery** | >=5.4 | 异步任务队列 |
| **LangGraph** | >=1.0 | 工作流编排引擎（状态机） |
| **Pydantic Settings** | >=2.5 | 配置管理 |
| **Boto3** | >=1.35 | S3 /MinIO 对象存储 |
| **Httpx** | >=0.27 | HTTP 客户端（调用外部服务） |
| **MinerU** | - | 文档解析服务（教学大纲解析） |
| **DeepSeek** | mimo-v2.5-pro | 大模型 API（语义提取、命题生成） |
| **Embedding** | qwen3.7-text-embedding | 文本向量化（知识图谱构建） |

**依赖文件**：[backend/pyproject.toml](backend/pyproject.toml)

### 2.2 前端技术栈

| 技术 | 版本/说明 | 用途 |
|------|----------|------|
| **React** | 19.2.8 | UI 框架 |
| **TypeScript** | ~6.0 | 类型安全 |
| **Vite** | 8.2 | 构建工具 |
| **Zustand** | ^5.0 | 轻量状态管理（登录态 / 当前课程 / toast） |
| **Lucide React** | ^1.34 | 图标库 |
| **Oxlint** | ^1.79 | 代码检查 |

**依赖文件**：[frontend/package.json](frontend/package.json)

> 前端早期有 Vitest + React Testing Library，测试文件已随重构移除；当前以
> `npm run build`（tsc + vite）与 `npm run lint`（oxlint）作为门禁，端到端行为
> 由后端 pytest 覆盖。

---

## 3. 系统架构

### 3.1 高层架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                    前端层 (React 19 + TypeScript)                │
│  ┌────────┐ ┌────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ │
│  │  概览   │ │ 资料库 │ │ 命题框架 │ │ 知识目录 │ │   试卷   │ │
│  │Dashboard│ │Materials│ │Framework │ │Knowledge │ │  Paper   │ │
│  └────────┘ └────────┘ └──────────┘ └──────────┘ └──────────┘ │
│      「试卷」模块内部是两个页签：                                  │
│      ┌────────────────────┐   ┌────────────────────┐          │
│      │ 出卷流水线           │   │ 试卷（双栏阅读器）  │          │
│      │ 蓝图 → 合同 → 生成   │   │ 查看/编辑/定稿/导出 │          │
│      └────────────────────┘   └────────────────────┘          │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                     API 网关层 (FastAPI)                         │
│  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐      │
│  │ Auth   │ │Courses │ │Materials│ │Framework│ │Knowledge│      │
│  │ Router │ │ Router │ │ Router │ │ Router │ │ Router │      │
│  └────────┘ └────────┘ └────────┘ └────────┘ └────────┘      │
│  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐      │
│  │Blueprint│ │Generation│ │Exam    │ │Paper   │ │Health  │      │
│  │ Router │ │ Router │ │Projects│ │Versions│ │ Check  │      │
│  └────────┘ └────────┘ └────────┘ └────────┘ └────────┘      │
└─────────────────────────────────────────────────────────────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         ▼                     ▼                     ▼
┌───────────────┐     ┌───────────────┐     ┌───────────────┐
│  LangGraph    │     │  数据库层      │     │  外部服务      │
│  工作流引擎    │     │ (PostgreSQL)  │     │ (MinerU/DeepSeek)│
│               │     │               │     │               │
│ • Framework   │     │ • 课程隔离     │     │ • 文档解析     │
│   Graph       │     │ • 多租户      │     │ • LLM 推理    │
│ • Generation  │     │ • 37 张表      │     │ • Embedding   │
│   Graph       │     │ • 外键约束    │     │ • 对象存储     │
│ • Knowledge   │     │               │     │               │
│   Catalog     │     │               │     │               │
│ • Organization│     │               │     │               │
│   Graph       │     │               │     │               │
└───────────────┘     └───────────────┘     └───────────────┘
```

### 3.2 数据流架构

```
教师上传资料 → 文档解析 → 框架构建 → 知识组织 → 蓝图/合同 → 生成 → 审核导出
      │              │          │          │          │         │         │
      ▼              ▼          ▼          ▼          ▼         ▼         ▼
   Materials    Parse Runs  Framework  Knowledge   Blueprint  Questions Paper
   (文件管理)   (解析任务)  Build Runs Catalog     Contract   Generation Versions
```

### 3.3 出卷流水线与试卷页签

出卷流水线只负责「出题」，由前端 `PipelinePanel` 实现三阶段可视化；
查看 / 编辑 / 定稿 / 导出**不在**流水线里，而是同一项目详情页的「试卷」页签
（`PaperPanel`，双栏阅读器）。生成成功后自动切到试卷页签继续。

| 流水线阶段 | 后端路由 | 说明 |
|------------|----------|------|
| **蓝图** | `exam_projects.py`（blueprints） | 题位计划：题型 / 分值 / 难度 / 章节 / 考点 |
| **合同** | `exam_projects.py`（contracts） | 逐题位锁定考查原子、答案域、禁用上下文 |
| **生成** | `exam_projects.py`（generate） | 按合同分批生成 + 质量校验 + 落库试卷版本 |

**试卷页签**：`paper_versions.py` 提供逐题编辑、增删、调序、定稿 / 撤销，以及三份导出
（学生卷 HTML、答卷 HTML、答案细则 JSON）。

---

## 4. 核心领域模型

### 4.1 Framework（评估导向大纲）

**职责**：构建以考核大纲为主线的课程知识框架，包括教学主题、考核范围、考点及其权重，
并抽取考核大纲声明的考试规则（题型比例、章节命题权重）。

**核心模型**：[backend/app/domain/framework/models.py](backend/app/domain/framework/models.py)

```python
class AssessmentOutline(BaseModel):
    """考核大纲提取结果"""
    anchors: list[AssessmentAnchor]        # 考核范围
    exam_points: list[ExamPoint]            # 考点列表
    final_exam_rules: dict                  # 期末考试规则（题型比例/章节权重/考试形式）

class FrameworkCandidate(BaseModel):
    """框架候选版本"""
    anchors: list[AssessmentAnchor]
    exam_points: list[ExamPoint]
    teaching_topics: list[TeachingTopic]
    conflicts: list[FrameworkConflict]      # 冲突列表（教师需确认）
    final_exam_rules: dict
```

**考试规则**：[backend/app/domain/framework/exam_rules.py](backend/app/domain/framework/exam_rules.py)

```python
def normalize_exam_rules(raw, *, anchor_keys) -> dict
def type_rules_from_ratios(ratios, *, total_score=100) -> dict | None
```

负责把模型 / 教师给的自由形态规则归一成内部约定：题型名统一英文枚举
（"选择题" → `single_choice`）、剔除未知项、比例归一到 100、未声明的章节锚点补 0；
`type_rules_from_ratios` 按题型比例推导蓝图 type_rules（题数折算后定点修正，
保证总分精确闭合）。

**考点模型**：[backend/app/domain/framework/exam_points.py](backend/app/domain/framework/exam_points.py)

```python
class ExamPoint(BaseModel):
    """单个考点"""
    code: str                    # 考点编码
    anchor_key: str              # 所属考核范围
    weight_value: float          # 权重值
    weight_source: WeightSource  # 权重来源（显式/继承）
    weight_group_id: str         # 权重组ID
    cognitive_targets: list[str] # 认知层级目标
    teaching_anchor_keys: list[str] # 关联教学主题
```

### 4.2 Generation（合同制命题）

**职责**：将蓝图转换为确定性的试题生成合同，确保每道题的原子内容、答案边界、禁用上下文被精确锁定。

**核心模型**：[backend/app/domain/generation/contract.py](backend/app/domain/generation/contract.py)

```python
class ContractSlot(BaseModel):
    """单个题位的合同：考哪个原子、答案域、禁用上下文"""
    item_index: int              # 题位序号
    question_type: str           # 题型（单选/多选/判断/简答/综合）
    score: float                 # 分值
    difficulty: str              # 难度
    cognitive_level: str         # 认知层级
    assessment_mode: str         # 评估方式（概念/计算/综合）
    exam_point_id: str           # 关联考点
    anchor_key: str              # 所属章节
    unit_id: str                 # 单元ID
    card_id: str                 # 知识卡ID
    coverage_atom: str           # 考查原子（核心知识点）
    answer_boundary: str         # 答案边界
    forbidden_context: ForbiddenContext  # 同考点其他题禁用内容
    # 综合题专用字段
    comprehensive_archetype: ComprehensiveArchetype | None = None
    material_form: MaterialForm | None = None
    cognitive_sequence: list[str] = []
    subquestion_count_range: list[int] | None = None

class PaperContract(BaseModel):
    """整卷合同"""
    total_score: float
    slots: list[ContractSlot]
    conflicts: list[ContractConflict]
    audit_summary: ContractAuditSummary
```

**原子聚类与分配算法**：
- `build_exam_point_pools()`: 按考点聚合知识卡原子，核心度阈值预过滤
- `cluster_pool_atoms()`: 基于 bigram Jaccard + 术语锚 + concept_cluster 的并查集聚类
- `assign_atoms_to_items()`: 同考点题位按 item_index 顺序，软评分贪心 + 答案域互斥地取原子

### 4.3 Knowledge（知识图谱）

**职责**：将材料内容组织为以考点为核心的知识卡片和证据链。

**核心模型**：[backend/app/domain/knowledge/models.py](backend/app/domain/knowledge/models.py)

```python
class KnowledgeCard(BaseModel):
    """知识卡片（原子化知识单元）"""
    card_id: str
    course_id: str
    unit_id: str
    exam_point_id: str
    title: str
    scope_boundary: dict
    assessable_content: list[str]      # 可考查原子列表
    answer_boundary: str               # 答案边界
    evidence_chunks: list[EvidenceChunk]  # 证据链
```

### 4.4 Blueprint（试卷蓝图）

**职责**：定义试卷的结构规划，包括题型分布、分值、难度、认知层级等。

**核心模型**：[backend/app/domain/blueprint/models.py](backend/app/domain/blueprint/models.py)

```python
class BlueprintRequest(BaseModel):
    """蓝图请求"""
    units: list[UnitCoverage]       # 单元覆盖计划
    chapter_weights: dict[str, float]  # 章节权重
    type_rules: dict                 # 题型规则
    card_semantic_profiles: dict[str, CardSemanticProfile]
    card_question_types: dict[str, list[str]]

class PlanItem(BaseModel):
    """单个题位计划"""
    item_index: int
    question_type: str
    score: float
    difficulty: str
    cognitive_level: str
    assessment_mode: str
    exam_point_id: str
    anchor_key: str
```

---

## 5. LangGraph 工作流

系统使用 LangGraph 实现 4 个核心状态机工作流，每个工作流都是一个节点化的处理流水线。

### 5.1 Framework Graph（框架构建）

**文件**：[backend/app/workflows/framework_graph.py](backend/app/workflows/framework_graph.py)

**节点流程**：
```
START → validate_inputs → freeze_outline_versions → ensure_document_parsed
                                    ↓
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
          extract_teaching_syllabus        extract_assessment_syllabus
                    └───────────────┬───────────────┘
                                    ▼
                    merge_assessment_led_framework
                                    ▼
                      align_exam_points_with_teaching
                                    ▼
                              validate_conflicts
                                    ▼
                              persist_candidate
                                    ▼
                        interrupt_teacher_confirmation  ←───┐
                                    │                        │
                                    ▼                        │
                        publish_framework_version ──────────┘
                                    │
                                   END
```

**状态定义**：
```python
class FrameworkState(TypedDict, total=False):
    course_id: str
    run_id: str
    teaching_material_version_id: str
    assessment_material_version_id: str
    teaching_blocks: list[str]           # 解析后的教学大纲文本
    assessment_blocks: list[str]         # 解析后的考核大纲文本
    teaching_topics: list[dict]          # 提取的教学主题
    assessment_outline: dict             # 提取的考核大纲
    candidate: dict                      # 框架候选
    candidate_id: str
    confirmation: dict                   # 教师确认
    published_id: str                    # 发布版本ID
```

**关键函数**：
- `validate_inputs()`: 验证输入材料完整性
- `extract_teaching_syllabus()`: LLM 提取教学大纲
- `extract_assessment_syllabus()`: LLM 提取考核大纲（含考试规则）
- `merge_assessment_led_framework()`: 合并为以考核为导向的框架
- `align_exam_points_with_teaching()`: 验证考点与教学覆盖的对齐
- `interrupt_teacher_confirmation()`: 中断等待教师确认（LangGraph interrupt）
- `publish_framework_version()`: 发布框架版本

### 5.2 Generation Graph（试卷生成）

**文件**：[backend/app/workflows/generation_graph.py](backend/app/workflows/generation_graph.py)

**节点流程**：
```
START → build_batches → batch_generate (并行)
                                    │
                                    ▼
                          merge_and_check → audit_paper
                                    │
                                    ▼
                                   END
```

**状态定义**：
```python
class GenerationState(TypedDict, total=False):
    contract: list[dict]                 # 试卷合同
    knowledge_cards: dict[str, dict]     # 知识卡片
    batches: list[dict]                  # 分批后的生成任务
    questions: list[dict]                # 生成的试题
    final_check: dict                    # 终检结果
    model_call_count: int                # 模型调用次数
```

**核心特性**：
- 按考点分批并行生成
- 三级回退策略：batch 重试 → 单 slot 重试 → 原子替换
- 合同约束校验（配额、难度、认知层级、答案域）
- 出题模型只见纯净知识卡（原子 / 答案域 / 禁用上下文），来源关系由后端生成后回链

### 5.3 Knowledge Catalog Subgraph

**文件**：[backend/app/workflows/knowledge_catalog_subgraph.py](backend/app/workflows/knowledge_catalog_subgraph.py)

**职责**：为每个已确认的考点构建来源分离的知识目录候选，支持多来源（教材、课件、习题等）的知识组织。

### 5.4 Organization Graph

**文件**：[backend/app/workflows/organization_graph.py](backend/app/workflows/organization_graph.py)

**职责**：以考点为导向的材料整理流程，将原始材料组织为结构化的知识卡片。

---

## 6. API 端点

### 6.1 路由结构

FastAPI 应用在 [backend/app/main.py](backend/app/main.py) 中注册了 9 个路由器：

```python
app = FastAPI(title="AI Exam System")
app.include_router(auth_router)              # /api/v1/auth
app.include_router(courses_router)           # /api/v1/courses/{course_id}
app.include_router(materials_router)         # /api/v1/courses/{course_id}/materials
app.include_router(framework_router)         # /api/v1/courses/{course_id}/framework-runs
app.include_router(knowledge_router)         # /api/v1/courses/{course_id}/knowledge
app.include_router(blueprints_router)        # /api/v1/courses/{course_id}/blueprints
app.include_router(generation_router)        # /api/v1/courses/{course_id}/generation-runs
app.include_router(exam_projects_router)     # /api/v1/courses/{course_id}/exam-projects
app.include_router(paper_versions_router)    # /api/v1/courses/{course_id}/paper-versions
```

### 6.2 核心端点

#### Auth [backend/app/api/v1/auth.py](backend/app/api/v1/auth.py)

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/api/v1/auth/login` | 登录，签发 token |

#### Framework [backend/app/api/v1/framework.py](backend/app/api/v1/framework.py)

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/api/v1/courses/{course_id}/framework-runs` | 创建框架构建任务，触发 LangGraph 工作流 |
| GET | `/api/v1/courses/{course_id}/framework-runs/latest` | 获取最新框架构建任务 |
| GET | `/api/v1/courses/{course_id}/framework-runs/{run_id}` | 获取指定任务详情 |
| GET | `/api/v1/courses/{course_id}/framework-runs/{run_id}/candidate` | 获取框架候选内容 |
| POST | `/api/v1/courses/{course_id}/framework-runs/{run_id}/confirm` | 教师确认并发布框架 |
| POST | `/api/v1/courses/{course_id}/framework-runs/{run_id}/reject` | 教师拒绝框架 |
| GET | `/api/v1/courses/{course_id}/framework-versions/current` | 获取当前框架（含 `exam_rules`） |
| PATCH | `/api/v1/courses/{course_id}/framework-versions/current/rules` | 修改考核规则（题型比例 / 章节权重） |

#### Exam Projects [backend/app/api/v1/exam_projects.py](backend/app/api/v1/exam_projects.py)

| 方法 | 路径 | 功能 |
|------|------|------|
| GET/POST | `/api/v1/courses/{course_id}/exam-projects` | 项目列表 / 新建项目 |
| GET/PATCH | `/api/v1/courses/{course_id}/exam-projects/{project_id}` | 项目详情 / 更新状态 |
| POST | `.../exam-projects/{project_id}/blueprints` | 创建蓝图（按考纲规则推导题型分布） |
| GET | `.../blueprints/current/plan-items` | 当前蓝图的题位计划 |
| POST | `.../blueprints/current/confirm` | 确认蓝图 |
| POST | `.../contracts/allocate` | 分配合同 |
| PATCH | `.../contracts/revise` | 修订合同 |
| POST | `.../contracts/confirm` | 确认合同 |
| GET | `.../contracts/current` | 当前已确认合同快照 |
| POST | `.../generate` | 启动生成任务（Celery） |
| GET | `.../task-runs/{task_run_id}` | 查询生成任务进度 |

#### Paper Versions [backend/app/api/v1/paper_versions.py](backend/app/api/v1/paper_versions.py)

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `.../paper-versions/current` | 当前试卷版本（含逐题解析后数据） |
| GET | `.../paper-versions/{pv_id}/needs-review` | 待审核题目清单 |
| PATCH | `.../paper-versions/{pv_id}/items/{item_index}` | 覆写单题（教师修改 / 清除待审核标记） |
| PUT | `.../paper-versions/{pv_id}/items/reorder` | 调整题目顺序 |
| POST | `.../paper-versions/{pv_id}/items` | 新增教师自拟题目（答案必填） |
| DELETE | `.../paper-versions/{pv_id}/items/{item_index}` | 删除题目 |
| POST | `.../paper-versions/{pv_id}/confirm` | 定稿 |
| POST | `.../paper-versions/{pv_id}/revert` | 撤销定稿 |
| GET | `.../export/json` | 答案细则 JSON 下载 |
| GET | `.../export/student` | 学生卷 HTML（可打印） |
| GET | `.../export/answer-key` | 答卷 HTML（含答案，可打印） |

#### Blueprints / Generation / Health

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/api/v1/courses/{course_id}/blueprints/allocate` | 分配试卷蓝图 |
| POST | `/api/v1/courses/{course_id}/blueprints/confirm` | 确认蓝图 |
| POST | `/api/v1/courses/{course_id}/generation-runs` | 创建试卷生成任务 |
| GET | `/api/v1/health` | 健康检查（PostgreSQL、Redis、MinerU、DeepSeek） |

---

## 7. 服务层

### 7.1 服务概览

| 服务文件 | 职责 |
|----------|------|
| [auth_service.py](backend/app/services/auth_service.py) | 登录认证与 token 签发 |
| [course_service.py](backend/app/services/course_service.py) | 课程 CRUD |
| [framework_service.py](backend/app/services/framework_service.py) | 框架构建的持久化、发布与规则修改 |
| [contract_service.py](backend/app/services/contract_service.py) | 试卷合同的分配与审计 |
| [contract_execution_service.py](backend/app/services/contract_execution_service.py) | 合同执行与原子分配 |
| [generation_service.py](backend/app/services/generation_service.py) | 单题质量校验、合同终检 |
| [generation_runner_service.py](backend/app/services/generation_runner_service.py) | 生成任务运行器 |
| [blueprint_service.py](backend/app/services/blueprint_service.py) | 蓝图分配引擎 |
| [blueprint_persistence_service.py](backend/app/services/blueprint_persistence_service.py) | 蓝图持久化 + 默认题型分布推导 |
| [knowledge_tree_service.py](backend/app/services/knowledge_tree_service.py) | 知识树构建 |
| [knowledge_publish_service.py](backend/app/services/knowledge_publish_service.py) | 知识目录发布 |
| [document_processing_service.py](backend/app/services/document_processing_service.py) | 文档解析服务（MinerU 集成） |
| [parse_service.py](backend/app/services/parse_service.py) | 解析任务编排 |
| [material_service.py](backend/app/services/material_service.py) | 材料管理（上传 / 版本 / 哈希） |
| [exam_project_service.py](backend/app/services/exam_project_service.py) | 考试项目管理 |
| [paper_version_service.py](backend/app/services/paper_version_service.py) | 试卷版本管理 + 三份导出渲染 |
| [model_call_service.py](backend/app/services/model_call_service.py) | 模型调用记录 |
| [staging_retrieval_service.py](backend/app/services/staging_retrieval_service.py) | 暂存区检索 |

### 7.2 关键服务详解

#### FrameworkService

**职责**：管理框架构建任务的完整生命周期，并支持教师修改考核规则。

**关键函数**：
```python
def create_framework_run(session, course_id, teaching_material_version_id, assessment_material_version_id) -> dict
def confirm_framework_run(session, course_id, run_id, confirmation) -> dict
def reject_framework_run(session, course_id, run_id) -> dict
def get_current_framework(session, course_id) -> dict      # 顶层附带 exam_rules
def update_exam_rules(self, state, exam_rules) -> str      # PATCH 规则
```

**仓库模式**：
```python
class DatabaseFrameworkRepository(FrameworkRepository):
    def persist_candidate(self, state, candidate) -> str
    def publish(self, state, confirmation) -> str
    def update_exam_rules(self, state, exam_rules) -> str
```

#### ContractService

**职责**：将蓝图分配为确定性的试卷合同。

**核心函数**：
```python
def allocate_paper_contract(request: ContractRequest) -> PaperContract
```

**分配策略**：
1. 调用 `allocate_plan_items()` 分配题位计划
2. 调用 `build_exam_point_pools()` 构建考点原子池
3. 检测配额不足冲突
4. 按考点聚类原子（`cluster_pool_atoms`）
5. 贪心分配原子到题位（`assign_atoms_to_items`）
6. 生成综合题原型轮换字段
7. 计算禁用上下文（同考点兄弟题位互斥）
8. 生成审计摘要

#### PaperVersionService

**职责**：从生成结果创建 candidate 试卷 → 教师覆写 → 定稿 / 回滚，并渲染三份导出。

**导出渲染要点**：
- `_strip_stem_noise()`：剥掉题干自带的编号 / 分值前缀，避免与导出题号叠成「1.1.」
- `_section_groups()` / `_section_caption()`：按题型分节，生成「一、单选题（共N题，每题X分，共Y分）」
- `_answer_keys()` / `answer_option_keys()`：答案解析成选项字母，兼容字母、选项原文、并列原文三种形态
- `_exam_shell()`：三份导出共用的正式卷面外壳（信息头 + 题次表 + 装订线 + 学号姓名页脚）
- 判断题答案渲染为「正确 / 错误」，缺答案显式标注【缺答案·需人工补充】

---

## 8. 适配器层

### 8.1 模型适配器

**DeepSeek Gateway**：[backend/app/adapters/model/deepseek_gateway.py](backend/app/adapters/model/deepseek_gateway.py)

```python
class DeepSeekJsonClient:
    """OpenAI 兼容的严格 JSON 客户端"""
    def __init__(self, api_key, base_url, model, recorder=None)
    def generate(self, messages, *, response_format=None, temperature=0) -> dict
    def generate_batch(self, prompts, **kwargs) -> list[dict]

class DeepSeekGateway:
    """高层 LLM 包装器，提供批量生成能力"""
    def generate_batch(self, prompts, **kwargs) -> list[dict]
```

**特性**：
- 严格 JSON 模式验证
- 自动重试与指数退避
- 模型调用记录（DatabaseModelCallRecorder）
- 可观测性（trace_id 追踪）

**语义提取器**：[backend/app/adapters/model/deepseek_semantic_extractors.py](backend/app/adapters/model/deepseek_semantic_extractors.py)

```python
class DeepSeekSyllabusExtractor:
    """教学大纲/考核大纲语义提取器"""
    def extract_teaching(self, blocks, call_context) -> list[TeachingTopic]
    def extract_assessment(self, blocks, call_context) -> AssessmentOutline   # 含考试规则
```

**Embedding Gateway**：[backend/app/adapters/model/embedding_gateway.py](backend/app/adapters/model/embedding_gateway.py)

### 8.2 文档适配器

**MinerU 客户端**：[backend/app/adapters/document/mineru_client.py](backend/app/adapters/document/mineru_client.py)

```python
class MinerUClient:
    """MinerU 文档解析服务客户端"""
    def submit_parse(self, request: ParseRequest) -> ParseSubmission
    def poll_progress(self, submission: ParseSubmission) -> ParseProgress
    def download_artifact(self, submission, artifact_type) -> ParseArtifact
```

**本地文本解析器**：[backend/app/adapters/document/local_text_parser.py](backend/app/adapters/document/local_text_parser.py)

```python
def parse_text_file(file_path: str) -> list[ContentBlock]
def parse_pdf_file(file_path: str) -> list[ContentBlock]
```

### 8.3 存储适配器

**MinIO/S3 存储**：[backend/app/adapters/storage/minio_storage.py](backend/app/adapters/storage/minio_storage.py)

```python
class MinioStorage:
    def put_bytes(self, key, data, content_type)
    def get_bytes(self, key) -> bytes
    def presign_get(self, key, expires_in=3600) -> str
```

**本地存储回退**：[backend/app/adapters/storage/local_storage.py](backend/app/adapters/storage/local_storage.py)

```python
class LocalStorage:
    """本地文件系统存储（开发环境回退）"""
    def put_bytes(self, key, data, content_type)
    def get_bytes(self, key) -> bytes
```

---

## 9. 数据库架构

### 9.1 多租户设计

系统采用**课程隔离（Course-Isolated）**的多租户架构。所有业务表都包含 `course_id` 外键，确保不同课程的数据完全隔离。

**核心表结构**（35 张课程级表 + `users` / `courses`）：

#### 文件与上传
| 表名 | 说明 |
|------|------|
| `materials` | 材料文件（教学大纲/考核大纲/教材/习题） |
| `material_versions` | 材料版本（支持版本管理） |
| `upload_sessions` | 上传会话（断点续传） |

#### 文档解析
| 表名 | 说明 |
|------|------|
| `parser_profiles` | 解析器配置 |
| `document_parse_runs` | 解析任务记录 |
| `document_artifacts` | 解析产物（ZIP/JSON/Markdown） |
| `content_blocks` | 解析后的内容块 |

#### 框架构建
| 表名 | 说明 |
|------|------|
| `framework_build_runs` | 框架构建任务 |
| `framework_versions` | 框架版本（candidate/published/superseded/rejected），payload 内含考试规则 |
| `framework_anchors` | 框架锚点（考核范围） |
| `framework_conflicts` | 框架冲突（教师确认项） |
| `exam_points` | 考点表 |

#### 知识图谱
| 表名 | 说明 |
|------|------|
| `organization_runs` | 资料整理任务 |
| `evidence_chunks` | 证据块（来源引用） |
| `exam_point_evidence_links` | 考点—证据关联 |
| `knowledge_catalog_versions` | 知识目录版本 |
| `content_domains` | 内容域 |
| `assessment_units` | 考核单元 |
| `knowledge_cards` | 知识卡片 |
| `knowledge_evidence_links` | 知识卡—证据关联 |
| `index_versions` / `index_memberships` | 索引版本与成员 |

#### 试卷生成
| 表名 | 说明 |
|------|------|
| `exam_projects` | 考试项目 |
| `blueprint_versions` | 蓝图版本 |
| `blueprint_sections` | 蓝图分节 |
| `plan_items` | 题位计划 |
| `generation_runs` | 生成任务（含合同快照） |
| `generation_attempts` | 生成尝试记录 |
| `generated_questions` | 生成的试题 |
| `quality_checks` | 质量检查 |
| `paper_versions` | 试卷版本 |
| `paper_items` | 试卷题项 |

#### 基础设施
| 表名 | 说明 |
|------|------|
| `model_calls` | 模型调用记录 |
| `task_runs` | 异步任务记录 |
| `outbox_events` | 出站事件（事件溯源） |

**架构文件**：[backend/app/db/schema.py](backend/app/db/schema.py)

### 9.2 关键约束

```python
# 课程隔离索引
Index("ix_{table}_course_id", table.c.course_id)

# 唯一约束（复合主键）
UniqueConstraint("id", "course_id", name="uq_{table}_id_course")

# 框架版本唯一性约束（部分索引，仅 published）
Index(
    "uq_framework_versions_current_published",
    framework_versions.c.course_id,
    unique=True,
    postgresql_where=framework_versions.c.status == "published",
)

# 材料类型检查约束
CheckConstraint(
    "material_type IN ('teaching_syllabus', 'assessment_syllabus', 'teaching_material', 'exercise')",
    name="ck_materials_material_type",
)
```

---

## 10. 前端架构

### 10.1 目录结构

```
frontend/src/
├── main.tsx                      # 入口
├── App.tsx                       # 路由表
├── config.ts                     # 运行时配置（apiBase / 超时 / credentials）
├── api/                          # HTTP 层
│   ├── http.ts                   # request() / uploadBinary()，统一鉴权与错误包装
│   ├── client.ts                 # 聚合成 api 对象（api.examProjects / api.paperVersions …）
│   ├── errors.ts                 # ApiError 与用户可读文案映射
│   └── domains/                  # 按业务域拆分
│       ├── auth.ts  courses.ts  materials.ts  framework.ts
│       ├── knowledgeRun.ts  knowledgePublish.ts  knowledgeView.ts
│       ├── examProjects.ts  paperVersions.ts  blueprints.ts
├── components/
│   ├── layout/                   # Layout.tsx（外壳）+ Sidebar.tsx（悬浮岛侧栏）
│   └── ui/                       # Badge Button Card Input Modal Select
│                                 # ProgressPanel Skeleton Spinner Toast
├── hooks/
│   └── useNameMaps.ts            # id → 中文名 映射（试卷模块共用）
├── lib/
│   ├── examDisplay.ts            # 题型/难度/项目与试卷状态展示常量（试卷模块共用）
│   └── sha256.ts                 # 上传文件分片哈希
├── pages/
│   ├── auth/                     # LoginPage  ProtectedRoute
│   ├── course-space/             # /courses 课程选择
│   ├── dashboard/                # /courses/:courseId 课程概览
│   ├── materials/                # 资料库（按四区归档上传）
│   ├── framework/                # 命题框架 + ExamRulesCard（考核规则）
│   ├── knowledge/                # 知识目录（树形/图谱双视图）
│   └── paper/                    # 「试卷」模块
│       ├── index.tsx             # 项目列表 + 项目详情（双页签外壳）
│       ├── PipelinePanel.tsx     # 出卷流水线：蓝图 → 合同 → 生成
│       └── PaperPanel.tsx        # 试卷页签：双栏阅读器 + 题目编辑器
├── stores/                       # zustand：auth / course / toast
├── styles/                       # design-tokens.css global.css App.css
├── types/api.ts                  # 与后端对应的 TypeScript 类型
└── utils/                        # cn / format
```

### 10.2 路由

| 路径 | 页面 |
|------|------|
| `/login` | 登录 |
| `/courses` | 课程空间（选择课程） |
| `/courses/:courseId` | 课程概览 |
| `/courses/:courseId/materials` | 资料库 |
| `/courses/:courseId/framework` | 命题框架 |
| `/courses/:courseId/knowledge` | 知识目录 |
| `/courses/:courseId/paper` | 试卷（出卷流水线 + 试卷查看/审核/导出） |
| `/courses/:courseId/exam-projects`、`/paper-center` | 重定向到 `/paper` |

详情页通过 `?project=<id>` 直达指定项目（从其它位置带项目过来时自动展开）。

### 10.3 核心组件

#### PaperPage（试卷模块外壳）

**文件**：[frontend/src/pages/paper/index.tsx](frontend/src/pages/paper/index.tsx)

**职责**：项目列表 → 项目详情；详情页用两个页签分别承载出卷流水线与试卷。
已生成试卷的项目打开即落到「试卷」页签，未生成的落到流水线；生成成功自动切页签。

#### PipelinePanel（出卷流水线）

**文件**：[frontend/src/pages/paper/PipelinePanel.tsx](frontend/src/pages/paper/PipelinePanel.tsx)

**职责**：三阶段（蓝图 / 合同 / 生成）的渲染与交互。合同快照与任务进度以服务端为
权威源（持久化在 `generation_runs.contract_snapshot` 与 `task_runs`），退出项目再进入
或刷新页面都不丢失；创建蓝图时章节权重优先取考核大纲声明的命题权重。

#### PaperPanel（试卷双栏阅读器）

**文件**：[frontend/src/pages/paper/PaperPanel.tsx](frontend/src/pages/paper/PaperPanel.tsx)

**职责**：左侧按题型分节的题号索引（待审核 / 缺答案 / 已修改圆点 + 筛选），右侧当前
题目完整呈现（题干、选项答案高亮、解析折叠、考点、待审核原因）；支持点题号、上一题 /
下一题与键盘 ↑↓ 翻题；编辑为按需展开的单题表单；顶部档案卡集中呈现总分、题型分布、
难度分布、待审核数与三个导出入口；定稿遇待审核题弹出门禁。

#### ExamRulesCard（考核规则卡）

**文件**：[frontend/src/pages/framework/ExamRulesCard.tsx](frontend/src/pages/framework/ExamRulesCard.tsx)

**职责**：展示考核大纲抽取出的考试形式、题型比例与章节命题权重，并支持教师修改后保存
（PATCH `/framework-versions/current/rules`）。蓝图按这里的比例推导题型分布。

#### API 客户端

**文件**：[frontend/src/api/http.ts](frontend/src/api/http.ts)

```typescript
export async function request<T>(path: string, options?: RequestInit, token?: string): Promise<T>
```

**Vite 代理配置**：[frontend/vite.config.ts](frontend/vite.config.ts)

```typescript
export default defineConfig({
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000'
    }
  }
})
```

---

## 11. 配置与环境

### 11.1 配置类

**文件**：[backend/app/config.py](backend/app/config.py)

```python
class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://exam:exam@localhost:5432/exam"
    redis_url: str = "redis://localhost:6379/0"

    # MinerU 配置
    mineru_api_token: str = ""
    mineru_base_url: str = "https://mineru.net"
    mineru_model_version: str = "vlm"
    mineru_poll_interval_seconds: int = 10
    mineru_max_poll_seconds: int = 1800

    # DeepSeek 配置
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.xiaomimimo.com/v1"
    deepseek_model: str = "mimo-v2.5-pro"

    # Embedding 配置
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "qwen3.7-text-embedding"
    embedding_api_format: str = "openai"

    # 组织/检索配置
    organization_retrieval_top_k: int = 12
    organization_retrieval_min_score: float = 0.30
    organization_max_workers: int = 16

    # 上传配置
    upload_max_bytes: int = 209715200  # 200MB

    # S3/MinIO 配置
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minio-dev"
    s3_secret_key: str = "minio-dev-password"
    s3_bucket: str = "exam-materials"
    s3_region: str = "us-east-1"
```

### 11.2 环境变量

支持通过 `.env` 文件或环境变量覆盖配置：

```bash
# 数据库
DATABASE_URL=postgresql+psycopg://exam:exam@localhost:5432/exam

# Redis
REDIS_URL=redis://localhost:6379/0

# 大模型
DEEPSEEK_API_KEY=your-api-key
DEEPSEEK_BASE_URL=https://api.xiaomimimo.com/v1
DEEPSEEK_MODEL=mimo-v2.5-pro

# Embedding
EMBEDDING_API_KEY=your-embedding-key
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_MODEL=qwen3.7-text-embedding
EMBEDDING_API_FORMAT=dashscope

# MinerU
MINERU_API_TOKEN=your-mineru-token

# 存储
S3_ENDPOINT=http://localhost:9000
S3_ACCESS_KEY=minio-dev
S3_SECRET_KEY=minio-dev-password
```

---

## 12. 依赖关系

### 12.1 后端模块依赖

```
app/
├── main.py
│   └── 依赖: config, api.v1.* (9 routers)
│
├── config.py
│   └── 依赖: pydantic-settings
│
├── api/v1/
│   ├── auth.py            → services.auth_service
│   ├── courses.py         → services.course_service
│   ├── framework.py       → workflows.framework_graph, services.framework_service
│   ├── knowledge.py       → services.knowledge_tree_service, knowledge_publish_service
│   ├── materials.py       → services.material_service, document_processing_service
│   ├── blueprints.py      → services.blueprint_service, contract_service
│   ├── generation.py      → workflows.generation_graph
│   ├── exam_projects.py   → services.exam_project_service, contract_execution_service
│   └── paper_versions.py  → services.paper_version_service
│
├── workflows/
│   ├── framework_graph.py                → domain.framework.*, adapters.model.*
│   ├── generation_graph.py               → domain.generation.*, services.generation_service
│   ├── knowledge_catalog_subgraph.py     → domain.knowledge.models
│   └── organization_graph.py             → domain.knowledge.*, services.staging_retrieval_service
│
├── services/
│   ├── framework_service.py              → db.schema, domain.framework.*
│   ├── blueprint_persistence_service.py  → domain.framework.exam_rules, services.blueprint_service
│   ├── paper_version_service.py          → services.generation_service（答案解析）
│   └── …
│
├── domain/
│   ├── framework/     → exam_points, exam_rules, models
│   ├── generation/    → contract, batching, archetypes, coverage, semantic_diversity, structure_signature
│   ├── knowledge/     → models, relevance
│   ├── blueprint/     → models
│   ├── course/        → models
│   ├── material/      → models
│   └── model_calls.py
│
├── adapters/
│   ├── model/     → deepseek_gateway, deepseek_semantic_extractors, embedding_gateway
│   ├── document/  → mineru_client, local_text_parser, protocol
│   └── storage/   → minio_storage, local_storage
│
├── infrastructure/tasks/  → celery_app, worker, outbox, inline_runner, recovery, models
│
└── db/  → schema.py, init_db.py, session.py
```

### 12.2 前端模块依赖

```
src/
├── App.tsx
│   └── 依赖: pages/*（7 个页面路由 + 2 个旧路径重定向）
│
├── api/client.ts
│   └── 依赖: api/http, api/domains/*
│
├── pages/paper/
│   ├── index.tsx           → PipelinePanel, PaperPanel, lib/examDisplay
│   ├── PipelinePanel.tsx   → hooks/useNameMaps, lib/examDisplay, components/ui
│   └── PaperPanel.tsx      → hooks/useNameMaps, lib/examDisplay, components/ui
│
├── pages/framework/
│   ├── index.tsx           → ExamRulesCard, components/ui
│   └── ExamRulesCard.tsx   → api/client, lib/examDisplay
│
├── pages/{dashboard,materials,knowledge} → api/client, components/ui, stores/*
│
├── hooks/useNameMaps.ts    → api/client
├── lib/examDisplay.ts      → （纯常量，无依赖）
├── components/layout/      → Layout + Sidebar（react-router-dom）
└── stores/                 → zustand
```

---

## 13. 项目运行方式

### 13.1 环境要求

- **Python**: >= 3.12
- **Node.js**: >= 20
- **PostgreSQL**: >= 14
- **Redis**: >= 6.0
- **MinIO**: （可选，生产环境对象存储；开发环境可回退本地存储）

### 13.2 后端启动

#### 1. 安装依赖

```bash
cd backend
pip install -e .
```

#### 2. 配置环境变量

```bash
cp ../.env.example .env
# 编辑 .env 文件，填入必要的 API Key 和连接信息
```

#### 3. 初始化数据库

```bash
python -m app.db.init_db
# 插入开发测试数据
python -m app.db.init_db --seed
```

#### 4. 启动服务

```bash
# 开发模式
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 或使用 PowerShell 脚本（自动加载仓库根 .env）
powershell -File start_dev.ps1
```

#### 5. 启动 Celery Worker（真实生成必须）

```bash
cd backend
celery -A app.infrastructure.tasks.celery_app.celery_app worker --loglevel=INFO
```

#### 6. 健康检查

```bash
curl http://localhost:8000/api/v1/health
```

期望响应：
```json
{
  "api": "ok",
  "postgresql": "ok",
  "redis": "ok",
  "mineru": "configured",
  "deepseek": "configured"
}
```

### 13.3 前端启动

#### 1. 安装依赖

```bash
cd frontend
npm ci --registry=https://registry.npmmirror.com
```

#### 2. 启动开发服务器

```bash
npm run dev
```

访问 http://localhost:5173

#### 3. 构建生产版本

```bash
npm run build     # tsc -b && vite build，产物在 frontend/dist/
npm run lint      # oxlint
```

### 13.4 部署

一键启动 / 停止脚本位于 `deploy/`，会自动完成初始化数据库、启动 API 与 Worker、
构建前端：

```bash
sudo bash deploy/start.sh
sudo bash deploy/restart.sh
sudo bash deploy/stop.sh
```

Nginx 配置模板见 `deploy/nginx.conf.example`，详细步骤见
[docs/DEPLOY_UBUNTU.md](docs/DEPLOY_UBUNTU.md)。

---

## 14. 关键类与函数速查

### 14.1 后端核心类

| 类/函数 | 文件 | 说明 |
|----------|------|------|
| `Settings` | config.py | 全局配置 |
| `app = FastAPI()` | main.py | FastAPI 应用实例 |
| `FrameworkState` | framework_graph.py | 框架工作流状态 |
| `GenerationState` | generation_graph.py | 生成工作流状态 |
| `ContractSlot` | generation/contract.py | 单个题位合同 |
| `PaperContract` | generation/contract.py | 整卷合同 |
| `PoolAtom` | generation/contract.py | 知识原子池项 |
| `ExamPoint` | framework/exam_points.py | 考点模型 |
| `normalize_exam_rules` | framework/exam_rules.py | 考核规则归一化 |
| `type_rules_from_ratios` | framework/exam_rules.py | 按题型比例推导 type_rules |
| `answer_option_keys` | generation_service.py | 答案解析成选项字母 |
| `validate_generated_question` | generation_service.py | 单题质量门禁 |
| `BlueprintRequest` | blueprint/models.py | 蓝图请求 |
| `DeepSeekJsonClient` | adapters/model/deepseek_gateway.py | LLM 客户端 |
| `DatabaseFrameworkRepository` | services/framework_service.py | 框架仓库 |

### 14.2 前端核心组件

| 组件 | 文件 | 说明 |
|------|------|------|
| `PaperPage` | paper/index.tsx | 试卷模块外壳（列表 + 双页签详情） |
| `PipelinePanel` | paper/PipelinePanel.tsx | 出卷流水线（蓝图/合同/生成） |
| `PaperPanel` | paper/PaperPanel.tsx | 试卷双栏阅读器与题目编辑器 |
| `ExamRulesCard` | framework/ExamRulesCard.tsx | 考核规则查看与修改 |
| `Sidebar` | components/layout/Sidebar.tsx | 悬浮岛侧栏（含「试卷」入口） |
| `useNameMaps` | hooks/useNameMaps.ts | id → 中文名映射 |

---

## 15. 测试架构

### 15.1 后端测试

**测试框架**: pytest >= 8.0

```
backend/tests/
├── contract/        # 合同领域测试
├── domain/          # 领域模型与算法（聚类/分配/批次）
├── integration/     # 端到端（API + 工作流 + 上传解析）
├── unit/            # 单元测试（服务/提取器/规则/导出渲染/蓝图推导）
└── workflow/        # LangGraph 工作流测试
```

```bash
cd backend
python -m pytest tests/ -q --ignore=tests/unit/test_material_service.py
```

> `test_material_service.py` 依赖 boto3，环境缺该依赖时跳过。

### 15.2 前端验证

前端当前无单元测试文件，门禁是类型 + 构建 + lint：

```bash
cd frontend
npm run build     # tsc -b && vite build
npm run lint      # oxlint
```

---

## 16. 部署架构

### 16.1 目录结构

```
deploy/
├── install_ubuntu.sh            # Ubuntu 安装脚本
├── nginx.conf.example           # Nginx 配置示例
├── start.sh                     # 启动脚本
├── restart.sh                   # 重启脚本
└── stop.sh                      # 停止脚本
```

### 16.2 生产环境建议

- 使用 Gunicorn + Uvicorn workers 部署 FastAPI
- Nginx 作为反向代理，处理静态资源和 SSL 终止
- PostgreSQL 使用连接池（PgBouncer）
- Redis 使用持久化模式
- Celery 使用 RabbitMQ 或 Redis 作为 broker
- 前端构建后由 Nginx 托管静态文件

---

## 17. 附录

### 17.1 关键术语表

| 术语 | 英文 | 说明 |
|------|------|------|
| 评估导向大纲 | Assessment-Led Syllabus | 以考核大纲为主线构建的课程框架 |
| 考核规则 | Exam Rules | 考纲声明的题型比例与章节命题权重，可查看可修改 |
| 合同 | Contract | 试卷生成前的确定性题位约束 |
| 原子 | Atom | 知识卡片中的最小可考查单元 |
| 考点 | Exam Point | 课程中需要考核的知识点 |
| 蓝图 | Blueprint | 试卷的结构规划（题型、分值、分布） |
| 知识卡片 | Knowledge Card | 原子化组织后的知识单元 |
| 证据块 | Evidence Chunk | 知识卡片的内容来源引用 |
| 出卷流水线 | Pipeline | 蓝图 → 合同 → 生成三个阶段 |
| 试卷页签 | Paper Panel | 同一项目详情页里的查看/编辑/定稿/导出视图 |

### 17.2 相关文档

- [部署指南](docs/DEPLOY_UBUNTU.md)
- [交接文档](docs/HANDOVER.md)
- [后端 API 文档](docs/backend-api.md)
- [设计规格与实施计划](docs/superpowers/)

---

*文档更新时间: 2026-09-22*
*基于代码版本: 0.1.0*
