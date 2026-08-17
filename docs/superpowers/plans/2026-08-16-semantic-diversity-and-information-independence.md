# Semantic Diversity and Information Independence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent a paper from concentrating on semantically equivalent knowledge families or concrete examples, and reject independent questions whose information jointly reveals another answer.

**Architecture:** Add source-free semantic profiles to atomic knowledge cards, carry those profiles into the blueprint allocator, and select cards with a three-level ledger for card, concept cluster, and instance carrier. Build a deterministic proposition dependency graph before generation, while retaining the existing model audit only for natural-language leakage introduced after generation.

**Tech Stack:** Python 3.12, Pydantic v2, LangGraph, pytest, Ruff.

---

## File structure

- Create `backend/app/domain/generation/semantic_diversity.py`: semantic profile models, concept/instance ledger scoring, proposition dependency graph, and conflict DTOs.
- Modify `backend/app/domain/knowledge/models.py`: persist source-free semantic profile fields on each atomic card.
- Modify `backend/app/domain/blueprint/models.py`: expose semantic profiles to the allocator and retain selected cluster/carrier metadata on plan items.
- Modify `backend/app/services/blueprint_service.py`: choose compatible cards lexicographically by unused cluster, unused illustrative carrier, unused card, and existing load pressure.
- Modify `backend/app/domain/generation/coverage.py`: compile cluster, proposition, relation, and carrier information into the full-paper planning contract without including source metadata.
- Modify `backend/app/workflows/generation_graph.py`: block proposition dependency conflicts before question generation and reuse the existing local replacement path.
- Modify `backend/app/adapters/model/deepseek_semantic_extractors.py`: accept the source-free semantic profile returned during knowledge consolidation.
- Modify `backend/scripts/build_real_material_demo.py`: request and carry semantic profiles in real-material atomic fact extraction and blueprint construction.
- Test `backend/tests/unit/test_semantic_diversity.py`, `backend/tests/unit/test_blueprint_allocation.py`, `backend/tests/unit/test_coverage_planner.py`, `backend/tests/workflow/test_generation_graph.py`, and `backend/tests/unit/test_real_material_demo.py`.
- Update `docs/superpowers/specs/2026-08-14-core-exam-system-development-design.md` with the finalized three-level ledger and information-dependency rules.

### Task 1: Add source-free semantic card profiles

**Files:**
- Create: `backend/app/domain/generation/semantic_diversity.py`
- Modify: `backend/app/domain/knowledge/models.py`
- Test: `backend/tests/unit/test_semantic_diversity.py`

- [ ] **Step 1: Write failing model and graph tests**

```python
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
            answer_proposition="protocol-a uses endpoint a",
            relation_edges=[AnswerRelation(kind="component_of", target="protocol paths differ")],
        ),
        2: CardSemanticProfile(
            concept_cluster="protocol-interface",
            answer_proposition="protocol-b uses endpoint b",
            relation_edges=[AnswerRelation(kind="component_of", target="protocol paths differ")],
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
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
python -m pytest backend/tests/unit/test_semantic_diversity.py -q
```

Expected: collection fails because `semantic_diversity` does not exist.

- [ ] **Step 3: Implement the semantic models and deterministic graph**

```python
class InstanceCarrier(BaseModel):
    normalized_name: str
    carrier_type: str = "other"
    role: Literal["required_subject", "illustrative_context"] = "illustrative_context"
    authorized_by_syllabus: bool = False
    replaceable: bool = True


class AnswerRelation(BaseModel):
    kind: Literal["equivalent_to", "specializes", "component_of", "contrasts_with", "summarizes", "requires"]
    target: str


class CardSemanticProfile(BaseModel):
    concept_cluster: str
    answer_proposition: str
    required_propositions: list[str] = Field(default_factory=list)
    relation_edges: list[AnswerRelation] = Field(default_factory=list)
    instance_carriers: list[InstanceCarrier] = Field(default_factory=list)


class InformationConflict(BaseModel):
    code: Literal["equivalent_answer", "direct_answer_leak", "combined_answer_leak"]
    source_items: list[int]
    target_item: int
```

`build_information_conflicts()` must normalize proposition text with the existing `semantic_text_key()`, reject equivalent propositions, follow `requires` and `equivalent_to` as direct edges, and group two or more `component_of` edges targeting a selected proposition as a combined leak. It must not inspect or special-case carrier names.

Add optional profile fields to `KnowledgeCardDraft`:

