"""AI 助手工具注册表：工具元数据的单一事实源。

每个工具一条 :class:`ToolSpec`，一处定义驱动四处使用：

1. 段1 function calling 的 ``action.tool`` 枚举——模型不可能"发明"工具名；
2. 系统提示词里的工具清单与参数口径（**渲染生成**，不再是手写散文，
   不会再出现"提示词说 A、校验器要求 B"的两套说法）；
3. 随消息下发的卡片元数据 ``label/impact/auto``——前端不再硬编码一份；
4. 里程碑标记 ``confirm``——只有它为 True 的工具才需要教师点「确认执行」，
   其余由助手自动执行后回报执（见 chat 侧自动执行队列）。

**本模块不含执行契约**：写操作仍只出提案卡，执行由前端确认/自动执行后调既有
业务 API；这里只描述"有哪些工具、何时用、参数长什么样、要不要教师确认"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.blueprint.models import ASSESSMENT_MODES
from app.domain.generation.archetypes import ARCHETYPE_CONTRACTS
from app.domain.generation.question_formats import QUESTION_TEMPLATES

# 工具类别：只读查询 / 写操作提案 / 资料内容问答
KIND_READ = "read"
KIND_PROPOSAL = "proposal"
KIND_RAG = "rag"

# 综合题原型的可选值（顺序即档案顺序，与 ARCHETYPE_CONTRACTS 同键）
_ARCHETYPE_ENUM = list(ARCHETYPE_CONTRACTS)
# 题型键（不含裸 comprehensive；综合题传原型 key）
_QUESTION_TYPE_ENUM = list(QUESTION_TEMPLATES)


@dataclass(frozen=True)
class ToolSpec:
    """一个工具：给模型看的说明与参数形状 + 给前端看的卡片元数据。"""

    name: str
    kind: str
    # 何时用 / 前提 / 注意事项（渲染进提示词）
    doc: str
    # JSON Schema 子集：properties 的 type/description/enum/items + required
    parameters: dict = field(default_factory=dict)
    # 卡片标题与影响说明（proposal；随 action.payload 下发）
    label: str = ""
    impact: str = ""
    # True = 教师点「确认执行」才执行；False = 助手自动执行
    confirm: bool = False


def _obj(properties: dict | None = None, required: list[str] | None = None) -> dict:
    schema: dict = {"type": "object", "properties": properties or {}}
    if required:
        schema["required"] = list(required)
    return schema


def _arr(item: dict, description: str | None = None) -> dict:
    prop: dict = {"type": "array", "items": item}
    if description:
        prop["description"] = description
    return prop


def _str(description: str = "", *, enum: list | None = None) -> dict:
    prop: dict = {"type": "string"}
    if enum:
        prop["enum"] = list(enum)
    if description:
        prop["description"] = description
    return prop


def _num(description: str = "") -> dict:
    prop: dict = {"type": "number"}
    if description:
        prop["description"] = description
    return prop


_PROJECT_ID = _str("试卷项目 id，取自 payload.ids.project_ids；仅一个项目时可省略")

# ---------------------------------------------------------------------------
# 工具清单（顺序即提示词中的呈现顺序）
# ---------------------------------------------------------------------------

TOOL_SPECS: tuple[ToolSpec, ...] = (
    # ---------------- 只读查询 ----------------
    # 六七个「状态查询」原本各占一个工具，但返回的都是同一份课程快照的不同切片
    # （资料/框架/目录/蓝图/合同/试卷全部来自同一次装配），属真重复——合并成一个
    # 查询工具，用 section 指定要呈现的部分，结果卡按 section 选对应视图。
    ToolSpec(
        name="course_overview",
        kind=KIND_READ,
        doc=(
            "查询课程状态。section 指定要看的部分，缺省 overview = 全阶段概览。"
            "教师点名了某个试卷项目时传 project_id，只呈现该项目。"
        ),
        parameters=_obj(
            {
                "section": _str(
                    "要看的部分",
                    enum=[
                        "overview",
                        "materials",
                        "framework",
                        "blueprint",
                        "contract",
                        "paper",
                        "projects",
                    ],
                ),
                "project_id": _PROJECT_ID,
            }
        ),
    ),
    ToolSpec(
        name="usage_guide",
        kind=KIND_READ,
        doc=(
            "使用引导（网站能力地图 + 出卷全流程 + 当前进行到哪一步；引导卡自带步骤与页面跳转按钮）。"
            "教师问「这个网站能干什么/怎么用/怎么出一份卷子/下一步做什么」时必须使用。"
        ),
    ),
    # ---------------- 资料内容问答 ----------------
    ToolSpec(
        name="answer_material_content",
        kind=KIND_RAG,
        doc=(
            "基于已解析资料正文回答问题/做总结。仅对 snapshot.materials 中 parse_status==\"ready\" "
            "的资料使用；课程里没有已解析资料时不使用本工具，回复引导教师先到「资料库」解析。"
            "回答正文由系统按检索片段生成，你的 reply 只给一句引导（如「已检索到相关资料，回答如下：」），不要复述片段。"
        ),
        parameters=_obj(
            {
                "material_id": _str(
                    "教师点名某份资料时必须传，取自 payload.ids.material_ids；问全课程资料时不传",
                )
            }
        ),
    ),
    # ---------------- 写操作提案：课程级 ----------------
    # 自动化只覆盖出卷主线的中间产物（建项目/改规则/建蓝图/建议/生成/评审）。
    # 这三条不属于出卷流程，且分别是「新建实体」与「覆盖既有解析结果」，
    # 保持教师点确认——不把自动化扩到流程之外。
    ToolSpec(
        name="create_course",
        kind=KIND_PROPOSAL,
        doc="新建课程。",
        label="新建课程",
        impact="将在课程空间新增一门课程；不影响当前课程的数据。",
        confirm=True,
        parameters=_obj(
            {
                "name": _str("课程名称，1~200 字"),
                "slug": _str("小写字母、数字与连字符组成的别名"),
                "description": _str("课程简介"),
                "category": _str("类别 key，取自 payload.course_categories"),
            },
            ["name"],
        ),
    ),
    ToolSpec(
        name="update_course",
        kind=KIND_PROPOSAL,
        doc="修改当前课程（至少给一个字段）。",
        label="修改课程信息",
        impact="覆盖当前课程的名称 / 别名 / 简介字段。",
        confirm=True,
        parameters=_obj(
            {
                "name": _str("课程名称，1~200 字"),
                "slug": _str("小写字母、数字与连字符组成的别名"),
                "description": _str("课程简介"),
            }
        ),
    ),
    ToolSpec(
        name="start_parse",
        kind=KIND_PROPOSAL,
        doc="启动某资料解析。",
        label="发起资料解析",
        impact="对所选资料执行解析流程；已有解析结果将被覆盖。",
        confirm=True,
        parameters=_obj(
            {"material_id": _str("资料 id，取自 payload.ids.material_ids")},
            ["material_id"],
        ),
    ),
    # ---------------- 写操作提案：出卷主线 ----------------
    ToolSpec(
        name="create_exam_project",
        kind=KIND_PROPOSAL,
        doc="创建试卷项目。课程可有多个试卷项目，既有项目保持冻结不受影响。",
        label="创建试卷项目",
        impact="在当前课程新增一个试卷项目；不影响既有项目的数据。",
        parameters=_obj(
            {"name": _str("项目名称，从教师原话取，如「期末考试卷」")},
            ["name"],
        ),
    ),
    ToolSpec(
        name="update_exam_rules",
        kind=KIND_PROPOSAL,
        doc=(
            "修改考核规则（题型比例/章节权重/考试侧重点；蓝图创建时按新规则确定性折算，"
            "你只提方案、不做换算）。至少给一个字段；各字段已有现值见 "
            "snapshot.framework.exam_rules，未给出的字段保持原值。"
        ),
        label="修改考核规则",
        impact="覆盖题型比例 / 章节权重 / 考试侧重点；蓝图创建时按新规则确定性生成，已存在的蓝图不受影响。",
        parameters=_obj(
            {
                "question_type_ratios": _arr(
                    _obj(
                        {
                            "question_type": _str("题型，可用中文题型名"),
                            "ratio": _num("占比百分比，>0"),
                        },
                        ["question_type", "ratio"],
                    ),
                    "题型比例列表",
                ),
                "chapter_weights": _arr(
                    _obj(
                        {
                            "anchor_key": _str("章节锚点"),
                            "weight": _num("权重，>0"),
                        },
                        ["anchor_key", "weight"],
                    ),
                    "章节权重列表",
                ),
                "assessment_focus": _arr(
                    _obj(
                        {
                            "assessment_mode": _str(
                                "考查方式", enum=list(ASSESSMENT_MODES)
                            ),
                            "weight": _num("权重，>0"),
                        },
                        ["assessment_mode", "weight"],
                    ),
                    "考试侧重点列表；教师说「偏理论」即提高 theory_recall 与 conceptual 的权重",
                ),
            }
        ),
    ),
    ToolSpec(
        name="create_blueprint",
        kind=KIND_PROPOSAL,
        doc=(
            "创建草稿蓝图（按考核规则与知识目录确定性生成题位、难度分布与章节权重）。"
            "前提：命题框架已冻结且知识目录已发布，否则不要选它。"
        ),
        label="创建草稿蓝图",
        impact="按考核规则与知识目录确定性生成题位计划（含难度分布）；已有合同将随蓝图重建失效。",
        parameters=_obj(
            {
                "project_id": _PROJECT_ID,
                "comprehensive_archetypes": _arr(
                    _str("综合题原型", enum=_ARCHETYPE_ENUM),
                    "综合题原型**按序列表**：顺序即指派顺序，**可重复，重复即数量**——"
                    "教师要两道代码题就把 code_completion_scenario 写两次、只写一次=一道；"
                    "教师要求综合题不出代码题时排除 code_completion_scenario",
                ),
            }
        ),
    ),
    ToolSpec(
        name="confirm_blueprint",
        kind=KIND_PROPOSAL,
        doc="确认当前草稿蓝图（里程碑）。蓝图不存在或已确认时不要选它。",
        label="确认蓝图（里程碑）",
        impact="确认后蓝图题位计划冻结，作为合同分配依据；要调整需新建蓝图版本或先发起 AI 建议。",
        confirm=True,
        parameters=_obj({"project_id": _PROJECT_ID}),
    ),
    ToolSpec(
        name="enqueue_blueprint_suggest",
        kind=KIND_PROPOSAL,
        doc="发起蓝图调整建议（系统把要求确定性换算成目标分布）。",
        label="发起蓝图 AI 建议",
        impact="创建调整建议任务，随后由助手自动应用建议；应用后蓝图题位按新分布落库。",
        parameters=_obj(
            {
                "project_id": _str("试卷项目 id，取自 payload.ids.project_ids"),
                "instruction": _str(
                    "一句话要求——教师的难度比例要求（如「难度按5简单3中等2难」「5:3:2」，"
                    "以及「难度偏中等」这类倾向说法）原样放进 instruction；教师原话里表示粒度的"
                    "限定词（如「每个题型」「各题型」「按题型」）必须原样保留，解析器按它决定"
                    "逐题型还是整卷换算，丢词会改变换算口径。逐题改考法/题型/分值"
                    "（「第5题改成问题求解」「这题换成多选」）同样把教师原话放进 instruction",
                ),
            },
            ["project_id"],
        ),
    ),
    ToolSpec(
        name="confirm_contract",
        kind=KIND_PROPOSAL,
        doc="重新分配并确认合同（里程碑）。合同已确认冻结时不要选它。",
        label="确认合同（分配落库）",
        impact="由既有确定性分配算法落库并冻结合同；冻结后只能新建版本继续修改。",
        confirm=True,
        parameters=_obj({"project_id": _PROJECT_ID}),
    ),
    ToolSpec(
        name="start_generation",
        kind=KIND_PROPOSAL,
        doc=(
            "发起 AI 分批生成。合同未确认时不要选它；generation_task_status 为 queued/running "
            "= 已发起在跑，同样不要选它。"
        ),
        label="发起 AI 生成",
        impact="按已确认合同分批生成题目；生成期间可在试卷页查看进度。",
        parameters=_obj({"project_id": _PROJECT_ID}),
    ),
    ToolSpec(
        name="enqueue_paper_review",
        kind=KIND_PROPOSAL,
        doc=(
            "发起整卷 AI 质量评审（只读报告，不改任何数据；教师要「检查试卷」「看看有没有不好的地方」时用）。"
            "前提：该项目已有试卷；课程有多份试卷且教师没点名时先列出项目名问教师评哪一份；"
            "已发起过就引导教师到试卷页看报告，不要重复发起。"
        ),
        label="发起整卷 AI 评审",
        impact="创建只读质量评审任务（不修改任何数据）；报告生成后在试卷页查看。",
        parameters=_obj(
            {
                "project_id": _PROJECT_ID,
                "instruction": _str(
                    "教师关注点原话，如「重点看填空题答案是否唯一」；没有就省略=常规评审",
                ),
            }
        ),
    ),
    ToolSpec(
        name="update_question_type_format",
        kind=KIND_PROPOSAL,
        doc=(
            "设置/修改某题型（或综合题原型）的出题格式要求（影响之后的生成；已设置的格式见 "
            "snapshot.framework.exam_rules.type_formats）。综合题原型覆盖只替换该原型的任务卡文本，"
            "分问数量与输出 schema 仍按原型档案确定性生效。"
        ),
        label="修改题型格式",
        impact="覆盖该题型的出题格式要求，之后的生成按新格式出题；不影响题型比例/难度/去重。",
        parameters=_obj(
            {
                "question_type": _str(
                    "题型键，或综合题原型 key；也接受中文题型名（不接受裸 comprehensive）",
                    enum=_QUESTION_TYPE_ENUM + _ARCHETYPE_ENUM,
                ),
                "template": _str(
                    "该题型/原型**完整**的出题格式要求，1~2000 字，须含该题型的结构与答案唯一性约束；"
                    "传空串=恢复系统默认格式",
                ),
            },
            ["question_type", "template"],
        ),
    ),
)

TOOL_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}

READ_TOOLS: tuple[str, ...] = tuple(s.name for s in TOOL_SPECS if s.kind == KIND_READ)
PROPOSAL_TOOLS: tuple[str, ...] = tuple(
    s.name for s in TOOL_SPECS if s.kind == KIND_PROPOSAL
)
RAG_TOOL: str = next(s.name for s in TOOL_SPECS if s.kind == KIND_RAG)

# 需要教师点「确认执行」的里程碑；其余提案由助手自动执行后回报执
CONFIRM_TOOLS: frozenset[str] = frozenset(s.name for s in TOOL_SPECS if s.confirm)


def proposal_card_meta(tool: str) -> dict:
    """提案卡的展示元数据（随 action.payload 落库，历史卡片同样可渲染）。"""
    spec = TOOL_BY_NAME.get(tool)
    if spec is None:
        return {}
    return {
        "label": spec.label or tool,
        "impact": spec.impact,
        # auto=True：前端不显示确认按钮，直接执行并回报执
        "auto": not spec.confirm,
    }


# ---------------------------------------------------------------------------
# 提示词渲染（把注册表变成「工具清单 + 参数口径」，不再手写）
# ---------------------------------------------------------------------------

_TYPE_TEXT = {
    "string": "string",
    "number": "number",
    "integer": "number",
    "boolean": "bool",
}


def _type_text(prop: dict) -> str:
    kind = str(prop.get("type") or "any")
    if kind == "array":
        items = prop.get("items") if isinstance(prop.get("items"), dict) else {}
        return f"array<{_type_text(items) if items else 'any'}>"
    if kind == "object":
        return "object"
    return _TYPE_TEXT.get(kind, kind)


def _prop_doc(name: str, prop: dict, required: set[str]) -> str:
    """单个参数的呈现：key(必填/选填，类型)——可选值——说明。"""
    mark = "必填" if name in required else "选填"
    head = f"{name}({mark}，{_type_text(prop)})"
    bits: list[str] = []
    # 对象/数组元素的内层形状（如 question_type_ratios 的每项字段）
    inner = prop.get("items") if prop.get("type") == "array" else prop
    # 枚举可能挂在自己身上（标量）或挂在数组元素上（枚举型列表）
    enum = prop.get("enum") or (inner.get("enum") if isinstance(inner, dict) else None)
    if isinstance(enum, list) and enum:
        bits.append("可选值：" + "/".join(str(v) for v in enum))
    description = str(prop.get("description") or "").strip()
    if description:
        bits.append(description)
    if isinstance(inner, dict) and inner.get("type") == "object":
        inner_required = set(inner.get("required") or [])
        inner_rows = [
            _prop_doc(key, value if isinstance(value, dict) else {}, inner_required)
            for key, value in (inner.get("properties") or {}).items()
        ]
        if inner_rows:
            bits.append("每项：" + "；".join(inner_rows))
    return "——".join([head, "；".join(bits)]) if bits else head


def render_args(spec: ToolSpec) -> str:
    properties = spec.parameters.get("properties") or {}
    if not properties:
        return "args={}"
    required = set(spec.parameters.get("required") or [])
    rows = [
        _prop_doc(name, prop if isinstance(prop, dict) else {}, required)
        for name, prop in properties.items()
    ]
    return "args={" + "；".join(rows) + "}"


def render_section(kind: str) -> str:
    """渲染某类工具清单：一行说明 + 一行参数口径。"""
    lines: list[str] = []
    for spec in TOOL_SPECS:
        if spec.kind != kind:
            continue
        lines.append(f"- {spec.name}：{spec.doc}")
        if spec.parameters:
            lines.append(f"  {render_args(spec)}")
    return "\n".join(lines)


def tool_names() -> list[str]:
    """段1 function calling 的 action.tool 枚举（与注册表同源）。"""
    return [spec.name for spec in TOOL_SPECS]