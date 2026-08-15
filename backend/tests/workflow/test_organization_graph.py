from __future__ import annotations

from time import sleep

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.domain.framework.exam_points import ExamPoint, OperationalDetailPolicy, WeightSource
from app.domain.knowledge.models import AssessmentUnitDraft, KnowledgeCardDraft
from app.domain.knowledge.relevance import (
    AssessmentUnitCandidate,
    ContentKind,
    EvidenceDecision,
    ExamPointFileDecision,
    KnowledgeCardCandidate,
    RelevanceClass,
    StagingChunk,
)
from app.services.staging_retrieval_service import HybridStagingRetriever, RankedChunk
from app.config import settings
from app.workflows.organization_graph import build_organization_graph


def _point(code: str, anchor: str) -> ExamPoint:
    return ExamPoint(
        code=code,
        anchor_key=anchor,
        title=f"考点{code}",
        assessment_requirement=f"理解并应用{code}",
        weight_value=50,
        weight_source=WeightSource.ASSESSMENT_SYLLABUS,
        weight_group_id=anchor,
        cognitive_targets=["understand", "apply"],
        allowed_question_types=["single_choice", "short_answer"],
        operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
        required_evidence_roles=["answer_or_rubric_basis"],
        retrieval_intent=f"检索{code}的定义、原理和评分依据",
    )


def _decision(point: ExamPoint, chunk: StagingChunk) -> EvidenceDecision:
    fact = f"{point.code}的可评分事实"
    return EvidenceDecision(
        exam_point_code=point.code,
        evidence_chunk_id=chunk.id,
        relevance_class=RelevanceClass.DIRECT,
        support_claim=fact,
        evidence_role="answer_or_rubric_basis",
        content_kind=ContentKind.FACT,
        candidate_assessment_unit=AssessmentUnitCandidate(
            code=f"unit-{point.code}",
            title=f"理解{point.code}",
            performance_statement=f"能够理解{point.code}",
        ),
        candidate_card_content=KnowledgeCardCandidate(
            name=f"{point.code}核心事实",
            performance_statement=f"能够说明{point.code}核心事实",
            assessable_content=[fact],
        ),
        confidence=95,
    )


class PairSelectingRetriever:
    """EP-1 recalls two files; EP-2 recalls only material-2."""

    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []

    def retrieve(self, exam_point, chunks):
        self.calls.append((exam_point.code, [chunk.id for chunk in chunks]))
        allowed = {"EP-1": {"chunk-1", "chunk-2"}, "EP-2": {"chunk-3"}}[exam_point.code]
        return [
            RankedChunk(chunk=chunk, score=0.9, lexical_score=0.8, semantic_score=0.95)
            for chunk in chunks
            if chunk.id in allowed
        ]


class RecordingClassifier:
    def __init__(self, *, fail_pair: tuple[str, str] | None = None):
        self.calls: list[tuple[str, str, list[str]]] = []
        self.fail_pair = fail_pair

    def classify(self, *, exam_point, material_version_id, chunks, call_context=None):
        self.calls.append((exam_point.code, material_version_id, [chunk.id for chunk in chunks]))
        assert all(chunk.material_version_id == material_version_id for chunk in chunks)
        assert call_context.course_id == "course-1"
        assert call_context.organization_run_id == "organization-run-1"
        assert call_context.stage == "classify_exam_point_file_pair"
        if self.fail_pair == (exam_point.code, material_version_id):
            raise RuntimeError("Bearer secret-token must not leak")
        if material_version_id == "material-1":
            sleep(0.01)
        return ExamPointFileDecision(
            exam_point_code=exam_point.code,
            material_version_id=material_version_id,
            decisions=[_decision(exam_point, chunk) for chunk in chunks],
        )


