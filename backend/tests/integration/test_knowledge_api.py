from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.mark.parametrize("missing_setting", ["deepseek_api_key", "deepseek_base_url", "deepseek_model"])
def test_knowledge_requires_complete_deepseek_configuration(monkeypatch, missing_setting):
    monkeypatch.setattr(settings, "embedding_api_key", "embedding-key")
    monkeypatch.setattr(settings, "embedding_base_url", "https://embedding.invalid/v1")
    monkeypatch.setattr(settings, "embedding_model", "embedding-model")
    monkeypatch.setattr(settings, "deepseek_api_key", "configured-test-key")
    monkeypatch.setattr(settings, "deepseek_base_url", "https://deepseek.invalid/v1")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(settings, missing_setting, "")
    for state_name in (
        "organization_embedder",
        "semantic_json_client",
        "exam_point_evidence_classifier",
        "exam_point_knowledge_consolidator",
    ):
        if hasattr(app.state, state_name):
            delattr(app.state, state_name)

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/v1/courses/course/organization-runs",
                json={"material_version_ids": ["material-v1"]},
            )

        assert response.status_code == 503
        assert response.json()["detail"] == "semantic classifier is not configured"
    finally:
        for state_name in (
            "organization_embedder",
            "semantic_json_client",
            "exam_point_evidence_classifier",
            "exam_point_knowledge_consolidator",
        ):
            if hasattr(app.state, state_name):
                delattr(app.state, state_name)
