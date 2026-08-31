# 前端重构计划

## 目标
清理项目前端中的重复、废弃和无用代码，修复构建错误，减少维护负担。

## Phase 1: 删除废弃目录与重复组件
- [x] 扫描并确认空目录和重复文件
- [x] 删除空目录：`src/components/ui`, `src/demo`, `src/hooks`, `src/lib`, `src/pages`
- [x] 删除重复布局组件：`src/console/shell/Layout.tsx` 和 `Layout.test.tsx`
- [x] 统一引用 `src/components/layout/Shell.tsx`

## Phase 2: 修复构建错误
- [x] 更新 `App.tsx` 中的引用
- [x] 确保 TypeScript 构建通过

## Phase 3: 依赖整理
- [x] 将 `@vitejs/plugin-react`, `vite`, `typescript` 移到 `devDependencies`
- [x] 重新安装依赖并验证

## Phase 4: CSS 精简（初步）
- [x] 合并重复的动画和栅格类
- [x] 删除未使用的 CSS 规则（stagger-list/item）

## Phase 5: 代码清理
- [x] 删除未使用的 import（main.tsx 的 React/ReactDOM 命名导入）
- [x] 标记 `dangerouslySetInnerHTML` 等风险点（未发现使用）

## 验证
- [x] `npm run build` 通过
- [x] `npx vitest run` 全部 97 个测试通过