class RecordingConsolidator:
    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []

    def consolidate(self, *, exam_point, admitted_decisions, call_context=None):
        ids = sorted(decision.evidence_chunk_id for decision in admitted_decisions)
        self.calls.append((exam_point.code, ids))
        assert call_context.stage == "consolidate_exam_point"
        if not ids:
            return []
        fact = f"{exam_point.code}的可评分事实"
        return [
            AssessmentUnitDraft(
                code=f"unit-{exam_point.code}",
                title=f"理解{exam_point.code}",
                performance_statement=f"能够理解{exam_point.code}",
                exam_point_code=exam_point.code,
                cards=[
                    KnowledgeCardDraft(
                        name=f"{exam_point.code}核心事实",
                        performance_statement=f"能够说明{exam_point.code}核心事实",
                        assessable_content=[fact],
                        evidence_chunk_ids=ids,
                    )
                ],
            )
        ]


class RecordingKnowledgeRepository:
    def __init__(self, chunks):
        self.chunks = {chunk.id: chunk for chunk in chunks}
        self.candidate = None
        self.persisted_state = None
        self.published = None

    def load_evidence_chunks(self, *, course_id, run_id, evidence_chunk_ids):
        assert course_id == "course-1"
        assert run_id == "organization-run-1"
        return [self.chunks[chunk_id] for chunk_id in evidence_chunk_ids]

    def persist_candidate(self, state, tree):
        self.candidate = tree
        self.persisted_state = state
        return "catalog-candidate-1"

    def publish(self, state, tree, confirmation):
        self.published = (tree, confirmation)
        return {"catalog_version_id": "catalog-v1", "index_version_id": "index-v1"}


def _chunks():
    return [
        StagingChunk(id="chunk-1", material_version_id="material-1", content="EP-1事实"),
        StagingChunk(id="chunk-2", material_version_id="material-2", content="EP-1同义事实"),
        StagingChunk(id="chunk-3", material_version_id="material-2", content="EP-2事实"),
        StagingChunk(id="chunk-4", material_version_id="material-1", content="无关内容"),
    ]


def _state(chunks=None):
    chunks = chunks or _chunks()
    return {
        "course_id": "course-1",
        "run_id": "organization-run-1",
        "framework_version_id": "framework-v1",
        "framework_anchors": [
            {"key": "rag", "title": "RAG", "exam_weight": 60},
            {"key": "agent", "title": "Agent", "exam_weight": 40},
        ],
        "exam_points": [
            _point("EP-1", "rag").model_dump(mode="json"),
            _point("EP-2", "agent").model_dump(mode="json"),
        ],
        "material_version_ids": ["material-1", "material-2"],
        "evidence_chunk_ids": [chunk.id for chunk in chunks],
        "frozen_input": {
            "organization_schema_version": 2,
            "framework_version_id": "framework-v1",
            "exam_points": [
                {"id": "db-ep-1", "code": "EP-1"},
                {"id": "db-ep-2", "code": "EP-2"},
            ],
            "material_version_ids": ["material-1", "material-2"],
        },
    }


def _graph(*, classifier=None, retriever=None, consolidator=None, chunks=None):
    chunks = chunks or _chunks()
    repository = RecordingKnowledgeRepository(chunks)
    retriever = retriever or PairSelectingRetriever()
    classifier = classifier or RecordingClassifier()
    consolidator = consolidator or RecordingConsolidator()
    graph = build_organization_graph(
        retriever,
        classifier,
        consolidator,
        repository,
        checkpointer=InMemorySaver(),
    )
    return graph, retriever, classifier, consolidator, repository


def test_organization_graph_classifies_only_recalled_exam_point_file_pairs():
    graph, retriever, classifier, consolidator, repository = _graph()

    paused = graph.invoke(_state(), config={"configurable": {"thread_id": "per-pair"}})

    assert len(retriever.calls) == 4
    assert all(
        len({repository.chunks[chunk_id].material_version_id for chunk_id in chunk_ids}) == 1
        for _, chunk_ids in retriever.calls
    )
    assert sorted((point, material) for point, material, _ in classifier.calls) == [
        ("EP-1", "material-1"),
        ("EP-1", "material-2"),
        ("EP-2", "material-2"),
    ]
    assert all(len(chunk_ids) == 1 for _, _, chunk_ids in classifier.calls)
    assert sorted(consolidator.calls) == [
        ("EP-1", ["chunk-1", "chunk-2"]),
        ("EP-2", ["chunk-3"]),
    ]
    ep1_card = next(
        unit.cards[0]
        for topic in repository.candidate.topics
        for unit in topic.units
        if unit.exam_point_code == "EP-1"
    )
    assert ep1_card.evidence_chunk_ids == ["chunk-1", "chunk-2"]
    assert "__interrupt__" in paused


