# 按题型难度比例 Implementation Plan

> **For agentic workers:** 本计划已在当前隔离工作树中执行完成。

**Goal:** 将难度从全卷统一属性改为每种题型独立的低/中/高比例，并按题型内部低到高排序。

**Architecture:** 蓝图服务读取每个题型的 `difficulty_distribution`，使用最大余数法转换为题数，生成有序 `PlanItem`。章节分值分配继续独立运行，生成图只读取题位级难度。

**Tech Stack:** Python, Pydantic, pytest, React/TypeScript/Vite.

---

### Task 1: 蓝图领域模型与分配

**Files:** `backend/app/domain/blueprint/models.py`, `backend/app/services/blueprint_service.py`

- [x] 允许题型规则携带 `difficulty_distribution`。
- [x] 校验低/中/高只允许三种键且合计 100%。
- [x] 使用最大余数法计算题型难度题数。
- [x] 按低、中、高顺序构建题位，输出 `difficulty_counts`。

### Task 2: 回归测试

**Files:** `backend/tests/unit/test_blueprint_allocation.py`

- [x] 验证题型内难度数量和排序。
- [x] 验证比例不合计 100% 时阻断。
- [x] 验证未提供比例的旧蓝图兼容为全中等。

### Task 3: 前端蓝图预览

**Files:** `frontend/src/App.tsx`, `frontend/src/styles.css`

- [x] 示例蓝图增加每题型难度比例。
- [x] 蓝图说明明确低→中→高排序和不使用全卷难度。
- [x] 预览显示每个题型实际低/中/高题数。

### Task 4: 文档维护与验证

**Files:** `docs/superpowers/specs/2026-08-14-core-exam-system-development-design.md`

- [x] 记录难度比例规则、排序规则、兼容策略和回归夹具。
- [x] 运行后端全量测试和前端生产构建。
