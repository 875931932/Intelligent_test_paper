# 后端接口清单（API Inventory）

> 本文档从 `backend/app/api/v1/*.py` 的 FastAPI 路由源码逐条提取，
> 是前端 API 层与联调测试的唯一权威接口依据。
> 基础前缀：`/api/v1`（除注明外）；JSON 请求体需 `Content-Type: application/json`。

## 约定

- 所有业务路由均以 **课程（course_id）** 为作用域前缀：`/api/v1/courses/{course_id}/...`
- 错误统一返回 `{"detail": string | object}`；常见状态码见各端点。
- 分页：当前无分页端点，列表全量返回。

---

## 1. 系统 / 健康

### 1.1 `GET /api/v1/health`
健康检查（不依赖 DB 即可返回）。
```json
{ "api": "ok", "postgresql": "ok", "redis": "ok", "mineru": "configured", "deepseek": "configured" }
```
字段取值：`api`=`ok`；`postgresql`/`redis`=`ok|unavailable|not_configured`；`mineru`/`deepseek`=`configured|not_configured`。

### 1.2 `PUT /api/v1/_local-storage/{object_key:path}`
当对象存储（MinIO）不可用、回退到本地存储时，前端直接用 PUT 上传二进制文件。
- 路径 `object_key` 为任意层级路径。
- Body：原始二进制；请求头 `Content-Type` 会被记录。
- 返回：`200`（空）。前端应在上传失败时改用此端口兜底。

---

## 2. 课程 Courses

> 来源 `app/api/v1/courses.py`。前缀 `/api/v1/courses`

| 方法 | 路径 | 状态码 | 响应 |
|------|------|--------|------|
| POST | `/api/v1/courses` | 201 | `CourseResponse` |
| GET  | `/api/v1/courses` | 200 | `CourseResponse[]` |
| GET  | `/api/v1/courses/{course_id}` | 200 | `CourseResponse` |
| PATCH| `/api/v1/courses/{course_id}` | 200 | `CourseResponse` |

**CourseCreate**（POST body）：
```json
{ "name": "string(≤200, 必填)", "slug": "string(≤120, 默认'')", "description": "string≤10000|null" }
```
**CourseUpdate**（PATCH body，均为可选）：`name`/`slug`/`description`；`slug` 需匹配 `^[a-z0-9]+(?:-[a-z0-9]+)*$`，且不能为 null。

**CourseResponse**：
```json
{ "id": "uuid", "owner_id": "uuid", "name": "string", "slug": "string", "description": "string|null" }
```
错误：404 `course not found`；409 `course slug already exists`。

---

## 3. 资料材料 Materials / 上传

> 来源 `app/api/v1/materials.py`。前缀 `/api/v1/courses/{course_id}`

### 3.1 创建上传会话
`POST /api/v1/courses/{course_id}/upload-sessions` → **201** `UploadSessionResponse`
```json
{ "filename": "x.pdf", "material_type": "teaching_syllabus", "size_bytes": 123, "sha256": "<64位小写hex>", "mime_type": "application/pdf", "existing_material_id": "uuid|null" }
```
`material_type` ∈ `teaching_syllabus|assessment_syllabus|teaching_material|exercise`。
`filename` 必须有允许扩展名（pdf/doc/docx/ppt/pptx/xls/xlsx/txt/md/jpg/jpeg/png/gif/webp/bmp），`mime_type` 必须匹配该扩展名。
响应：
```json
{ "session_id": "uuid", "object_key": "string", "upload_url": "string", "expires_at": "ISO8601", "headers": {"x-amz-...": "string"} }
```
`upload_url` + `headers` 用于直接上传文件二进制（S3 PUT）；冲突 409，参数错误 422，存储不可用 503。

### 3.2 完成上传会话
`POST /api/v1/courses/{course_id}/upload-sessions/{session_id}/complete` → **200** `MaterialVersionResponse`
```json
{ "id": "uuid", "material_id": "uuid", "status": "string", "version_no": 1, "sha256": "string", "mime_type": "string", "size_bytes": 123 }
```
过期 410；对象变更 409；存储不可用 503。

