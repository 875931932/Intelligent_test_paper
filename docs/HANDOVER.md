# AI 期末试卷命题系统 · 交接文档

> 更新日期：2026-08-18
> 状态：核心生成链路已定型并经真实数据验证（final_check 全绿），进入正式系统开发阶段（S1-S4）
> 产品基线：`docs/superpowers/specs/2026-08-12-ai-final-exam-paper-design.md`（v2.3，务必先读）
> 链路设计：`docs/superpowers/specs/2026-08-17-contract-first-generation-design.md`（合同优先生成）

---

## 1. 项目是什么

面向高校教师的**纸质期末试卷生产线**：教师上传课程大纲与教学资料 → 系统整理出知识目录 → 教师确认蓝图与命题合同 → AI 按合同分批出题 → 教师审核编辑 → 导出学生卷/答卷（Word/PDF）与**答案细则 JSON**（评分点/分值/可接受答案结构化输出，供阅卷环节程序化消费——本项目是"阅卷出题"一体，答案细则是阅卷端的直接输入）。

不是"一句话生成整卷"的玩具，而是**可控、可追溯、可审核**的考试资产生产流程。核心纪律：全局约束（不重复、不抄袭、比例对、不冷门）在命题前的"合同"阶段由确定性算法构造性保证，模型只负责写题。

**当前定位**：各学科通用（已去除所有课程拟合点）。演示课程为"SK3020 大模型调优与部署技术"。

---

## 2. 仓库结构与环境

### 2.1 Git Worktree 布局（新人最容易困惑的点）

```
f:\比赛项目\阅卷出题功能\                        ← 主仓库（大纲/素材/设计文档）
└── .worktrees\core-implementation\             ← 开发 worktree（所有代码在此！）
    ├── backend\                                ← FastAPI + 领域引擎（Python）
    ├── frontend\                               ← Vite + React（目前仅 demo 查看器）
    ├── docs\superpowers\                       ← 设计文档与实施计划
    │   └── 素材\                               ← 17 份实验报告 + 2 份大纲 PDF
    ├── docker-compose.dev.yml
    └── .env                                    ← DeepSeek/MinerU/DB 密钥（不入库）
```

日常开发、跑测试、跑 demo 全部在 `core-implementation` worktree 内进行。

### 2.2 环境与启动

```powershell
# 后端（加载 .env，端口 8000）
cd .worktrees\core-implementation\backend
.\start_dev.ps1

# 前端（端口 5173，/api 已代理到 8000）
cd .worktrees\core-implementation\frontend
npx vite --host

# 健康检查（deepseek/mineru 应为 configured）
curl http://127.0.0.1:8000/api/v1/health
```

依赖：PostgreSQL（必须）、Redis（当前健康检查显示 unavailable 但不阻塞链路）、DeepSeek API Key（.env 的 `DEEPSEEK_API_KEY`）、MinerU 服务（文档解析，结果缓存在 `backend\.runtime\mineru`）。

### 2.3 常用命令

```powershell
# 跑 demo 全流程（MinerU 与模型调用均有缓存，改动机制后会真实重跑受影响阶段）
cd backend
python scripts\build_real_material_demo.py

# 全量测试（test_material_service 需 boto3，环境缺依赖时 ignore）
python -m pytest tests\ -q --ignore=tests\unit\test_material_service.py
```

demo 产物：`frontend\public\demo\pipeline.json`（全链路快照：框架/知识树/合同/37 题试卷/终检报告），前端页面直接渲染它。该文件入 git，用于版本间对比。

---

## 3. 系统架构（四层）

```
┌─ 教师工作台（React，待正式新建；现为 demo 查看器）
├─ 应用服务层（FastAPI API + Celery Worker + 试卷项目服务[待建]）
├─ 领域引擎 ★已验证·封存不动★
│   ├─ 框架引擎    framework_graph       双大纲解析 → 考点表（权重/锚点/操作政策）
│   ├─ 资料整理引擎 organization_graph   批式分类 → 事实抽取 → 语义画像 → 知识目录
│   └─ 命题引擎    generation_graph      蓝图 → 合同 → 分批生成 → 终检
└─ 基础设施（PostgreSQL+pgvector / S3 / 队列 / 模型网关[LLM+解析均适配器可替换]）
```

数据主线：`课程空间 → 资料库(四区) → 命题框架版本(冻结) → 知识目录(内容域→考核单元→知识卡↔证据) → 试卷项目 → 蓝图 → 试卷合同 → 生成运行 → PaperVersion[待建] → 导出[待建]（学生卷/答卷 PDF + 答案细则 JSON）`

关键数据边界（设计文档 §2.1，已落地）：**出题模型只见纯净知识卡**（原子/答案域/禁用上下文/卡片名），来源关系（文件名/页码/证据ID）由后端在生成后回链，绝不进模型请求。

---

## 4. 核心链路详解（demo 脚本 = 活文档）

`backend\scripts\build_real_material_demo.py` 是全链路的可执行规格。七个阶段：

