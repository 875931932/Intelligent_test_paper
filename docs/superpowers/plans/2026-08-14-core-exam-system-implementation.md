# AI 期末试卷系统核心一期 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在空数据库和全新生产工程中，完成“多课程空间 → MinerU 文档解析 → 大纲命题框架 → 知识树 → 蓝图 → DeepSeek 候选试卷 → 结构化审核”的核心闭环。

**Architecture:** 采用 React + FastAPI 模块化单体。PostgreSQL 是业务事实来源，MinIO 保存文件和解析产物，Redis/Celery 执行后台任务，LangGraph 编排框架、资料整理和命题流程。核心一期不迁移已有数据库，只使用初始化脚本创建空库的扩展、表、约束、索引和开发种子。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2、PostgreSQL 16 + pgvector、MinIO、Redis、Celery、LangGraph、React、TypeScript、Vite、pytest、Docker Compose、MinerU 精准解析 API、OpenAI 兼容 DeepSeek API。

---

## 0. 执行边界

- 工作区没有生产数据库，不执行旧数据库迁移，不导入原型内存状态。
- 原型只作为提示词、课程夹具和质量回归参考。
- 首期使用固定开发教师身份，不实现登录和角色权限。
- 首期不实现 Word/PDF 导出、正式富文本和个性化对话入口。
- 真实验收使用 MinerU 和 DeepSeek，自动化测试使用 Fake Provider。
- 每个 Task 通过对应测试后才能进入下一个 Task。

### 0.1 当前执行进度（2026-08-14）

| Task | 状态 | 已验证结果 |
|---|---|---|
| Task 1 空库和工程 | 已完成 | 空库 Schema、课程复合外键、开发种子和健康检查 |
| Task 2 课程、文件暂存和 MinIO | 已完成 | 预签名上传、暂存、软删除和课程隔离 |
| Task 3 数据库任务、Outbox 和 Worker | 已完成 | 幂等、租约、恢复扫描和 Celery 入口 |
| Task 4 MinerU 文档解析 | 已完成 | 状态映射、安全 ZIP、ContentBlock 归一化和持久化 |
| Task 5 FrameworkGraph | 已完成 | 双大纲独立并行抽取、冲突、interrupt/确认、候选幂等和版本发布 |
| Task 6—11 | 待执行 | 按下述顺序继续 |

Task 5 的实现补充约束：同一 `framework_build_run_id` 最多产生一个候选版本；考核大纲和教学大纲从同一解析完成节点分叉，分别调用抽取器后再汇合；API 直接确认与 LangGraph 恢复发布必须执行相同的开放冲突完整性检查，未知冲突决定也不得写入。

## 1. Task 1：创建工程和空数据库

**Files:**

- Create: `backend/pyproject.toml`
- Create: `backend/app/main.py`
- Create: `backend/app/config.py`
- Create: `backend/app/db/schema.py`
- Create: `backend/app/db/init_db.py`
- Create: `backend/tests/unit/test_database_bootstrap.py`
- Create: `docker-compose.dev.yml`
- Create: `.env.example`

- [ ] **Step 1: 写空库初始化失败测试**

```python
def test_bootstrap_creates_core_schema(database_url):
    bootstrap_database(database_url, seed=False)
    assert table_exists(database_url, "courses")
    assert table_exists(database_url, "knowledge_cards")
    assert table_exists(database_url, "generation_runs")
    assert extension_exists(database_url, "vector")
```

- [ ] **Step 2: 运行测试并确认失败**

```powershell
docker compose -f docker-compose.dev.yml up -d postgres
cd backend
pytest tests/unit/test_database_bootstrap.py -q
```

预期：初始化模块尚不存在，测试失败。

- [ ] **Step 3: 实现空库初始化**

`init_db.py` 固定执行：

```text
连接 DATABASE_URL
→ CREATE EXTENSION IF NOT EXISTS vector
→ SQLAlchemy metadata.create_all
→ 创建复合索引和唯一约束
→ SEED_DEV_DATA=true 时幂等写入 owner-dev 和示例课程
```

不得创建旧库迁移任务，也不得读取原型状态。

