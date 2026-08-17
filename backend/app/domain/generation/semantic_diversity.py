"""Source-free semantic diversity and cross-question dependency contracts."""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, Field

from app.domain.knowledge.relevance import semantic_text_key


class InstanceCarrier(BaseModel):
    normalized_name: str
    carrier_type: str = "other"
    role: Literal["required_subject", "illustrative_context"] = "illustrative_context"
    authorized_by_syllabus: bool = False
    replaceable: bool = True


class AnswerRelation(BaseModel):
    kind: Literal[
        "equivalent_to",
        "specializes",
        "component_of",
        "contrasts_with",
        "summarizes",
        "requires",
    ]
    target: str


class CardSemanticProfile(BaseModel):
    concept_cluster: str
    answer_proposition: str
    required_propositions: list[str] = Field(default_factory=list)
    relation_edges: list[AnswerRelation] = Field(default_factory=list)
    instance_carriers: list[InstanceCarrier] = Field(default_factory=list)


class InformationConflict(BaseModel):
    code: Literal[
        "equivalent_answer",
        "direct_answer_leak",
        "combined_answer_leak",
    ]
    source_items: list[int]
    target_item: int


def build_information_conflicts(
    profiles: dict[int, CardSemanticProfile],
) -> list[InformationConflict]:
    """Find answer dependencies using reusable proposition relationships."""

    proposition_items: dict[str, list[int]] = defaultdict(list)
    for item_index, profile in profiles.items():
        proposition_items[semantic_text_key(profile.answer_proposition)].append(item_index)

    conflicts: list[InformationConflict] = []
    for indexes in proposition_items.values():
        ordered = sorted(indexes)
        for target_item in ordered[1:]:
            conflicts.append(
                InformationConflict(
                    code="equivalent_answer",
                    source_items=[ordered[0]],
                    target_item=target_item,
                )
            )

    component_sources: dict[str, set[int]] = defaultdict(set)
    for item_index, profile in profiles.items():
        for relation in profile.relation_edges:
            target_key = semantic_text_key(relation.target)
            if relation.kind == "component_of":
                component_sources[target_key].add(item_index)
            elif relation.kind == "equivalent_to":
                for target_item in proposition_items.get(target_key, []):
                    if target_item != item_index:
                        conflicts.append(
                            InformationConflict(
                                code="direct_answer_leak",
                                source_items=[item_index],
                                target_item=target_item,
                            )
                        )

    for target_key, source_items in component_sources.items():
        ordered_sources = sorted(source_items)
        if len(ordered_sources) < 2:
            continue
        for target_item in proposition_items.get(target_key, []):
            sources = [item for item in ordered_sources if item != target_item]
            if len(sources) >= 2:
                conflicts.append(
                    InformationConflict(
                        code="combined_answer_leak",
                        source_items=sources,
                        target_item=target_item,
                    )
                )

    unique: dict[tuple[str, tuple[int, ...], int], InformationConflict] = {}
    for conflict in conflicts:
        key = (conflict.code, tuple(conflict.source_items), conflict.target_item)
        unique[key] = conflict
    return sorted(
        unique.values(),
        key=lambda conflict: (
            conflict.target_item,
            conflict.code,
            conflict.source_items,
        ),
    )
