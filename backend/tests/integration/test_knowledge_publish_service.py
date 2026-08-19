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
from app.services.material_service import delete_material


ORGANIZATION_SCHEMA_VERSION = 2


def _frozen_input():
    return {
        "organization_schema_version": ORGANIZATION_SCHEMA_VERSION,
        "framework_version_id": "framework-v1",
        "exam_points": [{"id": "exam-point-1", "code": "EP-1"}],
        "material_version_ids": ["material-v1"],
    }


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
            input_snapshot=_frozen_input(),
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
            embedding=[0.75, 0.25],
        )
    )
    session.commit()
    return engine, session


def _tree(*, coverage_status="sufficient"):
    from app.domain.knowledge.relevance import (
        AssessmentUnitCandidate,
        ContentKind,
        EvidenceDecision,
        ExamPointCoverage,
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
                        concept_cluster="RAG流程组成",
                        answer_proposition="检索、上下文构造和生成",
                        prompt_material=["RAG流程语境"],
                    )],
                )],
            )
        ],
        evidence_decisions=[decision],
        coverage=[ExamPointCoverage(
            exam_point_code="EP-1",
            direct_count=1,
            supporting_count=0,
            background_count=0,
            out_of_scope_count=0,
            status=coverage_status,
            reasons=[] if coverage_status == "sufficient" else [f"coverage_{coverage_status}"],
        )],
    )


def _organization_state(tree):
    return {
        "course_id": "course",
        "run_id": "organization-run",
        "frozen_input": _frozen_input(),
        "file_decisions": [
            {
                "exam_point_code": "EP-1",
                "material_version_id": "material-v1",
                "decisions": [tree.evidence_decisions[0].model_dump(mode="json")],
            }
        ],
    }


def _add_outside_snapshot_evidence(session):
    session.execute(
        materials.insert().values(
            id="material-2",
            course_id="course",
            logical_name="notes.pdf",
            material_type="teaching_material",
            status="staged",
        )
    )
    session.execute(
        material_versions.insert().values(
            id="material-v2",
            course_id="course",
            material_id="material-2",
            version_no=1,
            status="staged",
            object_key="courses/course/notes.pdf",
            size_bytes=10,
            sha256="d" * 64,
            mime_type="application/pdf",
        )
    )
    session.execute(
        evidence_chunks.insert().values(
            id="evidence-2",
            course_id="course",
            organization_run_id="organization-run",
            material_version_id="material-v2",
            chunk_index=1,
            content="RAG包括检索、上下文构造和生成",
            content_hash="e" * 64,
            embedding=[0.5, 0.5],
        )
    )
    session.commit()


def _confirmation(*, operations=None, exclusions=None, reviewed=True):
    return KnowledgeTreeConfirmation(
        operations=operations or [],
        reviewed_topic_codes=["rag-topic"],
        reviewed_exam_point_codes=["EP-1"] if reviewed else [],
        teacher_exclusions=exclusions or [],
    )


