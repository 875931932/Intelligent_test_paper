# 全卷命题协调与重复考点防护 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不把来源信息暴露给命题模型的前提下，增加全卷考查原子规划、跨题审查和冲突题局部重生成。

**Architecture:** 蓝图确认后先由一个规划节点生成每道题的唯一考查指令，再由题型生成节点并行命题。规划器维护跨题型共享的全卷卡片使用账本，不在题型边界重置状态。所有题目完成后执行确定性全卷审查和受控语义审查；只有相关冲突题进入局部修复，经过终局确认仍有疑点时才保留 `needs_review` 状态。

**Tech Stack:** Python 3.13, Pydantic, LangGraph, pytest, existing DeepSeek gateway.

---

### Task 1: 建立覆盖计划和题型规则

**Files:**
- Create: `backend/app/domain/generation/coverage.py`
- Modify: `backend/app/schemas/generation.py`
- Test: `backend/tests/unit/test_coverage_planner.py`

- [x] 写覆盖计划的失败测试：重复考查原子必须被拒绝，填空题只能使用 remember/understand，来源字段不得出现在规划载荷。
- [x] 实现 `CoverageDirective`、`build_coverage_directives` 和题型规则校验。
- [x] 运行 `pytest backend/tests/unit/test_coverage_planner.py -q`。

### Task 2: 增加跨题确定性审查

**Files:**
- Modify: `backend/app/services/generation_service.py`
- Test: `backend/tests/unit/test_cross_question_quality.py`

- [x] 写答案出现在另一题题干、答案核心重复和括号过量的失败测试。
- [x] 实现标准化文本、答案签名、覆盖原子冲突和格式规则检查。
- [x] 运行新增质量测试和原有题目质量测试。

### Task 3: 重构 LangGraph 生成图

**Files:**
- Modify: `backend/app/workflows/generation_graph.py`
- Modify: `backend/app/schemas/generation.py`
- Test: `backend/tests/workflow/test_generation_graph.py`

- [x] 写主脑先规划、生成节点收到唯一指令、冲突只修复相关题位的失败测试。
- [x] 增加规划节点、全卷审查节点和局部修复节点，保留题型并行生成。
- [x] 确定性审查确认冲突时直接修复；没有确定性冲突时调用两次独立的精简语义审查，只有双审共识才修复相关题位。
- [x] 运行工作流测试，确认单题失败不会阻断其他题；局部修复达到上限后先做终局确认，只有终局仍有疑点才标记 `needs_review`。

### Task 4: 更新模型表达契约

**Files:**
- Modify: `backend/app/schemas/generation.py`
- Modify: `backend/app/adapters/model/deepseek_gateway.py`
- Test: `backend/tests/unit/test_generation_payload.py`

- [x] 写填空题理论型、常用术语和减少括号的载荷断言。
- [x] 将通用表达规范和 `preferred_terms` 加入纯净载荷，不增加学科专属禁词。
- [x] 运行全部后端测试：`pytest backend/tests -q`（146 passed）。

### Task 5: 固化文档和回归夹具

**Files:**
- Modify: `docs/superpowers/specs/2026-08-14-core-exam-system-development-design.md`

- [x] 将本次问题、根因、架构决策、验收指标和失败状态写入设计文档。
- [x] 检查文档没有来源隔离与教师追溯相互矛盾的描述。

### 验证后的回归约束

- `used_card_ids`、考查原子和答案边界属于全卷状态，选择、判断、填空、简答和综合题节点不得各自从空账本开始。
- 高分题需要多张原子卡时，按考点尚未满足的分值压力与剩余可用卡容量分配；综合题允许在蓝图范围内跨考点，但每个考点必须有独立考查原子、答案边界和分值归属。
- 一张知识卡只对应一个可独立判分的原子事实；`coverage_atom` 必须来自该卡唯一的 `assessable_content`，生成阶段不得拆出卡外原子。可执行蓝图禁止同卡复用；兼容卡不足时应在模型调用前阻断，要求补充知识卡或调整题型/题数。
- 填空题必须预留 `remember`/`understand` 层级并偏向定义、术语、条件和基本结论，不得被应用型题位挤占全部理论基础容量。
- 跨题语义冲突先换入未使用且与冲突对象语义更远的原子卡；仍冲突时重新规划考查原子和答案边界。只改题干、保留原答案核心的“修复”必须判定无效。
- 宽松语义审查采用双审共识，两个独立审查结果均确认冲突才自动修复。达到修复上限后执行终局确认；既有冲突只有连续两次独立阴性确认才可清除，否则相关题位进入教师复核，不能被单次模型波动误判为通过。
- 规划和生成模型都只接收纯净知识及题位合同；教师可见的文件名、页码、章节和证据 ID 留在后端追溯关系中。重复与冲突治理依赖原子卡和全卷账本，不增加课程专属禁词。
