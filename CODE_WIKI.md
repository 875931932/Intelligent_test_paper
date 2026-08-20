# AI 智能出卷系统 - Code Wiki

## 1. 项目概述

本项目是一个基于 AI 的智能试卷生成系统，采用"评估导向大纲 + 合同制命题"的架构设计。系统能够根据教师提供的教学大纲和考核大纲，自动构建课程知识框架，并通过多阶段流水线生成高质量的试卷。

**核心价值**：
- 评估导向的课程知识框架自动构建
- 基于知识图谱的试题原子化组织
- 合同驱动的确定性试卷生成（配额、难度、认知层级精确控制）
- 教师可干预的多阶段人工确认机制

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
| **Redis** | >=5.0 | 缓存与会话存储 |
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
| **TypeScript** | 7.0.2 | 类型安全 |
| **Vite** | 8.2.1 | 构建工具 |
| **Vitest** | ^2.1.0 | 测试框架 |
| **Lucide React** | 1.31.0 | 图标库 |

**依赖文件**：[frontend/package.json](frontend/package.json)

---

## 3. 系统架构

### 3.1 高层架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                        前端层 (React + TypeScript)               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │ Exam Workspace│  │ Knowledge    │  │ Course Space │          │
│  │ (4-Stage)    │  │ Catalog      │  │ Home         │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     API 网关层 (FastAPI)                         │
│  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐      │
│  │Courses │ │Materials│ │Framework│ │Knowledge│ │Blueprint│      │
│  │ Router │ │ Router │ │ Router │ │ Router │ │ Router │      │
│  └────────┘ └────────┘ └────────┘ └────────┘ └────────┘      │
│  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐      │
│  │Generation│ │Exam    │ │Paper   │ │Health  │ │Storage │      │
│  │ Router   │ │Projects│ │Versions│ │ Check  │ │ Fallback│      │
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
│ • Generation  │     │ • ~20 表      │     │ • Embedding   │
│   Graph       │     │ • 外键约束    │     │ • 对象存储     │
│ • Knowledge   │     │               │     │               │
│   Graph       │     │               │     │               │
│ • Organization│     │               │     │               │
│   Graph       │     │               │     │               │
└───────────────┘     └───────────────┘     └───────────────┘
```

### 3.2 数据流架构

```
教师上传资料 → 文档解析 → 框架构建 → 知识组织 → 蓝图/合同 → 生成 → 审阅导出
     │              │          │          │          │         │         │
     ▼              ▼          ▼          ▼          ▼         ▼         ▼
  Materials    Parse Runs  Framework  Knowledge   Blueprint  Questions Paper
  (文件管理)   (解析任务)  Build Runs Catalog     Contract   Generation Versions
```

### 3.3 多阶段流水线

系统通过前端 `ExamProjectWorkspace` 组件实现 4 阶段可视化流水线：

| 阶段 | 前端组件 | 后端路由 | 说明 |
|------|----------|----------|------|
| **Blueprint** | `BlueprintStage` | `blueprints.py` | 试卷蓝图分配与确认 |
| **Contract** | `ContractStage` | `blueprints.py` | 合同分配与原子绑定 |
| **Generating** | `GenerationStage` | `generation.py` | 试题生成与质量检查 |
| **Review/Exported** | `ReviewExportStage` | `paper_versions.py` | 审阅、修订与导出 |

**前端入口**：[frontend/src/console/exam/examProjectWorkspace.tsx](frontend/src/console/exam/examProjectWorkspace.tsx)

---

## 4. 核心领域模型

### 4.1 Framework（评估导向大纲）

**职责**：构建以考核大纲为主线的课程知识框架，包括教学主题、考核范围、考点及其权重。

**核心模型**：[backend/app/domain/framework/models.py](backend/app/domain/framework/models.py)

```python
class AssessmentOutline(BaseModel):
    """考核大纲提取结果"""
    anchors: list[AssessmentAnchor]        # 考核范围
    exam_points: list[ExamPoint]            # 考点列表
    final_exam_rules: dict                  # 期末考试规则

class FrameworkCandidate(BaseModel):
    """框架候选版本"""
    anchors: list[AssessmentAnchor]
    exam_points: list[ExamPoint]
    teaching_topics: list[TeachingTopic]
    conflicts: list[FrameworkConflict]      # 冲突列表（教师需确认）
    final_exam_rules: dict

class FrameworkConfirmation(BaseModel):
    """教师确认结果"""
    conflict_resolutions: dict[str, str]   # 冲突解决方式
