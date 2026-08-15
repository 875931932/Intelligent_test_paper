from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db.session import get_session
from app.domain.generation.structure_signature import build_structure_signature
from app.main import app
from app.api.v1 import generation as generation_api


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
            response = client.post(
                "/api/v1/courses/course/generation-runs",
                json={"plan_items": [], "knowledge_cards": {}},
            )

        assert response.status_code == 503
        assert response.json()["detail"] == "DeepSeek model is not configured"
    finally:
        if hasattr(app.state, "generation_gateway"):
            del app.state.generation_gateway


def test_generation_loads_recent_course_signatures_into_initial_graph_state(monkeypatch):
    recent = build_structure_signature(
        archetype="fault_diagnosis",
        material_form="symptom_list",
        cognitive_sequence=["analyze", "apply"],
        subquestion_actions=["定位原因", "提出修正"],
        answer_boundaries=["原因", "修正措施"],
    )
    session_sentinel = object()
    loader_calls = []

    class Gateway:
        def __init__(self):
            self.planning_payload = None

        def plan_coverage(self, payload):
            self.planning_payload = payload
            return {
                "directives": [
                    {
                        "item_index": 1,
                        "coverage_atom": "比较候选方案并作出选择",
                        "answer_boundary": "比较依据和选择结论",
                        "cognitive_level": "evaluate",
                        "comprehensive_archetype": "comparative_decision",
                        "material_form": "constraint_table",
                        "cognitive_sequence": ["analyze", "evaluate"],
                        "subquestion_count_range": [2, 3],
                        "subquestion_actions": ["比较方案", "作出选择"],
                        "answer_boundaries": ["比较依据", "选择结论"],
                    }
                ]
            }

        def generate(self, payload):
            return {
                "stem": "比较方案 A 与 B",
                "subquestions": [
                    {"action": "比较方案", "prompt": "比较", "answer_boundary": "比较依据", "answer": "依据", "rubric": ["完整"]},
                    {"action": "作出选择", "prompt": "选择", "answer_boundary": "选择结论", "answer": "A", "rubric": ["合理"]},
                ],
                "answer": "选择 A",
                "explanation": "按约束比较",
                "rubric": ["比较与选择"],
            }

        def audit_paper(self, payload):
            return {"conflicts": []}

    def override_session():
        yield session_sentinel

    def fake_loader(session, course_id, paper_limit=5):
        loader_calls.append((session, course_id, paper_limit))
        return [recent]

    gateway = Gateway()
    app.state.generation_gateway = gateway
    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(generation_api, "load_recent_structure_signatures", fake_loader)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/courses/course/generation-runs",
                json={
                    "plan_items": [
                        {
                            "item_index": 1,
                            "question_type": "comprehensive",
                            "score": 10,
                            "anchor_key": "anchor",
                            "unit_id": "unit",
                            "card_id": "card",
                            "cognitive_level": "evaluate",
                            "assessment_mode": "problem_solving",
                        }
                    ],
                    "knowledge_cards": {
                        "card": {
                            "performance_statement": "能够比较并选择方案",
                            "assessable_content": ["方案比较"],
                            "prompt_material": ["方案 A 与 B"],
                            "scope_boundary": {},
                        }
                    },
                },
            )

        assert response.status_code == 202, response.text
        assert loader_calls == [(session_sentinel, "course", 5)]
        summaries = gateway.planning_payload.global_policy["recent_comprehensive_structure_signatures"]
        assert summaries == [recent.model_dump()]
    finally:
        app.dependency_overrides.pop(get_session, None)
        if hasattr(app.state, "generation_gateway"):
            del app.state.generation_gateway
