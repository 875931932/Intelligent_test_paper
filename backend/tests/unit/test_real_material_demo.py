from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from app.domain.framework.exam_points import ExamPoint, OperationalDetailPolicy, WeightSource
from app.domain.knowledge.models import (
    AssessmentUnitDraft,
    KnowledgeCardDraft,
    KnowledgeTopicDraft,
    KnowledgeTreeCandidate,
)
from app.domain.knowledge.relevance import StagingChunk


def _load_demo_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "build_real_material_demo.py"
    specification = importlib.util.spec_from_file_location("real_material_demo", script)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_lexical_preselection_keeps_matching_chunks_without_embeddings():
    demo = _load_demo_module()
    point = ExamPoint(
        code="EP-1",
        anchor_key="retrieval",
        title="检索诊断",
        assessment_requirement="能够分析检索遗漏的原因",
        weight_value=10,
        weight_source=WeightSource.ASSESSMENT_SYLLABUS,
        weight_group_id="retrieval",
        operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
        retrieval_intent="检索链路诊断",
    )
    matching = StagingChunk(
        id="matching",
        material_version_id="m",
        content="检索链路诊断需要分析召回遗漏。",
    )
    noise = StagingChunk(
        id="noise",
        material_version_id="m",
        content="安装软件并提交截图。",
    )

    result = demo.select_lexical_candidates(point, [noise, matching], limit=3)

    assert [chunk.id for chunk in result] == ["matching", "noise"]


def test_candidate_document_selection_bounds_model_pairs():
    demo = _load_demo_module()
    point = ExamPoint(
        code="EP-1",
        anchor_key="retrieval",
        title="检索诊断",
        assessment_requirement="能够分析检索遗漏的原因",
        weight_value=10,
        weight_source=WeightSource.ASSESSMENT_SYLLABUS,
        weight_group_id="retrieval",
        operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
        retrieval_intent="检索链路诊断",
    )
    documents = [{"sha256": "a"}, {"sha256": "b"}, {"sha256": "c"}]
    chunks_by_material = {
        "a": [StagingChunk(id="a", material_version_id="a", content="安装软件")],
        "b": [StagingChunk(id="b", material_version_id="b", content="检索链路诊断")],
        "c": [StagingChunk(id="c", material_version_id="c", content="检索结果遗漏")],
    }

    result = demo.select_candidate_documents(point, documents, chunks_by_material, limit=2)

    assert [item["sha256"] for item in result] == ["b", "c"]


def test_demo_blueprint_does_not_reset_card_rotation_between_question_types():
    demo = _load_demo_module()
    point = ExamPoint(
        code="EP-1",
        anchor_key="chapter",
        title="课程考点",
        assessment_requirement="能够理解并应用课程核心知识",
        weight_value=100,
        weight_source=WeightSource.ASSESSMENT_SYLLABUS,
        weight_group_id="chapter",
        cognitive_targets=["understand", "apply"],
        assessment_orientations=["conceptual"],
        operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
        retrieval_intent="课程核心知识",
    )
    units = [
        AssessmentUnitDraft(
            code=f"U-{index:02d}",
            title=f"知识点{index}",
            performance_statement=f"能够解释知识点{index}",
            exam_point_code=point.code,
            cards=[
                KnowledgeCardDraft(
                    name=f"知识卡{index}",
                    performance_statement=f"能够解释知识点{index}",
                    assessable_content=[f"知识事实{index}"],
                    cognitive_targets=["understand", "apply"],
                    allowed_question_types=["single_choice", "true_false", "fill_blank"],
                )
            ],
        )
        for index in range(40)
    ]
    tree = KnowledgeTreeCandidate(
        framework_version_id="framework",
        topics=[
            KnowledgeTopicDraft(
                code="topic",
                name="课程主题",
                framework_anchor_key="chapter",
                units=units,
            )
        ],
    )

    blueprint, cards = demo.build_blueprint([point], tree)
    objective_items = blueprint["plan"]["items"][:20]

    assert len({item["card_id"] for item in objective_items}) == 20
    assert all(card["exam_point_id"] == point.code for card in cards.values())
    assert all(card["unit_id"].startswith("U-") for card in cards.values())


