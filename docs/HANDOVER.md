# PaperPact·AI 命题系统 · 交接文档

> 更新日期：2026-10-07
> 状态：引擎层 + 教师工作台 + 试卷模块（含考试侧重点与 AI 落点：考核规则助手 / 蓝图题位建议 / 框架候选评审 / 助手自然语言出卷提案链 / 使用引导卡）均已交付；出卷全链路可在浏览器端走通
> 产品基线：`docs/superpowers/specs/2026-08-12-ai-final-exam-paper-design.md`（v2.3，务必先读）
> 链路设计：`docs/superpowers/specs/2026-08-17-contract-first-generation-design.md`（合同优先生成）
> 接口权威清单：`docs/backend-api.md`；代码全景：`CODE_WIKI.md`；模型调优：`docs/LLM_TUNING.md`

---

## 1. 项目是什么

面向高校教师的**纸质期末试卷生产线**：教师上传课程大纲与教学资料 → 系统整理出知识目录 →
教师确认蓝图与命题合同 → AI 按合同分批出题 → 教师审核编辑 → 导出学生卷/答卷/答题卡与**答案细则 JSON**
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
┌─ 教师工作台（React 19 + TS，9 个页面路由）
├─ 应用服务层（FastAPI API + Celery Worker + outbox 派发）
├─ 领域引擎 ★已验证·封存不动★
│  ├─ 框架引擎    framework_graph       双大纲解析 → 考点表（权重/锚点/考试规则）
│  ├─ 资料整理引擎 organization_graph   批式分类 → 事实抽取 → 语义画像 → 知识目录
│  └─ 命题引擎    generation_graph      蓝图 → 合同 → 分批生成 → 终检
└─ 基础设施（PostgreSQL / S3 / 队列 / 模型网关[LLM+解析均适配器可替换]）
```

数据主线：
`课程空间 → 资料库(四区) → 命题框架版本(冻结) → 知识目录(内容域→考核单元→知识卡↔证据)
→ 试卷项目 → 蓝图 → 试卷合同 → 生成运行 → PaperVersion → 四份导出(学生卷/答卷/答题卡 HTML + 答案细则 JSON，另有 docx×2 同源)`

**前端一个「试卷」模块承载后半程**：项目详情页两个页签——「出卷流水线」（蓝图→合同→生成，
`pages/paper/PipelinePanel.tsx`）与「试卷」（查看/编辑/定稿/导出，`pages/paper/PaperPanel.tsx`，
左题号索引 + 右题目详情的双栏阅读器）。审核编辑**不在**流水线阶段里。
另有**对话页**（`pages/assistant/`）：AI 助手多会话流式问答、只读查询、RAG 资料问答、
使用引导卡，写操作一律走提案卡（教师点确认才由既有接口执行，不绕确认流）。

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

### 4.2 考核规则的链路（新增，2026-09；难度比例 2026-10-05 补齐）

考纲写明的"选择题占 20%…"与"第1章 5%…"命题权重表，现在**全链路可达**：

```
考纲 PDF → 提取提示词要求填 final_exam_rules（题型比例/章节权重/难度比例/考试侧重点）
        → domain/framework/exam_rules.py 归一化（题型名映射英文枚举、比例归一到 100、
          难度 {low,medium,high} 归一到 100 且未声明/非法则省略键）
        → 框架 payload 持久化（发布时继承）
        → 接口顶层 exam_rules（GET current / PATCH rules；难度与 type_formats
          未提供=保留现值、{}=显式清空）
        → 前端「考核规则」卡可查看可修改（含难度低/中/高三档输入）
        → 蓝图：type_rules 按题型比例推导（总分精确闭合；**卡池容量不足时按题型分值
          份额等比缩容**，见 §5.1）；chapter_weights 优先取考纲声明值；
          assessment_focus 与 difficulty_distribution 创建蓝图时逐题型确定性落位
          （最大余数法，题型显式下发优先）→ plan_items.difficulty → 合同槽位 → 成卷三层一致
