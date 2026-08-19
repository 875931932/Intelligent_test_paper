# 教师工作台前端地基 Implementation Plan (Plan 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建前端测试栈，把 `styles.css` 重写为暗色苹果灰设计系统，把 `App.tsx` 从线性四步 wizard 重构为两层导航（课程空间 + 试卷项目），交付课程空间首页、知识目录只读树浏览、试卷项目列表骨架——全部可测。

**Architecture:** Vitest + Testing Library + jsdom 做组件/纯逻辑测试。沿用现有 `useState` 路由模式（不引入路由库，YAGNI），把 `Route` 判别联合扩展为两层。复用现有 `MaterialsStep`/`FrameworkStep` 作为课程级区块内容；新建 `PublishedTreeBrowse`（知识目录只读）与 `ExamProjectList`（骨架）。设计系统只换 `styles.css` token 与结构类为暗色，`ui.tsx` 原语 API 不变。

**Tech Stack:** React 19.2、TypeScript 7、Vite 8、vitest、@testing-library/react、@testing-library/jest-dom、@testing-library/user-event、jsdom。

**Spec:** `docs/superpowers/specs/2026-08-19-teacher-workbench-frontend-design.md` §1-§3、§6（暗色苹果灰）。本计划覆盖 S1 的导航外壳与课程空间首页部分；知识目录图谱+树双视图与证据中心为 Plan 2，试卷项目生产线详情为 Plan 3。

**工作目录：** 所有相对路径基于 `frontend/`（即 `f:\比赛项目\阅卷出题功能\.worktrees\core-implementation\frontend\`）。命令在此目录执行。

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `frontend/vitest.config.ts` | vitest 配置（jsdom、setup、globals） |
| `frontend/src/test/setup.ts` | 测试全局：jest-dom matchers、cleanup |
| `frontend/src/test/render.tsx` | `render` 封装（统一 provider） |
| `frontend/package.json` | 增加测试依赖与 `test` 脚本 |
| `frontend/src/styles.css` | 重写为暗色苹果灰 token + 结构类 |
| `frontend/src/console/nav.ts` | 两层路由模型 + 纯导航函数 |
| `frontend/src/console/types.ts` | 追加 `CourseSection`? 否——放 nav.ts；追加 `ExamProjectSummary`/`CourseReadiness` |
| `frontend/src/console/shell/Layout.tsx` | 侧栏+顶栏外壳 |
| `frontend/src/console/CourseSpaceHome.tsx` | 四宫格状态卡首页 |
| `frontend/src/console/PublishedTreeBrowse.tsx` | 知识目录只读树 |
| `frontend/src/console/ExamProjectList.tsx` | 试卷项目列表骨架 |
| `frontend/src/App.tsx` | 两层路由装配 |
| 各组件旁 `*.test.tsx` | 组件/集成测试 |

---

## Task 1: 测试基础设施

**Files:**
- Create: `frontend/vitest.config.ts`
- Create: `frontend/src/test/setup.ts`
- Create: `frontend/src/test/render.tsx`
- Modify: `frontend/package.json`
- Test: `frontend/src/test/sanity.test.ts`

- [ ] **Step 1: 写失败的冒烟测试**

Create `frontend/src/test/sanity.test.ts`:
```typescript
import { describe, it, expect } from 'vitest'

describe('sanity', () => {
  it('vitest is wired', () => {
    expect(1 + 1).toBe(2)
  })
})
```

- [ ] **Step 2: 运行测试确认失败（无配置/依赖）**

Run: `npx vitest run src/test/sanity.test.ts`
Expected: FAIL（提示 vitest 未安装或无 config，`Cannot find module 'vitest'`）

- [ ] **Step 3: 增加依赖与脚本**

Modify `frontend/package.json`，在 `scripts` 增加 `test`，并新增 `devDependencies`：
```json
{
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "preview": "vite preview",
    "test": "vitest"
  },
  "devDependencies": {
    "@testing-library/jest-dom": "^6.4.0",
    "@testing-library/react": "^16.0.0",
    "@testing-library/user-event": "^14.5.0",
    "@types/react": "19.2.18",
    "@types/react-dom": "19.2.4",
    "jsdom": "^24.0.0",
    "vitest": "^2.1.0"
  }
}
```
> 注：保留原有 `dependencies`（react/react-dom/vite/typescript/@vitejs/plugin-react/lucide-react）不动。

- [ ] **Step 4: 写 vitest 配置与 setup**

Create `frontend/vitest.config.ts`:
```typescript
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
```

Create `frontend/src/test/setup.ts`:
```typescript
import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

afterEach(() => {
  cleanup()
})
```

Create `frontend/src/test/render.tsx`:
```typescript
import { render, type RenderOptions } from '@testing-library/react'
import type { ReactElement } from 'react'

export function renderWithProviders(ui: ReactElement, options?: RenderOptions) {
  return render(ui, options)
}

export { render }
```

- [ ] **Step 5: 安装依赖并运行测试确认通过**

Run: `npm install`
Run: `npx vitest run src/test/sanity.test.ts`
Expected: PASS（`1 passed`）

- [ ] **Step 6: 提交**

```bash
git add frontend/package.json frontend/package-lock.json frontend/vitest.config.ts frontend/src/test/sanity.test.ts frontend/src/test/setup.ts frontend/src/test/render.tsx
git commit -m "test: bootstrap vitest + testing-library for frontend"
```

---

## Task 2: 暗色苹果灰设计系统（styles.css 重写）

**Files:**
- Modify: `frontend/src/styles.css`（整文件重写为暗色，保留全部类名）

- [ ] **Step 1: 重写 styles.css 为暗色苹果灰**

整文件覆盖 `frontend/src/styles.css`：
```css
/* ═══════════════════════════════════════════════════════════
   教师控制台 · 暗色苹果灰设计系统
   石墨近黑底色 + #0a84ff 单一蓝强调 · 毛玻璃半透明白边
   ═══════════════════════════════════════════════════════════ */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

:root {
  --font-sans: 'Inter', -apple-system, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
  --font-mono: 'JetBrains Mono', ui-monospace, 'SF Mono', Consolas, 'Microsoft YaHei', monospace;

  /* 强调（单一苹果系统蓝，暗色版） */
  --accent-50: rgba(10, 132, 255, 0.08);
  --accent-100: rgba(10, 132, 255, 0.14);
  --accent-200: rgba(10, 132, 255, 0.22);
  --accent-500: #0a84ff;
  --accent-600: #0a84ff;
  --accent-700: #6eb5ff;
  --accent-900: #a9ccff;

  /* 中性面（石墨近黑，层叠用透明白叠加） */
  --bg: #0a0b0d;
  --surface: #1c1e22;
  --surface-subtle: #141619;
  --surface-muted: rgba(255, 255, 255, 0.04);
  --glass: rgba(255, 255, 255, 0.04);
  --glass-border: rgba(255, 255, 255, 0.07);
  --border: rgba(255, 255, 255, 0.07);
  --border-strong: rgba(255, 255, 255, 0.12);
  --text: #f5f5f7;
  --text-secondary: #8e8e93;
  --text-muted: #6b6b70;

  /* 状态色（暗色版成对） */
  --success: #30d158;
  --success-subtle: rgba(48, 209, 88, 0.15);
  --success-border: rgba(48, 209, 88, 0.28);
  --success-text: #30d158;
  --danger: #ff453a;
  --danger-subtle: rgba(255, 69, 58, 0.15);
  --danger-border: rgba(255, 69, 58, 0.28);
  --danger-text: #ff6961;
  --warning: #ff9f0a;
  --warning-subtle: rgba(255, 159, 10, 0.15);
  --warning-border: rgba(255, 159, 10, 0.28);
  --warning-text: #ffb340;
  --purple: #bf5af2;
  --purple-subtle: rgba(191, 90, 242, 0.15);

  /* 结构 */
  --radius: 12px;
  --radius-sm: 8px;
  --radius-lg: 16px;
  --shadow-sm: 0 1px 0 rgba(255, 255, 255, 0.04);
  --shadow-md: 0 4px 16px rgba(0, 0, 0, 0.4);

  /* 侧栏 */
  --sidebar: rgba(15, 16, 19, 0.72);
  --sidebar-border: rgba(255, 255, 255, 0.06);
  --sidebar-accent: rgba(255, 255, 255, 0.06);

  --space: 4px;
}