def test_assessable_fact_content_is_published_as_atomic_cards():
    demo = _load_demo_module()
    point = ExamPoint(
        code="EP-1",
        anchor_key="chapter",
        title="模型评估",
        assessment_requirement="能够解释评估参数的作用",
        weight_value=10,
        weight_source=WeightSource.ASSESSMENT_SYLLABUS,
        weight_group_id="chapter",
        operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
        retrieval_intent="模型评估参数",
    )
    facts = [
        {
            "evidence_chunk_id": "chunk-1",
            "name": "评估参数",
            "assessable_content": ["参数甲控制推理方式", "参数乙控制批大小"],
        }
    ]

    units = demo.build_atomic_units(point, facts)

    assert [unit.code for unit in units] == ["EP-1-U1", "EP-1-U2"]
    assert [unit.cards[0].assessable_content for unit in units] == [
        ["参数甲控制推理方式"],
        ["参数乙控制批大小"],
    ]
    assert all(unit.cards[0].evidence_chunk_ids == ["chunk-1"] for unit in units)


def test_multi_content_fact_is_exploded_before_semantic_profiling():
    demo = _load_demo_module()

    atomic = demo.explode_atomic_facts(
        [
            {
                "evidence_chunk_id": "chunk-1",
                "name": "训练参数",
                "assessable_content": ["参数甲控制更新幅度", "参数乙控制批大小"],
            }
        ]
    )

    assert atomic == [
        {
            "evidence_chunk_id": "chunk-1",
            "name": "参数甲控制更新幅度",
            "assessable_content": ["参数甲控制更新幅度"],
        },
        {
            "evidence_chunk_id": "chunk-1",
            "name": "参数乙控制批大小",
            "assessable_content": ["参数乙控制批大小"],
        },
    ]


def test_multi_clause_fact_is_split_into_independent_atoms():
    demo = _load_demo_module()

    atomic = demo.explode_atomic_facts(
        [
            {
                "evidence_chunk_id": "chunk-1",
                "name": "模型评估",
                "assessable_content": [
                    "模型评估用于衡量模型在未知数据上的泛化能力；训练损失低仅表示拟合训练集"
                ],
            }
        ]
    )

    assert [row["assessable_content"][0] for row in atomic] == [
        "模型评估用于衡量模型在未知数据上的泛化能力",
        "训练损失低仅表示拟合训练集",
    ]


def test_case_narrative_facts_never_enter_knowledge_cards():
    # 案例讲解的情境背景（上一轮训练/本次实验/我们的实验）不进卡片：
    # 知识卡是 RAG 检索库源头，汇聚点过滤兜底
    demo = _load_demo_module()

    card = demo.source_free_card(
        {
            "name": "数据失衡",
            "assessable_content": [
                "思考模式数据与非思考模式数据分布不均衡的问题出现在上一轮训练中。",
                "本次 QLoRA 微调实验使用本地 Qwen3-0.6B 作为基座模型。",
                "混合训练数据集的构建目标是解决两类数据分布不均衡的问题。",
            ],
        },
        "说明数据均衡",
    )

    assert card["assessable_content"] == [
        "混合训练数据集的构建目标是解决两类数据分布不均衡的问题。"
    ]


def test_admitted_evidence_is_partitioned_by_material_before_extraction():
    demo = _load_demo_module()
    chunks = {
        "chunk-a": StagingChunk(id="chunk-a", material_version_id="material-a", content="事实A"),
        "chunk-b": StagingChunk(id="chunk-b", material_version_id="material-b", content="事实B"),
    }
    decisions = [
        SimpleNamespace(
            evidence_chunk_id="chunk-a",
            material_version_id="material-a",
            support_claim="支持事实A",
        ),
        SimpleNamespace(
            evidence_chunk_id="chunk-b",
            material_version_id="material-b",
            support_claim="支持事实B",
        ),
    ]

    grouped = demo.group_admitted_evidence_by_material(decisions, chunks)

    assert set(grouped) == {"material-a", "material-b"}
    assert grouped["material-a"][0]["evidence_chunk_id"] == "chunk-a"


def test_fact_extraction_maps_evidence_index_back_to_real_id():
    demo = _load_demo_module()
    evidence = [
        {"evidence_chunk_id": "chunk-a", "content": "事实A"},
        {"evidence_chunk_id": "chunk-b", "content": "事实B"},
    ]

    facts = demo.validate_extracted_facts(
        {
            "facts": [
                {
                    "evidence_index": 0,
                    "name": "事实A",
                    "assessable_content": ["事实A"],
                },
                {
                    "evidence_index": 1,
                    "name": "事实B",
                    "assessable_content": ["事实B"],
                }
            ]
        },
        evidence,
    )

    assert facts[1]["evidence_chunk_id"] == "chunk-b"


def test_fact_extraction_accepts_one_based_evidence_indexes():
    demo = _load_demo_module()
    evidence = [
        {"evidence_chunk_id": "chunk-a", "content": "事实A"},
        {"evidence_chunk_id": "chunk-b", "content": "事实B"},
    ]

    facts = demo.validate_extracted_facts(
        {
            "facts": [
                {
                    "evidence_index": 2,
                    "name": "事实B",
                    "assessable_content": ["事实B"],
                }
            ]
        },
        evidence,
    )

    assert facts[0]["evidence_chunk_id"] == "chunk-b"