| 阶段 | 实现位置 | 要点 |
|---|---|---|
| 1 解析 | MinerU 适配器 | 块级解析，缓存命中零成本 |
| 2 框架 | `workflows/framework_graph.py` | 教学大纲→主题树；考核大纲→考点表（EP1-EP7，权重 5/25/35/5/10/15/5，来源 assessment_syllabus） |
| 3 分类 | `deepseek_semantic_extractors.py`（分类器） | **批式**：每资料 1 次调用判全部考点（42 次→6 次的降本关键） |
| 4 抽取 | demo 脚本 fact_prompt + `validate_extracted_facts` | 目标数 = ceil(权重×1.2)；不足则**补抽**（带已有事实清单对全部证据二轮抽取，语义 key 去重） |
| 5 画像 | 语义画像批 | 产出 concept_cluster / answer_proposition / relation_edges / instance_carriers，随卡片持久化 |
| 6 蓝图+合同 | `blueprint_service.py` + `contract_service.py` | 见 §5 机制清单 |
| 7 生成+终检 | `workflows/generation_graph.py` + `generation_service.py` | 按考点分批(≤6题)并行；终检五项 |

### 4.1 合同优先为什么重要（历史教训）

旧链路"生成后审计→修复循环"治不了语义重复：每次调用只见自己那题。新链路把**原子选择、答案域互斥、禁用上下文、原型轮换**全部在命题前的合同分配阶段用确定性算法算死，生成阶段零跨题协调。模型调用从 ~50 次/卷降到 ~12 次。

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

同类机制：**多子句原子按"；"切分**（填空题承载不了双子句语义，切分后子句是子串、证据包含判定不受影响）；**自包含归属限定**（"eval_batch_size参数…"→"大模型评测中，eval_batch_size参数…"，归属只能来自证据语境，防无主语碎片）。

### 5.3 生成三道防线（单题失败不阻塞整卷）

| 防线 | 触发 | 行为 |
|---|---|---|
| 单题重试 ≤2 | 校验失败 | 带失败原因单题重出 |
| 换原子兜底 | 重试耗尽 | 从同考点未用原子换一个重出（排除原原子、保持全卷互斥），成功则采用替换合同 |
| 批缺失恢复 | 批调用漏题 | 漏题也走重试链，三道全失守才标 needs_review |

### 5.4 其他已校准细节

- **难度关键词豁免**：低难度题干含"比较/分析"等词，若该词同时出现在合同原子原文中→是被考查术语本身，不拦截（`generation_service.py validate_generated_question(atom_text=...)`）
- **综合题原型池教师可控**：`type_rules.comprehensive.archetypes` 白名单（文科可只留 case_analysis 等），轮换起点受 allocation_seed 扰动；非法名过滤、空池回退全池
- **原型模板去课程化**：`archetypes.py` 所有模板不预设课程领域，场景以 prompt_material 为准
- **画像字段持久化**：knowledge_cards 表的 concept_cluster / answer_proposition / prompt_material 三列（曾因发布时丢弃导致后端链路防重复机制静默退化——这是一个深刻教训：**改机制必须检查 demo 和后端两条链路**）

---

## 6. 代码地图

```
backend\app\
├─ domain\generation\
│   ├─ contract.py          ★合同领域模型：PoolAtom/聚类/贪心分配/互斥/门槛
│   ├─ archetypes.py        综合题 8 原型契约（模板+材料形式+认知序列）
│   └─ batching.py          按考点分批(≤6)，子批携带禁用上下文
├─ services\
│   ├─ contract_service.py  ★合同分配器：配额→门槛→聚类→分配→禁用上下文→原型轮换
│   ├─ blueprint_service.py 蓝图：type_rules→plan_items（难度/认知/考查方式分布）
│   ├─ generation_service.py 单题校验(题型schema/来源话术/难度) + 全卷终检
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
│   └─ deepseek_semantic_extractors.py  分类/归并/大纲提取（含多子句切分）
├─ schemas\generation.py    批载荷编译（compile_batch_generation_payload）
└─ db\schema.py             全部表结构（knowledge_cards 含画像三列）

backend\scripts\build_real_material_demo.py   ★demo 全流程（活文档）
backend\tests\                                640+ 单测（domain/workflow/unit/integration 四层）
frontend\src\App.tsx                          demo 查看器（合同→生成→试卷展示）
```

---

## 7. 当前状态

### 已完成（引擎层，勿动）

- ✅ 全链路真实数据验证：37 题 / 100 分 / ~12 次模型调用 / final_check 全绿 / 0 needs_review
- ✅ 考点比例严格等于考纲权重（25/35/10/15/5/5/5）
- ✅ 原子不重复（唯一+互斥构造性保证）、语义簇分散、答案不互泄
- ✅ 640+ 单测全绿（boto3 缺失的 test_material_service 除外，属环境问题）
- ✅ 后端 API 骨架：materials/framework/knowledge/blueprints/generation + Celery worker + outbox 幂等恢复

### 未完成（= 正式开发计划 S1-S4，见 §8）