### 3.3 列表资料
`GET /api/v1/courses/{course_id}/materials?include_deleted=false` → **200** `MaterialResponse[]`
```json
{ "id": "uuid", "course_id": "uuid", "logical_name": "string", "material_type": "string", "status": "string",
  "latest_version": { "id":"uuid","material_id":"uuid","status":"string","version_no":1,"sha256":"string","mime_type":"string","size_bytes":123 } | null,
  "parse_status": { "id":"uuid","status":"string","error_code":"string?","error_summary":"string?" } | null }
```

### 3.4 单资料详情
`GET /api/v1/courses/{course_id}/materials/{material_id}` → `MaterialResponse`

### 3.5 启动解析（MinerU）
`POST /api/v1/courses/{course_id}/materials/{material_id}/parse` → **202**
同哈希已 ready 会直接复用。错误状态码随 `ParseError.status_code`（多为 409/422）。

### 3.6 轮询解析
`POST /api/v1/courses/{course_id}/materials/{material_id}/parse/poll` → 推进一次解析状态机，前端周期调用直至 `ready` / `failed`。返回解析状态对象。

### 3.7 修改资料类型
`PATCH /api/v1/courses/{course_id}/materials/{material_id}/type?material_type=exercise` → `MaterialResponse`；非法类型 422。

### 3.8 删除资料
`DELETE /api/v1/courses/{course_id}/materials/{material_id}` → **204**（无 body）

---

## 4. 命题框架 Framework

> 来源 `app/api/v1/framework.py`。前缀 `/api/v1/courses/{course_id}`

### 4.1 创建框架构建（异步）
`POST /api/v1/courses/{course_id}/framework-runs` → **202**
```json
{ "teaching_material_version_id": "uuid", "assessment_material_version_id": "uuid" }
```
响应：`{ "run_id": "uuid", "candidate_id": "uuid", "status": "awaiting_teacher_confirmation" }`
需要 DeepSeek 已配置，否则 503。

### 4.2 最近一次运行
`GET /api/v1/courses/{course_id}/framework-runs/latest` → `FrameworkBuildRun`
```json
{ "id": "uuid", "course_id": "uuid", "status": "string", "candidate_id": "uuid?", "error_code": "string?", "error_message": "string?", "created_at": "ISO8601" }
```
无记录 404。

### 4.3 运行详情
`GET /api/v1/courses/{course_id}/framework-runs/{run_id}` → `FrameworkBuildRun`

### 4.4 候选项（已展开 payload）
`GET /api/v1/courses/{course_id}/framework-runs/{run_id}/candidate`
返回运行记录顶层字段 + `payload` 展开到顶层：
```json
{ "anchors":[ {"key":"","title":"","exam_weight":0.0,"ability_requirements":[],"allowed_question_types":[],"excluded_content":[],"alignment_keys":[]} ],
  "exam_points":[ {"code":"","anchor_key":"","title":"","assessment_requirement":"","weight_value":0.0,"weight_source":"","cognitive_targets":[],"allowed_question_types":[],"operational_detail_policy":""} ],
  "teaching_topics":[], "conflicts":[{"key":"","kind":"","message":"","status":"open|resolved"}], "final_exam_rules":{} }
```

### 4.5 确认框架
`POST /api/v1/courses/{course_id}/framework-runs/{run_id}/confirm` → 200
```json
{ "anchors":[...同上...], "exam_points":[...同上...],
  "conflict_resolutions": {"<key>": "resolution"},
  "teacher_exclusions": ["string"] }
```
返回 `{ "candidate_id": "uuid", "published_id": "uuid" }`。

### 4.6 拒绝框架
`POST /api/v1/courses/{course_id}/framework-runs/{run_id}/reject` → 200

### 4.7 当前已发布框架
`GET /api/v1/courses/{course_id}/framework-versions/current` → 200
已发布：`{ "published": true, "id": "uuid", "candidate_id": "uuid", "payload": {...} }`
未发布：`{ "published": false, "detail": "no published framework version" }`

---

## 5. 知识目录 Knowledge

> 来源 `app/api/v1/knowledge.py`。前缀 `/api/v1/courses/{course_id}`

### 5.1 创建知识组织运行（异步）
`POST /api/v1/courses/{course_id}/organization-runs` → **202**
```json
{ "material_version_ids": ["uuid", "..."] }
```
响应：`{ "run_id": "uuid", "candidate_id": "uuid", "status": "awaiting_teacher_confirmation" }`

### 5.2 运行详情
`GET /api/v1/courses/{course_id}/organization-runs/{run_id}` → 运行记录对象

