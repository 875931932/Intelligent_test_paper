from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.schema import Base, generated_questions, generation_runs, paper_items, paper_versions
from app.domain.generation.structure_signature import build_structure_signature, load_recent_structure_signatures


def test_structure_signature_normalizes_synonymous_actions_and_boundary_wording():
    first = build_structure_signature(
        archetype="fault_diagnosis",
        material_form="symptom_list",
        cognitive_sequence=["analyze", "apply"],
        subquestion_actions=["定位原因", "提出修正"],
        answer_boundaries=["说明故障原因", "提出修正措施"],
    )
    second = build_structure_signature(
        archetype="fault_diagnosis",
        material_form="symptom_list",
        cognitive_sequence=["analyze", "apply"],
        subquestion_actions=["分析成因", "给出改进"],
        answer_boundaries=["分析问题成因", "给出改进方案"],
    )

    assert first.structure_key == second.structure_key
    assert first.signature_hash == second.signature_hash
    assert first.subquestion_actions == ["diagnose", "repair"]


def test_structure_signature_changes_when_the_structure_changes():
    diagnosis = build_structure_signature(
        archetype="fault_diagnosis",
        material_form="symptom_list",
        cognitive_sequence=["analyze", "apply"],
        subquestion_actions=["定位原因", "提出修正"],
        answer_boundaries=["原因", "修正措施"],
    )
    decision = build_structure_signature(
        archetype="comparative_decision",
        material_form="constraint_table",
        cognitive_sequence=["analyze", "evaluate"],
        subquestion_actions=["比较方案", "作出选择"],
        answer_boundaries=["比较依据", "选择结论"],
    )

    assert diagnosis.structure_key != decision.structure_key
    assert diagnosis.signature_hash != decision.signature_hash


def test_recent_signature_loader_uses_latest_five_confirmed_or_exported_papers_for_course():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 15, tzinfo=UTC)

    with Session(engine) as session:
        for index in range(1, 8):
            course_id = "course" if index != 7 else "other-course"
            run_id = f"run-{index}"
            paper_id = f"paper-{index}"
            question_id = f"question-{index}"
            signature = build_structure_signature(
                archetype="fault_diagnosis",
                material_form="symptom_list",
                cognitive_sequence=["analyze", "apply"],
                subquestion_actions=["定位原因", f"设计方案 {index}"],
                answer_boundaries=["原因", "方案"],
            )
            session.execute(
                generation_runs.insert().values(
                    id=run_id,
                    course_id=course_id,
                    framework_version_id=f"framework-{index}",
                    catalog_version_id=f"catalog-{index}",
                    index_version_id=f"index-{index}",
                    blueprint_version_id=f"blueprint-{index}",
                    prompt_template_version="v1",
                    run_type="paper",
                    status="completed",
                    created_at=now + timedelta(minutes=index),
                )
            )
            paper_status = "draft" if index == 6 else "confirmed" if index % 2 else "exported"
            session.execute(
                paper_versions.insert().values(
                    id=paper_id,
                    course_id=course_id,
                    exam_project_id=f"project-{index}",
                    generation_run_id=run_id,
                    version_no=index,
                    status=paper_status,
                )
            )
            payload = {
                "question_type": "comprehensive",
                "stem": f"old stem {index}",
                "answer": f"old answer {index}",
                "source": {"filename": f"secret-{index}.pdf"},
                "structure_signature": signature.model_dump(),
            }
            if index == 5:
                payload["structure_signature"] = {"malformed": True}
            session.execute(
                generated_questions.insert().values(
                    id=question_id,
                    course_id=course_id,
                    generation_run_id=run_id,
                    plan_item_id=f"plan-{index}",
                    revision_no=1,
                    status="candidate",
                    payload=payload,
                )
            )
            session.execute(
                paper_items.insert().values(
                    id=f"item-{index}",
                    course_id=course_id,
                    paper_version_id=paper_id,
                    generated_question_id=question_id,
                    display_order=1,
                )
            )
        session.commit()

        loaded = load_recent_structure_signatures(session, "course", paper_limit=5)

    assert len(loaded) == 4
    assert all(signature.structure_key for signature in loaded)
    serialized = str([signature.model_dump() for signature in loaded]).lower()
    for forbidden in ("old stem", "old answer", "secret", "filename", "source"):
        assert forbidden not in serialized
