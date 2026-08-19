"""合同→分批→生成→终检 全链路（fake 网关），断言设计成功标准六条。

fake 文案说明：判断题 answer 必须为布尔值（validate_generated_question 强制），
而布尔答案归一化为 "true"，无法命中中文答案域——因此判断题落到的卡
（每考点第 3 张卡，单选耗尽前两张后轮换到）answer_boundary 取含 "true" 的
互不相同的文本（"判断为 true" / "陈述成立为 true"）：布尔答案命中边界，
且两边界互不包含，全卷 answer_mutex 终检仍真实生效。
"""
from app.domain.blueprint.models import BlueprintRequest, UnitCoverage
from app.services.contract_service import ContractRequest, allocate_paper_contract
from app.workflows.generation_graph import build_generation_graph

UNITS = [
    UnitCoverage(unit_id="U1a", exam_point_id="EP1", anchor_key="A1", card_ids=["C1a"]),
    UnitCoverage(unit_id="U1b", exam_point_id="EP1", anchor_key="A1", card_ids=["C1b"]),
    UnitCoverage(unit_id="U1c", exam_point_id="EP1", anchor_key="A1", card_ids=["C1c"]),
    UnitCoverage(unit_id="U2a", exam_point_id="EP2", anchor_key="A2", card_ids=["C2a"]),
    UnitCoverage(unit_id="U2b", exam_point_id="EP2", anchor_key="A2", card_ids=["C2b"]),
    UnitCoverage(unit_id="U2c", exam_point_id="EP2", anchor_key="A2", card_ids=["C2c"]),
]

CARDS = {
    "C1a": {
        "is_core": True, "performance_statement": "掌握提示词角色与任务要素",
        "assessable_content": ["有效提示词包含角色设定要素", "有效提示词包含任务说明要求"],
        "preferred_terms": ["提示词"], "answer_boundary": "角色与任务要素",
    },
    "C1b": {
        "is_core": True, "performance_statement": "掌握提示词背景与格式要素",
        "assessable_content": ["提示词可加入背景信息补充", "提示词输出格式约束条件"],
        "preferred_terms": [], "answer_boundary": "背景与格式要素",
    },
    "C1c": {
        "is_core": True, "performance_statement": "掌握提示词评估与迭代方法",
        "assessable_content": ["提示词评估依赖输出质量判断"],
        "preferred_terms": [], "answer_boundary": "判断为 true",
    },
    "C2a": {
        "is_core": True, "performance_statement": "掌握SFT训练配置",
        "assessable_content": ["构建SFTTrainer需要SFTConfig配置", "训练学习率影响收敛效果"],
        "preferred_terms": ["SFT"], "answer_boundary": "SFT训练配置项",
    },
    "C2b": {
        "is_core": True, "performance_statement": "掌握参数高效微调方法",
        "assessable_content": ["QLoRA使用NF4量化格式压缩权重"],
        "preferred_terms": [], "answer_boundary": "参数高效微调机制",
    },
    "C2c": {
        "is_core": True, "performance_statement": "掌握领域适配训练",
        "assessable_content": ["继续预训练适配领域语料分布"],
        "preferred_terms": [], "answer_boundary": "陈述成立为 true",
    },
}


class ScriptedGateway:
    """按合同槽位确定性产出合格题目的假网关。"""

    def __init__(self):
        self.batch_count = 0

    def generate_batch(self, payload):
        self.batch_count += 1
        questions = []
        for spec in payload.questions:
            if spec.question_type == "true_false":
                questions.append({
                    "item_index": spec.item_index,
                    "stem": f"判断：下列关于{spec.coverage_atom}的陈述成立",
                    "answer": True, "explanation": "该陈述与课程内容一致",
                })
            else:
                answer_text = spec.answer_boundary or "标准答案表述"
                questions.append({
                    "item_index": spec.item_index,
                    "stem": f"关于{spec.coverage_atom}，下列说法最准确的是哪一项",
                    "options": [answer_text, "迷惑性的错误说法甲", "迷惑性的错误说法乙", "迷惑性的错误说法丙"],
                    "answer": answer_text,
                    "explanation": f"该题考查{spec.coverage_atom}，正确答案直接来源于答案域",
                })
        return questions


def _blueprint():
    return BlueprintRequest(
        total_score=10,
        type_rules={"single_choice": {"count": 4, "score": 2}, "true_false": {"count": 2, "score": 1}},
        chapter_weights={"A1": 50, "A2": 50},
        units=UNITS,
    )


