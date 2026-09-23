# AI 期末试卷命题系统 · 交接文档

> 更新日期：2026-09-22
> 状态：引擎层 + 教师工作台 + 试卷模块均已交付；出卷全链路可在浏览器端走通
> 产品基线：`docs/superpowers/specs/2026-08-12-ai-final-exam-paper-design.md`（v2.3，务必先读）
> 链路设计：`docs/superpowers/specs/2026-08-17-contract-first-generation-design.md`（合同优先生成）
> 接口权威清单：`docs/backend-api.md`；代码全景：`CODE_WIKI.md`

---

## 1. 项目是什么

面向高校教师的**纸质期末试卷生产线**：教师上传课程大纲与教学资料 → 系统整理出知识目录 →
教师确认蓝图与命题合同 → AI 按合同分批出题 → 教师审核编辑 → 导出学生卷/答卷与**答案细则 JSON**
（评分点/分值/可接受答案结构化输出，供阅卷环节程序化消费——本项目是"阅卷出题"一体，
答案细则是阅卷端的直接输入）。

不是"一句话生成整卷"的玩具，而是**可控、可追溯、可审核**的考试资产生产流程。核心纪律：
全局约束（不重复、不抄袭、比例对、不冷门）在命题前的"合同"阶段由确定性算法构造性保证，
模型只负责写题。

**当前定位**：各学科通用（已去除所有课程拟合点）。演示课程为"SK3020 大模型调优与部署技术"，
考纲素材在 `docs/素材/`（考核大纲权重 5/25/35/5/10/15/5 是所有配额测试的现实锚点）。

---

## 2. 仓库结构与环境

单仓库、单分支开发（代码就在仓库根，没有 worktree 陷阱）：

```
f:\比赛项目\阅卷出题功能\
├── backend\                 # FastAPI + 领域引擎（Python 3.12）
├── frontend\                # Vite + React 19 + TypeScript 教师工作台
├── deploy\                  # install/start/restart/stop 脚本 + nginx 模板
├── docs\                    # 项目文档（API 清单 / 部署 / 交接）与素材
├── CODE_WIKI.md             # 代码全景
└── .env                     # 密钥（不入库）
```

### 2.1 本地启动

```powershell
# 后端（自动加载仓库根 .env，端口 8000）
cd backend
.\start_dev.ps1

# Celery Worker（真实生成必须）
celery -A app.infrastructure.tasks.celery_app.celery_app worker --loglevel=INFO

# 前端（端口 5173，/api 已代理到 8000）
cd frontend
npm ci --registry=https://registry.npmmirror.com
npm run dev
```

健康检查：`curl http://127.0.0.1:8000/api/v1/health`（llm/mineru 应为 configured）。

依赖：PostgreSQL（必须）、Redis（Celery broker，真实生成必须）、LLM API Key、
MinerU（文档解析，结果缓存在 `backend\.runtime\mineru`）。
对象存储用 MinIO；不可用时回退本地存储（`PUT /api/v1/_local-storage/{key}`）。

### 2.2 部署

Ubuntu 服务器部署见 [`docs/DEPLOY_UBUNTU.md`](DEPLOY_UBUNTU.md)。一键脚本：

```bash
sudo bash deploy/start.sh      # 初始化 DB → 起 API + Worker → 构建前端
sudo bash deploy/restart.sh
sudo bash deploy/stop.sh
```

部署后最小验收顺序：`/api/v1/health` 四项依赖均可用 → 创建课程 → 上传一份文件 →
MinerU 解析 → 双大纲框架确认 → 知识目录发布 → 蓝图/合同确认 → 生成并审核一张候选试卷。
生成任务必须观察到 `queued → running → succeeded/failed`，并核对 `model_calls` 有
`paper_generation` 记录。

---

## 3. 系统架构（四层）

