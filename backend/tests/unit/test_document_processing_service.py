from __future__ import annotations

import asyncio
import io
import json
import zipfile

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.adapters.document.protocol import ParseArtifact, ParseProgress, ParseRequest, ParseState, ParseSubmission
from app.db.schema import (
    Base,
    Course,
    User,
    content_blocks,
    document_artifacts,
    document_parse_runs,
    material_versions,
    materials,
    parser_profiles,
)
from app.services.document_processing_service import create_parse_run, poll_parse_run, submit_parse_run


class FakeParser:
    async def submit(self, request):
        return ParseSubmission("provider-1")

    async def poll(self, provider_batch_id):
        return ParseProgress(ParseState.DONE, result_url="https://result.invalid/result.zip", trace_id="trace-1")

    async def fetch(self, provider_batch_id):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("content_list.json", json.dumps([{"type": "text", "text": "核心知识", "page_idx": 2}], ensure_ascii=False))
            archive.writestr("full.md", "核心知识")
        return ParseArtifact(provider_batch_id, buffer.getvalue(), "application/zip")


class FakeStore:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, object_key, content, content_type):
        self.objects[object_key] = (content, content_type)


async def _empty_content():
    if False:
        yield b""


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'parse.db'}")
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(User(id="owner", display_name="Owner", role="teacher"))
    session.flush()
    session.add(Course(id="course", owner_id="owner", slug="course", name="Course"))
    session.commit()
    session.execute(materials.insert().values(id="material", course_id="course", logical_name="lesson.pdf", material_type="teaching_material", status="staged"))
    session.execute(material_versions.insert().values(id="mv", course_id="course", material_id="material", version_no=1, sha256="a" * 64, mime_type="application/pdf", size_bytes=3, object_key="object", status="staged"))
    session.execute(parser_profiles.insert().values(id="profile", course_id="course", name="mineru", version="vlm", provider="mineru", configuration={}))
    session.commit()
    return engine, session


def test_parse_lifecycle_persists_artifacts_blocks_and_reuses_ready_hash(tmp_path):
    engine, session = _session(tmp_path)
    try:
        run_id, reused = create_parse_run(session, course_id="course", material_version_id="mv", parser_profile_id="profile")
        session.commit()
        assert not reused
        parser = FakeParser()
        request = ParseRequest("mv", "lesson.pdf", "application/pdf", _empty_content)
        assert asyncio.run(submit_parse_run(session, parser, course_id="course", run_id=run_id, request=request)) == "provider-1"
        store = FakeStore()
        assert asyncio.run(poll_parse_run(session, parser, store, course_id="course", run_id=run_id)) == "ready"

        run = session.execute(select(document_parse_runs).where(document_parse_runs.c.id == run_id)).one()._mapping
        assert run["status"] == "ready"
        block = session.execute(select(content_blocks)).one()._mapping
        assert block["text"] == "核心知识"
        assert block["page_index"] == 2
        assert len(session.execute(select(document_artifacts)).all()) == 3
        assert len(store.objects) == 3

        reused_id, reused = create_parse_run(session, course_id="course", material_version_id="mv", parser_profile_id="profile")
        assert reused and reused_id != run_id
        cloned = session.execute(select(document_parse_runs).where(document_parse_runs.c.id == reused_id)).one()._mapping
        assert cloned["status"] == "ready"
        assert cloned["reused_from_run_id"] == run_id
        assert len(session.execute(select(content_blocks)).all()) == 2
    finally:
        session.close()
        engine.dispose()
