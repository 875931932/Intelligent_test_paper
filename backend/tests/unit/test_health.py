from app import main
from app.config import Settings


def test_health_payload_reports_all_required_component_states(monkeypatch):
    monkeypatch.setattr(main, "settings", Settings(mineru_api_token="", deepseek_api_key=""))
    monkeypatch.setattr(main, "_database_status", lambda: "unavailable")
    monkeypatch.setattr(main, "_redis_status", lambda: "unavailable")

    assert main.health_payload() == {
        "api": "ok",
        "database": "unavailable",
        "redis": "unavailable",
        "mineru": "not_configured",
        "deepseek": "not_configured",
    }


def test_health_payload_reports_configured_external_providers_without_calling_them(monkeypatch):
    monkeypatch.setattr(main, "settings", Settings(mineru_api_token="mineru-token", deepseek_api_key="deepseek-key"))
    monkeypatch.setattr(main, "_database_status", lambda: "ok")
    monkeypatch.setattr(main, "_redis_status", lambda: "ok")

    payload = main.health_payload()

    assert payload["api"] == "ok"
    assert payload["database"] == "ok"
    assert payload["redis"] == "ok"
    assert payload["mineru"] == "configured"
    assert payload["deepseek"] == "configured"