* { box-sizing: border-box; }
html, body, #root { height: 100%; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 400 13.5px/1.5 var(--font-sans);
  -webkit-font-smoothing: antialiased;
}

button, input, select, textarea { font: inherit; color: inherit; }
button { cursor: pointer; }
a { color: var(--accent-600); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ── 应用壳：侧栏 + 主区 ─────────────────────────── */
.shell { display: flex; height: 100vh; overflow: hidden; }

.sidebar {
  width: 232px;
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 16px 12px;
  background: var(--sidebar);
  backdrop-filter: blur(20px);
  border-right: 1px solid var(--sidebar-border);
  overflow-y: auto;
}

.sidebar-brand { display: flex; align-items: center; gap: 10px; padding: 6px 10px 16px; border-bottom: 1px solid var(--sidebar-border); margin-bottom: 10px; }
.brand-mark { width: 34px; height: 34px; flex: none; display: grid; place-items: center; background: linear-gradient(135deg, #3b3f46, #1c1e22); color: #e5e5e7; border: 1px solid var(--glass-border); border-radius: 10px; font-weight: 700; font-size: 15px; }
.brand-copy { min-width: 0; }
.brand-copy b { display: block; font-size: 13.5px; font-weight: 700; letter-spacing: -0.01em; color: var(--text); }
.brand-copy span { display: block; color: var(--text-muted); font-size: 11px; }

.sidebar-section { padding: 14px 10px 6px; color: var(--text-muted); font-size: 10.5px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; }

.nav-item { display: flex; align-items: center; gap: 10px; width: 100%; min-height: 34px; padding: 0 10px; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text-secondary); text-align: left; font-size: 13px; font-weight: 500; transition: background-color 0.14s ease, color 0.14s ease; }
.nav-item:hover { background: var(--sidebar-accent); color: var(--text); }
.nav-item.active { background: var(--accent-100); color: var(--text); font-weight: 600; }
.nav-item .dot { width: 7px; height: 7px; flex: none; border-radius: 999px; background: currentColor; opacity: 0.72; }
.nav-item .count { margin-left: auto; padding: 1px 7px; border-radius: 999px; background: var(--surface-muted); color: var(--text-muted); font-size: 11px; font-weight: 600; }
.nav-item.active .count { background: var(--accent-200); color: var(--accent-900); }
.nav-item[disabled] { opacity: 0.45; cursor: not-allowed; }
.nav-item[disabled]:hover { background: transparent; color: var(--text-secondary); }

.sidebar-foot { margin-top: auto; padding-top: 10px; border-top: 1px solid var(--sidebar-border); display: flex; flex-direction: column; gap: 4px; }

.sidebar-course { padding: 10px; border-radius: var(--radius-sm); background: var(--glass); border: 1px solid var(--glass-border); margin: 0 2px 8px; }
.sidebar-course b { display: block; font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
.sidebar-course span { display: block; margin-top: 2px; color: var(--text-muted); font-size: 11px; font-family: var(--font-mono); }

.main { flex: 1; min-width: 0; display: flex; flex-direction: column; overflow: hidden; }

.topbar { flex: none; display: flex; align-items: center; gap: 12px; padding: 14px 28px; background: rgba(20, 22, 26, 0.6); backdrop-filter: blur(20px); border-bottom: 1px solid var(--border); }
.topbar h1 { margin: 0; font-size: 16px; font-weight: 700; letter-spacing: -0.01em; color: var(--text); }
.topbar .crumb-sep { color: var(--text-muted); }
.topbar .crumb-link { color: var(--text-muted); font-size: 13px; cursor: pointer; }
.topbar .crumb-link:hover { color: var(--text); text-decoration: none; }
.topbar-actions { margin-left: auto; display: flex; align-items: center; gap: 10px; }

.content { flex: 1; overflow-y: auto; padding: 24px 28px 48px; }
.content-inner { max-width: 1160px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }

/* ── 按钮 ───────────────────────────────────────── */
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-width: 0; border: 1px solid transparent; border-radius: var(--radius-sm); padding: 8px 16px; font: 600 13px/1 var(--font-sans); transition: background-color 0.16s ease, border-color 0.16s ease, color 0.16s ease, filter 0.16s ease; }
.btn:focus-visible { outline: 2px solid var(--accent-500); outline-offset: 2px; }
.btn:disabled { cursor: not-allowed; opacity: 0.5; }
.btn.primary { background: var(--accent-600); color: #fff; }
.btn.primary:hover:not(:disabled) { filter: brightness(1.1); }
.btn.secondary { background: var(--glass); color: var(--text); border-color: var(--border-strong); }
.btn.secondary:hover:not(:disabled) { background: var(--surface-muted); }
.btn.ghost { background: transparent; color: var(--text-secondary); padding: 8px 10px; }
.btn.ghost:hover:not(:disabled) { background: var(--surface-muted); color: var(--text); }
.btn.danger { background: var(--danger); color: #fff; }
.btn.danger:hover:not(:disabled) { filter: brightness(1.1); }
.btn.danger-ghost { background: transparent; color: var(--danger-text); }
.btn.danger-ghost:hover:not(:disabled) { background: var(--danger-subtle); }
.btn.sm { padding: 5px 10px; font-size: 12px; border-radius: var(--radius-sm); }

/* ── 卡片 ───────────────────────────────────────── */
.card { background: var(--glass); border: 1px solid var(--glass-border); border-radius: var(--radius); backdrop-filter: blur(10px); }
.card-head { display: flex; align-items: center; gap: 12px; padding: 14px 18px; border-bottom: 1px solid var(--border); }
.card-head h3 { margin: 0; font-size: 14px; font-weight: 600; letter-spacing: -0.01em; color: var(--text); }
.card-head .sub { color: var(--text-muted); font-size: 12px; }
.card-head .spacer { flex: 1; }
.card-body { padding: 18px; }
.card-body.tight { padding: 0; }

.metric-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
.metric { display: flex; flex-direction: column; gap: 6px; padding: 16px; border: 1px solid var(--glass-border); border-radius: var(--radius-lg); background: var(--glass); text-align: left; color: var(--text); cursor: pointer; transition: border-color 0.14s ease, background 0.14s ease; }
.metric:hover { border-color: var(--border-strong); background: var(--surface-muted); }
.metric .eyebrow { color: var(--text-secondary); font-size: 11.5px; font-weight: 600; }
.metric .value { font-size: 24px; font-weight: 700; letter-spacing: -0.03em; }
.metric .meta { color: var(--text-muted); font-size: 12px; margin-top: 2px; }

/* ── 表格 ───────────────────────────────────────── */
.table-card { background: var(--glass); border: 1px solid var(--glass-border); border-radius: var(--radius); overflow: hidden; }
.table { width: 100%; border-collapse: collapse; }
.table th, .table td { padding: 11px 16px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: middle; }
.table th { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); background: var(--surface-subtle); white-space: nowrap; }
.table tbody tr:last-child td { border-bottom: 0; }
.table tbody tr:hover { background: var(--surface-muted); }
.table td.num { font-family: var(--font-mono); font-size: 12.5px; }
.table .cell-title { font-weight: 600; }
.table .cell-sub { color: var(--text-muted); font-size: 12px; }

.table-empty { display: grid; place-items: center; min-height: 120px; padding: 16px; color: var(--text-muted); background: var(--surface-subtle); font-size: 13px; }

/* ── 徽章 / 状态 ────────────────────────────────── */
.pill { display: inline-flex; align-items: center; gap: 5px; padding: 3px 9px; border-radius: 999px; font-size: 11.5px; font-weight: 600; line-height: 1.4; white-space: nowrap; }
.pill .dot { width: 6px; height: 6px; border-radius: 999px; background: currentColor; }
.pill.neutral { background: var(--surface-muted); color: var(--text-secondary); }
.pill.info { background: var(--accent-100); color: var(--accent-900); }
.pill.success { background: var(--success-subtle); color: var(--success-text); border: 1px solid var(--success-border); }
.pill.danger { background: var(--danger-subtle); color: var(--danger-text); border: 1px solid var(--danger-border); }
.pill.warning { background: var(--warning-subtle); color: var(--warning-text); border: 1px solid var(--warning-border); }

/* ── 表单 ───────────────────────────────────────── */
.field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.field > label { color: var(--text-secondary); font-size: 12px; font-weight: 600; }
.field .hint { color: var(--text-muted); font-size: 11.5px; }

.input, .select, .textarea { width: 100%; padding: 8px 12px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--surface-subtle); color: var(--text); font-size: 13px; transition: border-color 0.14s ease, box-shadow 0.14s ease; }
.input::placeholder, .textarea::placeholder { color: var(--text-muted); }
.input:focus, .select:focus, .textarea:focus { outline: none; border-color: var(--accent-500); box-shadow: 0 0 0 3px var(--accent-100); }
.textarea { min-height: 84px; resize: vertical; font-family: var(--font-mono); font-size: 12px; line-height: 1.6; }

.form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; }
.form-row { display: flex; align-items: flex-end; gap: 12px; flex-wrap: wrap; }
.form-row .field { flex: 1 1 180px; }
.check-line { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-secondary); }
.check-line input { accent-color: var(--accent-600); width: 15px; height: 15px; }

