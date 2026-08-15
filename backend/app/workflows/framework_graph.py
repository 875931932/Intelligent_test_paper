"""LangGraph workflow for assessment-led framework construction."""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.domain.framework.models import (
    AssessmentOutline,
    FrameworkCandidate,
    FrameworkConflict,
    FrameworkConfirmation,
    FrameworkRepository,
    SyllabusExtractor,
    TeachingTopic,
)
from app.domain.model_calls import ModelCallContext


class FrameworkState(TypedDict, total=False):
    course_id: str
    run_id: str
    teaching_material_version_id: str
    assessment_material_version_id: str
    teaching_blocks: list[str]
    assessment_blocks: list[str]
    frozen_input: dict
    teaching_topics: list[dict]
    assessment_outline: dict
    candidate: dict
    candidate_id: str
    confirmation: dict
    published_id: str


def build_framework_graph(extractor: SyllabusExtractor, repository: FrameworkRepository, *, checkpointer=None):
    def validate_inputs(state: FrameworkState):
        required = (
            "course_id",
            "run_id",
            "teaching_material_version_id",
            "assessment_material_version_id",
            "teaching_blocks",
            "assessment_blocks",
        )
        if any(not state.get(field) for field in required):
            raise ValueError("both syllabus versions and parsed content are required")
        if state["teaching_material_version_id"] == state["assessment_material_version_id"]:
            raise ValueError("teaching and assessment syllabuses must be separate files")
        return {}

    def freeze_outline_versions(state: FrameworkState):
        return {
            "frozen_input": {
                "teaching_material_version_id": state["teaching_material_version_id"],
                "assessment_material_version_id": state["assessment_material_version_id"],
            }
        }

    def ensure_document_parsed(state: FrameworkState):
        if not state["teaching_blocks"] or not state["assessment_blocks"]:
            raise ValueError("both syllabuses must have ready parsed content")
        return {}

    def extract_teaching_syllabus(state: FrameworkState):
        topics = extractor.extract_teaching(
            state["teaching_blocks"],
            call_context=ModelCallContext(
                course_id=state["course_id"],
                framework_build_run_id=state["run_id"],
                stage="teaching_syllabus_extraction",
            ),
        )
        return {"teaching_topics": [topic.model_dump(mode="json") for topic in topics]}

    def extract_assessment_syllabus(state: FrameworkState):
        outline = extractor.extract_assessment(
            state["assessment_blocks"],
            call_context=ModelCallContext(
                course_id=state["course_id"],
                framework_build_run_id=state["run_id"],
                stage="assessment_syllabus_extraction",
            ),
        )
        return {"assessment_outline": outline.model_dump(mode="json")}

    def merge_assessment_led_framework(state: FrameworkState):
        teaching = [TeachingTopic.model_validate(item) for item in state["teaching_topics"]]
        assessment = AssessmentOutline.model_validate(state["assessment_outline"])
        teaching_keys = {topic.key for topic in teaching}
        conflicts = []
        for anchor in assessment.anchors:
            if not teaching_keys.intersection(anchor.alignment_keys):
                conflicts.append(
                    FrameworkConflict(
                        key=f"coverage:{anchor.key}",
                        kind="missing_teaching_coverage",
                        message=f"考核范围“{anchor.title}”未在教学大纲中找到覆盖依据",
                    )
                )
        total = sum(anchor.exam_weight for anchor in assessment.anchors)
        if abs(total - 100) > 0.01:
            conflicts.append(
                FrameworkConflict(
                    key="weight:total",
                    kind="weight_total",
                    message=f"期末考试章节权重合计为 {total:g}%，需要修订为 100%",
                )
            )
        candidate = FrameworkCandidate(
            anchors=assessment.anchors,
            exam_points=assessment.exam_points,
            teaching_topics=teaching,
            conflicts=conflicts,
            final_exam_rules=assessment.final_exam_rules,
        )
        return {"candidate": candidate.model_dump(mode="json")}

    def align_exam_points_with_teaching(state: FrameworkState):
        candidate = FrameworkCandidate.model_validate(state["candidate"])
        topics_by_key = {topic.key: topic for topic in candidate.teaching_topics}
        conflicts = list(candidate.conflicts)
        conflict_keys = {conflict.key for conflict in conflicts}
        for point in candidate.exam_points:
            aligned_topics = [
                topics_by_key[key]
                for key in point.teaching_anchor_keys
                if key in topics_by_key
            ]
            if not aligned_topics:
                key = f"exam-point-coverage:{point.code}"
                if key not in conflict_keys:
                    conflicts.append(
                        FrameworkConflict(
                            key=key,
                            kind="missing_teaching_coverage",
                            message=f"考点“{point.title}”未在教学大纲中找到覆盖依据",
                        )
                    )
                    conflict_keys.add(key)
                continue
            point_depth = _highest_depth(point.cognitive_targets)
            teaching_depths = [
                depth
                for topic in aligned_topics
                if (depth := _highest_depth([topic.depth, *topic.requirements])) is not None
            ]
            teaching_depth = max(teaching_depths, default=None)
            if point_depth is not None and teaching_depth is not None and point_depth > teaching_depth:
                key = f"exam-point-depth:{point.code}"
                if key not in conflict_keys:
                    conflicts.append(
                        FrameworkConflict(
                            key=key,
                            kind="teaching_depth_conflict",
                            message=f"考点“{point.title}”的认知要求高于教学大纲中的教学深度",
                        )
                    )
                    conflict_keys.add(key)
        revised = candidate.model_copy(update={"conflicts": conflicts})
        return {"candidate": revised.model_dump(mode="json")}

    def validate_conflicts(state: FrameworkState):
        FrameworkCandidate.model_validate(state["candidate"])
        return {}

    def persist_candidate(state: FrameworkState):
        candidate = FrameworkCandidate.model_validate(state["candidate"])
        return {"candidate_id": repository.persist_candidate(dict(state), candidate)}

    def interrupt_teacher_confirmation(state: FrameworkState):
        decision = interrupt({"candidate_id": state["candidate_id"], "candidate": state["candidate"]})
        confirmation = FrameworkConfirmation.model_validate(decision)
        open_keys = {item["key"] for item in state["candidate"].get("conflicts", []) if item.get("status") == "open"}
        if open_keys - set(confirmation.conflict_resolutions):
            raise ValueError("every open conflict requires a teacher resolution")
        if set(confirmation.conflict_resolutions) - open_keys:
            raise ValueError("confirmation contains an unknown conflict resolution")
        return {"confirmation": confirmation.model_dump(mode="json")}

    def publish_framework_version(state: FrameworkState):
        confirmation = FrameworkConfirmation.model_validate(state["confirmation"])
        return {"published_id": repository.publish(dict(state), confirmation)}

    graph = StateGraph(FrameworkState)
    graph.add_node("validate_inputs", validate_inputs)
    graph.add_node("freeze_outline_versions", freeze_outline_versions)
    graph.add_node("ensure_document_parsed", ensure_document_parsed)
    graph.add_node("extract_teaching_syllabus", extract_teaching_syllabus)
    graph.add_node("extract_assessment_syllabus", extract_assessment_syllabus)
    graph.add_node("merge_assessment_led_framework", merge_assessment_led_framework)
    graph.add_node("align_exam_points_with_teaching", align_exam_points_with_teaching)
    graph.add_node("validate_conflicts", validate_conflicts)
    graph.add_node("persist_candidate", persist_candidate)
    graph.add_node("interrupt_teacher_confirmation", interrupt_teacher_confirmation)
    graph.add_node("publish_framework_version", publish_framework_version)
    graph.add_edge(START, "validate_inputs")
    graph.add_edge("validate_inputs", "freeze_outline_versions")
    graph.add_edge("freeze_outline_versions", "ensure_document_parsed")
    graph.add_edge("ensure_document_parsed", "extract_teaching_syllabus")
    graph.add_edge("ensure_document_parsed", "extract_assessment_syllabus")
    graph.add_edge(["extract_teaching_syllabus", "extract_assessment_syllabus"], "merge_assessment_led_framework")
    graph.add_edge("merge_assessment_led_framework", "align_exam_points_with_teaching")
    graph.add_edge("align_exam_points_with_teaching", "validate_conflicts")
    graph.add_edge("validate_conflicts", "persist_candidate")
    graph.add_edge("persist_candidate", "interrupt_teacher_confirmation")
    graph.add_edge("interrupt_teacher_confirmation", "publish_framework_version")
    graph.add_edge("publish_framework_version", END)
    return graph.compile(checkpointer=checkpointer)


_DEPTH_TERMS = (
    ("create", 5),
    ("创造", 5),
    ("设计", 5),
    ("evaluate", 4),
    ("评价", 4),
    ("评估", 4),
    ("analyze", 3),
    ("分析", 3),
    ("apply", 2),
    ("应用", 2),
    ("掌握", 2),
    ("understand", 1),
    ("理解", 1),
    ("解释", 1),
    ("remember", 0),
    ("了解", 0),
    ("识记", 0),
    ("记忆", 0),
)


def _highest_depth(values: list[str]) -> int | None:
    ranks = [
        rank
        for value in values
        for term, rank in _DEPTH_TERMS
        if term in value.strip().lower()
    ]
    return max(ranks) if ranks else None
