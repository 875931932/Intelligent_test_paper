"""Material parsing orchestration: submit to MinerU and advance parse runs.

教师控制台的解析驱动：上传完成后教师显式触发解析（或复用同哈希的
ready 结果），前端轮询 poll 端点推进状态机直至 ready/failed。刻意不
做后台自动触发——教师主动确认"整理"边界（设计文档 §4.7 主动整理）。
"""

from __future__ import annotations

import asyncio
from typing import Iterator, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.document.mineru_client import MineruClient
from app.adapters.document.protocol import DocumentParserError, ParseRequest
from app.config import settings
from app.db.schema import document_parse_runs, material_versions, materials, parser_profiles
from app.services import material_service
from app.services.document_processing_service import create_parse_run, poll_parse_run, submit_parse_run


def _ensure_parser_profile(session: Session, *, course_id: str) -> str:
    """按课程获取或创建 MinerU 解析 profile（幂等），返回 profile id。"""

    version = settings.mineru_model_version
    existing = session.execute(
        select(parser_profiles.c.id).where(
            parser_profiles.c.course_id == course_id,
            parser_profiles.c.name == "mineru",
            parser_profiles.c.version == version,
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    from uuid import uuid4

    profile_id = uuid4().hex
    session.execute(
        parser_profiles.insert().values(
            id=profile_id,
            course_id=course_id,
            name="mineru",
            version=version,
            provider="mineru",
            configuration={"model_version": version},
        )
    )
    session.commit()
    return profile_id


class ParseStorage(Protocol):
    def stream_object(self, object_key: str) -> Iterator[bytes]: ...


class ParseError(Exception):
    """解析编排失败（对 API 层暴露为 4xx/5xx 语义信息）。"""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def build_mineru_client() -> MineruClient:
    if not settings.mineru_api_token.strip():
        raise ParseError("mineru is not configured", status_code=503)
    return MineruClient(
        base_url=settings.mineru_base_url,
        token=settings.mineru_api_token,
        timeout_seconds=90,
    )


def _latest_version(session: Session, *, course_id: str, material_id: str) -> dict:
    material = material_service.get_material(session, course_id=course_id, material_id=material_id)
    latest = material.get("latest_version")
    if latest is None:
        raise ParseError("material has no uploaded version")
    row = session.execute(
        select(material_versions).where(
            material_versions.c.id == latest["id"],
            material_versions.c.course_id == course_id,
        )
    ).mappings().one_or_none()
    if row is None:
        raise ParseError("material version was not found", status_code=404)
    return dict(row)


def latest_parse_status(session: Session, *, course_id: str, material_version_id: str) -> dict | None:
    """最新版本最近的解析运行（各状态，含 terminal），供资料列表展示。"""

    row = session.execute(
        select(
            document_parse_runs.c.id,
            document_parse_runs.c.status,
            document_parse_runs.c.error_code,
            document_parse_runs.c.error_summary,
        )
        .where(
            document_parse_runs.c.course_id == course_id,
            document_parse_runs.c.material_version_id == material_version_id,
        )
        .order_by(document_parse_runs.c.created_at.desc())
        .limit(1)
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def start_parse(
    session: Session,
    storage: ParseStorage,
    *,
    course_id: str,
    material_id: str,
) -> dict:
    """为资料最新版本启动解析：同哈希 ready 结果直接复用，否则提交 MinerU。"""

    version = _latest_version(session, course_id=course_id, material_id=material_id)
    filename = session.execute(
        select(materials.c.logical_name).where(
            materials.c.id == version["material_id"], materials.c.course_id == course_id
        )
    ).scalar_one()
    profile_id = _ensure_parser_profile(session, course_id=course_id)
    run_id, reused = create_parse_run(
        session,
        course_id=course_id,
        material_version_id=version["id"],
        parser_profile_id=profile_id,
    )
    if reused:
        session.commit()
        return {"run_id": run_id, "status": "ready", "reused": True}
    # 先提交 parse run 再提交 MinerU：submit_parse_run 内部以 rollback
    # 开场清理会话状态，未提交的插入会被丢弃
    session.commit()
    parser = build_mineru_client()

    def content_factory():
        async def _stream():
            for chunk in storage.stream_object(version["object_key"]):
                yield chunk

        return _stream()

    request = ParseRequest(
        material_version_id=version["id"],
        filename=filename,
        content_type=version["mime_type"],
        content_factory=content_factory,
        model_version=settings.mineru_model_version,
    )
    try:
        asyncio.run(submit_parse_run(session, parser, course_id=course_id, run_id=run_id, request=request))
    except DocumentParserError as exc:
        raise ParseError(f"mineru submit failed: {exc}", status_code=502) from exc
    row = session.execute(
        select(document_parse_runs.c.status).where(document_parse_runs.c.id == run_id)
    ).scalar_one()
    return {"run_id": run_id, "status": row, "reused": False}


def advance_parse(
    session: Session,
    storage: ParseStorage,
    *,
    course_id: str,
    material_id: str,
) -> dict:
    """推进一次解析状态机：轮询 MinerU；完成则拉取产物并落块。

    返回当前状态；terminal 状态重复调用为幂等空转。
    """

    version = _latest_version(session, course_id=course_id, material_id=material_id)
    row = session.execute(
        select(document_parse_runs).where(
            document_parse_runs.c.course_id == course_id,
            document_parse_runs.c.material_version_id == version["id"],
        )
        .order_by(document_parse_runs.c.created_at.desc())
        .limit(1)
    ).mappings().one_or_none()
    if row is None:
        raise ParseError("material has no parse run; start parsing first", status_code=404)
    run = dict(row)
    if run["status"] in {"ready", "failed"}:
        return {
            "run_id": run["id"],
            "status": run["status"],
            "error_code": run["error_code"],
            "error_summary": run["error_summary"],
        }
    if run["status"] == "queued":
        raise ParseError("parse run was created but never submitted", status_code=409)
    parser = build_mineru_client()
    try:
        status = asyncio.run(
            poll_parse_run(session, parser, storage, course_id=course_id, run_id=run["id"])
        )
    except DocumentParserError as exc:
        raise ParseError(f"mineru poll failed: {exc}", status_code=502) from exc
    fresh = session.execute(
        select(
            document_parse_runs.c.status,
            document_parse_runs.c.error_code,
            document_parse_runs.c.error_summary,
        ).where(document_parse_runs.c.id == run["id"])
    ).mappings().one()
    return {
        "run_id": run["id"],
        "status": status,
        "error_code": fresh["error_code"],
        "error_summary": fresh["error_summary"],
    }