### 5.3 候选项
`GET /api/v1/courses/{course_id}/organization-runs/{run_id}/candidate` → 候选对象

### 5.4 发布知识树
`POST /api/v1/courses/{course_id}/organization-runs/{run_id}/publish` → 200
```json
{ "operations":[ {"operation":"string","target_code":"string","value":"string?"} ],
  "reviewed_topic_codes":["string"], "reviewed_exam_point_codes":["string"], "teacher_exclusions":["string"] }
```
返回发布结果；冲突 409。

### 5.5 已发布知识（命题输入视图）★前端蓝图/合同主数据
`GET /api/v1/courses/{course_id}/published-knowledge` → 200
```json
{ "catalog_version_id": "uuid", "framework_version_id": "uuid",
  "exam_points":[ {"id":"uuid","code":"","title":"","assessment_requirement":"","anchor_key":"","weight_value":0.0,"weight_source":"","cognitive_targets":[],"allowed_question_types":[],"operational_detail_policy":""} ],
  "units":[ {"unit_id":"uuid","code":"","title":"","performance_statement":"","exam_point_id":"uuid|''","exam_point_code":"","anchor_key":"","card_ids":["uuid"]} ],
  "knowledge_cards": { "<card_id>": { "name":"","performance_statement":"","assessable_content":"...","scope_boundary":{},"cognitive_targets":[],"allowed_question_types":[],"importance":0.0,"concept_cluster":"","answer_proposition":"","answer_boundary":"","prompt_material":[],"relation_edges":[],"grounded":true } } }
```
未发布：`{ "published": false, "knowledge_cards": {}, "assessment_units": [], "content_domains": [], "exam_points": [], "units": [] }`

### 5.6 知识卡证据链
`GET /api/v1/courses/{course_id}/published-knowledge/cards/{card_id}/evidence` → 200
```json
[ { "evidence_role": "direct|supporting|background", "confidence": 0.9, "content": "string", "locator": "string", "material_version_id": "uuid" } ]
```

---

## 6. 蓝图（传统/独立路由）Blueprints

> 来源 `app/api/v1/blueprints.py`。前缀 `/api/v1/courses/{course_id}`
> 注：生产主流程改用「试卷项目」下的蓝图子端点（见 §8.4–8.7）。此路由保留为无项目作用域的蓝图契约端点。

### 6.1 分配合同（allocate）
`POST /api/v1/courses/{course_id}/blueprints/allocate` → 200 `PaperContract`
**ContractRequest**：
```json
{ "total_score": 100.0,
  "type_rules": {"single_choice": {"weight": 0.3}},
  "chapter_weights": {"<anchor_key>": 0.5},
  "units": [ {"unit_id":"uuid","exam_point_id":"uuid","anchor_key":"","card_ids":["uuid"],"allowed_assessment_modes":[],"operational_detail_policy":"","core":false} ],
  "card_question_types": {}, "card_semantic_profiles": {} }
```
**PaperContract**：
```json
{ "total_score": 100.0,
  "slots": [ { "item_index":1,"question_type":"","score":5.0,"difficulty":"","cognitive_level":"","assessment_mode":"",
               "exam_point_id":"uuid","anchor_key":"","unit_id":"uuid","card_id":"uuid","coverage_atom":"","answer_boundary":"",
               "performance_statement":"","prompt_material":[],"scope_boundary":{},"preferred_terms":[],
               "forbidden_context":{"atoms":[],"answer_cores":[]},"comprehensive_archetype":null,"material_form":null,
               "cognitive_sequence":[],"subquestion_count_range":null,"subquestion_actions":[],"answer_boundaries":[] } ],
  "conflicts": [ {"code":"atom_pool_insufficient|cluster_exhausted|missing_exam_point","exam_point_id":"","message":"","detail":{}} ],
  "audit_summary": { "exam_points":[{"exam_point_id":"","weight":0.0,"question_count":0,"proportion":0.0}], "type_counts":{}, "difficulty_counts":{} } }
```

### 6.2 确认合同
`POST /api/v1/courses/{course_id}/blueprints/confirm` → 200 `PaperContract`
```json
{ "contract": { ...PaperContract... },
  "slot_revisions": [ ... ],
  "units": [ ...UnitCoverage... ],
  "knowledge_cards": {} }
```

---

