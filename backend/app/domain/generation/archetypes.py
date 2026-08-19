from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from app.domain.blueprint.models import AssessmentMode


ComprehensiveArchetype: TypeAlias = Literal[
    "code_completion_scenario",
    "case_analysis",
    "fault_diagnosis",
    "comparative_decision",
    "solution_design",
    "process_optimization",
    "critique_correction",
    "integrated_explanation",
]
MaterialForm: TypeAlias = Literal[
    "code_skeleton",
    "config_template",
    "command_script",
    "case_text",
    "data_summary",
    "symptom_list",
    "error_process",
    "constraint_table",
    "option_matrix",
    "requirements",
    "resource_constraints",
    "process_description",
    "metric_summary",
    "incorrect_answer",
    "flawed_proposal",
    "compound_phenomenon",
    "causal_chain",
]


class ArchetypeContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_modes: frozenset[AssessmentMode] = Field(min_length=1)
    material_forms: frozenset[MaterialForm] = Field(min_length=1)
    question_template: str = Field(min_length=1)
    structure_requirements: tuple[str, ...] = Field(min_length=1)


ARCHETYPE_CONTRACTS: dict[ComprehensiveArchetype, ArchetypeContract] = {
    "code_completion_scenario": ArchetypeContract(
        allowed_modes={"application", "problem_solving", "practical_operation"},
        material_forms={"code_skeleton", "config_template", "command_script"},
        question_template=(
            "依据课程材料中的真实工具与操作流程给出一个具体的实践应用场景"
            "（场景领域以 prompt_material 为准，不预设特定课程），"
            "先以通俗文字交代任务目标与关键参数取值，再给出一段结构完整的代码框架、"
            "配置模板或操作脚本，在关键方法名、参数值、路径或命令选项处挖出带编号的空"
            "（形如 ____________(1)__________，共 4 至 6 处）。"
            "全题固定两个分问：（1）在不改变整体结构的前提下补全内容（占本题约六成分值）；"
            "（2）针对该场景的一个真实运行或优化问题分析原因并给出改进方向（占本题约四成分值）。"
            "代码、命令与参数只能来自 prompt_material，不得虚构课程材料未涉及的工具与用法。"
        ),
        structure_requirements=(
            "场景文字先说明任务目标和已给定的关键参数",
            "框架保持完整可读，挖空处为关键方法名、参数值或配置项",
            "分问（1）为补全内容，分问（2）为结合场景的原因分析与改进",
            "挖空处答案唯一且可从课程材料直接判定",
        ),
    ),
    "case_analysis": ArchetypeContract(
        allowed_modes={"application", "problem_solving"},
        material_forms={"case_text", "data_summary"},
        question_template="提供与课程事实一致的案例文本或数据摘要，先提取关键事实，再解释因果关系或据此解决明确问题；材料不得虚构超出 prompt_material 的业务细节。",
        structure_requirements=("从材料提取可核对事实", "把事实与课程原理建立联系", "结论必须由给定材料支持"),
    ),
    "fault_diagnosis": ArchetypeContract(
        allowed_modes={"problem_solving", "practical_operation"},
        material_forms={"symptom_list", "error_process"},
        question_template="给出异常表现清单或错误过程，要求依据症状逐步定位故障环节，说明判断依据、根因与可执行修正，并区分现象和原因。",
        structure_requirements=("呈现可诊断的异常表现", "要求给出定位依据与根因", "修正措施必须对应已定位原因"),
    ),
    "comparative_decision": ArchetypeContract(
        allowed_modes={"application", "problem_solving"},
        material_forms={"constraint_table", "option_matrix"},
        question_template="给出约束表或候选方案矩阵，要求按明确指标比较备选方案，在约束下作出选择，并用材料中的权衡依据说明决策。",
        structure_requirements=("至少包含可比较的候选方案", "约束和评价指标必须明确", "决策理由必须引用比较结果"),
    ),
    "solution_design": ArchetypeContract(
        allowed_modes={"problem_solving"},
        material_forms={"requirements", "resource_constraints"},
        question_template="给出需求清单或资源约束，要求设计满足边界的解决方案，说明组成、关键步骤、约束处理和验收依据，不预设唯一通用场景。",
        structure_requirements=("需求或资源边界可逐项核对", "方案组成与步骤相互衔接", "说明方案如何满足每项关键约束"),
    ),
    "process_optimization": ArchetypeContract(
        allowed_modes={"problem_solving"},
        material_forms={"process_description", "metric_summary"},
        question_template="给出现行流程描述或指标摘要，要求识别瓶颈，解释指标与瓶颈的关系，提出针对性优化并说明预期指标变化和验证方法。",
        structure_requirements=("现行流程或指标足以定位瓶颈", "优化措施必须对应瓶颈", "包含可验证的改进指标"),
    ),
    "critique_correction": ArchetypeContract(
        allowed_modes={"conceptual", "problem_solving"},
        material_forms={"incorrect_answer", "flawed_proposal"},
        question_template="给出错误答案或有缺陷的方案，要求逐点辨认问题、引用课程原则解释错误，并给出保持原目标的修正版与修正边界。",
        structure_requirements=("错误或缺陷必须可由课程知识判定", "批评包含明确原则依据", "修正版逐项回应已识别问题"),
    ),
    "integrated_explanation": ArchetypeContract(
        allowed_modes={"conceptual", "application"},
        material_forms={"compound_phenomenon", "causal_chain"},
        question_template="给出复合现象或因果链信息，要求串联多个相关概念，补全关键环节并解释各环节如何共同导致结果，避免拆成互不相关的知识点问答。",
        structure_requirements=("现象包含两个以上相互关联环节", "解释明确环节之间的因果联系", "结论形成完整而非并列的知识链"),
    ),
}
