"""模型端点测试（/api/v1/admin/monitoring）：聚合正确性 + 范围纪律 + 脱敏投影。

数据面：2 单位 × 2 教师 × 2 课程，4 条 model_calls（其中 1 条属外单位）。
- 平台管理员看全局 4 条；org_admin 只看本单位课程的 3 条；
- details 含 response / raw_response_snapshot 敏感键，断言永不出后端（§10.4）。
时间种子按 now 回填（默认窗口近 7 天即可命中）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.schema import (
    Base,
    Course,
    LlmModel,
    LlmProvider,
    Organization,
    User,
    model_calls,
)
from app.db.session import get_session
from app.main import app
from app.services.auth_service import hash_password

NAIVE = datetime.now(UTC).replace(tzinfo=None)


def _iso_url(dt: datetime) -> str:
    """URL 安全的 ISO：query 中裸 `+` 会被解码成空格（RFC 3986），改用 Z 后缀。"""

    return dt.isoformat().replace("+00:00", "Z")


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
                Organization(id="school1", kind="school", name="第一大学"),
                Organization(id="college1", kind="college", name="计算机学院", parent_id="school1"),
                Organization(id="school2", kind="school", name="第二大学"),
            ]
        )
        # 分段 flush：FK 强制开启时 UOW 不保证跨表插入序（组织→用户→课程→数据）
        session.flush()
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
                    display_name="第一大学管理员",
                    role="org_admin",
                    organization_id="school1",
                ),
                User(
                    id="teacher1",
                    username="teacher1",
                    password_hash=hash_password("123456"),
                    display_name="张老师",
                    role="teacher",
                    organization_id="college1",
                ),
                User(
                    id="teacher2",
                    username="teacher2",
                    password_hash=hash_password("123456"),
                    display_name="李老师",
                    role="teacher",
                    organization_id="school2",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Course(id="courseA", owner_id="teacher1", slug="course-a", name="课程A"),
                Course(id="courseB", owner_id="teacher2", slug="course-b", name="课程B"),
            ]
        )
        session.flush()
        # 价格种子：model-a 两价齐备（计价），model-b 无价（不计价）
        session.add(LlmProvider(id="prov1", name="供应商", base_url="https://x/v1", api_key="k"))
        session.flush()
        session.add_all(
            [
                LlmModel(
                    id="ma",
                    provider_id="prov1",
                    model_id="model-a",
                    display_name="Model A",
                    price_input_per_mtk=2.0,
                    price_output_per_mtk=8.0,
                ),
                LlmModel(id="mb", provider_id="prov1", model_id="model-b", display_name="Model B"),
            ]
        )
        session.flush()
        for row in (
            {
                "id": "c1",
                "course_id": "courseA",
                "stage": "generation",
                "provider": "stepfun",
                "model": "model-a",
                "status": "succeeded",
                "prompt_hash": "ph1",
                "input_tokens": 1000,
                "output_tokens": 500,
                "duration_ms": 1000,
                "details": {
                    "attempt_count": 1,
                    "retry_count": 0,
                    "response": {"secret": "DO_NOT_LEAK"},
                },
                "created_at": NAIVE - timedelta(hours=2),
            },
            {
                "id": "c2",
                "course_id": "courseA",
                "stage": "generation",
                "provider": "stepfun",
                "model": "model-a",
                "status": "failed",
                "error_code": "llm_transport_error",
                "error_message": "LLM request failed",
                "input_tokens": 2000,
                "output_tokens": 0,
                "duration_ms": 2000,
                "details": {
                    "attempt_count": 3,
                    "retry_count": 2,
                    "error_code": "llm_transport_error",
                    "raw_response_snapshot": "RAW_SECRET",
                },
                "created_at": NAIVE - timedelta(hours=3),
            },
            {
                "id": "c3",
                "course_id": "courseA",
                "stage": "extract",
                "provider": "stepfun",
                "model": "model-b",
                "status": "succeeded",
                "input_tokens": 100,
                "output_tokens": 100,
                "duration_ms": 500,
                "details": {},
                "created_at": NAIVE - timedelta(hours=26),
            },
            {
                "id": "c4",
                "course_id": "courseB",
                "stage": "generation",
                "provider": "stepfun",
                "model": "model-a",
                "status": "succeeded",
                "input_tokens": 4000,
                "output_tokens": 1000,
                "duration_ms": 3000,
                "details": {},
                "created_at": NAIVE - timedelta(hours=4),
            },
        ):
            session.execute(model_calls.insert().values(**row))
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


# ── 准入与范围 ────────────────────────────────────────────────────────────────


def test_access_matrix(env):
    assert TestClient(app).get("/api/v1/admin/monitoring/overview").status_code == 401
    assert _client("teacher1").get("/api/v1/admin/monitoring/overview").status_code == 403
    assert _client("orgadmin1").get("/api/v1/admin/monitoring/overview").status_code == 200
    assert _client("admin").get("/api/v1/admin/monitoring/overview").status_code == 200


def test_overview_global_aggregates(env):
    body = _client("admin").get("/api/v1/admin/monitoring/overview").json()
    assert body["total_calls"] == 4
    assert body["succeeded"] == 3
    assert body["failed"] == 1
    assert body["success_rate"] == 0.75
    assert body["input_tokens"] == 7100
    assert body["output_tokens"] == 1600
    assert body["total_tokens"] == 8700
    # 成本只计 model-a（两价齐备）：7000×2/1e6 + 1500×8/1e6 = 0.026；model-b 无价不计
    assert body["cost_estimate"] == 0.026
    assert body["avg_duration_ms"] == 1625
    # SQLite 无分位数（方言分支降级）
    assert body["p50_duration_ms"] is None
    assert body["p95_duration_ms"] is None


def test_org_admin_sees_own_unit_only(env):
    body = _client("orgadmin1").get("/api/v1/admin/monitoring/overview").json()
    assert body["total_calls"] == 3  # courseB（第二大学）不计入
    assert body["succeeded"] == 2
    assert body["input_tokens"] == 3100

    calls = _client("orgadmin1").get("/api/v1/admin/monitoring/calls").json()
    ids = {item["id"] for item in calls["items"]}
    assert ids == {"c1", "c2", "c3"}
    assert "c4" not in ids

    # 外单位明细统一 404（不泄露存在性）
    resp = _client("orgadmin1").get("/api/v1/admin/monitoring/calls/c4")
    assert resp.status_code == 404


# ── 趋势与分组 ────────────────────────────────────────────────────────────────


def test_timeseries_hourly_buckets(env):
    body = _client("admin").get("/api/v1/admin/monitoring/timeseries?bucket=hour").json()
    assert body
    buckets = [row["bucket"] for row in body]
    assert buckets == sorted(buckets)  # 按时间升序
    assert sum(row["calls"] for row in body) == 4
    first = next(row for row in body if row["calls"] > 0)
    assert 0.0 <= first["success_rate"] <= 1.0
    assert first["input_tokens"] > 0


@pytest.mark.parametrize("group_by,expected", [("model", {"model-a": 3, "model-b": 1}),
                                               ("stage", {"generation": 3, "extract": 1}),
                                               ("status", {"succeeded": 3, "failed": 1}),
                                               ("error_code", {"llm_transport_error": 1})])
def test_breakdown_dimensions(env, group_by, expected):
    body = _client("admin").get(f"/api/v1/admin/monitoring/breakdown?group_by={group_by}").json()
    assert {row["key"]: row["calls"] for row in body} == expected
    # 每组带成本与均耗（error_code 组只有失败行）
    for row in body:
        assert "cost_estimate" in row
        assert "avg_duration_ms" in row


def test_breakdown_by_model_includes_cost(env):
    body = _client("admin").get("/api/v1/admin/monitoring/breakdown?group_by=model").json()
    by_key = {row["key"]: row for row in body}
    assert by_key["model-a"]["cost_estimate"] == 0.026
    assert by_key["model-b"]["cost_estimate"] == 0.0


def test_invalid_bucket_and_group_by_rejected(env):
    admin = _client("admin")
    assert admin.get("/api/v1/admin/monitoring/timeseries?bucket=week").status_code == 400
    assert admin.get("/api/v1/admin/monitoring/breakdown?group_by=nope").status_code == 400


# ── 明细与脱敏 ────────────────────────────────────────────────────────────────


def test_calls_pagination_and_filters(env):
    admin = _client("admin")
    page1 = admin.get("/api/v1/admin/monitoring/calls?page=1&page_size=2").json()
    assert page1["total"] == 4
    assert len(page1["items"]) == 2
    # 近→旧排序：c1(2h) 或 c4(4h)… 最新在前
    assert page1["items"][0]["id"] == "c1"

    failed = admin.get("/api/v1/admin/monitoring/calls?status=failed").json()
    assert [i["id"] for i in failed["items"]] == ["c2"]

    by_model = admin.get("/api/v1/admin/monitoring/calls?model=model-b").json()
    assert [i["id"] for i in by_model["items"]] == ["c3"]

    by_stage = admin.get("/api/v1/admin/monitoring/calls?stage=extract").json()
    assert [i["id"] for i in by_stage["items"]] == ["c3"]


def test_call_detail_projects_details_whitelist(env):
    admin = _client("admin")

    ok = admin.get("/api/v1/admin/monitoring/calls/c1").json()
    assert ok["prompt_hash"] == "ph1"
    assert ok["details"]["attempt_count"] == 1
    assert ok["details"]["retry_count"] == 0
    assert "response" not in ok["details"]  # §10.4 永不回传
    assert "DO_NOT_LEAK" not in admin.get("/api/v1/admin/monitoring/calls/c1").text

    failed = admin.get("/api/v1/admin/monitoring/calls/c2").json()
    assert failed["details"]["last_error_code"] == "llm_transport_error"
    assert failed["details"]["attempt_count"] == 3
    assert "raw_response_snapshot" not in failed["details"]
    assert "RAW_SECRET" not in admin.get("/api/v1/admin/monitoring/calls/c2").text

    assert admin.get("/api/v1/admin/monitoring/calls/ghost").status_code == 404


# ── 区间校验 ──────────────────────────────────────────────────────────────────


def test_range_validation(env):
    admin = _client("admin")
    now = datetime.now(UTC)
    # from >= to → 400
    resp = admin.get(
        f"/api/v1/admin/monitoring/overview?from={_iso_url(now)}&to={_iso_url(now - timedelta(days=1))}"
    )
    assert resp.status_code == 400
    # 跨度 > 92 天 → 400
    resp = admin.get(
        f"/api/v1/admin/monitoring/overview?from={_iso_url(now - timedelta(days=93))}&to={_iso_url(now)}"
    )
    assert resp.status_code == 400
    # 非法格式 → 422（FastAPI 解析失败）
    resp = admin.get("/api/v1/admin/monitoring/overview?from=not-a-date")
    assert resp.status_code == 422


def test_empty_window_returns_zero_overview(env):
    admin = _client("admin")
    now = datetime.now(UTC)
    body = admin.get(
        f"/api/v1/admin/monitoring/overview?from={_iso_url(now - timedelta(days=90))}"
        f"&to={_iso_url(now - timedelta(days=89))}"
    ).json()
    assert body["total_calls"] == 0
    assert body["success_rate"] == 0.0
    assert body["avg_duration_ms"] is None
    assert body["cost_estimate"] == 0.0