def test_no_recall_marks_exam_point_insufficient_without_classifier_call():
    class OnlyFirstPointRetriever(PairSelectingRetriever):
        def retrieve(self, exam_point, chunks):
            if exam_point.code == "EP-2":
                self.calls.append((exam_point.code, [chunk.id for chunk in chunks]))
                return []
            return super().retrieve(exam_point, chunks)

    graph, _, classifier, _, repository = _graph(retriever=OnlyFirstPointRetriever())

    graph.invoke(_state(), config={"configurable": {"thread_id": "no-recall"}})

    assert all(call[0] != "EP-2" for call in classifier.calls)
    coverage = {item.exam_point_code: item for item in repository.candidate.coverage}
    assert coverage["EP-2"].status == "insufficient"
    assert "no_recalled_evidence" in coverage["EP-2"].reasons


def test_one_pair_failure_is_redacted_and_does_not_repeat_or_block_other_pairs():
    classifier = RecordingClassifier(fail_pair=("EP-1", "material-1"))
    graph, _, _, _, repository = _graph(classifier=classifier)

    graph.invoke(_state(), config={"configurable": {"thread_id": "pair-failure"}})

    assert sorted((point, material) for point, material, _ in classifier.calls) == [
        ("EP-1", "material-1"),
        ("EP-1", "material-2"),
        ("EP-2", "material-2"),
    ]
    assert len(repository.persisted_state["failed_pairs"]) == 1
    failure = repository.persisted_state["failed_pairs"][0]
    assert failure["error_code"] == "classification_failed"
    assert "secret-token" not in failure["error_message"]
    assert {item.exam_point_code for item in repository.candidate.coverage} == {"EP-1", "EP-2"}


def test_each_exam_point_material_pair_gets_its_own_top_k_budget(monkeypatch):
    class SameFileRetriever(PairSelectingRetriever):
        def retrieve(self, exam_point, chunks):
            self.calls.append((exam_point.code, [chunk.id for chunk in chunks]))
            if exam_point.code == "EP-2":
                return []
            return [
                RankedChunk(chunk=chunk, score=0.9, lexical_score=0.8, semantic_score=0.95)
                for chunk in chunks
            ]

    monkeypatch.setattr(settings, "organization_retrieval_top_k", 1)
    chunks = [
        StagingChunk(id="m1-a", material_version_id="material-1", content="A"),
        StagingChunk(id="m1-b", material_version_id="material-1", content="B"),
        StagingChunk(id="m2-a", material_version_id="material-2", content="C"),
        StagingChunk(id="m2-b", material_version_id="material-2", content="D"),
    ]
    graph, retriever, classifier, _, _ = _graph(
        retriever=SameFileRetriever(), chunks=chunks
    )

    graph.invoke(
        _state(chunks), config={"configurable": {"thread_id": "pair-top-k"}}
    )

    assert sorted((point, material, ids) for point, material, ids in classifier.calls) == [
        ("EP-1", "material-1", ["m1-a"]),
        ("EP-1", "material-2", ["m2-a"]),
    ]
    assert sorted((point, ids) for point, ids in retriever.calls) == [
        ("EP-1", ["m1-a", "m1-b"]),
        ("EP-1", ["m2-a", "m2-b"]),
        ("EP-2", ["m1-a", "m1-b"]),
        ("EP-2", ["m2-a", "m2-b"]),
    ]