```python
concept_cluster: str = ""
answer_proposition: str = ""
required_propositions: list[str] = Field(default_factory=list)
relation_edges: list[AnswerRelation] = Field(default_factory=list)
instance_carriers: list[InstanceCarrier] = Field(default_factory=list)
```

Keep backward-compatible defaults for stored candidates, but active publication and the executable demo must fill `concept_cluster` and `answer_proposition` before blueprint allocation.

- [ ] **Step 4: Run the focused tests and verify GREEN**

```powershell
python -m pytest backend/tests/unit/test_semantic_diversity.py -q
```

Expected: all tests in the file pass.

### Task 2: Add concept-cluster and instance-carrier allocation ledgers

**Files:**
- Modify: `backend/app/domain/blueprint/models.py`
- Modify: `backend/app/services/blueprint_service.py`
- Test: `backend/tests/unit/test_blueprint_allocation.py`

- [ ] **Step 1: Write failing generic allocation tests**

Add tests using neutral protocol and statistics fixtures:

```python
def test_allocator_prefers_unused_concept_clusters_over_distinct_cards_in_one_cluster():
    request = _request_with_profiles(
        cards={
            "a-path": {"concept_cluster": "interface-paths", "answer_proposition": "A uses path A"},
            "b-path": {"concept_cluster": "interface-paths", "answer_proposition": "B uses path B"},
            "auth": {"concept_cluster": "authentication", "answer_proposition": "tokens authenticate requests"},
        },
        choice_count=2,
    )

    plan = allocate_plan_items(request)

    assert {item.concept_cluster for item in plan.items} == {"interface-paths", "authentication"}


def test_allocator_avoids_reusing_replaceable_instance_when_alternative_exists():
    request = _request_with_profiles(
        cards={
            "tool-a-1": _profile("workflow-one", carrier="ExampleTool"),
            "tool-a-2": _profile("workflow-two", carrier="ExampleTool"),
            "neutral": _profile("workflow-three"),
        },
        choice_count=2,
    )

    plan = allocate_plan_items(request)

    assert sum("ExampleTool" in item.instance_carriers for item in plan.items) == 1


def test_syllabus_authorized_required_subject_can_be_selected_more_than_once():
    request = _request_with_profiles(
        cards={
            "work-1": _profile("theme", carrier="NamedWork", required=True, authorized=True),
            "work-2": _profile("form", carrier="NamedWork", required=True, authorized=True),
        },
        choice_count=2,
    )

    assert len(allocate_plan_items(request).items) == 2
```

- [ ] **Step 2: Run only the new allocation tests and verify RED**

```powershell
python -m pytest backend/tests/unit/test_blueprint_allocation.py -q -k "concept_cluster or replaceable_instance or syllabus_authorized"
```

Expected: tests fail because blueprint models do not carry semantic profiles.

- [ ] **Step 3: Extend blueprint contracts**

Add to `BlueprintRequest`:

```python
card_semantic_profiles: dict[str, CardSemanticProfile] = Field(default_factory=dict)
```

Add to `PlanItem`:

```python
concept_cluster: str = ""
answer_proposition: str = ""
required_propositions: list[str] = Field(default_factory=list)
relation_edges: list[AnswerRelation] = Field(default_factory=list)
instance_carriers: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Replace card-only selection with lexicographic selection**

Maintain `cluster_use_counts` and `carrier_use_counts`. Rank eligible `(unit, card)` pairs by:

```python
(
    cluster_use_counts.get(profile.concept_cluster, 0),
    max(
        carrier_use_counts.get(carrier.normalized_name, 0)
        for carrier in profile.instance_carriers
        if carrier.role == "illustrative_context"
        and not carrier.authorized_by_syllabus
    ) if replaceable_carriers else 0,
    card_use_counts.get(card_id, 0),
    unit_use_counts.get(unit.unit_id, 0),
    unit.unit_id,
    card_id,
)
```

Reject duplicate `card_id`. For independent objective questions, reject a repeated cluster when a compatible unused cluster exists. If no distinct cluster/card capacity can satisfy the plan, raise `BlueprintValidationError` with a source-free diversity-capacity message.

- [ ] **Step 5: Run focused allocation tests and verify GREEN**

```powershell
python -m pytest backend/tests/unit/test_blueprint_allocation.py -q -k "concept_cluster or replaceable_instance or syllabus_authorized or unique_atomic_card"
```

Expected: the selected focused tests pass.

### Task 3: Block information dependencies before generation

**Files:**
- Modify: `backend/app/domain/generation/coverage.py`
- Modify: `backend/app/workflows/generation_graph.py`
- Test: `backend/tests/unit/test_coverage_planner.py`
- Test: `backend/tests/workflow/test_generation_graph.py`

- [ ] **Step 1: Write failing payload-isolation and preflight tests**

```python
def test_planning_payload_contains_semantic_relations_but_no_source_metadata():
    payload = compile_coverage_planning_payload(_items_with_relations(), _cards())

    item = payload["items"][0]
    assert item["concept_cluster"] == "network-protocol"
    assert item["answer_proposition"] == "protocol A uses path A"
    assert item["relation_edges"] == [{"kind": "component_of", "target": "protocol paths differ"}]
    assert "evidence_chunk_ids" not in repr(payload)
    assert "source" not in repr(payload)


