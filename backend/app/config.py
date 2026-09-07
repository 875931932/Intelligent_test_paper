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
    deepseek_base_url: str = "https://api.xiaomimimo.com/v1"
    deepseek_model: str = "mimo-v2.5-pro"
    # 关闭 MiMo 思考模式（thinking）：结构化抽取不需要长链路推理，关闭可避免
    # 推理链消耗输出额度导致 JSON 内容为空/被截断，并节省推理 token。true=关闭。
    deepseek_disable_thinking: bool = True
    # 出题/命题阶段是否关闭 MiMo 思考模式。出题需要较长推理与质检，保留思考可提升
    # 题目质量与覆盖面；false=不关闭（保留思考）。此配置仅作用于命题生成，抽取/大纲
    # 阶段仍由 deepseek_disable_thinking 控制（默认关闭思考）。
    deepseek_generation_disable_thinking: bool = False
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "qwen3.7-text-embedding"
    embedding_api_format: str = "openai"
    # 召回规模直接决定分类阶段的 (考点, chunk) 对数量与模型 token 消耗：
    # top_k 24→12 且 min_score 0.25→0.30，削减重复 chunk 传递与无效分类输出。
    organization_retrieval_top_k: int = Field(default=12, gt=0)
    organization_retrieval_min_score: float = Field(default=0.30, ge=0, le=1)
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