def test_database_repository_publishes_catalog_and_index_atomically(tmp_path):
    engine, session = _session(tmp_path)
    try:
        repository = DatabaseKnowledgeRepository(session)
        tree = _tree()
        state = _organization_state(tree)
        candidate_id = repository.persist_candidate(state, tree)
        assert repository.persist_candidate(state, tree) == candidate_id
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
        assert (
            teacher_candidate["payload"]["organization_schema_version"]
            == ORGANIZATION_SCHEMA_VERSION
        )
        assert teacher_candidate["payload"]["frozen_input"] == _frozen_input()

        result = repository.publish(
            {**state, "candidate_id": candidate_id},
            tree,
            _confirmation(
                operations=[{
                    "operation": "rename_topic",
                    "target_code": "rag-topic",
                    "value": "RAG方法",
                }],
            ),
        )

        assert result == {"catalog_version_id": candidate_id, "index_version_id": result["index_version_id"]}
        assert session.scalar(select(knowledge_catalog_versions.c.status).where(knowledge_catalog_versions.c.id == candidate_id)) == "published"
        assert session.scalar(select(content_domains.c.level).where(content_domains.c.code == "rag-topic")) == 2
        assert session.scalar(select(content_domains.c.name).where(content_domains.c.code == "rag-topic")) == "RAG方法"
        assert session.scalar(select(assessment_units.c.code).where(assessment_units.c.code == "rag-flow")) == "rag-flow"
        assert session.scalar(select(assessment_units.c.exam_point_id).where(assessment_units.c.code == "rag-flow")) == "exam-point-1"
        assert session.scalar(select(knowledge_cards.c.name).where(knowledge_cards.c.name == "RAG基本流程")) == "RAG基本流程"
        assert session.scalar(select(knowledge_cards.c.concept_cluster).where(knowledge_cards.c.name == "RAG基本流程")) == "RAG流程组成"
        assert session.scalar(select(knowledge_cards.c.answer_proposition).where(knowledge_cards.c.name == "RAG基本流程")) == "检索、上下文构造和生成"
        assert session.scalar(select(knowledge_cards.c.prompt_material).where(knowledge_cards.c.name == "RAG基本流程")) == ["RAG流程语境"]
        assert session.scalar(select(knowledge_evidence_links.c.evidence_chunk_id)) == "evidence-1"
        assert session.scalar(select(index_memberships.c.knowledge_card_id)) is not None
        assert session.scalar(select(index_versions.c.status)) == "published"
        assert session.scalar(select(exam_point_evidence_links.c.relevance_class)) == "direct"
        published_payload = session.execute(
            select(knowledge_catalog_versions.c.payload).where(
                knowledge_catalog_versions.c.id == candidate_id
            )
        ).scalar_one()
        assert published_payload["organization_schema_version"] == ORGANIZATION_SCHEMA_VERSION
        assert published_payload["frozen_input"] == _frozen_input()
    finally:
        session.close()
        engine.dispose()


def test_publish_uses_persisted_candidate_tree_as_only_baseline(tmp_path):
    engine, session = _session(tmp_path)
    try:
        repository = DatabaseKnowledgeRepository(session)
        tree = _tree()
        state = _organization_state(tree)
        candidate_id = repository.persist_candidate(state, tree)
        mutated = tree.model_copy(deep=True)
        mutated.topics[0].units[0].cards[0].name = "UNREVIEWED MUTATION"

        repository.publish(
            {**state, "candidate_id": candidate_id},
            mutated,
            _confirmation(),
        )

        assert session.scalar(
            select(knowledge_cards.c.name).where(
                knowledge_cards.c.name == "UNREVIEWED MUTATION"
            )
        ) is None
        assert session.scalar(
            select(knowledge_cards.c.name).where(
                knowledge_cards.c.name == "RAG基本流程"
            )
        ) == "RAG基本流程"
    finally:
        session.close()
        engine.dispose()


def test_existing_candidate_revalidates_frozen_exam_points_before_idempotent_return(tmp_path):
    engine, session = _session(tmp_path)
    try:
        repository = DatabaseKnowledgeRepository(session)
        tree = _tree()
        state = _organization_state(tree)
        repository.persist_candidate(state, tree)
        session.execute(
            exam_points.update()
            .where(exam_points.c.id == "exam-point-1")
            .values(status="candidate")
        )
        session.commit()

        with pytest.raises(KnowledgePublishError, match="snapshot|exam point"):
            repository.persist_candidate(state, tree)
    finally:
        session.close()
        engine.dispose()


def test_persist_rejects_file_decision_from_material_outside_frozen_snapshot(tmp_path):
    engine, session = _session(tmp_path)
    try:
        _add_outside_snapshot_evidence(session)
        repository = DatabaseKnowledgeRepository(session)
        tree = _tree()
        outside_decision = tree.evidence_decisions[0].model_copy(
            update={"evidence_chunk_id": "evidence-2"}
        )
        state = _organization_state(tree)
        state["file_decisions"].append(
            {
                "exam_point_code": "EP-1",
                "material_version_id": "material-v2",
                "decisions": [outside_decision.model_dump(mode="json")],
            }
        )

        with pytest.raises(KnowledgePublishError, match="frozen material"):
            repository.persist_candidate(state, tree)

        assert session.scalar(select(knowledge_catalog_versions.c.id)) is None
        assert session.scalar(select(exam_point_evidence_links.c.id)) is None
    finally:
        session.close()
        engine.dispose()


