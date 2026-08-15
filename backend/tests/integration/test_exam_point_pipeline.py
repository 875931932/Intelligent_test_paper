from __future__ import annotations

from app.domain.blueprint.models import BlueprintRequest, UnitCoverage
from app.domain.framework.exam_points import (
    ExamPoint,
    OperationalDetailPolicy,
    WeightSource,
)
from app.domain.knowledge.models import AssessmentUnitDraft, KnowledgeCardDraft
from app.domain.knowledge.relevance import (
    AssessmentUnitCandidate,
    ContentKind,
    EvidenceDecision,
    ExamPointFileDecision,
    KnowledgeCardCandidate,
    RelevanceClass,
    admit_evidence_decision,
)
from app.services.blueprint_service import allocate_plan_items
from app.workflows.knowledge_catalog_subgraph import build_knowledge_catalog_candidate


def _exam_point(
    code: str = "EP-RAG",
    *,
    anchor_key: str = "rag",
    title: str = "RAG 基本流程",
) -> ExamPoint:
    return ExamPoint(
        code=code,
        anchor_key=anchor_key,
        title=title,
        assessment_requirement="说明 RAG 的组成并分析各阶段作用",
        weight_value=100,
        weight_source=WeightSource.ASSESSMENT_SYLLABUS,
        weight_group_id=anchor_key,
        cognitive_targets=["understand", "analyze"],
        assessment_orientations=["conceptual", "problem_solving"],
        allowed_question_types=["single_choice", "short_answer"],
        operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
        required_evidence_roles=["answer_or_rubric_basis"],
        retrieval_intent="检索 RAG 流程的理论事实、答案依据和可用场景",
        assessment_anchor_keys=[anchor_key],
        teaching_anchor_keys=["taught-rag"],
    )


def _decision(
    evidence_id: str,
    *,
    point_code: str = "EP-RAG",
    fact: str = "RAG 包括检索、上下文构造和生成三个阶段",
    relevance: RelevanceClass = RelevanceClass.DIRECT,
    content_kind: ContentKind = ContentKind.FACT,
    unit_title: str = "分析 RAG 流程",
    card_name: str = "RAG 基本流程",
    prompt_material: str | None = None,
) -> EvidenceDecision:
    return EvidenceDecision(
        exam_point_code=point_code,
        evidence_chunk_id=evidence_id,
        relevance_class=relevance,
        support_claim=fact,
        evidence_role="answer_or_rubric_basis",
        content_kind=content_kind,
        candidate_assessment_unit=AssessmentUnitCandidate(
            code=f"unit-{point_code}",
            title=unit_title,
            performance_statement="能够说明并分析 RAG 流程",
        ),
        candidate_card_content=KnowledgeCardCandidate(
            name=card_name,
            performance_statement="能够说明 RAG 的组成及各阶段作用",
            assessable_content=[fact],
            cognitive_targets=["understand", "analyze"],
            allowed_question_types=["single_choice", "short_answer"],
        ),
        prompt_material=prompt_material,
        confidence=90,
    )


def _direct_unit() -> AssessmentUnitDraft:
    return AssessmentUnitDraft(
        code="unit-EP-RAG",
        title="分析 RAG 流程",
        performance_statement="能够说明并分析 RAG 流程",
        exam_point_code="EP-RAG",
        cards=[
            KnowledgeCardDraft(
                name="RAG 基本流程",
                performance_statement="能够说明 RAG 的组成及各阶段作用",
                assessable_content=["RAG 包括检索、上下文构造和生成三个阶段"],
                cognitive_targets=["understand", "analyze"],
                allowed_question_types=["single_choice", "short_answer"],
                evidence_chunk_ids=["theory-direct"],
            )
        ],
    )


def _build_tree(*, operational_material_count: int):
    point = _exam_point()
    files = [
        ExamPointFileDecision(
            exam_point_code=point.code,
            material_version_id="theory-material",
            decisions=[_decision("theory-direct")],
        )
    ]
    files.extend(
        ExamPointFileDecision(
            exam_point_code=point.code,
            material_version_id=f"operation-material-{index:02d}",
            decisions=[
                _decision(
                    f"operation-{index:02d}",
                    fact=f"运行脚本并提交第 {index} 份截图",
                    content_kind=ContentKind.OPERATIONAL_DETAIL,
                    unit_title=f"操作步骤 {index}",
                    card_name=f"实验操作 {index}",
                    prompt_material=f"场景条件 {index}",
                )
            ],
        )
        for index in range(operational_material_count)
    )
    return build_knowledge_catalog_candidate(
        framework_version_id="framework-v1",
        exam_points=[point],
        file_decisions=files,
        consolidated_units={point.code: [_direct_unit()]},
    )


def _blueprint_request(tree) -> BlueprintRequest:
    units = [
        UnitCoverage(
            unit_id=unit.code,
            exam_point_id=unit.exam_point_code,
            anchor_key=topic.framework_anchor_key,
            card_ids=[f"{unit.code}:card:{index}" for index, _ in enumerate(unit.cards)],
            allowed_assessment_modes=[
                "theory_recall",
                "conceptual",
                "application",
                "problem_solving",
                "practical_operation",
            ],
            operational_detail_policy="supporting_only",
        )
        for topic in tree.topics
        for unit in topic.units
        if unit.status == "active"
    ]
    return BlueprintRequest(
        total_score=20,
        type_rules={
            "single_choice": {
                "count": 10,
                "score": 2,
                "assessment_mode_distribution": {
                    "theory_recall": 40,
                    "conceptual": 30,
                    "application": 10,
                    "problem_solving": 20,
                    "practical_operation": 0,
                },
            }
        },
        chapter_weights={"rag": 100},
        units=units,
        card_question_types={units[0].card_ids[0]: ["single_choice"]},
    )


