# 同考点兄弟命题唯一归属设计（重复题根治）

> 2026-10-10 ｜ 状态：已实现（三层）｜ 授权：用户确认可改封存 `generation_graph.py`，改后全量套件重跑

## 1. 事故与根因

线上试卷（course `eda43b14-2117-467a-812a-6be95f325da0`，paper `b1a0ebf0dd06401a` v2）
**单选第 3 题与判断第 1 题换措辞考查同一命题**——"最终回复在消息列表最后一项
content"——同考点 `14cb5ccbd753432d86b9fb6a4d06516a`（6.6），两题重复计分。

根因：答案域互斥判据 `boundaries_overlap` 只认**归一化整串相等或互含**。事故两边界：

- 单选：`Agent调用invoke的返回值为包含完整消息历史的列表，最终回复需通过['messages'][-1].content获取；…`
- 判断：`Agent invoke返回完整消息历史，最终回复为消息列表最后一项的content字段；…`

互不包含 → 合同硬过滤放行；两卡同考点 → 进同一合同；生成互避面（forbidden
answer_cores）也只拦**整段照抄**，换措辞拼接穿过 `_check_question` → 重复题成卷。

**确定性算法保证全局约束，模型只负责写题**——本修复不碰 prompt，判据与修剪全部确定性。

## 2. 判据与标定（真实 42 题合同，4 轮实验）

`boundaries_share_proposition(left, right)`（`domain/generation/contract.py`）：
答案域按句读（。；;，,、！!？?）分句（`coverage.py split_boundary_clauses`）、
只留中文字符（`cjk_compact`——英文术语 content/LangChain 是共享词汇不是共享命题）、
任一分句对共享连续子串 LCS（`coverage.py longest_common_run`）**≥6** 即判同一命题。

阈值 6 的标定依据（真实卷实测）：

| 方案 | 结果 | 结论 |
|---|---|---|
| 整串互含 | 漏报事故对 | 即根因 |
| 分句 LCS ≥5 | 13 对 | "结构化输出"类领域术语误报 |
| **分句 LCS ≥6** | **恰 3 对同考点，零跨考点误报** | **采用** |
| 全归一化 LCR ≥8 | 62 对 | 英文术语污染，弃用 |
| 答案级 ratio 比对 | 被 `python-dotenv`/`AgentState` 等 ratio 1.0 污染 | 弃用 |

3 对中的误报对 (4,12)/(10,31) 只共享引导性分句（"预置实现包含"/"…初始化均需配置
对应参数"），由修剪判据只剔该分句、命题本体保留，对阈值 6 安全。

## 3. 修剪判据（`contract.py trim_sibling_proposition_overlap`）

同考点兄弟对命中判据后，**卷面靠前题位（小 item_index）保留完整答案域**，后位题
逐分句让出；同时满足三条才剔：

1. 该分句中文紧凑长度 ≥4；
2. 与前位题任一分句 LCS ≥4；
3. **LCS / 该分句长度 ≥ 25%**（覆盖比守住——"类中间件"这类只占长分句一小截的领域
   术语共享不剔，事故对命题本体 "完整消息历史"(6/8) 与 "最终回复"(4/16=25%) 恰被剔）。

边界情况全部显式：

- 剔后仍重叠、或剔空答案域 → **不动边界**，报 `ContractConflict(code="boundary_overlap")`
  交教师修订，绝不静默降级；
- 被剔分句仍经**前位题的 `forbidden_context.answer_cores`** 流入后位题禁用面——
  后位题"自己的答案域不再含该命题 + 明确禁用该命题"，生成端指令一致；
- 剔除记录 `BoundaryTrim(item_index, kept_with_item_index, exam_point_id, removed_clauses)`
  落 `ContractAuditSummary.boundary_trims`（前端 `api.ts` 镜像），修订时按题位合并
  （本题位新记录覆盖旧记录）。

接线点（两处）：

- `contract_service.allocate_paper_contract`：槽位建成后、forbidden 组装前；
- `contract_service.apply_slot_revisions`：教师换单原子可能重新引入兄弟命题重叠，
  先修剪、剔不净即 `ContractRevisionError` 拒绝修订。

## 4. 三层落点（防线纵深）

| 层 | 位置 | 机制 |
|---|---|---|
| 合同层（构造性） | `contract_service` 两接线点 | 兄弟重叠确定性让出 + 显式冲突 + 审计留痕 |
| 生成终检层 | `generation_graph._check_question`（封存文件，授权改动） | forbidden 答案核心**分句**归一化后出现在题面（题干/选项/答案/解析）即 blocker 触发重试；整段判据保留 |
| 整卷审计层 | `generation_service.audit_paper_against_contract` | `answer_mutex` = `boundaries_overlap or boundaries_share_proposition`，成卷后显式报教师 |

残余风险（如实声明）：模型**换措辞**抄命题仍可能穿过字面分句校验——合同层修剪已让
后位题答案域不含该命题（无从照抄）、forbidden 明令禁用；极端不成网情况由教师终审
兜底。这是确定性字面判据的边界，不假装语义级检测。

## 5. 测试（+12）与门禁

- **domain**（`test_contract_models.py`）：事故对判据命中（含旧判据漏报对照）、无关
  边界零误报、引导语对命中、事故对修剪（背景+命题本体让出、非共享保留）、引导语只
  剔不伤命题、剔空报冲突、跨考点不动；
- **service**（`test_contract_service.py`）：分配修剪全链路（兄弟不再共享、留痕、无
  冲突、后位题 forbidden 含前位完整边界）；
- **修订**（`test_contract_revision.py`）：新引入重叠修剪成功留痕；剔空拒绝修订；
- **workflow**（`test_generation_graph.py`）：整段未抄、只抄单分句的泄漏触发重试；
- **audit**（`test_generation_audit.py`）：换措辞对 `answer_mutex` 判失败、无关对通过。

门禁：`uv run pytest -q` + 覆盖率 ≥80（封存 graph 改动后全量重跑）；前端
`npm run build` / `npm run lint`（`api.ts` 类型镜像改动）。
