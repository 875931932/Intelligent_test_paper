"""FastAPI application with a dependency-safe health endpoint."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import create_engine, select, text

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from app.config import settings
from app.db.session import get_session
from app.api.v1.admin import router as admin_router
from app.api.v1.auth import get_current_user, router as auth_router
from app.api.v1.courses import router as courses_router
from app.api.v1.framework import router as framework_router
from app.api.v1.knowledge import router as knowledge_router
from app.api.v1.knowledge_base import router as knowledge_base_router
from app.api.v1.materials import router as materials_router
from app.api.v1.exam_projects import router as exam_projects_router
from app.api.v1.paper_versions import router as paper_versions_router
from app.api.v1.paper_archives import router as paper_archives_router
from app.api.v1.assistant import router as assistant_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


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
        # LLM 需要 api_key + base_url + model 三项齐全才算 configured：网关构造期
        # 对三者都做非空校验，只报 key 会把"缺端点/缺模型名"藏成 configured。
        "llm": "configured"
        if all(
            _configured(value)
            for value in (settings.llm_api_key, settings.llm_base_url, settings.llm_model)
        )
        else "not_configured",
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
app.include_router(admin_router)
app.include_router(courses_router)
app.include_router(materials_router)
app.include_router(framework_router)
app.include_router(knowledge_router)
app.include_router(knowledge_base_router)
app.include_router(exam_projects_router)
app.include_router(paper_versions_router)
app.include_router(paper_archives_router)
app.include_router(assistant_router)


@app.get("/api/v1/health")
def health() -> dict:
    return health_payload()


def _authorize_local_put(session, object_key: str, user_id: str) -> bool:
    """本地对象写入必须对应本人、未过期且待完成的上传会话。

    只允许写入 ``courses/.../uploads/...`` 与
    ``knowledge/.../uploads/...`` 的上传路径，且 object_key 必须与上传会话
    登记的键完全一致；已完成的版本对象不可再被客户端覆盖。
    """

    if not object_key or ".." in object_key or object_key.startswith(("/", "\\")) or "\\" in object_key:
        return False
    from app.db.schema import Course, knowledge_upload_sessions, upload_sessions

    now = datetime.now(timezone.utc)

    def _not_expired(value) -> bool:
        if value is None:
            return True
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value > now

    if object_key.startswith("knowledge/"):
        row = session.execute(
            select(
                knowledge_upload_sessions.c.uploader_id,
                knowledge_upload_sessions.c.status,
                knowledge_upload_sessions.c.expires_at,
            ).where(knowledge_upload_sessions.c.object_key == object_key)
        ).one_or_none()
        if row is None or row.uploader_id != user_id or row.status != "created":
            return False
        return _not_expired(row.expires_at)

    if object_key.startswith("courses/"):
        course_table = Course.__table__
        row = session.execute(
            select(
                course_table.c.owner_id,
                upload_sessions.c.status,
                upload_sessions.c.expires_at,
            )
            .join(course_table, course_table.c.id == upload_sessions.c.course_id)
            .where(upload_sessions.c.object_key == object_key)
        ).one_or_none()
        if row is None or row.owner_id != user_id or row.status != "pending":
            return False
        return _not_expired(row.expires_at)

    return False


@app.put("/api/v1/_local-storage/{object_key:path}")
async def local_storage_put(
    object_key: str,
    request: Request,
    current_user=Depends(get_current_user),
    session=Depends(get_session),
) -> Response:
    """LocalStorage 回退：接收前端 PUT 上传的二进制文件。

    写入必须绑定本人未过期的上传会话；任意已登录用户覆盖他人课程或知识库
    版本对象的路径一律拒绝。正式共享部署应直接使用 MinIO/S3 预签名上传。
    """
    from fastapi import HTTPException

    from app.adapters.storage.local_storage import LocalStorage

    if not _authorize_local_put(session, object_key, current_user.id):
        raise HTTPException(
            status_code=404, detail={"code": "RESOURCE_NOT_FOUND", "message": "resource not found"}
        )
    storage = getattr(request.app.state, "storage", None)
    if storage is None or not isinstance(storage, LocalStorage):
        storage = LocalStorage()
        request.app.state.storage = storage
    body = await request.body()
    storage.put_bytes(object_key, body, content_type=request.headers.get("content-type", ""))
    return Response(status_code=200)
