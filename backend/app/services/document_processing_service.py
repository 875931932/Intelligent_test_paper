"""Safe MinerU artifact inspection and content normalization."""

from __future__ import annotations

import hashlib
import io
import json
import math
import posixpath
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Protocol
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.adapters.document.protocol import ContentBlock, DocumentParser, DocumentProtocolError, ParseRequest, ParseState
from app.db.schema import content_blocks, document_artifacts, document_parse_runs, material_versions


@dataclass(frozen=True)
class ParsedDocument:
    blocks: list[ContentBlock]
    artifacts: dict[str, bytes]


class ArtifactStore(Protocol):
    def put_bytes(self, object_key: str, content: bytes, content_type: str) -> None: ...


def create_parse_run(
    session: Session,
    *,
    course_id: str,
    material_version_id: str,
    parser_profile_id: str,
) -> tuple[str, bool]:
    """Create a parse run, reusing a ready result for the same hash/profile."""

    target_hash = session.execute(
        select(material_versions.c.sha256).where(
            material_versions.c.id == material_version_id,
            material_versions.c.course_id == course_id,
        )
    ).scalar_one_or_none()
    if target_hash is None:
        raise DocumentProtocolError("material version was not found")
    reusable = session.execute(
        select(document_parse_runs)
        .join(material_versions, material_versions.c.id == document_parse_runs.c.material_version_id)
        .where(
            document_parse_runs.c.course_id == course_id,
            document_parse_runs.c.parser_profile_id == parser_profile_id,
            document_parse_runs.c.status == "ready",
            material_versions.c.sha256 == target_hash,
        )
        .order_by(document_parse_runs.c.completed_at.desc())
        .limit(1)
    ).mappings().one_or_none()
    if reusable:
        run_id = uuid4().hex
        now = datetime.now(UTC)
        session.execute(
            document_parse_runs.insert().values(
                id=run_id,
                course_id=course_id,
                material_version_id=material_version_id,
                parser_profile_id=parser_profile_id,
                reused_from_run_id=reusable["id"],
                status="ready",
                provider_run_id=reusable["provider_run_id"],
                trace_id=reusable["trace_id"],
                completed_at=now,
                updated_at=now,
            )
        )
        source_artifacts = session.execute(
            select(document_artifacts).where(
                document_artifacts.c.course_id == course_id,
                document_artifacts.c.document_parse_run_id == reusable["id"],
            )
        ).mappings().all()
        for artifact in source_artifacts:
            values = dict(artifact)
            values.update(id=uuid4().hex, document_parse_run_id=run_id)
            session.execute(document_artifacts.insert().values(**values))
        source_blocks = session.execute(
            select(content_blocks).where(
                content_blocks.c.course_id == course_id,
                content_blocks.c.document_parse_run_id == reusable["id"],
            )
        ).mappings().all()
        for block in source_blocks:
            values = dict(block)
            values.update(
                id=uuid4().hex,
                document_parse_run_id=run_id,
                material_version_id=material_version_id,
            )
            session.execute(content_blocks.insert().values(**values))
        return run_id, True
    run_id = uuid4().hex
    session.execute(
        document_parse_runs.insert().values(
            id=run_id,
            course_id=course_id,
            material_version_id=material_version_id,
            parser_profile_id=parser_profile_id,
            status="queued",
        )
    )
    return run_id, False


async def submit_parse_run(
    session: Session,
    parser: DocumentParser,
    *,
    course_id: str,
    run_id: str,
    request: ParseRequest,
) -> str:
    session.rollback()
    submission = await parser.submit(request)
    session.execute(
        update(document_parse_runs)
        .where(document_parse_runs.c.id == run_id, document_parse_runs.c.course_id == course_id, document_parse_runs.c.status == "queued")
        .values(status="submitted", provider_run_id=submission.provider_batch_id, updated_at=datetime.now(UTC))
    )
    session.commit()
    return submission.provider_batch_id


