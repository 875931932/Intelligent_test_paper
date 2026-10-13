"""概览 / 知识目录两页首屏查询的性能回归测试。

2026-10-13 优化的三类慢查询模式（两页加载慢的根因）：

1. ``GET /materials`` —— 曾逐料查最新版本与解析状态（2+2N 次网络往返）；
   现整页固定少量查询。用引擎级 SQL 计数器锁死查询数上限。
2. ``GET /organization-runs/latest`` 与 ``/{run_id}`` —— 曾整行返回
   ``input_snapshot``（全量冻结输入，MB 级；前端只消费状态字段，构建
   轮询 3 秒一次）。现读路径瘦身为轻量列。
3. ``GET /published-knowledge?summary=true`` —— 课程概览页只显示卡片/证据
   计数，不再拉整树（cards 字典是该接口主体体积）；计数口径与整树一致。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.schema import Base, User
from app.db.session import get_session
from app.main import app
from app.services.auth_service import hash_password


class _StatementCounter:
    """引擎级 SQL 语句计数器：N+1 回归的直接证据。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, connection, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)


@pytest.fixture
def env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(
            id="admin", username="admin", password_hash=hash_password("123456"),
            display_name="System", role="admin",
        ))
        session.commit()

    def override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    counter = _StatementCounter()
    event.listen(engine, "before_cursor_execute", counter)
    try:
        bare = TestClient(app)
        login = bare.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
        assert login.status_code == 200, login.text
        auth = TestClient(app, headers={"Authorization": "Bearer " + login.json()["token"]})
        course = auth.post(
            "/api/v1/courses", json={"name": "性能回归课", "slug": "perf-course"}
        ).json()["id"]
        yield {"client": auth, "course_id": course, "engine": engine, "counter": counter}
    finally:
        app.dependency_overrides.clear()
        event.remove(engine, "before_cursor_execute", counter)
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_materials_list_is_batched(env):
    """5 份资料（多版本 / 未解析 / 多轮解析混合）→ 查询数有界且语义与逐料版一致。"""
    client, course_id = env["client"], env["course_id"]
    engine, counter = env["engine"], env["counter"]
    with engine.begin() as connection:
        materials_rows = [
            ("m1", [("v1", 1)]),
            ("m2", [("v2", 1)]),
            ("m3", [("v3", 1), ("v4", 2)]),  # 多版本：取最高版 v4
            ("m4", []),                       # 无版本
            ("m5", [("v5", 1)]),              # 同版本多轮解析：取最新一轮
        ]
        for material_id, versions in materials_rows:
            connection.execute(
                text("INSERT INTO materials (id, course_id, logical_name, material_type, status) "
                     "VALUES (:m, :c, :n, 'teaching_material', 'staged')"),
                {"m": material_id, "c": course_id, "n": f"资料{material_id}"},
            )
            for version_id, version_no in versions:
                connection.execute(
                    text("INSERT INTO material_versions (id, course_id, material_id, version_no, "
                         "sha256, mime_type, size_bytes, status, object_key) "
                         "VALUES (:v, :c, :m, :n, :h, 'application/pdf', 100, 'staged', 'k')"),
                    {"v": version_id, "c": course_id, "m": material_id, "n": version_no,
                     "h": version_id.ljust(64, "0")},
                )
        connection.execute(
            text("INSERT INTO parser_profiles (id, course_id, name, version, provider, configuration) "
                 "VALUES ('pp', :c, 'mineru', 'v1', 'mineru', '{}')"),
            {"c": course_id},
        )
        parse_runs = [
            ("r1", "v1", "ready", "2026-10-01 00:00:00"),
            ("r2", "v2", "failed", "2026-10-01 00:00:00"),
            ("r3", "v3", "ready", "2026-10-01 00:00:00"),   # 旧版本上的 run
            ("r4", "v5", "ready", "2026-10-01 00:00:00"),
            ("r5", "v5", "failed", "2026-10-02 00:00:00"),  # 更新一轮，覆盖 r4
        ]
        for run_id, version_id, status, created_at in parse_runs:
            connection.execute(
                text("INSERT INTO document_parse_runs (id, course_id, material_version_id, "
                     "parser_profile_id, status, created_at) "
                     "VALUES (:r, :c, :v, 'pp', :s, :t)"),
                {"r": run_id, "c": course_id, "v": version_id, "s": status, "t": created_at},
            )

    counter.statements.clear()
    response = client.get(f"/api/v1/courses/{course_id}/materials")

    assert response.status_code == 200
    # 逐料版在此数据下为 1 鉴权 + 1 课程 + 1 列表 + 5 版本 + 4 解析 = 12 条；
    # 批量版 = 1 鉴权 + 1 课程 + 1 列表 + 1 版本 + 1 解析 = 5 条。
    assert len(counter.statements) <= 7, counter.statements

    by_id = {item["id"]: item for item in response.json()}
    assert by_id["m1"]["parse_status"]["status"] == "ready"
    assert by_id["m2"]["parse_status"]["status"] == "failed"
    # 多版本取最高版；解析状态只认最新版本（旧版本有 run 也不算数）
    assert by_id["m3"]["latest_version"]["id"] == "v4"
    assert by_id["m3"]["parse_status"] is None
    # 无版本资料：两字段均空
    assert by_id["m4"]["latest_version"] is None
    assert by_id["m4"]["parse_status"] is None
    # 同版本多轮解析取 created_at 最新一条（批量版与单版版同语义）
    assert by_id["m5"]["parse_status"]["id"] == "r5"


