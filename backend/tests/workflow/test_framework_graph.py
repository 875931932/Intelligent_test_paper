from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.domain.framework.exam_points import ExamPoint, OperationalDetailPolicy, WeightSource
from app.domain.framework.models import AssessmentAnchor, AssessmentOutline, TeachingTopic
from app.workflows.framework_graph import build_framework_graph


class RecordingExtractor:
    def __init__(self, *, teaching_topics, assessment_outline):
        self.teaching_topics = teaching_topics
        self.assessment_outline = assessment_outline
        self.calls = []

    def extract_teaching(self, blocks, *, call_context=None):
        self.calls.append(("teaching", list(blocks), call_context))
        return self.teaching_topics

    def extract_assessment(self, blocks, *, call_context=None):
        self.calls.append(("assessment", list(blocks), call_context))
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
        "exam_points": [point.model_dump(mode="json") for point in _exam_points()],
        "conflict_resolutions": conflict_resolutions or {},
        "teacher_exclusions": [],
    }


def _exam_points(*, weight_source=WeightSource.ASSESSMENT_SYLLABUS, weight_value=50, teaching_anchor_keys=None):
    teaching_keys = ["taught-topic"] if teaching_anchor_keys is None else teaching_anchor_keys
    return [
        ExamPoint(
            code="rag-diagnosis",
            anchor_key="assessment-unit",
            title="RAG 检索诊断",
            assessment_requirement="诊断检索结果不相关的原因并提出改进方案",
            weight_value=weight_value,
            weight_source=weight_source,
            weight_group_id="assessment-unit",
            cognitive_targets=["analyze"],
            assessment_orientations=["problem_solving"],
            allowed_question_types=["short_answer", "comprehensive"],
            operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
            retrieval_intent="检索 RAG 诊断所需的原理、症状与改进依据",
            assessment_anchor_keys=["assessment-unit"],
            teaching_anchor_keys=teaching_keys,
        ),
        ExamPoint(
            code="rag-evaluation",
            anchor_key="assessment-unit",
            title="RAG 效果评价",
            assessment_requirement="评价 RAG 方案并说明判断依据",
            weight_value=weight_value,
            weight_source=weight_source,
            weight_group_id="assessment-unit",
            cognitive_targets=["evaluate"],
            assessment_orientations=["application"],
            allowed_question_types=["short_answer", "comprehensive"],
            operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
            retrieval_intent="检索 RAG 评价指标、现象与优化依据",
            assessment_anchor_keys=["assessment-unit"],
            teaching_anchor_keys=teaching_keys,
        ),
    ]


def _graph(
    *,
    teaching_topics=None,
    weight=100,
    alignment_keys=None,
    exam_points=None,
):
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
            exam_points=exam_points if exam_points is not None else _exam_points(),
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

    calls = {kind: (blocks, context) for kind, blocks, context in extractor.calls}
    assert calls["teaching"][0] == ["教学大纲正文"]
    assert calls["teaching"][1].course_id == "course-1"
    assert calls["teaching"][1].framework_build_run_id == "run-1"
    assert calls["teaching"][1].stage == "teaching_syllabus_extraction"
    assert calls["assessment"][0] == ["考核大纲期末考试正文"]
    assert calls["assessment"][1].course_id == "course-1"
    assert calls["assessment"][1].framework_build_run_id == "run-1"
    assert calls["assessment"][1].stage == "assessment_syllabus_extraction"
    candidate = repository.candidates[0][1]
    assert [anchor.key for anchor in candidate.anchors] == ["assessment-unit"]
    assert candidate.exam_points[0].code == "rag-diagnosis"
    assert candidate.exam_points[0].weight_source == WeightSource.ASSESSMENT_SYLLABUS
    assert candidate.exam_points[0].teaching_anchor_keys == ["taught-topic"]
    assert candidate.exam_points[0].operational_detail_policy == OperationalDetailPolicy.SUPPORTING_ONLY
    assert candidate.final_exam_rules["question_type_weights"]["short_answer"] == 60
    assert "__interrupt__" in paused


