from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.client import Config
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from app.adapters.storage.minio_storage import MinioStorage, StoragePreconditionError, StorageUnavailableError


class FakeS3Client:
    class exceptions:
        NoSuchKey = RuntimeError

    def __init__(self):
        self.presign_args = None
        self.body = None

    def generate_presigned_url(self, operation, **kwargs):
        self.presign_args = (operation, kwargs)
        return "https://storage.invalid/signed-put"

    def head_object(self, **_kwargs):
        return {"ContentLength": 5, "ContentType": "application/pdf", "Metadata": {"SHA256": "abc"}, "ETag": '"etag"'}

    def get_object(self, **_kwargs):
        self.body = FakeBody([b"ab", b"cde"])
        return {"Body": self.body}


class FakeBody:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False

    def read(self, _size):
        return next(self.chunks, b"")

    def close(self):
        self.closed = True


def test_minio_adapter_uses_fake_s3_client_for_presign_head_and_stream_without_docker():
    client = FakeS3Client()
    storage = MinioStorage.__new__(MinioStorage)
    storage.bucket = "materials"
    storage.client = client

    url = storage.presign_put(object_key="courses/c/session/file.pdf", content_type="application/pdf", sha256="a" * 64, expires_in=300)
    info = storage.head_object("courses/c/session/file.pdf")
    streamed = b"".join(storage.stream_object("courses/c/session/file.pdf"))

    assert url == "https://storage.invalid/signed-put"
    assert client.presign_args[0] == "put_object"
    assert client.presign_args[1]["Params"]["Metadata"] == {"sha256": "a" * 64}
    assert info.size == 5
    assert info.metadata == {"sha256": "abc"}
    assert info.etag == '"etag"'
    assert streamed == b"abcde"
    assert client.body.closed is True


def test_presigned_put_uses_sigv4_and_path_style():
    client = boto3.client(
        "s3",
        endpoint_url="http://localhost:9000",
        aws_access_key_id="access",
        aws_secret_access_key="secret",
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    storage = MinioStorage.__new__(MinioStorage)
    storage.bucket = "materials"
    storage.client = client

    url = storage.presign_put(object_key="courses/c/uploads/s/file.pdf", content_type="application/pdf", sha256="a" * 64, expires_in=300)

    query = parse_qs(urlparse(url).query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert "X-Amz-Signature" in query
    assert urlparse(url).path.startswith("/materials/")


def test_finalize_uses_copy_source_etag_precondition():
    client = boto3.client(
        "s3", endpoint_url="http://localhost:9000", aws_access_key_id="access", aws_secret_access_key="secret", region_name="us-east-1"
    )
    expected = {
        "Bucket": "materials",
        "Key": "final/file.pdf",
        "CopySource": {"Bucket": "materials", "Key": "temp/file.pdf"},
        "CopySourceIfMatch": '"etag-1"',
        "MetadataDirective": "COPY",
    }
    with Stubber(client) as stubber:
        stubber.add_response("copy_object", {}, expected)
        storage = MinioStorage.__new__(MinioStorage)
        storage.bucket = "materials"
        storage.client = client
        storage.finalize_object("temp/file.pdf", "final/file.pdf", '"etag-1"')


def test_finalize_maps_etag_precondition_failure():
    client = boto3.client(
        "s3", endpoint_url="http://localhost:9000", aws_access_key_id="access", aws_secret_access_key="secret", region_name="us-east-1"
    )
    expected = {
        "Bucket": "materials",
        "Key": "final/file.pdf",
        "CopySource": {"Bucket": "materials", "Key": "temp/file.pdf"},
        "CopySourceIfMatch": '"etag-1"',
        "MetadataDirective": "COPY",
    }
    with Stubber(client) as stubber:
        stubber.add_client_error("copy_object", service_error_code="PreconditionFailed", http_status_code=412, expected_params=expected)
        storage = MinioStorage.__new__(MinioStorage)
        storage.bucket = "materials"
        storage.client = client
        with pytest.raises(StoragePreconditionError):
            storage.finalize_object("temp/file.pdf", "final/file.pdf", '"etag-1"')


def test_minio_initialization_uses_sigv4_path_style_and_creates_missing_bucket(monkeypatch):
    captured = {}

    class MissingBucketClient:
        def head_bucket(self, **kwargs):
            captured["head"] = kwargs
            raise ClientError({"Error": {"Code": "404", "Message": "missing"}}, "HeadBucket")

        def create_bucket(self, **kwargs):
            captured["create"] = kwargs

    def client_factory(_service, **kwargs):
        captured["config"] = kwargs["config"]
        return MissingBucketClient()

    monkeypatch.setattr(boto3, "client", client_factory)

    MinioStorage(endpoint="http://localhost:9000", access_key="access", secret_key="secret", bucket="materials", region="us-east-1")

    assert captured["config"].signature_version == "s3v4"
    assert captured["config"].s3["addressing_style"] == "path"
    assert captured["head"] == {"Bucket": "materials"}
    assert captured["create"] == {"Bucket": "materials"}


def test_minio_initialization_does_not_swallow_bucket_permission_errors(monkeypatch):
    class DeniedClient:
        def head_bucket(self, **_kwargs):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "HeadBucket")

    monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: DeniedClient())

    with pytest.raises(StorageUnavailableError):
        MinioStorage(endpoint="http://localhost:9000", access_key="access", secret_key="secret", bucket="materials", region="us-east-1")
