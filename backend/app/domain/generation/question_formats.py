"""题型出题格式档案：任务卡文本与输出 schema 的唯一定义点。

「每种题型长什么样」的可设置层——调整出题风格（选项结构、空数与空位、
答案形态、表述要求）只改本文件，不碰装配逻辑与模型网关：

- ``schemas/generation.py`` 只负责把档案装配进任务卡
  （``compile_batch_generation_payload``），不复制格式文本；
- ``adapters/model/llm_gateway.py`` 的批次提示词只写批次级规则
  （JSON 结构、知识范围、同批互补、禁用清单），禁止重复题型格式——
  双源必漂移，且对未分配该格式的原型是噪声；
- 综合题的原型格式在 ``domain/generation/archetypes.py``
  （ARCHETYPE_CONTRACTS，含原型专属 extra_format_rule）。

档案按题型键登记；未知题型由 ``_template_and_schema_for`` 报清晰错误
（蓝图/合同层在上游过滤）。全部文本与学科无关：任何课程的同题型共用
同一格式，换学科不需要改这里——课程想换风格走**类别预设**
（``domain/course/category_profiles.py``）与**考核规则 type_formats**，
由生成 runner 随知识卡字典注入本模块定义的 ``COURSE_TYPE_FORMATS_KEY`` 键，
``compile`` 装配时逐题型覆盖（未覆盖的题型仍回落本档案）。
"""

# 课程级题型格式覆盖随知识卡字典注入的约定键（runner 组装 → compile 消费）。
# 值形如 {question_type: 完整任务卡文本}；graph 内知识卡只按 card_id 精确取值
# （uuid 键不会撞上本约定键），因此该约定不需要改已封存的 generation_graph。
COURSE_TYPE_FORMATS_KEY = "__course_type_formats__"

QUESTION_TEMPLATES = {
    "single_choice": (
        "给出一个明确问题和四个互斥选项。"
        "题干必须自包含——不依赖题外资料即可理解。"
        "四个选项应围绕同一维度展开（如同一概念的不同定义、同一问题的不同方案），"
        "干扰项应有 plausible 的迷惑性但不能有明显错误。"
        "正确答案必须唯一且无歧义。"
        "选项长度应大致均衡，避免正确答案因长度异常而被猜出。"
    ),
    "true_false": (
        "给出一个可判定真伪的陈述句。"
        "陈述必须明确到可以简单判断'对/错'的程度，不能含糊或有争议。"
        "避免使用'总是'、'从不'等绝对化词汇（除非确实绝对正确）。"
        "错误陈述的错误点应隐蔽但明确，不应是显而易见的常识性错误。"
    ),
    "fill_blank": (
        "给出一个只考查术语、定义、条件或核心结论的理论填空题。"
        "答案必须简短、唯一，不设计实际场景、开放分析或多步骤应用。"
        "题干中恰好包含 1 个空，用连续下划线（不少于 4 个下划线字符）表示。"
        "严禁出现 2 个及以上的空。"
        "空的位置应放在句末或句中关键位置，不应放在句首。"
        "空位必须由题干约束唯一确定——禁止把惯例值、默认值或可任选的"
        "示例（如可任取的种子、超参数取值）设为答案。"
    ),
    "short_answer": (
        "要求学生解释原理、比较方法或解决问题，并给出评分点。"
        "题干应明确问题的范围和期望的回答深度。"
        "答案应为2-5句话的核心要点，rubric 应列出关键的评分要素。"
        "explanation 应解释为什么正确答案是正确的，以及常见错误。"
    ),
    "multiple_choice": (
        "给出一个明确问题和四个互斥选项。"
        "其中**有两个或以上**选项是正确的，其余为干扰项。"
        "题干必须自包含——不依赖题外资料即可理解。"
        "各选项应围绕同一维度展开，干扰项需 plausible 但不能有歧义地正确或错误。"
        "选项长度应大致均衡，避免正确项因长度规律被猜出。"
        "答案给出全部正确项的字母组合（如 AB）。"
    ),
    "essay": (
        "给出一个需要系统论述的开放性问题。"
        "题干应明确论述范围、立场或任务，使学生知道要论证什么。"
        "答案给出核心论点与依据的要点化表述，rubric 列出评分要素。"
        "explanation 说明评分要点与常见失分点。"
    ),
}

QUESTION_SCHEMAS = {
    "single_choice": {
        "stem": "string — 自包含的题干，不依赖外部资料",
        "options": "array[4] — 四个互斥选项，按同一维度排列，长度均衡",
        "answer": "string — 唯一正确答案（写选项字母如 B，或与某一选项完全一致的原文）",
    },
    "true_false": {
        "stem": "string — 可明确判定真伪的陈述句",
        "answer": "boolean — true 或 false",
    },
    "fill_blank": {
        "stem": "string — 恰好含 1 处连续下划线空（不少于4个_），空在句中或句末，空位须被题干约束为唯一解",
        "answer": "string — 简短唯一的术语、数值或短语",
    },
    "short_answer": {
        "stem": "string — 明确问题和期望回答深度",
        "answer": "string — 2-5句话的核心要点",
        "explanation": "string — 解释答案正确性和常见错误",
        "rubric": "array — 评分要素列表",
    },
    "multiple_choice": {
        "stem": "string — 自包含的题干，不依赖外部资料",
        "options": "array[4] — 四个互斥选项，按同一维度排列，长度均衡",
        "answer": "string — 全部正确项的字母组合（如 AB），至少两个",
    },
    "essay": {
        "stem": "string — 明确论述范围与任务的开放性问题",
        "answer": "string — 核心论点与依据的要点化表述",
        "explanation": "string — 评分要点与常见失分点",
        "rubric": "array — 评分要素列表",
    },
}
