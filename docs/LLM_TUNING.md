# 模型调优与换模型运行手册

> 状态：生效中（2026-09-25 落地，StepFun 官方文档核对）。
> 目标：**换模型、调模型不动业务代码**——调优配置集中在独立文件，经 `.env` 插入。
> 权威约束见 `AGENTS.md` 核心纪律第 1 条（模型只负责写题）与第 7.2 节（外部系统只走 `adapters/`）。

---

## 0. 一条不可动摇的原则

**调优只改协议参数与预算，不改业务语义。**

- 题型比例、难度、去重 —— 由确定性算法在蓝图 / 合同阶段构造性保证，
  ⚠️ **永远不进 prompt**，换任何模型都不允许把它们挪给模型"自觉遵守"。
- 调优的合法落点只有四层：`.env` → `config.py`（键）→ `model_profiles.py`（型号档案）→
  `llm_gateway.py`（参数下发）。业务提取器只定义**业务 schema 与 prompt**，不感知供应商参数。
- ⚠️ 封存的三个 graph（framework / organization / generation）与 `schema.py`/`init_db`
  不因调优改动。

---

## 1. 分层架构（调优配置放哪）

```
.env                        ← 开关与预算（换模型/回退在这里操作）
  ↓
backend/app/config.py       ← Settings 键（默认值 + 注释写明依据）
  ↓
backend/app/adapters/model/model_profiles.py   ← ★型号调优档案（独立文件）
  ↓ 按模型名匹配档案，插供应商参数
backend/app/adapters/model/llm_gateway.py      ← 网关组装请求（统一出口）
  ↓
llm_semantic_extractors.py / generation 调用点  ← 只带业务 schema、prompt、effort 意图
```

### 1.1 型号档案 `model_profiles.py`

```python
@dataclass(frozen=True)
class ModelProfile:
    model_id: str
    thinking_style: str          # reasoning_effort（StepFun）| thinking_disabled（旧式 OpenAI 兼容）
    supports_tool_choice: bool   # StepFun 未文档化，下发会 400 → False
    supported_efforts: tuple[str, ...]   # 该型号接受的 reasoning_effort 档位（空=不支持）
    default_effort: str | None   # 档位非法时的收敛目标
    supports_json_schema: bool   # 是否可下发 response_format=json_schema strict
```

- **匹配顺序**（`resolve_model_profile`）：型号档案优先 → 未收录但 `base_url` 含
  `stepfun` 按端点特性回退 → 通用 OpenAI 兼容档（只关思考，不发任何供应商特性参数）。
  **不加档案也不会坏**，只是享受不到该供应商的特性参数。
- **档位收敛**（`normalize_effort`）：调用方档位不在型号支持集内时自动收敛到
  `default_effort`（如 `step-3.5-flash-2603` 只收 low/high，传 medium 会 400 → 收敛到 low）。
- 已收档案型号：`step-5-preview` / `step-3.7-flash` / `step-3.5-flash` / `step-3.5-flash-2603`。

### 1.2 StepFun 官方文档要点（2026-09-25 核对，文档站 `platform.stepfun.ai/docs`）

| 参数 | 结论 |
|---|---|
| `reasoning_effort` | 三档 low/medium/high；**low = 信息抽取/摘要类**（本项目抽取/分类/归并依此选定）、medium = 通用推理（官方默认）、high = 复杂数学/规划/代码 |
| 关闭思考 | 旧式 OpenAI 兼容端点用 `thinking={"type":"disabled"}` |
| `response_format` | `json_object` 保证可解析；`json_schema` + `strict:true` 按 schema 约束解码，**必填字段在场由协议保证**（实测生产 `step_plan` 端点 200 且输出结构合规） |
| `tool_choice` | 端点未文档化，下发 400 → stepfun 档案 `supports_tool_choice=False` |
| 计量怪癖 | `reasoning` 字段有内容但 `reasoning_tokens: 0` 是供应商计量行为，非异常 |

---

## 2. 关键旋钮速查（改哪里 → 什么效果）

### 2.1 `.env`（运行时开关，重启 API + Worker 生效）

| 变量 | 默认 | 作用 |
|---|---|---|
| `LLM_MODEL` | `step-3.7-flash` | **换模型主入口**：改这里即可，网关按名匹配档案 |
| `LLM_CLASSIFY_MODEL` / `LLM_CONSOLIDATE_MODEL` / `LLM_EXTRACT_MODEL` | 空=回退 `LLM_MODEL` | 知识目录三阶段分档选模（分类可降本，归并/抽取建议强模型） |
| `LLM_DISABLE_THINKING` | `true` | 抽取/大纲阶段关闭长链路推理（省 token、防截断） |
| `LLM_GENERATION_DISABLE_THINKING` | `true` | 出题阶段关闭思考 |
| `ORGANIZATION_EXTRACTION_JSON_SCHEMA` | `true` | 抽取响应 `json_schema` strict 结构约束；**`false` 一键回退** json_object 老行为 |
| `ORGANIZATION_EXTRACTION_MAX_TOKENS` | `6144` | 单次抽取输出预算（历史根因与校准史见 §3 表） |
| `ORGANIZATION_EXTRACTION_REASONING_EFFORT` | `low` | 抽取思考档（StepFun 信息抽取档） |
| `ORGANIZATION_EXTRACTION_BATCH_SIZE` | `3` | 抽取批大小（输入 token 权衡） |