def test_graph_embeds_each_exam_point_query_once_and_reuses_frozen_chunk_vectors():
    class QueryOnlyEmbedder:
        def __init__(self):
            self.calls: list[list[str]] = []

        def embed(self, texts):
            self.calls.append(list(texts))
            assert len(texts) == 1
            return [[1.0, 0.0]]

    chunks = [
        StagingChunk(
            id="m1",
            material_version_id="material-1",
            content="材料一",
            embedding=[1.0, 0.0],
        ),
        StagingChunk(
            id="m2",
            material_version_id="material-2",
            content="材料二",
            embedding=[1.0, 0.0],
        ),
    ]
    embedder = QueryOnlyEmbedder()
    retriever = HybridStagingRetriever(
        embedder=embedder,
        top_k=24,
        minimum_score=0.0,
    )
    graph, _, classifier, _, _ = _graph(retriever=retriever, chunks=chunks)

    graph.invoke(
        _state(chunks),
        config={"configurable": {"thread_id": "reuse-frozen-embeddings"}},
    )

    assert embedder.calls == [
        [_point("EP-1", "rag").retrieval_intent],
        [_point("EP-2", "agent").retrieval_intent],
    ]
    assert sorted((point, material) for point, material, _ in classifier.calls) == [
        ("EP-1", "material-1"),
        ("EP-1", "material-2"),
        ("EP-2", "material-1"),
        ("EP-2", "material-2"),
    ]


class AdapterOutputError(RuntimeError):
    def __init__(self, error_code, message="api_key=secret-value malformed output"):
        super().__init__(message)
        self.error_code = error_code


@pytest.mark.parametrize(
    "error_code",
    ["model_json_missing_field", "model_non_json_response", "model_empty_response"],
)
def test_classifier_failure_preserves_safe_adapter_error_code(error_code):
    class FailingClassifier(RecordingClassifier):
        def classify(self, *, exam_point, material_version_id, chunks, call_context=None):
            if (exam_point.code, material_version_id) == ("EP-1", "material-1"):
                raise AdapterOutputError(error_code)
            return super().classify(
                exam_point=exam_point,
                material_version_id=material_version_id,
                chunks=chunks,
                call_context=call_context,
            )

    graph, _, _, _, repository = _graph(classifier=FailingClassifier())

    graph.invoke(_state(), config={"configurable": {"thread_id": error_code}})

    failure = repository.persisted_state["failed_pairs"][0]
    assert failure["error_code"] == error_code
    assert "secret-value" not in failure["error_message"]


