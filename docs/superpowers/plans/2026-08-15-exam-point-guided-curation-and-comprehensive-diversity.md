# 考点驱动资料整理与综合题多样化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有“资料先自由提取知识点”的链路改为“考纲考试考点先行、资料按考点定向取证”，并让蓝图控制考查方式、综合题使用多种原型且支持同卷与跨卷结构去重。

**Architecture:** `FrameworkGraph` 先从考核大纲形成可确认的 `ExamPoint`，教学大纲只校验范围与深度；`OrganizationGraph` 对暂存资料执行混合检索，并以“一个考点 + 一个文件”为模型任务产生相关性判断、考核单元和纯净知识卡。`GenerationGraph` 在全卷覆盖规划中增加题位考查方式、综合题原型和历史结构签名，生成载荷继续与文件名、页码、证据 ID 和来源原文隔离。

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, SQLAlchemy 2, PostgreSQL/SQLite tests, LangGraph, httpx, DeepSeek JSON mode, OpenAI-compatible embedding API, pytest, React 19, TypeScript, Vite.

---

## Scope And File Map

本计划是一条有顺序依赖的质量链，不拆成互相独立的子项目。后续任务只在前一任务的稳定契约上工作。

### New backend files

- `backend/app/domain/framework/exam_points.py`：`ExamPoint`、权重来源和操作细节政策。
- `backend/app/domain/model_calls.py`：跨工作流共享的脱敏模型调用上下文。
- `backend/app/domain/knowledge/relevance.py`：资料相关性判断、考点覆盖状态和模型输入/输出契约。
- `backend/app/services/staging_retrieval_service.py`：暂存证据的关键词与向量混合检索。
- `backend/app/adapters/model/embedding_gateway.py`：OpenAI-compatible embedding 客户端。
- `backend/app/adapters/model/deepseek_semantic_extractors.py`：大纲抽取、“考点 + 文件”证据分类和逐考点最小充分归并适配器。
- `backend/app/services/model_call_service.py`：框架/整理模型调用的脱敏诊断记录。
- `backend/app/domain/generation/archetypes.py`：综合题原型及适配规则。
- `backend/app/domain/generation/structure_signature.py`：综合题结构签名生成和近期签名读取。
- `backend/tests/unit/test_exam_point_models.py`：考点、权重与操作政策测试。
- `backend/tests/unit/test_staging_retrieval.py`：混合召回和低相关剔除测试。
- `backend/tests/unit/test_knowledge_relevance.py`：四级相关性与操作准入测试。
- `backend/tests/unit/test_comprehensive_archetypes.py`：原型分配和签名测试。
- `backend/tests/integration/test_exam_point_pipeline.py`：框架、整理、蓝图和生成的端到端假模型测试。

### Existing backend files to modify

- `backend/app/db/schema.py`：持久化考试考点、考点证据判断、定位信息和题位考查方式。
- `backend/app/domain/framework/models.py`：大纲抽取、候选框架和教师确认包含考试考点。
- `backend/app/workflows/framework_graph.py`：增加考点构建、教学深度对齐和冲突节点。
- `backend/app/services/framework_service.py`：候选/已发布考点持久化。
- `backend/app/domain/knowledge/models.py`：考核单元绑定考试考点，知识卡支持纯净题干素材。
- `backend/app/workflows/organization_graph.py`：替换“每文件面对全部锚点自由抽取”为按考点定向整理。
- `backend/app/workflows/knowledge_catalog_subgraph.py`：按考试考点归并候选。
- `backend/app/services/knowledge_tree_service.py`：基于相关性和操作政策做准入，不依赖课程词表。
- `backend/app/services/knowledge_publish_service.py`：创建暂存证据、保存分类和发布考点关联。
- `backend/app/api/v1/framework.py`、`backend/app/api/v1/knowledge.py`：接入真实 DeepSeek/embedding 适配器并返回覆盖统计。
- `backend/app/domain/blueprint/models.py`、`backend/app/services/blueprint_service.py`：题位级 `assessment_mode` 和题型内模式配额。
- `backend/app/domain/generation/coverage.py`：综合题原型、材料形式和历史签名进入全卷规划。
- `backend/app/schemas/generation.py`：原型专用模板和来源无关载荷。
- `backend/app/workflows/generation_graph.py`：原型规划、签名输出和局部修复。
- `backend/app/services/generation_service.py`：综合题结构、分问数量和签名冲突检查。
- `backend/app/adapters/model/deepseek_gateway.py`：全卷主脑和命题提示接受新增合同。
- `backend/app/api/v1/generation.py`：加载近期结构签名。
- `backend/app/config.py`、`.env.example`、`backend/pyproject.toml`：embedding 和并发配置。

### Demo and UI files to modify

- `backend/scripts/build_real_material_demo.py`：真实资料验证改用考点优先链路。
- `frontend/src/App.tsx`：显示考试考点覆盖、相关性分类、题位考查方式和综合题原型。
- `frontend/src/styles.css`：新增紧凑的覆盖与原型状态样式。

### Database note

仓库仍采用初始 schema，不创建历史迁移脚本。所有自动化测试使用新建 SQLite/PostgreSQL 测试库。执行本计划时不得自动删除或重建远程数据库；需要把新 schema 应用于现有开发库时，先由用户明确批准一次可恢复的开发库重建或单独的 DDL 操作。

---

### Task 1: 建立考试考点和相关性持久化骨架

**Files:**
- Create: `backend/app/domain/framework/exam_points.py`
- Create: `backend/app/domain/model_calls.py`
- Modify: `backend/app/db/schema.py`
- Modify: `backend/tests/unit/test_database_bootstrap.py`
- Create: `backend/tests/unit/test_exam_point_models.py`

- [ ] **Step 1: 写考试考点领域模型失败测试**

```python
from pydantic import ValidationError
import pytest

from app.domain.framework.exam_points import ExamPoint, OperationalDetailPolicy


def test_exam_point_keeps_weight_provenance_and_operational_policy():
    point = ExamPoint(
        code="rag-retrieval",
        anchor_key="rag",
        title="检索链路与效果诊断",
        assessment_requirement="能够解释检索链路并诊断召回偏差",
        weight_value=25,
        weight_source="assessment_syllabus",
        weight_group_id="rag",
        cognitive_targets=["understand", "analyze"],
        assessment_orientations=["conceptual", "problem_solving"],
        allowed_question_types=["short_answer", "comprehensive"],
        operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
        required_evidence_roles=["fact_or_constraint", "answer_or_rubric_basis"],
        retrieval_intent="检索链路、召回偏差及诊断依据",
    )

    assert point.weight_source == "assessment_syllabus"
    assert point.operational_detail_policy == "supporting_only"


def test_exam_point_rejects_empty_assessment_requirement():
    with pytest.raises(ValidationError, match="assessment_requirement"):
        ExamPoint(
            code="invalid",
            anchor_key="rag",
            title="无效考点",
            assessment_requirement="",
            weight_value=0,
            weight_source="inherited_group",
            weight_group_id="rag",
            retrieval_intent="无效",
        )
```

- [ ] **Step 2: 运行测试并确认领域模型尚不存在**

Run: `cd backend; pytest tests/unit/test_exam_point_models.py -q`

Expected: FAIL with `ModuleNotFoundError: app.domain.framework.exam_points`.

- [ ] **Step 3: 实现考试考点领域模型**

```python
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class WeightSource(StrEnum):
    ASSESSMENT_SYLLABUS = "assessment_syllabus"
    INHERITED_GROUP = "inherited_group"
    TEACHER_CONFIRMED = "teacher_confirmed"


class OperationalDetailPolicy(StrEnum):
    FORBIDDEN = "forbidden"
    SUPPORTING_ONLY = "supporting_only"
    DIRECTLY_ASSESSABLE = "directly_assessable"


class ExamPoint(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    anchor_key: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=255)
    assessment_requirement: str = Field(min_length=1)
    weight_value: float = Field(ge=0, le=100)
    weight_source: WeightSource
    weight_group_id: str = Field(min_length=1, max_length=160)
    priority: str = "normal"
    cognitive_targets: list[str] = Field(default_factory=list)
    assessment_orientations: list[str] = Field(default_factory=list)
    allowed_question_types: list[str] = Field(default_factory=list)
    operational_detail_policy: OperationalDetailPolicy = OperationalDetailPolicy.SUPPORTING_ONLY
    scope_boundary: dict = Field(default_factory=dict)
    required_evidence_roles: list[str] = Field(default_factory=list)
    retrieval_intent: str = Field(min_length=1)
    assessment_anchor_keys: list[str] = Field(default_factory=list)
    teaching_anchor_keys: list[str] = Field(default_factory=list)
    status: str = "candidate"

    @field_validator("assessment_requirement", "retrieval_intent")
    @classmethod
    def require_non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value.strip()
```

- [ ] **Step 4: 写 schema 失败测试**

先在 `model_calls.py` 增加不含正文的上下文：

```python
class ModelCallContext(BaseModel):
    course_id: str
    framework_build_run_id: str | None = None
    organization_run_id: str | None = None
    generation_attempt_id: str | None = None
    stage: str
```

在 `test_database_bootstrap.py` 增加：

