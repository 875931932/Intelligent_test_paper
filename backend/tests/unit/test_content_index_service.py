"""content_index_service：解析块向量索引（幂等/模型溯源/降级）与语料装载。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.schema import (
    Base,
    Course,
    User,
    content_blocks,
    document_parse_runs,
    material_versions,
    materials,
    parser_profiles,
    task_runs,
)
from app.services import content_index_service


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'index.db'}")
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(User(id="u1", display_name="T1", role="teacher"))
        s.flush()
        s.add(Course(id="c1", owner_id="u1", slug="cs101", name="CS101"))
        s.add(Course(id="c2", owner_id="u1", slug="cs201", name="CS201"))
        s.flush()
        # c1：m1 已解析（3 块：文本/公式/空图），m2 未解析，m3 解析失败
        s.execute(
            materials.insert().values(
                id="m1", course_id="c1", logical_name="教学大纲",
                material_type="teaching_syllabus", status="staged",
            )
        )
        s.execute(
            materials.insert().values(
                id="m2", course_id="c1", logical_name="未解析讲义",
                material_type="teaching_material", status="staged",
            )
        )
        s.execute(
            materials.insert().values(
                id="m3", course_id="c1", logical_name="解析失败资料",
                material_type="teaching_material", status="staged",
            )
        )
        s.execute(
            material_versions.insert().values(
                id="v1", course_id="c1", material_id="m1", version_no=1, status="staged",
                object_key="k1", size_bytes=10, sha256="a" * 64, mime_type="application/pdf",
            )
        )
        s.execute(
            material_versions.insert().values(
                id="v3", course_id="c1", material_id="m3", version_no=1, status="staged",
                object_key="k3", size_bytes=10, sha256="c" * 64, mime_type="application/pdf",
            )
        )
        s.execute(
            parser_profiles.insert().values(
                id="pf1", course_id="c1", name="mineru", version="v1",
                provider="mineru", configuration={},
            )
        )
        s.execute(
            document_parse_runs.insert().values(
                id="r1", course_id="c1", material_version_id="v1",
                parser_profile_id="pf1", status="ready",
                completed_at=datetime.now(UTC),
            )
        )
        s.execute(
            document_parse_runs.insert().values(
                id="r3", course_id="c1", material_version_id="v3",
                parser_profile_id="pf1", status="failed",
                completed_at=datetime.now(UTC),
            )
        )
        blocks = [
            # b1：普通文本
            dict(id="b1", course_id="c1", document_parse_run_id="r1",
                 material_version_id="v1", block_index=0, block_type="text",
                 text="监督学习分为分类与回归两大任务。", latex=None, markdown=None,
                 heading_path=["第三章"], page_index=1,
                 reading_order=0, content_hash="1" * 64),
            # b2：公式块（text 空，latex 兜底）
            dict(id="b2", course_id="c1", document_parse_run_id="r1",
                 material_version_id="v1", block_index=1, block_type="equation",
                 text="", latex="E=mc^2", markdown=None,
                 heading_path=["第三章"], page_index=2,
                 reading_order=1, content_hash="2" * 64),
            # b3：空图块（无任何文本，不进语料）
            dict(id="b3", course_id="c1", document_parse_run_id="r1",
                 material_version_id="v1", block_index=2, block_type="image",
                 text="", latex=None, markdown=None,
                 heading_path=[], page_index=3,
                 reading_order=2, content_hash="3" * 64),
            # b4：失败 run 的块（不进语料）
            dict(id="b4", course_id="c1", document_parse_run_id="r3",
                 material_version_id="v3", block_index=0, block_type="text",
                 text="失败解析的内容不应出现。", latex=None, markdown=None,
                 heading_path=[], page_index=None,
                 reading_order=0, content_hash="4" * 64),
        ]
        s.execute(content_blocks.insert(), blocks)
        # c2：另一课程的 ready 块（course_id 隔离）
        s.execute(
            materials.insert().values(
                id="m9", course_id="c2", logical_name="他课资料",
                material_type="teaching_material", status="staged",
            )
        )
        s.execute(
            material_versions.insert().values(
                id="v9", course_id="c2", material_id="m9", version_no=1, status="staged",
                object_key="k9", size_bytes=10, sha256="d" * 64, mime_type="application/pdf",
            )
        )
        s.execute(
            parser_profiles.insert().values(
                id="pf9", course_id="c2", name="mineru", version="v1",
                provider="mineru", configuration={},
            )
        )
        s.execute(
            document_parse_runs.insert().values(
                id="r9", course_id="c2", material_version_id="v9",
                parser_profile_id="pf9", status="ready",
                completed_at=datetime.now(UTC),
            )
        )
        s.execute(
            content_blocks.insert().values(
                id="b9", course_id="c2", document_parse_run_id="r9",
                material_version_id="v9", block_index=0, block_type="text",
                text="另一门课程的解析内容。", heading_path=[],
                reading_order=0, content_hash="9" * 64,
            )
        )
        s.commit()
    yield s
    s.close()


class StubGateway:
    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.1, 0.2] for _ in texts]


def _configured(monkeypatch):
    monkeypatch.setattr(settings, "embedding_api_key", "key")
    monkeypatch.setattr(settings, "embedding_base_url", "https://embedding.invalid/v1")
    monkeypatch.setattr(settings, "embedding_model", "emb-v1")
    monkeypatch.setattr(settings, "embedding_api_format", "openai")


def _vector_rows(session) -> dict:
    rows = session.execute(
        select(
            content_blocks.c.id,
            content_blocks.c.embedding,
            content_blocks.c.embedding_model,
        ).where(content_blocks.c.course_id == "c1")
    ).all()
    return {row[0]: (row[1], row[2]) for row in rows}


def test_ensure_embedded_fills_missing_vectors_and_is_idempotent(session, monkeypatch):
    _configured(monkeypatch)
    gateway = StubGateway()
    monkeypatch.setattr(content_index_service, "build_embedder", lambda: gateway)

    first = content_index_service.ensure_embedded(session, course_id="c1")
    # b1/b2 需嵌入；b3 空文本跳过；b4 属于失败 run（不在 ready run 集合）
    assert first == 2
    assert gateway.calls == [["监督学习分为分类与回归两大任务。", "E=mc^2"]]
    rows = _vector_rows(session)
    assert rows["b1"] == ([0.1, 0.2], "emb-v1")
    assert rows["b2"] == ([0.1, 0.2], "emb-v1")
    assert rows["b3"] == (None, None)
    assert rows["b4"] == (None, None)

    # 幂等：第二次零缺块，不再调嵌入
    assert content_index_service.ensure_embedded(session, course_id="c1") == 0
    assert len(gateway.calls) == 1


def test_ensure_embedded_unconfigured_returns_zero_without_gateway(session, monkeypatch):
    monkeypatch.setattr(settings, "embedding_api_key", "")
    monkeypatch.setattr(settings, "embedding_base_url", "")
    monkeypatch.setattr(settings, "embedding_model", "")
    touched: list[int] = []
    monkeypatch.setattr(content_index_service, "build_embedder", lambda: touched.append(1))

    assert content_index_service.ensure_embedded(session, course_id="c1") == 0
    assert touched == []
    assert all(v[0] is None for v in _vector_rows(session).values())


def test_ensure_embedded_reembeds_on_model_change(session, monkeypatch):
    _configured(monkeypatch)
    gateway = StubGateway()
    monkeypatch.setattr(content_index_service, "build_embedder", lambda: gateway)
    # 预置旧模型向量：换模型后旧向量不可比 → 必须重嵌
    session.execute(
        content_blocks.update()
        .where(content_blocks.c.id == "b1")
        .values(embedding=[9.0, 9.0], embedding_model="old-v0")
    )
    session.commit()

    assert content_index_service.ensure_embedded(session, course_id="c1") == 2
    rows = _vector_rows(session)
    assert rows["b1"] == ([0.1, 0.2], "emb-v1")


def test_ensure_embedded_gateway_failure_returns_zero_without_partial_writes(
    session, monkeypatch
):
    _configured(monkeypatch)

    class BoomGateway:
        def embed(self, texts):
            raise RuntimeError("embedding down")

    monkeypatch.setattr(content_index_service, "build_embedder", lambda: BoomGateway())

    assert content_index_service.ensure_embedded(session, course_id="c1") == 0
    assert all(v[0] is None for v in _vector_rows(session).values())


def test_ensure_embedded_is_course_scoped(session, monkeypatch):
    _configured(monkeypatch)
    gateway = StubGateway()
    monkeypatch.setattr(content_index_service, "build_embedder", lambda: gateway)

    assert content_index_service.ensure_embedded(session, course_id="c1") == 2
    other = session.execute(
        select(content_blocks.c.embedding).where(content_blocks.c.id == "b9")
    ).scalar_one()
    assert other is None


def test_ensure_embedded_targets_single_run(session, monkeypatch):
    _configured(monkeypatch)
    gateway = StubGateway()
    monkeypatch.setattr(content_index_service, "build_embedder", lambda: gateway)

    # 指定 run：只嵌该 run 的缺块（material_index 任务路径）
    assert content_index_service.ensure_embedded(session, course_id="c1", run_ids=["r1"]) == 2
    rows = _vector_rows(session)
    assert rows["b1"][0] is not None and rows["b4"][0] is None


def test_load_content_chunks_returns_ready_blocks_with_locator(session):
    chunks = content_index_service.load_content_chunks(session, course_id="c1")
    # m1 的 ready 块；空图块/失败 run/未解析资料/他课资料全部排除
    assert [c.id for c in chunks] == ["b1", "b2"]
    assert chunks[0].locator["material_id"] == "m1"
    assert chunks[0].locator["material_name"] == "教学大纲"
    assert chunks[0].locator["page_index"] == 1
    assert chunks[0].locator["heading_path"] == ["第三章"]
    assert chunks[0].embedding is None  # 尚未嵌入
    assert chunks[1].content == "E=mc^2"  # 公式块 latex 兜底


def test_load_content_chunks_vector_only_with_current_model(session, monkeypatch):
    monkeypatch.setattr(settings, "embedding_model", "emb-v1")
    session.execute(
        content_blocks.update()
        .where(content_blocks.c.id == "b1")
        .values(embedding=[0.5, 0.5], embedding_model="emb-v1")
    )
    session.execute(
        content_blocks.update()
        .where(content_blocks.c.id == "b2")
        .values(embedding=[0.7, 0.7], embedding_model="old-v0")
    )
    session.commit()

    chunks = content_index_service.load_content_chunks(session, course_id="c1")
    by_id = {c.id: c for c in chunks}
    assert by_id["b1"].embedding == [0.5, 0.5]  # 当前模型 → 带出
    assert by_id["b2"].embedding is None  # 换模型旧向量不可比 → 丢弃，走词面


def test_load_content_chunks_targets_material_and_ignores_unparsed(session):
    assert content_index_service.load_content_chunks(
        session, course_id="c1", material_ids=["m2"]
    ) == []
    chunks = content_index_service.load_content_chunks(
        session, course_id="c1", material_ids=["m1"]
    )
    assert [c.id for c in chunks] == ["b1", "b2"]
    assert content_index_service.load_content_chunks(session, course_id="c2")[0].id == "b9"


def test_enqueue_index_task_is_idempotent(session):
    first = content_index_service.enqueue_index_task(session, course_id="c1", run_id="r1")
    session.commit()
    assert first
    row = session.execute(
        select(
            task_runs.c.task_type, task_runs.c.payload, task_runs.c.idempotency_key
        ).where(task_runs.c.id == first)
    ).one()
    assert row.task_type == "material_index"
    assert row.payload == {"course_id": "c1", "run_id": "r1"}
    assert row.idempotency_key == "material_index:r1"

    # 同 run_id 重复触发不重复入队（返回 None，调用方跳过派发）
    assert (
        content_index_service.enqueue_index_task(session, course_id="c1", run_id="r1")
        is None
    )
    count = session.execute(
        select(task_runs).where(task_runs.c.task_type == "material_index")
    ).all()
    assert len(count) == 1
