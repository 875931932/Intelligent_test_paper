"""Environment-backed settings used by the bootstrap application."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_env_file(start: Path | str | None = None) -> str:
    """定位 `.env`：从 `start`（默认本文件）起逐级向上找，找不到才退回 `".env"`。

    历史上这里写死相对路径 `".env"`，而 pydantic-settings 按**进程 CWD** 解析它。
    后端按文档是在 `backend/` 里启动的（Celery worker 更是只能手工 `cd backend`），
    仓库根的 `.env` 因此一次都没被读到，`settings` 静默回落到当时代码里的默认值——
    其中 `llm_base_url` 曾默认 `https://api.stepfun.com/v1`，一个**能连通但
    账号无额度**的 StepFun 端点。后果就是"框架/知识目录都正常，唯独生成试卷永远
    402 quota_exceeded"：那几个阶段跑在注入了环境变量的 uvicorn 里，出题跑在
    没注入的 worker 里。改配置后不重启进程也一样看不出来（settings 是 import
    期快照）。

    改为从 `__file__` 向上查找后，worker / 脚本 / `python -c` / pytest 在任何
    CWD 下都必然读到同一份 `.env`，不再有"取决于你怎么启动"的隐式分支。
    """
    origin = Path(start).resolve() if start is not None else Path(__file__).resolve()
    for directory in origin.parents:
        candidate = directory / ".env"
        if candidate.is_file():
            return str(candidate)
    return ".env"


class Settings(BaseSettings):
    """Settings read directly from the environment on every instantiation."""

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=_resolve_env_file(),
        env_file_encoding="utf-8",
    )

    database_url: str = "postgresql+psycopg://exam:exam@localhost:5432/exam"
    redis_url: str = "redis://localhost:6379/0"
    mineru_api_token: str = ""
    mineru_base_url: str = "https://mineru.net"
    mineru_model_version: str = "vlm"
    mineru_poll_interval_seconds: int = 10
    mineru_max_poll_seconds: int = 1800
    # 模型配置唯一来源是仓库根 .env（LLM_API_KEY/BASE_URL/MODEL）：这里不设
    # 代码默认值，缺配置时在网关构造期即抛 ValueError / health 报 not_configured，
    # 而不是静默回落到某个"能连通却非所配"的端点（402 事故根因）。
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""

    def llm_configured(self) -> bool:
        """LLM 三件套（key/base_url/model）是否齐备——齐备才允许发起真实模型调用。

        generate 端点的 503、生成运行器的前置校验、AI 改题的入队检查一律用它，
        禁止各自复制判定式（判定标准只此一处）。
        """
        return all(
            value.strip()
            for value in (self.llm_api_key, self.llm_base_url, self.llm_model)
        )
    # 知识目录组织阶段按能力分层选模：分类只做相关性判断，可换经济模型；
    # 归并做知识抽取，需更强模型。两者留空时回退到 llm_model，不受影响。
    llm_classify_model: str = ""
    llm_consolidate_model: str = ""
    # 知识点抽取模型：把原始文本块蒸馏成自包含知识点陈述、剔除封面/行政/纯
    # 操作流程等非知识块。留空回退到 llm_model，不受影响。
    llm_extract_model: str = ""
    # 抽取阶段单次调用送入的原始块数：chunk 约 1200 字符，输出为每块 0~3 条
    # 陈述。step-3.7-flash 是推理型模型：批过大（6 块重型块）时模型把全部输出
    # 预算耗在 reasoning 字段，content 为空导致整批失败；批 3 重型块实测 14s 稳定
    # 返回，思考在预算内结束且留有 content 输出。
    organization_extraction_batch_size: int = Field(default=3, gt=0)
    organization_extraction_max_tokens: int = Field(default=3072, gt=0)
    # 知识点抽取的推理强度（StepFun step-3.7-flash 的 reasoning_effort 三档：
    # low/medium/high）。信息抽取用 low 最省预算，避免思考占满输出额度导致
    # content 为空/截断非 JSON。显式下发优先于全局 llm_disable_thinking。
    organization_extraction_reasoning_effort: str = "low"
    # 抽取/分类/归并阶段关闭长链路推理：OpenAI 兼容端点发 thinking=disabled；
    # StepFun 无思考开关，改用 reasoning_effort=low（官方标注 low 档适合信息
    # 抽取），避免推理消耗输出额度导致 JSON 内容为空/被截断，并节省 token。true=关闭。
    llm_disable_thinking: bool = True
    # 命题生成阶段同样关闭长链路推理：StepFun 走 reasoning_effort=low（官方
    # low 档适合一次性命题，逐批出题所需的推理已足够，且能大幅缩短单次调用），
    # 其余 OpenAI 兼容端点发 thinking=disabled。此前默认 false 会让推理型模型用高档思考把单次
    # 请求拖到分钟级，是「出一套 41 题要近 50 分钟」的根因之一。true=关闭思考。
    # 此配置仅作用于命题生成，抽取/大纲阶段仍由 llm_disable_thinking 控制。
    llm_generation_disable_thinking: bool = True
    # embedding 同样只认 .env（EMBEDDING_*）：不设代码默认端点/模型名，缺配置
    # 在使用处响亮失败，避免静默打到某个公共端点上。
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_api_format: str = "openai"
    # 召回规模直接决定分类阶段的 (考点, chunk) 对数量与模型 token 消耗：
    # top_k 24→12 且 min_score 0.25→0.30，削减重复 chunk 传递与无效分类输出。
    organization_retrieval_top_k: int = Field(default=12, gt=0)
    organization_retrieval_min_score: float = Field(default=0.30, ge=0, le=1)
    # 检索查询增强：操作/实验类考点的 retrieval_intent 是"动词+对象"短句，与材料
    # 中"知识陈述"式文本词面重叠少，收紧阈值后相关块漏召回。开启后每个考点用
    # retrieval_intent 与「考点名+考核要求」两个 query 分别检索再合并取 top_k，
    # 不增加模型调用（仅多一次嵌入），零破坏地提升召回。
    organization_retrieval_expand_query: bool = True
    organization_max_workers: int = Field(default=16, gt=0)
    # 生成自愈：题位在本考点内三道防线（单题重试 → 换同考点原子 → 批缺失恢复）
    # 全部失守后，允许从**同章**其他考点回补，避免整题丢弃导致卷面缺题。
    # 上限刻意收得很紧——回补会改变题目实际考查的考点，只做兜底不作常态：
    #   max_per_batch  每批（≈一个考点的题位）最多回补几题
    #   max_per_point  同一兄弟考点最多被回补几题（防止把一个富余考点抽干）
    generation_backfill_max_per_batch: int = Field(default=1, ge=0)
    generation_backfill_max_per_point: int = Field(default=1, ge=0)
    # 可用题（有题干且有答案）占合同题位的最低比例。低于此值说明模型服务基本
    # 不可用或知识卡大面积不合格，此时整卷没有交付价值——直接让任务失败并给出
    # 明确原因，而不是"成功"地产出一张大面积空题的卷子让教师踩坑。
    generation_min_usable_ratio: float = Field(default=0.5, ge=0, le=1)
    # 知识目录组织阶段的模型调用超时（秒）。分类/归并 prompt 较大（数万 token），
    # 默认 90s 超时在推理型模型上不足以完成响应，超时失败会整材料放弃并烧掉 token；
    # 该阶段已异步化，放长超时不影响前端体验。
    organization_model_timeout: float = Field(default=240.0, gt=0)
    # 框架大纲抽取阶段的模型调用超时（秒）。考核大纲 prompt 较大（数万 token），
    # 默认 90s 超时会触发 transport error，需放长到足以容纳完整响应。
    framework_model_timeout: float = Field(default=240.0, gt=0)
    seed_dev_data: bool = False
    upload_max_bytes: int = 209715200
    s3_endpoint: str = "http://localhost:9000"
    # 后端自身访问对象存储的内网地址（例如 http://127.0.0.1:9000）。
    # 为空时后端直接使用 s3_endpoint；生成给浏览器直传的预签名 URL 始终使用 s3_endpoint。
    # 当 s3_endpoint 填的是公网地址（经 nginx 反代）却无法回环访问自身公网 IP 时，
    # 需要配置此变量为 MinIO 实际监听的内网地址，避免 head_bucket 等操作触发 301/重定向循环。
    s3_internal_endpoint: str = ""
    s3_access_key: str = "minio-dev"
    s3_secret_key: str = "minio-dev-password"
    s3_bucket: str = "exam-materials"
    s3_region: str = "us-east-1"
    # 登录令牌 HMAC 签名密钥，生产环境务必通过环境变量改写
    auth_secret: str = "exam-auth-dev-secret-change-me"
    auth_token_ttl_seconds: int = 7 * 24 * 3600


settings = Settings()