def test_publish_rejects_persisted_evidence_link_outside_frozen_snapshot(tmp_path):
    engine, session = _session(tmp_path)
    try:
        repository = DatabaseKnowledgeRepository(session)
        tree = _tree()
        state = _organization_state(tree)
        candidate_id = repository.persist_candidate(state, tree)
        _add_outside_snapshot_evidence(session)
        session.execute(
            exam_point_evidence_links.insert().values(
                id="outside-link",
                course_id="course",
                organization_run_id="organization-run",
                exam_point_id="exam-point-1",
                evidence_chunk_id="evidence-2",
                relevance_class="direct",
                support_claim="不属于冻结资料的证据",
                evidence_role="answer_or_rubric_basis",
                confidence=90,
                status="candidate",
            )
        )
        session.commit()

        with pytest.raises(KnowledgePublishError, match="frozen material"):
            repository.publish(
                {**state, "candidate_id": candidate_id},
                tree,
                _confirmation(),
            )

        assert session.scalar(select(index_versions.c.id)) is None
    finally:
        session.close()
        engine.dispose()


def test_publish_rejects_boolean_organization_schema_version(tmp_path):
    engine, session = _session(tmp_path)
    try:
        repository = DatabaseKnowledgeRepository(session)
        tree = _tree()
        state = _organization_state(tree)
        candidate_id = repository.persist_candidate(state, tree)
        payload = session.execute(
            select(knowledge_catalog_versions.c.payload).where(
                knowledge_catalog_versions.c.id == candidate_id
            )
        ).scalar_one()
        session.execute(
            knowledge_catalog_versions.update()
            .where(knowledge_catalog_versions.c.id == candidate_id)
            .values(payload={**payload, "organization_schema_version": True})
        )
        session.commit()

        with pytest.raises(KnowledgePublishError, match="schema version"):
            repository.publish(
                {**state, "candidate_id": candidate_id},
                tree,
                _confirmation(),
            )

        assert session.scalar(select(index_versions.c.id)) is None
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
            "organization_schema_version": ORGANIZATION_SCHEMA_VERSION,
            "framework_version_id": "framework-v1",
            "exam_points": [{"id": "exam-point-1", "code": "EP-1"}],
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
    ("vectors", "message"),
    [
        ([[0.0, 0.0], [1.0, 0.0]], "zero norm"),
        ([[1.0, 0.0], [1.0]], "dimension"),
    ],
)
def test_create_organization_state_rejects_invalid_snapshot_embeddings(
    tmp_path, vectors, message
):
    engine, session = _session(tmp_path)
    try:
        session.execute(
            content_blocks.insert().values(
                id="block-material-v1-2",
                course_id="course",
                document_parse_run_id="parse-material-v1",
                material_version_id="material-v1",
                block_index=1,
                block_type="text",
                text="RAG生成阶段使用检索上下文",
                page_index=4,
                bbox=[1, 2, 3, 4],
                heading_path=["RAG"],
                reading_order=1,
                content_hash="f" * 64,
            )
        )
        session.commit()

        class InvalidEmbedder:
            def embed(self, texts):
                assert len(texts) == 2
                return vectors

        with pytest.raises(KnowledgePublishError, match=message):
            create_organization_state(
                session,
                course_id="course",
                material_version_ids=["material-v1"],
                embedder=InvalidEmbedder(),
            )
    finally:
        session.close()
        engine.dispose()


