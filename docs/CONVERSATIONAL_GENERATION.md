# 对话式出卷：接线建议

> 状态：提案（未实现）。
> 目标：教师用自然语言提要求（"题型比例改 4:3:3"、"整体再难点"、"别老出重复题"），
> 系统把要求落成**结构化参数**并即时干跑预览，确认后再写库。
> 权威约束见 `AGENTS.md` 核心纪律第 1 条与第 3 条。

---

## 0. 一条不可动摇的原则

**对话产出的是结构化参数 diff，不是 prompt 指令。**

- 题型比例、章节权重、难度分布、去重 —— 这些全局约束由**确定性算法**
  在蓝图 / 合同两个阶段构造性保证（`allocate_plan_items` + `allocate_paper_contract`）。
- ⚠️ 禁止把"比例对、难度对、不重复"塞进 prompt 让模型自觉遵守。
- ⚠️ 禁止为对话式改动 `app/workflows/generation_graph.py`（已验证封存）。
- LLM 的职责始终只有一件事：**按合同把题干、选项、答案写出来**。

---

## 1. 现有链路（对话要挂在哪一层）

```
资料库 → 命题框架(冻结) → 知识目录 → 试卷项目
        → 蓝图 Blueprint  → 合同 Contract → 生成 Generation → PaperVersion → 导出
           ↑可干跑预览        ↑可干跑预览      ★封存不可动★      ↑教师编排
```

四个可接锚点，按上游→下游排列：

| # | 层 | 对话可调什么 | 结构化落点 | 干跑成本 |
|---|---|---|---|---|
| 1 | **蓝图参数** | 题型比例、章节权重、总分、题量 | `BlueprintRequest.type_rules` / `.chapter_weights` / `.total_score` | 极低（纯函数） |
| 2 | **合同参数** | 难度、重复度、综合题原型偏好 | `centrality_threshold`、`allocation_seed`、`_HISTORY_RUN_LIMIT`、`type_rules.comprehensive.archetypes` | 低（一次 DB 读 + 确定性分配） |
| 3 | **题位计划** | 某题更难 / 更偏应用 | plan_items 的 `difficulty` / `cognitive_level` / `assessment_mode` | 低（confirm 前有效） |
| 4 | **试卷编排** | 生成后的增删改覆写 | `paper_versions` 子端点（已存在） | 已实现 |

### 各锚点的既有实现位置

**锚点 1 · 蓝图**
- `blueprint_service.allocate_plan_items(request) -> BlueprintPlan`（纯函数，最大余数 + 按章权重 DP）
- `type_rules` 三级来源：客户端下发 → 框架 `exam_rules` 推导 → `_DEFAULT_TYPE_RULES`
  （100 分默认：单选 15×2 / 判断 10×1 / 填空 10×2 / 简答 4×5 / 综合 2×10）
- confirm 时会**防御性重跑**一次分配校验，比例/难度/章权重不通过就不冻结。

**锚点 2 · 合同**
- `contract_execution_service.allocate_with_fallback(...)`：阈值 0.6 → 0.5 → 0.45 回退，
  DB 全量构建只做一次，每轮回退仅 `model_copy` 替换 `centrality_threshold`。
- `_collect_used_atom_texts`：取该课程最近 10 份合同的原子做**跨卷避重**（窗口即"重复度"旋钮）。
- `_comprehensive_archetype_pool`：综合题原型白名单由 `type_rules.comprehensive.archetypes` 控制。

**锚点 3 · 题位计划**
- `PUT .../plan-items`（§8.6），confirm 之前改动都会被重跑校验兜住。

---

## 2. 推荐架构

```
用户自然语言
   ↓
intent 解析（新增 service，产出参数 diff）
   ↓
复用既有"只预览不落库"端点干跑
   ↓
返回可读摘要：「比例改 4:3:3 后 → 单选 12 道 / 判断 9 道 / 填空 9 道，A1 章 27 分」
   ↓
用户确认 → 走既有 draft / allocate 落库
```

现成的"只预览"范式：`PATCH .../contracts/revise`（`revise_contract_preview`）——
不落库、返回结果供前端展示，直接照抄这个模式即可。

### 职责边界

| 组件 | 职责 | 不做什么 |
|---|---|---|
| intent 解析 | 自然语言 → `BlueprintRequest` / `ContractRequest` 的字段 diff | 不碰题干内容、不写 prompt |
| 干跑预览 | 调纯函数算出题位分布，翻译成人话 | 不写库 |
| 既有 service | 校验 + 落库 + 冻结 | 不感知"这是对话来的" |
| generation_graph | 照旧按合同写题 | **完全不动** |

---

## 3. 实施顺序（建议）

1. **第一步：蓝图参数对话**（锚点 1）
   纯函数 + 可即时干跑预览，风险最低、反馈最快，能最快验证整套交互。
2. **第二步：合同参数对话**（锚点 2）
   引入难度 / 重复度 / 原型三个旋钮，需要给用户解释阈值含义。
3. **第三步：题位级对话**（锚点 3）
   逐题位微调，复用既有 plan-items 端点。
4. **试卷编排对话**（锚点 4）已有端点，只需包一层 intent 解析。

---

## 4. 红线自查清单

- [ ] 比例 / 难度 / 去重 / 答案域互斥 —— 是否仍由分配器构造性保证？
- [ ] 是否有任何对话输入绕过合同直接进 prompt？
- [ ] 是否改动了 `framework_graph` / `organization_graph` / `generation_graph`？
- [ ] 冻结后的命题框架 / 已确认合同 —— 是否只追加新版本而非原地修改？
- [ ] 所有查询是否仍带 `course_id`？
- [ ] 无修订的默认路径 —— 是否仍然零次额外全量 DB 重建？
