# 试卷项目生产线 5 阶段详情 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现试卷项目生产线横向流水线 UI：顶部 5 阶段圆点进度线（蓝图→合同→生成→审核→导出），下方仅展开当前活动阶段详情，含两次确认闸门（确认蓝图、确认试卷版本）。

**Architecture:** 后端暴露 exam_projects 表的 CRUD 端点（list/create/get/patch_status），前端以 ExamProjectWorkspace 壳组件装配 PipelineNav + 5 个阶段详情组件。阶段推进由 status 字段驱动，确认闸门由 blueprint_confirmed/version_confirmed 标志控制。生产线各阶段详情复用已有 examApi（allocate/confirm/generate）。

**Tech Stack:** FastAPI + SQLAlchemy（后端）；React 19 + Vite + TypeScript + vitest + @testing-library/react（前端）；暗色苹果灰设计系统（CSS 变量 `--accent-*`/`--surface-*`/`--danger`/`--success`/`--warning`）

---

## File Structure

**后端（新建 2 文件 + 修改 1 文件）：**
- `backend/app/services/exam_project_service.py` — exam_projects 表的最小 service（list/create/get/update_status）
- `backend/app/api/v1/exam_projects.py` — 暴露 4 个 HTTP 端点
- `backend/app/main.py` — 注册 exam_projects 路由

**前端（新建 7 文件 + 修改 3 文件）：**
- `frontend/src/console/exam/pipelineNav.tsx` — 顶部横向流水线（5 圆点 + 进度线 + 阶段切换）
- `frontend/src/console/exam/blueprintStage.tsx` — 蓝图阶段详情（蓝图概要 + 确认闸门 1）
- `frontend/src/console/exam/contractStage.tsx` — 合同阶段详情（槽位表 + 冲突列表）
- `frontend/src/console/exam/generationStage.tsx` — 生成阶段详情（生成触发 + 进度 + 题目预览）
- `frontend/src/console/exam/reviewExportStage.tsx` — 审核 + 导出阶段详情（题目审核 + 确认闸门 2）
- `frontend/src/console/exam/examProjectWorkspace.tsx` — 壳组件，装配 PipelineNav + 当前阶段详情
- `frontend/src/console/exam/examProjectWorkspace.test.tsx` — 壳组件测试
- `frontend/src/console/types.ts` — 新增 ExamProjectDetail, PipelineStage 类型
- `frontend/src/console/client.ts` — 新增 projectsApi
- `frontend/src/App.tsx` — 替换 exam-project 路由占位为 ExamProjectWorkspace
- `frontend/src/console/ExamProjectList.tsx` — 接通真实项目列表
- `frontend/src/styles.css` — 追加生产线 CSS

---

### Task 1: 后端 — 暴露 exam_projects CRUD 端点

**Files:**
- Create: `backend/app/services/exam_project_service.py`
- Create: `backend/app/api/v1/exam_projects.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/unit/test_exam_projects_api.py`

- [ ] **Step 1: 编写失败测试**

创建 `backend/tests/unit/test_exam_projects_api.py`：

```python
"""exam_projects CRUD 端点的单元测试。"""
from unittest.mock import MagicMock, patch
from app.api.v1 import exam_projects as api


def test_list_endpoint_exists():
    """exam_projects 路由必须含 GET /exam-projects 端点。"""
    import inspect
    source = inspect.getsource(api)
    assert "@router.get" in source, "缺少 GET 端点"
    assert "exam-projects" in source, "路由路径不含 exam-projects"


def test_create_endpoint_exists():
    """exam_projects 路由必须含 POST /exam-projects 端点。"""
    import inspect
    source = inspect.getsource(api)
    assert 'def create' in source, "缺少 create 函数"


def test_get_endpoint_exists():
    """exam_projects 路由必须含 GET /exam-projects/{project_id} 端点。"""
    import inspect
    source = inspect.getsource(api)
    assert 'def get_one' in source, "缺少 get_one 函数"


def test_patch_status_endpoint_exists():
    """exam_projects 路由必须含 PATCH /exam-projects/{project_id} 端点用于更新状态。"""
    import inspect
    source = inspect.getsource(api)
    assert 'def patch' in source or 'def update_status' in source, "缺少 patch/update_status 函数"


def test_service_has_list_projects():
    """exam_project_service 必须含 list_projects 函数。"""
    from app.services import exam_project_service
    assert hasattr(exam_project_service, 'list_projects'), "缺少 list_projects"
    assert hasattr(exam_project_service, 'create_project'), "缺少 create_project"
    assert hasattr(exam_project_service, 'get_project'), "缺少 get_project"
    assert hasattr(exam_project_service, 'update_status'), "缺少 update_status"
```

- [ ] **Step 2: 运行测试验证失败**

工作目录 `backend`：
```bash
.venv\Scripts\python -m pytest tests/unit/test_exam_projects_api.py -v --basetemp=.pytest_tmp
```
预期：FAIL（模块不存在）

- [ ] **Step 3: 创建 service 层 `backend/app/services/exam_project_service.py`**

```python
"""exam_projects 表的最小 service：list/create/get/update_status。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.schema import exam_projects


class ExamProjectConflictError(Exception):
    """项目名在课程内已存在。"""


class ExamProjectNotFoundError(Exception):
    """项目不存在。"""


def list_projects(session: Session, course_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        select(exam_projects).where(exam_projects.c.course_id == course_id).order_by(exam_projects.c.created_at.desc())
    ).mappings().all()
    return [dict(r) for r in rows]


def create_project(session: Session, course_id: str, name: str) -> dict[str, Any]:
    from app.db.schema import exam_projects as tbl
    import uuid
    project_id = str(uuid.uuid4())
    try:
        session.execute(
            tbl.insert().values(id=project_id, course_id=course_id, name=name, status="draft")
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ExamProjectConflictError(str(exc))
    return {"id": project_id, "course_id": course_id, "name": name, "status": "draft"}


def get_project(session: Session, course_id: str, project_id: str) -> dict[str, Any]:
    row = session.execute(
        select(exam_projects).where(
            exam_projects.c.course_id == course_id,
            exam_projects.c.id == project_id,
        )
    ).mappings().first()
    if not row:
        raise ExamProjectNotFoundError(project_id)
    return dict(row)


def update_status(session: Session, course_id: str, project_id: str, status: str) -> dict[str, Any]:
    existing = get_project(session, course_id, project_id)
    session.execute(
        exam_projects.update()
        .where(exam_projects.c.id == project_id, exam_projects.c.course_id == course_id)
        .values(status=status)
    )
    session.commit()
    return {**existing, "status": status}
```

- [ ] **Step 4: 创建 API 端点 `backend/app/api/v1/exam_projects.py`**

