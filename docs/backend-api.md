# 后端接口清单（API Inventory）

> 本文档从 `backend/app/api/v1/*.py` 的 FastAPI 路由源码逐条提取，
> 是前端 API 层与联调测试的唯一权威接口依据。
> 基础前缀：`/api/v1`（除注明外）；JSON 请求体需 `Content-Type: application/json`。

## 约定

- 所有业务路由均以 **课程（course_id）** 为作用域前缀：`/api/v1/courses/{course_id}/...`
- **多租户底线**：路径上的 `{course_id}` 是唯一租户判定依据。服务层每一条数据库查询、
  缓存键、对象存储路径都必须携带 `course_id` 过滤，⚠️ 且**禁止从被查询的行里反推租户**
  （`course_id = row["course_id"]` 等于把校验交给被传入的那行）。id 属于别的课程时一律按
  "不存在"处理（404/空结果），不区分"存在但不属于你"与"根本不存在"。
  例外仅限基础设施队列（`outbox_events`/`task_runs` 按 id + `claim_owner` 领取任务），
  它们是全局抢占语义，取到任务后再按行内 `course_id` 继续。
- 错误统一返回 `{"detail": string | object}`；常见状态码见各端点。
- 分页：当前无分页端点，列表全量返回。

---

## 1. 系统 / 健康

### 1.0 `POST /api/v1/auth/login`
> 来源 `app/api/v1/auth.py`。前端登录页调用，token 存入 localStorage（`exam_auth`）。
```json
// 请求
{ "username": "teacher", "password": "***" }
// 响应
{ "token": "jwt", "user": { "id":"uuid","username":"teacher","name":"姓名","role":"teacher" } }
```
失败 401 `{"detail":"用户名或密码错误"}`。

### 1.0b `GET /api/v1/auth/me`
`Authorization: Bearer <token>` → 当前用户；未登录 / 登录失效 401。

### 1.1 `GET /api/v1/health`
健康检查（不依赖 DB 即可返回）。
```json
{ "api": "ok", "postgresql": "ok", "redis": "ok", "mineru": "configured", "llm": "configured" }
```
字段取值：`api`=`ok`；`postgresql`/`redis`=`ok|unavailable|not_configured`；`mineru`/`llm`=`configured|not_configured`。

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
需要 LLM 已配置，否则 503。

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

两种形态都会在顶层附带 `exam_rules`（考核大纲的考试规则），字段齐全（缺失即为空数组）：

```json
{ "exam_rules": {
    "exam_form": "闭卷笔试", "duration_minutes": 90, "total_score": 100,
    "question_type_ratios": [ {"question_type":"single_choice","ratio":20} ],
    "chapter_weights": [ {"anchor_key":"第1章 …","weight":5} ] } }
```

> `payload` 里持久化的字段名是 `final_exam_rules`，对外统一暴露为 `exam_rules`。
> 旧框架（本次改动前构建的）该字段是空 dict，接口会补齐成完整形态再返回。

### 4.8 修改考核规则
`PATCH /api/v1/courses/{course_id}/framework-versions/current/rules`
body 同 `exam_rules` 结构（`question_type_ratios` / `chapter_weights` 等）。
→ 200 `{ "status":"ok", "framework_version_id":"uuid", "exam_rules":{...} }`

归一化规则：题型名映射到英文枚举（"选择题"→`single_choice`）、剔除未知项、比例归一到 100、
未声明的章节锚点补 0；考纲完全没有章节权重表时返回空列表，由消费方回退到考点权重。

**消费**：蓝图在未下发 `type_rules` 时按 `question_type_ratios` 推导题型分布（题数折算后
定点修正，保证总分精确 100）；创建蓝图时 `chapter_weights` 优先取 `chapter_weights`。

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

> 进程重启/崩溃中断的 run（线程已消亡但行停在 `queued`/`running`）在读取时就地判为 `failed`（`error_code=interrupted_by_restart`），前端轮询下一拍即解卡；`latest` 同理。

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

## 6. 蓝图（传统/独立路由）Blueprints —— 已移除