async def poll_parse_run(
    session: Session,
    parser: DocumentParser,
    storage: ArtifactStore,
    *,
    course_id: str,
    run_id: str,
) -> str:
    row = session.execute(
        select(document_parse_runs).where(document_parse_runs.c.id == run_id, document_parse_runs.c.course_id == course_id)
    ).mappings().one_or_none()
    if row is None or not row["provider_run_id"]:
        raise DocumentProtocolError("parse run is not submitted")
    provider_run_id = row["provider_run_id"]
    material_version_id = row["material_version_id"]
    session.rollback()
    progress = await parser.poll(provider_run_id)
    if progress.state == ParseState.FAILED:
        session.execute(
            update(document_parse_runs)
            .where(document_parse_runs.c.id == run_id, document_parse_runs.c.course_id == course_id)
            .values(
                status="failed",
                trace_id=progress.trace_id,
                error_code="provider_failed",
                error_summary=(progress.error_summary or "document provider failed")[:500],
                updated_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
        )
        session.commit()
        return "failed"
    if progress.state != ParseState.DONE:
        session.execute(
            update(document_parse_runs)
            .where(document_parse_runs.c.id == run_id, document_parse_runs.c.course_id == course_id)
            .values(status=progress.state.value, trace_id=progress.trace_id, updated_at=datetime.now(UTC))
        )
        session.commit()
        return progress.state.value

    artifact = await parser.fetch(provider_run_id)
    parsed = read_mineru_zip(artifact.content)
    stored_artifacts: dict[str, tuple[str, bytes]] = {"full_zip": ("application/zip", artifact.content)}
    for name, value in parsed.artifacts.items():
        artifact_type = _artifact_type(name)
        if artifact_type:
            stored_artifacts.setdefault(artifact_type, (_content_type(name), value))
    artifact_rows = []
    for artifact_type, (content_type, value) in stored_artifacts.items():
        key = f"courses/{course_id}/parse-runs/{run_id}/{artifact_type}"
        storage.put_bytes(key, value, content_type)
        artifact_rows.append(
            {
                "id": uuid4().hex,
                "course_id": course_id,
                "document_parse_run_id": run_id,
                "artifact_type": artifact_type,
                "storage_key": key,
                "content_hash": hashlib.sha256(value).hexdigest(),
                "size_bytes": len(value),
            }
        )
    now = datetime.now(UTC)
    with session.begin():
        session.execute(delete(content_blocks).where(content_blocks.c.document_parse_run_id == run_id, content_blocks.c.course_id == course_id))
        session.execute(delete(document_artifacts).where(document_artifacts.c.document_parse_run_id == run_id, document_artifacts.c.course_id == course_id))
        if artifact_rows:
            session.execute(document_artifacts.insert(), artifact_rows)
        if parsed.blocks:
            session.execute(
                content_blocks.insert(),
                [
                    {
                        "id": uuid4().hex,
                        "course_id": course_id,
                        "document_parse_run_id": run_id,
                        "material_version_id": material_version_id,
                        "block_index": block.block_index,
                        "block_type": block.block_type,
                        "text": block.text,
                        "markdown": block.markdown,
                        "latex": block.latex,
                        "page_index": block.page_index,
                        "bbox": block.bbox,
                        "heading_path": block.heading_path,
                        "asset_reference": block.asset_reference,
                        "reading_order": block.reading_order,
                        "content_hash": block.content_hash,
                    }
                    for block in parsed.blocks
                ],
            )
        session.execute(
            update(document_parse_runs)
            .where(document_parse_runs.c.id == run_id, document_parse_runs.c.course_id == course_id)
            .values(status="ready", trace_id=progress.trace_id, error_code=None, error_summary=None, updated_at=now, completed_at=now)
        )
    return "ready"


def read_mineru_zip(
    content: bytes,
    *,
    max_files: int = 5_000,
    max_uncompressed_bytes: int = 1_073_741_824,
    max_compression_ratio: float = 200,
) -> ParsedDocument:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocumentProtocolError("MinerU artifact is not a valid ZIP") from exc
    with archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) > max_files:
            raise DocumentProtocolError("MinerU ZIP contains too many files")
        total_size = 0
        artifacts: dict[str, bytes] = {}
        for item in members:
            name = _safe_member_name(item.filename)
            total_size += item.file_size
            if total_size > max_uncompressed_bytes:
                raise DocumentProtocolError("MinerU ZIP expands beyond the configured limit")
            ratio = item.file_size / max(item.compress_size, 1)
            if item.file_size > 1_048_576 and ratio > max_compression_ratio:
                raise DocumentProtocolError("MinerU ZIP has a suspicious compression ratio")
            artifacts[name] = archive.read(item)
    content_name = next((name for name in artifacts if name.endswith("content_list.json")), None)
    if content_name is None:
        raise DocumentProtocolError("MinerU ZIP does not contain content_list.json")
    try:
        payload = json.loads(artifacts[content_name].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DocumentProtocolError("content_list.json is invalid") from exc
    return ParsedDocument(blocks=normalize_content_list(payload), artifacts=artifacts)


def normalize_content_list(payload) -> list[ContentBlock]:
    if isinstance(payload, dict):
        payload = payload.get("content_list") or payload.get("content")
    if not isinstance(payload, list):
        raise DocumentProtocolError("content_list.json root must be an array")
    blocks: list[ContentBlock] = []
    headings: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        block_type = _normalize_block_type(item.get("type") or item.get("block_type"))
        text = _extract_text(item, block_type)
        if not text and block_type not in {"image"}:
            continue
        if block_type == "title":
            level = _heading_level(item)
            headings[level - 1 :] = [text]
        page = item.get("page_idx", item.get("page_index"))
        bbox = item.get("bbox")
        if not isinstance(bbox, list) or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in bbox):
            bbox = None
        asset = item.get("img_path") or item.get("image_path") or item.get("asset_reference")
        normalized = {
            "block_type": block_type,
            "text": text,
            "markdown": item.get("markdown") if isinstance(item.get("markdown"), str) else None,
            "latex": item.get("latex") if isinstance(item.get("latex"), str) else None,
            "page_index": page if isinstance(page, int) and page >= 0 else None,
            "bbox": bbox,
            "heading_path": list(headings),
            "asset_reference": asset if isinstance(asset, str) else None,
        }
        digest_payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        blocks.append(
            ContentBlock(
                block_index=len(blocks),
                reading_order=len(blocks),
                content_hash=hashlib.sha256(digest_payload.encode("utf-8")).hexdigest(),
                **normalized,
            )
        )
    return blocks