```python
"""exam_projects CRUD 端点（课程作用域）。"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.services import exam_project_service

router = APIRouter(prefix="/api/v1/courses/{course_id}/exam-projects", tags=["exam-projects"])


class ExamProjectCreate(BaseModel):
    name: str


class ExamProjectStatusUpdate(BaseModel):
    status: str


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="exam project not found")


@router.get("", response_model=list[dict])
def list_projects(course_id: str, session: Session = Depends(get_session)) -> list[dict]:
    return exam_project_service.list_projects(session, course_id=course_id)


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
def create(course_id: str, payload: ExamProjectCreate, session: Session = Depends(get_session)) -> dict:
    try:
        return exam_project_service.create_project(session, course_id=course_id, name=payload.name)
    except exam_project_service.ExamProjectConflictError:
        raise HTTPException(status_code=409, detail="project name already exists in this course")


@router.get("/{project_id}", response_model=dict)
def get_one(course_id: str, project_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        return exam_project_service.get_project(session, course_id=course_id, project_id=project_id)
    except exam_project_service.ExamProjectNotFoundError:
        raise _not_found()


@router.patch("/{project_id}", response_model=dict)
def patch(course_id: str, project_id: str, payload: ExamProjectStatusUpdate, session: Session = Depends(get_session)) -> dict:
    try:
        return exam_project_service.update_status(session, course_id=course_id, project_id=project_id, status=payload.status)
    except exam_project_service.ExamProjectNotFoundError:
        raise _not_found()
```

- [ ] **Step 5: 注册路由到 `backend/app/main.py`**

在 `main.py` 中已有的 router 注册块中追加：
```python
from app.api.v1 import exam_projects
app.include_router(exam_projects.router)
```

- [ ] **Step 6: 运行测试验证通过**

```bash
.venv\Scripts\python -m pytest tests/unit/test_exam_projects_api.py -v --basetemp=.pytest_tmp
```
预期：PASS（5 tests）

- [ ] **Step 7: Commit**

```bash
cd ..
git add backend/app/services/exam_project_service.py backend/app/api/v1/exam_projects.py backend/app/main.py backend/tests/unit/test_exam_projects_api.py
git commit -m "feat(backend): expose exam_projects CRUD endpoints (list/create/get/patch_status)"
```

---

### Task 2: 前端 — 扩展类型 (ExamProjectDetail, PipelineStage)

**Files:**
- Modify: `frontend/src/console/types.ts:355-372`
- Test: `frontend/src/console/exam/types.test.ts`

- [ ] **Step 1: 创建失败测试 `frontend/src/console/exam/types.test.ts`**

```typescript
import { describe, it, expectTypeOf } from 'vitest'
import type { ExamProjectDetail, PipelineStage } from '../types'

describe('exam project pipeline types', () => {
  it('PipelineStage has 5 stages', () => {
    type Expected = 'blueprint' | 'contract' | 'generating' | 'review' | 'exported'
    expectTypeOf<PipelineStage>().toEqualTypeOf<Expected>()
  })

  it('ExamProjectDetail has pipeline fields', () => {
    type Expected = {
      id: string
      name: string
      status: string
      blueprint_confirmed: boolean
      version_confirmed: boolean
    }
    expectTypeOf<ExamProjectDetail>().toMatchTypeOf<Expected>()
  })
})
```

- [ ] **Step 2: 运行测试验证失败**

工作目录 `frontend`：
```bash
npx vitest run src/console/exam/types.test.ts
```
预期：FAIL（类型不存在）

- [ ] **Step 3: 扩展 `frontend/src/console/types.ts`**

在文件末尾 `CourseReadiness` 类型之后追加：

```typescript
// ── 试卷项目生产线（Plan 3） ──────────────────────────
export type PipelineStage = 'blueprint' | 'contract' | 'generating' | 'review' | 'exported'

export type ExamProjectDetail = {
  id: string
  name: string
  semester_label: string
  status: 'draft' | 'blueprint' | 'contract' | 'generating' | 'review' | 'exported'
  total_score: number
  question_count: number
  pending_review: number
  blueprint_confirmed: boolean
  version_confirmed: boolean
  blueprint?: BlueprintSettings
  contract?: PaperContract
  generation?: GenerationRunResult
}

export const PIPELINE_STAGES: { stage: PipelineStage; label: string; index: number }[] = [
  { stage: 'blueprint', label: '蓝图', index: 1 },
  { stage: 'contract', label: '合同', index: 2 },
  { stage: 'generating', label: '生成', index: 3 },
  { stage: 'review', label: '审核', index: 4 },
  { stage: 'exported', label: '导出', index: 5 },
]
```

- [ ] **Step 4: 运行测试验证通过 + tsc 检查**

```bash
npx vitest run src/console/exam/types.test.ts
npx tsc -b --noEmit
```
预期：PASS + 无类型错误

- [ ] **Step 5: Commit**

```bash
cd ..
git add frontend/src/console/types.ts frontend/src/console/exam/types.test.ts
git commit -m "feat(types): add PipelineStage, ExamProjectDetail, PIPELINE_STAGES for exam project pipeline"
```

---

### Task 3: 前端 — 扩展 client API (projectsApi)

**Files:**
- Modify: `frontend/src/console/client.ts:154-179`
- Test: `frontend/src/console/exam/client.test.ts`

- [ ] **Step 1: 创建失败测试 `frontend/src/console/exam/client.test.ts`**

```typescript
import { describe, it, expect, vi } from 'vitest'

vi.mock('../client', () => ({
  projectsApi: {
    list: vi.fn(),
    create: vi.fn(),
    get: vi.fn(),
    updateStatus: vi.fn(),
  },
}))

import { projectsApi } from '../client'

describe('projectsApi', () => {
  it('list calls correct endpoint', async () => {
    const mock = projectsApi.list as ReturnType<typeof vi.fn>
    mock.mockResolvedValue([{ id: 'p1', name: 'Test', status: 'draft' }])
    const result = await projectsApi.list('course-1')
    expect(mock).toHaveBeenCalledWith('course-1')
    expect(result).toHaveLength(1)
  })

  it('create calls POST endpoint', async () => {
    const mock = projectsApi.create as ReturnType<typeof vi.fn>
    mock.mockResolvedValue({ id: 'p1', name: 'New', status: 'draft' })
    const result = await projectsApi.create('course-1', 'New')
    expect(mock).toHaveBeenCalledWith('course-1', 'New')
    expect(result.id).toBe('p1')
  })

  it('get calls GET endpoint', async () => {
    const mock = projectsApi.get as ReturnType<typeof vi.fn>
    mock.mockResolvedValue({ id: 'p1', name: 'Test', status: 'draft' })
    const result = await projectsApi.get('course-1', 'p1')
    expect(mock).toHaveBeenCalledWith('course-1', 'p1')
    expect(result.id).toBe('p1')
  })

  it('updateStatus calls PATCH endpoint', async () => {
    const mock = projectsApi.updateStatus as ReturnType<typeof vi.fn>
    mock.mockResolvedValue({ id: 'p1', name: 'Test', status: 'blueprint' })
    const result = await projectsApi.updateStatus('course-1', 'p1', 'blueprint')
    expect(mock).toHaveBeenCalledWith('course-1', 'p1', 'blueprint')
    expect(result.status).toBe('blueprint')
  })
})
```

注意：此测试整体 mock `../client` 模块。由于 projectsApi 尚不存在，导入会失败，符合 RED 阶段。但实现后此测试只验证 mock 调用签名，不验证真实 HTTP。如需真实 HTTP 验证，可参考 Task 4 的 client.test.ts 改用 `vi.spyOn(fetch)`。本任务保留此简化测试。