def test_generation_graph_reassigns_before_generate_when_two_answers_reveal_third():
    gateway = CountingGateway()
    state = _state_with_component_component_summary_items_and_unused_cluster()

    result = build_generation_graph(gateway).invoke(state)

    assert gateway.generate_calls == len(state["plan_items"])
    assert result["directives"][2]["concept_cluster"] != "interface-paths"
```

- [ ] **Step 2: Run the two exact tests and verify RED**

```powershell
python -m pytest backend/tests/unit/test_coverage_planner.py::test_planning_payload_contains_semantic_relations_but_no_source_metadata backend/tests/workflow/test_generation_graph.py::test_generation_graph_reassigns_before_generate_when_two_answers_reveal_third -q
```

Expected: payload fields and pre-generation conflict handling are missing.

- [ ] **Step 3: Compile semantic profiles into the source-free planning payload**

Add only these fields per item:

```python
"concept_cluster": item.concept_cluster,
"answer_proposition": item.answer_proposition,
"required_propositions": item.required_propositions,
"relation_edges": [edge.model_dump(mode="json") for edge in item.relation_edges],
"instance_carriers": item.instance_carriers,
```

Do not add card IDs, evidence IDs, filenames, page numbers, or source text to model-visible card content.

- [ ] **Step 4: Add a pre-generation information-independence node**

Before `plan_whole_paper_coverage` calls the model, build profiles keyed by `item_index` and call `build_information_conflicts()`. Reuse the existing unused-card replacement mechanism, but candidates must be ranked by different cluster first, different illustrative carrier second, and semantic distance third. If no legal replacement exists, stop with a bounded `coverage_planning_failed` error rather than generating the conflicting set.

- [ ] **Step 5: Run the exact tests and verify GREEN**

```powershell
python -m pytest backend/tests/unit/test_coverage_planner.py::test_planning_payload_contains_semantic_relations_but_no_source_metadata backend/tests/workflow/test_generation_graph.py::test_generation_graph_reassigns_before_generate_when_two_answers_reveal_third -q
```

Expected: both tests pass and `generate_calls` shows no wasted preflight generation.

### Task 4: Populate semantic profiles during knowledge organization

**Files:**
- Modify: `backend/app/adapters/model/deepseek_semantic_extractors.py`
- Modify: `backend/scripts/build_real_material_demo.py`
- Test: `backend/tests/unit/test_deepseek_semantic_extractors.py`
- Test: `backend/tests/unit/test_real_material_demo.py`

- [ ] **Step 1: Write failing strict-schema tests**

```python
def test_consolidator_accepts_source_free_semantic_profile():
    response = _consolidation_response(
        card={
            "name": "接口兼容关系",
            "performance_statement": "比较两类接口",
            "assessable_content": ["两类接口采用不同路径前缀"],
            "concept_cluster": "接口兼容与调用",
            "answer_proposition": "两类接口采用不同路径前缀",
            "required_propositions": [],
            "relation_edges": [],
            "instance_carriers": [
                {
                    "normalized_name": "ExampleRuntime",
                    "carrier_type": "software",
                    "role": "illustrative_context",
                    "authorized_by_syllabus": False,
                    "replaceable": True,
                }
            ],
        }
    )

    units = _consolidate(response)

    assert units[0].cards[0].concept_cluster == "接口兼容与调用"


