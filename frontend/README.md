# 教师工作台前端（frontend/）

React 19 + TypeScript + Vite + Zustand 的教师工作台。页面结构、路由与组件职责见
[`CODE_WIKI.md` §10 前端架构](../CODE_WIKI.md)；接口契约见
[`docs/backend-api.md`](../docs/backend-api.md)。

## 命令

```bash
npm install            # 依赖安装（锁文件 package-lock.json，禁止 pnpm/yarn）
npm run dev            # 开发服务器 :5173，/api 已代理到 127.0.0.1:8000
npm run build          # 门禁①：tsc 类型检查 + 产物构建（0 error 才能提交）
npm run lint           # 门禁②：oxlint（当前基线 15 warning，不得增加，2026-10-13 实测）
```

⚠️ **本项目前端没有单测框架**（无 jest/vitest），质量门禁就是 `npm run build` +
`npm run lint`；端到端行为由后端 pytest 锁定。不要为新页面引入测试框架，除非团队正式决议。

## 目录速览

```
src/
├── App.tsx            # 路由表（/login、/courses、/courses/:id/{概览,资料,框架,知识,试卷,
│                      #  归档编辑,对话助手}、/admin/{概览,组织,账号,模型,监控}）
├── pages/             # 教师 9 页（auth / course-space / dashboard / materials / framework /
│                      #  knowledge / paper / paper-archive / assistant）+ admin 后台 5 页
│   └── paper/         # 「试卷」模块：流水线 + 阅读器 + AI 任务面板
│                      #  （AiRevise 改题 / AiCreate 出题 / PaperReview 整卷评审 / BlueprintSuggest 蓝图建议）
├── api/               # HTTP 层：http.ts / client.ts / errors.ts + domains/*（按业务域）
│                      #  ⚠️ 组件内禁止散落 fetch，一律走这里
├── components/        # layout/（Layout + 悬浮岛 Sidebar）+ ui/（Badge Button Card …）
├── stores/            # Zustand：一域一 store（auth / course / toast / assistant）
├── hooks/ lib/        # useNameMaps（id→中文名）、examDisplay（展示常量）
└── types/api.ts       # 后端响应类型的手写镜像（改接口同步改这里）
```

## 约定

- **AI 助手是提案式**：面板/对话只产出提案/diff/报告（202 异步 + `task-runs` 轮询，终态自停），
  落库全部走既有端点不绕确认流；里程碑（确认蓝图/合同）与课程级操作需教师点「确认执行」，
  其余提案助手自动执行并回报执。
- **API 边界防御**：旧数据（如空 `final_exam_rules`）会打崩渲染，缺字段的响应一律兜底
  （详见 `docs/HANDOVER.md` §8.7）。
- 状态一律进 Zustand store，组件里不堆本地 state 冒充全局状态。