/* ── 步骤导航 ───────────────────────────────────── */
.step-nav { display: flex; gap: 6px; padding: 4px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface-subtle); }
.step-tab { flex: 1; display: flex; align-items: center; justify-content: center; gap: 8px; min-height: 36px; padding: 0 10px; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text-muted); font-size: 13px; font-weight: 500; white-space: nowrap; transition: background-color 0.14s ease, color 0.14s ease; }
.step-tab:hover:not([disabled]) { color: var(--text); }
.step-tab.active { background: var(--surface); color: var(--text); font-weight: 600; box-shadow: var(--shadow-sm); }
.step-tab[disabled] { opacity: 0.45; cursor: not-allowed; }
.step-index { display: inline-grid; place-items: center; width: 20px; height: 20px; border-radius: 999px; background: var(--border-strong); color: var(--text-secondary); font-size: 11px; font-weight: 700; }
.step-tab.active .step-index { background: var(--accent-600); color: #fff; }
.step-tab.done .step-index { background: var(--success); color: #fff; }

/* ── 提示条 ─────────────────────────────────────── */
.notice { display: flex; align-items: flex-start; gap: 10px; padding: 12px 14px; border-radius: var(--radius-sm); font-size: 13px; line-height: 1.5; }
.notice.info { background: var(--accent-50); color: var(--accent-900); border: 1px solid var(--accent-100); }
.notice.success { background: var(--success-subtle); color: var(--success-text); border: 1px solid var(--success-border); }
.notice.error { background: var(--danger-subtle); color: var(--danger-text); border: 1px solid var(--danger-border); }
.notice.warning { background: var(--warning-subtle); color: var(--warning-text); border: 1px solid var(--warning-border); }

/* ── 加载 / 空态 ────────────────────────────────── */
.spinner { width: 15px; height: 15px; flex: none; border: 2px solid var(--border-strong); border-top-color: var(--accent-600); border-radius: 50%; animation: spin 0.7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.loading-line { display: flex; align-items: center; gap: 10px; color: var(--text-muted); font-size: 13px; padding: 18px 0; }
.skeleton { position: relative; overflow: hidden; background: var(--surface-muted); border-radius: var(--radius-sm); }
.skeleton::after { content: ""; position: absolute; inset: 0; background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.06), transparent); animation: shimmer 1.4s infinite; }
@keyframes shimmer { from { transform: translateX(-100%); } to { transform: translateX(100%); } }

/* ── 知识树 ─────────────────────────────────────── */
.tree-topic { border: 1px solid var(--glass-border); border-radius: var(--radius-sm); overflow: hidden; }
.tree-topic + .tree-topic { margin-top: 10px; }
.tree-topic-head { display: flex; align-items: center; gap: 10px; padding: 10px 14px; background: var(--surface-subtle); border-bottom: 1px solid var(--border); font-weight: 600; font-size: 13px; color: var(--text); }
.tree-topic-head .code { color: var(--text-muted); font-family: var(--font-mono); font-size: 11.5px; }
.tree-unit { padding: 10px 14px; border-bottom: 1px solid var(--border); }
.tree-unit:last-child { border-bottom: 0; }
.tree-unit-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.tree-unit-head b { font-size: 13px; font-weight: 600; color: var(--text); }
.tree-cards { display: flex; flex-direction: column; gap: 6px; margin: 8px 0 0 18px; }
.tree-card { padding: 8px 12px; border: 1px solid var(--glass-border); border-radius: var(--radius-sm); background: var(--glass); font-size: 12.5px; color: var(--text); }
.tree-card b { font-weight: 600; }
.tree-card .meta { color: var(--text-muted); font-size: 11.5px; margin-top: 2px; }

/* ── 题目预览 ───────────────────────────────────── */
.question-card { border: 1px solid var(--glass-border); border-radius: var(--radius-sm); overflow: hidden; }
.question-card + .question-card { margin-top: 10px; }
.question-head { display: flex; align-items: center; gap: 10px; padding: 10px 14px; background: var(--surface-subtle); border-bottom: 1px solid var(--border); font-size: 12.5px; color: var(--text); }
.question-head .no { font-family: var(--font-mono); font-weight: 600; }
.question-body { padding: 14px; display: flex; flex-direction: column; gap: 10px; font-size: 13px; color: var(--text); }
.question-body .stem { line-height: 1.7; white-space: pre-wrap; }
.question-body .options { display: flex; flex-direction: column; gap: 6px; margin: 0; padding: 0; list-style: none; }
.question-body .options li { padding: 7px 10px; border: 1px solid var(--glass-border); border-radius: var(--radius-sm); font-size: 12.5px; }
.answer-line { display: flex; gap: 10px; padding: 8px 12px; background: var(--success-subtle); border: 1px solid var(--success-border); border-radius: var(--radius-sm); color: var(--success-text); font-size: 12.5px; }
.explain-line { color: var(--text-secondary); font-size: 12.5px; line-height: 1.7; border-left: 3px solid var(--border-strong); padding-left: 10px; }

/* ── 杂项 ───────────────────────────────────────── */
.mono { font-family: var(--font-mono); font-size: 0.92em; }
.muted { color: var(--text-muted); }
.small { font-size: 12px; }
.section-title { margin: 8px 0 2px; font-size: 13px; font-weight: 700; color: var(--text); }
.code-block { padding: 12px 14px; background: var(--surface-subtle); border: 1px solid var(--border); border-radius: var(--radius-sm); font-family: var(--font-mono); font-size: 11.5px; line-height: 1.7; overflow-x: auto; white-space: pre-wrap; word-break: break-all; }
.divider { margin: 4px 0; border: 0; border-top: 1px solid var(--border); }
.page-head { display: flex; align-items: flex-end; gap: 14px; flex-wrap: wrap; }
.page-head h2 { margin: 0; font-size: 22px; font-weight: 600; letter-spacing: -0.02em; color: var(--text); }
.page-head .desc { color: var(--text-muted); font-size: 13px; margin: 2px 0 0; }
.page-head .spacer { flex: 1; }
.kv { display: grid; grid-template-columns: auto 1fr; gap: 4px 14px; font-size: 12.5px; }
.kv dt { color: var(--text-muted); }
.kv dd { margin: 0; color: var(--text); }

@media (max-width: 900px) { .sidebar { display: none; } .content { padding: 16px; } }
```

- [ ] **Step 2: 构建验证（CSS 无语法错误、TS 通过）**

Run: `npm run build`
Expected: 构建成功，无 `tsc` 报错，产出 `dist/`（styles 暗色生效）

- [ ] **Step 3: 视觉冒烟（启动开发服务器人工确认暗色）**

Run: `npm run dev`
Expected: 浏览器打开 `http://localhost:5173`，整页石墨近黑底色，侧栏毛玻璃半透明，卡片白边 1px 半透明，按钮蓝 `#0a84ff`，文字 `#f5f5f7`。无残留浅色块。

- [ ] **Step 4: 提交**

```bash
git add frontend/src/styles.css
git commit -m "style: rewrite styles.css to dark apple-graphite design system"
```

---

## Task 3: 两层导航模型（nav.ts 纯函数）

**Files:**
- Create: `frontend/src/console/nav.ts`
- Test: `frontend/src/console/nav.test.ts`

- [ ] **Step 1: 写失败的测试**

Create `frontend/src/console/nav.test.ts`:
```typescript
import { describe, it, expect } from 'vitest'
import { openCourseSpace, goToSection, isCourseSpace, COURSE_SECTIONS, SECTION_LABELS } from './nav'
import type { Course } from './types'

const course: Course = { id: 'c1', name: '大模型调优', slug: 'sk3020' }

describe('nav', () => {
  it('openCourseSpace lands on course-space home', () => {
    const r = openCourseSpace(course)
    expect(r.page).toBe('course-space')
    if (r.page === 'course-space') {
      expect(r.course.id).toBe('c1')
      expect(r.section).toBe('home')
    }
  })

  it('goToSection switches section within course-space, preserves course', () => {
    const r = openCourseSpace(course)
    const next = goToSection(r, 'knowledge')
    expect(next).not.toBe(r)
    if (next.page === 'course-space') {
      expect(next.section).toBe('knowledge')
      expect(next.course.id).toBe('c1')
    }
  })

  it('goToSection is no-op when not in course-space', () => {
    const r = { page: 'courses' as const }
    expect(goToSection(r, 'knowledge')).toBe(r)
  })

  it('isCourseSpace narrows route type', () => {
    expect(isCourseSpace(openCourseSpace(course))).toBe(true)
    expect(isCourseSpace({ page: 'courses' })).toBe(false)
  })

  it('COURSE_SECTIONS covers materials/framework/knowledge/projects', () => {
    const keys = COURSE_SECTIONS.map((s) => s.key)
    expect(keys).toEqual(['materials', 'framework', 'knowledge', 'projects'])
  })

  it('SECTION_LABELS has a label for every section', () => {
    expect(SECTION_LABELS.home).toBe('课程空间')
    expect(SECTION_LABELS.knowledge).toBe('知识目录')
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npx vitest run src/console/nav.test.ts`
Expected: FAIL（`Cannot find module './nav'`）

- [ ] **Step 3: 实现 nav.ts**

Create `frontend/src/console/nav.ts`:
```typescript
import type { Course } from './types'

export type CourseSection = 'home' | 'materials' | 'framework' | 'knowledge' | 'projects'

export type Route =
  | { page: 'courses' }
  | { page: 'course-space'; course: Course; section: CourseSection }
  | { page: 'exam-project'; course: Course; projectId: string }
  | { page: 'demo' }

export const COURSE_SECTIONS: { key: CourseSection; label: string }[] = [
  { key: 'materials', label: '资料库' },
  { key: 'framework', label: '命题框架' },
  { key: 'knowledge', label: '知识目录' },
  { key: 'projects', label: '试卷项目' },
]

export const SECTION_LABELS: Record<CourseSection, string> = {
  home: '课程空间',
  materials: '资料库',
  framework: '命题框架',
  knowledge: '知识目录',
  projects: '试卷项目',
}

/** 打开课程空间，默认落在首页（四宫格概览）。 */
export function openCourseSpace(course: Course): Route {
  return { page: 'course-space', course, section: 'home' }
}

/** 在课程空间内切换区块；非课程空间路由原样返回。 */
export function goToSection(route: Route, section: CourseSection): Route {
  if (route.page !== 'course-space') return route
  return { ...route, section }
}

export function isCourseSpace(route: Route): route is Extract<Route, { page: 'course-space' }> {
  return route.page === 'course-space'
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `npx vitest run src/console/nav.test.ts`
Expected: PASS（`6 passed`）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/console/nav.ts frontend/src/console/nav.test.ts
git commit -m "feat(console): add two-layer navigation model with pure helpers"
```

---

## Task 4: 追加领域类型（ExamProjectSummary / CourseReadiness）

**Files:**
- Modify: `frontend/src/console/types.ts`（文件末尾追加）

- [ ] **Step 1: 写失败的类型契约测试**

Create `frontend/src/console/readiness-types.test.ts`:
```typescript
import { describe, it, expectTypeOf } from 'vitest'
import type { CourseReadiness, ExamProjectSummary } from './types'

describe('readiness types', () => {
  it('CourseReadiness shape', () => {
    const r: CourseReadiness = {
      materialsReady: true,
      frameworkReady: true,
      frameworkVersion: 'v3',
      knowledgeReady: true,
      knowledgeVersion: 'v8',
      knowledgeCardCount: 37,
      knowledgeUngroundedCount: 3,
      projects: [],
    }
    expectTypeOf(r).toMatchTypeOf<CourseReadiness>()
  })

  it('ExamProjectSummary status union', () => {
    const s: ExamProjectSummary['status'] = 'review'
    expectTypeOf(s).toEqualTypeOf<'draft' | 'blueprint' | 'contract' | 'generating' | 'review' | 'exported'>()
  })
})
```

- [ ] **Step 2: 运行确认失败（类型不存在）**

Run: `npx vitest run src/console/readiness-types.test.ts`
Expected: FAIL（`Module '"./types"' has no exported member 'CourseReadiness'`）

- [ ] **Step 3: 在 types.ts 末尾追加类型**

在 `frontend/src/console/types.ts` 末尾追加：
```typescript

// ── 课程空间首页就绪度（Plan 1 地基） ────────────────
export type ExamProjectSummary = {
  id: string
  semester_label: string
  status: 'draft' | 'blueprint' | 'contract' | 'generating' | 'review' | 'exported'
  total_score: number
  question_count: number
  pending_review: number
}

export type CourseReadiness = {
  materialsReady: boolean
  frameworkReady: boolean
  frameworkVersion: string | null
  knowledgeReady: boolean
  knowledgeVersion: string | null
  knowledgeCardCount: number
  knowledgeUngroundedCount: number
  projects: ExamProjectSummary[]
}
```

- [ ] **Step 4: 运行确认通过**

Run: `npx vitest run src/console/readiness-types.test.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/src/console/types.ts frontend/src/console/readiness-types.test.ts
git commit -m "feat(console): add CourseReadiness and ExamProjectSummary types"
```

---

## Task 5: 布局外壳 Layout（侧栏 + 顶栏）

**Files:**
- Create: `frontend/src/console/shell/Layout.tsx`
- Test: `frontend/src/console/shell/Layout.test.tsx`

- [ ] **Step 1: 写失败的组件测试**

Create `frontend/src/console/shell/Layout.test.tsx`:
```typescript
import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../../test/render'
import { Layout } from './Layout'
import { openCourseSpace, type Route } from '../nav'
import type { Course } from '../types'

const course: Course = { id: 'c1', name: '大模型调优', slug: 'sk3020' }

describe('Layout', () => {
  it('renders brand and section nav in course-space', async () => {
    const onNavigate = vi.fn()
    const route = openCourseSpace(course)
    renderWithProviders(
      <Layout route={route} onNavigateSection={onNavigate} onBackToCourses={() => {}} onOpenDemo={() => {}}>
        <div>CONTENT</div>
      </Layout>,
    )
    expect(screen.getByText('砚卷工作台')).toBeInTheDocument()
    expect(screen.getByText('大模型调优')).toBeInTheDocument()
    expect(screen.getByText('知识目录')).toBeInTheDocument()
    expect(screen.getByText('CONTENT')).toBeInTheDocument()
  })

  it('clicking a section calls onNavigateSection with that key', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    const route = openCourseSpace(course)
    renderWithProviders(
      <Layout route={route} onNavigateSection={onNavigate} onBackToCourses={() => {}} onOpenDemo={() => {}}>
        <div />
      </Layout>,
    )
    await user.click(screen.getByText('命题框架'))
    expect(onNavigate).toHaveBeenCalledWith('framework')
  })

  it('shows courses-list nav when not in workspace', () => {
    const route: Route = { page: 'courses' }
    renderWithProviders(
      <Layout route={route} onNavigateSection={() => {}} onBackToCourses={() => {}} onOpenDemo={() => {}}>
        <div />
      </Layout>,
    )
    expect(screen.getByText('课程列表')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: 运行确认失败**

Run: `npx vitest run src/console/shell/Layout.test.tsx`
Expected: FAIL（`Cannot find module './Layout'`）

- [ ] **Step 3: 实现 Layout**

Create `frontend/src/console/shell/Layout.tsx`:
```tsx
import type { ReactNode } from 'react'
import { COURSE_SECTIONS, SECTION_LABELS, type CourseSection, type Route } from '../nav'

export function Layout({ route, onNavigateSection, onBackToCourses, onOpenDemo, children }: {
  route: Route
  onNavigateSection: (section: CourseSection) => void
  onBackToCourses: () => void
  onOpenDemo: () => void
  children: ReactNode
}) {
  const inWorkspace = route.page === 'course-space' || route.page === 'exam-project'
  const course = inWorkspace ? route.course : null
  const section: CourseSection = route.page === 'course-space' ? route.section : 'home'

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <div className="brand-mark">卷</div>
          <div className="brand-copy">
            <b>砚卷工作台</b>
            <span>教师控制台</span>
          </div>
        </div>

        {inWorkspace && course ? (
          <>
            <div className="sidebar-course">
              <b>{course.name}</b>
              <span>{course.slug}</span>
            </div>
            <div className="sidebar-section">课程资产</div>
            <button
              className={`nav-item${section === 'home' ? ' active' : ''}`}
              onClick={() => onNavigateSection('home')}
            >
              <span className="dot" />
              {SECTION_LABELS.home}
            </button>
            {COURSE_SECTIONS.map((s) => (
              <button
                key={s.key}
                className={`nav-item${section === s.key ? ' active' : ''}`}
                onClick={() => onNavigateSection(s.key)}
              >
                <span className="dot" />
                {s.label}
              </button>
            ))}
          </>
        ) : (
          <>
            <div className="sidebar-section">导航</div>
            <button className="nav-item active">
              <span className="dot" />
              课程列表
            </button>
          </>
        )}

        <div className="sidebar-foot">
          <button className="nav-item" onClick={inWorkspace ? onBackToCourses : onOpenDemo}>
            <span className="dot" />
            {inWorkspace ? '返回课程列表' : '旧版演示流程'}
          </button>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          {inWorkspace && course ? (
            <>
              <span className="crumb-link" onClick={onBackToCourses}>课程</span>
              <span className="crumb-sep">/</span>
              <h1>{course.name}</h1>
              <span className="muted small">{SECTION_LABELS[section]}</span>
            </>
          ) : (
            <h1>课程列表</h1>
          )}
          <div className="topbar-actions">
            {inWorkspace && course ? <span className="muted small mono">{course.slug}</span> : null}
          </div>
        </header>

        <main className="content">{children}</main>
      </div>
    </div>
  )
}
```

- [ ] **Step 4: 运行确认通过**

Run: `npx vitest run src/console/shell/Layout.test.tsx`
Expected: PASS（`3 passed`）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/console/shell/Layout.tsx frontend/src/console/shell/Layout.test.tsx
git commit -m "feat(console): add Layout shell with two-layer sidebar/topbar"
```

