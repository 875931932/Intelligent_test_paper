# PaperPact·AI 后台管理系统设计方案

> 状态：**待评审**。本文档是后台管理系统的唯一设计权威，评审通过后按第 11 节分阶段实施。
> 范围声明：本方案不引入在线考试/阅卷能力（红线），系统止于"导出纸质生产线产物"的边界不变。

---

## 1. 背景与目标

### 1.1 现状问题

| 现状 | 问题 |
|---|---|
| 账号只有 `username/password/display_name/role(admin\|teacher)` | 无单位、职称、工号、状态；建号入口藏在课程空间页的弹窗里，无列表、无编辑、无停用 |
| 模型配置全在 `.env`（`LLM_MODEL` 等 4 个分阶段变量）+ `model_profiles.py` 硬编码 4 个型号 | 换模型要改环境变量并重启，约 10 处代码直接从 settings 构造 `LLMJsonClient`，无系统内切换能力 |
| `model_calls` 表每次调用都在落库（stage/模型/token/耗时/错误码） | **有数据没界面**：无查询 API、无看板，出问题只能翻数据库 |
| 角色只有 admin/teacher 两级 | 无法表达"某校管理员只管本校账号"的真实多单位场景 |

### 1.2 目标（本期交付）

1. **后台管理系统**：同一前端工程内 `/admin` 路由组，独立 AdminLayout，仅管理员可见。
2. **组织与账号管理**：学校→学院两级组织；账号带单位、职称、工号、状态；三级角色（平台管理员/单位管理员/教师）。
3. **模型管理**：系统内添加供应商与型号、按用途绑定（默认/抽取/分类/归并/生成）、一键切换、连通性测试、启停。
4. **模型调用监控**：调用量/成功率/Token/成本/延迟看板 + 按模型·阶段·时间的分组统计 + 明细查询。

### 1.3 非目标（本期不做）

- 在线考试、阅卷、学生端（范围外红线）。
- 自助注册、SSO/对接学校统一身份认证（保留管理员建号纪律；SSO 留作二期）。
- 计费/充值/配额扣费（监控只做成本**估算**展示，不做额度控制）。
- 提示词级别的调用内容审计（监控只存哈希与白名单字段，不展示 prompt 全文）。

---

## 2. 参考案例与提炼

| 参考 | 借鉴点 | 落到本方案 |
|---|---|---|
| **OneAPI / New API** 渠道管理 | 供应商（渠道）+ 模型两级建模；添加→填密钥→**测连通**→启停的闭环；按用途路由 | 第 5 节模型管理的主流程与页面交互 |
| **LiteLLM** `STORE_MODEL_IN_DB` | 模型配置入库、管理界面改动即生效、无需改配置文件重启；API 返回掩码密钥 | `.env` 降级为兜底，DB 为运行时权威；密钥永不明文回传（第 10.3 节） |
| **Helicone / LangSmith** 监控看板 | 概览指标卡 + 趋势图 + 分组统计（模型/阶段/状态/错误码）+ 请求明细下钻 + P50/P95 延迟与成本拆分 | 第 6 节监控看板的信息架构 |
| **高校系统 RBAC 实践**（R-RBAC、组织表 DeptID/ParentDeptID） | 组织自关联层级；角色绑定到组织范围而非逐 URL 硬编码；权限矩阵先粗后细 | 第 4 节组织自关联表 + 角色×范围权限矩阵；不引入逐权限点的复杂 RBAC 框架 |
| **vue-element-admin / Ant Design Pro** 中后台范式 | 左侧固定导航 + 顶部面包屑；列表页 = 筛选条 + 表格 + 分页 + 抽屉表单；操作有确认与权限守卫 | 第 9 节前端信息架构与组件复用 |

---

## 3. 总体架构

