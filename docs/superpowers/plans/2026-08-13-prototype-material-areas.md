# 原型资料分区与批量上传 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让质量验证原型支持教师一次选择多个文件，并将文件以“大纲”和“教学材料”两个明确区域保存、展示和组织，避免大纲混入教学材料候选知识库。

**Architecture:** 保持原型的内存状态和标准库 HTTP 服务。浏览器为每个资料区域提供独立的 `multiple` 文件输入，向同一上传端点提交 `material_area`；服务端解析表单字段并把区域写入每个 `material`。资料整理接口只接受教学材料 ID，从服务端阻断大纲与教学材料混选；页面按区域展示文件并提供教学材料多选、全选和清空选择。

**Tech Stack:** Python 3.12 标准库 HTTP 服务、`unittest`、PowerShell 冒烟测试、静态 HTML/CSS/JavaScript。

---

### Task 1: 锁定批量上传表单与资料区域的后端契约

**Files:**

- Modify: `prototype/test_server.py`
- Modify: `prototype/server.py`

- [ ] **Step 1: 写失败测试，要求 multipart 同时保留区域字段和多个文件**

在 `prototype/test_server.py` 中增加测试，以包含 `material_area=outline`、`files=教学大纲.txt`、`files=考核大纲.txt` 的原始 multipart 请求体调用 `parse_multipart_form`，断言字段值为 `outline`、文件按顺序返回两项。

- [ ] **Step 2: 运行失败测试**

Run:

```powershell
& 'C:\Users\Gnahz\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest test_server.StructuredModelRequestTests.test_multipart_form_preserves_area_and_multiple_files -v
```

Expected: FAIL，原因是 `parse_multipart_form` 尚不存在。

- [ ] **Step 3: 实现最小 multipart 表单解析和区域校验**

在 `prototype/server.py` 创建 `parse_multipart_form(content_type, body)`，返回 `{"fields": {"material_area": "..."}, "uploads": [(filename, bytes), ...]}`；保留现有 `parse_multipart_uploads` 作为只取 `uploads` 的兼容包装。定义仅允许 `outline` 与 `teaching_material` 的区域常量和 `normalize_material_area`，缺失字段默认 `teaching_material`，非法值返回 `ValueError`。

- [ ] **Step 4: 运行测试确认通过**

Run:

```powershell
& 'C:\Users\Gnahz\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest test_server.StructuredModelRequestTests.test_multipart_form_preserves_area_and_multiple_files -v
```

Expected: PASS。

### Task 2: 将区域写入资料记录，并阻断错误的整理混选

**Files:**

- Modify: `prototype/test_server.py`
- Modify: `prototype/server.py`

- [ ] **Step 1: 写失败测试，要求大纲选择不能进入教学资料整理**

在 `prototype/test_server.py` 中设置内存 `STATE`，放入一份 `material_area="outline"` 的暂存文件，然后调用 `organize_materials(["outline_1"])`。断言抛出的 `ValueError` 包含“大纲区”。

- [ ] **Step 2: 运行失败测试**

Run:

```powershell
& 'C:\Users\Gnahz\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest test_server.StructuredModelRequestTests.test_organize_rejects_outline_materials -v
```

Expected: FAIL，因为原型仍会把任意暂存文件送入 `organize_materials`。

- [ ] **Step 3: 写入区域字段并在服务端执行边界校验**

更新 `handle_upload`，读取 `material_area` 并把它保存为每个 `material` 的 `material_area`；上传事件记录区域。更新 `organize_materials`：选择中含 `outline` 时明确拒绝，只有 `teaching_material` 可进入当前候选知识库。`state_payload` 保留区域字段供前端渲染。

- [ ] **Step 4: 运行两项后端测试和全部单元测试**

Run:

```powershell
& 'C:\Users\Gnahz\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest test_server -v
```

Expected: 全部 PASS。

### Task 3: 将上传页面改为两个明确区域，并提供教学材料多选控制

**Files:**

- Modify: `prototype/static/index.html`
- Modify: `prototype/static/app.js`
- Modify: `prototype/static/app.css`

- [ ] **Step 1: 将单上传框替换为两个 `multiple` 上传框**

在 `index.html` 保留“大纲与命题依据”区域，提示可一次选择教学大纲和考核大纲；新增“教学材料”区域，提示可一次选择 PPT、PDF、Word、教材章节、实验讲义。两个输入都使用 `multiple`，并保留相同的可接受文件扩展名。

- [ ] **Step 2: 按区域渲染资料，并只让教学材料可勾选整理**

在 `app.js` 中让 `upload(files, materialArea)` 向 `FormData` 写入 `material_area`。按 `material_area` 分别渲染大纲清单与教学材料清单；仅教学材料行拥有 `.material-check`。加入“全选教学材料”和“清空选择”按钮；点击整理前在前端二次校验所选项目均属于教学材料。

- [ ] **Step 3: 添加两区布局与区域标签样式**

在 `app.css` 中添加响应式的 `.upload-area-grid`、`.upload-card`、`.area-label`、`.selection-actions` 样式，不改变现有移动端表单行为。

- [ ] **Step 4: 检查静态脚本语法**

Run:

```powershell
& 'C:\Users\Gnahz\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' --check prototype\static\app.js
```

Expected: exit code `0`。

### Task 4: 扩展冒烟测试和使用说明

**Files:**

- Modify: `prototype/smoke_test.ps1`
- Modify: `prototype/README.md`

- [ ] **Step 1: 扩展冒烟测试为双区域批量上传**

`smoke_test.ps1` 先以 `material_area=outline` 上传同一份夹具的两个副本，再以 `material_area=teaching_material` 上传一份教学夹具；断言返回的材料区域正确。它还必须验证仅传大纲 ID 到 `/api/organize` 获得 400，随后传教学材料 ID 仍可整理、发布和构建蓝图。

- [ ] **Step 2: 更新 README 的实际操作顺序**

说明教师先向“大纲与命题依据”区批量上传教学大纲和考核大纲，再向“教学材料”区批量上传课件/讲义；本次原型已经隔离资料区域和整理入口，但尚未实现完整的双大纲语义框架确认工作流，不能将其误称为生产版 `FrameworkGraph`。

- [ ] **Step 3: 运行全部验证**

Run:

```powershell
& 'C:\Users\Gnahz\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m py_compile prototype\server.py
& 'C:\Users\Gnahz\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s prototype -p test_server.py -v
& 'C:\Users\Gnahz\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' --check prototype\static\app.js
pwsh -ExecutionPolicy Bypass -File prototype\smoke_test.ps1
```

Expected: 编译、单元测试、脚本语法检查和冒烟测试全部通过，末行包含 `SMOKE TEST PASSED`。

## 计划自检

本计划覆盖了当前用户已确认的原型范围：批量文件选择、上传区域分区、后端可追踪的资料类型和防混选整理。它不把完整的教学大纲/考核大纲解析、框架冲突确认、RAG 锚点映射或 LangGraph 持久化伪装为本次已完成；这些仍是主设计文档 v1.7 的后续实现项。
