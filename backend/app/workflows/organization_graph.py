"""LangGraph orchestration for outline-constrained material organization."""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.domain.knowledge.models import (
    FileKnowledgeCandidate,
    KnowledgeFileExtractor,
    KnowledgeRepository,
    KnowledgeTreeCandidate,
    KnowledgeTreeConfirmation,
)
from app.services.knowledge_tree_service import apply_tree_operations, validate_publishable_tree
from app.workflows.knowledge_catalog_subgraph import build_knowledge_catalog_candidate


class OrganizationState(TypedDict, total=False):
    course_id: str
    run_id: str
    framework_version_id: str
    framework_anchors: list[dict]
    files: list[dict]
    frozen_input: dict
    file_candidates: list[dict]
    tree: dict
    candidate_id: str
    confirmation: dict
    catalog_version_id: str
    index_version_id: str


def build_organization_graph(
    extractor: KnowledgeFileExtractor,
    repository: KnowledgeRepository,
    *,
    checkpointer=None,
):
    def validate_inputs(state: OrganizationState):
        if not state.get("framework_version_id") or not state.get("framework_anchors"):
            raise ValueError("a published framework with anchors is required")
        if not state.get("files"):
            raise ValueError("at least one teaching material is required")
        material_ids = [item.get("material_version_id") for item in state["files"]]
        if any(not item for item in material_ids) or len(material_ids) != len(set(material_ids)):
            raise ValueError("material versions must be present and unique")
        return {}

    def freeze_selected_materials(state: OrganizationState):
        return {
            "frozen_input": {
                "framework_version_id": state["framework_version_id"],
                "material_version_ids": [item["material_version_id"] for item in state["files"]],
            }
        }

    def extract_per_file_candidates(state: OrganizationState):
        summary = {
            "anchors": [
                {
                    "key": item["key"],
                    "title": item.get("title", ""),
                    "exam_weight": item.get("exam_weight", 0),
                }
                for item in state["framework_anchors"]
            ]
        }
        candidates = []
        for item in state["files"]:
            candidate = extractor.extract_file(
                material_version_id=item["material_version_id"],
                blocks=list(item.get("blocks") or []),
                framework_summary=summary,
            )
            candidates.append(candidate.model_dump(mode="json"))
        return {"file_candidates": candidates}

    def build_catalog_candidate(state: OrganizationState):
        allowed = {item["key"] for item in state["framework_anchors"]}
        tree = build_knowledge_catalog_candidate(
            framework_version_id=state["framework_version_id"],
            allowed_anchor_keys=allowed,
            file_candidates=[FileKnowledgeCandidate.model_validate(item) for item in state["file_candidates"]],
        )
        return {"tree": tree.model_dump(mode="json")}

    def persist_candidate(state: OrganizationState):
        tree = KnowledgeTreeCandidate.model_validate(state["tree"])
        return {"candidate_id": repository.persist_candidate(dict(state), tree)}

    def interrupt_teacher_review(state: OrganizationState):
        decision = interrupt({"candidate_id": state["candidate_id"], "tree": state["tree"]})
        confirmation = KnowledgeTreeConfirmation.model_validate(decision)
        tree = KnowledgeTreeCandidate.model_validate(state["tree"])
        active_topic_codes = {item.code for item in tree.topics if item.status == "active"}
        if active_topic_codes - set(confirmation.reviewed_topic_codes):
            raise ValueError("every active topic requires teacher review")
        allowed = {item["key"] for item in state["framework_anchors"]}
        revised = apply_tree_operations(tree, confirmation.operations, allowed_anchor_keys=allowed)
        validate_publishable_tree(revised, allowed_anchor_keys=allowed)
        return {
            "tree": revised.model_dump(mode="json"),
            "confirmation": confirmation.model_dump(mode="json"),
        }

    def publish_catalog_and_index(state: OrganizationState):
        result = repository.publish(
            dict(state),
            KnowledgeTreeCandidate.model_validate(state["tree"]),
            KnowledgeTreeConfirmation.model_validate(state["confirmation"]),
        )
        return {
            "catalog_version_id": result["catalog_version_id"],
            "index_version_id": result["index_version_id"],
        }

    graph = StateGraph(OrganizationState)
    graph.add_node("validate_inputs", validate_inputs)
    graph.add_node("freeze_selected_materials", freeze_selected_materials)
    graph.add_node("extract_per_file_candidates", extract_per_file_candidates)
    graph.add_node("build_catalog_candidate", build_catalog_candidate)
    graph.add_node("persist_candidate", persist_candidate)
    graph.add_node("interrupt_teacher_review", interrupt_teacher_review)
    graph.add_node("publish_catalog_and_index", publish_catalog_and_index)
    graph.add_edge(START, "validate_inputs")
    graph.add_edge("validate_inputs", "freeze_selected_materials")
    graph.add_edge("freeze_selected_materials", "extract_per_file_candidates")
    graph.add_edge("extract_per_file_candidates", "build_catalog_candidate")
    graph.add_edge("build_catalog_candidate", "persist_candidate")
    graph.add_edge("persist_candidate", "interrupt_teacher_review")
    graph.add_edge("interrupt_teacher_review", "publish_catalog_and_index")
    graph.add_edge("publish_catalog_and_index", END)
    return graph.compile(checkpointer=checkpointer)