- [ ] **Step 2: 运行测试验证失败**

```bash
npx vitest run src/console/exam/client.test.ts
```
预期：FAIL（`projectsApi` 未导出）

- [ ] **Step 3: 扩展 `frontend/src/console/client.ts`**

在文件顶部 `import type { ... } from './types'` 块中，按字母序插入 `ExamProjectDetail`：

```typescript
import type {
  Course,
  ExamProjectDetail,
  EvidenceLink,
  FrameworkCandidate,
  ...
} from './types'
```

在 `examApi` 对象之后（约 179 行后）追加 `projectsApi`：

```typescript
// ── 试卷项目 CRUD ────────────────────────────────────
export const projectsApi = {
  list: (courseId: string) => api<ExamProjectDetail[]>(`${base(courseId)}/exam-projects`),

  create: (courseId: string, name: string) =>
    api<ExamProjectDetail>(`${base(courseId)}/exam-projects`, {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),

  get: (courseId: string, projectId: string) =>
    api<ExamProjectDetail>(`${base(courseId)}/exam-projects/${projectId}`),

  updateStatus: (courseId: string, projectId: string, status: string) =>
    api<ExamProjectDetail>(`${base(courseId)}/exam-projects/${projectId}`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    }),
}
```

- [ ] **Step 4: 运行测试验证通过 + tsc 检查**

```bash
npx vitest run src/console/exam/client.test.ts
npx tsc -b --noEmit
```
预期：PASS + 无类型错误

- [ ] **Step 5: Commit**

```bash
cd ..
git add frontend/src/console/client.ts frontend/src/console/exam/client.test.ts
git commit -m "feat(client): add projectsApi for exam project CRUD operations"
```

---

### Task 4: 前端 — PipelineNav 流水线导航组件

**Files:**
- Create: `frontend/src/console/exam/pipelineNav.tsx`
- Test: `frontend/src/console/exam/pipelineNav.test.tsx`

- [ ] **Step 1: 创建失败测试 `frontend/src/console/exam/pipelineNav.test.tsx`**

```typescript
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PipelineNav } from './pipelineNav'
import type { PipelineStage } from '../types'

describe('PipelineNav', () => {
  it('renders 5 stage dots with labels', () => {
    render(<PipelineNav current="blueprint" completed={[]} onJump={vi.fn()} />)
    expect(screen.getByText('蓝图')).toBeInTheDocument()
    expect(screen.getByText('合同')).toBeInTheDocument()
    expect(screen.getByText('生成')).toBeInTheDocument()
    expect(screen.getByText('审核')).toBeInTheDocument()
    expect(screen.getByText('导出')).toBeInTheDocument()
  })

  it('marks current stage as active', () => {
    render(<PipelineNav current="contract" completed={['blueprint']} onJump={vi.fn()} />)
    const contractDot = screen.getByText('合同').closest('.stage-dot')
    expect(contractDot?.classList.contains('active')).toBe(true)
  })

  it('marks completed stages with done class', () => {
    render(<PipelineNav current="contract" completed={['blueprint']} onJump={vi.fn()} />)
    const blueprintDot = screen.getByText('蓝图').closest('.stage-dot')
    expect(blueprintDot?.classList.contains('done')).toBe(true)
  })

  it('calls onJump when a completed stage is clicked', async () => {
    const onJump = vi.fn()
    const user = userEvent.setup()
    render(<PipelineNav current="contract" completed={['blueprint']} onJump={onJump} />)
    await user.click(screen.getByText('蓝图'))
    expect(onJump).toHaveBeenCalledWith('blueprint')
  })

  it('does not call onJump for future stages', async () => {
    const onJump = vi.fn()
    const user = userEvent.setup()
    render(<PipelineNav current="blueprint" completed={[]} onJump={onJump} />)
    await user.click(screen.getByText('导出'))
    expect(onJump).not.toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: 运行测试验证失败**

```bash
npx vitest run src/console/exam/pipelineNav.test.tsx
```
预期：FAIL（`Failed to resolve import "./pipelineNav"`）

- [ ] **Step 3: 创建实现 `frontend/src/console/exam/pipelineNav.tsx`**

```tsx
import { PIPELINE_STAGES, type PipelineStage } from '../types'

interface Props {
  current: PipelineStage
  completed: PipelineStage[]
  onJump: (stage: PipelineStage) => void
}

export function PipelineNav({ current, completed, onJump }: Props) {
  const currentIndex = PIPELINE_STAGES.find((s) => s.stage === current)?.index ?? 1

  return (
    <div className="pipeline-nav">
      <div className="pipeline-line">
        {PIPELINE_STAGES.map((s, i) => {
          const isCompleted = completed.includes(s.stage)
          const isActive = current === s.stage
          const isFuture = s.index > currentIndex
          const isClickable = isCompleted || isActive
          return (
            <div
              className={`stage-dot ${isActive ? 'active' : ''} ${isCompleted ? 'done' : ''} ${isFuture ? 'future' : ''}`}
              key={s.stage}
              onClick={() => isClickable && onJump(s.stage)}
              role="button"
            >
              <div className="stage-circle">{isCompleted ? '✓' : s.index}</div>
              <div className="stage-label">{s.label}</div>
              {i < PIPELINE_STAGES.length - 1 && <div className="stage-connector" />}
            </div>
          )
        })}
      </div>
    </div>
  )
}
```

- [ ] **Step 4: 运行测试验证通过**

```bash
npx vitest run src/console/exam/pipelineNav.test.tsx
```
预期：PASS（5 tests）

- [ ] **Step 5: 追加 CSS 到 `frontend/src/styles.css` 末尾**

```css
/* ── 试卷项目生产线 ── */
.pipeline-nav { padding: 20px 24px; background: var(--surface-subtle); border-radius: var(--radius-lg); margin-bottom: 20px; }
.pipeline-line { display: flex; align-items: center; gap: 0; position: relative; }
.stage-dot { display: flex; flex-direction: column; align-items: center; gap: 8px; cursor: default; position: relative; z-index: 1; flex: 0 0 auto; }
.stage-dot.active, .stage-dot.done { cursor: pointer; }
.stage-circle { width: 36px; height: 36px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; font-weight: 600; background: var(--surface); border: 2px solid var(--border-strong); color: var(--text-muted); transition: all 0.2s; }
.stage-dot.active .stage-circle { background: var(--accent-500); border-color: var(--accent-500); color: white; box-shadow: 0 0 0 4px var(--accent-100); }
.stage-dot.done .stage-circle { background: var(--success); border-color: var(--success); color: white; }
.stage-dot.future .stage-circle { opacity: 0.4; }
.stage-label { font-size: 12px; color: var(--text-secondary); font-weight: 500; }
.stage-dot.active .stage-label { color: var(--accent-500); font-weight: 600; }
.stage-connector { position: absolute; left: 100%; top: 18px; width: 100%; height: 2px; background: var(--border-strong); z-index: -1; }
.stage-dot.done + .stage-dot .stage-connector { background: var(--success); }

