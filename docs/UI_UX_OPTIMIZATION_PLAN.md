# 前端 UI/UX 优化方案

> 方法：先审计诊断、后小步修复（不重写）。每个工作流独立提交，门禁为
> `npm run build`（tsc）+ `npm run lint`（oxlint）；前端无单测，验收辅以 7 页视觉走查。
> 证据均为本次审计实读的 `文件:行号`，实施前请复核行号可能漂移。
>
> **全局纪律（每个工作流都适用）：旧代码删干净，不留冲突并存。**
> 被替代的旧定义（样式规则、keyframes、类名、工具函数）必须在同一提交内删除，
> 死文件/无调用方的死组件直接删除；不允许「新旧两套并存靠注释说明」。
> 删除前用 grep 确认无其他调用方，删除后 `npm run build` + `npm run lint` 门禁不变。

## 0. 问题清单总览（按用户反馈归类）

| # | 用户反馈 | 审计结论 | 工作流 | 优先级 |
|---|---|---|---|---|
| 1 | AI 对话占页面空间 | 4 个 AI 面板均为页内嵌入式（内嵌卡片/表格下方），打开即推挤布局 | A：AI 悬浮层 | P1 |
| 2 | 数值显示很多位 | `weight_value`/`exam_weight`/`score`/`confidence` 等 Float 裸渲染，无前端格式化工具 | B：可读性 | P0 |
| 3 | 显示裸 ID/编码 | 试卷「考点：0433…」UUID 兜底、原始枚举 Badge、原始校验错误码 | B：可读性 | P0 |
| 4 | 不合理动画 | 静态卡全局 hover 上浮、表格行 scale、多处无限循环动画 | C：动画治理 | P0 |
| 5 | 加载动画不一致 | Skeleton / Spinner / 纯文字 三套并存，`SpinnerOverlay` 死代码 | D：加载态统一 | P1 |
| 6 | 卡片套卡片 | `glass-card` 内再嵌 `glass-card`（全项目 35 处，几乎全 inline style） | E：卡片扁平化 | P2 |
| 7 | 内容过密难查看 | knowledge 2003 行 / PaperPanel 1245 行 / PipelinePanel 969 行；信息全量平铺 | F：密度治理 | P2 |
| — | （顺带）卫生 | `App.css`、`index.css` 无 import；`.card-hover`、`.table-wrapper` 被使用但**无 CSS 定义** | 并入 C | P0 |

---

## 工作流 A：AI 对话悬浮化（用户重点）

### 现状

| 面板 | 嵌入位置 | 打开时的布局影响 |
|---|---|---|
| `AiRevisePanel`（AI 改题） | `PaperPanel.tsx:1256` 右栏 QuestionDetail **下方兄弟节点** | 右栏变高，推挤双栏区（`maxHeight: calc(100vh - 140px)` 被压缩） |
| `PaperReviewPanel`（质量评审） | `PaperPanel.tsx:1168` PaperProfile 下方 | 整页下移，双栏区被顶下去 |
| `ContractExplainPanel`（合同解释） | `PipelinePanel.tsx:468` 合同表格 `glass-card` **内部** | 表格卡内膨胀，挤压合同表格 |
| `AiCreatePanel`（AI 生成） | `PaperPanel.tsx:1116` 新增题目 Modal 顶部 | 弹窗变高（本身是浮层，不占页面空间） |

四个面板共享同款「taskRunId 轮询 + TERMINAL 终态自停」逻辑，与 UI 布局解耦良好——
**这是悬浮化的有利条件：面板本体几乎不用改，改的是挂载位置与外壳。**

### 方案：通用 `FloatingPanel` 浮层外壳 + Portal 挂到 body

新增一个通用组件（不引入全局 store，保持「一切从简」）：

```
components/ui/FloatingPanel.tsx   # createPortal(document.body) + position: fixed
```

- **定位**：右下角 `right: 24px; bottom: 24px`；宽度 `min(560px, calc(100vw - 48px))`，
  最大高度 `min(72vh, …)`，内部滚动。窄屏（<640px）退化为底部全宽抽屉。