- [ ] **Step 4: 运行测试并确认重复初始化安全**

```powershell
pytest tests/unit/test_database_bootstrap.py -q
```

预期：PASS；连续执行两次不会重复插入开发种子。

- [ ] **Step 5: 实现健康检查**

`GET /api/v1/health` 返回 API、PostgreSQL、Redis、MinerU 配置和 DeepSeek 配置状态。

## 2. Task 2：课程、文件暂存和 MinIO

**Files:**

- Create: `backend/app/domain/course/models.py`
- Create: `backend/app/domain/material/models.py`
- Create: `backend/app/services/course_service.py`
- Create: `backend/app/services/material_service.py`
- Create: `backend/app/adapters/storage/minio_storage.py`
- Create: `backend/app/api/v1/courses.py`
- Create: `backend/app/api/v1/materials.py`
- Test: `backend/tests/unit/test_material_service.py`
- Test: `backend/tests/integration/test_material_upload.py`

- [ ] **Step 1: 写课程隔离和上传暂存测试**

```python
def test_completed_upload_is_staged_without_parser_task():
    material = complete_upload(course_id, "outline.pdf", expected_hash)
    assert material.status == "staged"
    assert count_task_runs("document_parse") == 0

def test_other_course_cannot_read_material():
    with pytest.raises(NotFoundError):
        get_material(other_course_id, material_id)
```

- [ ] **Step 2: 实现课程和文件领域对象**

所有资源查询必须同时包含资源 ID 和 `course_id`。文件完成接口校验对象存在、大小、SHA-256、扩展名、MIME 和课程对象前缀。

- [ ] **Step 3: 实现预签名上传**

浏览器直传 MinIO；后端只返回短期 PUT 地址，不下发 MinIO 密钥。`complete` 只创建 `MaterialVersion(staged)`。

- [ ] **Step 4: 运行测试**

```powershell
pytest tests/unit/test_material_service.py tests/integration/test_material_upload.py -q
```

## 3. Task 3：数据库任务、Outbox 和 Worker

**Files:**

- Create: `backend/app/infrastructure/tasks/models.py`
- Create: `backend/app/infrastructure/tasks/outbox.py`
- Create: `backend/app/infrastructure/tasks/celery_app.py`
- Create: `backend/app/infrastructure/tasks/recovery.py`
- Test: `backend/tests/unit/test_task_idempotency.py`
- Test: `backend/tests/integration/test_worker_recovery.py`

- [ ] **Step 1: 写幂等、租约和取消测试**

验证相同 `idempotency_key` 只创建一个任务；租约过期后可重新领取；取消任务的迟到结果不能写回。

- [ ] **Step 2: 实现 `task_runs` 和 `outbox_events`**

任务表保存运行 ID、课程、任务类型、输入版本、幂等键、阶段、进度、租约、重试和错误码。业务事务与 Outbox 写入必须原子完成。

- [ ] **Step 3: 实现投递和恢复扫描**

扫描 `queued` 未投递、`running` 租约过期、`waiting_external` 到达轮询时间的任务并重新投递。

- [ ] **Step 4: 运行测试**

```powershell
pytest tests/unit/test_task_idempotency.py tests/integration/test_worker_recovery.py -q
```

## 4. Task 4：MinerU 文档解析

**Files:**

- Create: `backend/app/adapters/document/protocol.py`
- Create: `backend/app/adapters/document/mineru_client.py`
- Create: `backend/app/adapters/document/local_text_parser.py`
- Create: `backend/app/services/document_processing_service.py`
- Test: `backend/tests/unit/test_mineru_state_mapping.py`
- Test: `backend/tests/unit/test_content_normalizer.py`
- Test: `backend/tests/contract/test_mineru_fake_provider.py`

- [ ] **Step 1: 创建 Fake MinerU 契约测试**

覆盖申请上传地址、PUT、`waiting-file`、`pending`、`running`、`converting`、`done`、`failed`、临时 HTTP 错误和空结果。

- [ ] **Step 2: 定义解析端口**

