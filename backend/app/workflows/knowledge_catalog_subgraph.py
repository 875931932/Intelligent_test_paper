"""Small-batch catalogue candidate construction."""

from __future__ import annotations

from app.domain.knowledge.models import FileKnowledgeCandidate, KnowledgeTreeCandidate
from app.services.knowledge_tree_service import merge_file_candidates


def build_knowledge_catalog_candidate(
    *,
    framework_version_id: str,
    allowed_anchor_keys: set[str],
    file_candidates: list[FileKnowledgeCandidate],
) -> KnowledgeTreeCandidate:
    tree = merge_file_candidates(file_candidates, allowed_anchor_keys=allowed_anchor_keys)
    tree.framework_version_id = framework_version_id
    return tree
