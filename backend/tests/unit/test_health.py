import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from app import main
from app.config import Settings


def test_health_endpoint_reports_all_required_component_states(monkeypatch):
    monkeypatch.setattr(main, "settings", Settings(mineru_api_token="", deepseek_api_key=""))
    monkeypatch.setattr(main, "_database_status", lambda: "unavailable")
    monkeypatch.setattr(main, "_redis_status", lambda: "unavailable")

    response = TestClient(main.app).get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "api": "ok",
        "postgresql": "unavailable",
        "redis": "unavailable",
        "mineru": "not_configured",
        "deepseek": "not_configured",
    }


def test_health_endpoint_reports_configured_external_providers_without_calling_them(monkeypatch):
    monkeypatch.setattr(main, "settings", Settings(mineru_api_token="mineru-token", deepseek_api_key="deepseek-key"))
    monkeypatch.setattr(main, "_database_status", lambda: "ok")
    monkeypatch.setattr(main, "_redis_status", lambda: "ok")

    response = TestClient(main.app).get("/api/v1/health")
    assert response.status_code == 200
    payload = response.json()

    assert payload["api"] == "ok"
    assert payload["postgresql"] == "ok"
    assert payload["redis"] == "ok"
    assert payload["mineru"] == "configured"
    assert payload["deepseek"] == "configured"