def test_teaching_and_assessment_extraction_are_parallel_graph_branches():
    graph, _, _ = _graph()

    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}

    assert ("ensure_document_parsed", "extract_teaching_syllabus") in edges
    assert ("ensure_document_parsed", "extract_assessment_syllabus") in edges
    assert ("extract_teaching_syllabus", "merge_assessment_led_framework") in edges
    assert ("extract_assessment_syllabus", "merge_assessment_led_framework") in edges
    assert ("merge_assessment_led_framework", "align_exam_points_with_teaching") in edges
    assert ("align_exam_points_with_teaching", "validate_conflicts") in edges


def test_invalid_weight_and_missing_teaching_coverage_are_explicit_conflicts():
    graph, _, repository = _graph(
        teaching_topics=[],
        weight=80,
        alignment_keys=["not-taught"],
        exam_points=_exam_points(weight_value=40),
    )
    config = {"configurable": {"thread_id": "explicit-conflicts"}}

    graph.invoke(_state(), config=config)

    conflicts = {item.key: item for item in repository.candidates[0][1].conflicts}
    assert conflicts["weight:total"].kind == "weight_total"
    assert conflicts["coverage:assessment-unit"].kind == "missing_teaching_coverage"
    assert conflicts["exam-point-coverage:rag-diagnosis"].kind == "missing_teaching_coverage"
    assert all(item.status == "open" for item in conflicts.values())


def test_exam_point_without_teaching_alignment_creates_point_coverage_conflict():
    graph, _, repository = _graph(
        exam_points=_exam_points(teaching_anchor_keys=["not-taught"]),
    )

    graph.invoke(_state(), config={"configurable": {"thread_id": "point-coverage"}})

    conflicts = {item.key: item for item in repository.candidates[0][1].conflicts}
    assert conflicts["exam-point-coverage:rag-diagnosis"].kind == "missing_teaching_coverage"
    assert conflicts["exam-point-coverage:rag-evaluation"].kind == "missing_teaching_coverage"


def test_exam_point_depth_above_aligned_teaching_depth_creates_conflict_without_lowering_target():
    graph, _, repository = _graph(
        teaching_topics=[TeachingTopic(key="taught-topic", title="已教主题", depth="理解")],
    )

    graph.invoke(_state(), config={"configurable": {"thread_id": "point-depth"}})

    candidate = repository.candidates[0][1]
    conflicts = {item.key: item for item in candidate.conflicts}
    assert conflicts["exam-point-depth:rag-diagnosis"].kind == "teaching_depth_conflict"
    assert candidate.exam_points[0].cognitive_targets == ["analyze"]


def test_depth_alignment_ignores_unknown_labels_when_a_known_teaching_depth_exists():
    graph, _, repository = _graph(
        teaching_topics=[
            TeachingTopic(key="taught-topic", title="已教主题", depth="应用"),
            TeachingTopic(key="ungraded-topic", title="未分级主题", depth="课程要求"),
        ],
        exam_points=_exam_points(teaching_anchor_keys=["taught-topic", "ungraded-topic"]),
    )

    graph.invoke(_state(), config={"configurable": {"thread_id": "mixed-depth-labels"}})

    conflicts = {item.key: item for item in repository.candidates[0][1].conflicts}
    assert conflicts["exam-point-depth:rag-diagnosis"].kind == "teaching_depth_conflict"


def test_inherited_group_weights_are_preserved_without_automatic_equal_split():
    inherited_points = _exam_points(weight_source=WeightSource.INHERITED_GROUP, weight_value=0)
    graph, _, repository = _graph(exam_points=inherited_points)

    graph.invoke(_state(), config={"configurable": {"thread_id": "inherited-weights"}})

    points = repository.candidates[0][1].exam_points
    assert [point.weight_source for point in points] == [WeightSource.INHERITED_GROUP, WeightSource.INHERITED_GROUP]
    assert [point.weight_value for point in points] == [0, 0]


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
                conflict_resolutions={
                    "coverage:assessment-unit": "确认该内容已在课堂补充讲授",
                    "exam-point-coverage:rag-diagnosis": "确认该考点已在课堂补充讲授",
                    "exam-point-coverage:rag-evaluation": "确认该考点已在课堂补充讲授",
                }
            )
        ),
        config=config,
    )

    assert completed["published_id"] == "framework-version-1"
    assert len(repository.publications) == 1
    assert repository.publications[0][1].teacher_exclusions == []
