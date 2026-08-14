from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.domain.knowledge.models import AssessmentUnitDraft, FileKnowledgeCandidate, KnowledgeCardDraft, KnowledgeTopicDraft
from app.workflows.organization_graph import build_organization_graph


class RecordingKnowledgeExtractor:
    def __init__(self):
        self.calls = []

    def extract_file(self, *, material_version_id, blocks, framework_summary):
        self.calls.append((material_version_id, list(blocks), dict(framework_summary)))
        anchor = "rag" if material_version_id == "material-1" else "agent"
        return FileKnowledgeCandidate(
            material_version_id=material_version_id,
            topics=[
                KnowledgeTopicDraft(
                    code=f"topic-{anchor}",
                    name=f"主题-{anchor}",
                    framework_anchor_key=anchor,
                    units=[
                        AssessmentUnitDraft(
                            code=f"unit-{anchor}",
                            title=f"分析{anchor}方法",
                            performance_statement=f"能够分析{anchor}方法",
                            cards=[
                                KnowledgeCardDraft(
                                    name=f"{anchor}核心原理",
                                    performance_statement=f"能够说明{anchor}核心原理",
                                    assessable_content=[f"{anchor}的课程事实"],
                                    evidence_chunk_ids=[f"evidence-{material_version_id}"],
                                )
                            ],
                        )
                    ],
                )
            ],
        )


class RecordingKnowledgeRepository:
    def __init__(self):
        self.candidate = None
        self.published = None

    def persist_candidate(self, state, tree):
        self.candidate = tree
        return "catalog-candidate-1"

    def publish(self, state, tree, confirmation):
        self.published = (tree, confirmation)
        return {"catalog_version_id": "catalog-v1", "index_version_id": "index-v1"}


def _state():
    return {
        "course_id": "course-1",
        "run_id": "organization-run-1",
        "framework_version_id": "framework-v1",
        "framework_anchors": [
            {"key": "rag", "title": "RAG", "exam_weight": 60},
            {"key": "agent", "title": "Agent", "exam_weight": 40},
        ],
        "files": [
            {"material_version_id": "material-1", "blocks": ["只属于资料一的正文"]},
            {"material_version_id": "material-2", "blocks": ["只属于资料二的正文"]},
        ],
    }


def test_organization_graph_calls_extractor_once_per_file_without_mixing_content():
    extractor = RecordingKnowledgeExtractor()
    repository = RecordingKnowledgeRepository()
    graph = build_organization_graph(extractor, repository, checkpointer=InMemorySaver())

    paused = graph.invoke(_state(), config={"configurable": {"thread_id": "per-file"}})

    assert [(item[0], item[1]) for item in extractor.calls] == [
        ("material-1", ["只属于资料一的正文"]),
        ("material-2", ["只属于资料二的正文"]),
    ]
    assert all(set(call[2]) == {"anchors"} for call in extractor.calls)
    assert {topic.framework_anchor_key for topic in repository.candidate.topics} == {"rag", "agent"}
    assert "__interrupt__" in paused


def test_organization_graph_resumes_with_structured_operations_and_publishes_catalog_and_index():
    extractor = RecordingKnowledgeExtractor()
    repository = RecordingKnowledgeRepository()
    graph = build_organization_graph(extractor, repository, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "publish-tree"}}
    graph.invoke(_state(), config=config)

    completed = graph.invoke(
        Command(
            resume={
                "operations": [
                    {"operation": "rename_topic", "target_code": "topic-rag", "value": "RAG方法"}
                ],
                "reviewed_topic_codes": ["topic-rag", "topic-agent"],
                "teacher_exclusions": [],
            }
        ),
        config=config,
    )

    assert completed["catalog_version_id"] == "catalog-v1"
    assert completed["index_version_id"] == "index-v1"
    assert repository.published[0].topics[0].name == "RAG方法"