```python
class DocumentParser(Protocol):
    async def submit(self, request: ParseRequest) -> ParseSubmission: ...
    async def poll(self, provider_batch_id: str) -> ParseProgress: ...
    async def fetch(self, provider_batch_id: str) -> ParseArtifact: ...
```

- [ ] **Step 3: 实现 MinerU 调用**

```text
POST /api/v4/file-urls/batch
→ material_version_id 作为 data_id
→ PUT 原文件
→ GET /api/v4/extract-results/batch/{batch_id}
→ done 后下载 full_zip_url
```

- [ ] **Step 4: 实现 ZIP 检查和归一化**

拒绝路径穿越、异常压缩比和超量文件。优先读取 `content_list.json`，并保存 `full.md`、`middle.json` 和资源文件。输出统一 `ContentBlock`。

- [ ] **Step 5: 运行测试**

```powershell
pytest tests/unit/test_mineru_state_mapping.py tests/unit/test_content_normalizer.py tests/contract/test_mineru_fake_provider.py -q
```

## 5. Task 5：FrameworkGraph

**Files:**

- Create: `backend/app/domain/framework/models.py`
- Create: `backend/app/services/framework_service.py`
- Create: `backend/app/workflows/framework_graph.py`
- Create: `backend/app/api/v1/framework.py`
- Test: `backend/tests/unit/test_framework_rules.py`
- Test: `backend/tests/workflow/test_framework_graph.py`

- [ ] **Step 1: 写大纲权威和冲突测试**

验证两份大纲独立解析；考核大纲创建范围、权重和题型；教学大纲只能校验已教内容；冲突不能静默解决。

- [ ] **Step 2: 实现图节点**

```text
validate_inputs
→ freeze_outline_versions
→ ensure_document_parsed
→ extract_teaching_syllabus
→ extract_assessment_syllabus
→ merge_assessment_led_framework
→ validate_conflicts
→ persist_candidate
→ interrupt_teacher_confirmation
→ publish_framework_version
```

- [ ] **Step 3: 实现结构化确认 API**

确认请求提交锚点修改、冲突决定、权重修订和排除项，不能只有 `confirmed=true`。

- [ ] **Step 4: 运行测试**

```powershell
pytest tests/unit/test_framework_rules.py tests/workflow/test_framework_graph.py -q
```

## 6. Task 6：知识树和 OrganizationGraph

**Files:**

- Create: `backend/app/domain/knowledge/models.py`
- Create: `backend/app/services/knowledge_tree_service.py`
- Create: `backend/app/services/knowledge_publish_service.py`
- Create: `backend/app/workflows/organization_graph.py`
- Create: `backend/app/workflows/knowledge_catalog_subgraph.py`
- Create: `backend/app/api/v1/knowledge.py`
- Test: `backend/tests/unit/test_knowledge_tree_rules.py`
- Test: `backend/tests/workflow/test_organization_graph.py`
- Test: `backend/tests/workflow/test_knowledge_catalog_subgraph.py`

- [ ] **Step 1: 写树准入和归并测试**

必须验证：L1 只来自考核大纲；无映射候选进入 `unmatched`；封面、文件名、提交要求不能成为 L3/L4；同义候选归并；无事实来源的知识卡不能发布。

- [ ] **Step 2: 实现 L1/L2 骨架和逐文件候选**

资料整理一次只处理一个文件及最小框架摘要，输出候选主题、考核单元、纯净知识卡和独立来源关系。

- [ ] **Step 3: 实现聚类归并子图**

按框架锚点分组，使用术语、关键词、哈希和向量形成候选簇；归并模型只接收小批候选摘要。

- [ ] **Step 4: 实现树编辑和原子发布**

支持合并、拆分、改名、同一 L1 内移动、排除、标重点和降级为素材。发布事务同时写知识目录、节点、知识卡、来源关系和索引版本。

- [ ] **Step 5: 运行测试**

```powershell
pytest tests/unit/test_knowledge_tree_rules.py tests/workflow/test_organization_graph.py tests/workflow/test_knowledge_catalog_subgraph.py -q
```

## 7. Task 7：蓝图和逐题 PlanItem

**Files:**