```
┌─ 教师工作台（React 19 + TS，7 个页面路由）
├─ 应用服务层（FastAPI API + Celery Worker + outbox 派发）
├─ 领域引擎 ★已验证·封存不动★
│  ├─ 框架引擎    framework_graph       双大纲解析 → 考点表（权重/锚点/考试规则）
│  ├─ 资料整理引擎 organization_graph   批式分类 → 事实抽取 → 语义画像 → 知识目录
│  └─ 命题引擎    generation_graph      蓝图 → 合同 → 分批生成 → 终检
└─ 基础设施（PostgreSQL / S3 / 队列 / 模型网关[LLM+解析均适配器可替换]）
```

数据主线：
`课程空间 → 资料库(四区) → 命题框架版本(冻结) → 知识目录(内容域→考核单元→知识卡↔证据)
→ 试卷项目 → 蓝图 → 试卷合同 → 生成运行 → PaperVersion → 导出(学生卷/答卷 HTML + 答案细则 JSON)`

**前端一个「试卷」模块承载后半程**：项目详情页两个页签——「出卷流水线」（蓝图→合同→生成，
`pages/paper/PipelinePanel.tsx`）与「试卷」（查看/编辑/定稿/导出，`pages/paper/PaperPanel.tsx`，
左题号索引 + 右题目详情的双栏阅读器）。审核编辑**不在**流水线阶段里。

关键数据边界（设计文档 §2.1，已落地）：**出题模型只见纯净知识卡**（原子/答案域/禁用上下文/
卡片名），来源关系（文件名/页码/证据ID）由后端在生成后回链，绝不进模型请求。

---

## 4. 核心链路详解（demo 脚本 = 活文档）

`backend\scripts\build_real_material_demo.py` 是全链路的可执行规格。七个阶段：

| 阶段 | 实现位置 | 要点 |
|---|---|---|
| 1 解析 | MinerU 适配器 | 块级解析，缓存命中零成本 |
| 2 框架 | `workflows/framework_graph.py` | 教学大纲→主题树；考核大纲→考点表 + **考试规则**（题型比例/章节权重） |
| 3 分类 | `llm_semantic_extractors.py`（分类器） | **批式**：每资料 1 次调用判全部考点（42 次→6 次的降本关键） |
| 4 抽取 | demo fact_prompt + `validate_extracted_facts` | 目标数 = ceil(权重×1.2)；不足则**补抽**（带已有事实清单对全部证据二轮抽取，语义 key 去重） |
| 5 画像 | 语义画像批 | 产出 concept_cluster / answer_proposition / relation_edges / instance_carriers，随卡片持久化 |
| 6 蓝图+合同 | `blueprint_service.py` + `contract_service.py` | 见 §5 机制清单 |
| 7 生成+终检 | `workflows/generation_graph.py` + `generation_service.py` | 按考点分批(≤6题)并行；终检五项 |

### 4.1 合同优先为什么重要（历史教训）

旧链路"生成后审计→修复循环"治不了语义重复：每次调用只见自己那题。新链路把**原子选择、
答案域互斥、禁用上下文、原型轮换**全部在命题前的合同分配阶段用确定性算法算死，生成阶段
零跨题协调。模型调用从 ~50 次/卷降到 ~12 次。

### 4.2 考核规则的链路（新增，2026-09）

考纲写明的"选择题占 20%…"与"第1章 5%…"命题权重表，现在**全链路可达**：

```
考纲 PDF → 提取提示词要求填 final_exam_rules
        → domain/framework/exam_rules.py 归一化（题型名映射英文枚举、比例归一到 100）
        → 框架 payload 持久化（发布时继承）
        → 接口顶层 exam_rules（GET current / PATCH rules）
        → 前端「考核规则」卡可查看可修改
        → 蓝图：type_rules 按题型比例推导（总分精确闭合）；chapter_weights 优先取考纲声明值
```

> 引擎铁律依旧：这套归一化是**确定性**的，不交给模型；模型只负责从考纲里把数字读出来。

---

## 5. 关键质量机制清单（历轮打磨，接手后请勿轻易改动）

这些机制全部是**通用抽象**（数量驱动/语法特征驱动/模型自产信号驱动），不带任何课程硬编码。每条都有单测锁定。

### 5.1 原子供给与选择

