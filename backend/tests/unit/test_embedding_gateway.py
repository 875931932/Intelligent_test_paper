from __future__ import annotations

import json
import math

import httpx
import pytest
from pydantic import ValidationError

from app.adapters.model.embedding_gateway import (
    EmbeddingGatewayError,
    OpenAICompatibleEmbeddingGateway,
)
from app.config import Settings


def test_embedding_gateway_sorts_response_by_index_and_returns_vectors():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = OpenAICompatibleEmbeddingGateway(
            base_url="https://embedding.invalid/v1/",
            api_key="secret-token",
            model="embedding-model",
            client=client,
        )
        result = gateway.embed(["查询", "证据"])

    assert result == [[0.1, 0.2], [0.3, 0.4]]
    assert requests[0].url == httpx.URL("https://embedding.invalid/v1/embeddings")
    assert requests[0].headers["Authorization"] == "Bearer secret-token"
    assert (
        requests[0].read()
        == b'{"model":"embedding-model","input":["\xe6\x9f\xa5\xe8\xaf\xa2","\xe8\xaf\x81\xe6\x8d\xae"]}'
    )


def test_embedding_gateway_supports_dashscope_text_embedding_contract():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "output": {
                    "embeddings": [
                        {"text_index": 1, "embedding": [0.3, 0.4]},
                        {"text_index": 0, "embedding": [0.1, 0.2]},
                    ]
                }
            },
        )

    endpoint = "https://embedding.invalid/api/v1/services/embeddings/text-embedding/text-embedding"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = OpenAICompatibleEmbeddingGateway(
            base_url=endpoint,
            api_key="secret-token",
            model="qwen3.7-text-embedding",
            api_format="dashscope",
            client=client,
        )
        result = gateway.embed(["查询", "证据"])

    assert result == [[0.1, 0.2], [0.3, 0.4]]
    assert requests[0].url == httpx.URL(endpoint)
    assert (
        requests[0].read()
        == b'{"model":"qwen3.7-text-embedding","input":{"texts":["\xe6\x9f\xa5\xe8\xaf\xa2","\xe8\xaf\x81\xe6\x8d\xae"]}}'
    )


