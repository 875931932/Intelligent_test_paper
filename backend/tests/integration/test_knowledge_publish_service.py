from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.db.schema import (
    Base,
    Course,
    User,
    assessment_units,
    content_domains,
    evidence_chunks,
    framework_build_runs,
    framework_versions,
)
from app.db.schema import (
    index_memberships,
    index_versions,
    knowledge_cards,
    knowledge_catalog_versions,
    knowledge_evidence_links,
    material_versions,
    materials,
    organization_runs,
)
from app.domain.knowledge.models import (
    AssessmentUnitDraft,
    KnowledgeCardDraft,
    KnowledgeTopicDraft,
    KnowledgeTreeCandidate,
    KnowledgeTreeConfirmation,
)
from app.services.knowledge_publish_service import DatabaseKnowledgeRepository


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'knowledge-publish.db'}")
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(User(id="owner-dev", display_name="Owner", role="teacher"))
    session.flush()
    session.add(Course(id="course", owner_id="owner-dev", slug="course", name="Course"))
    session.flush()
    session.execute(framework_build_runs.insert().values(id="framework-run", course_id="course", status="published", input_snapshot={}))
    session.execute(
        framework_versions.insert().values(
            id="framework-v1",
            course_id="course",
            framework_build_run_id="framework-run",
            version_no=1,
            status="published",
            payload={"anchors": [{"key": "rag", "title": "RAG", "exam_weight": 100}]},
        )
    )
    session.execute(
        organization_runs.insert().values(
            id="organization-run",
            course_id="course",
            framework_version_id="framework-v1",
            status="running",
            input_snapshot={},
        )
    )
    session.execute(materials.insert().values(id="material", course_id="course", logical_name="slides.pdf", material_type="teaching_material", status="staged"))
    session.execute(
        material_versions.insert().values(
            id="material-v1", course_id="course", material_id="material", version_no=1, status="staged",
            object_key="courses/course/slides.pdf", size_bytes=10, sha256="a" * 64, mime_type="application/pdf",
        )
    )
    session.execute(
        evidence_chunks.insert().values(
            id="evidence-1", course_id="course", organization_run_id="organization-run", material_version_id="material-v1",
            chunk_index=0, content="RAG包括检索、上下文构造和生成", content_hash="b" * 64,
        )
    )
    session.commit()
    return engine, session


def _tree():
    return KnowledgeTreeCandidate(
        framework_version_id="framework-v1",
        topics=[
            KnowledgeTopicDraft(
                code="rag-topic", name="检索增强生成", framework_anchor_key="rag",
                units=[AssessmentUnitDraft(
                    code="rag-flow", title="分析RAG流程", performance_statement="能够分析RAG流程",
                    cards=[KnowledgeCardDraft(
                        name="RAG基本流程", performance_statement="能够说明RAG基本流程",
                        assessable_content=["检索、上下文构造和生成"], evidence_chunk_ids=["evidence-1"],
                    )],
                )],
            )
        ],
    )


def test_database_repository_publishes_catalog_and_index_atomically(tmp_path):
    engine, session = _session(tmp_path)
    try:
        repository = DatabaseKnowledgeRepository(session)
        state = {"course_id": "course", "run_id": "organization-run"}
        candidate_id = repository.persist_candidate(state, _tree())
        assert repository.persist_candidate(state, _tree()) == candidate_id

        result = repository.publish(
            {**state, "candidate_id": candidate_id},
            _tree(),
            KnowledgeTreeConfirmation(operations=[], reviewed_topic_codes=["rag-topic"], teacher_exclusions=[]),
        )

        assert result == {"catalog_version_id": candidate_id, "index_version_id": result["index_version_id"]}
        assert session.scalar(select(knowledge_catalog_versions.c.status).where(knowledge_catalog_versions.c.id == candidate_id)) == "published"
        assert session.scalar(select(content_domains.c.level).where(content_domains.c.code == "rag-topic")) == 2
        assert session.scalar(select(assessment_units.c.code).where(assessment_units.c.code == "rag-flow")) == "rag-flow"
        assert session.scalar(select(knowledge_cards.c.name).where(knowledge_cards.c.name == "RAG基本流程")) == "RAG基本流程"
        assert session.scalar(select(knowledge_evidence_links.c.evidence_chunk_id)) == "evidence-1"
        assert session.scalar(select(index_memberships.c.knowledge_card_id)) is not None
        assert session.scalar(select(index_versions.c.status)) == "published"
    finally:
        session.close()
        engine.dispose()