| 机制 | 位置 | 解决的问题 |
|---|---|---|
| 抽取目标 = ceil(权重×1.2) ≈ 配额×1.7 | demo `target_fact_count` | 池子必须明显大于题位配额，否则每卷被迫选同一批原子（"每张卷都在考 eval_batch_size"的根因） |
| 补抽机制 | demo topup 调用 | 首轮不足目标时二轮抽取 |
| 聚类三信号：bigram Jaccard>0.5 + 共享英文术语锚 + **相同 concept_cluster 标签** | `contract.py cluster_pool_atoms` | 纯中文枚举类原子（"提示词要素"系列）字面相似度极低且无英文锚，只有画像标签能识别为同簇 |
| 种子扰动分配 | `contract.py _pick_atom` 评分元组末位 + `ContractRequest.allocation_seed` | 并列打破随机化：同种子复现同卷，异种子换原子组合（A/B 卷）；不传=确定性 |
| 答案域互斥 | `boundaries_overlap`（归一化后相等或互含≥4字符） | 防两题答案可互抄 |
| 核心度门槛阶梯 0.6→0.5→0.45 | demo 分配循环 | 0.45 是打分函数数学地板（基准0.5-最大罚分0.05），不静默降级也不硬失败 |

### 5.2 事实质量三道入库防线（知识卡是 RAG 检索库源头，污染即后患）

| 防线 | 机制 | 位置 |
|---|---|---|
| 正面引导 | 抽取/归并 prompt 要求"可迁移的通用知识，剥离情境" | demo fact/topup prompt、consolidator prompt |
| 确定性校验 | 情境绑定正则：指示词(上一轮/本轮/本次/我们的…)+≤12字符+运行词(实验/训练/微调/运行/实践) → 拒绝入库 | `relevance.py SITUATIONAL_BINDING_LANGUAGE` + `is_transferable_fact` |
| 汇聚点兜底 | 卡片组装时逐条过滤来源话术+情境绑定 | demo `source_free_card` |

同类机制：**多子句原子按"；"切分**（填空题承载不了双子句语义，切分后子句是子串、证据包含判定
不受影响）；**自包含归属限定**（"eval_batch_size参数…"→"大模型评测中，eval_batch_size参数…"，
归属只能来自证据语境，防无主语碎片）。

### 5.3 生成三道防线（单题失败不阻塞整卷）

| 防线 | 触发 | 行为 |
|---|---|---|
| 单题重试 ≤2 | 校验失败 | 带失败原因单题重出 |
| 换原子兜底 | 重试耗尽 | 从同考点未用原子换一个重出（排除原原子、保持全卷互斥），成功则采用替换合同 |
| 批缺失恢复 | 批调用漏题 | 漏题也走重试链，三道全失守才标 needs_review |

### 5.4 单题 schema 校验（答案口径，2026-09 补齐）

`generation_service.validate_generated_question` 是单题质量的确定性门禁：

- 单选：四个选项 + 答案**唯一对应一个选项**（历史上模型返回 "AB" 导致"单选题里出现多选"）
- 多选：至少四个选项 + 答案对应**两个及以上**选项
- 判断：答案必须是布尔值（后端契约），且该题型**没有 options 字段**
- 填空/简答/综合/论述：答案非空；主观题还必须有解析和评分细则
- 答案解析统一走 `answer_option_keys`：兼容字母（`B`/`ABD`）、选项原文、多个原文并列
  （`甲、丙`）三种形态——模型三种都会给。纯字母才按字母解析，避免把 `LoRA` 里的
  L/O/R/A 误当成选项字母。

### 5.5 其他已校准细节

- **难度关键词豁免**：低难度题干含"比较/分析"等词，若该词同时出现在合同原子原文中→是被考查
  术语本身，不拦截（`generation_service.py validate_generated_question(atom_text=...)`）
- **综合题原型池教师可控**：`type_rules.comprehensive.archetypes` 白名单（文科可只留
  case_analysis 等），轮换起点受 allocation_seed 扰动；非法名过滤、空池回退全池
