from app.domain.generation.semantic_diversity import (
    AnswerRelation,
    CardSemanticProfile,
    InstanceCarrier,
    build_information_conflicts,
)


def test_combined_component_answers_reveal_summary_proposition():
    profiles = {
        1: CardSemanticProfile(
            concept_cluster="protocol-interface",
            answer_proposition="protocol A uses endpoint A",
            relation_edges=[
                AnswerRelation(kind="component_of", target="protocol paths differ")
            ],
        ),
        2: CardSemanticProfile(
            concept_cluster="protocol-interface",
            answer_proposition="protocol B uses endpoint B",
            relation_edges=[
                AnswerRelation(kind="component_of", target="protocol paths differ")
            ],
        ),
        3: CardSemanticProfile(
            concept_cluster="protocol-interface",
            answer_proposition="protocol paths differ",
        ),
    }

    conflicts = build_information_conflicts(profiles)

    assert conflicts[0].code == "combined_answer_leak"
    assert conflicts[0].source_items == [1, 2]
    assert conflicts[0].target_item == 3


def test_equivalent_answers_conflict_across_distinct_cards():
    profiles = {
        1: CardSemanticProfile(
            concept_cluster="sampling",
            answer_proposition="random sampling reduces selection bias",
        ),
        2: CardSemanticProfile(
            concept_cluster="sampling",
            answer_proposition="random sampling reduces selection bias",
        ),
    }

    conflicts = build_information_conflicts(profiles)

    assert conflicts[0].code == "equivalent_answer"
    assert conflicts[0].source_items == [1]
    assert conflicts[0].target_item == 2


def test_prerequisite_knowledge_is_not_treated_as_disclosed_answer_text():
    profiles = {
        1: CardSemanticProfile(
            concept_cluster="optimization",
            answer_proposition="a learning rate controls update size",
        ),
        2: CardSemanticProfile(
            concept_cluster="optimization",
            answer_proposition="an excessive learning rate can destabilize training",
            required_propositions=["a learning rate controls update size"],
            relation_edges=[
                AnswerRelation(
                    kind="requires",
                    target="a learning rate controls update size",
                )
            ],
        ),
    }

    assert build_information_conflicts(profiles) == []


def test_instance_carrier_is_structural_metadata_not_a_blacklist():
    profile = CardSemanticProfile(
        concept_cluster="statistical-workflow",
        answer_proposition="standardization makes scales comparable",
        instance_carriers=[
            InstanceCarrier(
                normalized_name="ExampleTool",
                carrier_type="software",
                role="illustrative_context",
                authorized_by_syllabus=False,
                replaceable=True,
            )
        ],
    )

    assert profile.instance_carriers[0].normalized_name == "ExampleTool"