@pytest.mark.parametrize(
    ("message", "secrets"),
    [
        (
            "request failed Authorization: Bearer auth-secret-123",
            ["auth-secret-123"],
        ),
        (
            "Basic basic-secret-without-prefix",
            ["basic-secret-without-prefix"],
        ),
        (
            'Digest username="digest-user", response="digest-secret-without-prefix"',
            ["digest-user", "digest-secret-without-prefix"],
        ),
        (
            "AWS4-HMAC-SHA256 Credential=aws-access-without-prefix, "
            "Signature=aws-signature-without-prefix",
            ["aws-access-without-prefix", "aws-signature-without-prefix"],
        ),
        (
            "Token token-scheme-secret",
            ["token-scheme-secret"],
        ),
        (
            "ApiKey api-key-scheme-secret",
            ["api-key-scheme-secret"],
        ),
        (
            "X-API-Key: provider-api-secret\n"
            "X-Amz-Security-Token: provider-session-secret\n"
            "Ocp-Apim-Subscription-Key: provider-subscription-secret",
            [
                "provider-api-secret",
                "provider-session-secret",
                "provider-subscription-secret",
            ],
        ),
        ('model returned {"api_key":"json-secret-456"}', ["json-secret-456"]),
        ("password='single-secret-789'", ["single-secret-789"]),
        (
            "token=token-secret secret=raw-secret client_secret='client-secret'",
            ["token-secret", "raw-secret", "client-secret"],
        ),
        (
            'model returned {"password":"secret with space"}',
            ["secret with space"],
        ),
        (
            'model returned {"password":"secret,with;punctuation"}',
            ["secret,with;punctuation"],
        ),
        (
            r'model returned {"password":"abc\"def"}',
            ["abc", "def"],
        ),
        (
            "authorization=Basic basic-secret-123",
            ["basic-secret-123"],
        ),
        (
            '{"authorization":"Basic basic-secret-456"}',
            ["basic-secret-456"],
        ),
        (
            'Authorization: Digest username="user", response="digest-secret"',
            ["digest-secret"],
        ),
        (
            "Authorization: AWS4-HMAC-SHA256 Credential=access-key, "
            "Signature=aws-secret",
            ["access-key", "aws-secret"],
        ),
        (
            "authorization=Custom first-secret second-secret",
            ["first-secret", "second-secret"],
        ),
        (
            '{"api_key":"unterminated-secret',
            ["unterminated-secret"],
        ),
        (
            "password='unterminated-secret",
            ["unterminated-secret"],
        ),
        (
            '{"api_key":"dangling-secret\\',
            ["dangling-secret"],
        ),
        (
            "password='dangling-secret\\",
            ["dangling-secret"],
        ),
        (
            "password=secret with space",
            ["secret", "with space"],
        ),
        (
            "api-token=api-token-secret access_token=access-secret "
            "passwd=passwd-secret",
            ["api-token-secret", "access-secret", "passwd-secret"],
        ),
        (
            "x" * 480 + ' "api_key":"late-secret-value" ' + "z" * 100,
            ["late-secret-value"],
        ),
    ],
)
def test_failed_pair_message_redacts_credentials_before_truncation(message, secrets):
    class FailingClassifier(RecordingClassifier):
        def classify(self, *, exam_point, material_version_id, chunks, call_context=None):
            if (exam_point.code, material_version_id) == ("EP-1", "material-1"):
                raise AdapterOutputError("model_output_invalid", message)
            return super().classify(
                exam_point=exam_point,
                material_version_id=material_version_id,
                chunks=chunks,
                call_context=call_context,
            )

    graph, _, _, _, repository = _graph(classifier=FailingClassifier())

    paused = graph.invoke(
        _state(),
        config={
            "configurable": {
                "thread_id": f"redact-{len(message)}-{sum(map(len, secrets))}"
            }
        },
    )

    redacted = repository.persisted_state["failed_pairs"][0]["error_message"]
    assert len(redacted) <= 500
    assert all(secret not in redacted for secret in secrets)
    assert all(secret not in str(paused["__interrupt__"]) for secret in secrets)


@pytest.mark.parametrize("bad_output", ["cross_point", "duplicate"])
def test_bad_classifier_output_isolated_to_its_exam_point_file_pair(bad_output):
    class BadClassifier(RecordingClassifier):
        def classify(self, *, exam_point, material_version_id, chunks, call_context=None):
            if (exam_point.code, material_version_id) != ("EP-1", "material-1"):
                return super().classify(
                    exam_point=exam_point,
                    material_version_id=material_version_id,
                    chunks=chunks,
                    call_context=call_context,
                )
            if bad_output == "cross_point":
                decisions = [_decision(_point("EP-2", "agent"), chunks[0])]
            else:
                decision = _decision(exam_point, chunks[0])
                decisions = [decision, decision]
            return ExamPointFileDecision(
                exam_point_code=exam_point.code,
                material_version_id=material_version_id,
                decisions=decisions,
            )

    graph, _, _, _, repository = _graph(classifier=BadClassifier())

    graph.invoke(
        _state(), config={"configurable": {"thread_id": f"bad-{bad_output}"}}
    )

    assert len(repository.persisted_state["failed_pairs"]) == 1
    assert repository.persisted_state["failed_pairs"][0]["exam_point_code"] == "EP-1"
    coverage = {item.exam_point_code: item.status for item in repository.candidate.coverage}
    assert coverage == {"EP-1": "insufficient", "EP-2": "sufficient"}