```
前端（同一 Vite 工程）
  /login                        登录（现有）
  /courses…                     教师业务端（现有，不动）
  /admin                        后台路由组（新增，AdminRoute 守卫，仅 admin/org_admin）
      ├── 概览仪表盘
      ├── 组织与账号  →  单位管理 / 账号管理
      ├── 模型管理    →  供应商与型号 / 用途绑定
      └── 调用监控    →  概览看板 / 调用明细

后端（FastAPI）
  /api/v1/auth/**               现有登录与 /me（扩展返回组织、职称等）
  /api/v1/admin/**              新增后台 API（统一 require_admin 依赖）
      ├── organizations.py      组织 CRUD
      ├── users.py              账号列表/建号/编辑/停用/重置密码
      ├── llm.py                供应商、型号、用途绑定、连通性测试
      └── monitoring.py         聚合统计与明细（读 model_calls）

数据层
  organizations / llm_providers / llm_models / llm_bindings   ← 新增 4 张全局管理表
  users                                                        ← 扩展 8 列
  model_calls                                                  ← 既有表，仅补索引
  services/llm_registry.py        ← 配置读取（DB 优先，.env 兜底，进程内 TTL 缓存）
  adapters/model/client_factory.py ← 统一客户端工厂 build_llm_client(purpose=...)
```

**三级角色与权限矩阵**（`users.role` 取值扩展为三值）：

| 能力 | 平台管理员 `admin` | 单位管理员 `org_admin` | 教师 `teacher` |
|---|---|---|---|
| 组织管理（增改学校/学院） | ✅ | ❌（只读本校） | ❌ |
| 账号管理 | 全局增改停用 | 仅本单位（校级：全校；院级：本院），**不可**创建 `admin`/`org_admin` 超出其范围 | ❌ |
| 模型管理（供应商/型号/切换/测试） | ✅ | ❌ | ❌ |
| 调用监控 | 全局 | 本单位只读（按 6.4 节链路过滤） | ❌ |
| 教师业务端 | ✅ | ✅（其本人同时是教师身份） | ✅ |

> 兼容性：现有 `admin` 数据行语义升级为"平台管理员"，无需迁移；`teacher` 不变。`org_admin` 为新增枚举值。

**路由守卫双保险**：前端 `<AdminRoute>` 按 role 拦截渲染；后端每个 `/api/v1/admin/**` 端点挂 `Depends(require_admin)`（内部再按角色分 `require_platform_admin` / `require_org_admin`）。前端守卫只为体验，**授权以后端为准**。

---

## 4. 组织与账号设计

### 4.1 `organizations` 表（新增，全局表，不带 course_id）

```python
organizations = Table(
    "organizations",
    Base.metadata,
    Column("id", String(64), primary_key=True),
    Column("parent_id", String(64), ForeignKey("organizations.id")),  # NULL=学校
    Column("kind", String(20), nullable=False),        # 'school' | 'college'
    Column("name", String(200), nullable=False),       # "XX大学" / "计算机学院"
    Column("code", String(60)),                        # 机构代码，可空，全局唯一（有值时）
    Column("status", String(20), nullable=False, default="active"),  # active|disabled
    Column("created_at", ...), Column("updated_at", ...),
    CheckConstraint("kind IN ('school','college')"),
)
# 约束：parent_id 为空 ⇔ kind='school'；college 的 parent 必须是 school（服务层校验）
# 索引：ix_organizations_parent
```

- 学校 `parent_id=NULL`；学院 `parent_id=学校id`。深度固定两级，服务层拒绝三级挂载（从简，不实现任意树）。
- 删除策略：**只允许停用（status=disabled），不物理删除**——被用户引用的组织必须可追溯；有子学院或有账号的学校禁止停用以外的操作。

### 4.2 `users` 表扩展（init_db 幂等加列）

| 列 | 类型 | 说明 |
|---|---|---|
| `organization_id` | FK → organizations，可空 | 归属单位；平台管理员可为空。**存量数据全部为 NULL**，由管理员逐步补录 |
| `title` | String(40)，可空 | 职称：教授/副教授/讲师/助教/其他（前端下拉，后端校验枚举外允许自由文本，从简） |
| `employee_no` | String(60)，可空 | 工号/学号 |
| `status` | String(20) 默认 `active` | `active` \| `disabled`——**停用取代删除** |
| `last_login_at` | DateTime，可空 | 登录成功时刷新 |
| `created_by` | String(64)，可空 | 建号人（审计） |
| `created_at` / `updated_at` | DateTime | 常规审计列 |