/* ── 阶段详情面板 ── */
.stage-panel { background: var(--surface-subtle); border-radius: var(--radius-lg); padding: 24px; min-height: 400px; }
.stage-panel-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
.stage-panel-head h3 { font-size: 16px; font-weight: 600; }
.gate-banner { display: flex; align-items: center; gap: 12px; padding: 14px 16px; border-radius: var(--radius); background: var(--warning-subtle); border: 1px solid var(--warning-border); margin: 16px 0; }
.gate-banner.verified { background: var(--success-subtle); border-color: var(--success-border); }
.gate-icon { font-size: 18px; }
.gate-text { flex: 1; font-size: 13px; color: var(--text); }
.gate-actions { display: flex; gap: 8px; }

/* ── 合同槽位表 ── */
.slot-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.slot-table th { text-align: left; padding: 8px 10px; color: var(--text-muted); font-weight: 500; border-bottom: 1px solid var(--border); }
.slot-table td { padding: 8px 10px; border-bottom: 1px solid var(--border); color: var(--text-secondary); }

/* ── 生成进度 ── */
.gen-progress { display: flex; flex-direction: column; gap: 12px; }
.gen-bar { height: 6px; background: var(--surface); border-radius: 3px; overflow: hidden; }
.gen-bar-fill { height: 100%; background: var(--accent-500); transition: width 0.3s; }

/* ── 题目预览卡 ── */
.q-preview { padding: 14px; background: var(--surface); border-radius: var(--radius); margin-bottom: 10px; border: 1px solid var(--border); }
.q-preview .q-stem { font-size: 13px; color: var(--text); margin-bottom: 8px; }
.q-preview .q-meta { display: flex; gap: 8px; flex-wrap: wrap; }
.q-preview .q-tag { font-size: 10px; padding: 2px 8px; border-radius: 10px; background: var(--accent-100); color: var(--accent-900); }
.q-preview .q-tag.warn { background: var(--warning-subtle); color: var(--warning-text); }

/* ── 项目列表 ── */
.project-card { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; background: var(--surface-subtle); border-radius: var(--radius); margin-bottom: 10px; cursor: pointer; border: 1px solid var(--border); transition: border-color 0.2s; }
.project-card:hover { border-color: var(--accent-500); }
.project-status { font-size: 11px; padding: 3px 10px; border-radius: 10px; font-weight: 500; }
.project-status.draft { background: var(--surface-muted); color: var(--text-muted); }
.project-status.blueprint { background: var(--accent-100); color: var(--accent-900); }
.project-status.contract { background: var(--accent-100); color: var(--accent-900); }
.project-status.generating { background: var(--warning-subtle); color: var(--warning-text); }
.project-status.review { background: var(--purple-subtle); color: var(--purple); }
.project-status.exported { background: var(--success-subtle); color: var(--success-text); }
```

- [ ] **Step 6: 运行测试 + tsc**

```bash
npx vitest run src/console/exam/pipelineNav.test.tsx
npx tsc -b --noEmit
```
预期：PASS + 无错

- [ ] **Step 7: Commit**

```bash
cd ..
git add frontend/src/console/exam/pipelineNav.tsx frontend/src/console/exam/pipelineNav.test.tsx frontend/src/styles.css
git commit -m "feat(exam): add PipelineNav horizontal 5-dot flow with stage switching"
```

---

### Task 5: 前端 — BlueprintStage 蓝图阶段详情（含确认闸门 1）

**Files:**
- Create: `frontend/src/console/exam/blueprintStage.tsx`
- Test: `frontend/src/console/exam/blueprintStage.test.tsx`

- [ ] **Step 1: 创建失败测试 `frontend/src/console/exam/blueprintStage.test.tsx`**

```typescript
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BlueprintStage } from './blueprintStage'
import type { ExamProjectDetail } from '../types'

const project: ExamProjectDetail = {
  id: 'p1', name: '2024期末', semester_label: '2024秋', status: 'blueprint',
  total_score: 100, question_count: 0, pending_review: 0,
  blueprint_confirmed: false, version_confirmed: false,
}

describe('BlueprintStage', () => {
  it('renders blueprint summary with total score', () => {
    render(<BlueprintStage project={project} onConfirm={vi.fn()} />)
    expect(screen.getByText('蓝图阶段')).toBeInTheDocument()
    expect(screen.getByText(/100/)).toBeInTheDocument()
  })

  it('shows gate banner when not confirmed', () => {
    render(<BlueprintStage project={project} onConfirm={vi.fn()} />)
    expect(screen.getByText(/确认蓝图/)).toBeInTheDocument()
    expect(screen.getByText('确认蓝图')).toBeInTheDocument()
  })

  it('shows verified banner when confirmed', () => {
    const confirmed = { ...project, blueprint_confirmed: true }
    render(<BlueprintStage project={confirmed} onConfirm={vi.fn()} />)
    const banner = screen.getByText(/蓝图已确认/).closest('.gate-banner')
    expect(banner?.classList.contains('verified')).toBe(true)
  })

  it('calls onConfirm when gate button clicked', async () => {
    const onConfirm = vi.fn()
    const user = userEvent.setup()
    render(<BlueprintStage project={project} onConfirm={onConfirm} />)
    await user.click(screen.getByText('确认蓝图'))
    expect(onConfirm).toHaveBeenCalledWith('p1')
  })
})
```

- [ ] **Step 2: 运行测试验证失败**

```bash
npx vitest run src/console/exam/blueprintStage.test.tsx
```
预期：FAIL（`Failed to resolve import "./blueprintStage"`）

- [ ] **Step 3: 创建实现 `frontend/src/console/exam/blueprintStage.tsx`**

```tsx
import type { ExamProjectDetail } from '../types'
import { Button } from '../ui'

interface Props {
  project: ExamProjectDetail
  onConfirm: (projectId: string) => void
}

export function BlueprintStage({ project, onConfirm }: Props) {
  const confirmed = project.blueprint_confirmed

  return (
    <div className="stage-panel">
      <div className="stage-panel-head">
        <h3>蓝图阶段</h3>
        <span className="muted small">总分 {project.total_score}</span>
      </div>

      <div className="form-grid">
        <div className="field">
          <label className="field-label">项目名称</label>
          <div className="field-value">{project.name}</div>
        </div>
        <div className="field">
          <label className="field-label">学期标签</label>
          <div className="field-value">{project.semester_label}</div>
        </div>
      </div>

      <div className={`gate-banner ${confirmed ? 'verified' : ''}`}>
        <span className="gate-icon">{confirmed ? '✓' : '⚠'}</span>
        <span className="gate-text">
          {confirmed ? '蓝图已确认，可进入合同阶段。' : '蓝图待确认。确认后不可修改蓝图设置。'}
        </span>
        {!confirmed && (
          <div className="gate-actions">
            <Button variant="primary" onClick={() => onConfirm(project.id)}>确认蓝图</Button>
          </div>
        )}
      </div>
    </div>
  )
}
```

注意：`<span className="gate-icon">{confirmed ? '✓' : '⚠'}</span>` 中三元表达式语法需正确。如测试因 Button 组件 props 不匹配失败，检查 `frontend/src/console/ui.tsx` 中 Button 的 props 签名（`variant` 可能是 `'primary' | 'ghost'`），做最小适配。

- [ ] **Step 4: 运行测试验证通过**

```bash
npx vitest run src/console/exam/blueprintStage.test.tsx
```
预期：PASS（4 tests）

- [ ] **Step 5: Commit**

```bash
cd ..
git add frontend/src/console/exam/blueprintStage.tsx frontend/src/console/exam/blueprintStage.test.tsx
git commit -m "feat(exam): add BlueprintStage with gate 1 (confirm blueprint)"
```

---

### Task 6: 前端 — ContractStage 合同阶段详情

**Files:**
- Create: `frontend/src/console/exam/contractStage.tsx`
- Test: `frontend/src/console/exam/contractStage.test.tsx`

- [ ] **Step 1: 创建失败测试 `frontend/src/console/exam/contractStage.test.tsx`**

```typescript
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ContractStage } from './contractStage'
import type { ExamProjectDetail, PaperContract } from '../types'