def test_real_material_atomic_fact_keeps_cluster_separate_from_source_location():
    unit = build_atomic_units(
        _point(),
        [{
            "evidence_chunk_id": "e1",
            "name": "接口关系",
            "assessable_content": ["两类接口采用不同路径前缀"],
            "concept_cluster": "接口兼容与调用",
            "answer_proposition": "两类接口采用不同路径前缀",
            "instance_carriers": [],
        }],
    )[0]

    card = unit.cards[0]
    assert card.concept_cluster == "接口兼容与调用"
    assert "e1" not in card.answer_proposition
```

- [ ] **Step 2: Run the exact tests and verify RED**

```powershell
python -m pytest backend/tests/unit/test_deepseek_semantic_extractors.py::test_consolidator_accepts_source_free_semantic_profile backend/tests/unit/test_real_material_demo.py::test_real_material_atomic_fact_keeps_cluster_separate_from_source_location -q
```

Expected: fields are not defined or not preserved.

- [ ] **Step 3: Extend the consolidation response schema**

Add the semantic fields to `_KnowledgeCardResponse`, using strict nested Pydantic models from `semantic_diversity.py`. Active cards must contain a non-empty `concept_cluster` and `answer_proposition`; excluded/material-only cards may omit them. Continue requiring exactly one assessable fact after normalization.

Update the consolidator contract to request source-free cluster, proposition, relations, and carrier role. This is a knowledge-organization contract, not a generation prompt restriction. The model must never receive or return a course-specific blacklist.

- [ ] **Step 4: Extend real-material fact extraction and blueprint compilation**

The fact response must include:

```json
{
  "concept_cluster": "source-independent concept family",
  "answer_proposition": "one independently gradable proposition",
  "required_propositions": [],
  "relation_edges": [],
  "instance_carriers": []
}
```

`build_atomic_units()` stores the fields on every split atomic card. `build_blueprint()` adds `card_semantic_profiles` to `BlueprintRequest` and keeps source location data only in the teacher-review snapshot.

- [ ] **Step 5: Run the exact tests and verify GREEN**

```powershell
python -m pytest backend/tests/unit/test_deepseek_semantic_extractors.py::test_consolidator_accepts_source_free_semantic_profile backend/tests/unit/test_real_material_demo.py::test_real_material_atomic_fact_keeps_cluster_separate_from_source_location -q
```

Expected: both tests pass.

### Task 5: Add domain-generic regression coverage and documentation

**Files:**
- Modify: `backend/tests/unit/test_semantic_diversity.py`
- Modify: `backend/tests/unit/test_blueprint_allocation.py`
- Modify: `backend/tests/workflow/test_generation_graph.py`
- Modify: `docs/superpowers/specs/2026-08-14-core-exam-system-development-design.md`

- [ ] **Step 1: Add cross-domain fixtures**

Add parameterized tests for network protocols, historical figures, chemical process steps, statistical software as a replaceable example, and a syllabus-authorized literary work. Assert behavior by relations and carrier roles, never by checking a prohibited-name list.

- [ ] **Step 2: Run only semantic-diversity related tests**

```powershell
python -m pytest backend/tests/unit/test_semantic_diversity.py backend/tests/unit/test_blueprint_allocation.py backend/tests/unit/test_coverage_planner.py backend/tests/workflow/test_generation_graph.py -q -k "cluster or carrier or information or answer_leak or semantic"
```

Expected: all selected tests pass.

- [ ] **Step 3: Update the core design document**

Add a version entry stating:

```text
全卷账本由 card_id 唯一性升级为原子卡、语义概念簇和实例载体三层约束；
生成前使用答案命题关系图阻止等价、单向蕴含和组合泄露；
任何具体实例重复限制都来自考纲授权、角色和可替代性，不来自课程专属禁词。
```

- [ ] **Step 4: Run targeted static checks**

```powershell
python -m ruff check backend/app/domain/generation/semantic_diversity.py backend/app/domain/knowledge/models.py backend/app/domain/blueprint/models.py backend/app/services/blueprint_service.py backend/app/domain/generation/coverage.py backend/app/workflows/generation_graph.py backend/app/adapters/model/deepseek_semantic_extractors.py backend/scripts/build_real_material_demo.py backend/tests/unit/test_semantic_diversity.py backend/tests/unit/test_blueprint_allocation.py backend/tests/unit/test_coverage_planner.py backend/tests/workflow/test_generation_graph.py backend/tests/unit/test_real_material_demo.py
```

Expected: `All checks passed!`

- [ ] **Step 5: Do not run a real generation automatically**

Report the local focused-test result and ask the user whether to spend model quota on one real-material generation. Do not run the full 537-test suite unless explicitly requested.
