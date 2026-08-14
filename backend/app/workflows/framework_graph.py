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
        topics = extractor.extract_teaching(state["teaching_blocks"])
        return {"teaching_topics": [topic.model_dump(mode="json") for topic in topics]}

    def extract_assessment_syllabus(state: FrameworkState):
        outline = extractor.extract_assessment(state["assessment_blocks"])
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
            teaching_topics=teaching,
            conflicts=conflicts,
            final_exam_rules=assessment.final_exam_rules,
        )
        return {"candidate": candidate.model_dump(mode="json")}

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
    graph.add_edge("merge_assessment_led_framework", "validate_conflicts")
    graph.add_edge("validate_conflicts", "persist_candidate")
    graph.add_edge("persist_candidate", "interrupt_teacher_confirmation")
    graph.add_edge("interrupt_teacher_confirmation", "publish_framework_version")
    graph.add_edge("publish_framework_version", END)
    return graph.compile(checkpointer=checkpointer)