- **原型模板去课程化**：`archetypes.py` 所有模板不预设课程领域，场景以 prompt_material 为准
- **画像字段持久化**：knowledge_cards 表的 concept_cluster / answer_proposition / prompt_material
  三列（曾因发布时丢弃导致后端链路防重复机制静默退化——这是一个深刻教训：**改机制必须检查
  demo 和后端两条链路**）

---

## 6. 代码地图

```
backend\app\
├─ domain\generation\
│   ├─ contract.py          ★合同领域模型：PoolAtom/聚类/贪心分配/互斥/门槛
│   ├─ archetypes.py        综合题 8 原型契约（模板+材料形式+认知序列）
│   └─ batching.py          按考点分批(≤6)，子批携带禁用上下文
├─ domain\framework\
│   ├─ exam_rules.py        ★考核规则归一化 + 按比例推导题型分布
│   ├─ exam_points.py       考点领域模型
│   └─ models.py            AssessmentOutline / FrameworkCandidate …
├─ services\
│   ├─ contract_service.py  ★合同分配器：配额→门槛→聚类→分配→禁用上下文→原型轮换
│   ├─ blueprint_service.py 蓝图：type_rules→plan_items（难度/认知/考查方式分布）
│   ├─ blueprint_persistence_service.py  蓝图持久化 + 默认题型分布（优先考纲比例）
│   ├─ generation_service.py 单题校验(题型schema/来源话术/难度) + 全卷终检 + 答案解析
│   ├─ paper_version_service.py  ★试卷版本内核 + 三份导出渲染（学生卷/答卷/答案细则）
│   ├─ knowledge_publish_service.py  发布：候选→教师确认→原子入库(含画像字段)
│   └─ knowledge_tree_service.py     知识树校验（证据落地/同考点准入）
├─ workflows\
│   ├─ generation_graph.py  ★生成图：批并行→校验→重试→换原子→终检
│   ├─ organization_graph.py 资料整理编排
│   └─ knowledge_catalog_subgraph.py 知识目录构建与校验
├─ domain\knowledge\
│   ├─ relevance.py         ★证据准入/事实落地判定/情境绑定/语义归一化
│   └─ models.py            KnowledgeCardDraft 等领域对象
├─ adapters\model\
│   └─ llm_semantic_extractors.py  分类/归并/大纲提取（含考试规则）
├─ schemas\generation.py    批载荷编译（compile_batch_generation_payload）
└─ db\schema.py             全部表结构（knowledge_cards 含画像三列）

backend\scripts\
├─ build_real_material_demo.py   ★demo 全流程（活文档）
└─ build_pipeline_via_api.py     不依赖模型网关、走全部 API 的结构回归脚本

frontend\src\
├─ pages\paper\             ★「试卷」模块：index(外壳) / PipelinePanel(流水线) / PaperPanel(阅读器)
├─ pages\framework\         命题框架 + ExamRulesCard（考核规则查看/修改）
├─ pages\{dashboard,materials,knowledge}\  概览 / 资料库 / 知识目录
├─ components\layout\       Layout + Sidebar（悬浮岛侧栏）
├── hooks\useNameMaps.ts    id → 中文名映射
├── lib\examDisplay.ts      题型/难度/状态展示常量
└─ api\domains\             按业务域拆分的 fetch 封装
```

---

## 7. 当前状态

### 已完成

- ✅ 引擎层全链路真实数据验证：37 题 / 100 分 / ~12 次模型调用 / final_check 全绿 / 0 needs_review
- ✅ 考点比例严格等于考纲权重、原子不重复（唯一+互斥构造性保证）、语义簇分散、答案不互泄
- ✅ 教师工作台七个页面路由全部接通真实 API（概览/资料库/命题框架/知识目录/试卷）
- ✅ 「试卷」模块：出卷流水线（蓝图→合同→生成）+ 试卷双栏阅读器（查看/编辑/调序/增删/定稿）
- ✅ 考核规则全链路：提取 → 归一化 → 持久化 → 查看/修改 → 蓝图消费（题型比例与章节权重）
- ✅ 三份导出按高校卷面模板渲染：学生卷 / 答卷（信息头 + 题次表 + 装订线 + 答案速查表）/ 答案细则 JSON
- ✅ 后端 pytest 全量（`--ignore=tests/unit/test_material_service.py`）：778 passed / 18 failed
  （18 条均为历史存量失败，与本次改动无关，改动前后用 stash 对比确认过）