```python
def test_schema_persists_exam_points_and_point_evidence_decisions():
    assert {"exam_points", "exam_point_evidence_links"} <= CORE_TABLE_NAMES
    points = Base.metadata.tables["exam_points"]
    assert {
        "framework_version_id", "anchor_key", "code", "title",
        "assessment_requirement", "weight_value", "weight_source",
        "weight_group_id", "operational_detail_policy", "retrieval_intent",
    } <= set(points.c.keys())

    decisions = Base.metadata.tables["exam_point_evidence_links"]
    assert {
        "organization_run_id", "exam_point_id", "evidence_chunk_id",
        "relevance_class", "support_claim", "evidence_role", "confidence",
    } <= set(decisions.c.keys())

    assert "exam_point_id" in Base.metadata.tables["assessment_units"].c
    assert "assessment_mode" in Base.metadata.tables["plan_items"].c
    assert {"content_block_id", "locator", "embedding"} <= set(Base.metadata.tables["evidence_chunks"].c.keys())
    assert {
        "framework_build_run_id", "organization_run_id", "stage", "prompt_hash",
        "input_tokens", "output_tokens", "duration_ms", "error_code", "details", "created_at",
    } <= set(Base.metadata.tables["model_calls"].c.keys())
```

- [ ] **Step 5: 扩展初始 schema**

在 `schema.py` 导入 `Float`，新增 `exam_points`、`exam_point_evidence_links`，并扩展现有表：

```python
exam_points = _course_table(
    "exam_points",
    Column("framework_version_id", String(64), ForeignKey("framework_versions.id"), nullable=False),
    Column("anchor_key", String(160), nullable=False),
    Column("code", String(100), nullable=False),
    Column("title", String(255), nullable=False),
    Column("assessment_requirement", Text, nullable=False),
    Column("weight_value", Float, nullable=False, default=0),
    Column("weight_source", String(40), nullable=False),
    Column("weight_group_id", String(160), nullable=False),
    Column("priority", String(40), nullable=False, default="normal"),
    Column("cognitive_targets", JSON, nullable=False, default=list),
    Column("assessment_orientations", JSON, nullable=False, default=list),
    Column("allowed_question_types", JSON, nullable=False, default=list),
    Column("operational_detail_policy", String(40), nullable=False, default="supporting_only"),
    Column("scope_boundary", JSON, nullable=False, default=dict),
    Column("required_evidence_roles", JSON, nullable=False, default=list),
    Column("retrieval_intent", Text, nullable=False),
    Column("teaching_anchor_keys", JSON, nullable=False, default=list),
    Column("status", String(40), nullable=False, default="candidate"),
    constraints=(
        UniqueConstraint("framework_version_id", "code", name="uq_exam_points_framework_code"),
        CheckConstraint("weight_value >= 0 AND weight_value <= 100", name="ck_exam_points_weight"),
        CheckConstraint("operational_detail_policy IN ('forbidden','supporting_only','directly_assessable')", name="ck_exam_points_operation_policy"),
    ),
)

exam_point_evidence_links = _course_table(
    "exam_point_evidence_links",
    Column("organization_run_id", String(64), ForeignKey("organization_runs.id"), nullable=False),
    Column("exam_point_id", String(64), ForeignKey("exam_points.id"), nullable=False),
    Column("evidence_chunk_id", String(64), ForeignKey("evidence_chunks.id"), nullable=False),
    Column("relevance_class", String(40), nullable=False),
    Column("support_claim", Text, nullable=False),
    Column("evidence_role", String(60)),
    Column("confidence", Integer),
    Column("prompt_material", Text),
    Column("status", String(40), nullable=False, default="candidate"),
    constraints=(
        UniqueConstraint("organization_run_id", "exam_point_id", "evidence_chunk_id", name="uq_exam_point_evidence_decision"),
        CheckConstraint("relevance_class IN ('direct','supporting','background','out_of_scope')", name="ck_exam_point_evidence_relevance"),
    ),
)
```

同时给 `evidence_chunks` 增加可空的 `content_block_id`、`locator`、`embedding`，给 `assessment_units` 增加可空 `exam_point_id`，给 `plan_items` 增加带 `server_default="conceptual"` 的非空 `assessment_mode`。可空字段用于读取任务候选和兼容当前开发夹具；Task 5 的发布服务负责保证 active 考核单元必须有考点关联。将两个新表加入 `CORE_TABLE_NAMES` 和复合外键清单。

扩展 `model_calls`：保留现有 `generation_attempt_id`，新增可空 `framework_build_run_id`、`organization_run_id`，并增加 `stage`、`prompt_hash`、token、耗时、`error_code`、`error_message`、`details` 和 `created_at`。日志只保存 prompt hash、模型元数据和错误摘要，不保存完整大纲/教学资料请求。

- [ ] **Step 6: 运行领域和 schema 测试**

Run: `cd backend; pytest tests/unit/test_exam_point_models.py tests/unit/test_database_bootstrap.py tests/integration/test_knowledge_publish_service.py -q`

Expected: PASS.

- [ ] **Step 7: 提交持久化骨架**

```powershell
git add backend/app/domain/framework/exam_points.py backend/app/domain/model_calls.py backend/app/db/schema.py backend/tests/unit/test_exam_point_models.py backend/tests/unit/test_database_bootstrap.py
git commit -m "feat: add exam point persistence contracts"
```

---

### Task 2: 让 FrameworkGraph 生成并确认考试考点

**Files:**
- Modify: `backend/app/domain/framework/models.py`
- Modify: `backend/app/workflows/framework_graph.py`
- Modify: `backend/app/services/framework_service.py`
- Modify: `backend/tests/unit/test_framework_rules.py`
- Modify: `backend/tests/workflow/test_framework_graph.py`
- Modify: `backend/tests/integration/test_framework_api.py`

- [ ] **Step 1: 写考点先行的框架失败测试**

扩展现有 `_graph()` helper，使 `AssessmentOutline` 固定返回两个 `ExamPoint`，再断言候选框架保留考纲权重来源、教学大纲对齐和操作政策：

```python
def test_framework_builds_exam_points_before_material_organization():
    graph, _, repository = _graph()
    paused = graph.invoke(_state(), config={"configurable": {"thread_id": "exam-points"}})

    point = repository.candidates[0][1].exam_points[0]
    assert point.code == "rag-diagnosis"
    assert point.weight_source == "assessment_syllabus"
    assert point.teaching_anchor_keys == ["taught-rag"]
    assert point.operational_detail_policy == "supporting_only"
    assert "__interrupt__" in paused
```

再增加两个失败场景：考点找不到教学覆盖时产生 `missing_teaching_coverage`；只给章节权重时考点使用 `inherited_group`，不能被代码均分。

- [ ] **Step 2: 运行框架测试并确认候选模型缺少 exam_points**

Run: `cd backend; pytest tests/unit/test_framework_rules.py tests/workflow/test_framework_graph.py -q`

Expected: FAIL because `FrameworkCandidate` has no `exam_points` field.

- [ ] **Step 3: 扩展大纲抽取和确认契约**

在 `models.py` 中增加：

```python
class AssessmentOutline(BaseModel):
    anchors: list[AssessmentAnchor]
    exam_points: list[ExamPoint]
    final_exam_rules: dict = Field(default_factory=dict)


class FrameworkCandidate(BaseModel):
    anchors: list[AssessmentAnchor]
    exam_points: list[ExamPoint]
    teaching_topics: list[TeachingTopic]
    conflicts: list[FrameworkConflict]
    final_exam_rules: dict = Field(default_factory=dict)


class FrameworkConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchors: list[AnchorRevision]
    exam_points: list[ExamPoint]
    conflict_resolutions: dict[str, str]
    teacher_exclusions: list[str]
```

验证规则必须保证考点 `code` 唯一、`anchor_key` 属于确认锚点、显式考点权重不超过其父锚点权重、`inherited_group` 不被自动改写为等分权重。`FrameworkConflict.kind` 增加 `teaching_depth_conflict`、`exam_point_weight_conflict` 和 `exam_point_scope_conflict`。

`SyllabusExtractor.extract_teaching()` 和 `extract_assessment()` 增加 keyword-only `call_context: ModelCallContext | None = None`；FrameworkGraph 传入当前 `course_id`、`run_id` 和 `stage`。测试 fake 接受但不使用该上下文。

- [ ] **Step 4: 在 FrameworkGraph 中增加考点对齐节点**

将合并节点输出考点，并在冲突检查中按 `teaching_anchor_keys` 校验教学覆盖：

```python
def align_exam_points_with_teaching(state: FrameworkState):
    teaching_keys = {topic["key"] for topic in state["teaching_topics"]}
    outline = AssessmentOutline.model_validate(state["assessment_outline"])
    conflicts = list(state.get("framework_conflicts", []))
    for point in outline.exam_points:
        if not teaching_keys.intersection(point.teaching_anchor_keys):
            conflicts.append(
                FrameworkConflict(
                    key=f"exam-point-coverage:{point.code}",
                    kind="missing_teaching_coverage",
                    message=f"考试考点“{point.title}”缺少教学大纲覆盖依据",
                ).model_dump(mode="json")
            )
    return {
        "exam_points": [point.model_dump(mode="json") for point in outline.exam_points],
        "framework_conflicts": conflicts,
    }
```

图顺序固定为 `extract_* → merge_assessment_led_framework → align_exam_points_with_teaching → validate_conflicts`。

深度比较使用固定顺序 `remember < understand < apply < analyze < evaluate < create`，将教学大纲的“了解/理解/掌握/应用”映射到该顺序。考点要求高于所有对齐教学主题时产生 `teaching_depth_conflict`，不能自动降低考点认知目标。

- [ ] **Step 5: 持久化候选和已确认考点**

`DatabaseFrameworkRepository.persist_candidate()` 把候选考点写入 `exam_points`；`publish()` 先校验确认的考点均属于当前候选版本，再用教师确认值替换候选行并把 `status` 更新为 `confirmed`。框架 payload 同时保存 `exam_points`，供后续组织运行冻结。

- [ ] **Step 6: 扩展 API 集成测试**