> ⚠️ `app/api/v1/blueprints.py` 已删除。`/blueprints/allocate` 与 `/blueprints/confirm`
> 在前端改走「试卷项目」子端点后已无任何调用方；且它们接受客户端提交的
> `knowledge_cards` / `units`、不带 `course_id` 作用域校验，属于绕过服务层的旧入口。
> 合同分配与修订一律使用 §8.9–8.11（服务端从 DB 重建请求、课程作用域过滤）。

---

## 7. 生成 Generation（直接同步）—— 已移除

> ⚠️ `app/api/v1/generation.py` 已删除。`POST /generation-runs` 在请求线程里同步调用
> LLM，违反「长任务必须走 Celery + outbox」的架构纪律，且前端无调用方。
> 生成一律使用 §8.13 的异步 `generate` + §8.14 的 `task-runs` 轮询。

---

## 8. 试卷项目 Exam Projects（主流程）

> 来源 `app/api/v1/exam_projects.py`。前缀 `/api/v1/courses/{course_id}/exam-projects`
> ⚠️ **鉴权**：本节全部端点需请求头 `Authorization: Bearer <token>`（router 级
> `dependencies=[Depends(get_current_user)]`），缺失/失效返回 401。

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
`PATCH /api/v1/courses/{course_id}/exam-projects/plan-items/{plan_item_id}`
允许 key ∈ `score|question_type|difficulty|cognitive_level|exam_point_id|card_id`；其它 key 422。
题位必须属于路径上的 `{course_id}`，跨课程 id 按不存在处理（404），不会读写到别课程的题位。

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

### 8.12 读取当前合同（退出项目再进入时恢复）
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/contracts/current` → 200
尚未 confirm 过合同时返回 **404**（前端据此区分"还没有合同"与"已有合同"）。响应：
```json
{ "generation_run_id":"uuid",
  "contract_snapshot": { "slots":[ ...ContractSlot... ], "total_score":50.0,
    "conflicts":[], "audit_summary":{}, "centrality_threshold_used":0.6,
    "slot_revisions_applied":[], "conflicts_history":[] } }
```
说明：快照持久化在 `generation_runs.contract_snapshot`，由 `exam_projects.active_generation_run_id`
指向，与生成任务是否仍在进行无关。`total_score` 缺失时由服务端按槽位分值求和补齐；`conflicts`
已从落库位置 `conflicts_pre_vs_post` 归一化为顶层列表。这是"退出项目再进入后合同不消失"的权威数据源。

### 8.13 启动异步生成
`POST /api/v1/courses/{course_id}/exam-projects/{project_id}/generate` → **202**
body 可选 `{ "mock_graph": false }`；生产必须配置 LLM，否则 503。响应：`{ "task_run_id":"uuid" }`

### 8.14 任务运行详情
`GET /api/v1/courses/{course_id}/exam-projects/task-runs/{task_run_id}` → **200**
```json
{ "id":"uuid","course_id":"uuid","task_type":"string","status":"string","stage":"string","progress":0.0,"attempt":0,
  "payload":{},"result":{},"error_code":"string?","error_message":"string?",
  "created_at":"ISO8601","updated_at":"ISO8601","completed_at":"ISO8601?" }
```

---

## 9. 试卷版本 Paper Versions

> 来源 `app/api/v1/paper_versions.py`。前缀 `/api/v1/courses/{course_id}`
> 注意路径两种形态：`exam-projects/{project_id}/paper-versions/...` 与 `paper-versions/{pv_id}/...`。
>
> ⚠️ **鉴权**：本节全部端点（**含 §9.6–9.9 导出**）需请求头
> `Authorization: Bearer <token>`（router 级 `dependencies=[Depends(get_current_user)]`），
> 缺失或失效返回 401。token 禁止拼进 URL query；前端导出/整体预览不走裸 URL，
> 而是带鉴权拉取 Blob 后用 object URL 下载/内嵌（`frontend/src/api/domains/paperVersions.ts`
> 的 `fetchExport` + `src/api/http.ts` 的 `requestBlob`）。

### 9.1 当前试卷版本（按项目解析）
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-versions/current` → 200

当前版本解析规则（`paper_version_service.resolve_current_paper_version_id`，API 与项目摘要共用）：
1. `exam_projects.active_paper_version_id` —— 单一真值：生成完成时写入、定稿时重写，撤销定稿不清空；
2. 该指针为空（本规则落地前的遗留项目）时取 `version_no` 最大的一条。

