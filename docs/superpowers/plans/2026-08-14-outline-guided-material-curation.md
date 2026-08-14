# 大纲驱动资料提炼实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让教学大纲的“教学内容与要求”和考核大纲的范围约束实际进入每个教学材料文件的知识点整理，并阻止指令、实验来源和操作噪声成为可出题内容。

**Architecture:** 已确认的命题框架作为每个单文件资料整理请求的只读范围约束。整理模型同时接收该文件的原文、文件名、页码/章节和内容块 ID，以输出可追溯的知识卡；入库时将纯净知识内容与来源关系分字段保存。生成试卷只读取当前题位的已确认知识卡，来源关系由后端用于教师查看依据，不进入出题模型请求。原型不维护无模型的离线知识点回退；未配置 DeepSeek 时整理明确失败，避免用假候选验证流程。

**Tech Stack:** Python 标准库、DeepSeek JSON 调用、unittest。

---

### Task 1: 锁定大纲上下文契约

**Files:**
- Modify: `prototype/test_server.py`
- Modify: `prototype/server.py`

- [x] 先写测试：材料整理提示词包含已确认框架的教学内容/考核要求摘要，并保留当前教学材料的文件名、页码/章节和内容块 ID 以建立来源关系。
- [x] 运行单测并确认当前实现因缺少 `framework_context_for_organization` 失败。
- [x] 实现仅输出 `name`、`scope_text`、`outline_kind` 的框架摘要函数，并将其传入单文件整理任务和模型提示词。
- [x] 运行单测确认通过。

### Task 2: 锁定知识卡提纯契约

**Files:**
- Modify: `prototype/test_server.py`
- Modify: `prototype/server.py`

- [x] 先写测试：候选只有“安装、下载、运行、截图、提交、按实验手册完成”等指令内容时，不得进入候选知识库。
- [x] 运行单测确认当前实现未满足该约束。
- [x] 使候选必须至少包含一条去来源、可笔试考查的 `assessable_content`；过滤指令和来源泄漏后为空的候选直接拒绝。
- [x] 运行单测确认通过。

### Task 3: 固化设计与回归门槛

**Files:**
- Modify: `docs/superpowers/specs/2026-08-12-ai-final-exam-paper-design.md`
- Modify: `prototype/test_server.py`

- [x] 在主设计文档记录“大纲仅作准入而不进入材料整理上下文”与“生成阶段读取来源/原始证据而不是知识卡”的失败模式、数据边界及正式开发验收规则。
- [x] 验证生成请求只包含当前题位的纯净知识卡；来源关系由后端直接按知识点绑定供教师查看，不在生成阶段重新从原文自由检索。
- [x] 运行 `python -m unittest test_server.py`、`python -m py_compile server.py`、`node --check static/app.js`；`smoke_test.ps1` 仍假设无模型离线兜底，不作为本原型模型链路验收。

### Task 4: 修复大纲长请求导致的空内容失败

**Files:**
- Modify: `prototype/test_server.py`
- Modify: `prototype/server.py`
- Modify: `prototype/static/app.js`
- Modify: `docs/superpowers/specs/2026-08-12-ai-final-exam-paper-design.md`

- [x] 以真实诊断日志确认 `organize_outline` 的 reasoning 消耗了输出额度，导致 JSON 内容为空或被截断。
- [x] 为大纲阶段关闭 thinking/reasoning，并按单文件内容块小批调用；批次不跨文件，结果合并同名锚点及来源 ID。
- [x] 增加批次回归测试、错误原因展示和设计文档记录；使用两份真实大纲完成独立模型重试验证。