候选响应必须包含 `exam_points`，确认请求必须原样提交教师修订后的考点。数据库断言：

```python
point_rows = session.execute(
    select(exam_points).where(exam_points.c.framework_version_id == confirmed_id)
).mappings().all()
assert len(point_rows) == 1
assert point_rows[0]["status"] == "confirmed"
assert point_rows[0]["operational_detail_policy"] == "supporting_only"
```

- [ ] **Step 7: 运行框架相关测试**

Run: `cd backend; pytest tests/unit/test_framework_rules.py tests/workflow/test_framework_graph.py tests/integration/test_framework_api.py -q`

Expected: PASS.

- [ ] **Step 8: 提交考点框架**

```powershell
git add backend/app/domain/framework/models.py backend/app/workflows/framework_graph.py backend/app/services/framework_service.py backend/tests/unit/test_framework_rules.py backend/tests/workflow/test_framework_graph.py backend/tests/integration/test_framework_api.py
git commit -m "feat: build confirmed exam point inventory"
```

---

### Task 3: 建立暂存证据和混合检索

**Files:**
- Create: `backend/app/domain/knowledge/relevance.py`
- Create: `backend/app/services/staging_retrieval_service.py`
- Create: `backend/app/adapters/model/embedding_gateway.py`
- Modify: `backend/app/config.py`
- Modify: `.env.example`
- Create: `backend/tests/unit/test_staging_retrieval.py`

- [ ] **Step 1: 写混合检索失败测试**

```python
from app.domain.framework.exam_points import ExamPoint
from app.domain.knowledge.relevance import StagingChunk
from app.services.staging_retrieval_service import retrieve_for_exam_point


class FakeEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = {
            "检索链路、召回偏差及诊断依据": [1.0, 0.0],
            "RAG检索结果遗漏关键内容的原因": [0.9, 0.1],
            "安装CUDA并截图提交": [0.0, 1.0],
        }
        return [vectors[text] for text in texts]


def test_hybrid_retrieval_keeps_exam_point_evidence_and_drops_low_relevance():
    point = ExamPoint(
        code="rag-diagnosis",
        anchor_key="rag",
        title="检索效果诊断",
        assessment_requirement="能够诊断召回偏差",
        weight_value=100,
        weight_source="assessment_syllabus",
        weight_group_id="rag",
        retrieval_intent="检索链路、召回偏差及诊断依据",
    )
    chunks = [
        StagingChunk(id="good", material_version_id="m1", content="RAG检索结果遗漏关键内容的原因", locator={"page": 2}),
        StagingChunk(id="noise", material_version_id="m1", content="安装CUDA并截图提交", locator={"page": 9}),
    ]

    result = retrieve_for_exam_point(point, chunks, FakeEmbedder(), top_k=8, minimum_score=0.25)

    assert [item.chunk.id for item in result] == ["good"]
    assert result[0].score > 0.25
```

- [ ] **Step 2: 运行测试并确认检索服务尚不存在**

Run: `cd backend; pytest tests/unit/test_staging_retrieval.py -q`

Expected: FAIL with missing module.

- [ ] **Step 3: 实现可注入的混合检索**

先在 `relevance.py` 定义来源定位明确的暂存块：

```python
class StagingChunk(BaseModel):
    id: str
    material_version_id: str
    content: str = Field(min_length=1)
    locator: dict = Field(default_factory=dict)
```

`staging_retrieval_service.py` 定义 `RankedChunk`、`EmbeddingClient` 协议，以及：

```python
def retrieve_for_exam_point(
    point: ExamPoint,
    chunks: list[StagingChunk],
    embedder: EmbeddingClient,
    *,
    top_k: int,
    minimum_score: float,
) -> list[RankedChunk]:
    query = point.retrieval_intent
    vectors = embedder.embed([query, *[chunk.content for chunk in chunks]])
    query_vector, chunk_vectors = vectors[0], vectors[1:]
    ranked = []
    for chunk, vector in zip(chunks, chunk_vectors, strict=True):
        lexical = lexical_overlap(query, chunk.content)
        semantic = cosine_similarity(query_vector, vector)
        score = 0.35 * lexical + 0.65 * semantic
        if score >= minimum_score:
            ranked.append(RankedChunk(chunk=chunk, score=score))
    return sorted(ranked, key=lambda item: item.score, reverse=True)[:top_k]
```

同文件再实现 `HybridStagingRetriever`，构造函数固定保存 embedder、`top_k` 和 `minimum_score`，对外提供 `retrieve(exam_point, chunks)`；OrganizationGraph 只依赖这个小接口，不直接了解 embedding HTTP 客户端。

中文关键词使用字符 bigram/trigram 与 ASCII token 的并集；空向量或维度不一致时抛出 `RetrievalConfigurationError`，不能退回“把全部资料交给模型”。

- [ ] **Step 4: 实现 embedding 网关和配置**

`embedding_gateway.py` 使用 `/embeddings`，验证返回数量和向量维度：

```python
class OpenAICompatibleEmbeddingGateway:
    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = httpx.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
            timeout=self.timeout,
        )
        response.raise_for_status()
        rows = sorted(response.json()["data"], key=lambda item: item["index"])
        vectors = [list(map(float, row["embedding"])) for row in rows]
        if len(vectors) != len(texts) or not vectors or any(len(row) != len(vectors[0]) for row in vectors):
            raise EmbeddingGatewayError("embedding response shape does not match input")
        return vectors
```

配置新增 `embedding_base_url`、`embedding_api_key`、`embedding_model`、`organization_retrieval_top_k=24`、`organization_retrieval_min_score=0.25`、`organization_max_workers=16`。`.env.example` 只放空密钥占位符。

- [ ] **Step 5: 覆盖故障和低相关场景**

增加测试：embedding 数量错误必须失败；所有候选低于阈值时返回空列表；同主题但语义不支撑考点的块不进入结果；结果不跨课程/文件快照输入。

- [ ] **Step 6: 运行检索测试**

Run: `cd backend; pytest tests/unit/test_staging_retrieval.py -q`

Expected: PASS.

- [ ] **Step 7: 提交混合检索**

```powershell
git add backend/app/domain/knowledge/relevance.py backend/app/services/staging_retrieval_service.py backend/app/adapters/model/embedding_gateway.py backend/app/config.py .env.example backend/tests/unit/test_staging_retrieval.py
git commit -m "feat: add exam point staging retrieval"
```

---

### Task 4: 定义考点证据四级分类和知识准入

**Files:**
- Modify: `backend/app/domain/knowledge/relevance.py`
- Modify: `backend/app/domain/knowledge/models.py`
- Modify: `backend/app/services/knowledge_tree_service.py`
- Create: `backend/tests/unit/test_knowledge_relevance.py`
- Modify: `backend/tests/unit/test_knowledge_tree_rules.py`

- [ ] **Step 1: 写四级分类和操作政策失败测试**

```python
from app.domain.framework.exam_points import ExamPoint
from app.domain.knowledge.relevance import EvidenceDecision, admit_evidence_decision


def _point(policy: str) -> ExamPoint:
    return ExamPoint(
        code="model-loading",
        anchor_key="deployment",
        title="模型加载机制",
        assessment_requirement="能够解释加载组件的职责关系",
        weight_value=100,
        weight_source="assessment_syllabus",
        weight_group_id="deployment",
        operational_detail_policy=policy,
        retrieval_intent="模型加载组件职责和故障诊断",
    )


def test_supporting_only_operation_detail_cannot_create_assessment_unit():
    decision = EvidenceDecision(
        exam_point_code="model-loading",
        evidence_chunk_id="chunk-config",
        relevance_class="direct",
        support_claim="config.json 文件名需要记忆",
        evidence_role="fact_or_constraint",
        content_kind="operational_detail",
        candidate_assessment_unit={"code": "remember-config", "title": "记忆配置文件名"},
        candidate_card_content={"name": "config.json", "assessable_content": ["配置文件名"]},
        confidence=95,
    )

    admitted = admit_evidence_decision(_point("supporting_only"), decision)

    assert admitted.relevance_class == "supporting"
    assert admitted.candidate_assessment_unit is None
    assert admitted.candidate_card_content is None


def test_explicitly_assessable_operation_can_remain_direct():
    decision = EvidenceDecision(
        exam_point_code="model-loading",
        evidence_chunk_id="chunk-failure",
        relevance_class="direct",
        support_claim="加载组件缺失会导致初始化失败",
        evidence_role="answer_or_rubric_basis",
        content_kind="operational_detail",
        candidate_assessment_unit={"code": "diagnose-loading", "title": "诊断模型加载失败"},
        candidate_card_content={"name": "加载组件职责", "assessable_content": ["组件缺失与初始化失败的关系"]},
        confidence=92,
    )

    admitted = admit_evidence_decision(_point("directly_assessable"), decision)

    assert admitted.relevance_class == "direct"
```

- [ ] **Step 2: 运行测试并确认相关性模型尚不存在**

Run: `cd backend; pytest tests/unit/test_knowledge_relevance.py -q`

Expected: FAIL with missing module.

- [ ] **Step 3: 实现结构化分类模型**

`relevance.py` 定义：

