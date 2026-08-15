from __future__ import annotations

from time import sleep

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
from app.services.staging_retrieval_service import RankedChunk
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


def _state():
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
        "evidence_chunk_ids": [chunk.id for chunk in _chunks()],
        "frozen_input": {
            "framework_version_id": "framework-v1",
            "exam_point_ids": ["db-ep-1", "db-ep-2"],
            "material_version_ids": ["material-1", "material-2"],
        },
    }


def _graph(*, classifier=None, retriever=None):
    chunks = _chunks()
    repository = RecordingKnowledgeRepository(chunks)
    retriever = retriever or PairSelectingRetriever()
    classifier = classifier or RecordingClassifier()
    consolidator = RecordingConsolidator()
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

    assert len(retriever.calls) == 2
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


def test_each_classifier_pair_is_capped_to_configured_top_k(monkeypatch):
    class SameFileRetriever(PairSelectingRetriever):
        def retrieve(self, exam_point, chunks):
            if exam_point.code == "EP-2":
                return []
            return [
                RankedChunk(chunk=chunk, score=0.9, lexical_score=0.8, semantic_score=0.95)
                for chunk in chunks
                if chunk.id in {"chunk-1", "chunk-4"}
            ]

    monkeypatch.setattr(settings, "organization_retrieval_top_k", 1)
    graph, _, classifier, _, _ = _graph(retriever=SameFileRetriever())

    graph.invoke(_state(), config={"configurable": {"thread_id": "pair-top-k"}})

    assert [(point, material, ids) for point, material, ids in classifier.calls] == [
        ("EP-1", "material-1", ["chunk-1"])
    ]


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