- **不提供删除账号接口**：`courses.owner_id` 等外键依赖用户，删除会产生悬空数据。停用即禁止登录（`authenticate_user` 检查 `status`），历史数据保留。
- 密码重置：管理员设置新密码（复用 `hash_password`），不提供"查看原密码"。

### 4.3 账号管理 API（`/api/v1/admin/users`）

| 端点 | 权限 | 说明 |
|---|---|---|
| `GET /users?q&organization_id&role&status&page&page_size` | admin / org_admin | 列表：搜索用户名/姓名/工号，按单位/角色/状态筛选，分页。**org_admin 强制叠加本单位范围过滤** |
| `POST /users` | admin / org_admin | 建号：username/password/name/role/organization_id/title/employee_no。org_admin 只能建 `teacher` 且 organization_id 限本单位 subtree |
| `PATCH /users/{id}` | admin / org_admin | 改资料（姓名/职称/工号/单位/角色/状态）。org_admin 不可越权改单位外的人、不可授 `admin` |
| `POST /users/{id}/reset-password` | admin / org_admin（限本单位） | 重置密码 |
| `POST /users/{id}/disable` \| `/enable` | admin / org_admin（限本单位） | 停用/启用；禁止停用自己；平台管理员不可被 org_admin 停用 |

- **范围过滤实现**：org_admin 的单位 subtree = 本单位 + 其下所有学院的 id 集合（查询时一次 `SELECT id FROM organizations WHERE parent_id=:org OR id=:org`）。教师的业务端查询不变（仍按 `courses.owner_id`），与本表无耦合。
- 现有 `POST /api/v1/auth/users`：**阶段 1 起标记弃用并从课程空间页移除入口**，阶段 4 删除端点（旧集成测试同步迁移）。
- `GET /api/v1/auth/me` 响应扩展：`organization`（id/name/kind）、`title`、`employee_no`、`status`，前端 auth store 同步扩展 `User` 类型。

### 4.4 登录链路改动

`auth_service.authenticate_user` 追加：`user.status != "active"` → 抛 `AuthenticationError("账号已停用，请联系管理员")`。其余 token 逻辑（HMAC 签名、TTL）不动。

---

## 5. 模型管理设计

### 5.1 表结构（新增 3 张全局表）

```python
llm_providers = Table(            # 供应商/渠道（对应 OneAPI 的 channel）
    Column("id", String(64), primary_key=True),
    Column("name", String(120), nullable=False),          # "StepFun"、"自建代理"
    Column("base_url", String(300), nullable=False),      # 不含 /chat/completions
    Column("api_key", String(300)),                       # 见 10.3 安全说明
    Column("api_key_env", String(80)),                    # 可选：改从环境变量名取值（优先于 api_key 列）
    Column("status", String(20), default="active"),       # active|disabled
    Column("remark", String(300)),
    ...created_at/updated_at,
)

llm_models = Table(               # 型号（对应 OneAPI 的模型 + 本项目 model_profiles 档案）
    Column("id", String(64), primary_key=True),
    Column("provider_id", ForeignKey("llm_providers.id"), nullable=False),
    Column("model_id", String(120), nullable=False),      # 线上报给供应商的型号名，如 step-5-preview
    Column("display_name", String(120), nullable=False),
    Column("profile", JSON, default=dict),                # 调优档案（见 5.2）
    Column("price_input_per_mtk", Float),                 # 元/百万 input tokens，可空=不计价
    Column("price_output_per_mtk", Float),
    Column("status", String(20), default="active"),
    ...created_at/updated_at,
    UniqueConstraint("provider_id", "model_id"),
)

llm_bindings = Table(             # 用途绑定：一键切换的落点
    Column("purpose", String(30), primary_key=True),      # default|extract|classify|consolidate|generation
    Column("model_id", ForeignKey("llm_models.id"), nullable=False),
    ...updated_at,
)
```

**purpose 枚举与既有 `.env` 变量的对应**（保证迁移期语义连续）：