### 2.2 代码常量（改需带测试）

| 位置 | 常量 | 值与依据 |
|---|---|---|
| `llm_semantic_extractors.py` | `_CONSOLIDATION_MAX_TOKENS` | `8192` = 归并失败上限 4096 × 2；分类不设上限时实测均值 7833 / 最大 11784 全成功 |
| `llm_gateway.py` | `large_prompt_max_attempts=2` | 超 60k 字符的大 prompt 失败重试收紧到 2 次（重发 = 等额再烧一遍输入） |
| `config.py` | `organization_retrieval_min_score=0.30` | 陈述语料下旧值 0.40 等效语义门槛 0.62 超可达中位数 0.60，26 考点零召回 |

### 2.3 响应缓存（换模型自动失效）

`temperature=0` 的确定性调用按 `(model, prompt_hash)` 复用历史成功响应
（`services/model_call_service.py` 的 `DatabaseModelCallRecorder.lookup_response`，命中时
`details.cache_hit=True`；出题类 `temperature>0` 不缓存）。**换模型后 `model` 变了，缓存自然失效**；
改 prompt 同理触发重跑（费钱费时，改前想清楚——见 `docs/HANDOVER.md` §8.4）。

---

## 3. 故障速查（`model_calls.details` 是第一现场）

失败详情落在 `model_calls.details`（`validation` / `raw_response_snapshot`），本地 `.env`
的 `DATABASE_URL` 可直指云端库查询。

| 症状 / error_code | 根因 | 处置 |
|---|---|---|
| `model_empty_response`，`output_tokens` **恰好顶格** max_tokens | 思考与正文共享输出预算，思考烧光额度 content 为空 | 加大 `*_MAX_TOKENS`（抽取 3072→6144、归并 4096→8192 均为此根因）；或关思考/降 effort |
| `model_schema_validation_failed`，content 缺必填字段（如回 `{}`） | 模型"忘了"必填字段，纯 prompt 约束不可靠 | `ORGANIZATION_EXTRACTION_JSON_SCHEMA=true` 走协议层约束；型号不支持则档案关 `supports_json_schema` |
| 400 参数不识别 | 下发了该型号不支持的供应商参数（effort 档位 / tool_choice / json_schema） | 档案里登记该型号的 `supported_efforts` 与能力开关，让 `normalize_effort` 收敛 |
| `reasoning` 有内容但 `reasoning_tokens: 0` | StepFun 计量怪癖 | 非异常，不处理 |
| `LLMGatewayError` 网络类 | 传输失败 | 网关自带重试（4 次指数退避，大 prompt 2 次）；`Fact top-up failed` 非致命，首轮结果继续用 |

**协议探针**：验证供应商是否接受某参数（如 `json_schema+strict`），用 httpx 直接 POST
`{base_url}/chat/completions` 带最小消息试打——改码前先探针，可避免在业务链路上试错。

---

## 4. 换模型标准流程

1. **已收录型号**：只改 `.env` 的 `LLM_MODEL`（或分阶段 `LLM_EXTRACT_MODEL` 等），
   业务代码零改动。
2. **未收录型号**：先按 §3 探针实测其参数接受度（effort 档位 / json_schema / tool_choice），
   在 `MODEL_PROFILES` 加一条档案；不加也不会坏（回退通用档），但可能错过降本档位。
3. 跑档案测试：`cd backend && uv run pytest tests/unit/test_model_profiles.py -q`。
4. **重启 API + Worker**（`LLMJsonClient` 构造读 settings）。
5. 验证：跑一次知识目录构建，查 `model_calls` 的 `stage` / 耗时 / `error_code`，
   确认新模型生效且无 §3 表中的失败模式。

**回退演练**：结构约束出问题时 `.env` 加 `ORGANIZATION_EXTRACTION_JSON_SCHEMA=false`
即回老行为，无需回滚代码。

---

## 5. 相关测试与文档

- 档案与网关参数下发：`backend/tests/unit/test_model_profiles.py`（11 测试）
- 抽取端到端（schema_constrained 开关）：`backend/tests/unit/test_llm_semantic_extractors.py`
- 配置键定义与注释（含预算校准依据）：`backend/app/config.py`
- 环境变量模板：`.env.example`
- 历史教训与机制清单：`docs/HANDOVER.md` §5