def test_exam_point_led_tree_and_blueprint_ignore_supporting_material_volume():
    sparse_tree = _build_tree(operational_material_count=1)
    dense_tree = _build_tree(operational_material_count=40)

    assert sparse_tree.topics == dense_tree.topics
    coverage = dense_tree.coverage[0]
    assert coverage.status == "sufficient"
    assert coverage.direct_count == 1
    assert coverage.supporting_count == 40

    active_units = [
        unit
        for topic in dense_tree.topics
        for unit in topic.units
        if unit.status == "active"
    ]
    assert active_units
    assert all(unit.exam_point_code == "EP-RAG" for unit in active_units)
    active_cards = [card for unit in active_units for card in unit.cards if card.status == "active"]
    assert len(active_cards) == 1
    assert active_cards[0].evidence_chunk_ids == ["theory-direct"]
    assert active_cards[0].assessable_content == [
        "RAG 包括检索、上下文构造和生成三个阶段"
    ]

    sparse_plan = allocate_plan_items(_blueprint_request(sparse_tree))
    dense_plan = allocate_plan_items(_blueprint_request(dense_tree))

    assert sparse_plan == dense_plan
    assert dense_plan.assessment_mode_counts["single_choice"] == {
        "theory_recall": 4,
        "conceptual": 3,
        "application": 1,
        "problem_solving": 2,
        "practical_operation": 0,
    }
    assert sum(dense_plan.assessment_mode_counts["single_choice"].values()) == 10
    assert all(item.assessment_mode != "practical_operation" for item in dense_plan.items)


def test_low_relevance_and_single_point_failure_do_not_create_fallback_cards():
    main_point = _exam_point()
    failed_point = _exam_point(
        "EP-FAILED",
        anchor_key="failed-anchor",
        title="实验报告封面与附件目录",
    )
    file_decisions = [
        ExamPointFileDecision(
            exam_point_code=main_point.code,
            material_version_id="main-material",
            decisions=[
                _decision("theory-direct"),
                _decision(
                    "background",
                    fact="课程建设历史背景",
                    relevance=RelevanceClass.BACKGROUND,
                    content_kind=ContentKind.BACKGROUND,
                    unit_title="课程背景",
                    card_name="课程建设历史",
                ),
                _decision(
                    "outside",
                    fact="竞赛报名与材料提交说明",
                    relevance=RelevanceClass.OUT_OF_SCOPE,
                    unit_title="附件目录",
                    card_name="提交文件清单",
                ),
            ],
        ),
        ExamPointFileDecision(
            exam_point_code=failed_point.code,
            material_version_id="failed-material",
            decisions=[
                _decision(
                    "failed-outside",
                    point_code=failed_point.code,
                    fact="文件封面、命名和截图提交要求",
                    relevance=RelevanceClass.OUT_OF_SCOPE,
                    unit_title="实验报告封面",
                    card_name="附件目录",
                )
            ],
        ),
    ]

    tree = build_knowledge_catalog_candidate(
        framework_version_id="framework-v1",
        exam_points=[main_point, failed_point],
        file_decisions=file_decisions,
        consolidated_units={main_point.code: [_direct_unit()]},
        coverage_reasons={failed_point.code: ["single_file_consolidation_failed"]},
    )

    coverage = {item.exam_point_code: item for item in tree.coverage}
    assert coverage[main_point.code].status == "sufficient"
    assert coverage[failed_point.code].status == "insufficient"
    assert "single_file_consolidation_failed" in coverage[failed_point.code].reasons
    assert "single_file_consolidation_failed" not in coverage[main_point.code].reasons

    card_names = [card.name for topic in tree.topics for unit in topic.units for card in unit.cards]
    assert card_names == ["RAG 基本流程"]
    assert all(unit.exam_point_code == main_point.code for topic in tree.topics for unit in topic.units)
    quarantined = [
        item
        for item in tree.evidence_decisions
        if item.relevance_class in {RelevanceClass.BACKGROUND, RelevanceClass.OUT_OF_SCOPE}
    ]
    assert quarantined
    assert all(item.candidate_assessment_unit is None for item in quarantined)
    assert all(item.candidate_card_content is None for item in quarantined)


def test_operational_policy_controls_admission_without_course_specific_blacklists():
    point = _exam_point()
    same_surface_fact = "config.json 文件名用于标识配置内容"
    conceptual = admit_evidence_decision(
        point,
        _decision(
            "conceptual-file-term",
            fact=same_surface_fact,
            content_kind=ContentKind.FACT,
            unit_title="实验报告封面",
            card_name="config.json 文件名",
        ),
    )
    operational = admit_evidence_decision(
        point,
        _decision(
            "operational-file-term",
            fact=same_surface_fact,
            content_kind=ContentKind.OPERATIONAL_DETAIL,
            unit_title="实验报告封面",
            card_name="config.json 文件名",
            prompt_material="给定一份配置错误场景",
        ),
    )

    assert conceptual.relevance_class is RelevanceClass.DIRECT
    assert conceptual.candidate_card_content is not None
    assert operational.relevance_class is RelevanceClass.SUPPORTING
    assert operational.candidate_card_content is None
    assert operational.prompt_material == "给定一份配置错误场景"