@pytest.mark.parametrize("response_kind", ["empty", "partial"])
def test_incomplete_classifier_response_isolated_to_its_pair(response_kind):
    class MultiChunkRetriever(PairSelectingRetriever):
        def retrieve(self, exam_point, chunks):
            if exam_point.code == "EP-1" and chunks[0].material_version_id == "material-1":
                self.calls.append((exam_point.code, [chunk.id for chunk in chunks]))
                return [
                    RankedChunk(
                        chunk=chunk,
                        score=0.9,
                        lexical_score=0.8,
                        semantic_score=0.95,
                    )
                    for chunk in chunks
                ]
            return super().retrieve(exam_point, chunks)

    class IncompleteClassifier(RecordingClassifier):
        def classify(self, *, exam_point, material_version_id, chunks, call_context=None):
            if (exam_point.code, material_version_id) != ("EP-1", "material-1"):
                return super().classify(
                    exam_point=exam_point,
                    material_version_id=material_version_id,
                    chunks=chunks,
                    call_context=call_context,
                )
            self.calls.append(
                (exam_point.code, material_version_id, [chunk.id for chunk in chunks])
            )
            decisions = [] if response_kind == "empty" else [_decision(exam_point, chunks[0])]
            return ExamPointFileDecision(
                exam_point_code=exam_point.code,
                material_version_id=material_version_id,
                decisions=decisions,
            )

    graph, _, classifier, _, repository = _graph(
        retriever=MultiChunkRetriever(), classifier=IncompleteClassifier()
    )

    graph.invoke(
        _state(),
        config={"configurable": {"thread_id": f"incomplete-{response_kind}"}},
    )

    assert ("EP-1", "material-1", ["chunk-1", "chunk-4"]) in classifier.calls
    failures = repository.persisted_state["failed_pairs"]
    assert len(failures) == 1
    assert failures[0]["exam_point_code"] == "EP-1"
    assert failures[0]["material_version_id"] == "material-1"
    assert failures[0]["error_code"] == "classification_failed"
    coverage = {item.exam_point_code: item.status for item in repository.candidate.coverage}
    assert coverage == {"EP-1": "insufficient", "EP-2": "sufficient"}
    assert any(
        unit.exam_point_code == "EP-2"
        for topic in repository.candidate.topics
        for unit in topic.units
    )


def test_unsupported_consolidated_fact_isolated_to_one_exam_point():
    class BadConsolidator(RecordingConsolidator):
        def consolidate(self, *, exam_point, admitted_decisions, call_context=None):
            units = super().consolidate(
                exam_point=exam_point,
                admitted_decisions=admitted_decisions,
                call_context=call_context,
            )
            if exam_point.code == "EP-1":
                units[0].cards[0].assessable_content = ["模型臆造且证据中不存在的事实"]
            return units

    graph, _, _, _, repository = _graph(consolidator=BadConsolidator())

    graph.invoke(_state(), config={"configurable": {"thread_id": "bad-consolidation"}})

    assert repository.persisted_state["failed_pairs"][0]["error_code"] == "consolidation_failed"
    assert all(
        unit.exam_point_code != "EP-1"
        for topic in repository.candidate.topics
        for unit in topic.units
    )
    coverage = {item.exam_point_code: item.status for item in repository.candidate.coverage}
    assert coverage == {"EP-1": "insufficient", "EP-2": "sufficient"}


def test_empty_consolidation_with_direct_evidence_isolated_to_one_exam_point():
    class EmptyConsolidator(RecordingConsolidator):
        def consolidate(self, *, exam_point, admitted_decisions, call_context=None):
            if exam_point.code == "EP-1":
                return []
            return super().consolidate(
                exam_point=exam_point,
                admitted_decisions=admitted_decisions,
                call_context=call_context,
            )

    graph, _, _, _, repository = _graph(consolidator=EmptyConsolidator())

    graph.invoke(_state(), config={"configurable": {"thread_id": "empty-consolidation"}})

    failures = repository.persisted_state["failed_pairs"]
    assert len(failures) == 1
    assert failures[0]["exam_point_code"] == "EP-1"
    assert failures[0]["error_code"] == "consolidation_failed"
    coverage = {item.exam_point_code: item.status for item in repository.candidate.coverage}
    assert coverage == {"EP-1": "insufficient", "EP-2": "sufficient"}
    assert any(
        unit.exam_point_code == "EP-2"
        for topic in repository.candidate.topics
        for unit in topic.units
    )