### 已知问题（不阻塞，接手时留意）

1. `tests\unit\test_material_service.py` 因环境缺 boto3 无法收集（与代码无关）
2. Redis 未连接时健康检查黄；Celery 不可用则真实生成无法派发（inline_runner 仅测试用）
3. EP3（继续预训练）等池稀缺考点，同簇判断题可能到 3-4 题（互不相邻，属供给数学极限；
   根治靠补资料而非改算法）
4. 直接 `python -m uvicorn` 启动不加载 .env，必须用 `start_dev.ps1`
5. 偶发 `Fact top-up failed: LLMGatewayError`：补抽网络失败，非致命（首轮结果继续用）
6. **旧框架没有考试规则**：本次改动前构建的框架 payload 里 `final_exam_rules` 是空 dict，
   框架页会显示"没有解析出考试规则"并提供「补充规则」；重新构建一次框架即可自动带上考纲比例
7. 前端无单元测试文件，门禁是 `npm run build` + `npm run lint`；端到端行为由后端 pytest 锁定

---

## 8. 开发约定（血泪经验）

1. **双链路同步**：机制改动必须同时落 demo 脚本与后端（consolidator/service），只改一边=另一边
   静默退化（画像字段丢失事故的教训）。
2. **机制通用性自检**：改任何过滤/选择逻辑前问一句"这对任何学科都成立吗？"禁止出现课程专属
   词表、针对某张试卷的特判。
3. **改动三件套**：改机制 → 补单测锁定新行为 → 跑 demo 全流程验证 + 全量测试。
4. **改 prompt 后缓存失效**：模型调用缓存 key 含 prompt 内容，改 prompt 会触发对应阶段真实重跑
   （费钱费时），改前想清楚。
5. **不拟合单卷**：验收标准是"重跑两次原子组合不同且都全绿"，不是"这张卷子好看"。
6. **测试命名即文档**：中文注释写清"为什么"（根因/反例/防误伤），后来者靠测试理解机制边界。
7. **前端不假设接口形态**：旧数据（如空 `final_exam_rules`）会让缺字段的响应打崩渲染，
   API 边界上一律补齐/兜底（`_exam_rules_of` 与 `ExamRulesCard` 都因此加过防御）。

---

## 9. 文档索引

| 文档 | 位置 | 内容 |
|---|---|---|
| 代码全景 | `CODE_WIKI.md` | 架构/领域模型/工作流/API/服务/数据库/前端/测试 |
| 接口权威清单 | `docs/backend-api.md` | 从 FastAPI 路由逐条提取，联调唯一依据 |
| 产品设计基线 v2.3 | `docs/superpowers/specs/2026-08-12-ai-final-exam-paper-design.md` | 产品对象/权限/数据边界/P0-P5/27条必测场景（**接手必读**） |
| 合同优先生成设计 | `docs/superpowers/specs/2026-08-17-contract-first-generation-design.md` | 命题引擎重构的完整设计 rationale |
| 实施计划存档 | `docs/superpowers/plans/` | 历轮迭代的实施记录（历史档案，路径可能已变） |
| 部署 | `docs/DEPLOY_UBUNTU.md` | Ubuntu 部署前置条件与验收顺序 |
| 演示素材 | `docs/素材/` | 考核大纲 + 17 份实验报告 + 答卷/评分标准范本 |

---

## 10. 联系上下文

- 模型：LLM（.env `LLM_MODEL`）；文档解析 MinerU；向量 DashScope qwen embedding
- demo 每次运行会打印 `Contract allocation seed: <n>`——复现某张卷子时在代码里固定该种子即可
- 试卷导出的版式范本在 `docs/素材/`（A卷试卷 / 答卷A卷 / 评分标准A 三件套）
