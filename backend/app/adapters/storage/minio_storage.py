"""S3-compatible object storage adapter; credentials remain server-side."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    content_type: str | None
    metadata: dict[str, str]
    etag: str


class StorageUnavailableError(Exception):
    """Storage operation failed without exposing provider details."""


class StoragePreconditionError(Exception):
    """The source object changed after it was validated."""


class StoragePort(Protocol):
    def presign_put(self, *, object_key: str, content_type: str, sha256: str, expires_in: int) -> str: ...

    def head_object(self, object_key: str) -> ObjectInfo | None: ...

    def stream_object(self, object_key: str) -> Iterator[bytes]: ...

    def finalize_object(self, source_key: str, destination_key: str, source_etag: str) -> None: ...


class MinioStorage:
    """A narrow boto3 adapter that also works with MinIO's S3 endpoint."""

    def __init__(self, *, endpoint: str, access_key: str, secret_key: str, bucket: str, region: str) -> None:
        import boto3
        from botocore.client import Config

        self.bucket = bucket
        self.region = region
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return
        except Exception as exc:
            error_code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if error_code not in {"404", "NoSuchBucket", "NotFound"}:
                raise StorageUnavailableError from exc
        create_args = {"Bucket": self.bucket}
        if self.region != "us-east-1":
            create_args["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
        try:
            self.client.create_bucket(**create_args)
        except Exception as exc:
            raise StorageUnavailableError from exc

    def presign_put(self, *, object_key: str, content_type: str, sha256: str, expires_in: int) -> str:
        return self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": object_key, "ContentType": content_type, "Metadata": {"sha256": sha256}},
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )

    def head_object(self, object_key: str) -> ObjectInfo | None:
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=object_key)
        except self.client.exceptions.NoSuchKey:
            return None
        except Exception as exc:
            error_code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if error_code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return ObjectInfo(
            size=int(response["ContentLength"]),
            content_type=response.get("ContentType"),
            metadata={str(key).lower(): str(value) for key, value in response.get("Metadata", {}).items()},
            etag=str(response["ETag"]),
        )

    def stream_object(self, object_key: str) -> Iterator[bytes]:
        response = self.client.get_object(Bucket=self.bucket, Key=object_key)
        body = response["Body"]
        try:
            while chunk := body.read(1024 * 1024):
                yield chunk
        finally:
            body.close()

    def finalize_object(self, source_key: str, destination_key: str, source_etag: str) -> None:
        try:
            self.client.copy_object(
                Bucket=self.bucket,
                Key=destination_key,
                CopySource={"Bucket": self.bucket, "Key": source_key},
                CopySourceIfMatch=source_etag,
                MetadataDirective="COPY",
            )
        except Exception as exc:
            error_code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if error_code in {"PreconditionFailed", "412"}:
                raise StoragePreconditionError from exc
            raise StorageUnavailableError from exc
