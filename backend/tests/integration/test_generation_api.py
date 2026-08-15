from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


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