- **外壳**：玻璃表面（复用 `glass-card` 视觉），头部 = 图标 + 标题 +「—」最小化 +「×」关闭。
- **最小化 → 胶囊**：收起后右下角只剩一枚悬浮胶囊按钮（如「✦ AI 改题」），
  任务轮询中时胶囊内显示小 Spinner，点击还原。**面板保持挂载（仅 CSS 隐藏），
  轮询不中断**——最小化期间任务照常跑完。
- **层级**：design-tokens 现有 `--z-modal: 300 / --z-toast: 400`，新增
  `--z-floating: 350`（盖过 Modal、低于 Toast）。窄屏底部抽屉时同样适用。
- **交互**：Esc 关闭；`aria-modal` + 焦点圈定（头部关闭钮始终可聚焦）。

挂载改造（4 处 call site 包一层，面板组件本体不动）：

1. `PaperPanel.tsx:1256` — `<FloatingPanel title="AI 改题" …><AiRevisePanel/></FloatingPanel>`，
   右栏恢复为纯 QuestionDetail，双栏高度不再被推挤。
2. `PaperPanel.tsx:1168` — PaperReviewPanel 同上包浮层，整页不再下移。
3. `PipelinePanel.tsx:468` — ContractExplainPanel 从合同表格卡内提出，表格卡不再膨胀。
4. `AiCreatePanel`：**建议仍留在新增题目 Modal 内**（Modal 本就是浮层、不占页面空间，
   且其 `onFill` 回填表单的流程与弹窗强耦合，浮出去会造成双层浮层叠置）。
   但改为**默认收起的「✨ AI 生成」折叠条**，点击展开——同时解决弹窗过高的密度问题。
   （备选：也可一并浮出，代价是与 Modal 的叠层交互需额外处理。）

面板自身仅需两处小改：根元素 `className="glass-card"`（`AiRevisePanel:150` 等）
在浮层内由外壳接管表面样式，去掉内层卡底以免双层玻璃叠底。

### 涉及文件

- 新增 `frontend/src/components/ui/FloatingPanel.tsx`
- 改 `frontend/src/styles/design-tokens.css`（`--z-floating`）
- 改 `frontend/src/styles/global.css`（浮层/胶囊样式、窄屏抽屉、reduced-motion）
- 改 `frontend/src/pages/paper/PaperPanel.tsx`（3 处挂载点 + AiCreate 折叠）
- 改 `frontend/src/pages/paper/PipelinePanel.tsx`（1 处挂载点）
- 微改 4 个面板组件（去内层卡底）

### 验收

- 打开 AI 改题/合同解释/质量评审：页面布局零位移；浮层可最小化为胶囊且轮询继续；
  切题/切页签不残留浮层（随 call site 卸载）；Esc 可关。

---

## 工作流 B：可读性——数值格式化 + 裸 ID/枚举（P0）

### B1. 数值格式化：新增 `frontend/src/lib/format.ts`

后端导出侧已有 `_trim_number`（`paper_version_service.py:1205`：整数去小数、
浮点 `%g` 六位内），**前端没有对应物**，Float 字段直接裸渲染：

| 位置 | 现状 | 症状 |
|---|---|---|
| `knowledge/index.tsx:1473` | `{point.weight_value}%` | `23.3333%`（extractor `round(…,4)`） |
| `knowledge/index.tsx:1673` | `(p.weight_value ?? 0) + '%'` | 图 tooltip 同上 |
| `framework/index.tsx:650` | `{anchor.exam_weight}%` | `33.333333333333336%` |
| `paper/PipelinePanel.tsx:270,487`、`paper/PaperPanel.tsx:519,584` | `{item.score}分` | `plan_items.score` 是 Float（schema.py:529） |
| `knowledge/index.tsx:979` | `置信${s.confidence}` | 裸值；可能为 null |
| `knowledge/index.tsx:1251` | `置信度 {suppPreview.confidence}` | 裸值；null 时渲染成「置信度 」 |
| `framework/ExamRulesCard.tsx:113,126` | `{r.ratio}%`、`{c.weight}%` | 输入回显裸浮点 |

工具函数（单一出口，禁止页面内各自 `toFixed`）：

```ts
formatNumber(v)        // 整数去小数，浮点保留 ≤2 位（镜像后端 _trim_number）
formatPercent(v)       // 数值 → 最多 1 位小数 + '%'
formatScore(v)         // 分值：整数去 .0，半分制保留 1 位
formatConfidence(v)    // 置信度：量纲防御（0<v≤1 视为 0-1 换算 ×100），四舍五入取整 + '%'；null → '—'
```