| purpose | 对应 .env | 用途 |
|---|---|---|
| `default` | `LLM_MODEL` | 未细分用途的兜底（助手、改题、建题、评审、生成等当前均走它） |
| `extract` | `LLM_EXTRACT_MODEL` | 资料知识抽取 |
| `classify` | `LLM_CLASSIFY_MODEL` | 知识目录分类 |
| `consolidate` | `LLM_CONSOLIDATE_MODEL` | 事实归并 |
| `generation` | （当前无专用变量，走 default） | 命题生成；绑定后优先，未绑定回落 `default` |

### 5.2 profile JSON：承接 `model_profiles.py`

`profile` 字段结构与现有 `ModelProfile` dataclass 一一对应，让 DB 档案可直接构造档案对象：

```json
{
  "thinking_style": "reasoning_effort",       // reasoning_effort | thinking_disabled
  "supported_efforts": ["low", "medium", "high"],
  "default_effort": "low",
  "supports_tool_choice": false,
  "supports_json_schema": true
}
```

`resolve_model_profile` 改为三级解析：**DB 档案（registry）→ 代码 `MODEL_PROFILES` → 按 base_url 回退通用档**。代码档案保留，作为 DB 未配置时的兜底（既有部署行为零变化）。

### 5.3 运行时配置读取：`app/services/llm_registry.py`

```
resolve_llm_config(purpose) →
    1. 查 llm_bindings[purpose] → llm_models → llm_providers（一条 SQL join）
    2. 未绑定/已停用 → 回落 purpose='default' → 仍未命中 → 抛出"未配置"，
       由工厂回退 settings(.env)          ← .env 从此是兜底，不再是唯一来源
    3. 结果缓存在进程内 (TTL 30s)
```

- **生效时延**：管理端切换后，API 进程与各 Celery worker 进程最长 **30 秒**内自动刷新（各自 TTL，无需跨进程通知；从简，不引入 Redis 发布订阅）。UI 文案明确提示"切换最长 30 秒生效"。
- 保留 `.env` 回退的意义：空库启动、DB 配置被停用、故障兜底——现有部署升级后行为不变。

### 5.4 统一客户端工厂：`app/adapters/model/client_factory.py`

```python
def build_llm_client(purpose: str = "default", *, recorder=None, **overrides) -> LLMJsonClient:
    cfg = resolve_llm_config(purpose)      # 服务层
    return LLMJsonClient(
        api_key=cfg.api_key, base_url=cfg.base_url, model=cfg.model_id,
        disable_thinking=cfg.disable_thinking, recorder=recorder, **overrides)
```

**改造范围（根治散落构造点）**：以下 10 处 `LLMJsonClient(...)` 直接读 settings 的构造点，全部替换为 `build_llm_client(purpose=...)`：

- `services/`：assistant、ai_create、ai_revise、blueprint_suggest、exam_rules_ai、framework_review_ai、generation_runner、paper_review（8 处）
- `api/v1/`：framework、knowledge（2 处）
- 分阶段用途按语义归位：knowledge 的分类/归并/抽取分别传 `classify`/`consolidate`/`extract`；生成链路传 `generation`；其余传 `default`。

⚠️ **封存红线核对**：`framework_graph.py` / `organization_graph.py` / `generation_graph.py` 三个封存文件**不直接构造客户端**（构造点全部位于 services/api 层，graph 接收注入的 client），工厂收敛不触碰封存代码。改动后按红线要求跑全量验证套件 + `pytest -q`。

### 5.5 模型管理 API（`/api/v1/admin/llm`，仅平台管理员）

| 端点 | 说明 |
|---|---|
| `GET/POST/PATCH /providers` | 供应商 CRUD；PATCH 含启停 |
| `GET/POST/PATCH/DELETE /models` | 型号 CRUD（型号可真删：先检查未被绑定引用，被引用则只能停用） |
| `GET /bindings` | 当前五类用途的绑定一览（含各自型号、供应商、档案摘要） |
| `PUT /bindings/{purpose}` | **一键切换**：校验目标型号 active 后写入；TTL 自动生效 |
| `POST /test` | 连通性测试：body `{provider_id?, model_id?}`，最小 chat 请求（`max_tokens=1`，10s 超时），返回 {ok, latency_ms, error?} |

- **连通性测试说明**：这是管理面的短探测（非解析/生成类长任务），同步执行、10 秒硬超时、单并发；不进 Celery。若后续需要"批量体检"再升级为异步任务。
- 写操作记入现有日志体系（`logging`），变更打 INFO：谁、改了什么、从 X 切到 Y。

