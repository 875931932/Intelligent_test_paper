# docs/superpowers — 设计规格与实施计划存档

> ⚠️ **这是历史档案，不是现行文档。**
> 目录内的规格（`specs/`）与实施计划（`plans/`）都带日期，描述的是**那一天的设计意图与实施步骤**。
> 此后的多轮迭代已经改变了其中大量细节，典型例子：
>
> - 前端早期结构是 `frontend/src/console/`（`console/exam/examProjectWorkspace.tsx` 四/五阶段流水线），
>   现已重构为 `frontend/src/pages/**`，「出卷流水线」与「试卷查看」合并为同一项目详情页的两个页签；
> - 早期计划里的 `frontend/src/console/shell/Layout.tsx`、`console/client.ts`、`console/nav.ts` 等路径
>   均已不存在；
> - 早期计划假设代码在 `.worktrees/core-implementation/` worktree 中开发，现已回到单仓库主分支；
> - 早期计划提到的 `.trae/specs/...` 目录已不存在；
> - 「待建 / demo 查看器 / 前端仅为联调用」等描述均已过时——教师工作台七个页面路由已全部接通真实 API。
>
> **现行结构的唯一权威是仓库里的代码**，配套阅读：
>
> | 想知道什么 | 看哪里 |
> |---|---|
> | 架构 / 领域模型 / 工作流 / API / 数据库 / 前端 | 根目录 `CODE_WIKI.md` |
> | 接口契约（逐条从 FastAPI 路由提取） | `docs/backend-api.md` |
> | 当前状态、质量机制、开发约定 | `docs/HANDOVER.md` |
> | 部署 | `docs/DEPLOY_UBUNTU.md` |
>
> 读这里的文档时，请把它当作「当时的决策记录」——理解某个机制为什么这样设计仍然有价值，
> 但**任何路径、组件名、状态机的描述都不要当作现状**。