## 7. 生成 Generation（直接同步）

> 来源 `app/api/v1/generation.py`。前缀 `/api/v1/courses/{course_id}`
> 注：生产主流程用「试卷项目」下的异步 `generate` + `task-runs`（见 §8.12–8.13）。此路由为同步候选生成，供调试/短流程。

### 7.1 直接生成
`POST /api/v1/courses/{course_id}/generation-runs` → **202**
```json
{ "contract": [ ...ContractSlot... ], "knowledge_cards": {} }
```
响应：
```json
{ "status": "candidate", "questions": [ { "item_index":1,"question_type":"","stem":"","options":{},"answer":"","explanation":"","score":5.0,"difficulty":"","cognitive_level":"","assessment_mode":"","exam_point_id":"","card_id":"","coverage_atom":"" } ],
  "final_check": {}, "model_call_count": 0, "model": "mimo-v2.5-pro" }
```
非法 contract 422；生成失败 502。

---

## 8. 试卷项目 Exam Projects（主流程）

> 来源 `app/api/v1/exam_projects.py`。前缀 `/api/v1/courses/{course_id}/exam-projects`

### 8.1 列表
`GET /api/v1/courses/{course_id}/exam-projects` → 200 `list[ExamProject]`
```json
{ "id":"uuid","course_id":"uuid","name":"string","status":"string",
  "active_blueprint_version_id":"uuid?","active_paper_version_id":"uuid?",
  "model":"string?","total_score":0.0,"item_count":0,"created_at":"ISO8601","updated_at":"ISO8601" }
```

### 8.2 创建
`POST /api/v1/courses/{course_id}/exam-projects` → **201**；body `{ "name": "string" }`；同名 409。

### 8.3 详情
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}` → `ExamProject`

### 8.4 更新状态
`PATCH /api/v1/courses/{course_id}/exam-projects/{project_id}`；body `{ "status": "string" }`

### 8.5 创建蓝图
`POST /api/v1/courses/{course_id}/exam-projects/{project_id}/blueprints` → **201**
```json
{ "framework_version_id":"uuid", "catalog_version_id":"uuid",
  "type_rules":{}, "chapter_weights":{},
  "units":[ {...UnitCoverage...} ],
  "card_semantic_profiles":{}, "card_question_types":{} }
```
响应：`{ "blueprint_version_id":"uuid", "plan":[ ...PlanItem... ] }`
PlanItem：`{ "item_index":0,"question_type":"","score":0.0,"anchor_key":"","exam_point_id":"","unit_id":"","card_id":"","difficulty":"","cognitive_level":"","assessment_mode":"","concept_cluster":"","answer_proposition":"","required_propositions":[],"relation_edges":[],"instance_carriers":[] }`
缺 key 422；引用不存在 404。

### 8.6 当前蓝图计划项
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/blueprints/current/plan-items` → `list[PlanItem]`

### 8.7 修改计划项
`PATCH /api/v1/courses/{course_id}/plan-items/{plan_item_id}`
允许 key ∈ `score|question_type|difficulty|cognitive_level|exam_point_id|card_id`；其它 key 422。

### 8.8 确认蓝图
`POST /api/v1/courses/{course_id}/exam-projects/{project_id}/blueprints/current/confirm`
body 可选 `{ "blueprint_version_id": "uuid" }`。返回确认结果。

### 8.9 分配合同
`POST /api/v1/courses/{course_id}/exam-projects/{project_id}/contracts/allocate`
body 可选 `{ "blueprint_version_id":"uuid", "allocation_seed":123 }`。响应：
```json
{ "used_threshold": 0.6, "conflicts_history": [["string"]], "contract_snapshot": { ...PaperContract... } }
```

### 8.10 修订合同（仅预览不落库）
`PATCH /api/v1/courses/{course_id}/exam-projects/{project_id}/contracts/revise`
body：`{ "blueprint_version_id?":"uuid", "slot_revisions":[...], "allocation_seed?":123 }`
响应：`{ "revised_contract_snapshot": { ...PaperContract... } }`

### 8.11 确认合同（落库并生成 paper_version）
`POST /api/v1/courses/{course_id}/exam-projects/{project_id}/contracts/confirm` → **201**
body：`{ "blueprint_version_id?":"uuid", "slot_revisions":[...], "allocation_seed?":123 }`