```python
class RelevanceClass(StrEnum):
    DIRECT = "direct"
    SUPPORTING = "supporting"
    BACKGROUND = "background"
    OUT_OF_SCOPE = "out_of_scope"


class EvidenceDecision(BaseModel):
    exam_point_code: str
    evidence_chunk_id: str
    relevance_class: RelevanceClass
    support_claim: str
    evidence_role: str | None = None
    content_kind: str
    candidate_assessment_unit: dict | None = None
    candidate_card_content: dict | None = None
    prompt_material: str | None = None
    confidence: int = Field(ge=0, le=100)


class ExamPointFileDecision(BaseModel):
    exam_point_code: str
    material_version_id: str
    decisions: list[EvidenceDecision]


class ExamPointCoverage(BaseModel):
    exam_point_code: str
    direct_count: int
    supporting_count: int
    background_count: int
    out_of_scope_count: int
    status: Literal["sufficient", "insufficient", "conflicting"]
    reasons: list[str] = Field(default_factory=list)


class ExamPointEvidenceClassifier(Protocol):
    def classify(
        self,
        *,
        exam_point: ExamPoint,
        material_version_id: str,
        chunks: list[StagingChunk],
        call_context: ModelCallContext | None = None,
    ) -> ExamPointFileDecision:
        raise NotImplementedError
```

- [ ] **Step 4: 用政策准入替换课程词表过滤**

`admit_evidence_decision()` 只根据 `ExamPoint` 范围、证据字段完整性、`content_kind` 和 `operational_detail_policy` 决定是否可产生考核单元。删除 `_NON_ASSESSABLE_LABELS` 中针对 `.json`、`.safetensors` 等具体名称的正则；保留空字段、缺证据 ID、越界考点和无可评分表现等结构校验。

`background` 与 `out_of_scope` 永不产生考核单元/知识卡；`supporting` 只允许来源无关的 `prompt_material`；`direct` 必须同时包含考核单元和知识卡候选。

- [ ] **Step 5: 让考核单元绑定考试考点**

在 `AssessmentUnitDraft` 增加兼容默认值 `exam_point_code: str = ""`；在 `KnowledgeCardDraft` 增加 `prompt_material: list[str] = Field(default_factory=list)`；在 `KnowledgeTreeCandidate` 增加 `coverage: list[ExamPointCoverage] = Field(default_factory=list)` 和 `evidence_decisions: list[EvidenceDecision] = Field(default_factory=list)`。在 Draft 类型定义之后增加 `ExamPointKnowledgeConsolidator` 协议，签名为 `consolidate(*, exam_point: ExamPoint, admitted_decisions: list[EvidenceDecision], call_context: ModelCallContext | None = None) -> list[AssessmentUnitDraft]`，方法体抛出 `NotImplementedError`。这样 `relevance.py` 不反向导入知识树模型，避免循环依赖。

`validate_publishable_tree()` 增加可选参数 `allowed_exam_point_codes: set[str] | None = None`：Task 4 单元测试传入集合时严格校验，旧调用未传时保持兼容；Task 5 更新所有发布调用后，发布链路始终传入已确认考点集合并保证每个 active 单元至少有一张含直接证据的 active 卡。

- [ ] **Step 6: 运行知识准入测试**

Run: `cd backend; pytest tests/unit/test_knowledge_relevance.py tests/unit/test_knowledge_tree_rules.py -q`

Expected: PASS, including a regression proving `config.json` is rejected because policy is `supporting_only`, not because its text matches a blacklist.

- [ ] **Step 7: 提交证据准入**

```powershell
git add backend/app/domain/knowledge/relevance.py backend/app/domain/knowledge/models.py backend/app/services/knowledge_tree_service.py backend/tests/unit/test_knowledge_relevance.py backend/tests/unit/test_knowledge_tree_rules.py
git commit -m "feat: classify evidence by exam point relevance"
```

---

### Task 5: 重构 OrganizationGraph 为“考点 + 文件”任务

**Files:**
- Modify: `backend/app/workflows/organization_graph.py`
- Modify: `backend/app/workflows/knowledge_catalog_subgraph.py`
- Modify: `backend/app/services/knowledge_publish_service.py`
- Modify: `backend/app/services/material_service.py`
- Modify: `backend/app/api/v1/knowledge.py`
- Modify: `backend/tests/workflow/test_organization_graph.py`
- Modify: `backend/tests/workflow/test_knowledge_catalog_subgraph.py`
- Modify: `backend/tests/integration/test_knowledge_publish_service.py`
- Modify: `backend/tests/integration/test_material_upload.py`

- [ ] **Step 1: 写“考点 + 文件”调用粒度失败测试**

准备两个考点、两个文件，但只让检索器为三个组合返回候选。断言分类器只调用三次，且每次只看到一个考点和一个文件：

```python
class PairRetriever:
    allowed_pairs = {
        ("rag-principle", "material-1"),
        ("rag-principle", "material-2"),
        ("agent-design", "material-2"),
    }

    def retrieve(self, exam_point, chunks):
        return chunks if (exam_point.code, chunks[0].material_version_id) in self.allowed_pairs else []


def exam_point(code: str) -> ExamPoint:
    return ExamPoint(
        code=code,
        anchor_key=code.split("-", 1)[0],
        title=code,
        assessment_requirement=f"能够完成{code}考核任务",
        weight_value=50,
        weight_source="assessment_syllabus",
        weight_group_id=code.split("-", 1)[0],
        retrieval_intent=f"{code}课程事实",
    )


def staging_chunk(material_version_id: str, chunk_id: str) -> StagingChunk:
    return StagingChunk(
        id=chunk_id,
        material_version_id=material_version_id,
        content=f"{material_version_id}的候选事实",
        locator={"page": 1},
    )


class RecordingPairClassifier:
    def __init__(self):
        self.calls = []

    def classify(self, *, exam_point, material_version_id, chunks, call_context=None):
        self.calls.append(SimpleNamespace(exam_point=exam_point, material_version_id=material_version_id, chunks=chunks))
        return ExamPointFileDecision(
            exam_point_code=exam_point.code,
            material_version_id=material_version_id,
            decisions=[
                EvidenceDecision(
                    exam_point_code=exam_point.code,
                    evidence_chunk_id=chunks[0].id,
                    relevance_class="background",
                    support_claim="主题相关但不足以支撑答案",
                    content_kind="background",
                    confidence=70,
                )
            ],
        )


class RecordingConsolidator:
    def consolidate(self, *, exam_point, admitted_decisions, call_context=None):
        return []


def test_organization_classifies_only_retrieved_exam_point_file_pairs():
    classifier = RecordingPairClassifier()
    repository = RecordingKnowledgeRepository()
    graph = build_organization_graph(
        PairRetriever(),
        classifier,
        RecordingConsolidator(),
        repository,
        checkpointer=InMemorySaver(),
    )
    points = [exam_point("rag-principle"), exam_point("agent-design")]
    state = {
        "course_id": "course-1",
        "run_id": "organization-run-1",
        "framework_version_id": "framework-v1",
        "exam_points": [point.model_dump(mode="json") for point in points],
        "files": [
            {"material_version_id": "material-1", "chunks": [staging_chunk("material-1", "e1").model_dump()]},
            {"material_version_id": "material-2", "chunks": [staging_chunk("material-2", "e2").model_dump()]},
        ],
    }

    paused = graph.invoke(state, config={"configurable": {"thread_id": "point-file"}})

    assert {(call.exam_point.code, call.material_version_id) for call in classifier.calls} == {
        ("rag-principle", "material-1"),
        ("rag-principle", "material-2"),
        ("agent-design", "material-2"),
    }
    assert all(len({chunk.material_version_id for chunk in call.chunks}) == 1 for call in classifier.calls)
    assert "__interrupt__" in paused
```

再写：无召回候选的考点状态为 `insufficient`；一个组合失败不会重复处理其他组合；来自两个文件的同义候选只形成一个考核单元/知识卡，并保留两条直接证据链接。

- [ ] **Step 2: 运行工作流测试并确认旧图仍按整文件抽取**

Run: `cd backend; pytest tests/workflow/test_organization_graph.py -q`

Expected: FAIL because old graph sends all framework anchors to each file extraction call.

- [ ] **Step 3: 创建暂存证据快照**

`create_organization_state(session, *, course_id, material_version_ids, embedder)` 从选中文件的 `content_blocks` 建立本次运行的 `evidence_chunks`，保存 `content_block_id`、`locator`、内容哈希和 embedding。状态只保存证据 ID，不保存全文：

```python
files.append(
    {
        "material_version_id": version_id,
        "evidence_chunk_ids": [chunk["id"] for chunk in staged_chunks],
    }
)
```

运行快照继续冻结 `framework_version_id`、`exam_point_ids` 和 `material_version_ids`。

- [ ] **Step 4: 重写 OrganizationGraph 节点**

`build_organization_graph` 的签名改为 `(retriever: HybridStagingRetriever, classifier: ExamPointEvidenceClassifier, consolidator: ExamPointKnowledgeConsolidator, repository: KnowledgeRepository, *, checkpointer=None)`。内部节点固定为：

```python
graph.add_node("validate_inputs", validate_inputs)
graph.add_node("freeze_selected_materials", freeze_selected_materials)
graph.add_node("retrieve_per_exam_point", retrieve_per_exam_point)
graph.add_node("classify_exam_point_file_pairs", classify_exam_point_file_pairs)
graph.add_node("consolidate_per_exam_point", consolidate_per_exam_point)
graph.add_node("build_catalog_candidate", build_catalog_candidate)
graph.add_node("audit_exam_point_coverage", audit_exam_point_coverage)
graph.add_node("persist_candidate", persist_candidate)
graph.add_node("interrupt_teacher_review", interrupt_teacher_review)
graph.add_node("publish_catalog_and_index", publish_catalog_and_index)
```

`classify_exam_point_file_pairs` 使用 `ThreadPoolExecutor(max_workers=settings.organization_max_workers)`，任务输入是一个 `ExamPoint` 和同一文件中最多 `top_k` 个召回块。`consolidate_per_exam_point` 随后只把同一个考点 admitted 的 `direct` 与 `supporting` 结构化决策交给一次归并调用：direct 用于答案事实，supporting 只能进入 `prompt_material`。它输出该考点的最小充分 `AssessmentUnitDraft[]`；不同考点可并发，任何调用都看不到其他考点或资料全文。