def test_create_organization_state_rejects_embedding_dimension_changes_across_files(tmp_path):
    engine, session = _session(tmp_path)
    try:
        session.execute(
            materials.insert().values(
                id="material-2",
                course_id="course",
                logical_name="exercise.pdf",
                material_type="exercise",
                status="staged",
            )
        )
        session.execute(
            material_versions.insert().values(
                id="material-v2",
                course_id="course",
                material_id="material-2",
                version_no=1,
                status="staged",
                object_key="courses/course/exercise.pdf",
                size_bytes=10,
                sha256="d" * 64,
                mime_type="application/pdf",
            )
        )
        session.execute(
            document_parse_runs.insert().values(
                id="parse-material-v2",
                course_id="course",
                material_version_id="material-v2",
                parser_profile_id="parser-profile",
                status="ready",
                completed_at=datetime.now(UTC),
            )
        )
        session.execute(
            content_blocks.insert().values(
                id="block-material-v2",
                course_id="course",
                document_parse_run_id="parse-material-v2",
                material_version_id="material-v2",
                block_index=0,
                block_type="text",
                text="RAG练习材料",
                page_index=1,
                bbox=[1, 2, 3, 4],
                heading_path=["练习"],
                reading_order=0,
                content_hash="e" * 64,
            )
        )
        session.commit()

        class ChangingDimensionEmbedder:
            def __init__(self):
                self.calls = 0

            def embed(self, texts):
                self.calls += 1
                return [[1.0, 0.0]] if self.calls == 1 else [[1.0, 0.0, 0.0]]

        with pytest.raises(KnowledgePublishError, match="dimension"):
            create_organization_state(
                session,
                course_id="course",
                material_version_ids=["material-v1", "material-v2"],
                embedder=ChangingDimensionEmbedder(),
            )
    finally:
        session.close()
        engine.dispose()


def test_repository_loads_frozen_chunk_embedding_for_retrieval(tmp_path):
    engine, session = _session(tmp_path)
    try:
        chunks = DatabaseKnowledgeRepository(session).load_evidence_chunks(
            course_id="course",
            run_id="organization-run",
            evidence_chunk_ids=["evidence-1"],
        )

        assert len(chunks) == 1
        assert chunks[0].embedding == [0.75, 0.25]
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
        tree = _tree()
        state = _organization_state(tree)
        candidate_id = repository.persist_candidate(state, tree)

        with pytest.raises(KnowledgePublishError, match="review"):
            repository.publish(
                {**state, "candidate_id": candidate_id},
                tree,
                _confirmation(reviewed=False),
            )
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize(
    "operation",
    [
        {"operation": "exclude_topic", "target_code": "rag-topic"},
        {"operation": "exclude_unit", "target_code": "rag-flow"},
    ],
)
def test_excluded_topic_or_unit_never_enters_published_index(tmp_path, operation):
    engine, session = _session(tmp_path)
    try:
        repository = DatabaseKnowledgeRepository(session)
        tree = _tree()
        state = _organization_state(tree)
        candidate_id = repository.persist_candidate(state, tree)

        repository.publish(
            {**state, "candidate_id": candidate_id},
            tree,
            _confirmation(operations=[operation]),
        )

        assert session.scalar(select(index_memberships.c.id)) is None
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize("coverage_status", ["insufficient", "conflicting"])
def test_unresolved_exam_point_coverage_blocks_publish(tmp_path, coverage_status):
    engine, session = _session(tmp_path)
    try:
        repository = DatabaseKnowledgeRepository(session)
        tree = _tree(coverage_status=coverage_status)
        state = _organization_state(tree)
        candidate_id = repository.persist_candidate(state, tree)

        with pytest.raises(KnowledgePublishError, match="coverage"):
            repository.publish(
                {**state, "candidate_id": candidate_id},
                tree,
                _confirmation(),
            )
    finally:
        session.close()
        engine.dispose()


def test_teacher_exclusion_excludes_entire_exam_point_from_index(tmp_path):
    engine, session = _session(tmp_path)
    try:
        repository = DatabaseKnowledgeRepository(session)
        tree = _tree()
        state = _organization_state(tree)
        candidate_id = repository.persist_candidate(state, tree)

        repository.publish(
            {**state, "candidate_id": candidate_id},
            tree,
            _confirmation(exclusions=["EP-1"], reviewed=False),
        )

        assert session.scalar(select(index_memberships.c.id)) is None
        assert session.scalar(select(assessment_units.c.status)) == "excluded"
    finally:
        session.close()
        engine.dispose()