def _safe_member_name(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    has_drive_prefix = bool(path.parts and len(path.parts[0]) >= 2 and path.parts[0][1] == ":")
    if not normalized or path.is_absolute() or has_drive_prefix or ".." in path.parts or normalized.startswith("/"):
        raise DocumentProtocolError("MinerU ZIP contains an unsafe path")
    clean = posixpath.normpath(normalized)
    if clean in {".", ".."} or clean.startswith("../"):
        raise DocumentProtocolError("MinerU ZIP contains an unsafe path")
    return clean


def _normalize_block_type(value) -> str:
    raw = str(value or "text").lower().replace("-", "_")
    mapping = {
        "text": "paragraph",
        "paragraph": "paragraph",
        "title": "title",
        "heading": "title",
        "list": "list",
        "table": "table",
        "equation": "equation",
        "formula": "equation",
        "code": "code",
        "image": "image",
        "metadata": "metadata",
    }
    return mapping.get(raw, "paragraph")


def _extract_text(item: dict, block_type: str) -> str:
    for key in ("text", "content", "table_body", "caption"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if block_type == "equation" and isinstance(item.get("latex"), str):
        return item["latex"].strip()
    return ""


def _heading_level(item: dict) -> int:
    value = item.get("text_level", item.get("level", 1))
    return value if isinstance(value, int) and 1 <= value <= 6 else 1


def _artifact_type(name: str) -> str | None:
    basename = PurePosixPath(name).name.lower()
    if basename == "content_list.json":
        return "content_list"
    if basename == "full.md":
        return "full_markdown"
    if basename == "middle.json":
        return "middle_json"
    return None


def _content_type(name: str) -> str:
    return "application/json" if name.lower().endswith(".json") else "text/markdown; charset=utf-8"