项目无任何版本时返回 404 —— 即"尚未生成试卷"，与其它错误区分。

```json
{ "id":"uuid","exam_project_id":"uuid","generation_run_id":"uuid","version_no":1,
  "status":"candidate|finalized","total_score":100.0,
  "gen_run_id":"uuid","proj_id":"uuid","project_status":"review",
  "questions":[
    { "item_index":1,"plan_item_id":"uuid","knowledge_card_id":"uuid","exam_point_id":"uuid",
      "question_type":"","stem":"","options":{},"answer":"","explanation":"string|null",
      "subquestions":[],
      "score":2.0,"difficulty":"","cognitive_level":"",
      "needs_review":false,"needs_review_reason":"string|null",
      "teacher_override":{},"has_override":false,
      "finalized_text":{},"quality_audit":{} }
  ],
  "created_at":"ISO8601","confirmed_at":"ISO8601?","finalized_at":"ISO8601?" }
```

字段约定：

- `questions`（不是 `items`）为逐题数组，按题号升序；`item_index` 即 `paper_items.display_order`（题号，从 1 起），与 `PATCH /items/{item_index}` 同源。
- `score` 取 `plan_items.score`（合同同口径），逐题之和恒等于 `total_score`；`paper_versions` 表本身不存分值列。
- `exam_point_id` 以生成载荷盖章值为准，缺失时退回 `plan_items.exam_point_id`。
- `explanation` 由模型产出，部分题型（如单选）可能为 `null`，前端需对空值降级。
- `needs_review_reason` 为单数字符串（理由以 `；` 连接，截断至 200 字），不是数组。
- `teacher_override` 覆盖字段优先于生成载荷：`stem`/`options`/`answer`/`explanation`/`score` 可为教师手改值。
- `subquestions` 为综合题分问数组（`{prompt, score, answer, ...}`，各问分值之和等于本题总分），非综合题为 `[]`；
  导出渲染的分问排版与答题卡的分问作答区都依赖它，历史遗留题若缺失则按空数组降级。

### 9.2 待审核项
`GET /api/v1/courses/{course_id}/paper-versions/{pv_id}/needs-review` → 200
query 可选过滤：`item_index_min` / `item_index_max` / `question_type`
```json
[ { "item_index":1,"question_type":"","needs_review_reason":"","quality_message":"","exam_point_id":"uuid","card_id":"uuid" } ]
```

### 9.3 修改题目项
`PATCH /api/v1/courses/{course_id}/paper-versions/{pv_id}/items/{item_index}`
body：`{ "teacher_override_patch":{}, "clear_needs_review":false }`（仅这两 key）
：内容补丁按与手动新增同口径校验（题干/答案必填、多选答案须对应 ≥2 个选项），
违反返回 422；仅 `clear_needs_review`（无内容补丁）不触发校验。

覆写字段与题型口径：
- `answer` 对**判断题**是布尔值（`true`/`false`），其余题型为字符串；前端保存时会
  把「正确/错误」规范化回布尔值。
- `answer` 也兼容选项字母（`B`/`ABD`）与选项原文两种形态，由 `answer_option_keys` 统一解析；
  单选题答案必须唯一对应一个选项，否则生成侧判 blocker（历史上曾出现"单选题多个正确项"）。

### 9.3b 调整题目顺序
`PUT /api/v1/courses/{course_id}/paper-versions/{pv_id}/items/reorder`
body：`{ "ordered_indices":[3,1,2] }`（新顺序，须恰好包含当前全部题号且不重复）
→ `{ "status":"ok","item_count":n }`；`display_order` 重写为 1..N。

### 9.3c 新增教师自拟题目
`POST /api/v1/courses/{course_id}/paper-versions/{pv_id}/items`
body：`{ "stem":"","question_type":"short_answer","options":[],"answer":"","explanation":"","score":5,"difficulty":"medium","rubric":"" }`
→ 201，返回刷新后的完整试卷。

