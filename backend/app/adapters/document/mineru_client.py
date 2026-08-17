"""MinerU precise parsing API adapter."""

from __future__ import annotations

import asyncio

import httpx

from app.adapters.document.protocol import (
    DocumentNetworkError,
    DocumentProtocolError,
    DocumentProviderError,
    ParseArtifact,
    ParseProgress,
    ParseRequest,
    ParseState,
    ParseSubmission,
)

_STATE_MAP = {
    "waiting-file": ParseState.WAITING_FILE,
    "waiting_file": ParseState.WAITING_FILE,
    "pending": ParseState.PENDING,
    "running": ParseState.RUNNING,
    "converting": ParseState.CONVERTING,
    "done": ParseState.DONE,
    "failed": ParseState.FAILED,
}


class MineruClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 60,
        max_attempts: int = 3,
        retry_delay_seconds: float = 0.25,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def submit(self, request: ParseRequest) -> ParseSubmission:
        payload = {
            "model_version": request.model_version,
            "files": [{"name": request.filename, "data_id": request.material_version_id}],
        }
        body = await self._request_json("POST", "/api/v4/file-urls/batch", json=payload)
        data = _success_data(body)
        batch_id = data.get("batch_id")
        urls = data.get("file_urls")
        if not isinstance(batch_id, str) or not isinstance(urls, list) or not urls or not isinstance(urls[0], str):
            raise DocumentProtocolError("MinerU submission response is missing batch_id or file_urls")
        await self._upload_with_retry(urls[0], request)
        return ParseSubmission(provider_batch_id=batch_id)

    async def poll(self, provider_batch_id: str) -> ParseProgress:
        body = await self._request_json("GET", f"/api/v4/extract-results/batch/{provider_batch_id}")
        item = _result_item(_success_data(body))
        raw_state = str(item.get("state", "")).lower()
        state = _STATE_MAP.get(raw_state)
        if state is None:
            raise DocumentProtocolError(f"unknown MinerU state: {raw_state or 'empty'}")
        return ParseProgress(
            state=state,
            data_id=_optional_string(item.get("data_id")),
            result_url=_optional_string(item.get("full_zip_url")),
            error_summary=_optional_string(item.get("err_msg") or item.get("error")),
            trace_id=_optional_string(body.get("trace_id") or item.get("trace_id")),
            raw_status=raw_state,
        )

    async def fetch(self, provider_batch_id: str) -> ParseArtifact:
        progress = await self.poll(provider_batch_id)
        if progress.state == ParseState.FAILED:
            raise DocumentProviderError(progress.error_summary or "MinerU parsing failed")
        if progress.state != ParseState.DONE or not progress.result_url:
            raise DocumentProtocolError("MinerU result is not ready")
        response = await self._request_with_retry("GET", progress.result_url, headers={})
        if not response.content:
            raise DocumentProtocolError("MinerU returned an empty artifact")
        return ParseArtifact(provider_batch_id=provider_batch_id, content=response.content, content_type=response.headers.get("content-type"))

    async def _request_json(self, method: str, path: str, **kwargs) -> dict:
        response = await self._request_with_retry(method, f"{self.base_url}{path}", headers=self._auth_headers, **kwargs)
        try:
            body = response.json()
        except ValueError as exc:
            raise DocumentProtocolError("MinerU returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise DocumentProtocolError("MinerU returned a non-object JSON response")
        return body

    async def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await self.client.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:
                last_error = exc
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                retryable = status is None or status == 429 or status >= 500
                if not retryable or attempt == self.max_attempts:
                    break
                await asyncio.sleep(self.retry_delay_seconds * (2 ** (attempt - 1)))
        raise DocumentNetworkError("MinerU network request failed") from last_error

    async def _upload_with_retry(self, url: str, request: ParseRequest) -> None:
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await self.client.put(
                    url,
                    content=request.content_factory(),
                    # MinerU signs the OSS URL without Content-Type. Adding it
                    # changes Aliyun OSS' canonical request and returns 403
                    # SignatureDoesNotMatch.
                    headers={},
                )
                response.raise_for_status()
                return
            except httpx.HTTPError as exc:
                last_error = exc
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                retryable = status is None or status == 429 or status >= 500
                if not retryable or attempt == self.max_attempts:
                    break
                await asyncio.sleep(self.retry_delay_seconds * (2 ** (attempt - 1)))
        raise DocumentNetworkError("MinerU upload failed") from last_error


def _success_data(body: dict) -> dict:
    if body.get("code") not in (0, "0", None):
        raise DocumentProviderError(str(body.get("msg") or "MinerU request failed")[:500])
    data = body.get("data")
    if not isinstance(data, dict):
        raise DocumentProtocolError("MinerU response is missing data")
    return data


def _result_item(data: dict) -> dict:
    results = data.get("extract_result") or data.get("extract_results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise DocumentProtocolError("MinerU response is missing extract_result")
    return results[0]


def _optional_string(value) -> str | None:
    return value if isinstance(value, str) and value else None