def test_candidate_cannot_publish_after_its_only_direct_source_is_deleted(tmp_path):
    engine, session = _session(tmp_path)
    try:
        repository = DatabaseKnowledgeRepository(session)
        tree = _tree()
        state = _organization_state(tree)
        candidate_id = repository.persist_candidate(state, tree)
        delete_material(session, course_id="course", material_id="material")

        with pytest.raises(KnowledgePublishError, match="source"):
            repository.publish(
                {**state, "candidate_id": candidate_id},
                tree,
                _confirmation(),
            )

        assert session.scalar(select(index_memberships.c.id)) is None
        assert session.scalar(select(knowledge_catalog_versions.c.status)) == "candidate"
    finally:
        session.close()
        engine.dispose()


def test_publish_rejects_when_frozen_exam_point_is_no_longer_confirmed(tmp_path):
    engine, session = _session(tmp_path)
    try:
        repository = DatabaseKnowledgeRepository(session)
        tree = _tree()
        state = _organization_state(tree)
        candidate_id = repository.persist_candidate(state, tree)
        session.execute(
            exam_points.update()
            .where(exam_points.c.id == "exam-point-1")
            .values(status="candidate")
        )
        session.commit()
        delete_material(session, course_id="course", material_id="material")

        with pytest.raises(KnowledgePublishError, match="snapshot|exam point"):
            repository.publish(
                {**state, "candidate_id": candidate_id},
                tree,
                _confirmation(),
            )

        assert session.scalar(select(assessment_units.c.id)) is None
        assert session.scalar(select(knowledge_evidence_links.c.id)) is None
        assert session.scalar(select(index_versions.c.id)) is None
        assert session.scalar(
            select(knowledge_catalog_versions.c.status).where(
                knowledge_catalog_versions.c.id == candidate_id
            )
        ) == "candidate"
    finally:
        session.close()
        engine.dispose()


def test_publish_rejects_when_deleted_source_removes_only_answer_or_rubric_basis(tmp_path):
    engine, session = _session(tmp_path)
    try:
        session.execute(
            materials.insert().values(
                id="material-2",
                course_id="course",
                logical_name="notes.pdf",
                material_type="teaching_material",
                status="staged",
            )
        )
        session.execute(
            material_versions.insert().values(
                id="material-v2",
                course_id="course",
                material_id="material-2",
                version_no=1,
                status="staged",
                object_key="courses/course/notes.pdf",
                size_bytes=10,
                sha256="d" * 64,
                mime_type="application/pdf",
            )
        )
        session.execute(
            evidence_chunks.insert().values(
                id="evidence-2",
                course_id="course",
                organization_run_id="organization-run",
                material_version_id="material-v2",
                chunk_index=1,
                content="RAG包括检索、上下文构造和生成",
                content_hash="e" * 64,
            )
        )
        session.commit()
        tree = _tree()
        supporting_fact = tree.evidence_decisions[0].model_copy(
            update={"evidence_chunk_id": "evidence-2", "evidence_role": "fact"}
        )
        tree.evidence_decisions.append(supporting_fact)
        tree.topics[0].units[0].cards[0].evidence_chunk_ids.append("evidence-2")
        tree.coverage[0].direct_count = 2
        state = _organization_state(tree)
        expanded_frozen_input = _frozen_input()
        expanded_frozen_input["material_version_ids"] = ["material-v1", "material-v2"]
        state["frozen_input"] = expanded_frozen_input
        session.execute(
            organization_runs.update()
            .where(organization_runs.c.id == "organization-run")
            .values(input_snapshot=expanded_frozen_input)
        )
        state["file_decisions"].append(
            {
                "exam_point_code": "EP-1",
                "material_version_id": "material-v2",
                "decisions": [supporting_fact.model_dump(mode="json")],
            }
        )
        repository = DatabaseKnowledgeRepository(session)
        candidate_id = repository.persist_candidate(state, tree)
        delete_material(session, course_id="course", material_id="material")

        with pytest.raises(KnowledgePublishError, match="source"):
            repository.publish(
                {**state, "candidate_id": candidate_id},
                tree,
                _confirmation(),
            )
    finally:
        session.close()
        engine.dispose()


