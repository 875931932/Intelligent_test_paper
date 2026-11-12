"""检索评测打分的纯函数测试。"""

from __future__ import annotations

from app.services.knowledge_evaluation import score_case, summarize


def _hit(**overrides) -> dict:
    base = {
        "document_id": "doc-1",
        "document_title": "数据结构讲义",
        "version_no": 2,
        "page_index": 3,
        "block_index": 0,
        "heading_path": ["第三章", "树"],
        "text": "二叉树每个结点最多有两个子结点。",
        "score": 1.0,
    }
    base.update(overrides)
    return base


def test_score_case_matches_document_and_version():
    case = {
        "id": "q1",
        "question": "二叉树",
        "expected": [
            {"document_id": "doc-1", "version_no": 2, "page_index": 3, "quote": "最多有两个子结点"}
        ],
        "tags": ["dev"],
    }
    scored = score_case(case, [_hit(), _hit(document_id="doc-2")])
    assert scored["evidence_recall"] == 1.0
    assert scored["reciprocal_rank"] == 1.0
    assert scored["quote_locatable"] == 1.0
    assert scored["citation_support"] is None


def test_partial_evidence_and_block_index():
    case = {
        "id": "q-partial",
        "question": "两处证据",
        "expected": [
            {"document_id": "doc-1", "block_index": 0},
            {"document_id": "doc-1", "block_index": 9},
        ],
    }
    hit = _hit(block_index=0)
    scored = score_case(case, [hit])
    assert scored["evidence_recall"] == 0.5
    assert scored["reciprocal_rank"] == 1.0

    wrong_block = score_case(
        {"id": "q-block", "expected": [{"document_id": "doc-1", "block_index": 5}]},
        [_hit(block_index=0)],
    )
    assert wrong_block["evidence_recall"] == 0.0


def test_version_mismatch_is_not_a_hit():
    case = {
        "id": "q2",
        "question": "二叉树",
        "expected": [{"document_id": "doc-1", "version_no": 3}],
    }
    scored = score_case(case, [_hit(version_no=2)])
    assert scored["evidence_recall"] == 0.0
    assert scored["reciprocal_rank"] == 0.0


def test_refusal_case_only_counts_when_empty():
    case = {"id": "q3", "question": "库外问题", "must_refuse": True, "tags": ["refusal"]}
    assert score_case(case, [])["refusal_correct"] == 1.0
    assert score_case(case, [_hit()])["refusal_correct"] == 0.0


def test_summary_ignores_not_applicable_metrics():
    results = [
        score_case({"id": "a", "expected": [{"document_id": "doc-1"}]}, [_hit()]),
        score_case({"id": "b", "must_refuse": True}, []),
    ]
    summary = summarize(results, [10.0, 30.0])
    assert summary["cases"] == 2
    assert summary["evidence_recall"] == 1.0
    assert summary["refusal_accuracy"] == 1.0
    assert summary["latency_ms"]["p50"] == 10.0
    assert summary["by_tag"]["untagged"]["cases"] == 2