def test_capability_family_groups_merge_surface_different_cards():
    demo = _load_demo_module()
    units = [
        AssessmentUnitDraft(
            code="EP-1-U1",
            title="角色设定",
            performance_statement="理解提示词控制方式",
            exam_point_code="EP-1",
            cards=[
                KnowledgeCardDraft(
                    name="角色作用",
                    performance_statement="理解提示词控制方式",
                    assessable_content=["角色设定影响模型输出"],
                    concept_cluster="角色设定",
                    answer_proposition="角色设定影响模型输出",
                ),
                KnowledgeCardDraft(
                    name="SYSTEM作用",
                    performance_statement="理解提示词控制方式",
                    assessable_content=["SYSTEM提示词设定默认角色"],
                    concept_cluster="SYSTEM提示词",
                    answer_proposition="SYSTEM提示词设定默认角色",
                ),
            ],
        )
    ]

    normalized = demo.apply_capability_family_groups(
        units,
        {"groups": [{"card_indexes": [0, 1], "concept_cluster": "提示词角色与指令控制"}]},
    )

    assert [card.concept_cluster for card in normalized[0].cards] == [
        "提示词角色与指令控制",
        "提示词角色与指令控制",
    ]


def test_atomic_fact_keeps_semantic_profile_separate_from_source_location():
    demo = _load_demo_module()
    point = ExamPoint(
        code="EP-1",
        anchor_key="network",
        title="接口兼容",
        assessment_requirement="能够比较两类接口",
        weight_value=10,
        weight_source=WeightSource.ASSESSMENT_SYLLABUS,
        weight_group_id="network",
        operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
        retrieval_intent="接口兼容关系",
    )

    unit = demo.build_atomic_units(
        point,
        [
            {
                "evidence_chunk_id": "evidence-1",
                "name": "接口关系",
                "assessable_content": ["两类接口采用不同路径前缀"],
                "concept_cluster": "接口兼容与调用",
                "answer_proposition": "两类接口采用不同路径前缀",
                "required_propositions": [],
                "relation_edges": [],
                "instance_carriers": [],
            }
        ],
    )[0]

    card = unit.cards[0]
    assert card.concept_cluster == "接口兼容与调用"
    assert card.answer_proposition == "两类接口采用不同路径前缀"
    assert "evidence-1" not in card.answer_proposition


def test_semantic_profiles_are_merged_by_fact_index_without_source_metadata():
    demo = _load_demo_module()
    facts = [
        {
            "evidence_chunk_id": "evidence-1",
            "name": "接口关系",
            "assessable_content": ["两类接口采用不同路径前缀"],
        },
        {
            "evidence_chunk_id": "evidence-2",
            "name": "身份校验",
            "assessable_content": ["访问令牌用于身份校验"],
        },
    ]

    merged = demo.merge_semantic_profiles(
        facts,
        {
            "profiles": [
                {
                    "fact_index": 0,
                    "concept_cluster": "接口兼容与调用",
                    "answer_proposition": "两类接口采用不同路径前缀",
                    "required_propositions": [],
                    "relation_edges": [],
                    "instance_carriers": [],
                },
                {
                    "fact_index": 1,
                    "concept_cluster": "访问控制",
                    "answer_proposition": "访问令牌用于身份校验",
                    "required_propositions": [],
                    "relation_edges": [],
                    "instance_carriers": [],
                },
            ]
        },
    )

    assert merged[0]["evidence_chunk_id"] == "evidence-1"
    assert merged[0]["concept_cluster"] == "接口兼容与调用"
    assert merged[1]["answer_proposition"] == "访问令牌用于身份校验"


def test_semantic_profiles_must_cover_each_fact_exactly_once():
    demo = _load_demo_module()
    facts = [
        {
            "evidence_chunk_id": "evidence-1",
            "name": "知识事实",
            "assessable_content": ["一个独立知识事实"],
        }
    ]

    try:
        demo.merge_semantic_profiles(facts, {"profiles": []})
    except ValueError as exc:
        assert "every extracted fact" in str(exc)
    else:
        raise AssertionError("missing semantic profile must be rejected")