def test_publish_keeps_card_when_redundant_deleted_source_has_complete_live_replacement(tmp_path):
    engine, session = _session(tmp_path)
    try:
        _add_outside_snapshot_evidence(session)
        tree = _tree()
        replacement = tree.evidence_decisions[0].model_copy(
            update={"evidence_chunk_id": "evidence-2"}
        )
        tree.evidence_decisions.append(replacement)
        tree.topics[0].units[0].cards[0].evidence_chunk_ids.append("evidence-2")
        tree.coverage[0].direct_count = 2
        state = _organization_state(tree)
        expanded_frozen_input = _frozen_input()
        expanded_frozen_input["material_version_ids"] = ["material-v1", "material-v2"]
        state["frozen_input"] = expanded_frozen_input
        state["file_decisions"].append(
            {
                "exam_point_code": "EP-1",
                "material_version_id": "material-v2",
                "decisions": [replacement.model_dump(mode="json")],
            }
        )
        session.execute(
            organization_runs.update()
            .where(organization_runs.c.id == "organization-run")
            .values(input_snapshot=expanded_frozen_input)
        )
        repository = DatabaseKnowledgeRepository(session)
        candidate_id = repository.persist_candidate(state, tree)
        delete_material(session, course_id="course", material_id="material")

        repository.publish(
            {**state, "candidate_id": candidate_id},
            tree,
            _confirmation(),
        )

        assert session.scalar(select(index_memberships.c.id)) is not None
        assert session.scalars(select(knowledge_evidence_links.c.evidence_chunk_id)).all() == [
            "evidence-2"
        ]
    finally:
        session.close()
        engine.dispose()


def test_publish_rejects_sufficient_exam_point_without_active_card_chain(tmp_path):
    engine, session = _session(tmp_path)
    try:
        session.execute(
            exam_points.insert().values(
                id="exam-point-2",
                course_id="course",
                framework_version_id="framework-v1",
                anchor_key="rag",
                code="EP-2",
                title="RAG应用",
                assessment_requirement="应用RAG",
                weight_value=0,
                weight_source="assessment_syllabus",
                weight_group_id="rag",
                cognitive_targets=["apply"],
                assessment_orientations=["application"],
                allowed_question_types=["short_answer"],
                operational_detail_policy="supporting_only",
                scope_boundary={},
                required_evidence_roles=["answer_or_rubric_basis"],
                retrieval_intent="检索RAG应用依据",
                teaching_anchor_keys=[],
                status="confirmed",
            )
        )
        expanded_frozen_input = _frozen_input()
        expanded_frozen_input["exam_points"].append(
            {"id": "exam-point-2", "code": "EP-2"}
        )
        session.execute(
            organization_runs.update()
            .where(organization_runs.c.id == "organization-run")
            .values(input_snapshot=expanded_frozen_input)
        )
        session.commit()
        tree = _tree()
        tree.coverage.append(
            tree.coverage[0].model_copy(
                update={"exam_point_code": "EP-2", "direct_count": 1}
            )
        )
        state = _organization_state(tree)
        state["frozen_input"] = expanded_frozen_input
        repository = DatabaseKnowledgeRepository(session)
        candidate_id = repository.persist_candidate(state, tree)
        confirmation = _confirmation()
        confirmation.reviewed_exam_point_codes.append("EP-2")

        with pytest.raises(KnowledgePublishError, match="active.*chain"):
            repository.publish(
                {**state, "candidate_id": candidate_id},
                tree,
                confirmation,
            )
    finally:
        session.close()
        engine.dispose()