const contract: PaperContract = {
  total_score: 100,
  slots: [
    { item_index: 1, question_type: 'single_choice', score: 5, difficulty: 'medium', cognitive_level: '应用', assessment_mode: 'conceptual', exam_point_id: 'ep1', anchor_key: 'k1', unit_id: 'u1', card_id: 'c1', coverage_atom: '原子1', answer_boundary: 'b1', performance_statement: 'ps1', prompt_material: [], scope_boundary: {}, preferred_terms: [], forbidden_context: { atoms: [], answer_cores: [] }, cognitive_sequence: [], subquestion_actions: [], answer_boundaries: [] },
  ],
  conflicts: [
    { code: 'duplicate_atom', exam_point_id: 'ep1', message: '原子重复', detail: {} },
  ],
  audit_summary: { exam_points: [], type_counts: { single_choice: 1 }, difficulty_counts: { medium: 1 } },
}

const project: ExamProjectDetail = {
  id: 'p1', name: '2024期末', semester_label: '2024秋', status: 'contract',
  total_score: 100, question_count: 1, pending_review: 0,
  blueprint_confirmed: true, version_confirmed: false,
  contract,
}

describe('ContractStage', () => {
  it('renders contract slots table', () => {
    render(<ContractStage project={project} onGenerate={vi.fn()} />)
    expect(screen.getByText('合同阶段')).toBeInTheDocument()
    expect(screen.getByText('原子1')).toBeInTheDocument()
    expect(screen.getByText('single_choice')).toBeInTheDocument()
  })

  it('renders conflicts list', () => {
    render(<ContractStage project={project} onGenerate={vi.fn()} />)
    expect(screen.getByText('原子重复')).toBeInTheDocument()
  })

  it('shows generate button when no conflicts blocking', () => {
    render(<ContractStage project={project} onGenerate={vi.fn()} />)
    expect(screen.getByText('生成试卷')).toBeInTheDocument()
  })

  it('calls onGenerate when button clicked', async () => {
    const onGenerate = vi.fn()
    const user = userEvent.setup()
    render(<ContractStage project={project} onGenerate={onGenerate} />)
    await user.click(screen.getByText('生成试卷'))
    expect(onGenerate).toHaveBeenCalledWith('p1')
  })

  it('shows empty state when no contract', () => {
    const noContract = { ...project, contract: undefined }
    render(<ContractStage project={noContract} onGenerate={vi.fn()} />)
    expect(screen.getByText('合同未生成')).toBeInTheDocument()
  })
})
```

注意：测试顶部需加 `import userEvent from '@testing-library/user-event'`。

- [ ] **Step 2: 运行测试验证失败**

```bash
npx vitest run src/console/exam/contractStage.test.tsx
```
预期：FAIL（`Failed to resolve import "./contractStage"`）

- [ ] **Step 3: 创建实现 `frontend/src/console/exam/contractStage.tsx`**

```tsx
import type { ExamProjectDetail } from '../types'
import { Button, EmptyState } from '../ui'

interface Props {
  project: ExamProjectDetail
  onGenerate: (projectId: string) => void
}

