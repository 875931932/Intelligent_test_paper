# 原型大纲先行命题框架 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让原型中的教学大纲和考核大纲先被主动整理、由教师确认成命题框架，并用该框架约束后续教学材料的知识点候选与发布。

**Architecture:** 保持内存状态和单文件模型调用。大纲文件按文件名/正文线索归类为教学大纲或考核大纲，分别抽取范围锚点；两类大纲都存在时，教师确认候选框架。教学材料整理要求已确认框架，并仅保留能映射到框架锚点的知识点候选。

**Tech Stack:** Python 3.12 标准库 HTTP 服务、`unittest`、静态 HTML/CSS/JavaScript、PowerShell 冒烟测试。

---

### Task 1: 大纲整理与框架确认后端

**Files:**

- Modify: `prototype/server.py`
- Modify: `prototype/test_server.py`

- [ ] 增加独立的 `framework_run`、候选/已确认锚点和框架状态；实现大纲文件识别、逐文件抽取和候选框架生成。
- [ ] 增加 `/api/organize-outline` 与 `/api/confirm-framework`；只有教学大纲和考核大纲均已整理才能确认。
- [ ] 让教学材料整理要求已确认框架，并为候选知识点写入 `framework_anchor_id`；没有可映射锚点的候选不发布。
- [ ] 以单元测试覆盖：缺少双大纲不可确认、未确认框架不可整理教学材料、框架下候选携带锚点。

### Task 2: 大纲优先界面与端到端验证

**Files:**

- Modify: `prototype/static/index.html`
- Modify: `prototype/static/app.js`
- Modify: `prototype/static/app.css`
- Modify: `prototype/smoke_test.ps1`
- Modify: `prototype/README.md`

- [ ] 在“主动整理”前增加“大纲与命题框架”面板，提供整理大纲和确认框架按钮，以及锚点候选预览。
- [ ] 未确认框架时禁用教学材料整理并明确提示；确认后展示当前框架锚点数量。
- [ ] 让冒烟测试先上传并整理双大纲、确认框架，再整理教学材料、发布和构建蓝图；更新 README 的实际流程。

### 验证

- [ ] `python -m py_compile prototype/server.py`
- [ ] `python -m unittest discover -s prototype -p test_server.py -v`
- [ ] `node --check prototype/static/app.js`
- [ ] `pwsh -ExecutionPolicy Bypass -File prototype/smoke_test.ps1`
