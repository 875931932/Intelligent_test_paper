"""FastAPI application with a dependency-safe health endpoint."""

from __future__ import annotations

from sqlalchemy import create_engine, text

from fastapi import FastAPI

from app.config import settings
from app.api.v1.courses import router as courses_router
from app.api.v1.framework import router as framework_router
from app.api.v1.materials import router as materials_router


def _configured(value: str) -> bool:
    return bool(value and value.strip())


def _database_status() -> str:
    engine = None
    try:
        engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        return "unavailable"
    finally:
        if engine is not None:
            engine.dispose()


def _redis_status() -> str:
    if not _configured(settings.redis_url):
        return "not_configured"
    client = None
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=0.2, socket_timeout=0.2)
        return "ok" if client.ping() else "unavailable"
    except Exception:
        return "unavailable"
    finally:
        if client is not None:
            client.close()


def health_payload() -> dict:
    return {
        "api": "ok",
        "postgresql": _database_status(),
        "redis": _redis_status(),
        "mineru": "configured" if _configured(settings.mineru_api_token) else "not_configured",
        "deepseek": "configured" if _configured(settings.deepseek_api_key) else "not_configured",
    }


app = FastAPI(title="AI Exam System")
app.include_router(courses_router)
app.include_router(materials_router)
app.include_router(framework_router)


@app.get("/api/v1/health")
def health() -> dict:
    return health_payload()