```

> 引擎铁律依旧：这套归一化是**确定性**的，不交给模型；模型只负责从考纲里把数字读出来。
> 难度未声明的课程沿用历史缺省（蓝图全 `medium`），改规则只影响**新建**蓝图版本（冻结即不可变）。

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
| 蓝图题位容量缩容（2026-10-07） | `exam_rules.type_rules_from_ratios(capacity=...)` + `blueprint_persistence_service._atom_capacity` | 知识卡池可出题位（Σ 考点答案域容量，与合同同口径）小于蓝图题位数时，合同只给领到原子的题位建槽、其余按题号顺序静默丢弃——排在各章末尾的**综合题先全灭**（实测 42 题位 → 29 题、综合 0 道、卷面 45 分）。缩容保持每题型的**分值预算不变**（总分 100 与题型占比不失真），题数按容量比例缩减并吸附"能整除 2·S"的最近合法值（单题分值恒为 0.5 倍数、只升不降、题型不归零）；该课缩容后 = 单选 8×2.5 / 判断 10×2 / 填空 4×2.5 / 简答 4×5 / 综合 2×15 = 28 题 100 分。⚠️ 冻结即不可变：缩容只对**新建**蓝图生效，改动前创建的蓝图需重建后才按比例出卷 |

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
- **难度特征化标准 v1**（2026-10-07，`ae905d3`）：难度从"配额残余裸标签 + prompt 穿透"升级为
  六维特征三档操作定义（D1 认知操作 / D2 知识跨度 / D3 推理步骤 / D4 情境 / D5 信息方式 /
  D6 干扰项），全部判定在代码——`domain/generation/difficulty_standard.py` ①词表归一
  （easy/hard/中文别名→low/medium/high，**补齐终检只认 low、表单写 easy 绕过检查的缺口**）；
  ②`difficulty_spec` 三档规格随 `BatchQuestionSpec` 结构化字段下发（D1 允许集与蓝图
  `_DIFFICULTY_COGNITIVE_WEIGHTS` 同源，**对齐守护测试**防两处漂移）；③低档终检两规则
  （高阶关键词+原子术语豁免、认知超 remember/understand 拦截）确定性判废重试。中高档只随规格
  下发不做文本判定，留问卷 P 值实证校准收紧。封存 `generation_graph` 零改动（盖章先于校验）。
  设计：`docs/superpowers/specs/2026-10-07-difficulty-feature-standard-design.md`（+13 测试）
- **综合题原型池教师可控**：`type_rules.comprehensive.archetypes` 白名单（文科可只留
  case_analysis 等），顺序即偏好序、不参与洗牌；未指定或全非法时回退默认池，
  默认池按 allocation_seed **确定性洗牌**（sha256 排序，`_shuffled_default_pool`）+
  轮换起点平移——同种子复现，异种子整条序列不同（2026-09-27 反馈「默认序太可预测」前，
  固定序下每张卷第 1 道综合题永远是池首原型）；非法名过滤、空池回退全池
- **原型模板去课程化**：`archetypes.py` 所有模板不预设课程领域，场景以 prompt_material 为准
- **画像字段持久化**：knowledge_cards 表的 concept_cluster / answer_proposition / prompt_material
  三列（曾因发布时丢弃导致后端链路防重复机制静默退化——这是一个深刻教训：**改机制必须检查
  demo 和后端两条链路**）
- **难度比例确定性落位**（2026-10-05 根治）：考核规则 `difficulty_distribution`（全卷一份）
  在蓝图创建时**逐题型**注入 type_rules（最大余数法各自配比，题型显式下发永远优先）→
  `plan_items.difficulty` → 合同槽位 → 成卷三层一致；未声明 = 历史缺省全 medium，改规则
  只影响新建蓝图（冻结不可变）。根因曾是 schema 无此字段、引擎支持从未被喂而产出 42 题全
  medium——难度/比例换算永远在代码里、不进 prompt；normalize/注入/API roundtrip 单测锁定

### 5.6 模型调用鲁棒性与运行自愈（2026-09 根治，操作手册 `docs/LLM_TUNING.md`）

| 机制 | 位置 | 解决的问题 |
|---|---|---|
| 抽取输出预算 6144 / 归并预算 8192 | `config.py organization_extraction_max_tokens`、`llm_semantic_extractors._CONSOLIDATION_MAX_TOKENS` | 思考与正文共享 `max_tokens`，顶格截断致 content 恒空（`model_empty_response`，失败 out_tokens 精确卡上限） |
| json_schema strict 结构约束 | `ORGANIZATION_EXTRACTION_JSON_SCHEMA` + `_extraction_strict_schema()` | 模型漏必填字段（`model_schema_validation_failed`）改由协议层保证；`false` 一键回退 json_object |
| 型号调优档案 + effort 档位收敛 | `adapters/model/model_profiles.py` | 供应商参数因型号而异（如 2603 只收 low/high，乱发 400）；换模型只改 `.env`，业务码零改动 |
| temperature=0 响应缓存 | `llm_gateway.request_json` | 同 `(model, prompt_hash)` 复用成功响应，知识目录重建成本趋零；换模型/改 prompt 自动失效 |
| 大 prompt 重试收紧（>60k 字符 → 2 次） | `LLMJsonClient` | 分类批重发一次 = 等额再烧数万 token 输入 |
| 孤儿 run 读路径自愈 | `knowledge.py _heal_interrupted_run` | 重启后线程已死、行卡 `running`，前端永远轮询 200——读取时就地判 `failed(interrupted_by_restart)`；⚠️ 依赖单进程部署，多 worker 须换租约心跳 |
| 单次类 AI 工具思考档显式钉低（2026-10-07） | `AI_TOOL_REASONING_EFFORT`（默认 low）+ `LLMJsonClient` 构造期钉档 + 六服务注入 | 整卷评审/改题/建题/蓝图建议/考核规则提案/框架评审原先只靠 `LLM_DISABLE_THINKING` 推导档案缺省档：该开关为 false 时网关不发档位、模型回落供应商默认 medium，短调用平白多花数倍时间。显式下发优先于开关与档案缺省，换开关/换模型都不漂移（生成/知识目录/助手各有独立档位配置，互不影响） |

---

## 6. 代码地图

```
backend\app\
├─ domain\generation\
│   ├─ contract.py          ★合同领域模型：PoolAtom/聚类/贪心分配/互斥/门槛
│   ├─ archetypes.py        综合题 8 原型契约（模板+材料形式+认知序列）
│   ├─ difficulty_standard.py  ★难度特征标准：词表归一/三档规格下发/低档确定性终检
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
│   ├─ paper_version_service.py  ★试卷版本内核 + 四份导出渲染（学生卷/答卷/答题卡/答案细则）
│   ├─ knowledge_publish_service.py  发布：候选→教师确认→原子入库(含画像字段)
│   ├─ knowledge_tree_service.py     知识树校验（证据落地/同考点准入）
│   ├─ ai_revise_service.py          单题 AI 改题提案（diff→教师确认才落库）
│   ├─ ai_create_service.py          AI 整题生成提案（回填→教师确认）
│   ├─ exam_rules_ai_service.py      考核规则 AI 提案（回填编辑草稿→教师保存）
│   ├─ blueprint_suggest_service.py  蓝图题位 AI 调整建议（统计对照→教师逐条应用）
│   ├─ framework_review_ai_service.py 框架候选 AI 评审报告（只读异步）
│   └─ paper_review_service.py       整卷 AI 质量评审报告（只读异步）
├─ workflows\
│   ├─ generation_graph.py  ★生成图：批并行→校验→重试→换原子→终检
│   ├─ organization_graph.py 资料整理编排
│   └─ knowledge_catalog_subgraph.py 知识目录构建与校验
├─ domain\knowledge\
│   ├─ relevance.py         ★证据准入/事实落地判定/情境绑定/语义归一化
│   └─ models.py            KnowledgeCardDraft 等领域对象
├─ adapters\model\
│   ├─ model_profiles.py    ★型号调优档案：按型号登记供应商参数，换模型只改 .env
│   ├─ llm_gateway.py       网关：档案参数下发 + json_schema strict + 重试/缓存/调用记录
│   └─ llm_semantic_extractors.py  分类/归并/大纲提取（含考试规则）/事实抽取/补料推荐
├─ schemas\generation.py    批载荷编译（compile_batch_generation_payload）
└─ db\schema.py             全部表结构（knowledge_cards 含画像三列）

