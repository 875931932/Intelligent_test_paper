from __future__ import annotations

from app.domain.knowledge.models import FileKnowledgeCandidate
from app.workflows.knowledge_catalog_subgraph import build_knowledge_catalog_candidate


def test_catalog_subgraph_keeps_unmatched_candidates_outside_tree(sample_file_candidates=None):
    candidate = FileKnowledgeCandidate(
        material_version_id="material-v1",
        topics=[],
        unmatched=[],
    )

    tree = build_knowledge_catalog_candidate(
        framework_version_id="framework-v1",
        allowed_anchor_keys={"rag"},
        file_candidates=[candidate],
    )

    assert tree.framework_version_id == "framework-v1"
    assert tree.topics == []
