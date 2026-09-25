# AGENTS.md

> AI 智能组卷/阅卷系统单仓库工程规范。适用于所有在本仓库工作的 AI 编码代理与工程师。
> 推荐阅读顺序：项目概述 → 核心纪律（红线）→ 开发命令 → 项目结构 → 代码规范 → 测试策略。

## 1. 项目概述

- **类型**：全栈 Web 应用（FastAPI 后端 + React 前端），单仓库，主干分支 `main`。
- **核心功能**：面向高校教师的纸质期末试卷生产线——
  上传大纲与教学资料 → 知识目录 → 教师确认蓝图/合同 → AI 按合同出题 → 教师审核编辑 → 导出学生卷/答卷/答题卡 HTML + 答案细则 JSON（阅卷端的直接输入）。
- **技术栈**：
  - 后端 `backend/`：FastAPI + SQLAlchemy 2.0 + PostgreSQL + Redis + Celery + LangGraph 工作流；适配器层接 LLM 、MinerU（解析）、MinIO/S3（存储）。
  - 前端 `frontend/`：React 19 + TypeScript + Vite + Zustand，7 个页面模块（`src/pages/`）。
- **范围声明**：在线考试/阅卷本身**明确不做**，本系统止于"导出纸质生产线产物"，权威说明见 `docs/` 设计文档。

## 2. 核心纪律（先读这一节）

1. **确定性算法保证全局约束，模型只负责写题。**
   "不重复、比例对、难度对"由确定性算法在**合同（Contract）阶段构造性保证**。
   ⚠️ 禁止把比例/难度/去重等约束检查挪进 LLM prompt，让模型"自觉遵守"。
2. **领域引擎已验证封存。**
   ⚠️ `app/workflows/framework_graph.py`、`app/workflows/organization_graph.py`、`app/workflows/generation_graph.py` 为已验证封存代码（在 `workflows/` 下，**不在** `domain/` 下）。
   修改前必须获得项目负责人确认，且改动后必须重新跑全量验证套件，否则禁止合入。
3. **在线考试/阅卷是范围外需求。** ⚠️ 收到此类需求时拒绝实现，引导至设计文档的范围声明。
4. **课程隔离是多租户底线。** 所有数据库查询、缓存键、对象存储路径必须携带 `course_id` 过滤，37 张表无一例外。
5. **冻结即不可变。** 命题框架版本一旦冻结（已确认合同、PaperVersion 同理），只允许追加新版本，禁止原地修改。
6. **同一方法重复失败即止损换路。** ⚠️ 同一修复思路连续失败 2~3 次，禁止继续在同一路径上微调重试；必须停下来重新定位根因，换一条不同的实现路径（换层次、换工具、换数据流），并清理掉此前无效改动的残留。
7. **补丁只许权宜，根治才算修复。** 允许打补丁解燃眉之急（临时兜底、防御性校验、热修），⚠️ 但禁止把"叠加补丁"当作常规解决手段；每个 bug 必须追溯到根因并在源头修复。确需保留的补丁须注明 TODO、失效触发条件与后续根治计划，并在下一轮迭代中还账。

## 3. 开发命令

### 3.1 后端（包管理器：`uv`）

```bash
cd backend
uv sync                                        # 安装/同步依赖
docker compose -f docker-compose.dev.yml up -d postgres redis minio  # 本地基础设施
uv run python -m app.db.init_db                 # 建表/结构升级（create_all + 幂等迁移，本项目无 Alembic）
uv run uvicorn app.main:app --reload            # API 服务，默认 http://127.0.0.1:8000
uv run celery -A app.infrastructure.tasks.celery_app worker --loglevel=INFO  # Celery worker（或运行 backend/start_worker.ps1）
uv run pytest -q                               # 测试门禁
```

### 3.2 前端（包管理器：`npm`）

```bash
cd frontend
npm install
npm run dev        # 开发服务器（默认 :5173）
npm run build      # tsc 类型检查 + 产物构建（门禁）
npm run lint       # oxlint（门禁）
```

### 3.3 覆盖率

```bash
uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=80
```