def test_full_pipeline_meets_all_success_criteria():
    contract = allocate_paper_contract(ContractRequest(
        blueprint=_blueprint(), knowledge_cards=CARDS,
    ))
    # 合同层：无冲突、配额正确
    assert not contract.conflicts, contract.conflicts
    assert len(contract.slots) == 6
    ep_counts = {}
    for slot in contract.slots:
        ep_counts[slot.exam_point_id] = ep_counts.get(slot.exam_point_id, 0) + 1
    assert abs(ep_counts["EP1"] - 3) <= 1
    assert abs(ep_counts["EP2"] - 3) <= 1

    gateway = ScriptedGateway()
    result = build_generation_graph(gateway).invoke({
        "contract": [s.model_dump(mode="json") for s in contract.slots],
        "knowledge_cards": CARDS,
    })
    questions = sorted(result["questions"], key=lambda q: q["item_index"])
    report = result["final_check"]

    # 成功标准①：终检通过
    assert report["passed"] is True, report
    # 成功标准②：考点比例
    paper_ep = {}
    for q in questions:
        paper_ep[q["exam_point_id"]] = paper_ep.get(q["exam_point_id"], 0) + 1
    assert abs(paper_ep["EP1"] - 3) <= 1 and abs(paper_ep["EP2"] - 3) <= 1
    # 成功标准③：不重复（原子唯一）
    atoms = [q["coverage_atom"] for q in questions]
    assert len(atoms) == len(set(atoms)) == 6
    # 成功标准④：不抄袭（答案域互斥 + 无泄漏——由 final_check answer_mutex 与合同禁用保证）
    mutex_check = next(c for c in report["checks"] if c["code"] == "answer_mutex")
    assert mutex_check["passed"] and not mutex_check["detail"]["collisions"]
    # 成功标准⑤：可溯源
    for q in questions:
        assert q["exam_point_id"] and q["unit_id"] and q["card_id"] and q["coverage_atom"]
        assert q["quality"]["status"] == "pass"
        assert not q.get("needs_review")
    # 成功标准⑥：成本（2 考点批 + 0 重试）
    assert result["model_call_count"] == 2
    assert gateway.batch_count == 2


def test_api_level_flow_allocate_confirm_generate():
    """API 层全流程：allocate → confirm（无修订）→ generation-runs。"""
    from fastapi.testclient import TestClient
    from app.main import app

    app.state.generation_gateway = ScriptedGateway()
    client = TestClient(app)
    course = "/api/v1/courses/e2e-course"

    allocate_resp = client.post(f"{course}/blueprints/allocate", json={
        "blueprint": _blueprint().model_dump(mode="json"),
        "knowledge_cards": CARDS,
    })
    assert allocate_resp.status_code == 200
    contract = allocate_resp.json()
    assert not contract["conflicts"]
    assert len(contract["slots"]) == 6

    confirm_resp = client.post(f"{course}/blueprints/confirm", json={
        "contract": contract, "slot_revisions": [],
        "units": [u.model_dump(mode="json") for u in UNITS], "knowledge_cards": CARDS,
    })
    assert confirm_resp.status_code == 200
    confirmed = confirm_resp.json()

    gen_resp = client.post(f"{course}/generation-runs", json={
        "contract": confirmed["slots"], "knowledge_cards": CARDS,
    })
    assert gen_resp.status_code == 202
    body = gen_resp.json()
    assert body["status"] == "candidate"
    assert len(body["questions"]) == 6
    assert body["final_check"]["passed"] is True
    assert body["model_call_count"] == 2
    indexes = [q["item_index"] for q in body["questions"]]
    assert indexes == sorted(indexes)


def test_flawed_gateway_marks_review_not_blocks_pipeline():
    """部分题失败时：needs_review 标记 + 终检失败，但不阻塞其他题。"""
    contract = allocate_paper_contract(ContractRequest(
        blueprint=_blueprint(), knowledge_cards=CARDS,
    ))

    class HalfBadGateway(ScriptedGateway):
        def generate_batch(self, payload):
            questions = super().generate_batch(payload)
            # 第一批的题1 永远坏（含来源话术）
            for q in questions:
                if q["item_index"] == 1:
                    q["stem"] = "根据课件第3页的内容，此题必然违规"
            return questions

    result = build_generation_graph(HalfBadGateway()).invoke({
        "contract": [s.model_dump(mode="json") for s in contract.slots],
        "knowledge_cards": CARDS,
    })
    questions = sorted(result["questions"], key=lambda q: q["item_index"])
    bad = next(q for q in questions if q["item_index"] == 1)
    assert bad["needs_review"] is True
    good = [q for q in questions if q["item_index"] != 1]
    assert len(good) == 5 and all(q["quality"]["status"] == "pass" for q in good)
    assert result["final_check"]["passed"] is False
    # 成本上限：2 批 + 题1 重试 2 次 + 换原子重出 1 次 = 5
    assert result["model_call_count"] <= 5
