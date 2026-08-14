from app.adapters.storage.minio_storage import MinioStorage


class FakeS3Client:
    class exceptions:
        NoSuchKey = RuntimeError

    def __init__(self):
        self.presign_args = None

    def generate_presigned_url(self, operation, **kwargs):
        self.presign_args = (operation, kwargs)
        return "https://storage.invalid/signed-put"

    def head_object(self, **_kwargs):
        return {"ContentLength": 5, "ContentType": "application/pdf", "Metadata": {"SHA256": "abc"}}

    def get_object(self, **_kwargs):
        return {"Body": FakeBody([b"ab", b"cde"])}


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
    assert streamed == b"abcde"
