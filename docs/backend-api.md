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
转 `ready` 时入队 `task_runs(material_index)`（幂等键 `material_index:{run_id}`，
transactional outbox 派发，见 §10.5）——嵌入只在 worker 执行，端点只入队。

### 3.6 轮询解析
`POST /api/v1/courses/{course_id}/materials/{material_id}/parse/poll` → 推进一次解析状态机，前端周期调用直至 `ready` / `failed`。返回解析状态对象。
落块转 `ready` 同样触发 §3.5 的 `material_index` 入队（重复轮询 ready 幂等，不重复入队）。

### 3.7 修改资料类型
`PATCH /api/v1/courses/{course_id}/materials/{material_id}/type?material_type=exercise` → `MaterialResponse`；非法类型 422。

### 3.8 删除资料
`DELETE /api/v1/courses/{course_id}/materials/{material_id}` → **204**（无 body）

### 3.9 「试卷」文件夹（试卷归档，资料库第三分区）
> 来源 `app/api/v1/paper_archives.py`（服务层 `app/services/paper_archive_service.py`）。
> 归档是**独立副本**：存 `get_paper_version` 的解析快照，与 `paper_versions` **无外键牵连**，
> 因此不受 §9.1b「每项目只留最近 3 份」的保留策略影响——源卷被物理删除后归档仍可看/编辑/下载。
> 资料库页该分区**不接解析与索引**（无 material_type，不入 `material_index` 任务）。

`POST /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-archives` → **201**
body：`{ "source_paper_version_id":"uuid|null", "name":"string|null" }`
：不给 `source` 存项目当前卷（§9.1 解析规则），不给 `name` 用「`{项目名} v{version_no}`」。
源卷不属于该项目 422；项目不存在 404。

`GET /api/v1/courses/{course_id}/paper-archives` → **200**（新 → 旧）
```json
[ { "id":"uuid","exam_project_id":"uuid","project_name":"期末卷",
    "source_paper_version_id":"uuid|null","source_version_no":1,
    "name":"期末卷 v1","item_count":20,"total_score":100.0,
    "created_by":"uuid|null","created_at":"ISO8601","updated_at":"ISO8601" } ]
```
列表**不带** `snapshot`/`questions`（避免整卷载荷），详情才带。

`GET /api/v1/courses/{course_id}/paper-archives/{archive_id}` → 200
：档案字段 + `snapshot`（`{version_no, status, total_score, questions}`）+ 顶层 `questions`
（与 §9.1 同构的逐题数组，已合并 `teacher_override`，供预览/编辑/导出直接消费）。

`DELETE /api/v1/courses/{course_id}/paper-archives/{archive_id}` → **204**（真删，非软删）；
不存在 404。课程隔离：跨课程按 404 处理（`where course_id=…` 过滤）。

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
    "chapter_weights": [ {"anchor_key":"第1章 …","weight":5} ],
    "assessment_focus": [ {"assessment_mode":"conceptual","weight":60} ] } }
