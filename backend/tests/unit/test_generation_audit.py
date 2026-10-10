"""audit_paper_against_contract：答案域互斥终检（answer_mutex）。

互斥判据已从"整串互含"升级为"整串互含或分句级命题重叠"——换措辞的同命题
答案域（线上重复题事故形态）在整卷终检必须判失败，显式报给教师。
"""
from app.domain.generation.contract import ContractSlot
from app.services.generation_service import audit_paper_against_contract


def _slot(item_index: int, boundary: str) -> ContractSlot:
    return ContractSlot(
        item_index=item_index,
        question_type="single_choice",
        score=2,
        difficulty="medium",
        cognitive_level="understand",
        exam_point_id="EP1",
        anchor_key="A1",
        unit_id=f"U{item_index}",
        card_id=f"C{item_index}",
        coverage_atom=f"原子{item_index}",
        answer_boundary=boundary,
    )


def _question(item_index: int, boundary: str) -> dict:
    return {
        "item_index": item_index,
        "exam_point_id": "EP1",
        "unit_id": f"U{item_index}",
        "card_id": f"C{item_index}",
        "coverage_atom": f"原子{item_index}",
        "answer_boundary": boundary,
        "question_type": "single_choice",
        "difficulty": "medium",
        "cognitive_level": "understand",
        "stem": "关于该考点知识，下列说法正确的是？",
        "answer": "甲",
        "needs_review": False,
    }


def test_answer_mutex_flags_paraphrased_proposition_overlap():
    left = (
        "Agent调用invoke的返回值为包含完整消息历史的列表，"
        "最终回复需通过['messages'][-1].content获取"
    )
    right = (
        "Agent invoke返回完整消息历史，"
        "最终回复为消息列表最后一项的content字段"
    )
    report = audit_paper_against_contract(
        [_slot(1, left), _slot(2, right)],
        [_question(1, left), _question(2, right)],
    )
    mutex = next(c for c in report["checks"] if c["code"] == "answer_mutex")
    assert mutex["passed"] is False
    assert mutex["detail"]["collisions"] == [[1, 2]]
    assert report["passed"] is False


def test_answer_mutex_passes_for_disjoint_boundaries():
    left = "SFTTrainer需要SFTConfig传入超参"
    right = "QLoRA使用NF4量化降低显存占用"
    report = audit_paper_against_contract(
        [_slot(1, left), _slot(2, right)],
        [_question(1, left), _question(2, right)],
    )
    mutex = next(c for c in report["checks"] if c["code"] == "answer_mutex")
    assert mutex["passed"] is True
    assert mutex["detail"]["collisions"] == []