```

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
    items: list[PlanItem]           # 题位计划
    units: list[UnitCoverage]       # 单元覆盖计划
    chapter_weights: dict[str, float]  # 章节权重
    type_rules: dict                 # 题型规则

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
- `extract_assessment_syllabus()`: LLM 提取考核大纲
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
- 合同约束校验（配额、难度、认知层级）

### 5.3 Knowledge Catalog Subgraph

**文件**：[backend/app/workflows/knowledge_catalog_subgraph.py](backend/app/workflows/knowledge_catalog_subgraph.py)

**职责**：为每个已确认的考点构建来源分离的知识目录候选，支持多来源（教材、课件、习题等）的知识组织。

### 5.4 Organization Graph

**文件**：[backend/app/workflows/organization_graph.py](backend/app/workflows/organization_graph.py)

**职责**：以考点为导向的材料整理流程，将原始材料组织为结构化的知识卡片。

---

## 6. API 端点

### 6.1 路由结构

FastAPI 应用在 [backend/app/main.py](backend/app/main.py) 中注册了 8 个核心路由器：

```python
app = FastAPI(title="AI Exam System")
app.include_router(courses_router)         # /api/v1/courses/{course_id}
app.include_router(materials_router)       # /api/v1/courses/{course_id}/materials
app.include_router(framework_router)       # /api/v1/courses/{course_id}/framework-runs
app.include_router(knowledge_router)       # /api/v1/courses/{course_id}/knowledge
app.include_router(blueprints_router)      # /api/v1/courses/{course_id}/blueprints
app.include_router(generation_router)      # /api/v1/courses/{course_id}/generation-runs
app.include_router(exam_projects_router)   # /api/v1/courses/{course_id}/exam-projects
app.include_router(paper_versions_router)  # /api/v1/courses/{course_id}/paper-versions
```

### 6.2 核心端点

#### Framework 端点 [backend/app/api/v1/framework.py](backend/app/api/v1/framework.py)

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/api/v1/courses/{course_id}/framework-runs` | 创建框架构建任务，触发 LangGraph 工作流 |
| GET | `/api/v1/courses/{course_id}/framework-runs/latest` | 获取最新框架构建任务 |
| GET | `/api/v1/courses/{course_id}/framework-runs/{run_id}` | 获取指定任务详情 |
| GET | `/api/v1/courses/{course_id}/framework-runs/{run_id}/candidate` | 获取框架候选内容 |
| POST | `/api/v1/courses/{course_id}/framework-runs/{run_id}/confirm` | 教师确认框架 |
| POST | `/api/v1/courses/{course_id}/framework-runs/{run_id}/reject` | 教师拒绝框架 |
| GET | `/api/v1/courses/{course_id}/framework-versions/current` | 获取当前已发布框架 |

#### Generation 端点 [backend/app/api/v1/generation.py](backend/app/api/v1/generation.py)

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/api/v1/courses/{course_id}/generation-runs` | 创建试卷生成任务，触发合同制生成 |

#### Blueprints 端点 [backend/app/api/v1/blueprints.py](backend/app/api/v1/blueprints.py)

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/api/v1/courses/{course_id}/blueprints/allocate` | 分配试卷蓝图 |
| POST | `/api/v1/courses/{course_id}/blueprints/confirm` | 确认蓝图 |