---

## Task 6: 课程空间首页 CourseSpaceHome

**Files:**
- Create: `frontend/src/console/CourseSpaceHome.tsx`
- Test: `frontend/src/console/CourseSpaceHome.test.tsx`

- [ ] **Step 1: 写失败的组件测试**

Create `frontend/src/console/CourseSpaceHome.test.tsx`:
```typescript
import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../test/render'
import { CourseSpaceHome } from './CourseSpaceHome'
import type { CourseReadiness } from './types'

const ready: CourseReadiness = {
  materialsReady: true,
  frameworkReady: true,
  frameworkVersion: 'v3',
  knowledgeReady: true,
  knowledgeVersion: 'v8',
  knowledgeCardCount: 37,
  knowledgeUngroundedCount: 3,
  projects: [
    { id: 'p1', semester_label: '2026-2027-I', status: 'review', total_score: 100, question_count: 37, pending_review: 2 },
  ],
}

describe('CourseSpaceHome', () => {
  it('renders status pills from readiness', () => {
    renderWithProviders(<CourseSpaceHome readiness={ready} onOpenSection={() => {}} />)
    expect(screen.getByText('资料库')).toBeInTheDocument()
    expect(screen.getByText('published v3')).toBeInTheDocument()
    expect(screen.getByText(/37 卡/)).toBeInTheDocument()
    expect(screen.getByText(/3 未落地/)).toBeInTheDocument()
    expect(screen.getByText('1 个')).toBeInTheDocument()
  })

  it('clicking a card calls onOpenSection with that section', async () => {
    const user = userEvent.setup()
    const onOpen = vi.fn()
    renderWithProviders(<CourseSpaceHome readiness={ready} onOpenSection={onOpen} />)
    await user.click(screen.getByText('知识目录'))
    expect(onOpen).toHaveBeenCalledWith('knowledge')
  })

  it('renders neutral pill when knowledge not ready', () => {
    renderWithProviders(
      <CourseSpaceHome readiness={{ ...ready, knowledgeReady: false, knowledgeVersion: null, knowledgeCardCount: 0, knowledgeUngroundedCount: 0 }} onOpenSection={() => {}} />,
    )
    expect(screen.getByText('未发布')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: 运行确认失败**

Run: `npx vitest run src/console/CourseSpaceHome.test.tsx`
Expected: FAIL（`Cannot find module './CourseSpaceHome'`）

- [ ] **Step 3: 实现 CourseSpaceHome**

Create `frontend/src/console/CourseSpaceHome.tsx`:
```tsx
import { Pill } from './ui'
import type { CourseReadiness } from './types'
import type { CourseSection } from './nav'