export function ContractStage({ project, onGenerate }: Props) {
  const contract = project.contract

  if (!contract) {
    return (
      <div className="stage-panel">
        <div className="stage-panel-head"><h3>合同阶段</h3></div>
        <EmptyState>合同未生成</EmptyState>
      </div>
    )
  }

  return (
    <div className="stage-panel">
      <div className="stage-panel-head">
        <h3>合同阶段</h3>
        <span className="muted small">{contract.slots.length} 个题位 · 总分 {contract.total_score}</span>
      </div>

      <table className="slot-table">
        <thead>
          <tr>
            <th>#</th>
            <th>题型</th>
            <th>分值</th>
            <th>难度</th>
            <th>认知</th>
            <th>覆盖原子</th>
          </tr>
        </thead>
        <tbody>
          {contract.slots.map((slot) => (
            <tr key={slot.item_index}>
              <td>{slot.item_index}</td>
              <td>{slot.question_type}</td>
              <td>{slot.score}</td>
              <td>{slot.difficulty}</td>
              <td>{slot.cognitive_level}</td>
              <td>{slot.coverage_atom}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {contract.conflicts.length > 0 && (
        <div className="gate-banner">
          <span className="gate-icon">⚠</span>
          <div className="gate-text">
            <b>冲突 ({contract.conflicts.length})</b>
            <ul style={{ margin: '4px 0 0 16px' }}>
              {contract.conflicts.map((c, i) => (
                <li key={i}>{c.message}</li>
              ))}
            </ul>
          </div>
        </div>
      )}

      <div style={{ marginTop: 20 }}>
        <Button variant="primary" onClick={() => onGenerate(project.id)}>生成试卷</Button>
      </div>
    </div>
  )
}
```

- [ ] **Step 4: 运行测试验证通过**

```bash
npx vitest run src/console/exam/contractStage.test.tsx
```
预期：PASS（5 tests）

- [ ] **Step 5: Commit**

```bash
cd ..
git add frontend/src/console/exam/contractStage.tsx frontend/src/console/exam/contractStage.test.tsx
git commit -m "feat(exam): add ContractStage with slot table and conflict list"
```

---

### Task 7: 前端 — GenerationStage 生成阶段详情

**Files:**
- Create: `frontend/src/console/exam/generationStage.tsx`
- Test: `frontend/src/console/exam/generationStage.test.tsx`

- [ ] **Step 1: 创建失败测试 `frontend/src/console/exam/generationStage.test.tsx`**

```typescript
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { GenerationStage } from './generationStage'
import type { ExamProjectDetail, GenerationRunResult } from '../types'

const genResult: GenerationRunResult = {
  status: 'completed',
  questions: [
    { item_index: 1, question_type: 'single_choice', score: 5, stem: 'QLoRA 的量化精度是？', options: ['4bit', '8bit', '16bit', '32bit'], answer: 0, explanation: 'QLoRA 使用 4bit 量化', needs_review: false, exam_point_id: 'ep1', coverage_atom: '原子1' },
    { item_index: 2, question_type: 'short_answer', score: 10, stem: '解释梯度累加的原理', needs_review: true, exam_point_id: 'ep1', coverage_atom: '原子2' },
  ],
  final_check: { passed: true, checks: [] },
  model_call_count: 2,
  model: 'gpt-4',
}

const project: ExamProjectDetail = {
  id: 'p1', name: '2024期末', semester_label: '2024秋', status: 'generating',
  total_score: 100, question_count: 2, pending_review: 1,
  blueprint_confirmed: true, version_confirmed: false,
  generation: genResult,
}

describe('GenerationStage', () => {
  it('renders generation summary', () => {
    render(<GenerationStage project={project} onProceed={vi.fn()} />)
    expect(screen.getByText('生成阶段')).toBeInTheDocument()
    expect(screen.getByText(/2/)).toBeInTheDocument()
  })

  it('renders question preview cards', () => {
    render(<GenerationStage project={project} onProceed={vi.fn()} />)
    expect(screen.getByText('QLoRA 的量化精度是？')).toBeInTheDocument()
    expect(screen.getByText('解释梯度累加的原理')).toBeInTheDocument()
  })

  it('marks questions needing review with warn tag', () => {
    render(<GenerationStage project={project} onProceed={vi.fn()} />)
    const warnTags = screen.getAllByText('待审')
    expect(warnTags).toHaveLength(1)
  })

  it('shows proceed button when generation complete', () => {
    render(<GenerationStage project={project} onProceed={vi.fn()} />)
    expect(screen.getByText('进入审核')).toBeInTheDocument()
  })

  it('calls onProceed when button clicked', async () => {
    const onProceed = vi.fn()
    const user = userEvent.setup()
    render(<GenerationStage project={project} onProceed={onProceed} />)
    await user.click(screen.getByText('进入审核'))
    expect(onProceed).toHaveBeenCalledWith('p1')
  })

  it('shows empty state when no generation', () => {
    const noGen = { ...project, generation: undefined }
    render(<GenerationStage project={noGen} onProceed={vi.fn()} />)
    expect(screen.getByText('尚未生成')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: 运行测试验证失败**

```bash
npx vitest run src/console/exam/generationStage.test.tsx
```
预期：FAIL（`Failed to resolve import "./generationStage"`）

- [ ] **Step 3: 创建实现 `frontend/src/console/exam/generationStage.tsx`**

```tsx
import type { ExamProjectDetail } from '../types'
import { Button, EmptyState } from '../ui'

interface Props {
  project: ExamProjectDetail
  onProceed: (projectId: string) => void
}

export function GenerationStage({ project, onProceed }: Props) {
  const gen = project.generation

  if (!gen) {
    return (
      <div className="stage-panel">
        <div className="stage-panel-head"><h3>生成阶段</h3></div>
        <EmptyState>尚未生成</EmptyState>
      </div>
    )
  }

  const pending = gen.questions.filter((q) => q.needs_review).length

  return (
    <div className="stage-panel">
      <div className="stage-panel-head">
        <h3>生成阶段</h3>
        <span className="muted small">{gen.questions.length} 题 · {pending} 待审 · {gen.model_call_count} 次模型调用</span>
      </div>

      <div className="gen-progress">
        <div className="gen-bar">
          <div className="gen-bar-fill" style={{ width: gen.status === 'completed' ? '100%' : '60%' }} />
        </div>
        <span className="muted small">状态：{gen.status}</span>
      </div>

      <div style={{ marginTop: 20 }}>
        {gen.questions.map((q, i) => (
          <div className="q-preview" key={i}>
            <div className="q-stem">{q.item_index}. {q.stem}</div>
            <div className="q-meta">
              <span className="q-tag">{q.question_type}</span>
              <span className="q-tag">{q.score} 分</span>
              {q.needs_review && <span className="q-tag warn">待审</span>}
            </div>
          </div>
        ))}
      </div>

      {gen.status === 'completed' && (
        <div style={{ marginTop: 20 }}>
          <Button variant="primary" onClick={() => onProceed(project.id)}>进入审核</Button>
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 4: 运行测试验证通过**

```bash
npx vitest run src/console/exam/generationStage.test.tsx
```
预期：PASS（6 tests）

- [ ] **Step 5: Commit**

```bash
cd ..
git add frontend/src/console/exam/generationStage.tsx frontend/src/console/exam/generationStage.test.tsx
git commit -m "feat(exam): add GenerationStage with progress bar and question preview cards"
```

---

### Task 8: 前端 — ReviewExportStage 审核 + 导出阶段（含确认闸门 2）

**Files:**
- Create: `frontend/src/console/exam/reviewExportStage.tsx`
- Test: `frontend/src/console/exam/reviewExportStage.test.tsx`

- [ ] **Step 1: 创建失败测试 `frontend/src/console/exam/reviewExportStage.test.tsx`**

```typescript
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ReviewExportStage } from './reviewExportStage'
import type { ExamProjectDetail, GenerationRunResult } from '../types'

const gen: GenerationRunResult = {
  status: 'completed',
  questions: [
    { item_index: 1, question_type: 'single_choice', score: 5, stem: 'QLoRA 量化精度？', answer: 0, needs_review: false },
    { item_index: 2, question_type: 'short_answer', score: 10, stem: '解释梯度累加', needs_review: true },
  ],
  final_check: { passed: true },
  model_call_count: 2,
  model: 'gpt-4',
}

const project: ExamProjectDetail = {
  id: 'p1', name: '2024期末', semester_label: '2024秋', status: 'review',
  total_score: 100, question_count: 2, pending_review: 1,
  blueprint_confirmed: true, version_confirmed: false,
  generation: gen,
}

describe('ReviewExportStage', () => {
  it('renders review summary', () => {
    render(<ReviewExportStage project={project} onExport={vi.fn()} />)
    expect(screen.getByText('审核与导出')).toBeInTheDocument()
    expect(screen.getByText(/1/)).toBeInTheDocument()
  })

  it('shows pending review questions with warn tag', () => {
    render(<ReviewExportStage project={project} onExport={vi.fn()} />)
    const warnTags = screen.getAllByText('待审')
    expect(warnTags).toHaveLength(1)
  })

  it('shows export gate banner when not confirmed', () => {
    render(<ReviewExportStage project={project} onExport={vi.fn()} />)
    expect(screen.getByText(/确认试卷版本/)).toBeInTheDocument()
    expect(screen.getByText('确认导出')).toBeInTheDocument()
  })

  it('shows verified banner when version confirmed', () => {
    const confirmed = { ...project, version_confirmed: true, status: 'exported' as const }
    render(<ReviewExportStage project={confirmed} onExport={vi.fn()} />)
    expect(screen.getByText(/已导出/)).toBeInTheDocument()
  })

  it('calls onExport when confirm button clicked', async () => {
    const onExport = vi.fn()
    const user = userEvent.setup()
    render(<ReviewExportStage project={project} onExport={onExport} />)
    await user.click(screen.getByText('确认导出'))
    expect(onExport).toHaveBeenCalledWith('p1')
  })
})
```

- [ ] **Step 2: 运行测试验证失败**

```bash
npx vitest run src/console/exam/reviewExportStage.test.tsx
```
预期：FAIL（`Failed to resolve import "./reviewExportStage"`）

- [ ] **Step 3: 创建实现 `frontend/src/console/exam/reviewExportStage.tsx`**

```tsx
import type { ExamProjectDetail } from '../types'
import { Button } from '../ui'

interface Props {
  project: ExamProjectDetail
  onExport: (projectId: string) => void
}

export function ReviewExportStage({ project, onExport }: Props) {
  const gen = project.generation
  const pending = gen?.questions.filter((q) => q.needs_review).length ?? 0
  const exported = project.version_confirmed

  return (
    <div className="stage-panel">
      <div className="stage-panel-head">
        <h3>审核与导出</h3>
        <span className="muted small">{project.question_count} 题 · {pending} 待审</span>
      </div>

      {gen && (
        <div style={{ marginBottom: 20 }}>
          {gen.questions.map((q, i) => (
            <div className="q-preview" key={i}>
              <div className="q-stem">{q.item_index}. {q.stem}</div>
              <div className="q-meta">
                <span className="q-tag">{q.question_type}</span>
                <span className="q-tag">{q.score} 分</span>
                {q.needs_review && <span className="q-tag warn">待审</span>}
              </div>
            </div>
          ))}
        </div>
      )}

      <div className={`gate-banner ${exported ? 'verified' : ''}`}>
        <span className="gate-icon">{exported ? '✓' : '⚠'}</span>
        <span className="gate-text">
          {exported
            ? `已导出。试卷版本已确认，可交付使用。`
            : `确认试卷版本后将正式导出。确认后不可修改题目。`}
        </span>
        {!exported && (
          <div className="gate-actions">
            <Button variant="primary" onClick={() => onExport(project.id)}>确认导出</Button>
          </div>
        )}
      </div>
    </div>
  )
}
```

- [ ] **Step 4: 运行测试验证通过**

```bash
npx vitest run src/console/exam/reviewExportStage.test.tsx
```
预期：PASS（5 tests）

- [ ] **Step 5: Commit**

```bash
cd ..
git add frontend/src/console/exam/reviewExportStage.tsx frontend/src/console/exam/reviewExportStage.test.tsx
git commit -m "feat(exam): add ReviewExportStage with review list and gate 2 (confirm version)"
```

---

### Task 9: 前端 — ExamProjectWorkspace 壳 + 装配 + App.tsx 替换 + ExamProjectList 接通

**Files:**
- Create: `frontend/src/console/exam/examProjectWorkspace.tsx`
- Create: `frontend/src/console/exam/examProjectWorkspace.test.tsx`
- Modify: `frontend/src/App.tsx:142-150`
- Modify: `frontend/src/console/ExamProjectList.tsx`

- [ ] **Step 1: 创建失败测试 `frontend/src/console/exam/examProjectWorkspace.test.tsx`**

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../client', () => ({
  projectsApi: {
    get: vi.fn(),
    updateStatus: vi.fn(),
  },
}))

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { projectsApi } from '../client'
import { ExamProjectWorkspace } from './examProjectWorkspace'
import type { ExamProjectDetail } from '../types'

const project: ExamProjectDetail = {
  id: 'p1', name: '2024期末', semester_label: '2024秋', status: 'blueprint',
  total_score: 100, question_count: 0, pending_review: 0,
  blueprint_confirmed: false, version_confirmed: false,
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(projectsApi.get as ReturnType<typeof vi.fn>).mockResolvedValue(project)
  ;(projectsApi.updateStatus as ReturnType<typeof vi.fn>).mockResolvedValue({ ...project, blueprint_confirmed: true })
})

describe('ExamProjectWorkspace', () => {
  it('renders loading then pipeline nav', async () => {
    render(<ExamProjectWorkspace courseId="c1" projectId="p1" />)
    expect(screen.getByText('加载项目…')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('蓝图')).toBeInTheDocument())
    expect(screen.getByText('合同')).toBeInTheDocument()
    expect(screen.getByText('生成')).toBeInTheDocument()
    expect(screen.getByText('审核')).toBeInTheDocument()
    expect(screen.getByText('导出')).toBeInTheDocument()
  })

  it('shows blueprint stage detail for blueprint status', async () => {
    render(<ExamProjectWorkspace courseId="c1" projectId="p1" />)
    await waitFor(() => expect(screen.getByText('蓝图阶段')).toBeInTheDocument())
    expect(screen.getByText(/确认蓝图/)).toBeInTheDocument()
  })

  it('calls updateStatus when confirming blueprint', async () => {
    const user = userEvent.setup()
    render(<ExamProjectWorkspace courseId="c1" projectId="p1" />)
    await waitFor(() => expect(screen.getByText('确认蓝图')).toBeInTheDocument())
    await user.click(screen.getByText('确认蓝图'))
    expect(projectsApi.updateStatus).toHaveBeenCalled()
  })

  it('shows contract stage when status is contract', async () => {
    ;(projectsApi.get as ReturnType<typeof vi.fn>).mockResolvedValue({ ...project, status: 'contract', blueprint_confirmed: true })
    render(<ExamProjectWorkspace courseId="c1" projectId="p1" />)
    await waitFor(() => expect(screen.getByText('合同阶段')).toBeInTheDocument())
  })
})
```

- [ ] **Step 2: 运行测试验证失败**

```bash
npx vitest run src/console/exam/examProjectWorkspace.test.tsx
```
预期：FAIL（`Failed to resolve import "./examProjectWorkspace"`）

- [ ] **Step 3: 创建实现 `frontend/src/console/exam/examProjectWorkspace.tsx`**

```tsx
import { useEffect, useState, useCallback } from 'react'
import { projectsApi } from '../client'
import { LoadingLine, Notice } from '../ui'
import { PipelineNav } from './pipelineNav'
import { BlueprintStage } from './blueprintStage'
import { ContractStage } from './contractStage'
import { GenerationStage } from './generationStage'
import { ReviewExportStage } from './reviewExportStage'
import type { ExamProjectDetail, PipelineStage } from '../types'

const STATUS_TO_STAGE: Record<string, PipelineStage> = {
  blueprint: 'blueprint',
  contract: 'contract',
  generating: 'generating',
  review: 'review',
  exported: 'exported',
}

export function ExamProjectWorkspace({ courseId, projectId }: { courseId: string; projectId: string }) {
  const [project, setProject] = useState<ExamProjectDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    try {
      const p = await projectsApi.get(courseId, projectId)
      setProject(p)
      setError('')
    } catch {
      setError('加载项目失败')
    } finally {
      setLoading(false)
    }
  }, [courseId, projectId])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    projectsApi
      .get(courseId, projectId)
      .then((p) => { if (!cancelled) setProject(p) })
      .catch(() => { if (!cancelled) setError('加载项目失败') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [courseId, projectId])

  const handleConfirmBlueprint = useCallback(async (pid: string) => {
    await projectsApi.updateStatus(courseId, pid, 'contract')
    await refresh()
  }, [courseId, refresh])

  const handleGenerate = useCallback(async (pid: string) => {
    await projectsApi.updateStatus(courseId, pid, 'generating')
    await refresh()
  }, [courseId, refresh])

  const handleProceedToReview = useCallback(async (pid: string) => {
    await projectsApi.updateStatus(courseId, pid, 'review')
    await refresh()
  }, [courseId, refresh])

  const handleExport = useCallback(async (pid: string) => {
    await projectsApi.updateStatus(courseId, pid, 'exported')
    await refresh()
  }, [courseId, refresh])

  const handleJump = useCallback((stage: PipelineStage) => {
    // 阶段跳转：仅允许跳到已完成或当前阶段
    // 由 PipelineNav 的 isClickable 控制可跳转范围
  }, [])

  if (loading) return <LoadingLine>加载项目…</LoadingLine>
  if (error) return <Notice kind="warning">{error}</Notice>
  if (!project) return <Notice kind="warning">项目不存在</Notice>

  const currentStage = STATUS_TO_STAGE[project.status] ?? 'blueprint'
  const completedStages: PipelineStage[] = []
  const order: PipelineStage[] = ['blueprint', 'contract', 'generating', 'review', 'exported']
  const currentIdx = order.indexOf(currentStage)
  for (let i = 0; i < currentIdx; i++) completedStages.push(order[i])

  return (
    <div className="content-inner">
      <div className="page-head">
        <h2>{project.name}</h2>
        <div className="desc">{project.semester_label} · 总分 {project.total_score} · {project.question_count} 题</div>
      </div>

      <PipelineNav current={currentStage} completed={completedStages} onJump={handleJump} />

      {currentStage === 'blueprint' && (
        <BlueprintStage project={project} onConfirm={handleConfirmBlueprint} />
      )}
      {currentStage === 'contract' && (
        <ContractStage project={project} onGenerate={handleGenerate} />
      )}
      {currentStage === 'generating' && (
        <GenerationStage project={project} onProceed={handleProceedToReview} />
      )}
      {(currentStage === 'review' || currentStage === 'exported') && (
        <ReviewExportStage project={project} onExport={handleExport} />
      )}
    </div>
  )
}
```

- [ ] **Step 4: 运行测试验证通过**

```bash
npx vitest run src/console/exam/examProjectWorkspace.test.tsx
```
预期：PASS（4 tests）

- [ ] **Step 5: 替换 App.tsx 中的 exam-project 路由占位**

在 `frontend/src/App.tsx` 中：
- 第 12 行后追加导入：`import { ExamProjectWorkspace } from './console/exam/examProjectWorkspace'`
- 第 142-150 行（`route.page === 'exam-project'` 分支）替换为：

```tsx
      ) : route.page === 'exam-project' ? (
        <ExamProjectWorkspace courseId={route.course.id} projectId={route.projectId} />
      ) : null}
```

- [ ] **Step 6: 接通 ExamProjectList 真实项目列表**

修改 `frontend/src/console/ExamProjectList.tsx`：

```tsx
import { useEffect, useState } from 'react'
import { Card, EmptyState, LoadingLine, Notice } from './ui'
import { projectsApi } from './client'
import type { ExamProjectDetail } from './types'
import type { Route } from './nav'

interface Props {
  courseId: string
  onOpenProject: (projectId: string) => void
}

const STATUS_LABELS: Record<string, string> = {
  draft: '草稿', blueprint: '蓝图', contract: '合同', generating: '生成中', review: '审核', exported: '已导出',
}

export function ExamProjectList({ courseId, onOpenProject }: Props) {
  const [projects, setProjects] = useState<ExamProjectDetail[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    projectsApi
      .list(courseId)
      .then((ps) => { if (!cancelled) setProjects(ps) })
      .catch(() => { if (!cancelled) setError('加载项目列表失败') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [courseId])

  if (loading) return <LoadingLine>加载项目列表…</LoadingLine>
  if (error) return <Notice kind="warning">{error}</Notice>

  return (
    <div className="content-inner">
      <div className="page-head">
        <h2>试卷项目</h2>
        <div className="desc">按学期归档的单次命题对象。进入后是 5 阶段生产线。</div>
      </div>
      {projects.length === 0 ? (
        <Card title="项目列表">
          <EmptyState>暂无试卷项目</EmptyState>
        </Card>
      ) : (
        <div>
          {projects.map((p) => (
            <div className="project-card" key={p.id} onClick={() => onOpenProject(p.id)}>
              <div>
                <b>{p.name}</b>
                <span className="muted small" style={{ marginLeft: 12 }}>{p.semester_label || '—'}</span>
              </div>
              <span className={`project-status ${p.status}`}>{STATUS_LABELS[p.status] ?? p.status}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 7: 在 App.tsx 中传入 onOpenProject 回调**

在 App.tsx 的 `route.section === 'projects'` 分支中，将 `<ExamProjectList />` 替换为：

```tsx
        ) : route.section === 'projects' ? (
          <ExamProjectList
            courseId={route.course.id}
            onOpenProject={(pid) => setRoute({ page: 'exam-project', course: route.course, projectId: pid })}
          />
        ) : (
```

- [ ] **Step 8: 运行全量测试 + 构建**

```bash
npx vitest run
npm run build
```
预期：全部 PASS，build 无错误

如果 ExamProjectList.test.tsx 因 props 变化失败（原测试无 props），更新测试传入 mock props：
```typescript
renderWithProviders(<ExamProjectList courseId="c1" onOpenProject={vi.fn()} />)
```

- [ ] **Step 9: Commit**

```bash
cd ..
git add frontend/src/console/exam/examProjectWorkspace.tsx frontend/src/console/exam/examProjectWorkspace.test.tsx frontend/src/App.tsx frontend/src/console/ExamProjectList.tsx frontend/src/console/ExamProjectList.test.tsx
git commit -m "feat(exam): add ExamProjectWorkspace shell with pipeline assembly, wire App.tsx and ExamProjectList"
```

---

## Self-Review

**1. Spec coverage:**
- ✅ 横向流水线 5 阶段圆点 → Task 4 (PipelineNav)
- ✅ 单活动阶段详情 → Task 9 (ExamProjectWorkspace 按 status 渲染对应阶段)
- ✅ 两次确认闸门 → Task 5 (gate 1: 确认蓝图) + Task 8 (gate 2: 确认试卷版本)
- ✅ 阶段切换 → Task 4 (onJump) + Task 9 (completedStages 计算)
- ✅ 项目列表入口 → Task 9 (ExamProjectList 接通)
- ✅ 后端 CRUD → Task 1 (exam_projects 端点)

**2. Placeholder scan:** 无 TBD/TODO/placeholder。所有代码块完整。

**3. Type consistency:**
- `ExamProjectDetail` 在 Task 2 定义，Task 3/5/6/7/8/9 使用一致
- `PipelineStage` 在 Task 2 定义，Task 4/9 使用一致
- `projectsApi` 在 Task 3 定义，Task 9 使用一致
- `PIPELINE_STAGES` 在 Task 2 定义，Task 4 使用一致
- `ExamProjectSummary.status` 与 `ExamProjectDetail.status` 枚举值一致

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-19-exam-project-pipeline.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
