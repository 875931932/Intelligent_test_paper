from fastapi.testclient import TestClient
import redis

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


def test_database_status_disposes_engine_when_connection_fails(monkeypatch):
    disposed = False

    class Engine:
        def connect(self):
            raise OSError("database unavailable")

        def dispose(self):
            nonlocal disposed
            disposed = True

    monkeypatch.setattr(main, "create_engine", lambda *_args, **_kwargs: Engine())

    assert main._database_status() == "unavailable"
    assert disposed is True


def test_redis_status_closes_client_and_uses_bounded_timeouts(monkeypatch):
    closed = False
    captured_kwargs = {}

    class Client:
        def ping(self):
            raise OSError("redis unavailable")

        def close(self):
            nonlocal closed
            closed = True

    def from_url(_url, **kwargs):
        captured_kwargs.update(kwargs)
        return Client()

    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    monkeypatch.setattr(main, "settings", Settings(redis_url="redis://localhost:6379/0"))

    assert main._redis_status() == "unavailable"
    assert captured_kwargs["socket_connect_timeout"] > 0
    assert captured_kwargs["socket_timeout"] > 0
    assert closed is True
