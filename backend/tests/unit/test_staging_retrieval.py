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
from app.domain.framework.exam_points import (
    ExamPoint,
    OperationalDetailPolicy,
    WeightSource,
)
from app.domain.knowledge.relevance import StagingChunk
from app.services.staging_retrieval_service import (
    HybridStagingRetriever,
    RetrievalConfigurationError,
    lexical_overlap,
    lexical_rank_for_question,
    retrieve_for_exam_point,
    retrieve_for_question,
    retrieve_multi_for_question,
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
    # score 量化到 3 位小数（跨 run 确定性截断），lexical 分不变。
    assert result[0].score == pytest.approx(
        round(0.35 * result[0].lexical_score + 0.65 * result[0].semantic_score, 3)
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


def test_retrieve_multi_scores_lexical_term_with_each_query_text():
    """2026-10-06 回归：多路查询的词法项必须用各自查询原文打分。

    曾无论向量来自哪一路，词法项都锚在 retrieval_intent 上——考点名/考核要求
    展开查询只剩语义召回，短标题最精准的词面信号被整段丢弃。
    """
    point = _exam_point(retrieval_intent="提取该考点的考核内容、考核要求及对应知识点")
    topic = StagingChunk(
        id="topic",
        material_version_id="material-1",
        content="LangChain四大支柱包括基础能力层与运行时编排层。",
        embedding=[1.0, 0.0],
    )
    retriever = HybridStagingRetriever(
        embedder=StaticEmbedder([[1.0, 0.0], [0.0, 1.0]]),
        top_k=8,
        minimum_score=0.25,
    )
    title_query = "LangChain四大支柱与大模型硬伤解法"

    result = retriever.retrieve_multi(
        point,
        [topic],
        query_vectors=[[1.0, 0.0], [0.0, 1.0]],
        query_texts=[title_query, point.retrieval_intent],
    )

    assert [ranked.chunk.id for ranked in result] == ["topic"]
    assert result[0].lexical_score == pytest.approx(
        lexical_overlap(title_query, topic.content)
    )
    assert result[0].lexical_score > 0

    # 缺省 query_texts 退回检索意图（占位符 intent 与正文零词元交集 → 词法项 0）。
    fallback = retriever.retrieve_multi(point, [topic], query_vectors=[[1.0, 0.0]])
    assert fallback[0].lexical_score == pytest.approx(0.0)


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

    # 同分并列时决胜键为 (content_hash, chunk.id)：跨 run 顺序确定（缓存
    # prompt 稳定的前提），同内容下按 id 字典序，与输入顺序无关。
    assert [item.chunk.id for item in first] == ["a-chunk"]
    assert [item.chunk.id for item in reversed_input] == ["a-chunk"]


# ---------------------------------------------------------------------------
# 资料内容问答（助手 RAG）：裸问题串检索 + 词面降级
# ---------------------------------------------------------------------------


def _question_chunks() -> list[StagingChunk]:
    return [
        StagingChunk(
            id="q1",
            material_version_id="v1",
            content="监督学习分为分类与回归两大任务。",
            embedding=[1.0, 0.0],
        ),
        StagingChunk(
            id="q2",
            material_version_id="v1",
            content="聚类与降维属于无监督学习方法。",
            embedding=[0.6, 0.8],
        ),
    ]


def test_retrieve_for_question_ranks_by_similarity_and_is_deterministic():
    embedder = StaticEmbedder([[1.0, 0.0]])

    first = retrieve_for_question(
        "监督学习有哪些任务", _question_chunks(), embedder, top_k=2, minimum_score=0.05
    )
    second = retrieve_for_question(
        "监督学习有哪些任务", _question_chunks(), embedder, top_k=2, minimum_score=0.05
    )

    # 查询向量贴近 q1 → q1 首位；两次排序完全一致（量化 + 决胜键）
    assert [item.chunk.id for item in first] == ["q1", "q2"]
    assert [(item.chunk.id, item.score) for item in first] == [
        (item.chunk.id, item.score) for item in second
    ]
    # 问题串原样进嵌入调用（不经模型转述）
    assert embedder.calls[0] == ["监督学习有哪些任务"]
    assert all(item.semantic_score > 0 for item in first)


def test_retrieve_for_question_empty_chunks_skips_embedder():
    embedder = StaticEmbedder([])
    assert retrieve_for_question("任意问题", [], embedder, top_k=3, minimum_score=0.1) == []
    assert embedder.calls == []


def test_retrieve_for_question_wraps_embedder_failure():
    class FailingEmbedder:
        def embed(self, texts):
            raise RuntimeError("network down")

    with pytest.raises(RetrievalConfigurationError, match="检索已中止"):
        retrieve_for_question("问题", _question_chunks(), FailingEmbedder(), top_k=2, minimum_score=0.1)


def test_retrieve_for_question_rejects_unsafe_configuration():
    with pytest.raises(RetrievalConfigurationError):
        retrieve_for_question("问题", _question_chunks(), StaticEmbedder([]), top_k=0, minimum_score=0.1)
    with pytest.raises(RetrievalConfigurationError):
        lexical_rank_for_question("问题", _question_chunks(), top_k=1, minimum_score=1.5)


def test_retrieve_multi_for_question_merges_variants_and_collapses_template_duplicates():
    """双变体各取所长；跨文档同文模板碎片（表头/填空线）只保留得分最高的一份。"""
    chunks = [
        StagingChunk(
            id="a", material_version_id="v1",
            content="教学大纲的课程性质与地位。", embedding=[1.0, 0.0],
        ),
        StagingChunk(
            id="b", material_version_id="v1",
            content="学时分配与考核方式说明。", embedding=[0.0, 1.0],
        ),
        StagingChunk(
            id="dup1", material_version_id="v1",
            content="教学班：____________", embedding=[0.9, 0.9],
        ),
        StagingChunk(
            id="dup2", material_version_id="v2",
            content="教学班：__________", embedding=[0.9, 0.9],
        ),
    ]
    embedder = StaticEmbedder([[1.0, 0.0], [0.0, 1.0]])

    first = retrieve_multi_for_question(
        ["总结教学大纲讲了什么", "教学大纲"], chunks, embedder, top_k=6, minimum_score=0.05
    )
    second = retrieve_multi_for_question(
        ["总结教学大纲讲了什么", "教学大纲"], chunks, embedder, top_k=6, minimum_score=0.05
    )

    ids = [item.chunk.id for item in first]
    # 两个变体各自的主题块都进入合并结果
    assert {"a", "b"} <= set(ids)
    # 归一文本相同的模板碎片只留一份（下划线长度差异被折叠），且不含重复项
    assert len({"dup1", "dup2"} & set(ids)) == 1
    assert len(ids) == len(set(ids)) == 3
    # 变体一次批量嵌入（每组打分复用查询向量），两次调用完全一致
    assert embedder.calls[0] == ["总结教学大纲讲了什么", "教学大纲"]
    assert len(embedder.calls) == 2
    assert [item.chunk.id for item in first] == [item.chunk.id for item in second]
    assert all(item.score >= 0.05 for item in first)


def test_retrieve_multi_for_question_single_variant_delegates_to_single_path():
    """无变体可分（去重/过滤后只剩一条）→ 与既有单查询路径行为一致。"""
    embedder = StaticEmbedder([[1.0, 0.0]])
    multi = retrieve_multi_for_question(
        ["监督学习有哪些任务"], _question_chunks(), embedder, top_k=2, minimum_score=0.05
    )
    direct = retrieve_for_question(
        "监督学习有哪些任务", _question_chunks(), StaticEmbedder([[1.0, 0.0]]),
        top_k=2, minimum_score=0.05,
    )

    assert [item.chunk.id for item in multi] == [item.chunk.id for item in direct]
    assert embedder.calls == [["监督学习有哪些任务"]]


def test_retrieve_multi_for_question_skips_blank_variants_and_empty_chunks():
    embedder = StaticEmbedder([[1.0, 0.0]])

    assert retrieve_multi_for_question(
        ["", "   "], _question_chunks(), embedder, top_k=2, minimum_score=0.05
    ) == []
    assert retrieve_multi_for_question(
        ["问题", "题"], [], embedder, top_k=2, minimum_score=0.05
    ) == []
    assert embedder.calls == []


def test_retrieve_multi_for_question_wraps_embedder_failure():
    class FailingEmbedder:
        def embed(self, texts):
            raise RuntimeError("network down")

    with pytest.raises(RetrievalConfigurationError, match="检索已中止"):
        retrieve_multi_for_question(
            ["问题", "题"], _question_chunks(), FailingEmbedder(), top_k=2, minimum_score=0.1
        )


def test_retrieve_multi_for_question_rejects_unsafe_configuration():
    with pytest.raises(RetrievalConfigurationError):
        retrieve_multi_for_question(
            ["问题", "题"], _question_chunks(), StaticEmbedder([]), top_k=0, minimum_score=0.1
        )


def test_retrieve_multi_uses_precomputed_semantic_scores_without_embedder():
    """SQL 下推分：给定即完全不调嵌入，打分/量化/决胜与 Python cosine 路径同款。"""
    chunks = _question_chunks()

    # 等值对照：预计算分取 cosine([1,0], chunk) 的精确值（q1=1.0、q2=0.6）
    scores = [{"q1": 1.0, "q2": 0.6}, {"q1": 1.0, "q2": 0.6}]
    via_scores = retrieve_multi_for_question(
        ["监督学习有哪些任务", "监督学习"], chunks, None,
        top_k=4, minimum_score=0.0, semantic_scores=scores,
    )
    via_vectors = retrieve_multi_for_question(
        ["监督学习有哪些任务", "监督学习"], chunks, StaticEmbedder([[1.0, 0.0], [1.0, 0.0]]),
        top_k=4, minimum_score=0.0,
    )

    assert via_scores  # 双路径都召回
    assert [
        (item.chunk.id, item.score, item.lexical_score, item.semantic_score)
        for item in via_scores
    ] == [
        (item.chunk.id, item.score, item.lexical_score, item.semantic_score)
        for item in via_vectors
    ]
    assert via_scores[0].chunk.id == "q1"


def test_retrieve_multi_single_variant_uses_scores_without_embedder():
    chunks = _question_chunks()
    direct = retrieve_for_question(
        "监督学习有哪些任务", chunks, None,
        top_k=2, minimum_score=0.0, semantic_scores={"q1": 1.0, "q2": 0.6},
    )
    via_multi = retrieve_multi_for_question(
        ["监督学习有哪些任务"], chunks, None,
        top_k=2, minimum_score=0.0, semantic_scores=[{"q1": 1.0, "q2": 0.6}],
    )
    assert [item.chunk.id for item in via_multi] == [item.chunk.id for item in direct]
    assert direct and direct[0].chunk.id == "q1"


def test_retrieve_multi_rejects_scores_variant_count_mismatch():
    """预计算分与去重后变体数量不一致是编程错误，立即抛配置错误（上层降级词面）。"""
    with pytest.raises(RetrievalConfigurationError, match="语义分与查询变体数量不一致"):
        retrieve_multi_for_question(
            ["问题", "题"], _question_chunks(), None,
            top_k=2, minimum_score=0.05, semantic_scores=[{"q1": 1.0}],
        )


def test_lexical_rank_for_question_orders_by_overlap_without_embedder():
    chunks = [
        StagingChunk(id="a", material_version_id="v1", content="监督学习分为分类与回归。"),
        StagingChunk(id="b", material_version_id="v1", content="实验使用 QLoRA 微调模型。"),
    ]

    ranked = lexical_rank_for_question("监督学习的分类", chunks, top_k=2, minimum_score=0.1)

    assert ranked[0].chunk.id == "a"  # 词面重合度高的块在前
    assert all(item.semantic_score == 0.0 for item in ranked)  # 词面路径不碰语义
    assert all(item.score >= 0.1 for item in ranked)  # 低于阈值的块被过滤
    assert all(item.chunk.id != "b" for item in ranked) or ranked[-1].score >= 0.1


def test_lexical_rank_for_question_applies_top_k_deterministically():
    chunks = [
        StagingChunk(id="c1", material_version_id="v1", content="监督学习"),
        StagingChunk(id="c2", material_version_id="v1", content="监督学习"),
        StagingChunk(id="c3", material_version_id="v1", content="完全无关内容"),
    ]

    top = lexical_rank_for_question("监督学习", chunks, top_k=1, minimum_score=0.0)
    full = lexical_rank_for_question("监督学习", chunks, top_k=3, minimum_score=0.0)

    # top_k 截断 + 同分并列按 (content_hash, id) 决胜：与输入顺序无关
    assert len(top) == 1
    reversed_order = lexical_rank_for_question(
        "监督学习", list(reversed(chunks)), top_k=1, minimum_score=0.0
    )
    assert [item.chunk.id for item in reversed_order] == [item.chunk.id for item in top]
    assert len(full) == 3


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
