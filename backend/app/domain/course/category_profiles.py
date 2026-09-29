"""课程类别档案：创建课程时选定类别，类别预设本课程的题型集合与出题格式。

分层取用（生成装配时逐题型合并，见 ``generation_runner_service``）：

    全局档案 question_formats.py  <  类别预设（本文件）  <  课程考核规则 type_formats（教师/AI 助手可改）

- ``question_types``：该类别常规提供的题型（英文枚举）。当前作为题型池的
  **回退来源**（考点未声明可用题型时按类别给，见
  ``framework_service.allowed_question_types``）与前端展示；硬上限（蓝图
  题型池按类别裁剪）是后续扩展点，本轮不做。
- ``type_formats``：与全局默认**不同**的题型才登记；值是该题型的完整出题
  格式要求（**替换**而非追加），必须自带「自包含、答案唯一」等通用防线。
  未登记的题型自动回落全局档案。综合题由原型档案（archetypes.py）驱动，
  不接受类别/课程级格式覆盖。

换类别风格只改本档案；改单个课程的格式走考核规则 ``type_formats``。
"""
from __future__ import annotations

DEFAULT_CATEGORY = "general"

# 每类别：label（展示名）、description（创建弹窗说明）、question_types（常规题型）、
# type_formats（仅登记与全局默认不同的题型）。
COURSE_CATEGORY_PROFILES: dict[str, dict] = {
    "general": {
        "label": "通用",
        "description": "常规课程：使用系统默认的题型与出题格式。",
        "question_types": [
            "single_choice", "multiple_choice", "true_false",
            "fill_blank", "short_answer", "essay", "comprehensive",
        ],
        "type_formats": {},
    },
    "computer": {
        "label": "计算机与信息技术",
        "description": "偏操作与实践：填空落在术语/参数/命令，简答结合配置与排障场景。",
        "question_types": [
            "single_choice", "multiple_choice", "true_false",
            "fill_blank", "short_answer", "comprehensive",
        ],
        "type_formats": {
            "fill_blank": (
                "给出一个考查术语、参数、命令或关键结论的理论填空题。"
                "答案必须简短、唯一，不设计实际场景、开放分析或多步骤应用。"
                "题干中恰好包含 1 个空，用连续下划线（不少于 4 个下划线字符）表示，"
                "严禁出现 2 个及以上的空。"
                "空的位置应放在句末或句中关键位置，不应放在句首。"
                "空位必须由题干约束唯一确定——禁止把惯例值、默认值或可任选的"
                "示例（如可任取的种子、超参数取值）设为答案。"
                "涉及参数、命令或配置项时必须写清其归属（哪个框架/工具/流程），"
                "使题干脱离语境仍可独立理解。"
            ),
            "single_choice": (
                "给出一个明确问题和四个互斥选项。"
                "题干必须自包含——不依赖题外资料即可理解；涉及参数、命令、工具特性"
                "或配置时，题干写清其归属语境。"
                "四个选项应围绕同一维度展开，干扰项应有 plausible 的迷惑性但不能有"
                "明显错误。正确答案必须唯一且无歧义。"
                "选项长度应大致均衡，避免正确答案因长度异常而被猜出。"
            ),
            "true_false": (
                "给出一个可判定真伪的陈述句，常围绕概念辨析、操作规范或常见误解。"
                "陈述必须明确到可以简单判断'对/错'的程度，不能含糊或有争议。"
                "避免使用'总是'、'从不'等绝对化词汇（除非确实绝对正确）。"
                "错误陈述的错误点应隐蔽但明确，不应是显而易见的常识性错误。"
                "涉及命令/参数行为时写清适用前提，避免脱离语境产生歧义。"
            ),
            "short_answer": (
                "要求学生解释原理、比较方案或解决一个具体的技术问题，并给出评分点。"
                "题干应明确问题的范围和期望的回答深度；涉及场景时给出必要的前提"
                "（框架/工具/流程的归属）。"
                "答案应为2-5句话的核心要点，rubric 应列出关键的评分要素。"
                "explanation 应解释为什么正确答案是正确的，以及常见错误。"
            ),
        },
    },
    "science_engineering": {
        "label": "理工计算",
        "description": "偏计算与推导：填空落在公式结论与数值，简答强调思路与依据。",
        "question_types": [
            "single_choice", "multiple_choice", "true_false",
            "fill_blank", "short_answer", "comprehensive",
        ],
        "type_formats": {
            "fill_blank": (
                "给出一个考查公式结论、关键数值或成立条件的理论填空题。"
                "答案必须简短、唯一，不设计实际场景、开放分析或多步骤应用。"
                "题干中恰好包含 1 个空，用连续下划线（不少于 4 个下划线字符）表示，"
                "严禁出现 2 个及以上的空。"
                "空的位置应放在句末或句中关键位置，不应放在句首。"
                "空位必须由题干约束唯一确定——若结果依赖未给定的参数或可任选的条件，"
                "应在题干中补全约束，或改考结论表述。"
            ),
            "short_answer": (
                "要求学生说明推导思路、比较方法或解释现象，并给出评分点。"
                "题干应明确范围与期望深度，涉及计算时给出必要的已知条件。"
                "答案应为2-5句话的核心要点（思路、依据与结论），"
                "rubric 应列出关键的评分要素。"
                "explanation 应解释答案成立的理由以及常见错误。"
            ),
        },
    },
    "humanities": {
        "label": "人文社科",
        "description": "偏论述与材料分析：简答/论述强调观点与依据，评分点可分层。",
        "question_types": [
            "single_choice", "true_false", "fill_blank",
            "short_answer", "essay", "comprehensive",
        ],
        "type_formats": {
            "short_answer": (
                "要求学生解释概念、比较观点或分析材料，并给出评分点。"
                "题干应明确问题的范围与作答角度，必要时限定所依据的材料或界定。"
                "答案应为2-5句话的核心要点（观点+依据），"
                "rubric 应列出关键的评分要素。"
                "explanation 应说明评分要点与常见失分点。"
                "评分点须围绕题干界定展开且可区分层次，不设无边界的开放结论。"
            ),
            "essay": (
                "给出一个需要系统论述的开放性问题。"
                "题干应明确论述范围、立场或任务，必要时限定所依据的材料或观点范围，"
                "使学生知道要论证什么。"
                "答案给出核心论点与依据的要点化表述，rubric 列出评分要素。"
                "explanation 说明评分要点与常见失分点。"
                "论点须围绕题干界定展开，评分点可区分层次。"
            ),
        },
    },
}


def category_profile(category: str | None) -> dict:
    """按类别取档案；未知/空类别回退通用档（数据库旧值、外部传参都不至于炸）。"""
    key = str(category or "").strip()
    return COURSE_CATEGORY_PROFILES.get(key) or COURSE_CATEGORY_PROFILES[DEFAULT_CATEGORY]


def normalize_category(category: str | None) -> str:
    """创建/校验入口：命中档案才接受，否则回退默认类别。"""
    key = str(category or "").strip()
    return key if key in COURSE_CATEGORY_PROFILES else DEFAULT_CATEGORY


def available_categories() -> list[dict]:
    """给 API/前端的类别清单（key/label/description/question_types）。"""
    return [
        {
            "key": key,
            "label": profile["label"],
            "description": profile["description"],
            "question_types": list(profile["question_types"]),
        }
        for key, profile in COURSE_CATEGORY_PROFILES.items()
    ]
