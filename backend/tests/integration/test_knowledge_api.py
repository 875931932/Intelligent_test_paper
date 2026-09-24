from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.v1.knowledge import (
    _ACTIVE_ORG_RUN_IDS,
    _heal_interrupted_run,
    _mark_organization_run_failed,
)
from app.config import settings
from app.db.schema import Base, Course, User, organization_runs
from app.main import app


def _mark_failed_env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'knowledge-api.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(User(id="owner-dev", display_name="Owner", role="teacher"))
    session.flush()
    session.add(Course(id="course", owner_id="owner-dev", slug="course", name="Course"))
    session.commit()
    session.close()
    return engine, sessionmaker(bind=engine)


def test_mark_organization_run_failed_updates_existing_running_row(tmp_path):
    engine, factory = _mark_failed_env(tmp_path)
    try:
        session = factory()
        session.execute(
            organization_runs.insert().values(
                id="run-1",
                course_id="course",
                status="running",
                input_snapshot={"material_version_ids": ["material-v1"]},
            )
        )
        session.commit()
        session.close()

        _mark_organization_run_failed(
            factory, course_id="course", run_id="run-1", message="boom"
        )

        session = factory()
        row = session.execute(
            select(organization_runs).where(organization_runs.c.id == "run-1")
        ).mappings().one()
        session.close()
        assert row["status"] == "failed"
        assert row["error_message"] == "boom"
        assert row["input_snapshot"] == {"material_version_ids": ["material-v1"]}
    finally:
        engine.dispose()


def test_mark_organization_run_failed_inserts_row_when_run_missing(tmp_path):
    """UPDATE 落 0 行时必须补插 failed 行：否则前端对已发放 run_id 永远 404（事故回归）。"""
    engine, factory = _mark_failed_env(tmp_path)
    try:
        _mark_organization_run_failed(
            factory,
            course_id="course",
            run_id="run-ghost",
            message="embedding service is unavailable",
        )

        session = factory()
        row = session.execute(
            select(organization_runs).where(
                organization_runs.c.id == "run-ghost",
                organization_runs.c.course_id == "course",
            )
        ).mappings().one_or_none()
        session.close()
        assert row is not None
        assert row["status"] == "failed"
        assert row["error_code"] == "organization_invariant_error"
        assert row["error_message"] == "embedding service is unavailable"
        assert row["input_snapshot"] == {}
    finally:
        engine.dispose()


@pytest.mark.parametrize("missing_setting", ["llm_api_key", "llm_base_url", "llm_model"])
def test_knowledge_requires_complete_llm_configuration(monkeypatch, missing_setting):
    monkeypatch.setattr(settings, "embedding_api_key", "embedding-key")
    monkeypatch.setattr(settings, "embedding_base_url", "https://embedding.invalid/v1")
    monkeypatch.setattr(settings, "embedding_model", "embedding-model")
    monkeypatch.setattr(settings, "llm_api_key", "configured-test-key")
    monkeypatch.setattr(settings, "llm_base_url", "https://llm.invalid/v1")
    monkeypatch.setattr(settings, "llm_model", "generic-model")
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
        # org-runs 依赖序：embedder → knowledge point extractor → classifier →
        # consolidator。LLM 配置不全时，第一个 LLM 守卫（extractor）先失败。
        assert response.json()["detail"] == "knowledge point extractor is not configured"
    finally:
        for state_name in (
            "organization_embedder",
            "semantic_json_client",
            "exam_point_evidence_classifier",
            "exam_point_knowledge_consolidator",
        ):
            if hasattr(app.state, state_name):
                delattr(app.state, state_name)


@pytest.mark.parametrize("stale_status", ["queued", "running"])
def test_heal_interrupted_run_flips_stale_row_to_failed(tmp_path, stale_status):
    """进程重启后残留的 running/queued 行在读取时判死（前端轮询解卡的根治回归）。"""
    engine, factory = _mark_failed_env(tmp_path)
    try:
        session = factory()
        session.execute(
            organization_runs.insert().values(
                id="run-stale",
                course_id="course",
                status=stale_status,
                input_snapshot={},
            )
        )
        session.commit()
        row = dict(
            session.execute(
                select(organization_runs).where(organization_runs.c.id == "run-stale")
            ).mappings().one()
        )

        healed = _heal_interrupted_run(session, row)

        assert healed["status"] == "failed"
        assert healed["error_code"] == "interrupted_by_restart"
        assert healed["completed_at"] is not None
        stored = session.execute(
            select(organization_runs).where(organization_runs.c.id == "run-stale")
        ).mappings().one()
        assert stored["status"] == "failed"
        session.close()
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "settled_status",
    ["awaiting_teacher_confirmation", "published", "rejected", "failed"],
)
def test_heal_interrupted_run_keeps_settled_status(tmp_path, settled_status):
    engine, factory = _mark_failed_env(tmp_path)
    try:
        session = factory()
        session.execute(
            organization_runs.insert().values(
                id="run-settled",
                course_id="course",
                status=settled_status,
                input_snapshot={},
            )
        )
        session.commit()
        row = dict(
            session.execute(
                select(organization_runs).where(organization_runs.c.id == "run-settled")
            ).mappings().one()
        )

        healed = _heal_interrupted_run(session, row)

        assert healed["status"] == settled_status
        session.close()
    finally:
        engine.dispose()


def test_heal_interrupted_run_keeps_run_executing_in_this_process(tmp_path):
    engine, factory = _mark_failed_env(tmp_path)
    try:
        session = factory()
        session.execute(
            organization_runs.insert().values(
                id="run-active",
                course_id="course",
                status="running",
                input_snapshot={},
            )
        )
        session.commit()
        row = dict(
            session.execute(
                select(organization_runs).where(organization_runs.c.id == "run-active")
            ).mappings().one()
        )

        _ACTIVE_ORG_RUN_IDS.add("run-active")
        try:
            healed = _heal_interrupted_run(session, row)
        finally:
            _ACTIVE_ORG_RUN_IDS.discard("run-active")

        assert healed["status"] == "running"
        assert healed["completed_at"] is None
        session.close()
    finally:
        engine.dispose()