def test_snapshot_write_retries_a_transient_replace_lock(tmp_path, monkeypatch):
    demo = _load_demo_module()
    output_file = tmp_path / "pipeline.json"
    monkeypatch.setattr(demo, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(demo, "OUTPUT_FILE", output_file)
    real_replace = Path.replace
    attempts = 0

    def replace_after_one_lock(path, target):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("transient file watcher lock")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", replace_after_one_lock)

    demo.write_snapshot({"status": "testing"})

    assert attempts == 2
    assert output_file.read_text(encoding="utf-8").find('"testing"') > 0


def test_published_evidence_candidate_aggregates_all_atomic_card_facts():
    demo = _load_demo_module()
    units = [
        AssessmentUnitDraft(
            code=f"EP-1-U{index}",
            title=f"事实{index}",
            performance_statement="能够解释两个独立事实",
            exam_point_code="EP-1",
            cards=[
                KnowledgeCardDraft(
                    name=f"事实{index}",
                    performance_statement="能够解释两个独立事实",
                    assessable_content=[f"独立事实{index}"],
                    evidence_chunk_ids=["chunk-1"],
                )
            ],
        )
        for index in (1, 2)
    ]

    candidates = demo.aggregate_published_evidence({"EP-1": units})
    _, candidate = candidates[("EP-1", "chunk-1")]

    assert candidate.assessable_content == ["独立事实1", "独立事实2"]


def test_assessable_fact_rejects_evidence_reference_language():
    demo = _load_demo_module()

    assert demo.is_source_free_assessable_fact("OpenAI兼容接口可返回模型结果")
    assert demo.is_source_free_assessable_fact("文件系统使用 inode 标识文件")
    assert demo.is_source_free_assessable_fact("训练资料经过清洗后用于模型训练")
    assert demo.is_source_free_assessable_fact("教材内容采用分层结构组织")
    assert not demo.is_source_free_assessable_fact("按照实验手册可完成OpenAI兼容接口调用")
    assert not demo.is_source_free_assessable_fact("根据课件第3页说明接口配置")
    assert not demo.is_source_free_assessable_fact("根据资料说明分析检索结果")
    assert not demo.is_source_free_assessable_fact("文件名为 train.csv")
    assert not demo.is_source_free_assessable_fact("实验编号为 3")


def test_outline_extraction_keeps_the_complete_matching_section():
    demo = _load_demo_module()
    document = {
        "material_type": "teaching_syllabus",
        "blocks": [
            {"heading_path": ["一、课程概况"], "text": "课程概况"},
            {
                "heading_path": ["二、课程教学内容"],
                "text": "课程教学内容与要求",
            },
            {
                "heading_path": ["二、课程教学内容", "第一章"],
                "text": "考点一：解释基本概念",
            },
            {
                "heading_path": ["二、课程教学内容", "第一章"],
                "text": "考点二：比较核心机制",
            },
            {
                "heading_path": ["二、课程教学内容", "第二章"],
                "text": "考点三：分析处理流程",
            },
            {
                "heading_path": ["二、课程教学内容", "第二章"],
                "text": "考点四：应用关键方法",
            },
            {"heading_path": ["三、实践安排"], "text": "实验安排与提交要求"},
        ],
    }

    result = demo.outline_extraction_blocks(document)

    for exam_point in ("考点一", "考点二", "考点三", "考点四"):
        assert any(exam_point in block for block in result)
    assert all("实验安排与提交要求" not in block for block in result)


def test_fact_extraction_target_scales_with_exam_weight_and_is_bounded():
    demo = _load_demo_module()

    # 目标 ≈ 配额(w*0.7) × 1.7：池子必须明显大于题位配额，
    # 分配器才有选择自由（否则每卷被迫选同样的原子）
    assert demo.target_fact_count(5) == 6
    assert demo.target_fact_count(10) == 12
    assert demo.target_fact_count(25) == 30
    assert demo.target_fact_count(35) == 30
    assert demo.target_fact_count(100) == 30


def test_support_claim_fallback_publishes_distinct_atomic_units():
    demo = _load_demo_module()
    point = ExamPoint(
        code="EP-1",
        anchor_key="chapter",
        title="服务化部署",
        assessment_requirement="能够解释服务化部署流程",
        weight_value=15,
        weight_source=WeightSource.ASSESSMENT_SYLLABUS,
        weight_group_id="chapter",
        operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
        retrieval_intent="服务化部署流程",
    )

    units = demo.build_support_claim_units(
        point,
        [
            {"evidence_chunk_id": "chunk-1", "support_claim": "服务端需要常驻运行"},
            {"evidence_chunk_id": "chunk-2", "support_claim": "客户端通过稳定接口发起请求"},
        ],
    )

    assert [unit.code for unit in units] == ["EP-1-U1", "EP-1-U2"]
    assert [unit.cards[0].assessable_content for unit in units] == [
        ["服务端需要常驻运行"],
        ["客户端通过稳定接口发起请求"],
    ]
    assert len({unit.title for unit in units}) == 2