⚠️ 前端**没有单测**，前端的质量门禁就是 `npm run build`（tsc）+ `npm run lint`（oxlint）。
不要为新页面引入 jest/vitest 等测试框架，除非团队形成正式决议。
⚠️ 包管理器固定：后端 `uv`、前端 `npm`，禁止混用 pnpm/yarn 或混入多份锁文件；后端锁文件为 `uv.lock`，生成后必须提交。

## 4. 项目结构

```text
backend/
├── app/
│   ├── api/v1/               # 7 个 FastAPI router，一个业务概念一个 router
│   ├── workflows/            # 领域引擎 ★已验证封存★（见核心纪律）
│   │   ├── framework_graph.py      # 双大纲 → 考点 / 考试规则
│   │   ├── organization_graph.py   # 分类 → 事实 → 画像 → 知识卡
│   │   ├── generation_graph.py     # 分批生成 + 三级回退 + 终检
│   │   └── knowledge_catalog_subgraph.py
│   ├── domain/               # 领域模块（blueprint / course / framework / generation / knowledge / material）
│   ├── services/             # 用例编排（合同分配、导出等）
│   ├── infrastructure/       # Celery（tasks/celery_app、outbox、worker）、技术设施
│   ├── adapters/             # LLM / MinerU / MinIO-S3 外部系统适配器（含型号调优档案 model_profiles）
│   ├── db/                   # schema.py（37 表，按 course_id 隔离）+ session.py + init_db.py（结构变更唯一入口）
│   ├── schemas/              # Pydantic v2 请求/响应模型
│   ├── config.py             # 配置（自动向上查找仓库根 .env）
│   └── main.py               # FastAPI 入口
├── tests/                    # pytest，按 tests/{unit,integration,contract,workflow,domain} 分层
└── pyproject.toml

⚠️ 不存在 `migrations/`、`app/models/`、`app/workers/`、`app/core/`——旧文档中这些路径作废。

frontend/
├── src/
│   ├── pages/                # 7 个路由页面
│   ├── components/           # PipelinePanel / PaperPanel / ExamRulesCard 等
│   ├── stores/               # Zustand 状态仓库，一域一 store
│   ├── api/                  # 后端 API 客户端（统一 baseURL 与错误处理）
│   └── types/                # 前后端共享类型的手写镜像
├── index.html
└── package.json

docs/                         # 设计文档，范围声明的唯一权威来源
```

## 5. 架构与数据主线

四层架构（自上而下）：

1. **教师工作台（React）**：`/login`、`/courses`、课程概览、资料库、命题框架、知识目录、试卷。
2. **应用服务层**：FastAPI 7 个 router（`app/api/v1/`）+ Celery worker + outbox 派发。
3. **领域引擎 ★已验证封存★**：`app/workflows/` 下三个 graph（见第 4 节）。
4. **基础设施**：PostgreSQL（37 表、课程隔离多租户）、Redis 队列、模型网关。

数据主线（一次组卷的完整链路）：

```text
课程空间 → 资料库(四区) → 命题框架版本(冻结) → 知识目录(知识卡↔证据)
→ 试卷项目 → 蓝图 → 合同 → 生成运行 → PaperVersion → 四份导出（学生卷/答卷/答题卡 HTML + 答案细则 JSON）
```

## 6. 核心概念速查

| 概念 | 含义 |
|---|---|
| exam_rules 考核规则 | 考纲声明的题型比例/章节权重，可查看可修改（ExamRulesCard），蓝图按它推导 |
| Blueprint 蓝图 | 题位计划：题型/分值/难度/章节 |
| Contract 合同 | 逐题位锁定考查原子、答案域、禁用上下文（确定性分配算法） |
| 知识卡/原子 | 一张卡只承载一个可独立判分的原子事实 |
| 出卷流水线 | PipelinePanel：蓝图 → 合同 → 生成 |
| 试卷页签 | PaperPanel：双栏阅读器，查看/编辑/定稿/导出（与流水线同属项目详情页的两个页签） |
| 四份导出 | 学生卷 / 答卷 / 答题卡 HTML + 答案细则 JSON（阅卷端直接输入） |
| 型号调优档案 | `adapters/model/model_profiles.py`：按型号登记供应商参数（effort 档位/json_schema/思考控制），换模型只改 `.env`，手册 `docs/LLM_TUNING.md` |