每个 future 单独捕获模型/格式错误并写入 `failed_pairs`，其他组合继续完成；失败组合对应考点进入 `insufficient` 或 `needs_teacher_review`。归并器失败也只影响当前考点，禁止回退为按标题生成卡。

- [ ] **Step 5: 修改候选归并和覆盖审计**

`build_knowledge_catalog_candidate()` 接受 `exam_points`、`ExamPointFileDecision[]` 和逐考点归并结果，先校验每张卡引用的证据确实来自该考点的 admitted direct 决策，再构建树。归并器必须合并同义、近义和上下位重复，不得按文件创建卡；它可以把重复事实关联到同一卡，但不能删除支撑不同答案边界的独立事实。覆盖状态由计数和证据角色决定：缺少 `answer_or_rubric_basis` 时为 `insufficient`，互相矛盾的 direct 主张为 `conflicting`。

- [ ] **Step 6: 持久化分类、考点关联和教师确认**

`DatabaseKnowledgeRepository.persist_candidate()` 写入 `exam_point_evidence_links` 候选行；`publish()` 只把 `direct` 卡加入 `index_memberships`，将 `assessment_units.exam_point_id` 指向同一 framework version 的已确认考点。`KnowledgeTreeConfirmation` 增加 `reviewed_exam_point_codes`，所有非 excluded 考点必须被教师确认或明确标为证据不足。

- [ ] **Step 7: API 返回覆盖统计**

`GET /organization-runs/{run_id}/candidate` 的 payload 包含 `coverage` 和四类数量。创建运行时缺少 embedding 或 semantic classifier 配置返回 503；模型 JSON 失败保留具体 `error_code` 和截断后的 `error_message`，不生成结构标题回退卡。

- [ ] **Step 8: 让资料删除使相关证据失效**

`material_service.delete_material()` 在同一事务中把对应 `exam_point_evidence_links.status` 更新为 `source_deleted`、把 `knowledge_evidence_links.lifecycle_status` 更新为 `source_deleted`。若某张卡已无 active direct 证据，则把卡状态更新为 `affected_by_source_deletion` 并移除其当前 published index membership；历史试卷 payload 和旧目录版本不删除。集成测试断言该卡不能再被新蓝图读取。

- [ ] **Step 9: 运行工作流与持久化测试**

Run: `cd backend; pytest tests/workflow/test_organization_graph.py tests/workflow/test_knowledge_catalog_subgraph.py tests/integration/test_knowledge_publish_service.py tests/integration/test_material_upload.py -q`

Expected: PASS.

- [ ] **Step 10: 提交组织工作流**

```powershell
git add backend/app/workflows/organization_graph.py backend/app/workflows/knowledge_catalog_subgraph.py backend/app/services/knowledge_publish_service.py backend/app/services/material_service.py backend/app/api/v1/knowledge.py backend/tests/workflow/test_organization_graph.py backend/tests/workflow/test_knowledge_catalog_subgraph.py backend/tests/integration/test_knowledge_publish_service.py backend/tests/integration/test_material_upload.py
git commit -m "feat: organize materials per exam point"
```

---

### Task 6: 接入 DeepSeek 大纲和资料语义适配器

**Files:**
- Create: `backend/app/adapters/model/deepseek_semantic_extractors.py`
- Create: `backend/app/services/model_call_service.py`
- Modify: `backend/app/adapters/model/deepseek_gateway.py`
- Modify: `backend/app/api/v1/framework.py`
- Modify: `backend/app/api/v1/knowledge.py`
- Create: `backend/tests/unit/test_deepseek_semantic_extractors.py`
- Modify: `backend/tests/integration/test_framework_api.py`

- [ ] **Step 1: 写严格 JSON 适配器失败测试**

使用 `httpx.MockTransport` 返回固定 JSON，断言：考核大纲响应包含 `exam_points`；资料分类请求只包含一个考点和一个文件；归并请求只包含一个考点 admitted 的 direct/supporting 决策；来源定位存在于分类/归并响应，但不会进入知识卡正文。

```python
class RecordingJsonClient:
    def __init__(self):
        self.recorded_payloads = []

    def request_json(self, *, system_prompt, payload, temperature):
        self.recorded_payloads.append({"system": system_prompt, "user": payload, "temperature": temperature})
        return {
            "exam_point_code": "rag-diagnosis",
            "material_version_id": "material-v1",
            "decisions": [{
                "exam_point_code": "rag-diagnosis",
                "evidence_chunk_id": "e1",
                "relevance_class": "direct",
                "support_claim": "切分粒度会影响关键内容召回",
                "evidence_role": "answer_or_rubric_basis",
                "content_kind": "principle",
                "candidate_assessment_unit": {"code": "diagnose-retrieval", "title": "诊断检索偏差"},
                "candidate_card_content": {"name": "切分粒度影响", "assessable_content": ["切分粒度会影响召回"]},
                "confidence": 95,
            }],
        }


def test_material_classifier_sends_one_exam_point_and_one_file():
    client = RecordingJsonClient()
    classifier = DeepSeekExamPointEvidenceClassifier(client)
    point = ExamPoint(
        code="rag-diagnosis",
        anchor_key="rag",
        title="检索效果诊断",
        assessment_requirement="能够诊断召回偏差",
        weight_value=100,
        weight_source="assessment_syllabus",
        weight_group_id="rag",
        retrieval_intent="检索偏差及诊断依据",
    )
    chunk = StagingChunk(id="e1", material_version_id="material-v1", content="检索失败可能由切分粒度不当造成", locator={"page": 1})
    classifier.classify(
        exam_point=point,
        material_version_id="material-v1",
        chunks=[chunk],
    )

    request = client.recorded_payloads[-1]["user"]
    assert request["exam_point"]["code"] == "rag-diagnosis"
    assert request["material_version_id"] == "material-v1"
    assert {item["material_version_id"] for item in request["chunks"]} == {"material-v1"}
    assert "all_exam_points" not in request
```

- [ ] **Step 2: 运行适配器测试并确认类尚不存在**

Run: `cd backend; pytest tests/unit/test_deepseek_semantic_extractors.py -q`

Expected: FAIL with missing module.

- [ ] **Step 3: 提取可复用的 DeepSeek JSON 客户端**

将 `_request_json` 的 HTTP 与重试逻辑抽为 `DeepSeekJsonClient.request_json(system_prompt, payload, temperature, call_context)`，保留现有生成网关行为。客户端接受可注入的 `httpx.Client`，错误中记录 HTTP 状态、request id、空内容、JSON 解析和 Pydantic 校验原因，但不记录完整资料正文。

`model_call_service.py` 定义 `ModelCallContext(course_id, framework_build_run_id, organization_run_id, stage)` 和 `DatabaseModelCallRecorder.record(context, provider, model, status, prompt_hash, input_tokens, output_tokens, duration_ms, error_code, error_message)`。每次最终成功或最终失败写一行 `model_calls`；重试次数、状态码和最后错误放入脱敏 details。FrameworkGraph、OrganizationGraph 调用适配器时传入当前 run context。

- [ ] **Step 4: 实现大纲抽取器**

`DeepSeekSyllabusExtractor.extract_assessment()` 的 schema 必须要求：

```python
{
    "anchors": [{"key": "string", "title": "string", "exam_weight": 0}],
    "exam_points": [{
        "code": "string",
        "anchor_key": "string",
        "title": "string",
        "assessment_requirement": "string",
        "weight_value": 0,
        "weight_source": "assessment_syllabus|inherited_group",
        "weight_group_id": "string",
        "cognitive_targets": ["understand"],
        "assessment_orientations": ["conceptual"],
        "operational_detail_policy": "supporting_only",
        "retrieval_intent": "string",
        "teaching_anchor_keys": ["string"],
    }],
    "final_exam_rules": {},
}
```

提示明确只读取期末考试栏目，不把平时、实验过程或课程封面转成期末考点。操作细节默认 `supporting_only`，只有考纲明确声明实践配置/操作考核才输出 `directly_assessable`。

- [ ] **Step 5: 实现考点证据分类器**

分类器提示只定义 `direct/supporting/background/out_of_scope` 判据和操作政策，不包含课程专属过滤词。响应经 `ExamPointFileDecision.model_validate()`；返回未知证据 ID、其他考点 code 或其他文件 ID 时整个子任务失败并可局部重试。

- [ ] **Step 6: 实现逐考点最小充分归并器**

`DeepSeekExamPointKnowledgeConsolidator` 每次接收一个 `ExamPoint` 及其 admitted direct/supporting 决策，输出 `AssessmentUnitDraft[]`。提示要求按可评分表现归并同义事实、保留不同答案边界、聚合 direct 的 `evidence_chunk_ids`，并只把 supporting 的来源无关内容放入 `prompt_material`；禁止按文件名、章节或来源数量拆卡。后端校验知识事实引用的每个证据 ID 均属于输入 direct 决策，低证据或越界输出使该考点进入 `needs_teacher_review`。

- [ ] **Step 7: 在 API 依赖中惰性创建真实适配器**

`get_syllabus_extractor()` 和 `get_knowledge_extractor()` 与 `get_gateway()` 一样从 `settings.deepseek_*` 创建单例；组织 API 同时创建 embedding gateway。缺少任一必需配置时返回明确的 503，不运行伪候选。

- [ ] **Step 8: 运行适配器和 API 测试**

