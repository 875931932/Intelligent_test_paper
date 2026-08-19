"""Schemas and file policy for staged course materials."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

MATERIAL_TYPES = {"teaching_syllabus", "assessment_syllabus", "teaching_material", "exercise"}
MIME_BY_EXTENSION = {
    "pdf": {"application/pdf"},
    "doc": {"application/msword"},
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "ppt": {"application/vnd.ms-powerpoint"},
    "pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    "xls": {"application/vnd.ms-excel"},
    "xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    "txt": {"text/plain"},
    "md": {"text/markdown", "text/x-markdown"},
    "jpg": {"image/jpeg"},
    "jpeg": {"image/jpeg"},
    "png": {"image/png"},
    "gif": {"image/gif"},
    "webp": {"image/webp"},
    "bmp": {"image/bmp"},
}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class UploadSessionCreate(BaseModel):
    filename: str = Field(max_length=255)
    material_type: str
    size_bytes: int = Field(gt=0)
    sha256: str
    mime_type: str = Field(max_length=200)
    existing_material_id: str | None = None

    @field_validator("filename")
    @classmethod
    def valid_filename(cls, value: str) -> str:
        value = unicodedata.normalize("NFC", value).strip()
        if not value or "/" in value or "\\" in value or any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
            raise ValueError("filename is unsafe")
        if "." not in value or value.rsplit(".", 1)[1].lower() not in MIME_BY_EXTENSION:
            raise ValueError("file extension is not allowed")
        return value

    @field_validator("material_type")
    @classmethod
    def valid_material_type(cls, value: str) -> str:
        if value not in MATERIAL_TYPES:
            raise ValueError("unsupported material type")
        return value

    @field_validator("sha256")
    @classmethod
    def valid_sha256(cls, value: str) -> str:
        value = value.lower()
        if not SHA256_PATTERN.fullmatch(value):
            raise ValueError("sha256 must be a lowercase hexadecimal digest")
        return value

    @field_validator("mime_type")
    @classmethod
    def valid_mime_type(cls, value: str, info) -> str:
        filename = info.data.get("filename")
        if filename:
            extension = filename.rsplit(".", 1)[1].lower()
            if value not in MIME_BY_EXTENSION[extension]:
                raise ValueError("MIME type is not valid for file extension")
        return value


class UploadSessionResponse(BaseModel):
    session_id: str
    object_key: str
    upload_url: str
    expires_at: datetime
    headers: dict[str, str]


class MaterialVersionResponse(BaseModel):
    id: str
    material_id: str
    status: str
    version_no: int
    sha256: str
    mime_type: str
    size_bytes: int


class MaterialResponse(BaseModel):
    id: str
    course_id: str
    logical_name: str
    material_type: str
    status: str
    latest_version: MaterialVersionResponse | None = None
    # 最新版本的解析状态（{id,status,error_code,error_summary} 或 None=未解析）
    parse_status: dict | None = None