- ❌ 前端仅为 demo 查看器，无教师工作台
- ❌ demo 链路未完全走正式 API（部分逻辑在脚本内，需收口进服务边界）
- ❌ PaperVersion 结构化内核 / 两次确认流 / 富文本编辑器
- ❌ 导出（学生卷/答卷 docx→PDF + 版式检查；答案细则 JSON）
- ❌ 权限最小化与审计事件硬化

### 已知问题（不阻塞，接手时留意）

1. `tests\unit\test_material_service.py` 因环境缺 boto3 无法收集（与代码无关）
2. Redis 未连接：健康检查黄，当前链路不依赖；S4 硬化时补
3. EP3（继续预训练）等池稀缺考点，同簇判断题可能到 3-4 题（互不相邻，属供给数学极限；根治靠补资料而非改算法）
4. 直接 `python -m uvicorn` 启动不加载 .env，必须用 `start_dev.ps1`
5. 偶发 `Fact top-up failed: DeepSeekGatewayError`：补抽网络失败，非致命（首轮结果继续用）

---

## 8. 正式开发计划（S1-S4，已与负责人确认方向）

| 阶段 | 内容 | 完成标准 |
|---|---|---|
| **S1 服务化收口** | 生成链路从 demo 脚本搬进正式 API 边界（上传→整理→发布→蓝图→合同→生成全走服务端任务）；教师工作台骨架（课程空间/资料区/整理进度） | 浏览器端到端走通一次完整出卷，无脚本参与；demo 退位为回归脚本 |
| **S2 试卷项目与审核** | PaperVersion 结构化内核、两次确认流（蓝图合同确认→候选整卷→最终确认）、待处理清单（needs_review 处置）、富文本编辑器（结构化为唯一事实源） | 审核编辑排序确认全流程可操作 |
| **S3 导出** | 同一 PaperVersion 的两类投影：①学生卷/答卷 docx→PDF（院校模板、一致性+版式检查）；②答案细则 JSON（题号→标准答案→评分点[{point, score, acceptable_answers, deduction}]→扣分说明→证据状态，schema 校验后导出） | 两者同源一致；学生卷无答案泄露；JSON 通过 schema 校验且可被阅卷端直接解析 |
| **S4 硬化验收** | 权限最小化、审计事件、幂等恢复补全；按设计文档 §12 的 27 条必测场景逐项回归 | 验收标准 12 条全过 |

> **与设计文档 §9.4 的差异（2026-08-18 决定）**：答案细则导出由 Word/PDF 改为 **JSON**——评分点/分值/可接受答案是结构化数据，JSON 可被阅卷环节直接程序化消费（客观题自动判分、主观题评分点辅助），也免去无意义的排版成本；学生卷与答卷仍为 Word/PDF。设计文档该节后续更新时同步修订。

**引擎层铁律**：S1-S4 只在引擎外面包服务与界面，`domain/generation`、`workflows`、整理链路的内部机制**不重构、不"顺手优化"**——它们是被 640+ 测试和真实数据锁定的成品。发现疑似 bug 先写测试复现，再最小修复。

---

## 9. 开发约定（血泪经验）

1. **双链路同步**：机制改动必须同时落 demo 脚本与后端（consolidator/service），只改一边=另一边静默退化（画像字段丢失事故的教训）。
2. **机制通用性自检**：改任何过滤/选择逻辑前问一句"这对任何学科都成立吗？"禁止出现课程专属词表、针对某张试卷的特判。
3. **改动三件套**：改机制 → 补单测锁定新行为 → 跑 demo 全流程验证 + 全量测试。
4. **改 prompt 后缓存失效**：模型调用缓存 key 含 prompt 内容，改 prompt 会触发对应阶段真实重跑（费钱费时），改前想清楚。
5. **不拟合单卷**：验收标准是"重跑两次原子组合不同且都全绿"，不是"这张卷子好看"。
6. **测试命名即文档**：中文注释写清"为什么"（根因/反例/防误伤），后来者靠测试理解机制边界。

---

## 10. 文档索引

| 文档 | 位置 | 内容 |
|---|---|---|
| 产品设计基线 v2.3 | `..\..\docs\superpowers\specs\2026-08-12-ai-final-exam-paper-design.md`（主仓库） | 产品对象/权限/数据边界/P0-P5/27条必测场景（**接手必读**） |
| 合同优先生成设计 | `docs\superpowers\specs\2026-08-17-contract-first-generation-design.md` | 命题引擎重构的完整设计 rationale |
| 实施计划存档 | `docs\superpowers\plans\` | 历轮迭代的实施记录 |
| 试卷快照 | `frontend\public\demo\pipeline.json` | 最近一次 demo 产物（git 有历史版本可对比） |

---

## 11. 联系上下文

- 演示课程素材：`docs\素材\`（考核大纲权重 5/25/35/5/10/15/5 是所有配额测试的现实锚点）
- 模型：DeepSeek（.env `DEEPSEEK_MODEL`，默认 deepseek-v4-flash）；文档解析 MinerU
- demo 每次运行会打印 `Contract allocation seed: <n>`——复现某张卷子时在代码里固定该种子即可