Run: `cd backend; pytest tests/unit/test_deepseek_semantic_extractors.py tests/integration/test_framework_api.py -q`

Expected: PASS, and all existing generation gateway tests remain green.

- [ ] **Step 9: 提交模型接入**

```powershell
git add backend/app/adapters/model/deepseek_semantic_extractors.py backend/app/adapters/model/deepseek_gateway.py backend/app/services/model_call_service.py backend/app/api/v1/framework.py backend/app/api/v1/knowledge.py backend/tests/unit/test_deepseek_semantic_extractors.py backend/tests/integration/test_framework_api.py
git commit -m "feat: connect DeepSeek semantic curation"
```

---

### Task 7: 在蓝图中分配题位考查方式

**Files:**
- Modify: `backend/app/domain/blueprint/models.py`
- Modify: `backend/app/services/blueprint_service.py`
- Modify: `backend/tests/unit/test_blueprint_allocation.py`

- [ ] **Step 1: 写考查方式失败测试**

```python
def test_blueprint_mode_distribution_is_not_driven_by_material_card_count():
    request = BlueprintRequest(
        total_score=20,
        type_rules={
            "single_choice": {
                "count": 10,
                "score": 2,
                "assessment_mode_distribution": {
                    "theory_recall": 40,
                    "conceptual": 50,
                    "application": 10,
                    "problem_solving": 0,
                    "practical_operation": 0,
                },
            }
        },
        chapter_weights={"chapter": 100},
        units=[
            UnitCoverage(
                unit_id="theory",
                exam_point_id="ep-theory",
                anchor_key="chapter",
                card_ids=["theory-card"],
                allowed_assessment_modes=["theory_recall", "conceptual", "application"],
                operational_detail_policy="supporting_only",
            ),
            UnitCoverage(
                unit_id="practical",
                exam_point_id="ep-practical",
                anchor_key="chapter",
                card_ids=[f"operation-{index}" for index in range(20)],
                allowed_assessment_modes=["practical_operation"],
                operational_detail_policy="directly_assessable",
            ),
        ],
    )

    plan = allocate_plan_items(request)

    assert plan.assessment_mode_counts["single_choice"] == {
        "theory_recall": 4,
        "conceptual": 5,
        "application": 1,
        "problem_solving": 0,
        "practical_operation": 0,
    }
    assert all(item.assessment_mode != "practical_operation" for item in plan.items)
```

同时测试：比例不合计 100 阻断；`practical_operation` 不能分给 `supporting_only`；题型内部仍按低、中、高排序。

- [ ] **Step 2: 运行蓝图测试并确认模型缺少 assessment_mode**

Run: `cd backend; pytest tests/unit/test_blueprint_allocation.py -q`

Expected: FAIL.

- [ ] **Step 3: 扩展蓝图模型**

```python
ASSESSMENT_MODES = (
    "theory_recall",
    "conceptual",
    "application",
    "problem_solving",
    "practical_operation",
)


class UnitCoverage(BaseModel):
    unit_id: str
    exam_point_id: str = ""
    anchor_key: str
    card_ids: list[str] = Field(min_length=1)
    allowed_assessment_modes: list[str] = Field(
        default_factory=lambda: ["theory_recall", "conceptual", "application", "problem_solving"]
    )
    operational_detail_policy: str = "supporting_only"


class PlanItem(BaseModel):
    item_index: int
    question_type: str
    score: float
    anchor_key: str
    exam_point_id: str = ""
    unit_id: str
    card_id: str
    difficulty: str = "medium"
    cognitive_level: str = "understand"
    assessment_mode: str = "conceptual"
```

`BlueprintPlan` 增加 `assessment_mode_counts`。

旧请求未提供 `exam_point_id` 时仅为现有测试和未发布草稿兼容，分配器内部使用 `unit_id` 作为临时值；正式知识目录创建蓝图时必须传入真实 `exam_point_id`。这样 Task 7 提交后现有 GenerationGraph 测试仍可运行。

- [ ] **Step 4: 实现题型内模式配额**

复用 `_largest_remainder()`。未提供模式比例的旧题型规则使用确定性默认值：选择/判断以理论和概念为主，填空为 80% 理论识记 + 20% 概念理解，简答为 40% 概念 + 30% 应用 + 30% 问题解决，综合题为 30% 应用 + 70% 问题解决；默认实践操作比例为 0。

先创建题型的难度序列和模式序列，再按题位 zip。选卡时同时满足题型和模式；`practical_operation` 额外要求 `directly_assessable`。找不到合格单元时抛出包含题型、模式和章节的 `BlueprintValidationError`。

- [ ] **Step 5: 运行蓝图回归**

Run: `cd backend; pytest tests/unit/test_blueprint_allocation.py -q`

Expected: PASS, including existing chapter weight and per-type difficulty tests.

- [ ] **Step 6: 提交考查方式分配**

```powershell
git add backend/app/domain/blueprint/models.py backend/app/services/blueprint_service.py backend/tests/unit/test_blueprint_allocation.py
git commit -m "feat: allocate assessment modes in blueprints"
```

---

### Task 8: 增加综合题原型规划和专用生成合同

**Files:**
- Create: `backend/app/domain/generation/archetypes.py`
- Modify: `backend/app/domain/generation/coverage.py`
- Modify: `backend/app/schemas/generation.py`
- Modify: `backend/app/adapters/model/deepseek_gateway.py`
- Create: `backend/tests/unit/test_comprehensive_archetypes.py`
- Modify: `backend/tests/unit/test_generation_payload.py`
- Modify: `backend/tests/workflow/test_generation_graph.py`

- [ ] **Step 1: 写多原型失败测试**

```python
def test_three_comprehensive_slots_require_distinct_structure_contracts():
    items = [
        PlanItem(
            item_index=index,
            question_type="comprehensive",
            score=10,
            anchor_key="rag",
            exam_point_id=f"ep-{index}",
            unit_id=f"unit-{index}",
            card_id=f"card-{index}",
            difficulty="high",
            cognitive_level="analyze",
            assessment_mode="problem_solving",
        )
        for index in (1, 2, 3)
    ]
    cards = {
        f"card-{index}": {
            "performance_statement": f"能够完成综合任务{index}",
            "assessable_content": [f"综合任务{index}的课程事实"],
            "scope_boundary": {},
        }
        for index in (1, 2, 3)
    }
    raw = {
        "directives": [
            {
                "item_index": 1,
                "coverage_atom": "案例中的因果关系",
                "answer_boundary": "依据事实解释现象",
                "cognitive_level": "analyze",
                "novelty_contract": "只分析给定案例",
                "comprehensive_archetype": "case_analysis",
                "material_form": "case_text",
                "cognitive_sequence": ["understand", "analyze"],
                "subquestion_count_range": [2, 3],
            },
            {
                "item_index": 2,
                "coverage_atom": "故障原因与修正",
                "answer_boundary": "定位原因并给出修正",
                "cognitive_level": "analyze",
                "novelty_contract": "只诊断指定故障",
                "comprehensive_archetype": "fault_diagnosis",
                "material_form": "symptom_list",
                "cognitive_sequence": ["analyze", "apply"],
                "subquestion_count_range": [2, 4],
            },
            {
                "item_index": 3,
                "coverage_atom": "约束下的方案选择",
                "answer_boundary": "比较方案并说明选择依据",
                "cognitive_level": "evaluate",
                "novelty_contract": "只评价给定方案",
                "comprehensive_archetype": "comparative_decision",
                "material_form": "constraint_table",
                "cognitive_sequence": ["analyze", "evaluate"],
                "subquestion_count_range": [2, 3],
            },
        ]
    }

    directives = build_coverage_directives(items, cards, raw)

    assert [item.comprehensive_archetype for item in directives] == [
        "case_analysis",
        "fault_diagnosis",
        "comparative_decision",
    ]
    assert len({(item.comprehensive_archetype, item.material_form) for item in directives}) == 3
```

再写重复“原型 + 材料形式 + 认知序列”被拒绝、非综合题不能携带综合题原型、分问范围必须为 2 至 4。

- [ ] **Step 2: 运行测试并确认原型模型尚不存在**

Run: `cd backend; pytest tests/unit/test_comprehensive_archetypes.py -q`

Expected: FAIL.

- [ ] **Step 3: 实现七类原型和适配合同**

`archetypes.py` 定义 `ComprehensiveArchetype`、`MaterialForm`、`ArchetypeContract`，并提供完整映射：

```python
ARCHETYPE_CONTRACTS = {
    "case_analysis": ArchetypeContract(allowed_modes={"application", "problem_solving"}, material_forms={"case_text", "data_summary"}),
    "fault_diagnosis": ArchetypeContract(allowed_modes={"problem_solving", "practical_operation"}, material_forms={"symptom_list", "error_process"}),
    "comparative_decision": ArchetypeContract(allowed_modes={"application", "problem_solving"}, material_forms={"constraint_table", "option_matrix"}),
    "solution_design": ArchetypeContract(allowed_modes={"problem_solving"}, material_forms={"requirements", "resource_constraints"}),
    "process_optimization": ArchetypeContract(allowed_modes={"problem_solving"}, material_forms={"process_description", "metric_summary"}),
    "critique_correction": ArchetypeContract(allowed_modes={"conceptual", "problem_solving"}, material_forms={"incorrect_answer", "flawed_proposal"}),
    "integrated_explanation": ArchetypeContract(allowed_modes={"conceptual", "application"}, material_forms={"compound_phenomenon", "causal_chain"}),
}
```

- [ ] **Step 4: 扩展覆盖规划载荷和指令**