export function CourseSpaceHome({ readiness, onOpenSection }: {
  readiness: CourseReadiness
  onOpenSection: (section: CourseSection) => void
}) {
  const inProgress = readiness.projects.filter((p) => p.status !== 'exported').length
  return (
    <div className="content-inner">
      <div className="page-head">
        <h2>课程空间</h2>
        <div className="desc">长期可复用的课程级资产。维护一次，用于多个学期的命题。</div>
      </div>
      <div className="metric-row">
        <button className="metric" onClick={() => onOpenSection('materials')}>
          <span className="eyebrow">资料库</span>
          <Pill kind={readiness.materialsReady ? 'success' : 'neutral'}>
            {readiness.materialsReady ? '已就绪' : '未就绪'}
          </Pill>
          <span className="meta">四区文件 · 上传一次多次复用</span>
        </button>
        <button className="metric" onClick={() => onOpenSection('framework')}>
          <span className="eyebrow">命题框架</span>
          <Pill kind={readiness.frameworkReady ? 'success' : 'neutral'}>
            {readiness.frameworkReady ? `published ${readiness.frameworkVersion ?? ''}`.trim() : '未构建'}
          </Pill>
          <span className="meta">双大纲 → 考点表</span>
        </button>
        <button className="metric" onClick={() => onOpenSection('knowledge')}>
          <span className="eyebrow">知识目录</span>
          <Pill kind={readiness.knowledgeReady ? 'success' : 'neutral'}>
            {readiness.knowledgeReady ? `published ${readiness.knowledgeVersion ?? ''}`.trim() : '未发布'}
          </Pill>
          <span className="meta">
            {readiness.knowledgeReady
              ? `${readiness.knowledgeCardCount} 卡${readiness.knowledgeUngroundedCount > 0 ? ` · ${readiness.knowledgeUngroundedCount} 未落地` : ''}`
              : '图谱+树双视图'}
          </span>
        </button>
        <button className="metric" onClick={() => onOpenSection('projects')}>
          <span className="eyebrow">试卷项目</span>
          <Pill kind="neutral">{readiness.projects.length} 个</Pill>
          <span className="meta">{inProgress} 进行中</span>
        </button>
      </div>
    </div>
  )
}
```

- [ ] **Step 4: 运行确认通过**

Run: `npx vitest run src/console/CourseSpaceHome.test.tsx`
Expected: PASS（`3 passed`）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/console/CourseSpaceHome.tsx frontend/src/console/CourseSpaceHome.test.tsx
git commit -m "feat(console): add CourseSpaceHome with four status metric cards"
```

