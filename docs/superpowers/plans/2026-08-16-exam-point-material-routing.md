# Exam-point Material Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the real demonstration generate a paper without embedding every uploaded teaching-material chunk.

**Architecture:** The assessment outline first produces weighted exam points. Every teaching-material file is split locally, then a deterministic lexical preselection supplies a bounded candidate set to the existing DeepSeek evidence classifier. The classifier and consolidator remain the sole semantic admission path; the resulting source-bearing tree is still converted to source-free knowledge cards before generation.

**Tech Stack:** Python 3.12, existing DeepSeek semantic extractors, Pydantic, pytest.

## Verified design constraints

- Authority remains one-way: the assessment syllabus decides exam points and weights; the teaching syllabus only clarifies taught scope and depth; teaching materials and assignments only supply evidence constrained by those exam points.
- A knowledge card contains exactly one independently assessable atomic fact. One evidence block may support multiple atomic cards, so evidence-to-card is one-to-many; repeated or equivalent cards are consolidated without losing their source links.
- Exam-point weight sets a target fact capacity. Normalize input to percentage points at the parsing boundary (`15` means 15%; a legacy `0.15` is multiplied by 100 only when its raw unit is explicitly marked or confirmed as a 0—1 ratio), then apply `max(4, min(20, ceil(weight_percent * 0.7)))`; it is configurable and calibratable, not a permanent domain constant.
- Lexical or vector retrieval only bounds candidates. Full-corpus vectorization and course-specific forbidden-word lists are not root solutions for scope pollution, duplicate facts, or assessment imbalance.
- The organization layer keeps filenames, pages, sections, block IDs, and evidence IDs for teacher review. The generation payload is rebuilt from a whitelist and contains only source-free knowledge; none of those source fields may cross the generation boundary.
- Empty model content, non-JSON output, a JSON top level that is not an object, and schema/structure failure are distinct retryable protocol failures. The formal gateway defaults to 4 requests with 1/2/4-second backoff; the demo curation requester may use 2 requests to bound prototype cost. Every failure records a safe machine-readable reason; an exhausted bad result is not cached or converted into a fallback knowledge card.
- On Windows, demo snapshot updates use a same-directory temporary file plus atomic replacement. Transient locks on `snapshot`/`pipeline.json` receive at most 5 attempts with 50/100/200/400-millisecond backoff; failure must leave the previous complete JSON intact and report the lock reason.

---

### Task 1: Add bounded lexical candidate selection for the demo

**Files:**
- Modify: `backend/scripts/build_real_material_demo.py`
- Test: `backend/tests/unit/test_staging_retrieval.py`

- [ ] **Step 1: Write the failing test**

```python
def test_lexical_preselection_keeps_top_matches_without_embeddings():
    point = _exam_point(retrieval_intent="检索链路诊断")
    matching = StagingChunk(id="matching", material_version_id="m", content="检索链路诊断需要分析召回遗漏")
    noise = StagingChunk(id="noise", material_version_id="m", content="安装软件并提交截图")

    result = select_lexical_candidates(point, [noise, matching], limit=3)

    assert [chunk.id for chunk in result] == ["matching", "noise"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/unit/test_staging_retrieval.py -q`

Expected: FAIL because `select_lexical_candidates` does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
def select_lexical_candidates(point: ExamPoint, chunks: list[StagingChunk], *, limit: int) -> list[StagingChunk]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    return sorted(
        chunks,
        key=lambda chunk: (-lexical_overlap(point.retrieval_intent, chunk.content), chunk.id),
    )[:limit]
```

Use this selector only in `build_real_material_demo.py`; preserve `retrieve_for_exam_point` as the optional vector-retrieval service for the formal system.

Apply the per-exam-point capacity target before scheduling classifier work, and validate after consolidation that each retained card contains one atomic fact. Capacity limits scheduling and coverage; they must not truncate a multi-fact card instead of splitting it or discard valid source links shared by several cards.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest backend/tests/unit/test_staging_retrieval.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scripts/build_real_material_demo.py backend/tests/unit/test_staging_retrieval.py docs/superpowers/plans/2026-08-16-exam-point-material-routing.md
git commit -m "feat: route demo material without full embeddings"
```

### Task 2: Make the real pipeline observable and rerun it

**Files:**
- Modify: `backend/scripts/build_real_material_demo.py`

- [ ] **Step 1: Write a minimal pipeline assertion**

```python
snapshot = json.loads((ROOT / "frontend/public/demo/pipeline.json").read_text(encoding="utf-8"))
assert snapshot["staging"]["retrieval_mode"] == "lexical_preselection"
assert snapshot["knowledge_organization"]["pair_count"] == snapshot["knowledge_organization"]["pair_total"] == 21
```

- [ ] **Step 2: Execute the real pipeline**

Run: `python backend/scripts/build_real_material_demo.py`

Expected: the snapshot advances through `knowledge_organization`, `blueprint`, `generating`, and `complete` without calling the embedding endpoint.

- [ ] **Step 3: Verify generation isolation**

```python
assert_source_free(snapshot["source_free_generation_payloads"], path="source_free_generation_payloads")
```

- [ ] **Step 4: Verify Windows-safe snapshot replacement**

Exercise a transient-lock fixture around `frontend/public/demo/pipeline.json`. The writer must retry a bounded number of times, either complete the atomic replacement or report the lock reason, and never expose partial JSON.

- [ ] **Step 5: Commit verified output-independent code only**

```bash
git add backend/scripts/build_real_material_demo.py
git commit -m "feat: expose lexical material routing progress"
```
