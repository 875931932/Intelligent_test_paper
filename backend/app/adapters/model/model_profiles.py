"""模型调优档案：按模型集中管理供应商参数与能力，换模型只改 .env。

依据 StepFun 官方文档（https://platform.stepfun.ai/docs，2026-09-25 核对）：

- **思考控制**：Chat Completions 用 ``reasoning_effort`` 三档 low/medium/high。
  官方口径：low=信息抽取/摘要类任务（本项目抽取/分类/归并即依此选定）、
  medium=通用推理（官方默认推荐）、high=复杂数学/规划/代码。旧式 OpenAI
  兼容端点的关闭思考参数为 ``thinking={"type":"disabled"}``。
  ⚠️ 并非所有型号支持三档——``step-3.5-flash-2603`` 只收 low/high，下发
  非法档位会触发 400。故档案登记各型号支持集，网关统一把调用方档位
  收敛到支持集内（normalize_effort）。
- **结构约束**：``response_format={"type":"json_object"}`` 保证可解析
  （JSON Mode）；``{"type":"json_schema","json_schema":{...,"strict":true}}``
  按 schema 约束解码（constrained decoding），必填字段在场由协议保证，
  从源头消灭 model_schema_validation_failed（2026-09-25 实测生产
  step_plan 端点接受该参数且输出结构合规）。
- **tool_choice**：StepFun 端点未文档化该参数，下发会 400（既有经验），
  故 stepfun 档案 supports_tool_choice=False。

换模型的操作成本：
- 换已收录型号：只改 .env 的 ``LLM_MODEL``（或分阶段 ``LLM_EXTRACT_MODEL``
  等），网关按模型名自动匹配档案，业务代码零改动。
- 接入未收录型号：在下方 ``MODEL_PROFILES`` 加一条档案即可；不加也不会坏——
  :func:`resolve_model_profile` 按 base_url 回退通用 OpenAI 兼容档（只关闭
  思考，不发 reasoning_effort / tool_choice / json_schema 等供应商特性参数）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("model.profile")

# StepFun 三档思考深度（官方文档：low=信息抽取，medium=通用推理，high=复杂推理）。
_STEPFUN_EFFORTS: tuple[str, ...] = ("low", "medium", "high")


@dataclass(frozen=True)
class ModelProfile:
    """一个型号的调用参数档案：思考控制风格 + 能力开关。"""

    model_id: str
    # 思考控制下发方式：reasoning_effort（StepFun 三档）| thinking_disabled（旧式 OpenAI 兼容）
    thinking_style: str
    # 是否可下发 tool_choice=required（StepFun 未文档化，下发 400）
    supports_tool_choice: bool = True
    # 该型号接受的 reasoning_effort 档位（空=不支持）
    supported_efforts: tuple[str, ...] = ()
    # disable_thinking 且调用方未显式指定时的缺省档位（None=不下发）
    default_effort: str | None = None
    # 是否可用 response_format=json_schema strict 结构约束
    supports_json_schema: bool = False


def _stepfun_profile(model_id: str, *, supported_efforts: tuple[str, ...] = _STEPFUN_EFFORTS) -> ModelProfile:
    return ModelProfile(
        model_id=model_id,
        thinking_style="reasoning_effort",
        supports_tool_choice=False,
        supported_efforts=supported_efforts,
        default_effort="low",
        supports_json_schema=True,
    )


# 已核档型号（模型清单见官方 Quickstart：step-5-preview / step-3.7-flash /
# step-3.5-flash；step-3.5-flash-2603 为仅 two-tier 的特殊版本）。
MODEL_PROFILES: dict[str, ModelProfile] = {
    "step-5-preview": _stepfun_profile("step-5-preview"),
    "step-3.7-flash": _stepfun_profile("step-3.7-flash"),
    "step-3.5-flash": _stepfun_profile("step-3.5-flash"),
    "step-3.5-flash-2603": _stepfun_profile(
        "step-3.5-flash-2603", supported_efforts=("low", "high")
    ),
}


def resolve_model_profile(model: str, *, base_url: str) -> ModelProfile:
    """按模型名查档案；未收录时按 base_url 判定供应商，再回退通用档。

    顺序：型号档案优先（换模型档案自动跟随）→ base_url 含 stepfun 的
    未收录型号按端点特性回退（保持档案化之前的行为）→ 通用 OpenAI 兼容档。
    """
    profile = MODEL_PROFILES.get(model)
    if profile is not None:
        return profile
    if "stepfun" in base_url.lower():
        return _stepfun_profile(model)
    return ModelProfile(model_id=model, thinking_style="thinking_disabled")


def normalize_effort(effort: str | None, profile: ModelProfile) -> str | None:
    """把调用方档位收敛到型号支持集；不在集内退回档案缺省档（可能为 None）。

    典型场景：调用方固定传 medium，而型号是只收 low/high 的
    step-3.5-flash-2603——不收敛会 400，收敛到缺省档保住调用。
    """
    if effort is None:
        return None
    if effort in profile.supported_efforts:
        return effort
    if profile.default_effort is not None:
        logger.warning(
            "reasoning_effort=%s 不被模型 %s 支持（支持集=%s），回退档位=%s",
            effort,
            profile.model_id,
            "/".join(profile.supported_efforts) or "-",
            profile.default_effort,
        )
        return profile.default_effort
    return None