```

> `payload` 里持久化的字段名是 `final_exam_rules`，对外统一暴露为 `exam_rules`。
> 旧框架（本次改动前构建的）该字段是空 dict，接口会补齐成完整形态再返回。
> `assessment_focus` 为空数组 = 均衡（蓝图按题型默认分布）。

### 4.8 修改考核规则
`PATCH /api/v1/courses/{course_id}/framework-versions/current/rules`
body 同 `exam_rules` 结构（`question_type_ratios` / `chapter_weights` /
`assessment_focus` 考试侧重点 / `difficulty_distribution` 难度比例 / `type_formats`
题型格式覆盖等）。
→ 200 `{ "status":"ok", "framework_version_id":"uuid", "exam_rules":{...} }`

**条件键的保留/清空语义**（`type_formats` 与 `difficulty_distribution` 同约定）：
请求**未携带**（缺 key 或 `null`）= 保留现值——旧前端/AI 提案不带新字段时不能把已设置的
格式与难度抹掉；传 **`{}` = 显式清空**（难度回退「未声明」缺省 = 蓝图全 `medium`，
格式恢复类别/全局默认）。

归一化规则：题型名映射到英文枚举（"选择题"→`single_choice`）、剔除未知项、比例归一到 100、
未声明的章节锚点补 0；考纲完全没有章节权重表时返回空列表，由消费方回退到考点权重。
`assessment_focus` 是教师声明的**考试侧重点**：五项 `assessment_mode`
（`theory_recall` / `conceptual` / `application` / `problem_solving` /
`practical_operation`）的 `%` 权重，归一到 100、零值剔除、空数组 = 均衡。
`difficulty_distribution` 是**全卷难度比例** `{low, medium, high}`（`%` 数值，归一到 100；
非法条目剔除后对其余项缩放——与题型比例丢未知项同口径；剔后为空则省略该键 = 未声明）。

**消费**：蓝图在未下发 `type_rules` 时按 `question_type_ratios` 推导题型分布（题数折算后
定点修正，保证总分精确 100）；创建蓝图时 `chapter_weights` 优先取 `chapter_weights`。
`assessment_focus` 由蓝图构造**确定性**折算为题位 `assessment_mode` 分布（按权重分配，
两层收敛兜底：无实操可考单元时先降级到邻近考查方式、再按可考性归并）——权重异常/无可考
单元只会让分布收敛，不会让出卷失败。
`difficulty_distribution` 在**创建蓝图时逐题型注入**：每个题型各自按同一比例用最大余数法
把比例落成题数（如单选 10 题 × 20/60/20 → 2/6/2），题型规则里显式下发的难度永远优先；
蓝图把难度拷进 `plan_items.difficulty` → 合同槽位继承 → 成卷三层一致。**已确认/冻结的
蓝图不受改规则影响**，改完要新建蓝图版本并确认才生效；未声明难度的课程沿用历史缺省
（全 `medium`）。

### 4.9 考核规则 AI 助手（提案，需教师保存）
`POST /api/v1/courses/{course_id}/framework-versions/current/rules/ai-propose`
body：`{ "instruction":"闭卷笔试90分钟，选择题40%，第3章多考一些，侧重实操" }`
（`instruction` 必填非空）→ **202** `{ "task_run_id":"uuid" }`。
LLM 未配置 503；无命题框架 404（detail 含「命题框架」）；空要求 422。

- **只产提案，不写规则**：worker（`task_type=propose_exam_rules`，租约 300s）把当前
  `exam_rules`、允许题型、章节锚点作事实数据喂给模型（比例/难度等约束检查在代码里，不
  进 prompt 让模型自觉遵守）；提案整包过既有 `normalize_exam_rules` 归一（英文枚举、
  比例与侧重点归一 100、难度 `{low,medium,high}` 归一且未提及就照抄当前/未声明则省略键、
  锚点按已知过滤），模型未提及的字段照抄当前规则，防止清空教师已有设置。
- 校验收口：`ratios_empty`（题型比例为空）、`fields_lost`（提案清空教师已有的章节权重 /
  考试形式 / 时长 / 总分）——未过带 `previous_validation_error` 纠错 1 次，仍不过如实报错。
- 结果存 `task_runs.result`：`{ course_id, instruction, proposal:{ exam_form,
  duration_minutes, total_score, question_type_ratios, chapter_weights,
  assessment_focus, difficulty_distribution }, explanation }`；用 §8.14 `GET /exam-projects/task-runs/{id}` 轮询，
  `succeeded` 后前端把 proposal 回填考核规则卡**编辑草稿**，教师核对/修改后点「保存」走
  §4.8 PATCH 落库——AI 提案不直接生效。
- 幂等：同课同要求的**在途**任务复用同一 `task_run_id`；已到终态换新键重新提案。
- 键 = `sha256(f"propose:{course_id}:{instruction}")[:24]`。

### 4.10 框架候选 AI 评审（只读报告，异步）
`POST /api/v1/courses/{course_id}/framework-versions/current/ai-review`
body 可选 `{ "instruction":"重点看权重与覆盖" }`（可空 = 常规评审）
→ **202** `{ "task_run_id":"uuid" }`。LLM 未配置 503；无命题框架 404；其它业务错误 422。

- **纯只读、零写路径**：worker（`task_type=review_framework_candidate`，租约 300s）只建
  `task_runs`，不碰框架/冲突任何表。目标版本**候选优先**（教师正决定发不发布，与 §4.7 的
  published 优先相反），无候选则取最新已发布版。
- **确定性 grounding，模型只解读不重算**：锚点/考点瘦身、考试规则归一、待裁决冲突读表的
  当前状态（不是 payload 快照），覆盖/权重/认知分层统计（`anchor_count`、`point_count`、
  `points_per_anchor`、`anchor_weight_sum`、`cognitive_coverage`、允许题型并集）由后端算好
  作为事实数据进 prompt。
- 结果存 `task_runs.result`：`{ course_id, framework_version_id, framework_status,
  instruction, verdict:"ready|revise_first", summary, findings:[{severity:
  "info|warning|critical", area:"coverage|weight|question_type|cognitive|rules|conflicts|
  other", message, suggestion}] }`。归一收口：verdict/severity 别名归一、未知 area 归
  `other`、短 message 丢弃、去重、限 12 条；校验 verdict 词表 + `summary` ≥10 字，未过带
  `previous_validation_error` 纠错 1 次，仍不过如实报错。
- 用 §8.14 轮询取 `result`；前端 `FrameworkReviewPanel` 把报告展示在**确认按钮之前**，
  verdict 只是给教师的参考意见——确认/拒绝与冲突裁决仍走既有 §4.5 / §4.6 确认流。
- 幂等：同课同版本同要求的**在途**任务复用同一 `task_run_id`；已到终态换新键。
- 键 = `sha256(f"review:{course_id}:{framework_version_id}:{instruction}")[:24]`（含版本
  id：重建框架后即使要求相同也拿到针对新候选的评审）。

---

## 5. 知识目录 Knowledge

> 来源 `app/api/v1/knowledge.py`。前缀 `/api/v1/courses/{course_id}`

### 5.1 创建知识组织运行（异步）
`POST /api/v1/courses/{course_id}/organization-runs` → **202**
```json
{ "material_version_ids": ["uuid", "..."] }
```
响应：`{ "run_id": "uuid", "candidate_id": null, "status": "queued" }`
（run 行由后台线程在耗时阶段前先落库（`status=running`）并推进到
`awaiting_teacher_confirmation`；HTTP 立即返回 `queued` 只是受理回执，
后续一律以轮询 §5.2 的状态为准。）

### 5.1b 最近一次运行
`GET /api/v1/courses/{course_id}/organization-runs/latest` → 运行记录对象（附 `run_id`）
无任何 run 时 404。与 §5.2 同样经过孤儿自愈（见下）。

### 5.2 运行详情
`GET /api/v1/courses/{course_id}/organization-runs/{run_id}` → 运行记录对象

> 进程重启/崩溃中断的 run（线程已消亡但行停在 `queued`/`running`）在读取时就地判为 `failed`（`error_code=interrupted_by_restart`），前端轮询下一拍即解卡；`latest` 同理。
> run 状态机：`running → awaiting_teacher_confirmation → published | rejected`；任一阶段失败 → `failed`（带 `error_code`/`error_message`）。发布新版本后旧版本 `superseded`。

### 5.3 候选项
`GET /api/v1/courses/{course_id}/organization-runs/{run_id}/candidate` → 候选对象

### 5.3b 补料推荐（AI 预选，需教师确认）
`POST /api/v1/courses/{course_id}/organization-runs/{run_id}/supplement-recommendations` → 200
body：`{ "exam_point_code": "string" }`
对覆盖不足的考点，AI 从间接证据里预选可改判为直接证据的条目。模型只做建议，
改判仍由教师在发布确认时提交，不绕过确认流；推荐失败降级为 `recommended: []`（200），
不阻塞手动补证据。考点不在候选中 404；覆盖已充足时返回空推荐 + `note`。

### 5.4 发布知识树
`POST /api/v1/courses/{course_id}/organization-runs/{run_id}/publish` → 200
```json
{ "operations":[ {"operation":"string","target_code":"string","value":"string?"} ],
  "reviewed_topic_codes":["string"], "reviewed_exam_point_codes":["string"], "teacher_exclusions":["string"] }
```
返回发布结果；冲突 409。

### 5.4b 拒绝候选知识树
`POST /api/v1/courses/{course_id}/organization-runs/{run_id}/reject` → 200
把候选版本与 run 一并标记为 `rejected`（终态，不可再 publish）。
仅限仍处于待确认态的候选，否则 409（`no longer awaiting confirmation`）。

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

### 8.4b 删除项目
`DELETE /api/v1/courses/{course_id}/exam-projects/{project_id}` → **204**（无 body）
级联删除项目及其全部派生数据（蓝图 / 题位 / 生成运行 / 题目 / 试卷版本）；不存在 404。

### 8.5 创建蓝图
`POST /api/v1/courses/{course_id}/exam-projects/{project_id}/blueprints` → **201**
```json
{ "framework_version_id":"uuid", "catalog_version_id":"uuid",
  "type_rules":{}, "chapter_weights":{},
  "units":[ {...UnitCoverage...} ],
  "card_semantic_profiles":{}, "card_question_types":{} }
```
服务端缺省注入：`type_rules`/`chapter_weights` 未显式下发时按**当前考核规则**补齐——题型
比例推导题型题数（总分精确闭合）、章节权重取规则声明值、`assessment_focus` 与
`difficulty_distribution` 逐题型确定性落位到题位（难度为全卷一份比例，每个题型各自配比；
显式下发的 type_rules 永远优先——语义详见 §4.8「消费」）。
响应：`{ "blueprint_version_id":"uuid", "plan":[ ...PlanItem... ] }`
PlanItem（`list_plan_items` 响应，8.6 同构）：
`{ "id":"","item_index":0,"question_type":"","score":0.0,"difficulty":"","cognitive_level":"","assessment_mode":"","exam_point_id":"","exam_point_title":"","exam_point_code":"","anchor_key":"","knowledge_card_id":"","knowledge_card_name":"","assessment_unit_id":"","assessment_unit_title":"","section_index":null }`
其中 `exam_point_title`/`exam_point_code`/`anchor_key` 为**读时解析的展示名与章节**：蓝图常钉在已被取代的框架/目录上，当前已发布目录里查不到这些 id，后端按蓝图自身的 `framework_version_id` 从 `exam_points` 旧行取名（id 精确匹配，历史 code 口径在该框架内兜底），解析不到为 `null`。
缺 key 422；引用不存在 404。

### 8.6 当前蓝图计划项
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/blueprints/current/plan-items` → `list[PlanItem]`

### 8.7 修改计划项
`PATCH /api/v1/courses/{course_id}/exam-projects/plan-items/{plan_item_id}`
允许 key ∈ `score|question_type|difficulty|cognitive_level|exam_point_id|card_id`；其它 key 422。
题位必须属于路径上的 `{course_id}`，跨课程 id 按不存在处理（404），不会读写到别课程的题位。
仅 `draft` 蓝图可原地修改：已确认（confirmed/superseded）蓝图返回 **409**（冻结纪律——其难度/分值已被合同槽位拷贝，只能新建蓝图版本）。

### 8.7b 蓝图题位 AI 调整建议（提案，需教师逐条应用）
`POST /api/v1/courses/{course_id}/exam-projects/{project_id}/blueprints/current/ai-suggest`
body 可选 `{ "instruction":"难度整体压低一点" }`（可空 = 常规建议）
→ **202** `{ "task_run_id":"uuid" }`。
LLM 未配置 503；项目不存在/不在课程 404；蓝图非 draft（已确认不可原地修改）409；蓝图无题位 422。

- **只产建议，不改题位**：worker（`task_type=suggest_blueprint_adjustments`，租约 300s）
  把题位全量 + 后端算好的**确定性对照**（题型分值期望差 = 规则比例×总分 vs 实际、难度/
  认知/章节分布、考试规则、允许题型、已知考点/知识卡集合）作事实数据进 prompt——比例与
  总分约束在代码里校验：**score 类建议的分值增减合计必须为 0**（调分只许挪动不许改总分），
  未过带 `previous_validation_error` 纠错 1 次，仍不过如实报错。
- **整卷难度目标（2026-09-28 反馈「我要求整体调整，只给了 3 道」）**：教师指令里的难度比例
  （「按5简单3中等2难」「50%简单30%中等20%难」「难度分布按5:3:2」）由后端**确定性换算**为
  `difficulty_target = { ratio, scope, target_counts, current_counts, gap }`（目标题数按题位数 ×
  比例取整、最大余数法配平；冒号式要求比例紧邻难度语境，章节比例不误触；解析不到则为 null
  不设门禁）随 payload 进 prompt——模型照 `gap` 点题位、不自己算比例；第三条**达标门禁**：
  模拟应用全部 difficulty 建议后的各档题数必须**恰好等于** `target_counts`，否则打回带反馈
  纠错 1 次——「清单必须给全、不许只回示范条目」由代码判定，不靠模型自觉。
- **逐题型难度目标（2026-09-28 反馈「每个题型按5:3:2 却答全卷匹配」）**：指令带
  「每个题型/各题型/按题型」等范围词时粒度切到逐题型（`scope="question_type"`，范围词仅在
  难度比例已解析时生效）：`by_type = { 题型: { count, target_counts, current_counts, gap } }`
  按题型分组各自最大余数法配平，顶层 `target_counts` = **各题型之和**（两层目标数学一致，
  门禁不打架）。门禁逐题型分别判定：**全卷 counts 恰好等于顶层目标也照打回**（实测反例——
  全卷 21:13:8 恰为 5:3:2，判断题却 18 易 1 中 1 难），空清单/「无需调整」在目标未达成时同样
  打回带反馈；此时只准调 difficulty、改 question_type 会打回（题型占比不在该指令范围内）。
- 归一收口：题号不在册丢弃；词表 coerce（难度 `easy→low` / `hard→high` 别名、中文题型名
  canonical 到英文枚举）；`exam_point_id` / `card_id` 限定已知集合；去重、去 no-op、去空理由；
  每条附 `from_value`（**提案时原值快照**）——教师应用后题位已是新值，面板仍显示
  「原值 → 新值」，不漂移成「易→易」。
- 结果存 `task_runs.result`：`{ course_id, project_id, instruction, summary,
  suggestions:[{ item_index, field, value, from_value, reason }], total_score }`，其中 `field` ∈
  §8.7 的允许 key（`assessment_mode` 不可由 AI 改——由 §4.8 侧重点确定性折算）。
  用 §8.14 轮询，`succeeded` 后取 `result`。
- **应用走既有端点**：前端 `BlueprintSuggestPanel` 逐条/全部「应用」= 对每条建议调 §8.7
  `PATCH plan-items/{id}`（分值 0.5 步进、draft 冻结、课程隔离等服务端校验原样生效），
  AI 不直接写题位。全部落地后清单**自动折叠**为一行状态条（「已应用 x/y · 展开查看」，
  可随时展开回看；不销毁——已应用清单带 `from_value` 是改动审计记录，重新生成才会再调模型），
  部分失败不收起，教师能看见漏网条目。
- 幂等：同课同项目同要求的**在途**任务复用同一 `task_run_id`；已到终态换新键。
- 键 = `sha256(f"suggest:{course_id}:{project_id}:{instruction}")[:24]`。

**读回（恢复）**：`GET .../blueprints/current/ai-suggest/latest` → **200**
`{ "task_run": {...§8.14 形状...} | null }`。
`task_run: null` = 当前蓝图版本下尚无建议历史（或已因蓝图重建作废）——「没有历史」是首次
访问的常态，按 200 而非 404 返回，前端静默跳过恢复。返回本项目**最近一次**建议任务：
- 只认当前蓝图版本创建之后的任务：蓝图重建只追加新版本，旧建议针对旧题位快照，恢复
  出来会把过期提案盖到新题位上，按 `created_at` 丢弃（比较在 Python 侧做——SQLite 的
  `func.now()` 存储无微秒，同秒字符串比较会误排除）。
- 在途任务原样返回（前端续 §8.14 轮询跑完照常出清单）；`succeeded` 返回 `result`
  （面板恢复清单与已应用状态，全部已落地则恢复即折叠）；failed/cancelled 前端静默忽略。
- 蓝图已确认（冻结）**仍可读回**——只读恢复不吃 draft 门禁，留作改动审计。
- 项目不存在/不在课程 404；课程隔离由 `course_id` 过滤兜底，项目过滤按
  `payload.project_id` 在 Python 侧做。

### 8.8 确认蓝图
`POST /api/v1/courses/{course_id}/exam-projects/{project_id}/blueprints/current/confirm`
body 可选 `{ "blueprint_version_id": "uuid" }`。返回确认结果。

### 8.9 分配合同
`POST /api/v1/courses/{course_id}/exam-projects/{project_id}/contracts/allocate`
body 可选 `{ "blueprint_version_id":"uuid", "allocation_seed":123 }`。响应：
```json
{ "used_threshold": 0.6, "conflicts_history": [["string"]], "contract_snapshot": { ...PaperContract... } }
```
响应快照的 `slots[]`、`conflicts[]`、`audit_summary.backfilled_points[]` 附带读时补齐的展示名
（`exam_point_title`、槽位另有 `card_name`，backfilled 另有 `from/to_exam_point_title`），
按蓝图的框架/目录版本从 `exam_points`/`knowledge_cards` 旧行按 id 取名——目录重建后旧 id
在当前已发布目录里查不到，前端名字映射必然落空。只增强响应，不回写任何落库快照。

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
与 8.9 同理，`slots[]`/`conflicts[]`/`backfilled_points[]` 附带读时补齐的
`exam_point_title`/`card_name` 等展示名（不回写冻结快照）。

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

- `questions`（不是 `items`）为逐题数组，按卷面顺序；`item_index` 即 `paper_items.display_order`（全局稳定序号，从 1 起），与 `PATCH /items/{item_index}` 同源。
  **卷面展示题号每种题型从 1 重新计数**（前端与全部导出同口径，见 §9.7）；`item_index` 只作内部 id，不直接上卷面。
- `score` 取 `plan_items.score`（合同同口径），逐题之和恒等于 `total_score`；`paper_versions` 表本身不存分值列。
- `exam_point_id` 以生成载荷盖章值为准，缺失时退回 `plan_items.exam_point_id`。
- `explanation` 由模型产出，部分题型（如单选）可能为 `null`，前端需对空值降级。
- `needs_review_reason` 为单数字符串（理由以 `；` 连接，截断至 200 字），不是数组。
- `teacher_override` 覆盖字段优先于生成载荷：`stem`/`options`/`answer`/`explanation`/`score` 可为教师手改值。
- `subquestions` 为综合题分问数组（`{prompt, score, answer, ...}`，各问分值之和等于本题总分），非综合题为 `[]`；
  学生卷/答卷的分问排版依赖它（答题卡按范本只给整页空白大框、不分问），历史遗留题若缺失则按空数组降级。

### 9.1b 试卷历史（读取即执行「只留最近 3 份」）
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-versions` → **200**（新 → 旧，≤3 条）
```json
[ { "id":"uuid","version_no":4,"status":"candidate","item_count":20,
    "is_current":true,"created_at":"ISO8601",
    "confirmed_at":"ISO8601?","finalized_at":"ISO8601?" } ]
```

- **保留策略** `PAPER_VERSION_HISTORY_LIMIT = 3`：超出的最旧版本**物理删除**，不是置状态、不是软删。
  两处触发：① 生成新卷时在 `create_paper_version_from_generation` 内**同事务**修剪（删除失败整次生成回滚）；
  ② 本端点读取时修剪（清掉规则落地前的存量超量卷）。
- **删除深度**（按外键自底向上）：`model_calls`（`details.response` 存着完整题面，不删等于旧卷还在库里）
  → `quality_checks` → `paper_items` → `generated_questions` → `paper_versions`。
  `generation_runs` / `generation_attempts` **保留**：只有状态与耗时，不含题面，删掉会让运行日志引用悬空。
- **两条豁免**：被删卷的 `generated_questions` 若仍被**保留卷**的 `paper_items` 挂着则不删；
  当前卷指针若指向将删卷，先改指最新保留卷（绝不留悬空外键）。
- `is_current` 按 `exam_projects.active_paper_version_id` 标记。

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

### 9.3g 整卷 AI 质量评审（只读报告，异步）
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

### 9.5b 切换当前卷（历史 → 当前）
`POST /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-versions/{pv_id}/activate` → 200（返回完整试卷，与 §9.1 同构）

只改 `exam_projects.active_paper_version_id` 指针，**不动版本本身**。切到已定稿旧卷仍是 `finalized`
（冻结即不可变）：要继续编辑须先走 §9.5 撤销定稿。版本不存在或不属于该项目 → 404。

### 9.6 导出：答案细则 JSON
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-versions/{pv_id}/export/json`
→ 附件下载（`Content-Disposition: attachment; filename="answer_detail_v{n}.json"`）。
需 `Authorization: Bearer <token>`；401 时不会下发文件。
含 `missing_answer_count` 与逐题 `answer_missing` 标记；`stem` 已剥离题干自带的编号/分值前缀。
逐题带 `no`（卷面题号：每种题型从 1 重新计数，与学生卷/答卷/答题卡一致；`item_index` 保持全局稳定序号），
schema 自 `1.2.0` 起新增该字段；逐题带 `rubric`（主观题评分细则：要点数组或文本，无则 `null`），schema 自 `1.1.0` 起新增该字段。

### 9.7 导出：学生卷 HTML / docx
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-versions/{pv_id}/export/student`
查询参数 `format=html|docx`（默认 `html`，非法值 422）；
需 `Authorization: Bearer <token>`（§9.7–9.9 各导出 docx 变体同规则）。

**format=html** → `text/html`（无答案，可打印 PDF）。正式卷面：信息头（课程名称/总分/题量，考试时间/形式/
试卷类型/学分留空待填）+ 题次表 + 按题型分节（一、单选题（共N题，每题X分，共Y分））+ 分节题号
（**每种题型从 1 重新计数**：单选 1…n、判断 1…n，不跨类型续号；docx 与其余导出同口径）。

卷面细节（对齐命题范本）：

- 节标题带去向提示「（将答案写在答题纸上）」，判断题为「（将答案写在答题纸上，对的打钩 √ ，错的打叉 ×）」；
  答案实际落在 §9.9 答题卡上。
- 单选/多选题干末尾补作答括号 `（  ）`，判断题补 `（ ）`；题干已自带括号时不重复补。
- 综合题按 `subquestions` 渲染分问：`（1）题面（6分）`；题干里的 ``` 围栏切成等宽 `<pre class="code">` 代码块。
- 题干里的 GFM 管道表格（首尾都有 `|`、第二行为 `---` 分隔行）渲染为真实 `<table class="md-table">`：
  全宽、collapse、1px 边框、单元格居中、表头灰底加粗；列数以表头行为准（少补空、多截断）；
  ``` 围栏内部保持代码原样不转表格；无表格的纯文本排版与原先逐字一致。
  站内阅读（题目详情的题干/解析）按同一规则渲染（前端 `StemBlocks`），AI 编辑场景仍显示原文。
- 不打印难度/分值元信息行（难度属内部信息；分值由节标题与分问标注表达）。该行仅答卷保留。

**format=docx** → `application/vnd.openxmlformats-officedocument.wordprocessingml.document`
（`Content-Disposition: attachment; filename=exam-paper.docx`，前端命名 `考试卷.docx`）：

- 可编辑 Word 试卷，版式取自 `docs/素材/A卷试卷_转自DOC.docx`，实现见
  `app/services/exam_paper_docx.py`；模板 `app/services/templates/exam_paper.docx`
  （运行时资源随代码提交），生成时清空正文保留 `sectPr`——页眉装订线/考场/座位号/专业名称/
  学号栏、页脚页码域、页面设置原样继承；考生信息在页眉，正文不再补底部署名栏。
- 卷面头（标题「考试卷」16pt 黑体 / 信息头 14pt / 题次表 / 页脚注记）与答题卡同构，
  公共件在 `app/services/docx_kit.py`；题目按题型分节渲染，结构与 HTML 版同构：
  题号加粗、客观题补作答括号（规则同上，不重复补）、选项与分问缩进、
  整题段落 keepNext + 题间/节间空段（题尽量不跨页）、
  ``` 围栏 → 1×1 描边代码框（新宋体 10.5pt 逐行 `w:br` 保缩进）、
  GFM 管道表格 → 有框真表格（表头 F5F5F7 浅灰加粗居中，列数以表头为准少补多截）；
  块解析与 HTML 共用 `paper_version_service._stem_blocks`，两份导出规则唯一。
- 前端「学生卷」下载按钮取 docx；整体预览/打印仍走 html（`?format` 缺省）。

### 9.8 导出：答卷（含答案）HTML
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-versions/{pv_id}/export/answer-key`
→ `text/html`（含答案）。在学生卷版式基础上加装订线、客观题答案速查表（题号|答案），
答案选项打 ✓ 标绿；缺答案标注【缺答案·需人工补充】。综合题额外给出**逐问答案**
（`subquestions[].answer`），并保留难度/分值元信息行供阅卷参考。

### 9.9 导出：答题卡 HTML / docx
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-versions/{pv_id}/export/answer-card`
查询参数 `format=html|docx`（默认 `html`，非法值 422）；需 `Authorization: Bearer <token>`。

**format=html** → `text/html`（空白作答卷，可打印 PDF）。与学生卷配套：只承接作答，**不出题面、不含答案**。
版式对齐 `docs/素材/答卷A卷` 范本（A4，`@page { size: A4; }`）：

- 考生信息栏（学号/姓名/考场/座位号/专业名称）置于信息头与题次表之间，底部不再重复学号栏。
- 客观题（单选/多选/判断）→ 全宽题号表格 + 空白答案格，每 10 题换行防溢出。
- 填空题 → 一题一条横线（题号 + 约 40% 宽下划线）。
- 其余主观题 → 题号 + 矩形作答大框（44mm 高）；综合题 → 整页大框（240mm 高），
  首题随节标题同块不拆页、其后逐题强制换页；不出分值与分问文字（结构在学生卷上）。
- 每节标题右侧带「得分 / 评卷人」小格；节标题与首个作答区不拆页。
- 装订线为**左侧双虚线**（与 A卷试卷 / 答卷A卷 范本一致），页面中部与右侧无竖线；
  答卷（§9.8）同款，学生卷不带装订线。

**format=docx** → `application/vnd.openxmlformats-officedocument.wordprocessingml.document`
（`Content-Disposition: attachment; filename=answer-card.docx`，前端命名 `答题卡.docx`）：

- 可编辑 Word 试卷，实现见 `app/services/answer_card_docx.py`；模板
  `app/services/templates/answer_card.docx`（源自 `docs/素材/答卷A卷.doc` 转换），
  生成时清空正文保留 `sectPr`——页眉装订线/考场学号栏、页脚页码域、页面设置原样继承；
  卷面头与表格段落公共件在 `app/services/docx_kit.py`（与考试卷 §9.7 docx 共用）。
- 正文按数据重建：标题与信息头（14pt）、考生信息栏（带框，专业名称主动断行防拆词）、
  题次表、每节「节标题 + 得分/评卷人小格」（全段 keepNext，含竖向合并延续格，保证节标题与首作答区不拆页）、
  客观题格子表（每 10 题一块全宽等分）、填空横线（12pt 新宋体 `_`×31）、
  主观题矩形框（EXACTLY 行高 + cantSplit 整行不跨页）、综合题整页大框
  （首题高按「页高 − 节间距 − 节标题 − 题号行 − 余量」扣减使其与节标题同页，其后逐题题号行 `page-break-before`）。
- 前端「下载」按钮取 docx；整体预览/打印仍走 html（`?format` 缺省）。

---

### 9.10 一键打包下载（全部导出产物 → ZIP）
`GET /api/v1/courses/{course_id}/exam-projects/{project_id}/paper-versions/{pv_id}/export/bundle`
需 `Authorization: Bearer <token>`；`project_id` 仅作路径占位，版本定位同其它导出（`pv_id + course_id`）。

- **实现**：`app/services/paper_export_bundle.py::bundle_paper_exports`——包内六份与逐个导出端点
  **同源渲染**（复用 §9.6–§9.9 的 `export_*` 函数，不复制渲染逻辑），`zipfile` 标准库内存打包
  （无新依赖）。纯同步 CPU 渲染，不涉及 LLM/外部系统，故不走 Celery。
- **包内清单**（文件名带版本号 `{n}`，便于教师区分多版本导出）：
  `考试卷_v{n}.docx` / `学生卷_v{n}.html` / `答题卡_v{n}.docx` / `答题卡_v{n}.html` /
  `答卷_含答案_v{n}.html` / `answer_detail_v{n}.json`。
- **响应**：`application/zip`，`Content-Disposition: attachment; filename="paper-bundle-v{n}.zip"`
  （头用 ASCII 命名，下载名由前端命名为中文 `试卷包_v{n}.zip`）。
- **错误**：版本不存在/跨课程访问 → 404（与其它导出端点同口径，`PaperVersionError` 映射）。
- **前端**：档案卡导出区「一键打包」按钮（`PaperProfile` → `PaperPanel.handleBundle` →
  `api.paperVersions.fetchBundle`），打包期间按钮 loading。

---

## 10. AI 助手 Assistant（对话页）

> 来源 `app/api/v1/assistant.py`。前缀 `/api/v1/courses/{course_id}/assistant`
> ⚠️ **鉴权**：本节全部端点需 `Authorization: Bearer <token>`（router 级依赖，与 §8 同款）。
> 红线：**SSE 端点零 LLM**——意图解析与生成全部在 Celery worker（`assistant_turn`，
> transactional outbox 派发）；只读查询出确定性结果卡，写操作只产提案卡，执行由前端
> 确认后调既有业务 API（§2 / §3.5 / §8.7b / §8.11），助手没有第二套写路径。

### 10.1 入一轮对话（202）

`POST /api/v1/courses/{course_id}/assistant/turns`

- 请求：`{ "message": "...", "session_id": "..." }`（`message` 必填，去空格后 ≤ 2000 字；
  `session_id` 可选——缺省时取课程最近会话，无则自动新建，旧客户端兼容）
- 响应 202：`{ "task_run_id", "user_message_id", "session_id" }`
- 行为：写 `assistant_messages(user, session_id)` + `task_runs(assistant_turn, queued)` + outbox 派发；
  同课程**同会话**在途同文本任务复用同一 `task_run_id`（双击不重复烧模型，会话参与幂等键），
  终态后同文本换新键；会话标题为「新会话」时以首条消息自动改题（去换行取前 40 字）。
- 状态码：503 `LLM model is not configured`；404 课程不存在/会话不存在；422 空消息/超长。

### 10.2 SSE 事件流

`GET .../assistant/turns/{task_run_id}/stream?last_id=0`

- `text/event-stream`；事件：`delta {text}` / `think {text}`（模型思考增量——意图阶段与段2 流式
  的 reasoning 实时回调 `on_think`，聚批推送、阶段收口 flush；前端思考区实时展示，不计入正文回复）/
  `card {kind,tool,payload}`（kind ∈
  `result` | `proposal` | `sources`，sources=资料内容问答的来源引用卡，§10.5）/
  `done {message_id,task_run_id}` / `error {message}`，另有心跳注释行 `: ping`。
- 每帧带 `id:`（Redis 流条目 id）——断线重连带 `?last_id=<最后收到的 id>` 从该条目后续读，
  不重放已收增量；前端用 `fetch` + `ReadableStream`（`EventSource` 带不了 Authorization）。
- 只读转发 Redis Stream（`assistant:turn:{task_run_id}`，MAXLEN ~5000 / TTL 600s）；
  通道不可用时按 `task_runs` 终态 DB 兜底（首轮 + 每 8 轮 probe 推 done/error；
  **`cancelled` 按 `done` 收口**——教师主动停止是正常结束，不是错误）。跨课程/未知任务 404。
  **端点不调用模型**。

### 10.3 历史恢复

`GET .../assistant/messages?session_id=<sid>` →
`[{ id, task_run_id, role, content, action, stream_status, created_at }]`

按 `created_at` 时间序（≤200 条）；`session_id` 给定只返回该会话，**缺省返回全课程**
（向后兼容）。挂载拉取 + `done` 后刷新，前端以它为权威；
末条为 `user` 即视为上一轮在途 → 重连 §10.2 续读。

### 10.4 提案卡状态回写（只记账）

`PATCH .../assistant/messages/{message_id}`

- 请求：`{ "action_status": "executed" | "dismissed", "receipt": "..." }`
- 仅 `proposed → executed/dismissed` 单向迁移；**不执行任何业务**——提案的真正执行由
  前端在教师确认后调既有业务 API，成功后才回写状态。
- 状态码：404 不存在/跨课程；422 非法状态值；409 非提案卡或已非 proposed。

### 10.5 数据与工具

- 新表 `assistant_messages`（`app/db/schema.py`，`python -m app.db.init_db` 幂等迁移）：
  `task_run_id` FK + `role` + `content` + `action`(JSON) + `stream_status`
  （`complete` | `failed` | `stopped`，`stopped` = v3 停止生成保留的部分正文）+ `session_id`
  （可空，v3），全部带 `course_id`；索引 `ix_assistant_messages_course_session`。
- 新表 `assistant_sessions`（v3 多会话，会话 = 时间线与记忆边界）：
  `course_id` + `title` + `created_at`/`updated_at`（活跃排序用），按 `course_id` 隔离；
  历史消息按课程回填会话的幂等迁移见 §10.6。
- 只读工具（结果卡）：`course_overview` / `list_materials` / `framework_status` /
  `blueprint_status` / `contract_status` / `paper_status` / `list_exam_projects`。
  项目级工具（overview/blueprint/contract/paper/list_exam_projects）支持可选
  `args={project_id}`——教师点名项目时卡片只呈现该项目（与提案同一套 id 白名单硬校验），
  未点名呈现全部；前端单项目卡片 CTA 深链 `?project={id}`；
- **使用引导（`usage_guide`，能力地图 + 操作引导）**：`kind="result"` 引导卡（前端 `GuideCard`），
  `args={}` 恒定、不触库——载荷 `payload:{steps:[{key,label,nav,status,hint}…], pages:[{label,nav,desc}…],
  current_step}`。步骤 = 出卷主线六步（上传解析 → 框架 → 目录 → 蓝图 → 合同生成 → 审核导出），
  `status`（done/current/todo）由本轮 snapshot 的既有完成信号按前缀推导（第一未完成步 = current，
  全完成 = `current_step:null`）；段1 prompt 同时注入产品能力地图（页面模块 / 出卷主线 / 助手边界），
  教师问「这个网站能干什么/怎么出卷/下一步做什么」时模型选此工具，reply 结合 `current_step`
  给 1~3 句引导（不复述步骤）；卡上每步带「前往」跳转按钮 + 卡底五模块页面导航；
- 提案工具（`PROPOSAL_TOOLS` 共 12 个，确认后调用）：`create_course` → §2、`update_course` → §2、
  `start_parse` → §3.5、`create_exam_project` → §8.2、`update_exam_rules` → §4.8、
  `create_blueprint` → §8.5、`confirm_blueprint` → §8.8、`enqueue_blueprint_suggest` → §8.7b、
  `confirm_contract` → §8.11、`start_generation` → §8.13、`enqueue_paper_review` → §9.3g、
  `update_question_type_format` → §4.8（单题型出题格式写入考核规则 `type_formats`，空串恢复默认；
  综合题由原型档案驱动、不适用本工具）。模型回传的 id 必须命中段1 上下文白名单，非法带反馈重试
  一次；蓝图确认与发起生成已移入提案（卡上「确认执行」= 教师确认），定稿/导出/删除资料等仍在
  `REFUSED_TOOLS` 硬拒。
- **出卷主线逐级提案（阶梯）**：教师要出卷、继续出卷或直接给出出卷要求时，模型按 snapshot 的项目
  状态选**下一步**提案、一次一张卡推进：`create_exam_project` → `update_exam_rules`（考核要求落点）
  → `create_blueprint`（综合题原型白名单等）→ `enqueue_blueprint_suggest`（已有蓝图微调：
  难度比例 → 指令）→
  `confirm_blueprint` → `confirm_contract` → `start_generation`；卡片确认成功后前端自动追问
  「继续」，模型按阶梯接续，不要求教师手动输入。教师具体要求的确定性落点表：难度比例 →
  蓝图创建前 `update_exam_rules.difficulty_distribution`（全卷一份 `{low,medium,high}`，
  蓝图创建时逐题型确定性落位，已存在蓝图不受影响）、蓝图已存在待确认时
  `enqueue_blueprint_suggest`（难度比例 → 指令，**系统确定性换算**目标分布，模型不自行
  换算、不承诺达标）、偏理论/侧重理解 → `assessment_focus`、
  题型比例/章节权重 → `update_exam_rules`、综合题形态（如不出代码题）→
  `create_blueprint.comprehensive_archetypes`、单题型格式 → `update_question_type_format`、
  整卷质量检查 → `enqueue_paper_review`。停点按项目 `status` 优先判定：`review`/`exported` →
  定式话术引导「试卷」页审核编辑（例外放行：另出新卷 `create_exam_project`、整卷评审
  `enqueue_paper_review`，均不推进本项目）；`generating` 且生成任务在跑 → 引导看进度不重复发起
  （状态双读 `status` + `generation_task_status`：合同刚确认未发起照发 `start_generation`，
  任务 failed/cancelled → 引导试卷页重新生成）。比例/难度/去重的**换算与结果**始终由确定性算法
  负责，助手只把教师原话转成指令/提案，不把规则塞给模型「自觉遵守」。
- **资料内容问答（RAG，助手 v2）**：`answer_material_content`（第 4 类路由，`kind="sources"`）——
  - `args={material_id?}` 点名资料（复用同一套材料白名单 + `parse_status=="ready"` 硬校验，
    未解析引导先解析）；不传则限定**全课程已解析资料**（无已解析资料时带反馈重试后落确定性文案）；
  - 问题 = 教师原话（`route_intent(message=...)` 原样下传，不经模型转述）；
  - worker 内：`ensure_embedded` 自愈缺向量块（历史数据/任务失败兜底）→ `load_content_chunks`
    装载（仅 staged 最新版本最新 ready run 的块）→ 检索：**多查询混合**——确定性改写出双变体
    （原问题 + 去问句框架的主题核心，如「总结教学大纲讲了什么？」→「教学大纲」），一次批量嵌入，
    各变体独立打分（混合 `0.35 词面 + 0.65 语义`，`top_k=6`、hybrid min 0.15），合并期按归一文本
    折叠近重复（跨文档同文模板碎片只留得分最高一份，正文块才进得了 top_k；单变体退化为既有单查询）；
    语义打分可**下推 PG 库内算**（`load_semantic_scores` unnest 向量逐变体算 cosine，向量不过网络，
    实测 59MB/轮 → ~4MB；非 PG 方言/查询向量非法 → 返回 None 回落装载向量旧路径，任一环失败再降
    词面，嵌入故障不断轮）；装载时剔除清洗后无正文的结构残片（`<details>` 类——不可嵌入的块混入会让
    「全部块带向量」门恒假，混合检索永不点亮，与 `ensure_embedded` 可嵌入判据同源根治）；
    命中后**邻域扩展**（`_expand_rag_neighborhood`）：每个命中附带同资料同/邻页的正文块
    （len≥60 排除又一个标题；同页优先、长块优先，轮转分配保证后位命中不吃光预算，
    同块只带一次、单轮封顶 6 块）进段2 与来源卡——「总结类」问题与正文天然低相似、
    短标题必胜（实测正文表 rank #291），据此把被短块洪泛淹没的正文表/长段确定性捞回；
    嵌入未配置/失败/向量缺失 → 纯词面降级（Jaccard 2/3-gram，min 0.2）；
  - 段2 流式用 `answer_material_content` 专用 grounding prompt（temperature 0.3，只依据片段作答，
    找不到就说找不到；拒绝话术第 6 条：全文照抄/朗读仍拒，`read_material_content` 保持在 `REFUSED_TOOLS`）；
  - 命中：`action={kind:"sources", tool, args, status:"completed", payload:{question,
    material_id, material_name, mode:"hybrid"|"lexical", sources:[{block_id, material_id,
    material_name, page_index, heading_path, snippet}]}}`，SSE `card {kind:"sources"}`，
    前端来源卡展示片段出处 +「打开资料库」CTA（score 不进 payload，避免把相关度当可信度）；
    无命中 → `action={}`（普通问答形态，正文说明没找到，不落卡）。
- **语料向量列（v2）**：`content_blocks` 增 `embedding`(JSON，可空) + `embedding_model`
  (VARCHAR(64))——换嵌入模型时按 `embedding_model != settings.embedding_model` 重嵌；
  `embedding_text_version`(INTEGER，非空默认 0) 记录**嵌入输入清洗版本**：嵌入走
  `_embedding_text` 提取（嵌套表格 HTML → 「表头 | 单元格」平铺文本、剥标签、实体反转义，
  只作用于嵌入输入，生成上下文仍用原块 HTML），清洗逻辑 bump `EMBEDDING_TEXT_VERSION`
  后按 `embedding_text_version < 当前版本` 自动重嵌（全库重嵌跑
  `scripts/reembed_content_blocks.py`，已实测：清洗对排序无增益，捞回正文靠邻域扩展）；
  由 `app/db/init_db.py` 幂等迁移（无 Alembic）。索引任务 `task_runs(material_index)`
  （幂等键 `material_index:{run_id}`，lease 300s）在 worker 内调嵌入，端点只入队（§3.5/§3.6）。

### 10.6 会话管理（v3 多会话）

前缀同上 `.../assistant/sessions`，全部带 `course_id` 隔离；会话是**记忆边界**
（历史、幂等键都按会话隔离）。响应形状：
`{ id, title, created_at, updated_at }`。

| 方法 | 路径 | 成功 | 说明 / 失败 |
|---|---|---|---|
| GET | `/sessions` | 200 `[{...}]` | 按 `updated_at` 倒序（最近活跃在前） |
| POST | `/sessions` | 201 `{...}` | 请求 `{title?}`，缺省「新会话」，超长截断 40 字 |
| PATCH | `/sessions/{sid}` | 200 `{...}` | 请求 `{title}`；422 空标题；404 不存在/跨课程 |
| DELETE | `/sessions/{sid}` | 204 | 级联删本会话全部消息；404 不存在/跨课程；**409 有在途任务**（先停本轮，§10.7） |

- 首条用户消息自动改题：会话标题仍为「新会话」时，以消息（去换行取前 40 字）改题
  （`ensure_default_session` 路径）。
- 所有端点纯 DB、**零 LLM**。
- 迁移（v2 → v3）：`init_db` 幂等补 `assistant_messages.session_id` 列与索引，
  逐课程回填历史消息到「该课首条 user 消息」命名的回填会话；PostgreSQL 另把
  `stream_status` CHECK 追加 `'stopped'`（SQLite 不能原地改约束，按既有先例跳过，
  新库 `create_all` 自带新 CHECK）。

### 10.7 停止生成（v3 协作式取消）

`POST .../assistant/turns/{task_run_id}/cancel` → 200 `{ "task_run_id", "status": "cancelled" }`

- **纯 DB**：`task_runs.queued|running → cancelled`，端点零 LLM、不碰外部系统；
  已到终态则**幂等**返回现态（`status` 即现态，非 `cancelled` = 没停成/已跑完）。
- worker 三检查点协作式中止（`assistant_service.run_turn`）：
  **A** 轮次开始前发现取消 → 直接返回，不产出任何消息、零模型调用；
  **B** 流式 `on_delta` 节流 0.5s 探测 → `TurnCancelled` 穿透降级，**保留已流出的部分正文**；
  **C** 路由产出后、落库前 → 照常落卡与正文，标记 `stream_status='stopped'`。
- 落库的停止消息带 `stream_status='stopped'`，前端渲染「已停止」徽标（任何 kind）。
- SSE 侧：worker 发布 `done` 正常收口；通道兜底把 `cancelled` 按 `done`（非 `error`）推给前端。
- 状态码：404 未知任务/跨课程；422 路径参数非法。
- worker `claim` 为条件更新（仅 `queued` 可领取）：已取消的任务领不到，不跑 handler、零模型调用。

## 11. 数据模型汇总（复用类型）

| 类型 | 说明 | 关键字段 |
|------|------|----------|
| `MaterialType` | `teaching_syllabus` / `assessment_syllabus` / `teaching_material` / `exercise` | — |
| `AssessmentMode` | `theory_recall` / `conceptual` / `application` / `problem_solving` / `practical_operation` | — |
| `ComprehensiveArchetype` | 综合题原型 | 见 `domain/generation/archetypes.py` |
| `MaterialForm` | 材料形式 | 同上 |
| `UnitCoverage` | 单元覆盖 | `unit_id, exam_point_id, anchor_key, card_ids, allowed_assessment_modes, operational_detail_policy, core` |
| `FrameworkConfirmation` | 框架确认 | `anchors, exam_points, conflict_resolutions, teacher_exclusions` |
| `KnowledgeTreeConfirmation` | 知识树确认 | `operations, reviewed_topic_codes, reviewed_exam_point_codes, teacher_exclusions` |

## 12. 状态码约定

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