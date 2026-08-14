from __future__ import annotations

import asyncio

import httpx
import pytest

from app.adapters.document.mineru_client import MineruClient
from app.adapters.document.protocol import DocumentProtocolError, ParseRequest, ParseState


async def _content():
    yield b"pdf"


def test_submit_uploads_without_authorization_and_uses_material_version_as_data_id():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path == "/api/v4/file-urls/batch":
            return httpx.Response(200, json={"code": 0, "data": {"batch_id": "batch-1", "file_urls": ["https://upload.invalid/file"]}})
        assert request.url.host == "upload.invalid"
        assert "authorization" not in request.headers
        return httpx.Response(200)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = MineruClient(base_url="https://mineru.net", token="secret", client=http)
            return await client.submit(ParseRequest("mv-1", "course.pdf", "application/pdf", _content))

    submission = asyncio.run(run())
    assert submission.provider_batch_id == "batch-1"
    assert b'"data_id":"mv-1"' in seen[0].content
    assert seen[0].headers["authorization"] == "Bearer secret"


@pytest.mark.parametrize(
    ("provider_state", "expected"),
    [
        ("waiting-file", ParseState.WAITING_FILE),
        ("pending", ParseState.PENDING),
        ("running", ParseState.RUNNING),
        ("converting", ParseState.CONVERTING),
        ("done", ParseState.DONE),
        ("failed", ParseState.FAILED),
    ],
)
def test_poll_maps_all_documented_states(provider_state, expected):
    def handler(request):
        return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{"state": provider_state, "data_id": "mv-1", "full_zip_url": "https://result.invalid/a.zip"}]}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await MineruClient(base_url="https://mineru.net", token="secret", client=http).poll("batch")

    assert asyncio.run(run()).state == expected


def test_poll_rejects_unknown_or_empty_result():
    def handler(request):
        return httpx.Response(200, json={"code": 0, "data": {"extract_result": []}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await MineruClient(base_url="https://mineru.net", token="secret", client=http).poll("batch")

    with pytest.raises(DocumentProtocolError):
        asyncio.run(run())
