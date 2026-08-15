from __future__ import annotations

import math

import httpx
import pytest
from pydantic import ValidationError

from app.adapters.model.embedding_gateway import (
    EmbeddingGatewayError,
    OpenAICompatibleEmbeddingGateway,
)
from app.config import Settings
from app.domain.framework.exam_points import (
    ExamPoint,
    OperationalDetailPolicy,
    WeightSource,
)
from app.domain.knowledge.relevance import StagingChunk
from app.services.staging_retrieval_service import (
    HybridStagingRetriever,
    RetrievalConfigurationError,
    retrieve_for_exam_point,
)


def _exam_point(*, retrieval_intent: str = "检索链路、召回偏差及诊断依据") -> ExamPoint:
    return ExamPoint(
        code="EP-1",
        anchor_key="retrieval-diagnosis",
        title="检索偏差诊断",
        assessment_requirement="能够分析检索遗漏的原因并提出诊断依据",
        weight_value=10,
        weight_source=WeightSource.ASSESSMENT_SYLLABUS,
        weight_group_id="retrieval",
        operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
        retrieval_intent=retrieval_intent,
    )


class StaticEmbedder:
    def __init__(self, vectors: list[list[float]]):
        self.vectors = vectors
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return self.vectors


def test_retrieval_combines_semantic_and_lexical_scores_without_returning_noise():
    point = _exam_point()
    good = StagingChunk(
        id="good",
        material_version_id="material-1",
        content="RAG检索结果遗漏关键内容的原因",
        locator={"page": 2},
        embedding=[0.9, 0.1],
    )
    noise = StagingChunk(
        id="noise",
        material_version_id="material-1",
        content="安装CUDA并截图提交",
        embedding=[0.0, 1.0],
    )
    embedder = StaticEmbedder([[1.0, 0.0]])

    result = retrieve_for_exam_point(
        point,
        [good, noise],
        embedder,
        top_k=8,
        minimum_score=0.25,
    )

    assert [ranked.chunk.id for ranked in result] == ["good"]
    assert result[0].chunk is good
    assert result[0].semantic_score == pytest.approx(0.9938837)
    assert result[0].score == pytest.approx(
        0.35 * result[0].lexical_score + 0.65 * result[0].semantic_score
    )
    assert embedder.calls == [[point.retrieval_intent]]


def test_retrieval_returns_empty_when_all_chunks_are_below_threshold():
    embedder = StaticEmbedder([[1.0, 0.0]])
    chunk = StagingChunk(
        id="noise",
        material_version_id="material-1",
        content="安装环境",
        embedding=[0.0, 1.0],
    )

    result = retrieve_for_exam_point(
        _exam_point(),
        [chunk],
        embedder,
        top_k=8,
        minimum_score=0.25,
    )

    assert result == []


def test_retrieval_does_not_accept_lexically_related_chunk_without_semantic_support():
    point = _exam_point(retrieval_intent="检索链路召回偏差诊断")
    unsupported = StagingChunk(
        id="unsupported",
        material_version_id="material-2",
        content="检索链路软件安装说明",
        embedding=[0.0, 1.0],
    )
    embedder = StaticEmbedder([[1.0, 0.0]])

    result = retrieve_for_exam_point(
        point,
        [unsupported],
        embedder,
        top_k=8,
        minimum_score=0.25,
    )

    assert result == []


def test_retrieval_rejects_exact_lexical_match_with_orthogonal_semantics():
    point = _exam_point(retrieval_intent="完全相同的检索表述")
    exact_match = StagingChunk(
        id="lexical-only",
        material_version_id="material-2",
        content=point.retrieval_intent,
        embedding=[0.0, 1.0],
    )

    result = retrieve_for_exam_point(
        point,
        [exact_match],
        StaticEmbedder([[1.0, 0.0]]),
        top_k=8,
        minimum_score=0.25,
    )

    assert result == []


