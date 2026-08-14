# 全卷命题协调与重复考点防护 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不把来源信息暴露给命题模型的前提下，增加全卷考查原子规划、跨题审查和冲突题局部重生成。

**Architecture:** 蓝图确认后先由一个规划节点生成每道题的唯一考查指令，再由题型生成节点并行命题。所有题目完成后执行确定性全卷审查；只有冲突题进入局部修复，最终保留 `needs_review` 状态。

**Tech Stack:** Python 3.13, Pydantic, LangGraph, pytest, existing DeepSeek gateway.

---

### Task 1: 建立覆盖计划和题型规则

**Files:**
- Create: `backend/app/domain/generation/coverage.py`
- Modify: `backend/app/schemas/generation.py`
- Test: `backend/tests/unit/test_coverage_planner.py`

- [x] 写覆盖计划的失败测试：重复考查原子必须被拒绝，填空题只能使用 recall/understand，来源字段不得出现在规划载荷。
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
- [x] 在确定性审查无冲突时调用一次精简语义审查，语义冲突只修复相关题位。
- [x] 运行工作流测试，确认单题失败不会阻断其他题，无法修复的题标记 `needs_review`。

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
