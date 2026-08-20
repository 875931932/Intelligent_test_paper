"""Local filesystem storage adapter — fallback when MinIO/S3 is unavailable."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

from .minio_storage import ObjectInfo, StoragePort, StoragePreconditionError, StorageUnavailableError

# 本地存储根目录
_ROOT = Path(os.getenv("LOCAL_STORAGE_ROOT", "./uploads")).resolve()


class LocalStorage(StoragePort):
    """把文件存在本地磁盘，实现和 MinioStorage 相同的接口。"""

    def __init__(self) -> None:
        _ROOT.mkdir(parents=True, exist_ok=True)

    def _path(self, object_key: str) -> Path:
        # 防止路径穿越
        safe = object_key.replace("..", "").lstrip("/")
        p = (_ROOT / safe).resolve()
        if not str(p).startswith(str(_ROOT)):
            raise StoragePreconditionError("invalid object key")
        return p

    def presign_put(self, *, object_key: str, content_type: str, sha256: str, expires_in: int) -> str:
        # 返回一个本地 PUT 端点 URL；前端用 PUT 方法上传二进制
        return f"/api/v1/_local-storage/{object_key}"

    def _meta_path(self, object_key: str) -> Path:
        return self._path(object_key).with_suffix(self._path(object_key).suffix + ".meta")

    def head_object(self, object_key: str) -> ObjectInfo | None:
        p = self._path(object_key)
        if not p.exists():
            return None
        stat = p.stat()
        meta_p = self._meta_path(object_key)
        content_type = None
        if meta_p.exists():
            content_type = meta_p.read_text(encoding="utf-8").strip() or None
        return ObjectInfo(
            size=stat.st_size,
            content_type=content_type,
            metadata={},
            etag=hashlib.md5(p.read_bytes()).hexdigest(),
        )

    def stream_object(self, object_key: str) -> Iterator[bytes]:
        p = self._path(object_key)
        if not p.exists():
            raise StorageUnavailableError(f"object not found: {object_key}")
        with open(p, "rb") as f:
            while chunk := f.read(64 * 1024):
                yield chunk

    def finalize_object(self, source_key: str, destination_key: str, source_etag: str) -> None:
        src = self._path(source_key)
        dst = self._path(destination_key)
        if not src.exists():
            raise StorageUnavailableError(f"source object not found: {source_key}")
        actual_etag = hashlib.md5(src.read_bytes()).hexdigest()
        if actual_etag != source_etag:
            raise StoragePreconditionError("etag mismatch")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        # 复制 meta 文件
        src_meta = self._meta_path(source_key)
        if src_meta.exists():
            dst_meta = self._meta_path(destination_key)
            shutil.copy2(src_meta, dst_meta)

    def put_bytes(self, object_key: str, content: bytes, content_type: str) -> None:
        p = self._path(object_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        # 保存 content_type 到 .meta 文件
        meta_p = self._meta_path(object_key)
        meta_p.write_text(content_type or "", encoding="utf-8")