def test_embedding_gateway_accepts_dashscope_index_field_contract():
    """现网 MaaS 返回 `index`（OpenAI 同构）而非旧契约 `text_index`（404 事故根因回归）。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "output": {
                    "embeddings": [
                        {"embedding": [0.3, 0.4], "index": 1, "type": "text"},
                        {"embedding": [0.1, 0.2], "index": 0, "type": "text"},
                    ]
                }
            },
        )

    endpoint = "https://embedding.invalid/api/v1/services/embeddings/text-embedding/text-embedding"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = OpenAICompatibleEmbeddingGateway(
            base_url=endpoint,
            api_key="secret-token",
            model="qwen3.7-text-embedding",
            api_format="dashscope",
            client=client,
        )
        result = gateway.embed(["查询", "证据"])

    # 乱序返回须按 index 重排，且两键并存时 text_index 优先（上一用例已锁定 text_index）。
    assert result == [[0.1, 0.2], [0.3, 0.4]]
    assert requests[0].url == httpx.URL(endpoint)


def test_embedding_gateway_prefers_text_index_when_both_index_keys_present():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": {
                    "embeddings": [
                        {"text_index": 0, "index": 7, "embedding": [0.1, 0.2]},
                    ]
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = OpenAICompatibleEmbeddingGateway(
            base_url="https://embedding.invalid/dashscope",
            api_key="secret-token",
            model="qwen3.7-text-embedding",
            api_format="dashscope",
            client=client,
        )
        assert gateway.embed(["查询"]) == [[0.1, 0.2]]


def test_embedding_gateway_rejects_dashscope_item_without_any_index_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"output": {"embeddings": [{"embedding": [0.1, 0.2]}]}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = OpenAICompatibleEmbeddingGateway(
            base_url="https://embedding.invalid/dashscope",
            api_key="secret-token",
            model="qwen3.7-text-embedding",
            api_format="dashscope",
            client=client,
        )
        with pytest.raises(EmbeddingGatewayError, match="invalid index"):
            gateway.embed(["查询"])


def test_dashscope_embedding_gateway_splits_requests_at_provider_limit():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        texts = json.loads(request.content)["input"]["texts"]
        return httpx.Response(
            200,
            json={
                "output": {
                    "embeddings": [
                        {"text_index": index, "embedding": [float(index + 1)]}
                        for index in range(len(texts))
                    ]
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = OpenAICompatibleEmbeddingGateway(
            base_url="https://embedding.invalid/dashscope",
            api_key="secret-token",
            model="qwen3.7-text-embedding",
            api_format="dashscope",
            client=client,
        )
        result = gateway.embed([f"text-{index}" for index in range(21)])

    assert [len(json.loads(request.content)["input"]["texts"]) for request in requests] == [20, 1]
    assert len(result) == 21


def test_embedding_gateway_returns_empty_without_sending_a_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("empty input must not trigger HTTP")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = OpenAICompatibleEmbeddingGateway(
            base_url="https://embedding.invalid/v1",
            api_key="secret-token",
            model="embedding-model",
            client=client,
        )
        assert gateway.embed([]) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
        {
            "data": [
                {"index": 0, "embedding": [0.1, 0.2]},
                {"index": 0, "embedding": [0.3, 0.4]},
            ]
        },
        {
            "data": [
                {"index": 0, "embedding": []},
                {"index": 1, "embedding": []},
            ]
        },
        {
            "data": [
                {"index": 0, "embedding": [0.1, 0.2]},
                {"index": 1, "embedding": [0.3]},
            ]
        },
    ],
)
def test_embedding_gateway_rejects_invalid_response_shapes(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = OpenAICompatibleEmbeddingGateway(
            base_url="https://embedding.invalid/v1",
            api_key="secret-token",
            model="embedding-model",
            client=client,
        )
        with pytest.raises(EmbeddingGatewayError):
            gateway.embed(["sensitive input one", "sensitive input two"])


@pytest.mark.parametrize("failure", ["http", "json"])
def test_embedding_gateway_redacts_credentials_and_input_from_errors(failure):
    api_key = "api-key-must-not-leak"
    full_input = "complete private teaching material must not leak"

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "http":
            return httpx.Response(503, text=f"{api_key}: {full_input}")
        return httpx.Response(200, text=f"not-json {api_key} {full_input}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = OpenAICompatibleEmbeddingGateway(
            base_url="https://embedding.invalid/v1",
            api_key=api_key,
            model="embedding-model",
            client=client,
        )
        with pytest.raises(EmbeddingGatewayError) as error:
            gateway.embed([full_input])

    assert api_key not in str(error.value)
    assert full_input not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_organization_retrieval_settings_have_safe_defaults(monkeypatch):
    for variable in (
        "EMBEDDING_BASE_URL",
        "EMBEDDING_API_KEY",
        "EMBEDDING_MODEL",
        "EMBEDDING_API_FORMAT",
        "ORGANIZATION_RETRIEVAL_TOP_K",
        "ORGANIZATION_RETRIEVAL_MIN_SCORE",
        "ORGANIZATION_MAX_WORKERS",
    ):
        monkeypatch.delenv(variable, raising=False)

    settings = Settings(_env_file=None)

    # embedding 的端点/模型名不设代码默认值：缺配置必须在使用处响亮失败。
    assert settings.embedding_base_url == ""
    assert settings.embedding_api_key == ""
    assert settings.embedding_model == ""
    assert settings.embedding_api_format == "openai"
    assert settings.organization_retrieval_top_k == 12
    # 陈述语料标定：0.40 等效语义门槛 0.62 超过可达 top1 中位数 0.60，
    # 半数考点结构性零召回；0.30 使零召回归零（详见 config 注释）。
    assert settings.organization_retrieval_min_score == 0.30
    assert settings.organization_max_workers == 16


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("ORGANIZATION_RETRIEVAL_TOP_K", "0"),
        ("ORGANIZATION_RETRIEVAL_MIN_SCORE", "-0.01"),
        ("ORGANIZATION_RETRIEVAL_MIN_SCORE", "1.01"),
        ("ORGANIZATION_MAX_WORKERS", "0"),
    ],
)
def test_organization_retrieval_settings_reject_invalid_environment_values(
    monkeypatch, variable, value
):
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError):
        Settings()


# ---------------------------------------------------------------------------
# 检索后端开关：langchain（默认）与 custom（回滚）等价
# ---------------------------------------------------------------------------