### 5.6 首次初始化（种子数据）

`init_db` 幂等种子：若 `llm_providers` 为空且 `.env` 已配置，自动把当前 `settings` 的 `base_url/api_key/model` 落成"StepFun（默认）"供应商 + 型号 + `default` 绑定——**管理员首次进后台即看到现状的镜像**，之后一切在界面操作；`.env` 从此只作兜底。

---

## 6. 模型调用监控设计

### 6.1 数据源与索引

数据源为既有 `model_calls` 表（每次逻辑调用一行：`stage/provider/model/status/input_tokens/output_tokens/duration_ms/error_code/details/created_at`），**零改造复用**。新增聚合索引（init_db 幂等创建）：

```sql
CREATE INDEX IF NOT EXISTS ix_model_calls_created_at      ON model_calls (created_at);
CREATE INDEX IF NOT EXISTS ix_model_calls_model_created   ON model_calls (model, created_at);
CREATE INDEX IF NOT EXISTS ix_model_calls_stage_created   ON model_calls (stage, created_at);
CREATE INDEX IF NOT EXISTS ix_model_calls_status_created  ON model_calls (status, created_at);
```

### 6.2 聚合 API（`/api/v1/admin/monitoring`）

所有端点必带 `from`/`to`（ISO 时间，默认近 7 天），**强制时间范围上限 92 天**，防全表扫。

| 端点 | 返回 |
|---|---|
| `GET /overview` | 概览卡：总调用数、成功率、失败数、input/output tokens 合计、成本估算（Σ tokens×单价，单价空则该型号不计价）、avg 耗时、P50/P95 耗时 |
| `GET /timeseries?bucket=hour\|day` | 趋势序列：调用量、成功率、tokens（按 bucket 聚合） |
| `GET /breakdown?group_by=model\|stage\|status\|error_code` | 分组统计：每组的调用量、成功率、tokens、成本、avg 耗时 |
| `GET /calls?page&page_size&model&stage&status&error_code` | 明细分页列表（近→旧） |
| `GET /calls/{id}` | 单条详情：白名单字段 + `details` 脱敏投影（见 10.4） |

P50/P95 用 SQL `PERCENTILE_CONT`（PG）计算；SQLite 测试环境降级为只返回 avg（实现处做方言分支，测试不依赖分位数）。

### 6.3 看板信息架构（借鉴 Helicone/LangSmith）

```
顶部      [时间范围选择：24h | 7d | 30d | 自定义]  [自动刷新开关 30s]
第一行    指标卡 ×6：调用量 / 成功率 / Token(入+出) / 成本估算 / 平均耗时 / P95
第二行    趋势折线：调用量(柱) + 成功率(线) 按 hour/day
第三行    左：按模型分组表（可切 按阶段/按错误码）  右：错误分布（Top 错误码条形）
第四行    最近失败列表（时间/阶段/模型/错误码）→ 点击进入调用明细
明细页    筛选 + 表格 + 行点击抽屉（阶段链路、尝试次数、耗时、token、错误详情）
```

### 6.4 单位管理员的本单位视图

`model_calls.course_id → courses.owner_id → users.organization_id` 三表 join 得到"该单位产生调用"的子集：

- org_admin 请求时，monitoring 查询自动叠加 `courses.owner_id IN (本单位 subtree 的用户)`。
- 平台管理员不叠加，看全局。
- 这是管理面**只读聚合**例外（红线处理见第 10.1 节），业务代码不得模仿该跨课程查询模式。

---

## 7. API 汇总

```
/api/v1/admin/organizations     GET(tree) POST PATCH /{id}
/api/v1/admin/users             GET POST PATCH /{id} PUT /{id}/password PUT /{id}/status
/api/v1/admin/llm/providers     GET POST PATCH /{id}
/api/v1/admin/llm/models        GET POST PATCH /{id} DELETE /{id}
/api/v1/admin/llm/bindings      GET PUT /{purpose}
/api/v1/admin/llm/test          POST
/api/v1/admin/monitoring/overview|timeseries|breakdown|calls  GET

依赖：require_admin（router 级挂载，内部分平台/单位两级校验）
变更：/auth/me 响应扩展；/auth/users 已删除（阶段 4，建号统一走 /admin/users）
```

