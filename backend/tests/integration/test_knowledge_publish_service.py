from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.db.schema import (
    Base,
    Course,
    User,
    assessment_units,
    content_domains,
    content_blocks,
    document_parse_runs,
    evidence_chunks,
    exam_point_evidence_links,
    exam_points,
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
    parser_profiles,
)
from app.domain.knowledge.models import (
    AssessmentUnitDraft,
    KnowledgeCardDraft,
    KnowledgeTopicDraft,
    KnowledgeTreeCandidate,
    KnowledgeTreeConfirmation,
)
from app.services.knowledge_publish_service import (
    DatabaseKnowledgeRepository,
    KnowledgePublishError,
    create_organization_state,
    get_organization_candidate,
)


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
        exam_points.insert().values(
            id="exam-point-1",
            course_id="course",
            framework_version_id="framework-v1",
            anchor_key="rag",
            code="EP-1",
            title="RAG流程",
            assessment_requirement="说明RAG流程",
            weight_value=100,
            weight_source="assessment_syllabus",
            weight_group_id="rag",
            cognitive_targets=["understand"],
            assessment_orientations=["conceptual"],
            allowed_question_types=["short_answer"],
            operational_detail_policy="supporting_only",
            scope_boundary={},
            required_evidence_roles=["answer_or_rubric_basis"],
            retrieval_intent="检索RAG流程和评分依据",
            teaching_anchor_keys=[],
            status="confirmed",
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
        parser_profiles.insert().values(
            id="parser-profile",
            course_id="course",
            name="mineru",
            version="1",
            provider="mineru",
            configuration={},
        )
    )
    session.execute(
        document_parse_runs.insert().values(
            id="parse-material-v1",
            course_id="course",
            material_version_id="material-v1",
            parser_profile_id="parser-profile",
            status="ready",
            completed_at=datetime.now(UTC),
        )
    )
    session.execute(
        content_blocks.insert().values(
            id="block-material-v1",
            course_id="course",
            document_parse_run_id="parse-material-v1",
            material_version_id="material-v1",
            block_index=0,
            block_type="text",
            text="RAG包括检索、上下文构造和生成",
            page_index=3,
            bbox=[1, 2, 3, 4],
            heading_path=["RAG"],
            reading_order=0,
            content_hash="c" * 64,
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
    from app.domain.knowledge.relevance import (
        AssessmentUnitCandidate,
        ContentKind,
        EvidenceDecision,
        KnowledgeCardCandidate,
        RelevanceClass,
    )

    decision = EvidenceDecision(
        exam_point_code="EP-1",
        evidence_chunk_id="evidence-1",
        relevance_class=RelevanceClass.DIRECT,
        support_claim="检索、上下文构造和生成",
        evidence_role="answer_or_rubric_basis",
        content_kind=ContentKind.FACT,
        candidate_assessment_unit=AssessmentUnitCandidate(
            code="rag-flow",
            title="分析RAG流程",
            performance_statement="能够分析RAG流程",
        ),
        candidate_card_content=KnowledgeCardCandidate(
            name="RAG基本流程",
            performance_statement="能够说明RAG基本流程",
            assessable_content=["检索、上下文构造和生成"],
        ),
        confidence=95,
    )
    return KnowledgeTreeCandidate(
        framework_version_id="framework-v1",
        topics=[
            KnowledgeTopicDraft(
                code="rag-topic", name="检索增强生成", framework_anchor_key="rag",
                units=[AssessmentUnitDraft(
                    code="rag-flow", title="分析RAG流程", performance_statement="能够分析RAG流程",
                    exam_point_code="EP-1",
                    cards=[KnowledgeCardDraft(
                        name="RAG基本流程", performance_statement="能够说明RAG基本流程",
                        assessable_content=["检索、上下文构造和生成"], evidence_chunk_ids=["evidence-1"],
                    )],
                )],
            )
        ],
        evidence_decisions=[decision],
    )


def test_database_repository_publishes_catalog_and_index_atomically(tmp_path):
    engine, session = _session(tmp_path)
    try:
        repository = DatabaseKnowledgeRepository(session)
        state = {
            "course_id": "course",
            "run_id": "organization-run",
            "file_decisions": [
                {
                    "exam_point_code": "EP-1",
                    "material_version_id": "material-v1",
                    "decisions": [_tree().evidence_decisions[0].model_dump(mode="json")],
                }
            ],
        }
        candidate_id = repository.persist_candidate(state, _tree())
        assert repository.persist_candidate(state, _tree()) == candidate_id
        teacher_candidate = get_organization_candidate(
            session, course_id="course", run_id="organization-run"
        )
        assert teacher_candidate["payload"]["relevance_counts"] == {
            "direct": 1,
            "supporting": 0,
            "background": 0,
            "out_of_scope": 0,
        }
        assert teacher_candidate["payload"]["evidence_sources"][0]["exam_point_code"] == "EP-1"

        result = repository.publish(
            {**state, "candidate_id": candidate_id},
            _tree(),
            KnowledgeTreeConfirmation(
                operations=[{
                    "operation": "rename_topic",
                    "target_code": "rag-topic",
                    "value": "RAG方法",
                }],
                reviewed_topic_codes=["rag-topic"],
                reviewed_exam_point_codes=["EP-1"],
                teacher_exclusions=[],
            ),
        )

        assert result == {"catalog_version_id": candidate_id, "index_version_id": result["index_version_id"]}
        assert session.scalar(select(knowledge_catalog_versions.c.status).where(knowledge_catalog_versions.c.id == candidate_id)) == "published"
        assert session.scalar(select(content_domains.c.level).where(content_domains.c.code == "rag-topic")) == 2
        assert session.scalar(select(content_domains.c.name).where(content_domains.c.code == "rag-topic")) == "RAG方法"
        assert session.scalar(select(assessment_units.c.code).where(assessment_units.c.code == "rag-flow")) == "rag-flow"
        assert session.scalar(select(assessment_units.c.exam_point_id).where(assessment_units.c.code == "rag-flow")) == "exam-point-1"
        assert session.scalar(select(knowledge_cards.c.name).where(knowledge_cards.c.name == "RAG基本流程")) == "RAG基本流程"
        assert session.scalar(select(knowledge_evidence_links.c.evidence_chunk_id)) == "evidence-1"
        assert session.scalar(select(index_memberships.c.knowledge_card_id)) is not None
        assert session.scalar(select(index_versions.c.status)) == "published"
        assert session.scalar(select(exam_point_evidence_links.c.relevance_class)) == "direct"
    finally:
        session.close()
        engine.dispose()


class RecordingEmbedder:
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[float(index + 1), 1.0] for index, _ in enumerate(texts)]


def test_create_organization_state_snapshots_ready_blocks_without_putting_text_in_state(tmp_path):
    engine, session = _session(tmp_path)
    try:
        embedder = RecordingEmbedder()

        state = create_organization_state(
            session,
            course_id="course",
            material_version_ids=["material-v1"],
            embedder=embedder,
        )

        assert embedder.calls == [["RAG包括检索、上下文构造和生成"]]
        assert "files" not in state
        assert "content" not in str(state)
        assert len(state["evidence_chunk_ids"]) == 1
        assert state["frozen_input"] == {
            "framework_version_id": "framework-v1",
            "exam_point_ids": ["exam-point-1"],
            "material_version_ids": ["material-v1"],
        }
        row = session.execute(
            select(evidence_chunks).where(evidence_chunks.c.id == state["evidence_chunk_ids"][0])
        ).mappings().one()
        assert row["content_block_id"] == "block-material-v1"
        assert row["locator"] == {
            "page_index": 3,
            "bbox": [1, 2, 3, 4],
            "heading_path": ["RAG"],
            "reading_order": 0,
            "block_type": "text",
        }
        assert row["embedding"] == [1.0, 1.0]
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("deleted", "available"),
        ("unready", "ready"),
    ],
)
def test_create_organization_state_rejects_deleted_or_unready_versions(tmp_path, mutation, message):
    engine, session = _session(tmp_path)
    try:
        if mutation == "deleted":
            session.execute(materials.update().where(materials.c.id == "material").values(status="deleted"))
        else:
            session.execute(
                document_parse_runs.update()
                .where(document_parse_runs.c.id == "parse-material-v1")
                .values(status="pending")
            )
        session.commit()

        with pytest.raises(KnowledgePublishError, match=message):
            create_organization_state(
                session,
                course_id="course",
                material_version_ids=["material-v1"],
                embedder=RecordingEmbedder(),
            )
    finally:
        session.close()
        engine.dispose()


def test_formal_publish_requires_review_of_sufficient_exam_points(tmp_path):
    engine, session = _session(tmp_path)
    try:
        repository = DatabaseKnowledgeRepository(session)
        state = {
            "course_id": "course",
            "run_id": "organization-run",
            "file_decisions": [{
                "exam_point_code": "EP-1",
                "material_version_id": "material-v1",
                "decisions": [_tree().evidence_decisions[0].model_dump(mode="json")],
            }],
        }
        candidate_id = repository.persist_candidate(state, _tree())

        with pytest.raises(KnowledgePublishError, match="review"):
            repository.publish(
                {**state, "candidate_id": candidate_id},
                _tree(),
                KnowledgeTreeConfirmation(
                    operations=[],
                    reviewed_topic_codes=["rag-topic"],
                    teacher_exclusions=[],
                ),
            )
    finally:
        session.close()
        engine.dispose()