def test_organization_run_endpoints_omit_input_snapshot(env):
    """run 读端点不再拖回 input_snapshot——状态轮询只该拿状态。"""
    client, course_id, engine = env["client"], env["course_id"], env["engine"]
    fat = json.dumps({"exam_points": ["x"] * 100, "blob": "重" * 5000}, ensure_ascii=False)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO organization_runs (id, course_id, status, error_code, created_at, input_snapshot) "
                 "VALUES ('run-fat', :c, 'failed', 'boom', '2026-10-01 00:00:00', :snap)"),
            {"c": course_id, "snap": fat},
        )
        connection.execute(
            text("INSERT INTO organization_runs (id, course_id, status, created_at, input_snapshot) "
                 "VALUES ('run-live', :c, 'running', '2026-10-02 00:00:00', :snap)"),
            {"c": course_id, "snap": fat},
        )

    latest = client.get(f"/api/v1/courses/{course_id}/organization-runs/latest")
    assert latest.status_code == 200
    payload = latest.json()
    assert payload["run_id"] == "run-live"
    # 无执行线程的 running 孤儿照旧被判死（heal 重取同样瘦身）
    assert payload["status"] == "failed"
    assert payload["error_code"] == "interrupted_by_restart"
    assert "input_snapshot" not in payload

    detail = client.get(f"/api/v1/courses/{course_id}/organization-runs/run-fat")
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == "run-fat"
    assert body["status"] == "failed"
    assert "input_snapshot" not in body


def test_published_knowledge_summary_matches_full_tree(env):
    """summary=true 的两个计数与整树模式同口径（active 卡 relation_edges 求和）。"""
    client, course_id = env["client"], env["course_id"]
    engine = env["engine"]
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO framework_versions (id, course_id, version_no, status, payload) "
                 "VALUES ('fw-1', :c, 1, 'published', '{}')"),
            {"c": course_id},
        )
        connection.execute(
            text("INSERT INTO organization_runs (id, course_id, status, input_snapshot) "
                 "VALUES ('run-1', :c, 'published', '{}')"),
            {"c": course_id},
        )
        connection.execute(
            text("INSERT INTO knowledge_catalog_versions (id, course_id, organization_run_id, "
                 "framework_version_id, version_no, status, payload) "
                 "VALUES ('cat-1', :c, 'run-1', 'fw-1', 1, 'published', '{}')"),
            {"c": course_id},
        )
        connection.execute(
            text("INSERT INTO content_domains (id, course_id, catalog_version_id, level, "
                 "framework_anchor_key, code, name, status) "
                 "VALUES ('dom-1', :c, 'cat-1', 1, 'chapter-1', 'd1', '内容域', 'active')"),
            {"c": course_id},
        )
        connection.execute(
            text("INSERT INTO assessment_units (id, course_id, catalog_version_id, content_domain_id, "
                 "code, title, performance_statement, scope_boundary, status) "
                 "VALUES ('unit-1', :c, 'cat-1', 'dom-1', 'U1', '单元', '陈述', '{}', 'active')"),
            {"c": course_id},
        )
        card_sql = text(
            "INSERT INTO knowledge_cards (id, course_id, catalog_version_id, assessment_unit_id, name,"
            " performance_statement, assessable_content, scope_boundary, cognitive_targets,"
            " allowed_question_types, importance, concept_cluster, answer_proposition, prompt_material,"
            " relation_edges, content_hash, status, version)"
            " VALUES (:id, :c, 'cat-1', 'unit-1', :n, '陈述', '[]', '{}', '[]', '[]', 1, '', '',"
            " '[]', :edges, :h, :status, 1)"
        )
        for card_id, edges, status in [
            ("card-1", '[{"type": "requires"}, {"type": "contrasts_with"}]', "active"),
            ("card-2", "[]", "active"),
            ("card-3", '[{"type": "specializes"}]', "active"),
            ("card-d", "[]", "draft"),  # 非 active：两种模式都不计
        ]:
            connection.execute(
                card_sql,
                {"id": card_id, "c": course_id, "n": card_id, "edges": edges,
                 "h": (card_id * 20)[:64], "status": status},
            )

    full = client.get(f"/api/v1/courses/{course_id}/published-knowledge").json()
    summary = client.get(
        f"/api/v1/courses/{course_id}/published-knowledge?summary=true"
    ).json()

    assert summary["published"] is True
    assert summary["knowledge_card_count"] == len(full["knowledge_cards"]) == 3
    assert summary["evidence_count"] == sum(
        len(card["relation_edges"]) for card in full["knowledge_cards"].values()
    ) == 3


def test_published_knowledge_summary_without_catalog(env):
    """无已发布目录：summary 模式回零计数（概览页不再显示空白树语义）。"""
    client = env["client"]
    empty_course = client.post(
        "/api/v1/courses", json={"name": "空课", "slug": "empty-course"}
    ).json()["id"]

    response = client.get(f"/api/v1/courses/{empty_course}/published-knowledge?summary=true")

    assert response.status_code == 200
    assert response.json() == {"published": False, "knowledge_card_count": 0, "evidence_count": 0}