---

## 8. 数据库变更清单

| 变更 | 内容 | 入口 |
|---|---|---|
| 新表 ×4 | `organizations`、`llm_providers`、`llm_models`、`llm_bindings` | `schema.py` 定义 + `create_all` |
| `users` 加列 ×8 | 4.2 节清单 | `schema.py` + `init_db` 幂等 `ADD COLUMN`（复用既有 `_ensure_users_columns` 模式） |
| 索引 ×4 | 6.1 节 | `init_db` 幂等 `CREATE INDEX IF NOT EXISTS` |
| 种子 | 平台管理员（已有）；`.env` 镜像进模型表（5.6） | `init_db` 幂等 upsert |
| 无 Alembic | 全部走 `uv run python -m app.db.init_db` | 空库与既有库各验一次，结构一致 |

全局管理表（organizations/llm_*）**不带 `course_id`**：它们是平台级配置，不属于任何课程域。此为对"40 张表课程隔离"纪律的显式边界声明（见 10.1）。

---

## 9. 前端设计

### 9.1 路由与守卫

```
/admin                AdminRoute 包裹（role ∈ {admin, org_admin}，否则跳 /courses）
  index               → 概览仪表盘
  organizations       → 单位管理（admin 全量；org_admin 只读本校）
  users               → 账号管理
  llm                 → 模型管理（仅 admin；org_admin 进入显示 403 提示页）
  monitoring          → 调用监控
    calls/:id         → 调用详情（或抽屉，见 9.4）
```

- 新增 `AdminLayout`：左侧固定深色导航（概览/组织与账号/模型管理/调用监控）+ 顶栏面包屑与返回业务端入口；复用现有 `Layout` 的插槽模式，不复制代码。
- 教师业务端侧栏**不出现**后台入口（按 role 渲染），`/admin` 直接输入 URL 由 AdminRoute 兜底。

### 9.2 页面与组件

| 页面 | 结构 | 复用组件 |
|---|---|---|
| 概览仪表盘 | 指标卡行 + 最近失败 + 模型状态一览 | `Card`/`Badge` |
| 单位管理 | 左树（学校→学院）+ 右侧表单抽屉（新建/编辑/停用） | 自研轻量 Tree + `Modal`/`Input`/`Select` |
| 账号管理 | 筛选条（搜索/单位/角色/状态）+ 表格 + 分页 + 抽屉表单（建号/编辑/重置密码/停启用，含二次确认） | `Input`/`Select`/`Modal`/`Badge` |
| 模型管理 | 两 Tab：① 供应商&型号（主从列表 + 表单 + 测试按钮）② 用途绑定（5 行 purpose → 型号下拉，切换即时保存 + 生效提示） | 同上 |
| 调用监控 | 6.3 节信息架构 | 图表组件见 9.3 |

**不引入新 UI 组件库**，沿用现有 ui/ 体系与设计变量（从简、风格一致）；仅当表格出现新交互（固定列/列排序）时在 ui/ 下补一个 `Table` 基础组件。

### 9.3 图表方案

监控看板需要折线/柱状/条形三类图。两案：

- **A（推荐）引入 `recharts`**：React 生态标准、按需 tree-shake、开发速度快；新增约一个前端依赖（唯一新增，需评审同意）。
- **B 零依赖自绘 SVG**：实现折线/柱状/条形三个轻量组件（数据点经后端聚合后数量有限，交互只需 tooltip）。无新依赖，但开发与维护成本略高。

> 默认按 **A** 实施；若评审要求零新依赖则切 B，接口层不受影响。

### 9.4 状态与 API 层（遵守既有纪律）

- 新增 stores：`useAdminOrgStore`、`useAdminUserStore`、`useAdminModelStore`、`useAdminMonitorStore`（一域一 store）。
- 新增 `src/api/domains/admin.ts`（organizations/users/llm/monitoring 四组），全部走 `request()`，禁止组件内散落 fetch。
- `types/api.ts` 手写镜像后端新类型；`stores/auth.ts` 的 `User` 扩展 `organization/title/employee_no/status`。
- 类型门禁 `npm run build` 零 error、`npm run lint` 通过。

