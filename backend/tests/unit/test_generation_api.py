"""generation-runs API 测试：入口合同校验、排序返回、终检透出与网关降级。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def _slot(index, point="EP1"):
    return {
        "item_index": index, "question_type": "single_choice", "score": 2,
        "difficulty": "medium", "cognitive_level": "understand", "assessment_mode": "conceptual",
        "exam_point_id": point, "anchor_key": "A1", "unit_id": f"U-{point}", "card_id": f"C{index}",
        "coverage_atom": f"原子{index}", "answer_boundary": f"边界{index}",
        "performance_statement": "掌握某知识",
        "forbidden_context": {"atoms": [], "answer_cores": []},
    }


class FakeBatchGateway:
    def generate_batch(self, payload):
        return [
            {
                "item_index": spec.item_index, "question_type": spec.question_type,
                "stem": f"关于{spec.coverage_atom}的题干文本内容",
                "options": [spec.answer_boundary or "标准答案项", "干扰项一", "干扰项二", "干扰项三"],
                "answer": spec.answer_boundary or "标准答案项",
                "explanation": "解析", "difficulty": spec.difficulty,
            }
            for spec in payload.questions
        ]


class FailingBatchGateway:
    def generate_batch(self, payload):
        raise RuntimeError("model unavailable")


def _post_generation(client, contract):
    return client.post(
        "/api/v1/courses/course/generation-runs",
        json={"contract": contract, "knowledge_cards": {}},
    )


@pytest.mark.parametrize("missing_setting", ["deepseek_api_key", "deepseek_base_url", "deepseek_model"])
def test_generation_requires_complete_deepseek_configuration(monkeypatch, missing_setting):
    monkeypatch.setattr(settings, "deepseek_api_key", "configured-test-key")
    monkeypatch.setattr(settings, "deepseek_base_url", "https://deepseek.invalid/v1")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(settings, missing_setting, "")
    if hasattr(app.state, "generation_gateway"):
        del app.state.generation_gateway

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = _post_generation(client, [_slot(1)])

        assert response.status_code == 503
        assert response.json()["detail"] == "DeepSeek model is not configured"
    finally:
        if hasattr(app.state, "generation_gateway"):
            del app.state.generation_gateway


def test_generation_runs_returns_sorted_questions_with_final_check():
    app.state.generation_gateway = FakeBatchGateway()
    try:
        with TestClient(app) as client:
            response = _post_generation(
                client,
                [_slot(1), _slot(2), _slot(3), _slot(4, "EP2"), _slot(5, "EP2"), _slot(6, "EP2")],
            )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "candidate"
        assert [q["item_index"] for q in body["questions"]] == [1, 2, 3, 4, 5, 6]
        assert all(q["needs_review"] is False for q in body["questions"])
        assert body["final_check"]["passed"] is True
        assert body["model_call_count"] == 2  # 两个考点各成一批，各一次模型调用
        assert body["model"] == settings.deepseek_model
    finally:
        del app.state.generation_gateway


def test_generation_rejects_missing_contract():
    app.state.generation_gateway = FakeBatchGateway()
    try:
        with TestClient(app) as client:
            missing = client.post(
                "/api/v1/courses/course/generation-runs",
                json={"knowledge_cards": {}},
            )
            empty = _post_generation(client, [])

        assert missing.status_code == 422
        assert empty.status_code == 422
    finally:
        del app.state.generation_gateway


def test_generation_rejects_invalid_slot():
    app.state.generation_gateway = FakeBatchGateway()
    invalid = _slot(1)
    del invalid["coverage_atom"]  # 缺少必填原子 → 入口校验 422，不进图
    try:
        with TestClient(app) as client:
            response = _post_generation(client, [invalid])

        assert response.status_code == 422
        assert "contract invalid" in response.json()["detail"]
    finally:
        del app.state.generation_gateway


def test_generation_gateway_failure_returns_502():
    """批网关持续失败：图内已捕获降级 needs_review，API 正常 202 + 终检不通过，而非 502。"""
    app.state.generation_gateway = FailingBatchGateway()
    try:
        with TestClient(app) as client:
            response = _post_generation(client, [_slot(1), _slot(2), _slot(3)])

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["final_check"]["passed"] is False
        assert all(q["needs_review"] is True for q in body["questions"])
        # 一批一次调用 + 批缺失的 3 题各走一次单题重试（异常即断）= 4
        assert body["model_call_count"] == 4
    finally:
        del app.state.generation_gateway