- Create: `backend/app/domain/blueprint/models.py`
- Create: `backend/app/services/blueprint_service.py`
- Create: `backend/app/api/v1/blueprints.py`
- Test: `backend/tests/unit/test_blueprint_allocation.py`
- Test: `backend/tests/integration/test_blueprint_api.py`

- [ ] **Step 1: 写分值和权重测试**

覆盖总分、题数、每题分值、0.5 分倍数、题型配额、期末考试章节权重和题型比例。

- [ ] **Step 2: 实现题型边际和章节分配**

先固定题型题数和总分，再求章节、认知层级和难度分配。禁止按知识点数量创建题位。

- [ ] **Step 3: 实现知识树覆盖和蓝图确认**

每个 PlanItem 绑定已确认 L3 和至少一张 L4。确认后修改必须创建新 `BlueprintVersion`。

- [ ] **Step 4: 运行测试**

```powershell
pytest tests/unit/test_blueprint_allocation.py tests/integration/test_blueprint_api.py -q
```

## 8. Task 8：GenerationGraph 和 DeepSeek

**Files:**

- Create: `backend/app/domain/generation/models.py`
- Create: `backend/app/schemas/generation.py`
- Create: `backend/app/adapters/model/deepseek_gateway.py`
- Create: `backend/app/services/generation_service.py`
- Create: `backend/app/workflows/generation_graph.py`
- Create: `backend/app/api/v1/generation.py`
- Test: `backend/tests/unit/test_generation_payload.py`
- Test: `backend/tests/unit/test_question_quality_rules.py`
- Test: `backend/tests/workflow/test_generation_graph.py`
- Test: `backend/tests/contract/test_deepseek_fake_provider.py`

- [ ] **Step 1: 写来源隔离测试**

`backend/app/schemas/generation.py` 必须分别定义仅后端使用的 `EvidenceTracePack` 和唯一允许发送给模型的 `QuestionGenerationPayload`，二者不能继承同一个包含来源字段的基类。

```python
payload = compile_question_generation_payload(plan_item, knowledge_card)
text = json.dumps(payload, ensure_ascii=False)
assert "evidence_id" not in text
assert "filename" not in text
assert "page_index" not in text
assert "assessment_unit_id" not in text
```

- [ ] **Step 2: 实现 DeepSeek 网关**

网关只负责请求、超时、HTTP 错误、请求 ID、JSON 解析、用量和脱敏诊断，不负责课程业务规则。

- [ ] **Step 3: 实现 GenerationGraph**

```text
load_frozen_versions
→ validate_blueprint
→ compile_source_free_payloads
→ generate_type_batch
→ validate_schema
→ deterministic_checks
→ semantic_review
→ local_repair(max=2)
→ backend_attach_evidence
→ aggregate_paper
```

- [ ] **Step 4: 实现首期题型模板**

实现单选、判断、填空、简答的输入字段、输出 Schema、答案规则、评分规则和禁止项。

- [ ] **Step 5: 运行测试**

```powershell
pytest tests/unit/test_generation_payload.py tests/unit/test_question_quality_rules.py tests/workflow/test_generation_graph.py tests/contract/test_deepseek_fake_provider.py -q
```

## 9. Task 9：候选卷审核

**Files:**

- Create: `backend/app/domain/paper/models.py`
- Create: `backend/app/services/paper_service.py`
- Create: `backend/app/api/v1/papers.py`
- Test: `backend/tests/unit/test_paper_revision.py`
- Test: `backend/tests/integration/test_paper_review_api.py`

- [ ] **Step 1: 写题目修订和排序测试**

题干、答案、评分点、分值或顺序变化必须创建修订；显示题号不能作为业务主键。

- [ ] **Step 2: 实现审核操作**

支持采用、拒绝、编辑、单题重生成、删除和排序。编辑后重新运行受影响质量检查。

- [ ] **Step 3: 实现整卷校验**

检查总分、题型题数、蓝图偏差、重复风险、未处理 blocker 和顺序。

- [ ] **Step 4: 运行测试**

```powershell
pytest tests/unit/test_paper_revision.py tests/integration/test_paper_review_api.py -q
```

## 10. Task 10：核心前端

**Files:**