`CoveragePlanningSlot` 增加 `assessment_mode` 和 `prompt_material`；`CoverageDirective` 对综合题增加 `comprehensive_archetype`、`material_form`、`cognitive_sequence`、`subquestion_count_range`。`QuestionGenerationPayload` 增加来源无关的 `assessment_mode: str`、`prompt_material: list[str]` 和四个原型合同字段。`global_policy` 要求同卷综合题结构键唯一，并携带最近结构签名。

`build_coverage_directives()` 确定性校验原型与题位模式适配、认知序列合法、分问 2 至 4；无效规划进入现有全卷规划重试，不直接交给生成节点。

- [ ] **Step 5: 用原型专用模板替换单一综合题模板**

`compile_question_generation_payload()` 从 `ARCHETYPE_CONTRACTS` 生成 `question_template`，例如 `fault_diagnosis` 明确要求给出异常表现、定位依据、原因和修正；`comparative_decision` 明确要求给出约束、候选方案和决策理由。载荷只包含纯净内容和 `prompt_material`，不包含证据/文件字段。

- [ ] **Step 6: 更新 DeepSeek 主脑提示**

`plan_coverage()` 提示主脑先选择兼容原型，禁止多道综合题重复同一结构键，禁止固定“某公司/某团队 + 三问”。`generate()` 提示必须执行已分配原型和 2 至 4 个分问，不能自行改回通用模板。

- [ ] **Step 7: 运行原型、载荷和生成图测试**

Run: `cd backend; pytest tests/unit/test_comprehensive_archetypes.py tests/unit/test_generation_payload.py tests/workflow/test_generation_graph.py -q`

Expected: PASS.

- [ ] **Step 8: 提交综合题原型系统**

```powershell
git add backend/app/domain/generation/archetypes.py backend/app/domain/generation/coverage.py backend/app/schemas/generation.py backend/app/adapters/model/deepseek_gateway.py backend/tests/unit/test_comprehensive_archetypes.py backend/tests/unit/test_generation_payload.py backend/tests/workflow/test_generation_graph.py
git commit -m "feat: plan diverse comprehensive archetypes"
```

---

### Task 9: 生成结构签名并执行同卷、跨卷去重

**Files:**
- Create: `backend/app/domain/generation/structure_signature.py`
- Modify: `backend/app/workflows/generation_graph.py`
- Modify: `backend/app/services/generation_service.py`
- Modify: `backend/app/api/v1/generation.py`
- Modify: `backend/tests/unit/test_comprehensive_archetypes.py`
- Modify: `backend/tests/unit/test_question_quality_rules.py`
- Modify: `backend/tests/workflow/test_generation_graph.py`

- [ ] **Step 1: 写结构签名失败测试**

```python
def test_structure_signature_ignores_wording_but_keeps_structure():
    first = build_structure_signature(
        archetype="fault_diagnosis",
        material_form="symptom_list",
        cognitive_sequence=["analyze", "apply"],
        subquestion_actions=["定位原因", "提出修正"],
        answer_boundaries=["检索粒度", "调整切块"],
    )
    second = build_structure_signature(
        archetype="fault_diagnosis",
        material_form="symptom_list",
        cognitive_sequence=["analyze", "apply"],
        subquestion_actions=["分析成因", "给出改进"],
        answer_boundaries=["检索粒度", "调整切块"],
    )

    assert first.signature_hash == second.signature_hash
```

再写：同卷重复签名只修复后一题；近期签名进入主脑规划但旧题全文、旧答案和来源不进入；非综合题不生成结构签名。

- [ ] **Step 2: 实现来源无关结构签名**

`structure_signature.py` 使用受控动作词归一化和 SHA-256：

```python
ACTION_ALIASES = {
    "定位原因": "diagnose",
    "分析成因": "diagnose",
    "提出修正": "repair",
    "给出改进": "repair",
    "比较方案": "compare",
    "作出选择": "decide",
    "解释现象": "explain",
    "设计方案": "design",
}


def normalize_action(value: str) -> str:
    compact = re.sub(r"\s+", "", value)
    return ACTION_ALIASES.get(compact, compact.casefold())


def build_structure_signature(
    *,
    archetype: str,
    material_form: str,
    cognitive_sequence: list[str],
    subquestion_actions: list[str],
    answer_boundaries: list[str],
) -> QuestionStructureSignature:
    canonical = {
        "archetype": archetype,
        "material_form": material_form,
        "cognitive_sequence": cognitive_sequence,
        "subquestion_actions": [normalize_action(item) for item in subquestion_actions],
        "answer_boundaries": [normalize_answer_boundary(item) for item in answer_boundaries],
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode()
    structure_key = "|".join([
        archetype,
        material_form,
        ",".join(cognitive_sequence),
        ",".join(canonical["subquestion_actions"]),
    ])
    return QuestionStructureSignature(signature_hash=sha256(encoded).hexdigest(), structure_key=structure_key, **canonical)
```

- [ ] **Step 3: 在生成图输出签名并参与审查**

综合题生成成功后由后端根据指令和结构化分问动作生成签名，写入题目 payload。`audit_question_set()` 发现同卷签名相同时返回 `duplicate_comprehensive_structure`，只修复后出现题位。语义审查 payload 增加原型/材料形式，不增加来源。

- [ ] **Step 4: 加载同课程最近五份已确认试卷签名**

在 `structure_signature.py` 增加 `load_recent_structure_signatures(session, course_id, paper_limit=5)`：先查询最近五个状态为 `confirmed` 或 `exported` 的 `paper_versions.id`，再通过 `paper_items → generated_questions.payload` 读取综合题签名。只返回签名对象，不返回题干、答案或来源。

`generation.py` 注入数据库 session，把签名列表写入图初始状态。没有历史卷时传空列表。

- [ ] **Step 5: 让近期重复触发规划重试**

`build_coverage_directives()` 若新综合题 `structure_key` 命中近期列表，抛出 `CoveragePlanError("综合题结构与近期试卷重复")`。重试提示只携带冲突的 `structure_key`，不携带旧题内容。

- [ ] **Step 6: 运行结构审查测试**

Run: `cd backend; pytest tests/unit/test_comprehensive_archetypes.py tests/unit/test_question_quality_rules.py tests/workflow/test_generation_graph.py -q`

Expected: PASS.

- [ ] **Step 7: 提交结构去重**

```powershell
git add backend/app/domain/generation/structure_signature.py backend/app/workflows/generation_graph.py backend/app/services/generation_service.py backend/app/api/v1/generation.py backend/tests/unit/test_comprehensive_archetypes.py backend/tests/unit/test_question_quality_rules.py backend/tests/workflow/test_generation_graph.py
git commit -m "feat: prevent repeated comprehensive structures"
```

---

### Task 10: 更新真实资料演示和教师预览

**Files:**
- Modify: `backend/scripts/build_real_material_demo.py`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: 让真实演示复用正式考点与相关性契约**

删除脚本中 `extract_material_candidate()` 的“整文件 + 全部 anchors”调用。改为：

```python
def build_demo_staging_chunks(documents: list[dict[str, Any]]) -> list[StagingChunk]:
    chunks = []
    for document in documents:
        for block in compact_blocks(document, 1_000_000):
            chunks.append(
                StagingChunk(
                    id=f"{document['sha256'][:12]}:{block['block_id']}",
                    material_version_id=document["sha256"],
                    content=block["text"],
                    locator={"filename": document["filename"], "page": block.get("page")},
                )
            )
    return chunks


def retrieve_demo_pairs(exam_points, chunks, embedding_gateway):
    pairs = []
    material_ids = sorted({chunk.material_version_id for chunk in chunks})
    for point in exam_points:
        for material_id in material_ids:
            file_chunks = [chunk for chunk in chunks if chunk.material_version_id == material_id]
            ranked = retrieve_for_exam_point(point, file_chunks, embedding_gateway, top_k=24, minimum_score=0.25)
            if ranked:
                pairs.append((point, material_id, [item.chunk for item in ranked]))
    return pairs


async def classify_demo_pairs(pairs, semantic_classifier, max_workers: int):
    semaphore = asyncio.Semaphore(max_workers)

    async def classify_one(point, material_id, chunks):
        async with semaphore:
            return await asyncio.to_thread(
                semantic_classifier.classify,
                exam_point=point,
                material_version_id=material_id,
                chunks=chunks,
            )

    return await asyncio.gather(*(classify_one(*pair) for pair in pairs))


async def consolidate_demo_points(exam_points, file_decisions, semantic_consolidator, max_workers: int):
    semaphore = asyncio.Semaphore(max_workers)

    async def consolidate_one(point):
        admitted = [
            decision
            for file_result in file_decisions
            if file_result.exam_point_code == point.code
            for decision in file_result.decisions
            if decision.relevance_class in {"direct", "supporting"}
        ]
        async with semaphore:
            units = await asyncio.to_thread(
                semantic_consolidator.consolidate,
                exam_point=point,
                admitted_decisions=admitted,
            )
        return point.code, units

    rows = await asyncio.gather(*(consolidate_one(point) for point in exam_points))
    return dict(rows)


framework = await build_framework(model, documents)
exam_points = [ExamPoint.model_validate(item) for item in framework["exam_points"]]
staged_chunks = build_demo_staging_chunks(material_documents)
ranked_pairs = retrieve_demo_pairs(exam_points, staged_chunks, embedding_gateway)
file_decisions = await classify_demo_pairs(ranked_pairs, semantic_classifier, max_workers=16)
consolidated_units = await consolidate_demo_points(exam_points, file_decisions, semantic_consolidator, max_workers=8)
tree = build_knowledge_catalog_candidate(
    framework_version_id=framework["source_versions"]["assessment"][:32],
    exam_points=exam_points,
    file_decisions=file_decisions,
    consolidated_units_by_exam_point=consolidated_units,
)
```

