from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.domain.framework.models import AssessmentAnchor, AssessmentOutline, TeachingTopic
from app.workflows.framework_graph import build_framework_graph


class RecordingExtractor:
    def __init__(self, *, teaching_topics, assessment_outline):
        self.teaching_topics = teaching_topics
        self.assessment_outline = assessment_outline
        self.calls = []

    def extract_teaching(self, blocks):
        self.calls.append(("teaching", list(blocks)))
        return self.teaching_topics

    def extract_assessment(self, blocks):
        self.calls.append(("assessment", list(blocks)))
        return self.assessment_outline


class RecordingRepository:
    def __init__(self):
        self.candidates = []
        self.publications = []

    def persist_candidate(self, state, candidate):
        self.candidates.append((state, candidate))
        return "framework-candidate-1"

    def publish(self, state, confirmation):
        self.publications.append((state, confirmation))
        return "framework-version-1"


def _state():
    return {
        "course_id": "course-1",
        "run_id": "run-1",
        "teaching_material_version_id": "teaching-v1",
        "assessment_material_version_id": "assessment-v1",
        "teaching_blocks": ["教学大纲正文"],
        "assessment_blocks": ["考核大纲期末考试正文"],
    }


def _confirmation(*, conflict_resolutions=None):
    return {
        "anchors": [
            {
                "key": "assessment-unit",
                "title": "考核大纲范围",
                "exam_weight": 100,
                "ability_requirements": ["理解并应用"],
                "allowed_question_types": ["single_choice", "short_answer"],
                "alignment_keys": ["taught-topic"],
                "excluded_content": [],
            }
        ],
        "conflict_resolutions": conflict_resolutions or {},
        "teacher_exclusions": [],
    }


def _graph(*, teaching_topics=None, weight=100, alignment_keys=None):
    extractor = RecordingExtractor(
        teaching_topics=teaching_topics
        if teaching_topics is not None
        else [TeachingTopic(key="taught-topic", title="已教主题", depth="apply")],
        assessment_outline=AssessmentOutline(
            anchors=[
                AssessmentAnchor(
                    key="assessment-unit",
                    title="考核大纲范围",
                    exam_weight=weight,
                    ability_requirements=["理解并应用"],
                    allowed_question_types=["single_choice", "short_answer"],
                    alignment_keys=alignment_keys if alignment_keys is not None else ["taught-topic"],
                )
            ],
            final_exam_rules={"question_type_weights": {"single_choice": 40, "short_answer": 60}},
        ),
    )
    repository = RecordingRepository()
    graph = build_framework_graph(extractor, repository, checkpointer=InMemorySaver())
    return graph, extractor, repository


def test_two_syllabuses_are_extracted_independently_and_assessment_controls_scope():
    graph, extractor, repository = _graph(
        teaching_topics=[
            TeachingTopic(key="taught-topic", title="已教主题", depth="apply"),
            TeachingTopic(key="teaching-only", title="只在教学大纲出现", depth="understand"),
        ]
    )
    config = {"configurable": {"thread_id": "separate-syllabuses"}}

    paused = graph.invoke(_state(), config=config)

    assert sorted(extractor.calls) == sorted([
        ("teaching", ["教学大纲正文"]),
        ("assessment", ["考核大纲期末考试正文"]),
    ])
    candidate = repository.candidates[0][1]
    assert [anchor.key for anchor in candidate.anchors] == ["assessment-unit"]
    assert candidate.final_exam_rules["question_type_weights"]["short_answer"] == 60
    assert "__interrupt__" in paused


def test_teaching_and_assessment_extraction_are_parallel_graph_branches():
    graph, _, _ = _graph()

    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}

    assert ("ensure_document_parsed", "extract_teaching_syllabus") in edges
    assert ("ensure_document_parsed", "extract_assessment_syllabus") in edges
    assert ("extract_teaching_syllabus", "merge_assessment_led_framework") in edges
    assert ("extract_assessment_syllabus", "merge_assessment_led_framework") in edges


def test_invalid_weight_and_missing_teaching_coverage_are_explicit_conflicts():
    graph, _, repository = _graph(teaching_topics=[], weight=80, alignment_keys=["not-taught"])
    config = {"configurable": {"thread_id": "explicit-conflicts"}}

    graph.invoke(_state(), config=config)

    conflicts = {item.key: item for item in repository.candidates[0][1].conflicts}
    assert conflicts["weight:total"].kind == "weight_total"
    assert conflicts["coverage:assessment-unit"].kind == "missing_teaching_coverage"
    assert all(item.status == "open" for item in conflicts.values())


def test_resume_requires_decisions_for_every_open_conflict():
    graph, _, repository = _graph(teaching_topics=[], weight=100, alignment_keys=["not-taught"])
    config = {"configurable": {"thread_id": "missing-resolution"}}
    graph.invoke(_state(), config=config)

    with pytest.raises(ValueError, match="every open conflict"):
        graph.invoke(Command(resume=_confirmation()), config=config)

    assert repository.publications == []


def test_complete_confirmation_resumes_and_publishes_framework_version():
    graph, _, repository = _graph(teaching_topics=[], weight=100, alignment_keys=["not-taught"])
    config = {"configurable": {"thread_id": "publish-framework"}}
    graph.invoke(_state(), config=config)

    completed = graph.invoke(
        Command(
            resume=_confirmation(
                conflict_resolutions={"coverage:assessment-unit": "确认该内容已在课堂补充讲授"}
            )
        ),
        config=config,
    )

    assert completed["published_id"] == "framework-version-1"
    assert len(repository.publications) == 1
    assert repository.publications[0][1].teacher_exclusions == []