**题干与答案必填**（422）：卷面里不允许出现无答案的题——否则答卷与答案细则导出就是空白。
判断题答案传布尔值；多选题答案须对应两个及以上选项（字母如 `AB` 或选项原文）。
主观题（简答/综合）可带 `rubric`（评分细则，每行一个要点）：AI 生成提案回填的
`rubric` 即由此字段落库，GET questions（§9.2）与答案细则 JSON（§9.6）均带出；
编辑时也可经 §9.3 的 `teacher_override_patch.rubric` 覆写。

### 9.3d 删除题目
`DELETE /api/v1/courses/{course_id}/paper-versions/{pv_id}/items/{item_index}`
→ 200；其后题目的 `display_order` 自动前移 1。

### 9.3e 单题 AI 改题（提案，需教师确认）
`POST /api/v1/courses/{course_id}/paper-versions/{pv_id}/items/{item_index}/ai-revise`
body：`{ "instruction":"让四个选项表述更平行" }`（`instruction` 必填非空）
→ **202** `{ "task_run_id":"uuid" }`。LLM 未配置 503；试卷不存在/题号越界 404；已定稿 409；空要求 422。

- **只产提案，不写试卷数据**：worker（`task_type=ai_revise_item`，租约 300s）以合同槽位
  （`coverage_atom`/`answer_boundary`/`forbidden_context`）+ 知识卡为约束调模型；提案必须过
  `validate_generated_question` 收口——未过则带反馈纠错 1 次，仍不过如实上报（比例/难度/去重
  等约束由确定性校验兜底，不进 prompt）。
- 提案存 `task_runs.result`：`{ item_index, instruction, current, proposal, change_summary,
  validation:{passed,code,message}, attempts }`；用 §8 的 `GET /exam-projects/task-runs/{id}` 轮询，
  `succeeded` 后取 `result`。
- AI 只许改 `stem/options/answer/explanation`；题型/分值/难度/认知层级/考查原子由合同锁定不给改。
- `validation.passed=false` 时前端禁用确认；教师确认后由前端调 §9.3 PATCH（`teacher_override_patch`
  只传变更字段）落库——与手动改题同一条写路径，教师可继续手动改回（原题分层保留在 `payload`）。
- 幂等：同题同要求的**在途**任务复用同一 `task_run_id`；已到终态则换新键真正重新生成。

### 9.3f AI 生成整道新题（提案，需教师确认）
`POST /api/v1/courses/{course_id}/paper-versions/{pv_id}/items/ai-generate`
body：`{ "instruction":"出一道单选题，考查进程与线程的区别" }`（`instruction` 必填非空）
→ **202** `{ "task_run_id":"uuid" }`。LLM 未配置 503；试卷不存在/不在课程 404；已定稿 409；空要求 422。

- **只产提案，不写试卷数据**：worker（`task_type=ai_create_item`，租约 300s）以该试卷
  现有题目清单（题号/题型/题干前 40 字，防重复的素材约束）+ 知识卡（generation_run
  关联过才带，拿不到则纯指令生成）为 grounding 调模型；提案必须过
  `validate_generated_question` 收口——未过则带反馈纠错 1 次，仍不过如实上报
  （比例/难度/合同去重等全局约束由确定性算法兜底，不进 prompt）。
- **一期题型白名单**：`single_choice / multiple_choice / true_false / fill_blank /
  short_answer`；综合题与论述题不支持整题生成（`validation.code=question_type_unsupported`）。
- 提案存 `task_runs.result`：`{ instruction, proposal:{ question_type, stem, options,
  answer, explanation, difficulty, rubric }, change_summary, validation:{passed,code,message},
  attempts }`；用 §8 的 `GET /exam-projects/task-runs/{id}` 轮询，`succeeded` 后取 `result`。
- **回填而非直写**：前端把 proposal 填进「新增题目」表单（QuestionEditor，含 `rubric`
  评分细则字段），教师微调（含分值——`score` 不进提案，由教师自己定）后走 §9.3c 既有
  POST items 落库，内部 `_validate_teacher_item` 再把一次关；`rubric` 随题落库，
  读取（§9.2）与导出（§9.6）全链带出。
- 幂等：同卷同指令的**在途**任务复用同一 `task_run_id`；已到终态则换新键真正重新生成。
- 键 = `sha256(f"ai-create:{paper_version_id}:{instruction}")[:24]`。

