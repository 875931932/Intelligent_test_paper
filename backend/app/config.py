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
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "qwen3.7-text-embedding"
    embedding_api_format: str = "openai"
    organization_retrieval_top_k: int = Field(default=24, gt=0)
    organization_retrieval_min_score: float = Field(default=0.25, ge=0, le=1)
    organization_max_workers: int = Field(default=16, gt=0)
    seed_dev_data: bool = False
    upload_max_bytes: int = 209715200
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minio-dev"
    s3_secret_key: str = "minio-dev-password"
    s3_bucket: str = "exam-materials"
    s3_region: str = "us-east-1"


settings = Settings()