模型缓存 key 必须包含考点、文件哈希、候选块哈希、模型版本和 schema 版本；避免复用旧的宽泛知识提取缓存。

- [ ] **Step 2: 在演示快照中保存可审查统计**

`pipeline.json` 增加：`framework.exam_points`、`knowledge_organization.relevance_counts`、`exam_point_coverage`、题位 `assessment_mode`、综合题 `comprehensive_archetype`、`material_form` 和 `structure_signature`。来源定位只在教师查看区域保存，不复制到生成载荷快照。

- [ ] **Step 3: 跨演示运行读取最近签名**

覆盖 `pipeline.json` 前读取其中已完成试卷的综合题签名，最多保留最近五次运行的签名列表并传给生成图。缓存或文件不存在时使用空列表。

- [ ] **Step 4: 更新前端类型和预览**

`App.tsx` 的 `PlanItem` 增加 `assessment_mode`，考点区域显示权重来源、操作政策、直接/辅助/背景/排除数量与证据状态；蓝图表增加“考查方式”；综合题标题旁显示中文原型名称。不要显示模型内部 ID、证据 ID 或结构哈希正文。

- [ ] **Step 5: 构建前端**

Run: `cd frontend; npm run build`

Expected: Vite production build succeeds with no TypeScript errors.

- [ ] **Step 6: 在已配置环境运行真实链路**

Required env: `MINERU_API_TOKEN`, `DEEPSEEK_API_KEY`, `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`.

Run: `cd backend; python scripts/build_real_material_demo.py`

Expected: `frontend/public/demo/pipeline.json` reaches `status: complete`; every published unit has `exam_point_code`; low-related chunks are absent from knowledge cards; comprehensive questions contain at least two distinct archetypes; generation payload snapshots contain no source fields.

- [ ] **Step 7: 提交演示与预览**

只提交脚本、两个已跟踪前端源文件和经人工确认需要保留的演示 JSON，不提交 `.runtime/`、`frontend/dist/` 或 `frontend/node_modules/`。

```powershell
git add backend/scripts/build_real_material_demo.py frontend/src/App.tsx frontend/src/styles.css
git commit -m "feat: demonstrate exam-point-led generation"
```

---

### Task 11: 增加端到端回归并执行完整验证

**Files:**
- Create: `backend/tests/integration/test_exam_point_pipeline.py`
- Modify: `backend/tests/unit/test_generation_payload.py`
- Modify: `backend/tests/unit/test_question_quality_rules.py`
- Modify: `docs/superpowers/specs/2026-08-14-core-exam-system-development-design.md`

- [ ] **Step 1: 写端到端假模型回归**

测试使用新建 SQLite 库和确定性 fake adapters，覆盖：

```python
def test_exam_point_led_pipeline_blocks_material_volume_bias_and_source_leakage(tmp_path):
    point = ExamPoint(
        code="theory",
        anchor_key="chapter",
        title="核心原理",
        assessment_requirement="能够解释核心原理并作出判断",
        weight_value=100,
        weight_source="assessment_syllabus",
        weight_group_id="chapter",
        assessment_orientations=["theory_recall", "conceptual"],
        operational_detail_policy="supporting_only",
        retrieval_intent="核心原理、关系和判断依据",
    )
    direct = ExamPointFileDecision(
        exam_point_code="theory",
        material_version_id="theory-material",
        decisions=[EvidenceDecision(
            exam_point_code="theory",
            evidence_chunk_id="theory-evidence",
            relevance_class="direct",
            support_claim="该段直接说明核心原理和因果关系",
            evidence_role="answer_or_rubric_basis",
            content_kind="principle",
            candidate_assessment_unit={
                "code": "explain-principle",
                "title": "解释核心原理",
                "performance_statement": "能够解释核心原理",
            },
            candidate_card_content={
                "name": "核心原理及因果关系",
                "performance_statement": "能够说明核心原理",
                "assessable_content": ["核心原理决定给定现象的结果"],
                "allowed_question_types": ["single_choice"],
            },
            confidence=95,
        )],
    )
    operation_files = [
        ExamPointFileDecision(
            exam_point_code="theory",
            material_version_id=f"operation-{index}",
            decisions=[EvidenceDecision(
                exam_point_code="theory",
                evidence_chunk_id=f"operation-evidence-{index}",
                relevance_class="supporting",
                support_claim="操作过程只能作为场景条件",
                content_kind="operational_detail",
                prompt_material="给定一个操作异常现象",
                confidence=80,
            )],
        )
        for index in range(20)
    ]
    consolidated_unit = AssessmentUnitDraft(
        code="explain-principle",
        title="解释核心原理",
        performance_statement="能够解释核心原理",
        exam_point_code="theory",
        cards=[KnowledgeCardDraft(
            name="核心原理及因果关系",
            performance_statement="能够说明核心原理",
            assessable_content=["核心原理决定给定现象的结果"],
            allowed_question_types=["single_choice"],
            evidence_chunk_ids=["theory-evidence"],
        )],
    )
    tree = build_knowledge_catalog_candidate(
        framework_version_id="framework-v1",
        exam_points=[point],
        file_decisions=[direct, *operation_files],
        consolidated_units_by_exam_point={"theory": [consolidated_unit]},
    )
    unit = tree.topics[0].units[0]
    card = unit.cards[0]
    request = BlueprintRequest(
        total_score=20,
        type_rules={
            "single_choice": {
                "count": 10,
                "score": 2,
                "assessment_mode_distribution": {
                    "theory_recall": 40,
                    "conceptual": 60,
                    "application": 0,
                    "problem_solving": 0,
                    "practical_operation": 0,
                },
            }
        },
        chapter_weights={"chapter": 100},
        units=[UnitCoverage(
            unit_id=unit.code,
            exam_point_id="theory",
            anchor_key="chapter",
            card_ids=["card-theory"],
            allowed_assessment_modes=["theory_recall", "conceptual"],
            operational_detail_policy="supporting_only",
        )],
    )
    plan = allocate_plan_items(request)
    payload = compile_question_generation_payload(
        plan.items[0],
        {
            "performance_statement": card.performance_statement,
            "assessable_content": card.assessable_content,
            "scope_boundary": card.scope_boundary,
            "preferred_terms": [],
        },
    )
    payload_text = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False).lower()

    assert point.weight_source == "assessment_syllabus"
    assert unit.exam_point_code == "theory"
    assert tree.coverage[0].status == "sufficient"
    assert plan.assessment_mode_counts["single_choice"]["practical_operation"] == 0
    assert all(field not in payload_text for field in ("filename", "page_index", "evidence", "material_version_id", "exam_point_id"))
```

同一文件加入同主题低相关片段、操作细节明确可考、证据不足、单个考点/文件模型失败和大纲深度冲突五个场景。

- [ ] **Step 2: 增加生成载荷白名单回归**

序列化所有 `QuestionGenerationPayload` 后，允许字段只有题型、分值、难度、认知层级、考查方式、纯净知识内容、考查原子、答案边界、原型合同、表达政策和教师修订指令。断言不存在 `filename`、`page`、`evidence`、`framework_anchor`、`material_version_id`、`exam_point_id`。

- [ ] **Step 3: 运行新增端到端测试**

Run: `cd backend; pytest tests/integration/test_exam_point_pipeline.py -q`

Expected: PASS.

- [ ] **Step 4: 运行完整后端测试**

Run: `cd backend; pytest -q`

Expected: all tests pass; no previous framework, knowledge, blueprint, generation, MinerU or database test regresses.

- [ ] **Step 5: 执行静态和差异检查**

```powershell
git diff --check
rg -n "config\.json|safetensors|实验\s*\d+" backend/app -g '*.py'
```

Expected: `git diff --check` is clean. 第二条只能命中通用测试夹具或来源话术质量规则，不能命中新加入的课程专属知识过滤列表。

- [ ] **Step 6: 更新开发设计中的实现状态**

在核心开发设计第 22 节追加实际完成的模型、节点、回归数量和真实运行结果。只记录验证过的数据，不把未运行的网络链路写成已通过。

- [ ] **Step 7: 提交端到端回归**

```powershell
git add backend/tests/integration/test_exam_point_pipeline.py backend/tests/unit/test_generation_payload.py backend/tests/unit/test_question_quality_rules.py docs/superpowers/specs/2026-08-14-core-exam-system-development-design.md
git commit -m "test: cover exam-point-led paper quality"
```

---

## Final Verification Checklist

- [ ] 考核大纲先形成已确认 `ExamPoint`，教学资料不能创建新考试范围。
- [ ] 教学大纲深度冲突会阻断相关考点，不能静默放行。
- [ ] 每次语义分类只处理一个考点和一个文件的召回块。
- [ ] `background`、`out_of_scope` 和低于检索阈值的块不进入知识卡。
- [ ] 操作细节政策从考纲考点读取，不依赖课程专属禁词。
- [ ] 每个 active `AssessmentUnit` 有 `exam_point_id`，每张发布卡有有效 direct 证据。
- [ ] 蓝图按题型配置难度和考查方式，资料数量不会改变分配。
- [ ] `practical_operation` 只使用 `directly_assessable` 考点。
- [ ] 同卷综合题结构键不重复，近期五份试卷签名进入规划去重。
- [ ] 出题载荷不含文件、页码、证据、框架锚点和业务 ID。
- [ ] 单个考点/文件失败只影响对应覆盖状态，单题失败只局部重试。
- [ ] 后端完整测试和前端生产构建通过。
- [ ] 真实模型链路只有在 DeepSeek、MinerU 和 embedding 均配置后运行，并如实记录结果。