## 7. 代码规范

### 7.1 通用

- **一切从简**：能用直接实现就不用抽象，不为假想需求提前设计；三个相似片段优于一个过早的抽象。
- **复用优先**：跨模块复用已有函数/组件/hook，不允许复制粘贴出第二份。
- 命名：Python 函数/变量 `snake_case`、类 `PascalCase`；TS 组件与类型 `PascalCase`、hook `useXxx`、store `useXxxStore`。

### 7.2 后端

- FastAPI router 按业务概念拆分放在 `app/api/v1/`；router 只做协议转换，业务逻辑进 `services/`，重活进 `workers/`。
- SQLAlchemy 2.0 写法：`Mapped[]` 标注 + `mapped_column`，查询必须显式 `where(course_id=...)`。
- ⚠️ 外部系统（LLM、MinerU、MinIO/S3）**只能**通过 `adapters/` 访问；禁止在 router/service 里直接实例化厂商 SDK。
- 换模型 / 调模型参数只改 `.env` 与 `app/adapters/model/model_profiles.py`（型号档案），供应商参数禁止散落在业务代码与 prompt 里；操作手册 `docs/LLM_TUNING.md`。
- LangGraph 节点保持小而纯，输入输出走显式 state schema；模型调用只允许发生在 generation_graph 既有节点内，不要新增绕过合同的调用点。
- 表结构变更必须同步修改 `app/db/schema.py`，并保证 `uv run python -m app.db.init_db` 能把旧库升到最新（在 init_db 的幂等迁移步骤中处理既有库）。⚠️ 本项目没有 Alembic，禁止执行任何 alembic 命令。
- 解析、生成等长任务全部走 Celery + outbox 异步化，禁止在请求线程里同步调用 LLM/解析。

### 7.3 前端

- 全局状态一律 Zustand store，按业务概念一域一 store；禁止在组件里堆本地 state 冒充全局状态。
- 前端 API 调用只走 `src/api/` 客户端，禁止组件内散落 `fetch`。
- `npm run build` 零 error 才能提交；禁止用 `any` 掩盖类型问题，确需使用时加注释说明原因。

## 8. 测试策略

| 层 | 框架 | 门禁命令 |
|---|---|---|
| 后端 | pytest | `uv run pytest -q` |
| 后端覆盖率 | pytest-cov | `uv run pytest --cov=app --cov-fail-under=80` |
| 前端 | 无单测 | `npm run build` + `npm run lint` |

- 后端测试按 `tests/{unit,integration,contract,workflow,domain}` 分层组织（不是镜像 `app/` 目录）；合同分配算法、`framework_graph`、终检逻辑的改动必须带测试。
- 领域引擎封存期间，只允许运行其既有验证套件，不允许"顺手"重写。
- 修改表结构后必须验证：空库与既有库各执行一次 `uv run python -m app.db.init_db` 后 schema 一致。

## 9. 项目类型附加要求：Web 应用

- **环境配置**：本地使用 `.env`（参考 `.env.example`），至少包含 `DATABASE_URL`、`REDIS_URL`、`LLM_API_KEY`、MinIO/S3、MinerU 配置。⚠️ 密钥禁止提交仓库。
- **部署流程**：前端 `npm run build` 产出静态文件托管；后端进程固定两个角色——API（uvicorn/gunicorn）+ Celery worker；数据库结构变更只走 `app/db/schema.py` + `init_db`。
- **服务架构**：前后端分离，前端只经 `/api/v1` 与后端通信；长任务（解析、生成）全部异步化，前端轮询或 SSE 获取进度。
- **数据库配置**：`app/db/schema.py` + `python -m app.db.init_db` 是结构变更的唯一入口，⚠️ 禁止直接手改生产库结构，也禁止引入 Alembic 与之并存。
