"""模型管理端点测试（/api/v1/admin/llm）：准入 + CRUD + 一键切换 + 密钥掩码。

准入矩阵：匿名 401 / 教师 403 / 单位管理员 403（平台专属）/ 平台管理员 200。
连通性测试不打真实网络：成功路径 monkeypatch _probe，失败路径用 127.0.0.1
关闭端口（瞬时拒连），未配置路径用清空 settings 触发前置校验。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.schema import Base, User
from app.db.session import get_session
from app.main import app
from app.services import llm_admin_service
from app.services.auth_service import hash_password


@pytest.fixture
def env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                User(
                    id="admin",
                    username="admin",
                    password_hash=hash_password("123456"),
                    display_name="系统管理员",
                    role="admin",
                ),
                User(
                    id="orgadmin1",
                    username="orgadmin1",
                    password_hash=hash_password("123456"),
                    display_name="学院管理员",
                    role="org_admin",
                ),
                User(
                    id="t1",
                    username="teacher1",
                    password_hash=hash_password("123456"),
                    display_name="张老师",
                    role="teacher",
                ),
            ]
        )
        session.commit()

    def override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        yield factory
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _client(username: str) -> TestClient:
    runner = TestClient(app)
    login = runner.post("/api/v1/auth/login", json={"username": username, "password": "123456"})
    assert login.status_code == 200, login.text
    return TestClient(app, headers={"Authorization": "Bearer " + login.json()["token"]})


# ── 准入 ─────────────────────────────────────────────────────────────────────


def test_access_matrix(env):
    assert TestClient(app).get("/api/v1/admin/llm/providers").status_code == 401
    assert _client("teacher1").get("/api/v1/admin/llm/providers").status_code == 403
    # 模型管理是平台专属：单位管理员也 403
    assert _client("orgadmin1").get("/api/v1/admin/llm/providers").status_code == 403
    assert _client("orgadmin1").put(
        "/api/v1/admin/llm/bindings/default", json={"model_id": "x"}
    ).status_code == 403
    assert _client("admin").get("/api/v1/admin/llm/providers").status_code == 200


# ── 供应商 ───────────────────────────────────────────────────────────────────


def test_provider_crud_and_key_never_leaks(env):
    admin = _client("admin")
    created = admin.post(
        "/api/v1/admin/llm/providers",
        json={
            "name": "StepFun",
            "base_url": "https://api.stepfun.com/step_plan/v1",
            "api_key": "sk-super-secret-1234567890",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert "sk-super-secret" not in created.text  # 明文绝不回传
    assert body["api_key_masked"].startswith("sk-")
    assert body["api_key_masked"].endswith("890")
    provider_id = body["id"]

    listed = admin.get("/api/v1/admin/llm/providers").json()
    assert len(listed) == 1
    assert "sk-super-secret" not in str(listed)

    patched = admin.patch(
        f"/api/v1/admin/llm/providers/{provider_id}",
        json={"status": "disabled", "remark": "备用渠道"},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "disabled"

    missing = admin.patch("/api/v1/admin/llm/providers/nope", json={"name": "x"})
    assert missing.status_code == 404


def test_provider_requires_key_or_env(env):
    admin = _client("admin")
    resp = admin.post(
        "/api/v1/admin/llm/providers",
        json={"name": "无密钥", "base_url": "https://x.example/v1"},
    )
    assert resp.status_code == 400, resp.text


# ── 型号 ─────────────────────────────────────────────────────────────────────


def _create_provider(admin, base_url: str = "https://api.example/v1") -> str:
    resp = admin.post(
        "/api/v1/admin/llm/providers",
        json={"name": "供应商", "base_url": base_url, "api_key": "sk-test-key-000111222"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_model_crud_and_duplicate_conflict(env):
    admin = _client("admin")
    provider_id = _create_provider(admin)

    created = admin.post(
        "/api/v1/admin/llm/models",
        json={"provider_id": provider_id, "model_id": "step-x", "display_name": "Step X"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["provider_name"] == "供应商"

    dup = admin.post(
        "/api/v1/admin/llm/models",
        json={"provider_id": provider_id, "model_id": "step-x", "display_name": "重复"},
    )
    assert dup.status_code == 409, dup.text

    bad = admin.post(
        "/api/v1/admin/llm/models",
        json={"provider_id": "ghost", "model_id": "m", "display_name": "M"},
    )
    assert bad.status_code == 404, bad.text

    model_pk = created.json()["id"]
    patched = admin.patch(
        f"/api/v1/admin/llm/models/{model_pk}",
        json={
            "display_name": "Step X (改)",
            "profile": {"thinking_style": "reasoning_effort", "supports_json_schema": True},
        },
    )
    assert patched.status_code == 200
    assert patched.json()["display_name"] == "Step X (改)"
    assert patched.json()["profile"]["supports_json_schema"] is True


# ── 用途绑定（一键切换） ──────────────────────────────────────────────────────


def test_binding_switch_and_guards(env):
    admin = _client("admin")
    provider_id = _create_provider(admin)
    model_pk = admin.post(
        "/api/v1/admin/llm/models",
        json={"provider_id": provider_id, "model_id": "step-x", "display_name": "Step X"},
    ).json()["id"]

    # 初始：测试库无绑定（conftest 关 DB 查询）→ 来源为 .env
    bindings = admin.get("/api/v1/admin/llm/bindings").json()
    assert [b["purpose"] for b in bindings] == [
        "default", "extract", "classify", "consolidate", "generation",
    ]
    assert all(b["source"] != "db" for b in bindings)

    # 一键切换 default → step-x
    switched = admin.put(
        "/api/v1/admin/llm/bindings/default", json={"model_id": model_pk}
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["source"] == "db"
    assert switched.json()["model"]["model_id"] == "step-x"

    bindings = admin.get("/api/v1/admin/llm/bindings").json()
    by_purpose = {b["purpose"]: b for b in bindings}
    assert by_purpose["default"]["source"] == "db"
    # 未细分用途回落 db default（生效视图标 via=default）
    assert by_purpose["classify"]["source"] == "db"
    assert by_purpose["classify"]["model"]["model_id"] == "step-x"

    # 守卫：非法 purpose / 不存在型号 / 停用型号
    assert admin.put(
        "/api/v1/admin/llm/bindings/bogus", json={"model_id": model_pk}
    ).status_code == 400
    assert admin.put(
        "/api/v1/admin/llm/bindings/default", json={"model_id": "ghost"}
    ).status_code == 404
    admin.patch(f"/api/v1/admin/llm/models/{model_pk}", json={"status": "disabled"})
    refused = admin.put("/api/v1/admin/llm/bindings/default", json={"model_id": model_pk})
    assert refused.status_code == 400, refused.text


def test_delete_model_guarded_by_binding(env):
    admin = _client("admin")
    provider_id = _create_provider(admin)
    m1 = admin.post(
        "/api/v1/admin/llm/models",
        json={"provider_id": provider_id, "model_id": "m1", "display_name": "M1"},
    ).json()["id"]
    m2 = admin.post(
        "/api/v1/admin/llm/models",
        json={"provider_id": provider_id, "model_id": "m2", "display_name": "M2"},
    ).json()["id"]

    admin.put("/api/v1/admin/llm/bindings/default", json={"model_id": m1})
    # 被绑定引用 → 409（停用取代删除）
    assert admin.delete(f"/api/v1/admin/llm/models/{m1}").status_code == 409

    # 改绑后可真删
    admin.put("/api/v1/admin/llm/bindings/default", json={"model_id": m2})
    assert admin.delete(f"/api/v1/admin/llm/models/{m1}").status_code == 204
    assert admin.delete(f"/api/v1/admin/llm/models/{m1}").status_code == 404
    assert admin.delete("/api/v1/admin/llm/models/ghost").status_code == 404


# ── 连通性测试 ────────────────────────────────────────────────────────────────


def test_connection_probe_success_via_stub(env, monkeypatch):
    admin = _client("admin")
    provider_id = _create_provider(admin, base_url="https://api.example/v1")
    model_pk = admin.post(
        "/api/v1/admin/llm/models",
        json={"provider_id": provider_id, "model_id": "step-x", "display_name": "X"},
    ).json()["id"]

    monkeypatch.setattr(
        llm_admin_service,
        "_probe",
        lambda base_url, api_key, model: {"ok": True, "latency_ms": 42},
    )
    resp = admin.post("/api/v1/admin/llm/test", json={"model_id": model_pk})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "latency_ms": 42}


def test_connection_probe_unreachable_endpoint(env):
    admin = _client("admin")
    # 127.0.0.1:9 是关闭端口：瞬时拒连，不产生外网流量
    provider_id = _create_provider(admin, base_url="http://127.0.0.1:9")
    resp = admin.post("/api/v1/admin/llm/test", json={"provider_id": provider_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]


def test_connection_probe_unconfigured_settings(env, monkeypatch):
    from app.config import settings

    admin = _client("admin")
    monkeypatch.setattr(settings, "llm_api_key", "")
    resp = admin.post("/api/v1/admin/llm/test", json={})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "未配置" in resp.json()["error"]
