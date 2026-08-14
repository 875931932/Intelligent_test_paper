"""FastAPI application with a dependency-safe health endpoint."""

from __future__ import annotations

from sqlalchemy import create_engine, text

from fastapi import FastAPI

from app.config import settings


def _configured(value: str) -> bool:
    return bool(value and value.strip())


def _database_status() -> str:
    try:
        engine = create_engine(settings.database_url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return "ok"
    except Exception:
        return "unavailable"


def _redis_status() -> str:
    if not _configured(settings.redis_url):
        return "not_configured"
    try:
        import redis

        return "ok" if redis.Redis.from_url(settings.redis_url, socket_connect_timeout=0.2).ping() else "unavailable"
    except Exception:
        return "unavailable"


def health_payload() -> dict:
    return {
        "api": "ok",
        "postgresql": _database_status(),
        "redis": _redis_status(),
        "mineru": "configured" if _configured(settings.mineru_api_token) else "not_configured",
        "deepseek": "configured" if _configured(settings.deepseek_api_key) else "not_configured",
    }


app = FastAPI(title="AI Exam System")


@app.get("/api/v1/health")
def health() -> dict:
    return health_payload()