#### Health 端点 [backend/app/main.py](backend/app/main.py)

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/api/v1/health` | 健康检查（PostgreSQL、Redis、MinerU、DeepSeek 状态） |

---

## 7. 服务层

### 7.1 服务概览

| 服务文件 | 职责 |
|----------|------|
| [framework_service.py](backend/app/services/framework_service.py) | 框架构建的持久化与状态管理 |
| [contract_service.py](backend/app/services/contract_service.py) | 试卷合同的分配与审计 |
| [contract_execution_service.py](backend/app/services/contract_execution_service.py) | 合同执行与原子分配 |
| [generation_service.py](backend/app/services/generation_service.py) | 试题生成与质量校验 |
| [generation_runner_service.py](backend/app/services/generation_runner_service.py) | 生成任务运行器 |
| [blueprint_service.py](backend/app/services/blueprint_service.py) | 蓝图分配逻辑 |
| [blueprint_persistence_service.py](backend/app/services/blueprint_persistence_service.py) | 蓝图持久化 |
| [knowledge_tree_service.py](backend/app/services/knowledge_tree_service.py) | 知识树构建 |
| [knowledge_publish_service.py](backend/app/services/knowledge_publish_service.py) | 知识目录发布 |
| [document_processing_service.py](backend/app/services/document_processing_service.py) | 文档解析服务（MinerU 集成） |
| [material_service.py](backend/app/services/material_service.py) | 材料管理 |
| [exam_project_service.py](backend/app/services/exam_project_service.py) | 考试项目管理 |
| [paper_version_service.py](backend/app/services/paper_version_service.py) | 试卷版本管理 |
| [model_call_service.py](backend/app/services/model_call_service.py) | 模型调用记录 |
| [staging_retrieval_service.py](backend/app/services/staging_retrieval_service.py) | 暂存区检索 |

### 7.2 关键服务详解

#### FrameworkService

**职责**：管理框架构建任务的完整生命周期。

**关键函数**：
```python
def create_framework_run(session, course_id, teaching_material_version_id, assessment_material_version_id) -> dict
def confirm_framework_run(session, course_id, run_id, confirmation) -> dict
def reject_framework_run(session, course_id, run_id) -> dict
def get_current_framework(session, course_id) -> dict
```

**仓库模式**：
```python
class DatabaseFrameworkRepository(FrameworkRepository):
    def persist_candidate(self, state, candidate) -> str
    def publish(self, state, confirmation) -> str
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
    def extract_assessment(self, blocks, call_context) -> AssessmentOutline
```

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

**核心表结构**（~20 张表）：

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
| `framework_versions` | 框架版本（candidate/published/superseded/rejected） |
| `framework_anchors` | 框架锚点（考核范围） |
| `framework_conflicts` | 框架冲突（教师确认项） |
| `exam_points` | 考点表 |

#### 知识图谱
| 表名 | 说明 |
|------|------|
| `knowledge_catalog_versions` | 知识目录版本 |
| `knowledge_cards` | 知识卡片 |
| `evidence_chunks` | 证据块（来源引用） |

#### 试卷生成
| 表名 | 说明 |
|------|------|
| `exam_projects` | 考试项目 |
| `blueprint_versions` | 蓝图版本 |
| `plan_items` | 题位计划 |
| `generation_runs` | 生成任务 |
| `generated_questions` | 生成的试题 |
| `paper_versions` | 试卷版本 |
| `paper_items` | 试卷题项 |

#### 基础设施
| 表名 | 说明 |
|------|------|
| `model_calls` | 模型调用记录 |
| `task_runs` | 异步任务记录 |
| `outbox_events` | 出站事件（事件溯源） |

该 schema 共定义 **约 28 张表**，包括 `User`、`Course` 以及 26 张课程级业务/基础设施表。

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
├── console/
│   ├── exam/                    # 考试项目工作台
│   │   ├── examProjectWorkspace.tsx   # 主工作区（4阶段流水线）
│   │   ├── pipelineNav.tsx            # 阶段导航
│   │   ├── blueprintStage.tsx         # 蓝图阶段
│   │   ├── contractStage.tsx          # 合同阶段
│   │   ├── generationStage.tsx        # 生成阶段
│   │   └── reviewExportStage.tsx      # 审阅导出阶段
│   ├── knowledge/               # 知识目录可视化
│   │   ├── knowledgeCatalog.tsx       # 知识目录主组件
│   │   ├── treeView.tsx               # 树形视图
│   │   ├── graphView.tsx              # 图形视图
│   │   └── detailDrawer.tsx           # 详情抽屉
│   ├── shell/
│   │   └── Layout.tsx                 # 全局布局
│   ├── steps/                   # 向导步骤
│   │   ├── MaterialsStep.tsx
│   │   ├── FrameworkStep.tsx
│   │   ├── KnowledgeStep.tsx
│   │   └── ExamStep.tsx
│   ├── client.ts                # API 客户端
│   ├── types.ts                 # TypeScript 类型定义
│   └── nav.ts                   # 导航配置
├── App.tsx                      # 根组件
├── main.tsx                     # 入口文件
└── api.ts                       # HTTP 请求封装
```

### 10.2 核心组件

#### ExamProjectWorkspace（考试项目工作区）

**文件**：[frontend/src/console/exam/examProjectWorkspace.tsx](frontend/src/console/exam/examProjectWorkspace.tsx)

**职责**：编排 4 阶段流水线，根据项目状态动态渲染对应阶段组件。

**阶段转换逻辑**：
```typescript
const STATUS_TO_STAGE: Record<string, PipelineStage> = {
  blueprint: 'blueprint',
  contract: 'contract',
  generating: 'generating',
  review: 'review',
  exported: 'exported',
}

const currentStage = STATUS_TO_STAGE[project.status] ?? 'blueprint'
```

