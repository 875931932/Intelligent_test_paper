"""Environment-backed settings used by the bootstrap application."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings read directly from the environment on every instantiation."""

    model_config = SettingsConfigDict(extra="ignore")

    database_url: str = "postgresql+psycopg://exam:exam@localhost:5432/exam"
    redis_url: str = "redis://localhost:6379/0"
    mineru_api_token: str = ""
    deepseek_api_key: str = ""
    seed_dev_data: bool = False
    upload_max_bytes: int = 209715200
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minio-dev"
    s3_secret_key: str = "minio-dev-password"
    s3_bucket: str = "exam-materials"
    s3_region: str = "us-east-1"


settings = Settings()