def test_organization_graph_resumes_only_after_exam_points_are_reviewed():
    graph, _, _, _, repository = _graph()
    config = {"configurable": {"thread_id": "publish-tree"}}
    graph.invoke(_state(), config=config)

    completed = graph.invoke(
        Command(
            resume={
                "operations": [],
                "reviewed_topic_codes": ["topic-rag", "topic-agent"],
                "reviewed_exam_point_codes": ["EP-1", "EP-2"],
                "teacher_exclusions": [],
            }
        ),
        config=config,
    )

    assert completed["catalog_version_id"] == "catalog-v1"
    assert completed["index_version_id"] == "index-v1"
    assert repository.published[1].reviewed_exam_point_codes == ["EP-1", "EP-2"]


@pytest.mark.parametrize(
    ("operation", "target_kind"),
    [
        ({"operation": "exclude_topic", "target_code": "topic-rag"}, "topic"),
        ({"operation": "exclude_unit", "target_code": "unit-EP-1"}, "unit"),
    ],
)
def test_graph_resume_preserves_explicit_hierarchy_exclusions(operation, target_kind):
    graph, _, _, _, repository = _graph()
    config = {
        "configurable": {"thread_id": f"resume-exclude-{target_kind}"}
    }
    graph.invoke(_state(), config=config)

    graph.invoke(
        Command(
            resume={
                "operations": [operation],
                "reviewed_topic_codes": ["topic-rag", "topic-agent"],
                "reviewed_exam_point_codes": ["EP-1", "EP-2"],
                "teacher_exclusions": [],
            }
        ),
        config=config,
    )

    published_tree, confirmation = repository.published
    assert [item.model_dump(mode="json") for item in confirmation.operations] == [
        {**operation, "value": None}
    ]
    if target_kind == "topic":
        target = next(topic for topic in published_tree.topics if topic.code == "topic-rag")
    else:
        target = next(
            unit
            for topic in published_tree.topics
            for unit in topic.units
            if unit.code == "unit-EP-1"
        )
        assert target.status == "excluded"


def test_graph_resume_reviews_only_topics_left_active_after_exclusions():
    graph, _, _, _, repository = _graph()
    config = {"configurable": {"thread_id": "exclude-all-topics-before-review"}}
    graph.invoke(_state(), config=config)

    graph.invoke(
        Command(
            resume={
                "operations": [
                    {"operation": "exclude_topic", "target_code": "topic-rag"},
                    {"operation": "exclude_topic", "target_code": "topic-agent"},
                ],
                "reviewed_topic_codes": [],
                "reviewed_exam_point_codes": ["EP-1", "EP-2"],
                "teacher_exclusions": [],
            }
        ),
        config=config,
    )

    published_tree, _ = repository.published
    assert {topic.status for topic in published_tree.topics} == {"excluded"}


def test_graph_blocks_unresolved_coverage_until_exam_point_is_excluded():
    classifier = RecordingClassifier(fail_pair=("EP-1", "material-1"))
    graph, _, _, _, _ = _graph(classifier=classifier)
    config = {"configurable": {"thread_id": "unresolved-coverage"}}
    graph.invoke(_state(), config=config)

    with pytest.raises(ValueError, match="coverage"):
        graph.invoke(
            Command(
                resume={
                    "operations": [],
                    "reviewed_topic_codes": ["topic-rag", "topic-agent"],
                    "reviewed_exam_point_codes": ["EP-1", "EP-2"],
                    "teacher_exclusions": [],
                }
            ),
            config=config,
        )


def test_graph_teacher_exclusion_marks_every_unit_for_exam_point_excluded():
    classifier = RecordingClassifier(fail_pair=("EP-1", "material-1"))
    graph, _, _, _, repository = _graph(classifier=classifier)
    config = {"configurable": {"thread_id": "exclude-unresolved"}}
    graph.invoke(_state(), config=config)

    graph.invoke(
        Command(
            resume={
                "operations": [],
                "reviewed_topic_codes": ["topic-rag", "topic-agent"],
                "reviewed_exam_point_codes": ["EP-2"],
                "teacher_exclusions": ["EP-1"],
            }
        ),
        config=config,
    )

    ep1_units = [
        unit
        for topic in repository.published[0].topics
        for unit in topic.units
        if unit.exam_point_code == "EP-1"
    ]
    assert ep1_units and all(unit.status == "excluded" for unit in ep1_units)
