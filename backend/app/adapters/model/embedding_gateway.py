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
        timeout: float = 60,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("embedding base URL is required")
        if not api_key.strip():
            raise ValueError("embedding API key is required")
        if not model.strip():
            raise ValueError("embedding model is required")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("embedding timeout must be positive and finite")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.client = client

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        try:
            response = self._post(texts)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise EmbeddingGatewayError(
                f"embedding request failed with HTTP status {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingGatewayError("embedding request failed") from exc

        try:
            payload = response.json()
            return self._parse_embeddings(payload, expected_count=len(texts))
        except EmbeddingGatewayError:
            raise
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise EmbeddingGatewayError(
                "embedding response was not valid JSON data"
            ) from exc

    def _post(self, texts: list[str]) -> httpx.Response:
        request = {
            "headers": {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            "json": {"model": self.model, "input": texts},
            "timeout": self.timeout,
        }
        if self.client is not None:
            return self.client.post(f"{self.base_url}/embeddings", **request)
        with httpx.Client(timeout=self.timeout) as client:
            return client.post(f"{self.base_url}/embeddings", **request)

    @staticmethod
    def _parse_embeddings(payload: Any, *, expected_count: int) -> list[list[float]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise EmbeddingGatewayError("embedding response is missing the data array")
        data = payload["data"]
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
            index = item.get("index")
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
            try:
                vector = [float(value) for value in raw_vector]
            except (TypeError, ValueError) as exc:
                raise EmbeddingGatewayError(
                    "embedding response contains a non-numeric vector"
                ) from exc
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
