"""Environment-backed settings used by the bootstrap application."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "postgresql+psycopg://exam:exam@localhost:5432/exam")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    mineru_api_token: str = os.getenv("MINERU_API_TOKEN", "")
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    seed_dev_data: bool = _bool_env("SEED_DEV_DATA", False)


settings = Settings()
