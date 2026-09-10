"""Environment-backed settings used by the bootstrap application."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings read directly from the environment on every instantiation."""

    model_config = SettingsConfigDict(extra="ignore", env_file=".env", env_file_encoding="utf-8")

    database_url: str = "postgresql+psycopg://exam:exam@localhost:5432/exam"
    redis_url: str = "redis://localhost:6379/0"
    mineru_api_token: str = ""
    mineru_base_url: str = "https://mineru.net"
    mineru_model_version: str = "vlm"
    mineru_poll_interval_seconds: int = 10
    mineru_max_poll_seconds: int = 1800
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.stepfun.com/v1"
    deepseek_model: str = "step-3.7-flash"
    # 知识目录组织阶段按能力分层选模：分类只做相关性判断，可换经济模型；
    # 归并做知识抽取，需更强模型。两者留空时回退到 deepseek_model，不受影响。
    deepseek_classify_model: str = ""
    deepseek_consolidate_model: str = ""
    # 抽取/分类/归并阶段关闭长链路推理：MiMo 发 thinking=disabled；StepFun 无
    # 思考开关，改用 reasoning_effort=low（官方标注 low 档适合信息抽取），
    # 避免推理消耗输出额度导致 JSON 内容为空/被截断，并节省 token。true=关闭。
    deepseek_disable_thinking: bool = True
    # 出题/命题阶段保留推理（StepFun 默认 medium 档，不额外设置）。出题需要较长
    # 推理与质检，保留思考可提升题目质量与覆盖面；false=不关闭（保留思考）。
    # 此配置仅作用于命题生成，抽取/大纲阶段仍由 deepseek_disable_thinking 控制。
    deepseek_generation_disable_thinking: bool = False
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "qwen3.7-text-embedding"
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
    # 知识目录组织阶段的模型调用超时（秒）。分类/归并 prompt 较大（数万 token），
    # 默认 90s 超时在 MiMo 上不足以完成响应，超时失败会整材料放弃并烧掉 token；
    # 该阶段已异步化，放长超时不影响前端体验。
    organization_model_timeout: float = Field(default=240.0, gt=0)
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