**各阶段回调**：
- `handleConfirmBlueprint`: 确认蓝图后刷新项目
- `handleGenerate`: 确认合同后刷新项目
- `handleProceedToReview`: 生成完成后推进到审阅
- `handleExport`: 导出试卷后推进到已导出

#### API 客户端

**文件**：[frontend/src/api.ts](frontend/src/api.ts)

**核心函数**：
```typescript
export async function api<T>(path: string, options?: RequestInit): Promise<T>
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
    organization_retrieval_top_k: int = 24
    organization_retrieval_min_score: float = 0.25
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
│   └── 依赖: config, api.v1.* (8 routers)
│
├── config.py
│   └── 依赖: pydantic-settings
│
├── api/v1/
│   ├── framework.py
│   │   └── 依赖: workflows.framework_graph, services.framework_service, adapters.model.*
│   ├── generation.py
│   │   └── 依赖: workflows.generation_graph, adapters.model.deepseek_gateway
│   ├── blueprints.py
│   │   └── 依赖: services.blueprint_service, services.contract_service
│   ├── knowledge.py
│   │   └── 依赖: services.knowledge_tree_service, services.knowledge_publish_service
│   ├── materials.py
│   │   └── 依赖: services.material_service, services.document_processing_service
│   ├── exam_projects.py
│   │   └── 依赖: services.exam_project_service
│   ├── paper_versions.py
│   │   └── 依赖: services.paper_version_service
│   └── courses.py
│       └── 依赖: services.course_service
│
├── workflows/
│   ├── framework_graph.py
│   │   └── 依赖: domain.framework.models, adapters.model.deepseek_semantic_extractors
│   ├── generation_graph.py
│   │   └── 依赖: domain.generation.contract, adapters.model.deepseek_gateway
│   ├── organization_graph.py
│   │   └── 依赖: domain.knowledge.models, services.staging_retrieval_service
│   └── knowledge_catalog_subgraph.py
│       └── 依赖: domain.knowledge.models
│
├── services/
│   ├── framework_service.py
│   │   └── 依赖: db.schema, domain.framework.models
│   ├── contract_service.py
│   │   └── 依赖: domain.generation.contract, domain.blueprint.models
│   ├── generation_service.py
│   │   └── 依赖: domain.generation.contract
│   ├── blueprint_service.py
│   │   └── 依赖: domain.blueprint.models
│   ├── document_processing_service.py
│   │   └── 依赖: adapters.document.mineru_client, adapters.document.local_text_parser
│   └── ...
│
├── domain/
│   ├── framework/
│   │   └── 依赖: pydantic
│   ├── generation/
│   │   └── 依赖: pydantic, domain.blueprint.models, domain.knowledge.relevance
│   ├── knowledge/
│   │   └── 依赖: pydantic
│   └── blueprint/
│       └── 依赖: pydantic
│
├── adapters/
│   ├── model/
│   │   ├── deepseek_gateway.py
│   │   │   └── 依赖: httpx, services.model_call_service
│   │   └── deepseek_semantic_extractors.py
│   │       └── 依赖: deepseek_gateway
│   ├── document/
│   │   └── 依赖: httpx, pydantic
│   └── storage/
│       └── 依赖: boto3
│
└── db/
    └── schema.py
        └── 依赖: sqlalchemy
```

### 12.2 前端模块依赖

```
src/
├── App.tsx
│   └── 依赖: console/shell/Layout, console/nav
│
├── console/
│   ├── shell/Layout.tsx
│   │   └── 依赖: console/nav, console/ui
│   │
│   ├── exam/examProjectWorkspace.tsx
│   │   ├── 依赖: console/exam/pipelineNav
│   │   ├── 依赖: console/exam/blueprintStage
│   │   ├── 依赖: console/exam/contractStage
│   │   ├── 依赖: console/exam/generationStage
│   │   └── 依赖: console/exam/reviewExportStage
│   │
│   ├── knowledge/knowledgeCatalog.tsx
│   │   ├── 依赖: knowledge/treeView
│   │   ├── 依赖: knowledge/graphView
│   │   └── 依赖: knowledge/detailDrawer
│   │
│   ├── client.ts
│   │   └── 依赖: api.ts
│   │
│   └── types.ts
│       └── 定义: PipelineStage, ExamProjectDetail, etc.
│
└── api.ts
    └── 封装: fetch API
```

---

## 13. 项目运行方式

### 13.1 环境要求