def test_retrieval_only_ranks_the_supplied_course_snapshot_chunks():
    supplied = StagingChunk(
        id="course-a-v2",
        material_version_id="course-a-material-v2",
        content="召回遗漏可由切块边界不合理引起",
        embedding=[1.0, 0.0],
    )
    embedder = StaticEmbedder([[1.0, 0.0]])

    result = HybridStagingRetriever(
        embedder=embedder,
        top_k=24,
        minimum_score=0.25,
    ).retrieve(_exam_point(), [supplied])

    assert len(result) == 1
    assert result[0].chunk is supplied
    assert embedder.calls == [[_exam_point().retrieval_intent]]


@pytest.mark.parametrize(
    ("vectors", "message"),
    [
        ([], "数量"),
        ([[1.0, 0.0], [1.0, 0.0]], "数量"),
        ([[]], "空向量"),
        ([[math.nan, 0.0]], "有限"),
        ([[0.0, 0.0]], "零范数"),
        ([[1.7e308, 1.7e308]], "有限范数"),
    ],
)
def test_retrieval_rejects_invalid_embedding_results(vectors, message):
    chunk = StagingChunk(
        id="chunk",
        material_version_id="material-1",
        content="检索结果",
        embedding=[1.0, 0.0],
    )

    with pytest.raises(RetrievalConfigurationError, match=message):
        retrieve_for_exam_point(
            _exam_point(),
            [chunk],
            StaticEmbedder(vectors),
            top_k=8,
            minimum_score=0.25,
        )


@pytest.mark.parametrize(
    ("embedding", "message"),
    [
        ([], "空向量"),
        ([math.nan, 0.0], "有限"),
        ([0.0, 0.0], "零范数"),
        ([1.7e308, 1.7e308], "有限范数"),
        ([1.0], "维度"),
    ],
)
def test_retrieval_rejects_invalid_persisted_chunk_embedding(embedding, message):
    chunk = StagingChunk(
        id="chunk",
        material_version_id="material-1",
        content="检索结果",
        embedding=embedding,
    )

    with pytest.raises(RetrievalConfigurationError, match=message):
        retrieve_for_exam_point(
            _exam_point(),
            [chunk],
            StaticEmbedder([[1.0, 0.0]]),
            top_k=8,
            minimum_score=0.25,
        )


@pytest.mark.parametrize(
    ("top_k", "minimum_score"),
    [(0, 0.25), (8, -0.01), (8, 1.01), (8, math.nan)],
)
def test_hybrid_retriever_rejects_unsafe_configuration(top_k, minimum_score):
    with pytest.raises(RetrievalConfigurationError):
        HybridStagingRetriever(
            embedder=StaticEmbedder([]),
            top_k=top_k,
            minimum_score=minimum_score,
        )


def test_equal_scores_use_material_version_and_chunk_id_as_stable_tiebreakers():
    point = _exam_point()
    preferred = StagingChunk(
        id="z-chunk",
        material_version_id="material-a",
        content="相同证据内容",
        embedding=[1.0, 0.0],
    )
    other = StagingChunk(
        id="a-chunk",
        material_version_id="material-b",
        content="相同证据内容",
        embedding=[1.0, 0.0],
    )

    first = retrieve_for_exam_point(
        point,
        [preferred, other],
        StaticEmbedder([[1.0, 0.0]]),
        top_k=1,
        minimum_score=0.25,
    )
    reversed_input = retrieve_for_exam_point(
        point,
        [other, preferred],
        StaticEmbedder([[1.0, 0.0]]),
        top_k=1,
        minimum_score=0.25,
    )

    assert [item.chunk.id for item in first] == ["z-chunk"]
    assert [item.chunk.id for item in reversed_input] == ["z-chunk"]


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
        "ORGANIZATION_RETRIEVAL_TOP_K",
        "ORGANIZATION_RETRIEVAL_MIN_SCORE",
        "ORGANIZATION_MAX_WORKERS",
    ):
        monkeypatch.delenv(variable, raising=False)

    settings = Settings()

    assert settings.embedding_base_url == "https://api.openai.com/v1"
    assert settings.embedding_api_key == ""
    assert settings.embedding_model == "qwen3.7-text-embedding"
    assert settings.organization_retrieval_top_k == 24
    assert settings.organization_retrieval_min_score == 0.25
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