- Create: `frontend/src/app/router.tsx`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/features/courses/CourseListPage.tsx`
- Create: `frontend/src/features/materials/MaterialUploadPage.tsx`
- Create: `frontend/src/features/framework/FrameworkReviewPage.tsx`
- Create: `frontend/src/features/knowledge-tree/KnowledgeTreePage.tsx`
- Create: `frontend/src/features/blueprint/BlueprintPage.tsx`
- Create: `frontend/src/features/paper-review/PaperReviewPage.tsx`
- Create: `frontend/src/components/RunProgress.tsx`
- Test: `frontend/src/features/knowledge-tree/KnowledgeTreePage.test.tsx`
- Test: `frontend/src/features/paper-review/PaperReviewPage.test.tsx`

- [ ] **Step 1: 实现类型化 API 和运行轮询**

统一处理 `202 + run_id`、版本冲突、课程路径、错误码和页面刷新恢复。

- [ ] **Step 2: 实现课程和上传页**

分开显示大纲与教学材料，支持多选；上传后只显示 `staged`，必须主动创建运行。

- [ ] **Step 3: 实现框架和知识树审核页**

知识树左侧显示 L1-L4，右侧显示纯净知识卡、覆盖、来源和差异；来源详情不能作为出题载荷提交。

- [ ] **Step 4: 实现蓝图和候选卷页**

展示逐题 PlanItem、章节分布、质量状态、答案、评分细则、来源、单题重生成和排序。

- [ ] **Step 5: 运行测试和构建**

```powershell
cd frontend
npm test -- --run
npm run build
```

## 11. Task 11：端到端验收

**Files:**

- Create: `backend/tests/e2e/test_core_flow.py`
- Create: `backend/tests/fixtures/course/`
- Create: `backend/tests/fixtures/fake/mineru/`
- Create: `backend/tests/fixtures/fake/deepseek/`
- Create: `README.md`

- [ ] **Step 1: 编写核心闭环测试**

```text
创建课程
→ 上传两份大纲和教学资料
→ 构建并确认框架
→ 整理资料并确认知识树
→ 发布目录和索引
→ 创建并确认蓝图
→ 生成候选卷
→ 检查来源回链和模型载荷隔离
→ 修改一道题并重新质检
```

- [ ] **Step 2: 加入历史问题夹具**

验证封面、文件名、实验编号、安装步骤和提交要求不会进入 L3/L4；考纲权重进入蓝图；模型请求没有来源字段；空内容和网络错误保留诊断。

- [ ] **Step 3: 执行全新空库验收**

```powershell
docker compose -f docker-compose.dev.yml down -v
docker compose -f docker-compose.dev.yml up -d postgres redis minio
Push-Location backend
python -m app.db.init_db --seed
pytest tests -q
Pop-Location
```

`down -v` 只用于开发 Compose 项目，不得用于生产数据或工作区目录。

- [ ] **Step 4: 完成运行手册**

README 必须写明空库初始化、MinIO、MinerU、DeepSeek、API、Worker、前端、Fake Provider、外部资料发送提示和诊断位置。

## 12. 计划自检

### 12.1 不做旧库迁移

数据库阶段只执行：

```text
创建空 PostgreSQL
→ 安装 pgvector
→ 创建初始表、约束和索引
→ 写入可选开发种子
```

### 12.2 阻断条件

以下情况不能进入下一任务：

- 课程数据查询没有 `course_id`；
- 知识树脱离考核大纲创建 L1；
- 无事实来源的知识卡可以发布；
- `EvidenceTracePack` 被序列化到模型请求；
- 题位没有冻结蓝图版本；
- MinerU/DeepSeek 失败被伪装成功；
- 单题 blocker 被忽略；
- 前端能绕过教师确认直接发布或生成。

## 13. 执行顺序

```text
Task 1 空库和工程
→ Task 2 课程/上传
→ Task 3 任务系统
→ Task 4 MinerU
→ Task 5 命题框架
→ Task 6 知识树
→ Task 7 蓝图
→ Task 8 生成
→ Task 9 候选卷
→ Task 10 前端
→ Task 11 端到端验收
```
