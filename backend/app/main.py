"""FastAPI application with a dependency-safe health endpoint."""

from __future__ import annotations

from sqlalchemy import create_engine, text

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from app.config import settings
from app.api.v1.auth import router as auth_router
from app.api.v1.courses import router as courses_router
from app.api.v1.framework import router as framework_router
from app.api.v1.blueprints import router as blueprints_router
from app.api.v1.generation import router as generation_router
from app.api.v1.knowledge import router as knowledge_router
from app.api.v1.materials import router as materials_router
from app.api.v1.exam_projects import router as exam_projects_router
from app.api.v1.paper_versions import router as paper_versions_router


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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(courses_router)
app.include_router(materials_router)
app.include_router(framework_router)
app.include_router(knowledge_router)
app.include_router(blueprints_router)
app.include_router(generation_router)
app.include_router(exam_projects_router)
app.include_router(paper_versions_router)


@app.get("/api/v1/health")
def health() -> dict:
    return health_payload()


@app.put("/api/v1/_local-storage/{object_key:path}")
async def local_storage_put(object_key: str, request: Request) -> Response:
    """LocalStorage 回退：接收前端 PUT 上传的二进制文件。"""
    from app.adapters.storage.local_storage import LocalStorage

    storage = getattr(request.app.state, "storage", None)
    if storage is None or not isinstance(storage, LocalStorage):
        storage = LocalStorage()
        request.app.state.storage = storage
    body = await request.body()
    storage.put_bytes(object_key, body, content_type=request.headers.get("content-type", ""))
    return Response(status_code=200)