---

## 10. 安全与红线合规

### 10.1 红线逐条对照

| AGENTS.md 红线 | 本方案处理 |
|---|---|
| ② 领域引擎已验证封存 | 三个 graph 文件**零改动**（构造点均在 services/api 层，见 5.4）；工厂收敛后跑全量验证套件 |
| ④ 课程隔离 course_id 过滤，40 表无一例外 | 两处**显式例外**，评审须知悉：**(a)** 新增 4 张全局管理表不含 course_id（平台级配置，非课程域数据）；**(b)** 监控聚合与 org_admin 范围过滤需跨课程读 `model_calls`/`courses`——该例外只存在于 `/api/v1/admin/**` 管理面、只读、且必须带角色校验，业务代码禁止模仿。除此之外一切业务查询照旧 |
| ⑤ 冻结即不可变 | 不触碰蓝图/合同/版本对象 |
| ⑥ 换路止损 / ⑦ 补丁与根治 | 10 处散落构造点统一收敛到 `build_llm_client` 是**根治**（单一配置入口），不是叠补丁 |
| 外部系统只经 adapters | 连通性测试的 HTTP 调用放在 `adapters/model/`（复用网关请求路径）；registry 只做 DB 读取 |
| 结构变更唯一入口 | 全走 `schema.py` + `init_db`，无 Alembic |
| 长任务异步化 | 监控查询为读聚合（毫秒级）；连通性测试为 10s 硬超时的管理面探测，非解析/生成长任务，明示例外 |
| 前端门禁 | 不引入测试框架；`npm run build` + `npm run lint` |

### 10.2 三级角色的授权校验

- 后端依赖链：`get_current_user`（既有）→ `require_admin`（拒绝 `teacher`）→ 端点内 `require_platform_admin` / org_admin 范围过滤（4.3 节）。
- org_admin 的每次读写都必须显式带单位 subtree 过滤，**默认拒绝**：写操作校验目标资源 ∈ 本单位，越权返回 404（不泄露存在性）。

### 10.3 密钥安全

- `api_key` 落库明文、**API 一律掩码返回**（`sk-…abc`，仅创建/更新时接受完整值写入，任何 GET 不回传全文）；日志与监控详情永不打印密钥。
- 支持 `api_key_env`（引用环境变量名）模式：高安全场景下库中只存变量名，运行时从 `.env` 取值——密钥可以完全不进库。
- 与现状对比：`.env` 本就持有同一批密钥，入库未扩大暴露面（数据库访问受控）；KMS/字段加密列为二期。
- 管理员密码沿用现有 PBKDF2-SHA256（26 万轮）；token 机制不变。

### 10.4 监控数据脱敏

`model_calls.details` 中存有 `response` 完整 JSON（temperature=0 缓存所需）。监控 API 对 `details` 做**白名单投影**：只回传 `attempt_count / retry_count / final_http_status / last_error_code / validation / message_shape`，**不回传 `response` 与 `raw_response_snapshot`**；`prompt_hash` 可展示（哈希本身不可逆）。

---

## 11. 实施计划（分阶段，每阶段过门禁）

> 顺序即依赖顺序；每阶段结束跑：`uv run pytest -q` + `cd frontend && npm run build && npm run lint`，后端覆盖率门禁 `--cov-fail-under=80` 在阶段 4 全量核验。

### 阶段 0：骨架（约 0.5 天）
- 后端：`/api/v1/admin` router 包 + `require_admin` 依赖 + 空端点接线。
- 前端：`/admin` 路由组 + `AdminRoute` + `AdminLayout`（侧栏/面包屑）+ 概览页占位。
- 验收：admin 登录进 `/admin` 看到骨架；teacher 被守卫挡下；pytest/build/lint 全绿。