量纲防御的理由：证据表 confidence 为 Integer 0–100（`schema.py:351,374` 约束），
但 LLM 原始 payload 通道可能给 0–1 小数；`knowledge/index.tsx:796` 的
`Math.round(ev.confidence || 0)}%` 遇 0.85 会渲染成「1%」——用 `formatConfidence`
统一换算，显示层归一（顺带修掉该潜在错显）。

### B2. 裸 ID：永不把 UUID/原始 key 渲给教师

**核心案例（用户原诉）**：`PaperPanel.tsx:676-678`
`考点：{examPointName || item.exam_point_id}` —— 映射未命中即渲染 UUID。

根因链（`hooks/useNameMaps.ts:14-35`）：
1. `buildNameMaps` 的 `examPoints` **只**从已发布知识目录构建，framework 只取了 `anchors`
   （framework payload 里其实也有 `exam_points`，见 `framework_service.py:166`）——
   知识目录未发布时映射必空；
2. `reload` 用 `Promise.allSettled` 静默失败，接口 404/网络错误 → 全量空映射；
3. 纸卷是历史快照：目录重建后旧 `exam_point_id` 在新目录中不存在，查表必失败。

修复分三层：

- **前端兜底（立即，展示层职责）**：统一「名称 → 业务 code → 友好占位」降级链，
  **UUID 永不出现在可见文本**：
  - `PaperPanel.tsx:676-678`、`PipelinePanel.tsx:70-80`
    （`examPointLabel/anchorLabel/cardLabel` 的 `|| id`）改为：
    `maps.examPoints[id] ?? nameMaps.codeOf(id) ?? '未匹配考点（已归档）'`，
    UUID 只进 `title` 悬浮提示；
  - `useNameMaps` 增加数据源：framework payload 的 `exam_points`（补上「未发布目录」缺口）；
    加载失败时 console.warn 一次，不再纯静默。
- **数据源（根治，后端增量）**：生成写卷时把考点名快照进试卷——
  `generation_runner_service` 写 `PaperVersion.questions` 时随 `exam_point_id`
  追加 `exam_point_title` / `exam_point_code`（JSON 载荷加键，不动表结构、只增不改，
  符合「冻结即不可变」）。前端优先读快照字段，历史卷再走映射。
  （此项需后端配合，可排 P1；在那之前第一层兜底已保证不再露 UUID。）
- **同类一并修**：
  - `dashboard/index.tsx:322` `<Badge>{project.status}</Badge>` → 用已有的
    `EXAM_PROJECT_STATUS_META`（`lib/examDisplay.ts:59`）；
  - `AiCreatePanel.tsx:144-145` `{p.question_type}` / `{p.difficulty}` →
    `QUESTION_TYPE_LABELS` / `DIFFICULTY_LABELS`；
  - `AiRevisePanel.tsx:180`、`AiCreatePanel.tsx:122` `未通过校验 · {validation.code}` →
    错误码→中文话术映射表，原始码放 `title`；
  - `ExamRulesCard.tsx:126` `c.anchor_key` 兜底、`framework/index.tsx:545,560`
    `c.message || c.key` 兜底 → 同样走「友好占位 + title 真值」。

### 涉及文件

- 新增 `frontend/src/lib/format.ts`
- 改 `hooks/useNameMaps.ts`、`pages/paper/PaperPanel.tsx`、`pages/paper/PipelinePanel.tsx`、
  `pages/dashboard/index.tsx`、`pages/paper/AiCreatePanel.tsx`、
  `pages/paper/AiRevisePanel.tsx`、`pages/framework/ExamRulesCard.tsx`、
  `pages/framework/index.tsx`、`pages/knowledge/index.tsx`（数值点）
- （P1，后端）`services/generation_runner_service.py`（考点名快照）+ `types/api.ts` 类型

### 验收

- 试卷页/流水线任何位置不再出现 32 位 UUID；status/difficulty/question_type 全中文；
  所有 `%` 与分值 ≤1 位小数；置信度恒为整数百分比。

---

## 工作流 C：动画治理（P0）

### 移除/收紧清单（`global.css` + 页面内联）