---

## Task 7: 知识目录只读树 PublishedTreeBrowse

**Files:**
- Create: `frontend/src/console/PublishedTreeBrowse.tsx`
- Test: `frontend/src/console/PublishedTreeBrowse.test.tsx`

- [ ] **Step 1: 写失败的组件测试（mock knowledgeApi）**

Create `frontend/src/console/PublishedTreeBrowse.test.tsx`:
```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/render'
import { PublishedTreeBrowse } from './PublishedTreeBrowse'

vi.mock('./client', () => ({
  knowledgeApi: {
    getPublished: vi.fn(),
  },
}))

import { knowledgeApi } from './client'

beforeEach(() => vi.clearAllMocks())

describe('PublishedTreeBrowse', () => {
  it('renders published knowledge tree', async () => {
    ;(knowledgeApi.getPublished as ReturnType<typeof vi.fn>).mockResolvedValue({
      catalog_version_id: 'cat-1',
      framework_version_id: 'fw-1',
      exam_points: [{ id: 'ep1', code: 'EP2', title: '参数高效微调', assessment_requirement: '', anchor_key: 'a', weight_value: 25, weight_source: '', cognitive_targets: [], allowed_question_types: [], operational_detail_policy: '' }],
      units: [{ unit_id: 'u1', code: 'AU03', title: '参数高效微调', performance_statement: '', exam_point_id: 'ep1', exam_point_code: 'EP2', anchor_key: 'a', card_ids: ['k1'] }],
      knowledge_cards: {
        k1: { name: 'QLoRA 量化微调', performance_statement: '', assessable_content: ['NF4 量化', '双量化'], scope_boundary: {}, cognitive_targets: [], allowed_question_types: [], importance: 4, concept_cluster: '量化微调簇', answer_proposition: '', answer_boundary: '', prompt_material: [] },
      },
    })
    renderWithProviders(<PublishedTreeBrowse courseId="c1" />)
    await waitFor(() => expect(screen.getByText('QLoRA 量化微调')).toBeInTheDocument())
    expect(screen.getByText(/2 原子/)).toBeInTheDocument()
    expect(screen.getByText('量化微调簇')).toBeInTheDocument()
  })

  it('shows warning when knowledge not published', async () => {
    ;(knowledgeApi.getPublished as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('not found'))
    renderWithProviders(<PublishedTreeBrowse courseId="c1" />)
    await waitFor(() => expect(screen.getByText(/尚未发布知识目录/)).toBeInTheDocument())
  })
})
```

- [ ] **Step 2: 运行确认失败**

Run: `npx vitest run src/console/PublishedTreeBrowse.test.tsx`
Expected: FAIL（`Cannot find module './PublishedTreeBrowse'`）

- [ ] **Step 3: 实现 PublishedTreeBrowse**

Create `frontend/src/console/PublishedTreeBrowse.tsx`:
```tsx
import { useEffect, useState } from 'react'
import { knowledgeApi } from './client'
import { Card, LoadingLine, EmptyState, Notice } from './ui'
import type { PublishedKnowledge } from './types'

export function PublishedTreeBrowse({ courseId }: { courseId: string }) {
  const [data, setData] = useState<PublishedKnowledge | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    knowledgeApi
      .getPublished(courseId)
      .then((d) => {
        if (!cancelled) setData(d)
      })
      .catch(() => {
        if (!cancelled) setError('尚未发布知识目录，请先完成知识整理。')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [courseId])

  if (loading) return <LoadingLine>加载知识目录…</LoadingLine>
  if (error) return <Notice kind="warning">{error}</Notice>
  if (!data) return <EmptyState>无知识目录</EmptyState>

  const cards = data.knowledge_cards
  return (
    <div className="content-inner">
      <div className="page-head">
        <h2>知识目录</h2>
        <div className="desc">只读浏览。图谱+树双视图与证据中心将在后续交付。</div>
      </div>
      <Card title={`已发布 · ${Object.keys(cards).length} 张知识卡`} sub={`基于框架 v${(data.framework_version_id ?? '').slice(0, 8)}`}>
        {data.exam_points.map((ep) => {
          const units = data.units.filter((u) => u.exam_point_id === ep.id)
          return (
            <div className="tree-topic" key={ep.id}>
              <div className="tree-topic-head">
                <span className="code">{ep.code}</span>
                {ep.title}
              </div>
              {units.map((u) => (
                <div className="tree-unit" key={u.unit_id}>
                  <div className="tree-unit-head">
                    <b>{u.code}</b>
                    <span className="muted small">{u.title}</span>
                  </div>
                  <div className="tree-cards">
                    {u.card_ids.map((cid) => {
                      const c = cards[cid]
                      if (!c) return null
                      return (
                        <div className="tree-card" key={cid}>
                          <b>{c.name}</b>
                          <div className="meta">
                            {c.assessable_content.length} 原子 · {c.concept_cluster}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
              ))}
            </div>
          )
        })}
      </Card>
    </div>
  )
}
```

- [ ] **Step 4: 运行确认通过**