### 阶段 1：组织与账号（约 2~3 天）
- `organizations` 表 + `users` 加列 + init_db 幂等迁移（空库/既有库双验）。
- `/admin/organizations`、`/admin/users` 全套 API + 单位树页 + 账号页（建/改/停用/重置密码/筛选/分页）。
- 三级角色落地：`org_admin` 枚举 + 范围过滤 + `/auth/me` 扩展 + 登录检查 `status` + auth store 扩展。
- 课程空间页移除建号弹窗，改为跳转后台提示。
- 测试：`tests/integration/test_auth_users_api.py` 迁移扩展；新增组织/账号/范围过滤用例。
- 验收：管理员全流程操作真实可用；org_admin 越权被 404/403 挡住。

### 阶段 2：模型管理（约 3~4 天，本方案最大风险点）
- 3 张模型表 + `.env` 种子镜像 + `llm_registry.py`（TTL 缓存 + 三级 profile 解析）。
- `client_factory` 并替换 10 处构造点（逐处替换、逐处跑相关测试）。
- `/admin/llm` 全套 API + 模型管理两 Tab 页 + 连通性测试。
- 测试：registry 解析/回退/停用回退单测；工厂 purpose 归位测试；`model_profiles` 三级解析测试；**跑领域引擎全量验证套件**（红线②）。
- 验收：界面添加供应商→测连通→绑定用途→**不改 .env 不重启**，最长 30 秒后新调用走新模型（`model_calls.model` 字段可证）。

### 阶段 3：调用监控（约 2 天）
- 4 个聚合索引 + `/admin/monitoring` 五个端点（方言分支：PG 分位数 / SQLite 降级）。
- 概览看板页 + 明细页 + 抽屉（含 10.4 脱敏）；图表按 9.3 选型落地。
- 测试：聚合端点集成测试（种子调用数据→断言各维度统计）；脱敏投影测试；org_admin 单位过滤测试。
- 验收：看板数据与直接 SQL 核对一致；明细筛选/下钻可用。

### 阶段 4：收尾（约 1 天）
- 删除 `POST /api/v1/auth/users` 旧端点与其测试（前端已无引用）。
- 更新 `AGENTS.md`（"换模型只改 .env"→"后台模型管理为运行时权威，.env 为兜底"；新增管理面例外条目）与 `docs/LLM_TUNING.md`。
- 全量门禁：`uv run pytest --cov=app --cov-fail-under=80`、`npm run build`、`npm run lint`；空库/既有库 init_db 双验。

**总计约 9~11 人日。** 里程碑依赖：0 → 1 与 2 互不依赖（可并行），2 → 3（监控展示依赖模型表做 join 展示名，弱依赖，可提前写死 model 字符串先展示）。

---

## 12. 风险与开放问题

| # | 风险/问题 | 应对 |
|---|---|---|
| 1 | 10 处构造点替换引入回归（配错 purpose 导致阶段用错模型） | 逐处替换 + 每处对应测试；`.env` 兜底保证最坏情况行为=现状；阶段 2 验收用 `model_calls.model` 实证 |
| 2 | 30 秒 TTL 内切换未生效被当作 bug | UI 明示生效时延；提供"当前生效配置"查询端点（绕过缓存直读 DB）供核对 |
| 3 | 监控聚合在大表上慢 | 强制时间范围 + 4 个索引；超 92 天窗口直接 422 |
| 4 | `model_calls.details.response` 体量大导致明细页慢 | 明细列表只 SELECT 白名单列，`details` 仅详情端点读取 |
| 5 | org_admin 范围过滤遗漏形成越权 | 所有 admin 端点默认拒绝 + 集成测试覆盖每个端点的越权用例 |
| 6 | recharts 依赖需评审同意（9.3） | 已列为决策点，默认 A，可无痛切 B |
| 7 | 存量账号无单位，org_admin 上线初期"无兵可用" | 组织与账号页支持批量设置单位；存量 `organization_id=NULL` 仅平台管理员可见可管 |

### 待评审确认清单

1. **红线例外**（10.1）：4 张全局管理表不含 course_id；管理面跨课程只读聚合——是否认可？
2. **图表选型**（9.3）：A 引入 recharts / B 自绘 SVG。
3. **模型连通性测试同步执行**（5.5，10s 超时管理面探测）是否接受？
4. **密钥入库**（10.3）：明文落库+掩码回传+可选 `api_key_env` 引用环境变量，是否接受（KMS 二期）？