### 9.3g 合同槽位 AI 解释与调整建议（只读，异步）
`POST /api/v1/courses/{course_id}/exam-projects/{project_id}/contract-slots/{item_index}/explain`
body 可选 `{ "allocation_seed":0, "blueprint_version_id":"uuid", "instruction":"为什么不是另一个原子？" }`
（均缺省可用；`instruction` 可为空串 = 标准解释）→ **202** `{ "task_run_id":"uuid" }`。
LLM 未配置 503；项目不存在/不在课程 404；项目尚无蓝图 409；`item_index` 不在槽位内或 body
含未知键/`instruction` 非字符串 422。

- **纯只读、零写路径**：端点只建 `task_runs`（`task_type=explain_contract_slot`，租约 300s），
  不碰合同/蓝图任何表；上下文用 `allocate_with_fallback` 同路只读重算（与 §9.2 revise 预览同源），
  把槽位字段、知识卡、蓝图题位计划、章节权重、其余槽位概览喂给模型——prompt 不让模型重做分配，
  解释必须落在确定性算法的真实输出上（红线：比例/难度/去重不进 prompt）。
- 建议只引导两条既有落地路径：①合同修订（先 §9.2 `PATCH contracts/revise` 预览、再
  `POST contracts/confirm` 落库）；②换分配方案（`allocation_seed`）或调整蓝图后重新分配——
  不建议教师手改分值/难度去凑比例。`target_item_index` 不在已知槽位号的一律归 `null`。
- 结果存 `task_runs.result`：`{ project_id, item_index, instruction, explanation, suggestions:
  [{concern, suggestion, target_item_index}], instruction_response, validated }`；用 §8 的
  `GET /exam-projects/task-runs/{id}` 轮询，`succeeded` 后取 `result`。
- 幂等：同槽位同方案同追问的**在途**任务复用同一 `task_run_id`；已到终态则换新键重新生成。
- 键 = `sha256(f"explain:{project_id}:{item_index}:{seed}:{instruction}")[:24]`。

### 9.3h 整卷 AI 质量评审（只读报告，异步）
`POST /api/v1/courses/{course_id}/paper-versions/{pv_id}/ai-review`
body 可选 `{ "instruction":"重点关注难度分布" }`（教师指定关注点；可空 = 标准评审）
→ **202** `{ "task_run_id":"uuid" }`。LLM 未配置 503；试卷不存在/不在课程 404；试卷无题目
422（detail「试卷没有题目…」）；body 含未知键或 `instruction` 非字符串 422。

- **纯只读、零写路径**：端点只建 `task_runs`（`task_type=review_paper_version`，租约 300s），
  不碰试卷/合同/蓝图任何表（不改 `paper_items`，不动既有 PATCH/confirm/导出端点）。
- **无状态禁令**：不做 409——报告是只读的，`finalized` 定稿卷照样可评审（这正是它的价值），
  readonly 不限制它。
- **定位 = 试卷稿质量评审，不是学生答卷评分**（范围红线 3：在线阅卷明确不做）：system prompt
  硬规则禁止输出学生分数、评分建议、给答卷打分的任何内容；报告只针对试卷稿本身。
- **确定性 grounding，模型只解读不重算**：prompt 只喂真实数据——题目全量
  （item_index/题型/难度/分值/题干/选项/答案/解析/needs_review，按 `get_paper_version`
  生效题面口径）、`list_needs_review` 待审核清单、合同终检 `audit_paper_against_contract`
  的真实 checks（**调用既有函数**，不把终检逻辑抄进 prompt；无合同快照则 `final_check=null`，
  模型须如实标注「合同终检不可用」，不伪造结果）、蓝图/卷面难度与题型配额计数
  （generation_run 可读就读，读不到 `plan=null` 跳过）。引用题目必须带真实 `item_index`
  （规整时越界的剔除）。所有查询带 `course_id` 过滤。
