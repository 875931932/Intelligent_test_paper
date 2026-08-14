from __future__ import annotations

import asyncio
import io
import zipfile

import httpx
import pytest

from app.adapters.document.mineru_client import MineruClient
from app.adapters.document.protocol import DocumentNetworkError, DocumentProtocolError


def _artifact() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("content_list.json", "[]")
    return buffer.getvalue()


def test_fetch_downloads_ready_zip_without_leaking_token():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "mineru.net":
            return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{"state": "done", "full_zip_url": "https://result.invalid/a.zip"}]}})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=_artifact(), headers={"content-type": "application/zip"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await MineruClient(base_url="https://mineru.net", token="secret", client=http).fetch("batch")

    assert asyncio.run(run()).content.startswith(b"PK")
    assert requests[0].headers["authorization"] == "Bearer secret"


@pytest.mark.parametrize("response", [httpx.Response(200, content=b""), httpx.Response(200, content=b"not-json")])
def test_empty_or_invalid_provider_result_is_not_success(response):
    def handler(request):
        return response

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await MineruClient(base_url="https://mineru.net", token="secret", client=http).poll("batch")

    with pytest.raises(DocumentProtocolError):
        asyncio.run(run())


def test_http_failure_maps_to_network_error():
    def handler(request):
        return httpx.Response(503, text="unavailable")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await MineruClient(base_url="https://mineru.net", token="secret", client=http).poll("batch")

    with pytest.raises(DocumentNetworkError):
        asyncio.run(run())


def test_temporary_http_failure_retries_then_succeeds():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, text="temporary")
        return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{"state": "running"}]}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = MineruClient(base_url="https://mineru.net", token="secret", client=http, retry_delay_seconds=0)
            return await client.poll("batch")

    assert asyncio.run(run()).state.value == "running"
    assert calls == 3