Run: `npx vitest run src/console/PublishedTreeBrowse.test.tsx`
Expected: PASS（`2 passed`）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/console/PublishedTreeBrowse.tsx frontend/src/console/PublishedTreeBrowse.test.tsx
git commit -m "feat(console): add read-only PublishedTreeBrowse for knowledge catalog"
```

---

## Task 8: 试卷项目列表骨架 ExamProjectList

**Files:**
- Create: `frontend/src/console/ExamProjectList.tsx`
- Test: `frontend/src/console/ExamProjectList.test.tsx`

- [ ] **Step 1: 写失败的组件测试**

Create `frontend/src/console/ExamProjectList.test.tsx`:
```typescript
import { describe, it, expect } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from '../test/render'
import { ExamProjectList } from './ExamProjectList'

describe('ExamProjectList', () => {
  it('renders the S2 skeleton notice and empty list', () => {
    renderWithProviders(<ExamProjectList />)
    expect(screen.getByText('试卷项目')).toBeInTheDocument()
    expect(screen.getByText(/S2/)).toBeInTheDocument()
    expect(screen.getByText(/暂无试卷项目/)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: 运行确认失败**

Run: `npx vitest run src/console/ExamProjectList.test.tsx`
Expected: FAIL（`Cannot find module './ExamProjectList'`）

- [ ] **Step 3: 实现 ExamProjectList**

Create `frontend/src/console/ExamProjectList.tsx`:
```tsx
import { Card, EmptyState, Notice } from './ui'

export function ExamProjectList() {
  return (
    <div className="content-inner">
      <div className="page-head">
        <h2>试卷项目</h2>
        <div className="desc">按学期归档的单次命题对象。进入后是 5 阶段生产线。</div>
      </div>
      <Notice kind="info">
        试卷项目服务（蓝图 → 合同 → 生成 → 审核 → 导出 5 阶段生产线）将在 S2 交付。当前为入口骨架。
      </Notice>
      <Card title="项目列表">
        <EmptyState>暂无试卷项目（后端 PaperVersion 内核待 S2 落地）</EmptyState>
      </Card>
    </div>
  )
}
```

- [ ] **Step 4: 运行确认通过**

Run: `npx vitest run src/console/ExamProjectList.test.tsx`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/src/console/ExamProjectList.tsx frontend/src/console/ExamProjectList.test.tsx
git commit -m "feat(console): add ExamProjectList skeleton placeholder"
```

---

## Task 9: App.tsx 两层路由装配（集成）

**Files:**
- Modify: `frontend/src/App.tsx`（整文件重写）
- Test: `frontend/src/App.test.tsx`

- [ ] **Step 1: 写失败的集成测试（mock client）**

Create `frontend/src/App.test.tsx`:
```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './test/render'
import App from './App'

vi.mock('./console/client', () => ({
  coursesApi: { list: vi.fn(), create: vi.fn() },
  materialsApi: { list: vi.fn(), upload: vi.fn(), remove: vi.fn(), startParse: vi.fn(), pollParse: vi.fn() },
  frameworkApi: { createRun: vi.fn(), getCandidate: vi.fn(), confirm: vi.fn(), reject: vi.fn(), getCurrent: vi.fn() },
  knowledgeApi: { createRun: vi.fn(), getCandidate: vi.fn(), publish: vi.fn(), getPublished: vi.fn() },
  examApi: { allocate: vi.fn(), confirm: vi.fn(), generate: vi.fn() },
}))

import { coursesApi, materialsApi, frameworkApi, knowledgeApi } from './console/client'

beforeEach(() => {
  vi.clearAllMocks()
  ;(coursesApi.list as ReturnType<typeof vi.fn>).mockResolvedValue([
    { id: 'c1', name: '大模型调优', slug: 'sk3020' },
  ])
  ;(materialsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue([])
  ;(frameworkApi.getCurrent as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('no framework'))
  ;(knowledgeApi.getPublished as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('no knowledge'))
})

describe('App two-layer navigation', () => {
  it('starts at courses list', async () => {
    renderWithProviders(<App />)
    await waitFor(() => expect(screen.getByText('大模型调优')).toBeInTheDocument())
    expect(screen.getByText('课程列表')).toBeInTheDocument()
  })

  it('opening a course lands on course-space home', async () => {
    const user = userEvent.setup()
    renderWithProviders(<App />)
    await waitFor(() => expect(screen.getByText('大模型调优')).toBeInTheDocument())
    await user.click(screen.getByText('大模型调优'))
    await waitFor(() => expect(screen.getByText('课程空间')).toBeInTheDocument())
    expect(screen.getByText('未发布')).toBeInTheDocument() // knowledge not ready
  })

  it('clicking knowledge section in sidebar shows browse', async () => {
    const user = userEvent.setup()
    ;(knowledgeApi.getPublished as ReturnType<typeof vi.fn>).mockResolvedValue({
      catalog_version_id: 'cat-1', framework_version_id: 'fw-1',
      exam_points: [{ id: 'ep1', code: 'EP2', title: '参数高效微调', assessment_requirement: '', anchor_key: 'a', weight_value: 25, weight_source: '', cognitive_targets: [], allowed_question_types: [], operational_detail_policy: '' }],
      units: [{ unit_id: 'u1', code: 'AU03', title: '参数高效微调', performance_statement: '', exam_point_id: 'ep1', exam_point_code: 'EP2', anchor_key: 'a', card_ids: ['k1'] }],
      knowledge_cards: { k1: { name: 'QLoRA', performance_statement: '', assessable_content: ['a'], scope_boundary: {}, cognitive_targets: [], allowed_question_types: [], importance: 4, concept_cluster: '簇', answer_proposition: '', answer_boundary: '', prompt_material: [] } },
    })
    renderWithProviders(<App />)
    await waitFor(() => expect(screen.getByText('大模型调优')).toBeInTheDocument())
    await user.click(screen.getByText('大模型调优'))
    await waitFor(() => expect(screen.getByText('课程空间')).toBeInTheDocument())
    // 侧栏点"知识目录"
    const knowledgeNav = screen.getAllByText('知识目录').find((el) => el.closest('.nav-item'))
    await user.click(knowledgeNav!)
    await waitFor(() => expect(screen.getByText('QLoRA')).toBeInTheDocument())
  })
})
```

- [ ] **Step 2: 运行确认失败**

Run: `npx vitest run src/App.test.tsx`
Expected: FAIL（旧 App 还是线性 wizard，无"课程空间"首页；测试断言失败）

- [ ] **Step 3: 重写 App.tsx 为两层路由**

整文件覆盖 `frontend/src/App.tsx`:
```tsx
/**
 * 教师控制台应用壳：两层导航（课程空间 + 试卷项目）。
 * 课程空间=长期可复用资产；试卷项目=按学期归档的单次命题。
 */

import { useCallback, useEffect, useState } from 'react'
import { CoursesPage } from './console/CoursesPage'
import { MaterialsStep } from './console/steps/MaterialsStep'
import { FrameworkStep } from './console/steps/FrameworkStep'
import { CourseSpaceHome } from './console/CourseSpaceHome'
import { PublishedTreeBrowse } from './console/PublishedTreeBrowse'
import { ExamProjectList } from './console/ExamProjectList'
import { Layout } from './console/shell/Layout'
import { frameworkApi, knowledgeApi, materialsApi } from './console/client'
import { openCourseSpace, goToSection, type CourseSection, type Route } from './console/nav'
import type { Course, CourseReadiness, Material } from './console/types'
import { DemoApp } from './demo/DemoApp'

const EMPTY_READINESS: CourseReadiness = {
  materialsReady: false,
  frameworkReady: false,
  frameworkVersion: null,
  knowledgeReady: false,
  knowledgeVersion: null,
  knowledgeCardCount: 0,
  knowledgeUngroundedCount: 0,
  projects: [],
}

export default function App() {
  const [route, setRoute] = useState<Route>({ page: 'courses' })
  const [materials, setMaterials] = useState<Material[]>([])
  const [readiness, setReadiness] = useState<CourseReadiness>(EMPTY_READINESS)

  const refreshMaterials = useCallback(async (courseId: string) => {
    try {
      setMaterials(await materialsApi.list(courseId))
    } catch {
      setMaterials([])
    }
  }, [])

  const refreshReadiness = useCallback(async (courseId: string) => {
    const next: CourseReadiness = { ...EMPTY_READINESS }
    try {
      await frameworkApi.getCurrent(courseId)
      next.frameworkReady = true
    } catch {
      next.frameworkReady = false
    }
    try {
      const k = await knowledgeApi.getPublished(courseId)
      next.knowledgeReady = true
      next.knowledgeVersion = (k.catalog_version_id ?? '').slice(0, 8) || null
      next.knowledgeCardCount = Object.keys(k.knowledge_cards).length
    } catch {
      next.knowledgeReady = false
    }
    setReadiness(next)
  }, [])

  const openCourse = useCallback(
    async (course: Course) => {
      setRoute(openCourseSpace(course))
      setMaterials([])
      setReadiness(EMPTY_READINESS)
      await refreshMaterials(course.id)
      await refreshReadiness(course.id)
    },
    [refreshMaterials, refreshReadiness],
  )

  const backToCourses = useCallback(() => {
    setRoute({ page: 'courses' })
    setMaterials([])
  }, [])

  const navigateSection = useCallback((section: CourseSection) => {
    setRoute((r) => goToSection(r, section))
  }, [])

  // 工作台视图下轮询进行中的解析状态（与资料步骤内部轮询互补）
  useEffect(() => {
    if (route.page !== 'course-space' && route.page !== 'exam-project') return
    const hasActive = materials.some(
      (m) => m.parse_status != null && !['ready', 'failed'].includes(m.parse_status.status),
    )
    if (!hasActive) return
    const timer = setInterval(() => {
      void refreshMaterials(route.course.id)
    }, 4000)
    return () => clearInterval(timer)
  }, [route, materials, refreshMaterials])

  if (route.page === 'demo') {
    return (
      <div style={{ minHeight: '100vh' }}>
        <div style={{ maxWidth: 1160, margin: '0 auto', padding: '16px 24px' }}>
          <button className="btn ghost" onClick={() => setRoute({ page: 'courses' })}>
            ← 返回教师控制台
          </button>
        </div>
        <DemoApp />
      </div>
    )
  }

  return (
    <Layout
      route={route}
      onNavigateSection={navigateSection}
      onBackToCourses={backToCourses}
      onOpenDemo={() => setRoute({ page: 'demo' })}
    >
      {route.page === 'courses' ? (
        <CoursesPage onOpen={(course) => void openCourse(course)} />
      ) : route.page === 'course-space' ? (
        route.section === 'home' ? (
          <CourseSpaceHome readiness={readiness} onOpenSection={navigateSection} />
        ) : route.section === 'materials' ? (
          <MaterialsStep
            courseId={route.course.id}
            materials={materials}
            onRefresh={() => refreshMaterials(route.course.id)}
          />
        ) : route.section === 'framework' ? (
          <FrameworkStep
            courseId={route.course.id}
            materials={materials}
            onDone={() => refreshReadiness(route.course.id)}
          />
        ) : route.section === 'knowledge' ? (
          <PublishedTreeBrowse courseId={route.course.id} />
        ) : (
          <ExamProjectList />
        )
      ) : route.page === 'exam-project' ? (
        <div className="content-inner">
          <div className="page-head">
            <h2>试卷项目生产线</h2>
            <div className="desc">蓝图 → 合同 → 生成 → 审核 → 导出 · 5 阶段流水线将在 Plan 3 交付。</div>
          </div>
          <Notice placeholder>S2/S3 占位</Notice>
        </div>
      ) : null}
    </Layout>
  )
}

import { Notice } from './console/ui'
```

> 注：`exam-project` 路由当前不可达（项目列表骨架无数据），仅为类型完备保留。`Notice` 导入置于文件末尾仅为本步简化展示，实际实现时应合并到文件顶部 import 块中。

- [ ] **Step 4: 修正 import 顺序，运行测试确认通过**

把 `import { Notice } from './console/ui'` 移到文件顶部 import 块（与其它 console 导入并列），删除底部那行。

Run: `npx vitest run src/App.test.tsx`
Expected: PASS（`3 passed`）

- [ ] **Step 5: 全量测试 + 构建确认**

Run: `npx vitest run`
Expected: 全部 PASS（9 个测试文件）
Run: `npm run build`
Expected: `tsc -b` 无报错，`vite build` 产出 `dist/`

- [ ] **Step 6: 人工冒烟（开发服务器全流程）**

Run: `npm run dev`
Expected: 
1. 打开 `http://localhost:5173`，看到暗色苹果灰课程列表。
2. 点课程 → 进入"课程空间"首页，四宫格状态卡（知识目录卡显示"未发布"或"published v8 / 37 卡"）。
3. 侧栏点"知识目录" → 只读树浏览。
4. 侧栏点"试卷项目" → S2 骨架占位。
5. 侧栏"返回课程列表"回到首页。

- [ ] **Step 7: 提交**

```bash
git add frontend/src/App.tsx frontend/src/App.test.tsx
git commit -m "feat(app): refactor App to two-layer navigation with dark layout shell"
```

---

## Self-Review（plan 作者自检，已执行）

**Spec coverage：**
- §1.2 心智模型两层拆分 → Task 3（Route 两层）+ Task 9（App 装配） ✓
- §2 整体信息架构（课程空间四区块 + 试卷项目） → Task 5/6/7/8/9 ✓
- §3 课程空间首页四宫格 → Task 6 ✓
- §6 暗色苹果灰设计系统 → Task 2（完整 styles.css 重写） ✓
- §4 知识目录图谱+树双视图 → **本计划只交付只读树（PublishedTreeBrowse）**，图谱+树双视图与证据中心属 Plan 2，已在 Goal/Spec 注明 ✓（合理拆分）
- §5 试卷项目生产线 → **本计划只交付列表骨架（ExamProjectList）**，5 阶段详情属 Plan 3，已注明 ✓

**Placeholder scan：** Task 9 Step 3 末尾的 `Notice placeholder` 与底部 import 是真实可运行代码（Notice 组件存在），非计划占位；Step 4 已显式要求修正。其余无 TBD/TODO/"add appropriate"。

**Type consistency：** `CourseSection`/`Route` 在 nav.ts（Task 3）定义，被 Layout（Task 5）、CourseSpaceHome（Task 6）、App（Task 9）一致引用；`CourseReadiness`/`ExamProjectSummary` 在 types.ts（Task 4）定义，被 CourseSpaceHome/App 一致引用；`Pill`/`Card`/`Notice`/`LoadingLine`/`EmptyState` 均来自现有 ui.tsx，props 一致。已校验。

---

## 后续计划（不在本计划范围）

- **Plan 2：知识目录图谱+树双视图 + 证据中心**——引入 reactflow 或手搓 SVG 力导向图谱；复用 `relation_edges`/`concept_cluster`；证据中心反向链路（`evidence_chunks`+`knowledge_evidence_links`）。需先核查后端是否已暴露 relation_edges/evidence API。
- **Plan 3：试卷项目生产线**——横向流水线 + 5 阶段单活动详情；依赖 S2 的 PaperVersion 内核与 exam_projects API。