- **Python**: >= 3.12
- **Node.js**: >= 18 (推荐 20+)
- **PostgreSQL**: >= 14
- **Redis**: >= 6.0
- **MinIO**: (可选，用于生产环境对象存储)

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
```

#### 4. 启动服务

```bash
# 开发模式
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 或使用 PowerShell 脚本
powershell -File start_dev.ps1
```

#### 5. 健康检查

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
npm install
```

#### 2. 启动开发服务器

```bash
npm run dev
```

访问 http://localhost:5173

#### 3. 构建生产版本

```bash
npm run build
npm run preview
```

### 13.4 异步任务（Celery）

如需处理文档解析等异步任务：

```bash
cd backend
celery -A app.infrastructure.tasks.celery_app worker --loglevel=info
```

### 13.5 Docker Compose（推荐）

```bash
# 启动完整开发环境
docker-compose -f docker-compose.dev.yml up -d

# 包含服务：
# - PostgreSQL
# - Redis
# - MinIO
# - Backend API
# - Frontend (Nginx)
```

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
| `BlueprintRequest` | blueprint/models.py | 蓝图请求 |
| `DeepSeekJsonClient` | adapters/model/deepseek_gateway.py | LLM 客户端 |
| `DatabaseFrameworkRepository` | services/framework_service.py | 框架仓库 |

### 14.2 前端核心组件

| 组件 | 文件 | 说明 |
|------|------|------|
| `ExamProjectWorkspace` | exam/examProjectWorkspace.tsx | 考试项目主工作区 |
| `PipelineNav` | exam/pipelineNav.tsx | 阶段导航 |
| `BlueprintStage` | exam/blueprintStage.tsx | 蓝图阶段 |
| `ContractStage` | exam/contractStage.tsx | 合同阶段 |
| `GenerationStage` | exam/generationStage.tsx | 生成阶段 |
| `ReviewExportStage` | exam/reviewExportStage.tsx | 审阅导出阶段 |
| `KnowledgeCatalog` | knowledge/knowledgeCatalog.tsx | 知识目录 |
| `Layout` | shell/Layout.tsx | 全局布局 |

---

## 15. 测试架构

### 15.1 后端测试

**测试框架**: pytest >= 8.0

**测试结构**：
```
backend/tests/
├── contract/                    # 合同领域测试
│   ├── test_mineru_fake_provider.py
├── domain/                      # 领域模型测试
│   ├── test_batching.py
│   ├── test_contract_assignment.py
│   ├── test_contract_clustering.py
│   ├── test_contract_models.py
│   └── test_contract_pool.py
├── integration/                 # 集成测试
│   ├── test_contract_e2e.py
│   ├── test_exam_point_pipeline.py
│   ├── test_framework_api.py
│   ├── test_knowledge_api.py
│   ├── test_material_upload.py
│   ├── test_parse_and_published_knowledge.py
│   ├── test_pipeline_e2e.py
│   └── test_worker_recovery.py
├── unit/                        # 单元测试
│   ├── test_blueprint_allocation.py
│   ├── test_contract_execution.py
│   ├── test_deepseek_semantic_extractors.py
│   ├── test_question_quality_rules.py
│   └── ...
└── workflow/                    # 工作流测试
    ├── test_framework_graph.py
    ├── test_generation_graph.py
    ├── test_knowledge_catalog_subgraph.py
    └── test_organization_graph.py
```

### 15.2 前端测试

**测试框架**: Vitest + React Testing Library

```bash
cd frontend
npm test
```

---

## 16. 部署架构

### 16.1 目录结构

```
deploy/
├── install_ubuntu.sh            # Ubuntu 安装脚本
├── nginx.conf.example           # Nginx 配置示例
├── start.sh                     # 启动脚本
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
| 合同 | Contract | 试卷生成前的确定性题位约束 |
| 原子 | Atom | 知识卡片中的最小可考查单元 |
| 考点 | Exam Point | 课程中需要考核的知识点 |
| 蓝图 | Blueprint | 试卷的结构规划（题型、分值、分布） |
| 知识卡片 | Knowledge Card | 原子化组织后的知识单元 |
| 证据块 | Evidence Chunk | 知识卡片的内容来源引用 |

### 17.2 相关文档

- [部署指南](../deploy/DEPLOY_UBUNTU.md)
- [交接文档](../deploy/HANDOVER.md)
- [项目计划文档](../docs/superpowers/plans/)
- [技术规格文档](../docs/superpowers/specs/)

---

*文档生成时间: 2026-08-20*
*基于代码版本: 0.1.0*
