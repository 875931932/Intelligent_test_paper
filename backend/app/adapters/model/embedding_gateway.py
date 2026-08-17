"""OpenAI-compatible embeddings adapter with fail-closed response validation."""

from __future__ import annotations

import math
from typing import Any

import httpx


class EmbeddingGatewayError(RuntimeError):
    """A redacted embedding request or response failure."""


class OpenAICompatibleEmbeddingGateway:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        api_format: str = "openai",
        max_batch_size: int | None = None,
        timeout: float = 60,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("embedding base URL is required")
        if not api_key.strip():
            raise ValueError("embedding API key is required")
        if not model.strip():
            raise ValueError("embedding model is required")
        if api_format not in {"openai", "dashscope"}:
            raise ValueError("embedding API format must be openai or dashscope")
        if max_batch_size is not None and max_batch_size <= 0:
            raise ValueError("embedding max batch size must be positive")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("embedding timeout must be positive and finite")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.api_format = api_format
        self.max_batch_size = max_batch_size or (
            20 if api_format == "dashscope" else 64
        )
        self.timeout = timeout
        self.client = client

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        result: list[list[float]] = []
        for start in range(0, len(texts), self.max_batch_size):
            result.extend(self._embed_batch(texts[start : start + self.max_batch_size]))
        return result

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:

        request_error: str | None = None
        try:
            response = self._post(texts)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            request_error = (
                f"embedding request failed with HTTP status {exc.response.status_code}"
            )
        except httpx.HTTPError:
            request_error = "embedding request failed"
        if request_error is not None:
            raise EmbeddingGatewayError(request_error) from None

        invalid_json = False
        try:
            payload = response.json()
        except (TypeError, ValueError, KeyError, IndexError):
            invalid_json = True
        if invalid_json:
            raise EmbeddingGatewayError(
                "embedding response was not valid JSON data"
            ) from None
        return self._parse_embeddings(
            payload,
            expected_count=len(texts),
            api_format=self.api_format,
        )

    def _post(self, texts: list[str]) -> httpx.Response:
        request = {
            "headers": {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            "timeout": self.timeout,
        }
        if self.api_format == "dashscope":
            url = self.base_url
            request["json"] = {"model": self.model, "input": {"texts": texts}}
        else:
            url = f"{self.base_url}/embeddings"
            request["json"] = {"model": self.model, "input": texts}
        if self.client is not None:
            return self.client.post(url, **request)
        with httpx.Client(timeout=self.timeout) as client:
            return client.post(url, **request)

    @staticmethod
    def _parse_embeddings(
        payload: Any,
        *,
        expected_count: int,
        api_format: str,
    ) -> list[list[float]]:
        if api_format == "dashscope":
            data = payload.get("output", {}).get("embeddings") if isinstance(payload, dict) else None
            index_key = "text_index"
        else:
            data = payload.get("data") if isinstance(payload, dict) else None
            index_key = "index"
        if not isinstance(data, list):
            raise EmbeddingGatewayError("embedding response is missing the data array")
        if len(data) != expected_count:
            raise EmbeddingGatewayError(
                "embedding response count does not match input count"
            )

        by_index: dict[int, list[float]] = {}
        dimension: int | None = None
        for item in data:
            if not isinstance(item, dict):
                raise EmbeddingGatewayError(
                    "embedding response contains an invalid item"
                )
            index = item.get(index_key)
            if isinstance(index, bool) or not isinstance(index, int):
                raise EmbeddingGatewayError(
                    "embedding response contains an invalid index"
                )
            if index in by_index:
                raise EmbeddingGatewayError(
                    "embedding response contains a duplicate index"
                )
            raw_vector = item.get("embedding")
            if not isinstance(raw_vector, list) or not raw_vector:
                raise EmbeddingGatewayError(
                    "embedding response contains an empty vector"
                )
            invalid_vector = False
            try:
                vector = [float(value) for value in raw_vector]
            except (TypeError, ValueError):
                invalid_vector = True
                vector = []
            if invalid_vector:
                raise EmbeddingGatewayError(
                    "embedding response contains a non-numeric vector"
                ) from None
            if not all(math.isfinite(value) for value in vector):
                raise EmbeddingGatewayError(
                    "embedding response contains a non-finite vector"
                )
            if dimension is None:
                dimension = len(vector)
            elif len(vector) != dimension:
                raise EmbeddingGatewayError(
                    "embedding response vector dimensions do not match"
                )
            by_index[index] = vector

        expected_indexes = set(range(expected_count))
        if set(by_index) != expected_indexes:
            raise EmbeddingGatewayError("embedding response indexes are incomplete")
        return [by_index[index] for index in range(expected_count)]