backend\scripts\
├─ build_real_material_demo.py   ★demo 全流程（活文档）
└─ build_pipeline_via_api.py     不依赖模型网关、走全部 API 的结构回归脚本

frontend\src\
├─ pages\paper\             ★「试卷」模块：index(外壳) / PipelinePanel(流水线) / PaperPanel(阅读器)
│                            + AiRevise(改题) / AiCreate(出题) / PaperReview(整卷评审) / stage\BlueprintSuggestPanel(蓝图建议) 面板
├─ pages\framework\         命题框架 + ExamRulesCard（规则查看/修改 + AI 助手提案）+ FrameworkReviewPanel（AI 评审）
├─ pages\assistant\         对话页：多会话 / 流式问答（think+delta）/ 提案卡确认制 / RAG 来源卡 / 使用引导卡 / 停止生成
├─ pages\paper-archive\     资料库「试卷」文件夹的归档快照编辑页（与试卷页同款双栏阅读器）
├─ pages\{dashboard,materials,knowledge, course-space}\  概览 / 资料库 / 知识目录 / 课程空间
├─ components\layout\       Layout + Sidebar（悬浮岛侧栏）
├── hooks\useNameMaps.ts    id → 中文名映射
├── lib\examDisplay.ts      题型/难度/状态展示常量 + 卷面题号映射 questionNumbers（每题型从 1）
└─ api\domains\             按业务域拆分的 fetch 封装
```

---

## 7. 当前状态

### 已完成

- ✅ 引擎层全链路真实数据验证：37 题 / 100 分 / ~12 次模型调用 / final_check 全绿 / 0 needs_review
- ✅ 考点比例严格等于考纲权重、原子不重复（唯一+互斥构造性保证）、语义簇分散、答案不互泄
- ✅ 教师工作台九个页面路由全部接通真实 API（登录/课程空间/概览/资料库/命题框架/知识目录/试卷/归档编辑/对话助手）
- ✅ 「试卷」模块：出卷流水线（蓝图→合同→生成）+ 试卷双栏阅读器（查看/编辑/调序/增删/定稿）
- ✅ **AI 助手落点重构**（2026-09，提案式、不绕确认流）：移除合同槽位解释；新增考核规则
  AI 助手（一句话提案→回填编辑草稿→教师保存）、蓝图题位 AI 调整建议（后端确定性统计对照
  →教师逐条「应用」走既有 PATCH；教师指令的难度比例由后端换算目标分布，「整卷清单给全、
  应用后恰好达标」由代码门禁判定）、框架候选 AI 评审（确认发布前的只读报告）；原有单题改题
  （提案→diff→确认/撤销）、AI 整题生成（回填表单）、整卷质量评审保留（`docs/backend-api.md`
  §4.9、§4.10、§8.7b、§9.3e–g）
- ✅ 考核规则全链路：提取 → 归一化 → 持久化 → 查看/修改（含考试侧重点 `assessment_focus`
  五项权重，预设+微调；含难度比例 `difficulty_distribution`，低/中/高三档 UI）→
  蓝图消费（题型比例、章节权重与侧重点**确定性**折算题位考查方式分布，无实操可考单元
  两层收敛、出卷不失败；难度**逐题型确定性落位**，见下条）
- ✅ 四份导出按高校卷面模板渲染：学生卷 / 答卷（信息头 + 题次表 + 装订线 + 答案速查表）/
  答题卡 / 答案细则 JSON（schema 1.1.0 起逐题带 `rubric`、1.2.0 起带卷面题号 `no`，
  生成→编辑→导出全链路贯通）；**卷面题号每种题型从 1 重新计数**（2026-10-03，前端与四份
  HTML/两份 docx 导出同口径；`item_index` 保持全局唯一只作内部 id——蓝图/合同的题位表仍
  全局 1..N，题位 ≠ 卷面题号）；另有试卷整体预览与综合题分问排版；档案卡「一键打包」把六份导出产物
  （docx×2 + HTML×3 + JSON，同源渲染）打成 `试卷包_v{n}.zip` 一次下载（§9.10）
- ✅ 知识目录鲁棒性根治（2026-09-24/25）：run 行先于内容落库、失败标记补插兜底、
  孤儿 run 读路径自愈（`interrupted_by_restart`）、构建轮询止损/基线回退、嵌入索引键双契约
- ✅ 模型调优体系（2026-09-25，StepFun 官方文档核对）：型号调优档案独立成档
  `model_profiles.py` + 抽取 `json_schema` strict 结构约束 + 输出预算重校准（抽取 6144 /
  归并 8192）+ 召回阈值 min_score 0.30——手册 `docs/LLM_TUNING.md`
- ✅ 助手 v2/v3 演进（2026-09-29 ~ 10-03，接口权威见 `docs/backend-api.md` §10）：资料内容
  RAG 问答（多查询混合检索 + 命中块邻域扩展捞回正文 + 来源卡，语义打分下推 PG 向量不出库）；
  嵌入输入清洗版本化（`embedding_text_version` 全库重嵌——实测清洗对排序无增益，正文捞回
  靠邻域扩展）；多会话与停止生成（协作式取消）；使用引导卡（`usage_guide` 能力地图 +
  出卷六步跳转）；**自然语言出卷提案链**（`PROPOSAL_TOOLS` 12 个，按项目状态逐级发卡：
  创建项目 → 考核规则 → 蓝图 → 蓝图建议 → 确认蓝图/合同 → 生成；教师要求有确定性落点表，
  难度比例等换算不交模型）；意图阶段流式推理（SSE `think` 事件实时展示）+ 气泡 Markdown；
  整卷 AI 评审接入助手提案卡
- ✅ 期间修复（2026-10-01 ~ 10-03）：一键补证据保留教师手动 supplement 不再静默丢弃
  （existing 过滤与注释意图相反的根因）、蓝图建议面板 StrictMode 下刷新恢复失效、进入合同
  阶段先落库蓝图确认、生成状态双读（助手按 `generation_task_status` 判停 + 前端徽章细分
  生成中/待生成/生成失败）、概览/资料库 hero 卡排版根治
- ✅ **难度比例全链路根治**（2026-10-05，`63bb227`）：根因 = exam_rules schema 无难度字段，
  蓝图引擎的 `difficulty_distribution` 支持从未被喂、缺键默认全 medium。落点：归一化接难度
  （剔坏项再缩放）→ `PATCH /rules` 未提供=保留现值、`{}`=显式清空 → 蓝图创建**逐题型注入**
  （最大余数法各题型各自配比，题型显式下发优先）→ 合同槽位 → 成卷三层保真；前端考核规则卡
  三档输入、AI 提案白名单与助手 `update_exam_rules` 同口径、考纲提取 prompt 补字段（+9 测试）。
  E2E 实测（step-3.7-flash）：规则 20/60/20 → 题位 8/25/9（单选 2/6/2、判断 4/12/4、
  填空 1/3/1 逐题型精确）；`conceptual=85` → 题位考查方式 conceptual 37/42；两道代码综合题
  按 archetype 落位；42 题 3.2 分钟、16 次生成调用零重试零失败、>0.72 查重零命中
  （对照：同指令在旧代码产出 42 全 medium——同一助手同一句话，修复前后各落一次）
- ✅ **dispatch 失败留痕**（2026-10-05，`092363b`）：outbox 加载相位 `logger.exception`
  重抛 + 发布失败 warning；exam_projects 两处静默 `except` 补带 course/task 上下文的
  `logger.exception`——此前派发失败无日志可查（01:27/01:32 事件成悬案），现首次复现
  即可从 api.log 定位
- ✅ **生成任务收尾口径**（2026-10-05，`21312db`/`d44e324`）：收尾只看任务 owner +
  进度心跳续租（长任务不被误判 stale 抢跑）；终态耗时按服务端完成时刻收表，不再随页面
  打开时长虚增；生成思考档钉死 `generation_reasoning_effort=low`
- ✅ **助手气泡与时间线重做**（2026-10-06，`c60c1be`）：单头像接力 + 详情弹窗 + 阶段深链
- ✅ **稳定性与体验修复**（2026-10-06）：
  - 删课程 500 根治（`2f9c5fc`）：改逆依赖序 + 先解 FK 环；
  - 删试卷项目不再被归档/助手对话外键挡死（`01b5dcd`）：引用字段先置空、课程级资产保留；
  - 课程被删后资料列表停止无限轮询 404（`6eb7707`）；
  - **管理员建号**（`d72bcfb`）：`POST /auth/users` 仅管理员（教师 403/未登录 401），
    用户名≥3 位、密码≥6 位、姓名必填，重名 409；登录页去掉测试账号提示
- ✅ **AI 发起的蓝图建议恢复可执行**（2026-10-06，`9e001de`）：读超时 45s 写死而实测单次
  40~50s，两次尝试全超时 → `llm_transport_error`、任务失败；改为可配
  `BLUEPRINT_SUGGEST_MODEL_TIMEOUT`（默认 150s = 租约 300s ÷ 2 次尝试）
- ✅ **知识目录检索查询集确定性装配**（2026-10-06，`52b93d5`）：考点名恒进查询 +
  retrieval_intent + 「考点名：考核要求」三路合并（候选规模由 top_k 封顶，退役 expand 开关）；
  多路查询的词法分用各自原文打分——修复 retrieval_intent 被框架模型同填一句空话时的
  零召回与考点覆盖不足
- ✅ **知识目录批量并发与档位统一**（2026-10-06，`29073f7`/`5e65b09`）：分类批次并发 + 短代理
  id + 思考档钉低，并发额度 4（批量）+1（助手）用满账号 5；归并钉 low，并发口径统一为
  `ORGANIZATION_MODEL_MAX_WORKERS`
- ✅ **前端加载可靠性**（2026-10-06，`91ccb9a`/`2f69937`/`467669c`）：nginx 静态资源 gzip +
  `/assets/` 长缓存（immutable）+ `index.html` no-cache；路由 chunk 拉取失败退避重试
  （300ms/900ms）+ 整页 reload 兜底；`/assets/` 缺失 chunk 明确 404，不再落回 index.html
  触发 MIME 报错
- ✅ **蓝图题位容量缩容**（2026-10-07，`3fc3c1a`）：见 §5.1 行——卡池容量不足时题型按分值
  份额等比缩容（总分 100 与题型占比不变、单题分值只升不降、综合题不再全灭）；容量口径与
  合同 `_point_capacity` 一致，纠正循环兜题数吸附的进位误差（+5 测试）
- ✅ **助手卷面口径**（2026-10-07，`06b7ac2`）：`_paper_summary` 补成卷实际构成
  `item_count/total_score/by_type`（逐题解析口径与试卷页 `get_paper_version` 一致：
  题型 override→payload、分值 override→plan_items→payload、无槽位题回落 payload）；
  段1 提示词硬规则「卷面实况一律读 paper，蓝图=计划题位不得顶替」；「试卷」结果卡加
  题量与题型分布两列。根因：模型只能拿蓝图计划数字回答卷面（实测蓝图综合题 3 道、
  成卷 0 道，模型答「3 道」）
- ✅ **单次类 AI 工具思考档钉低**（2026-10-07，`e715845`）：见 §5.6 行（评审/改题/建题/
  蓝图建议/考核规则提案/框架评审统一 `AI_TOOL_REASONING_EFFORT=low`）
- ✅ **AI 助手能力与收口话术**（2026-10-07）：① 复核/建议类能力全部内置为提案工具——新增
  `enqueue_framework_review`（框架 AI 评审 → §4.10；课程级、有框架才发卡、不受项目状态牵连），
  与既有 `enqueue_paper_review` / `enqueue_blueprint_suggest` / `update_exam_rules` 组成完整
  检查链（改题/建题/定稿导出仍按设计引导到对应页面）；② 段1 提示词加两条红线：复核要求直接
  发对应卡、工具覆盖不到时**不得臆造工具名或发空动作**（action 置 null，说明并给手动出路）；
  ③ 两次校验失败的兜底文案改为「我暂时无法回答这个问题，您可以手动操作看看。」——原始错误
  （如 `action.tool 缺失`）只留日志，不再暴露给教师；段2 问答同样按该话术诚实收口
- ✅ **难度特征化标准 v1**（2026-10-07，`ae905d3`）：见 §5.5 行——六维特征三档操作定义落成
  `domain/generation/difficulty_standard.py`（词表归一补 easy 缺口 + `difficulty_spec` 任务卡
  结构化下发 + 低档终检两规则 + 蓝图配对表对齐守护测试），封存 `generation_graph` 零改动；
  设计 `docs/superpowers/specs/2026-10-07-difficulty-feature-standard-design.md`，
  作品方案 v5 已写入对应章节（三(一)6 + 问卷校准闭环）
- ✅ 后端门禁全绿：`uv run pytest -q` **1504 passed / 1 xfailed**（唯一 xfail=编造检测的
  联合 bigram 阈值已知缺口，测试 docstring 注明根因）+ 覆盖率 **86.69%**（≥80 门禁，
  2026-10-07 实测）；前端 `npm run build` 0 error、
  oxlint 0 error（warning 均为既有文件基线）

### 已知问题（不阻塞，接手时留意）

1. Redis 未连接时健康检查黄；Celery 不可用则真实生成无法派发（inline_runner 仅测试用）
2. EP3（继续预训练）等池稀缺考点，同簇判断题可能到 3-4 题（互不相邻，属供给数学极限；
   根治靠补资料而非改算法）
3. 直接 `python -m uvicorn` 启动不加载 .env，必须用 `start_dev.ps1`（或 `uv run uvicorn`）
4. 偶发 `Fact top-up failed: LLMGatewayError`：补抽网络失败，非致命（首轮结果继续用）
5. **旧框架没有考试规则**：构建改动前的框架 payload 里 `final_exam_rules` 是空 dict，
   框架页会显示"没有解析出考试规则"并提供「补充规则」；重新构建一次框架即可自动带上考纲比例
6. 前端无单元测试文件，门禁是 `npm run build` + `npm run lint`；端到端行为由后端 pytest 锁定
7. **github（origin）push 曾连续 443 超时**（2026-09 历史问题，现已恢复双远端同步）：再遇
   超时时 gitee 是上游，网络恢复后 `git push origin main` 补推，避免服务器从 github 拉到旧代码
8. 孤儿 run 自愈依赖**单进程部署**（`_ACTIVE_ORG_RUN_IDS` 进程内存态）：改多 worker 须换
   租约心跳，否则跨进程误杀活跃 run（`knowledge.py` 注释有说明）
9. **解析侧结构信号缺口（待根治）**：全库 `content_blocks` 的 `heading_path` 非空 = 0、
   `block_type=title` = 0——MinerU 回传的块类型/标题层级没进归一化
   （`document_processing_service`），RAG 丢章节结构信号；正文捞回目前靠命中块邻域扩展兜底。
   根治路径：抓 `document_artifacts` 原始 content_list 确认回传 type → 修 `normalize_content_list`
   / `_extract_text` → 全库重解析重嵌 → 复跑 RAG 对照

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
| 模型调优手册 | `docs/LLM_TUNING.md` | 型号档案/旋钮速查/换模型流程/故障速查（`model_calls.details`） |
| 对话式出卷提案 | `docs/CONVERSATIONAL_GENERATION.md` | 接线建议 + 红线自查清单；出卷提案链已按其红线部分落地（干跑预览未实现） |
| 产品设计基线 v2.3 | `docs/superpowers/specs/2026-08-12-ai-final-exam-paper-design.md` | 产品对象/权限/数据边界/P0-P5/27条必测场景（**接手必读**） |
| 合同优先生成设计 | `docs/superpowers/specs/2026-08-17-contract-first-generation-design.md` | 命题引擎重构的完整设计 rationale |
| 难度特征标准设计 | `docs/superpowers/specs/2026-10-07-difficulty-feature-standard-design.md` | 六维特征 D1~D6 / 三档操作定义 / 四段管线 / v1 范围与 v2 展望（问卷实证校准） |
| 实施计划存档 | `docs/superpowers/plans/` | 历轮迭代的实施记录（历史档案，路径可能已变） |
| 部署 | `docs/DEPLOY_UBUNTU.md` | Ubuntu 部署前置条件与验收顺序 |
| 演示素材 | `docs/素材/` | 考核大纲 + 17 份实验报告 + 答卷/评分标准范本 |

---

## 10. 联系上下文

- 模型：LLM（.env `LLM_MODEL`，**换模型只改这里**——型号调优档案 `model_profiles.py`，
  手册 `docs/LLM_TUNING.md`）；文档解析 MinerU；向量 DashScope qwen embedding
- demo 每次运行会打印 `Contract allocation seed: <n>`——复现某张卷子时在代码里固定该种子即可
- 试卷导出的版式范本在 `docs/素材/`（A卷试卷 / 答卷A卷 / 评分标准A 三件套）