| 动画 | 位置 | 处置 |
|---|---|---|
| `.glass-card:hover` 全局上浮 2px | global.css | **从 `.glass-card` 移除**——静态内容卡被迫「可点击」是不合理的暗示 |
| `.card-hover`（被 `course-space:138`、`dashboard:149,196,245,291` 使用但**无 CSS 定义**） | — | **补定义**：hover 上浮 + 阴影只作用于真正可点的卡（dashboard 项目卡、课程卡），接替上面移除的效果 |
| `.table-wrapper`（`PipelinePanel:262,478` 使用但**无定义**） | — | 补定义：横向滚动容器 `overflow-x:auto` + 滚动条样式 |
| `.data-table tbody tr:hover { scale(1.002) }` | global.css | 移除 scale，保留背景高亮（表格行非可点对象） |
| `stagger-item` 交错入场 | global.css + dashboard | 收敛为仅首屏 4 张统计卡、时长 ≤400ms；列表行不再交错 |
| `icon-float` 无限浮动 | global.css | 移除无限循环；改为入场一次的轻位移 |
| knowledge 内联 `gbFloat/tw1/tw2/tw3` 循环 | `knowledge/index.tsx:1769` 附近 | 移除装饰性循环（图谱连线脉动）；保留有信息含义的高亮 |
| LoginPage `orbFloat 12s` 无限背景球 | `LoginPage.tsx:158,169` | 改静态渐变（或单次 20s 极慢且透明度 ≤0.5 的漂移） |
| 未知/新增动画 | — | 一律遵守 `prefers-reduced-motion: reduce` 全局短路（新增一条 media query） |

**保留**：spinner、skeleton 微光、progress 不确定态——它们是功能性反馈，不是装饰。

### 涉及文件

`global.css`、`LoginPage.tsx`、`knowledge/index.tsx`、`dashboard/index.tsx`（stagger 收敛）。

### 验收

- 页面静止时无任何无限循环动画（登录页除外的例外需注明理由）；
  hover 上浮只出现在真正可点的卡上；`.card-hover`/`.table-wrapper` 首次有真实样式。

---

## 工作流 D：加载态统一（P1）

### 现状（三套并存 + 一个死组件）

- 骨架屏 `SkeletonCardGrid`：dashboard、paper 列表
- `Spinner size="lg"` + 文案：materials、framework、knowledge
- 纯文字「正在加载试卷…」：`paper/index.tsx:352`
- `SpinnerOverlay`：**全项目无调用方（死代码）**

### 约定（选定「骨架屏为默认」）

1. **首屏/页面级数据** → 骨架屏。`Skeleton.tsx` 扩三个形状变体：
   `SkeletonList`（行卡）、`SkeletonForm`（详情/编辑器）、`SkeletonTable`（表格），
   形状贴合目标页真实布局，避免「闪一下就换布局」。
2. **动作级（提交、刷新、切页签）** → 按钮内 `Spinner size="sm"`（已有，`loading` prop）。
3. **长任务（生成/解析）** → `ProgressPanel`（已有，进度/排队/失败态齐全，是四者中最完善的——作为长任务唯一入口）。
4. 迁移：materials/framework/knowledge 的页级 Spinner → 对应 Skeleton 变体；
   `paper/index.tsx:352` 纯文字 → `SkeletonList`；
   `SpinnerOverlay` 无调用方 → **删除**（若后续浮层需要遮罩，走 `.modal-overlay` 复用）。

### 涉及文件

`components/ui/Skeleton.tsx`（变体）、`components/ui/Spinner.tsx`（不动）、
删除 `SpinnerOverlay`、`pages/materials|framework|knowledge|paper/index.tsx` 装载分支。

### 验收

- 7 页首屏加载观感一致（同形状骨架 + 同节奏微光）；仓库内不存在第四种加载表达。

---

## 工作流 E：卡片扁平化（P2）

### 规则

1. **一层玻璃**：页面主区块用 `glass-card`；其内部**禁止**再嵌 `glass-card`。
   内部分组改用 `.sub-section`（`rgba(0,0,0,0.02)` 底 + 1px 分隔线 + 10px 圆角，
   无阴影无独立 hover）——「视觉降级一档」表达从属关系。