- 报告维度枚举固定 5 类：`难度分布 / 题面表述 / 答案与解析一致性 / 覆盖与配额 / 风险题`
  （模型可只给其中若干类，越界维度丢弃）；建议只引导试卷页既有功能（手动编辑、单题 AI 改题、
  AI 生成新题、重新生成、确认定稿），不得输出绕过确定性约束（比例/难度/去重/答案互斥）的改法。
- 结果存 `task_runs.result`：`{ paper_version_id, instruction, verdict:"pass|attention",
  summary, sections:[{dimension, severity:"info|warn", finding, suggestion, item_indexes}],
  deterministic:{needs_review_count, final_check_available}, validated }`；用 §8 的
  `GET /exam-projects/task-runs/{id}` 轮询，`succeeded` 后取 `result`。
- 校验收口：`summary` ≥10 字、`sections` 非空且每节 `finding` 非空、`verdict` 合法——未过则带
  `previous_validation_error` 纠错 1 次，仍不过如实上报 `validated=false`。
- 幂等：同卷同关注点的**在途**任务复用同一 `task_run_id`；已到终态则换新键重新评审。
- 键 = `sha256(f"review:{paper_version_id}:{instruction}")[:24]`。

### 9.4 确认试卷版本
`POST /api/v1/courses/{course_id}/paper-versions/{pv_id}/confirm`
body 可选 `{ "force_ignore_needs_review":false }`。有未审核项返回 409（detail 含 `item_indices`）。

### 9.5 回退到候选
`POST /api/v1/courses/{course_id}/paper-versions/{pv_id}/revert`

### 9.6 导出：答案细则 JSON
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-versions/{pv_id}/export/json`
→ 附件下载（`Content-Disposition: attachment; filename="answer_detail_v{n}.json"`）。
需 `Authorization: Bearer <token>`；401 时不会下发文件。
含 `missing_answer_count` 与逐题 `answer_missing` 标记；`stem` 已剥离题干自带的编号/分值前缀。
逐题带 `rubric`（主观题评分细则：要点数组或文本，无则 `null`），schema 自 `1.1.0` 起新增该字段。

### 9.7 导出：学生卷 HTML
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-versions/{pv_id}/export/student`
需 `Authorization: Bearer <token>`（§9.7–9.9 三份 HTML 导出同规则）。
→ `text/html`（无答案，可打印 PDF）。正式卷面：信息头（课程名称/总分/题量，考试时间/形式/
试卷类型/学分留空待填）+ 题次表 + 按题型分节（一、单选题（共N题，每题X分，共Y分））+ 连续题号。

卷面细节（对齐命题范本）：

- 节标题带去向提示「（将答案写在答题纸上）」，判断题为「（将答案写在答题纸上，对的打钩 √ ，错的打叉 ×）」；
  答案实际落在 §9.9 答题卡上。
- 单选/多选题干末尾补作答括号 `（  ）`，判断题补 `（ ）`；题干已自带括号时不重复补。
- 综合题按 `subquestions` 渲染分问：`（1）题面（6分）`；题干里的 ``` 围栏切成等宽 `<pre class="code">` 代码块。
- 不打印难度/分值元信息行（难度属内部信息；分值由节标题与分问标注表达）。该行仅答卷保留。

### 9.8 导出：答卷（含答案）HTML
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-versions/{pv_id}/export/answer-key`
→ `text/html`（含答案）。在学生卷版式基础上加装订线、客观题答案速查表（题号|答案），
答案选项打 ✓ 标绿；缺答案标注【缺答案·需人工补充】。综合题额外给出**逐问答案**
（`subquestions[].answer`），并保留难度/分值元信息行供阅卷参考。

### 9.9 导出：答题卡 HTML
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-versions/{pv_id}/export/answer-card`
→ `text/html`（空白作答卷，可打印 PDF）。与学生卷配套：只承接作答，**不出题面、不含答案**。

- 考生信息栏（学号/姓名/考场/座位号/专业名称）置于信息头与题次表之间，底部不再重复学号栏。
- 客观题（单选/多选/判断）→ 题号表格 + 空白答案格，每 10 题换行防溢出。
- 其余题型 → 逐题「题号 + 分值 + 作答横线」，行数按分值取 2~8 行。
- 综合题 → 按 `subquestions` 逐问给出「（1）题面（4分）」+ 独立作答横线。
- 带三道装订线。

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