### 8.12 启动异步生成
`POST /api/v1/courses/{course_id}/exam-projects/{project_id}/generate` → **202**
body 可选 `{ "mock_graph": false }`；生产必须配置 LLM，否则 503。响应：`{ "task_run_id":"uuid" }`

### 8.13 任务运行详情
`GET /api/v1/courses/{course_id}/task-runs/{task_run_id}` → **200**
```json
{ "id":"uuid","course_id":"uuid","task_type":"string","status":"string","stage":"string","progress":0.0,"attempt":0,
  "payload":{},"result":{},"error_code":"string?","error_message":"string?",
  "created_at":"ISO8601","updated_at":"ISO8601","completed_at":"ISO8601?" }
```

---

## 9. 试卷版本 Paper Versions

> 来源 `app/api/v1/paper_versions.py`。前缀 `/api/v1/courses/{course_id}`
> 注意路径两种形态：`exam-projects/{project_id}/paper-versions/...` 与 `paper-versions/{pv_id}/...`。

### 9.1 当前试卷版本（按项目解析）
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-versions/current` → 200
```json
{ "id":"uuid","exam_project_id":"uuid","version_no":1,"total_score":100.0,"status":"string",
  "items":[ { "item_index":1,"question_type":"","stem":"","options":{},"answer":"","explanation":"","scoring_detail":"",
              "needs_review":false,"needs_review_reasons":[],"traceability":{},"teacher_override_patch":{},"teacher_override_at":"ISO8601?" } ],
  "created_at":"ISO8601" }
```

### 9.2 待审核项
`GET /api/v1/courses/{course_id}/paper-versions/{pv_id}/needs-review` → 200
```json
[ { "item_index":1,"question_type":"","stem_preview":"","reasons":[] } ]
```

### 9.3 修改题目项
`PATCH /api/v1/courses/{course_id}/paper-versions/{pv_id}/items/{item_index}`
body：`{ "teacher_override_patch":{}, "clear_needs_review":false }`（仅这两 key）

### 9.4 确认试卷版本
`POST /api/v1/courses/{course_id}/paper-versions/{pv_id}/confirm`
body 可选 `{ "force_ignore_needs_review":false }`。有未审核项返回 409（detail 含 `item_indices`）。

### 9.5 回退到候选
`POST /api/v1/courses/{course_id}/paper-versions/{pv_id}/revert`

### 9.6 导出：答案细则 JSON
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-versions/{pv_id}/export/json`
→ 附件下载（`Content-Disposition: attachment; filename="answer_detail_v{n}.json"`）。

### 9.7 导出：学生卷 HTML
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-versions/{pv_id}/export/student`
→ `text/html`（无答案，可打印 PDF）。

### 9.8 导出：答卷（含答案）HTML
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-versions/{pv_id}/export/answer-key`
→ `text/html`（含答案）。

---

## 10. 数据模型汇总（复用类型）

| 类型 | 说明 | 关键字段 |
|------|------|----------|
| `MaterialType` | `teaching_syllabus` / `assessment_syllabus` / `teaching_material` / `exercise` | — |
| `AssessmentMode` | `theory_recall` / `conceptual` / `application` / `problem_solving` / `practical_operation` | — |
| `ComprehensiveArchetype` | 综合题原型 | 见 `domain/generation/archetypes.py` |
| `MaterialForm` | 材料形式 | 同上 |
| `UnitCoverage` | 单元覆盖 | `unit_id, exam_point_id, anchor_key, card_ids, allowed_assessment_modes, operational_detail_policy, core` |
| `FrameworkConfirmation` | 框架确认 | `anchors, exam_points, conflict_resolutions, teacher_exclusions` |
| `KnowledgeTreeConfirmation` | 知识树确认 | `operations, reviewed_topic_codes, reviewed_exam_point_codes, teacher_exclusions` |

## 11. 状态码约定

| 码 | 含义 |
|----|------|
| 200 | OK |
| 201 | 创建成功（POST 资源） |
| 202 | 已受理（异步任务） |
| 204 | 无内容（DELETE） |
| 404 | 资源不存在 |
| 409 | 冲突（重名、状态冲突、未审核确认等） |
| 410 | 上传会话过期 |
| 422 | 参数/校验错误 |
| 502 | 下游（LLM/嵌入）生成失败 |
| 503 | 依赖未配置/存储不可用 |