2. **inline style 停止扩张**：本次先为高频结构（卡片头、分区、行/列堆叠）抽
   `.card-head` / `.sub-section` / `.row` / `.stack` 四个工具类进 `global.css`；
   35 处 inline style **不一次性改写**（改动面过大），仅新代码与被触碰的代码顺手迁移。
3. 嵌套实例点名（审计实锤）：
   - `materials/index.tsx` 文件夹按钮卡嵌在资料卡内
   - `framework/index.tsx` 分组卡嵌套
   - `paper/` 详情头卡 + 流水线卡叠床架屋
   - `knowledge/index.tsx` 工具栏卡嵌页面卡
   - `dashboard/index.tsx` 统计 `Card className="card-hover"` 外再包卡

### 涉及文件

`global.css`（新工具类）+ 上述 5 个页面的嵌套点位（每点位一处替换）。

### 验收

- `grep -rn 'glass-card' | grep -c` 嵌套：任一 `glass-card` 子树内不再出现第二个
  `glass-card`；视觉走查确认分区仍清晰（分隔线 + 浅底接管）。

---

## 工作流 F：信息密度治理（P2）

### F1. 大文件拆分（纯机械提取，不改行为）

| 文件 | 行数 | 拆法 |
|---|---|---|
| `knowledge/index.tsx` | 2003 | 提取 `knowledge/` 子目录：`TreePanel`（1330-1520 树）、`GraphView`（1660+ 图谱）、`CandidatePanel`（900-1300 候选/补证据）、`EvidenceList`、`SupplementDialog`；页面只留编排与状态 |
| `paper/PaperPanel.tsx` | 1245 | 提取 `QuestionIndex`（已内联定义）、`QuestionDetail`、`PaperProfile`、`QuestionEditor` 到 `paper/` 子组件 |
| `paper/PipelinePanel.tsx` | 969 | 蓝图/合同/生成三阶段已是清晰三段，按 `StageBlueprint` / `StageContract` / `StageGeneration` 拆文件 |

拆分门禁仍是 `npm run build`；一个文件一次提交，便于回滚。

### F2. 渐进披露（让页面「可读」）

- **双栏阅读器**（`PaperPanel`）：题目元数据区（考点/难度/认知/审核原因）默认一行
  摘要徽标，`<details>` 式展开看详情；`needs_review_reason` 长文本 clamp 2 行。
- **knowledge 树**：单元/卡片默认收起（已有展开机制，保证默认全收起）；
  候选 payload 的 `code` 副行只在悬浮时显示。
- **framework 考核规则**：`ExamRulesCard` 只读态与编辑态分离（现在混排），
  编辑才展开输入控件。
- **sticky 操作条**：`framework` 顶部操作、`paper` 工具栏统一 sticky，
  长列表滚动时操作不丢失（注意 `--z-sticky: 200` 已有 token）。

### F3. 布局卫生（顺带）

- `Layout.tsx` 主区 `padding-left: 260px` 硬编码 → 改 `var(--sidebar-width)` token，
  `maxWidth: 1400px` 提为 token；
- 删除死文件 `App.css`、`index.css`（无任何 import）。

---

## 实施顺序与里程碑

| 批次 | 工作流 | 预估 | 风险 |
|---|---|---|---|
| 第 1 批 | B（可读性）+ C（动画/死类） | 1 天 | 低（纯展示层） |
| 第 2 批 | A（AI 悬浮层）+ D（加载态） | 2–3 天 | 中（布局交互变更，需走查） |
| 第 3 批 | E（扁平化）+ F（密度/拆分） | 3–5 天 | 中（面广但机械） |
| 贯穿 | 每批完成即 `npm run build` + `npm run lint` + 7 页视觉走查，通过再进下一批 | | |

### 全局门禁

```bash
cd frontend
npm run build   # tsc 类型检查 + 产物构建
npm run lint    # oxlint
```

### 待用户拍板的决策点

1. **AiCreatePanel**：推荐留在 Modal 内改折叠条（叠层简单）；是否也一并悬浮？
2. **考点名快照**（B2 根治项）涉及后端 `generation_runner_service` 改动，本轮前端方案先行、后端快照是否同期做？
3. **骨架屏 vs Spinner**：方案默认「首屏骨架、动作 Spinner」，若团队更偏好 Spinner 全站统一只需把 D1 换向，成本相当。
