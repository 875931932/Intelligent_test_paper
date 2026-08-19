"""合同驱动生成图测试：分批并行、批内互见、单题重试、合并终检。"""
import pytest

from app.schemas.generation import BatchGenerationPayload
from app.workflows.generation_graph import build_generation_graph


def _slot(index: int, point: str = "EP1", **overrides) -> dict:
    payload = dict(
        item_index=index, question_type="single_choice", score=2, difficulty="medium",
        cognitive_level="understand", assessment_mode="conceptual",
        exam_point_id=point, anchor_key="A1", unit_id=f"U-{point}", card_id=f"C{index}",
        coverage_atom=f"原子{index}", answer_boundary=f"边界{index}",
        performance_statement="掌握某知识",
        forbidden_context={"atoms": [], "answer_cores": []},
    )
    payload.update(overrides)
    return payload


def _question(index: int, **overrides) -> dict:
    payload = dict(
        item_index=index, question_type="single_choice",
        stem=f"关于原子{index}的知识，下列说法正确的是哪一项",
        options=["正确的选项内容", "干扰项一", "干扰项二", "干扰项三"],
        answer="正确的选项内容", explanation="解析文本", difficulty="medium",
    )
    payload.update(overrides)
    return payload


class FakeBatchGateway:
    """按批返回题目的假网关。scenarios[item_index] 是该题的失败剧本序列：
    批调用返回剧本[0]；第k次重试返回剧本[k]；剧本耗尽返回合格题。"""

    def __init__(self, scenarios: dict[int, list[dict]] | None = None):
        self.scenarios = scenarios or {}
        self.batch_payloads: list[BatchGenerationPayload] = []
        self.retry_payloads: list[BatchGenerationPayload] = []
        self._consumed: dict[int, int] = {}

    def _script_step(self, item_index: int) -> dict:
        script = self.scenarios.get(item_index, [])
        used = self._consumed.get(item_index, 0)
        self._consumed[item_index] = used + 1
        return script[used] if used < len(script) else _question(item_index)

    def generate_batch(self, payload: BatchGenerationPayload) -> list[dict]:
        if len(payload.questions) == 1:
            spec = payload.questions[0]
            self.retry_payloads.append(payload)
            return [self._script_step(spec.item_index)]
        self.batch_payloads.append(payload)
        return [self._script_step(spec.item_index) for spec in payload.questions]


def _state(slots: list[dict]) -> dict:
    return {"contract": slots, "knowledge_cards": {}}


def test_two_points_run_as_two_batches_and_merge_sorted():
    slots = [_slot(i, "EP1") for i in (1, 2, 3)] + [_slot(i, "EP2") for i in (4, 5, 6)]
    gateway = FakeBatchGateway()
    result = build_generation_graph(gateway).invoke(_state(slots))
    assert [q["item_index"] for q in sorted(result["questions"], key=lambda q: q["item_index"])] == [1, 2, 3, 4, 5, 6]
    assert len(gateway.batch_payloads) == 2
    assert result["model_call_count"] == 2


def test_batch_payload_carries_forbidden_context_for_split_batches():
    slots = [_slot(i, "EP1") for i in range(1, 9)]  # 8 题拆两子批
    gateway = FakeBatchGateway()
    build_generation_graph(gateway).invoke(_state(slots))
    first = min(gateway.batch_payloads, key=lambda p: min(q.item_index for q in p.questions))
    assert first.forbidden_atoms  # 子批1 携带子批2 原子
    assert "原子7" in first.forbidden_atoms


def test_failed_question_retries_at_most_twice_then_needs_review():
    bad = _question(1, stem="根据课件第3页的内容，关于原子1的问题", options=["甲"], answer="")
    slots = [_slot(1), _slot(2)]
    gateway = FakeBatchGateway(scenarios={1: [bad, bad, bad]})
    result = build_generation_graph(gateway).invoke(_state(slots))
    question = next(q for q in result["questions"] if q["item_index"] == 1)
    assert question["quality"]["status"] == "blocker"
    assert question["needs_review"] is True
    assert len([p for p in gateway.retry_payloads if p.questions[0].item_index == 1]) == 2
    other = next(q for q in result["questions"] if q["item_index"] == 2)
    assert other["quality"]["status"] == "pass"
    assert other.get("needs_review") is not True


def test_retry_success_returns_passing_question():
    bad = _question(1, stem="根据课件的内容，关于原子1的问题", options=["甲"], answer="")
    gateway = FakeBatchGateway(scenarios={1: [bad, _question(1)]})
    result = build_generation_graph(gateway).invoke(_state([_slot(1), _slot(2)]))
    question = next(q for q in result["questions"] if q["item_index"] == 1)
    assert question["quality"]["status"] == "pass"
    assert len(gateway.retry_payloads) == 1


def test_forbidden_context_leak_is_caught_and_retried():
    slot1 = _slot(1, forbidden_context={
        "atoms": ["另一个考点兄弟原子的完整文本样例"],
        "answer_cores": ["兄弟答案核心的完整文本"],
    })
    leaking = _question(1, stem="关于另一个考点兄弟原子的完整文本样例，正确的是")
    gateway = FakeBatchGateway(scenarios={1: [leaking, _question(1)]})
    result = build_generation_graph(gateway).invoke(_state([slot1, _slot(2)]))
    question = next(q for q in result["questions"] if q["item_index"] == 1)
    assert question["quality"]["status"] == "pass"
    assert len(gateway.retry_payloads) == 1


def test_answer_must_hit_boundary():
    off_boundary = _question(1, answer="完全无关的答案内容样例文本")
    gateway = FakeBatchGateway(scenarios={1: [off_boundary, _question(1)]})
    result = build_generation_graph(gateway).invoke(
        _state([_slot(1, answer_boundary="正确的选项内容"), _slot(2)]),
    )
    question = next(q for q in result["questions"] if q["item_index"] == 1)
    assert question["quality"]["status"] == "pass"
    assert len(gateway.retry_payloads) == 1


def test_missing_question_in_batch_is_recovered_by_retry():
    # 批调用丢了题2，但单题重试取回合格题 → 恢复，不再标 needs_review
    class DropOnceGateway(FakeBatchGateway):
        def __init__(self):
            super().__init__()
            self.dropped = False

        def generate_batch(self, payload):
            questions = super().generate_batch(payload)
            if not self.dropped and len(payload.questions) > 1:
                self.dropped = True
                return [q for q in questions if q.get("item_index") != 2]
            return questions

    gateway = DropOnceGateway()
    result = build_generation_graph(gateway).invoke(_state([_slot(1), _slot(2)]))
    recovered = next(q for q in result["questions"] if q["item_index"] == 2)
    assert recovered["quality"]["status"] == "pass"
    assert recovered.get("needs_review") is not True
    assert len(gateway.retry_payloads) == 1


def test_missing_question_survives_only_as_review_after_all_defenses():
    # 批丢题且每次重试也丢 → 三道防线失守，保留槽位标 needs_review
    class AlwaysDropTwoGateway(FakeBatchGateway):
        def generate_batch(self, payload):
            questions = super().generate_batch(payload)
            return [q for q in questions if q.get("item_index") != 2]

    gateway = AlwaysDropTwoGateway()
    result = build_generation_graph(gateway).invoke(_state([_slot(1), _slot(2)]))
    missing = next(q for q in result["questions"] if q["item_index"] == 2)
    assert missing["needs_review"] is True
    assert missing["quality"]["status"] == "blocker"


def test_final_check_reports_all_codes_and_passes():
    slots = [_slot(i, "EP1") for i in (1, 2, 3)]
    gateway = FakeBatchGateway()
    result = build_generation_graph(gateway).invoke(_state(slots))
    report = result["final_check"]
    assert report["passed"] is True
    codes = {c["code"] for c in report["checks"]}
    assert {"quota_match", "atom_uniqueness", "answer_mutex", "traceability", "needs_review"} <= codes


def test_duplicate_contract_atom_fails_final_check():
    slots = [_slot(1), _slot(2)]
    slots[1]["coverage_atom"] = slots[0]["coverage_atom"]
    slots[1]["answer_boundary"] = "不同边界内容"
    gateway = FakeBatchGateway()
    result = build_generation_graph(gateway).invoke(_state(slots))
    assert result["final_check"]["passed"] is False


def test_gateway_exception_falls_back_to_missing_review():
    class ExplodingGateway:
        def generate_batch(self, payload):
            raise RuntimeError("模型服务不可用")
    result = build_generation_graph(ExplodingGateway()).invoke(_state([_slot(1), _slot(2)]))
    for q in result["questions"]:
        assert q["needs_review"] is True
    assert result["final_check"]["passed"] is False


def test_true_false_boolean_answer_passes_boundary_check():
    # 判断题答案是对原子的真伪判断，不以答案域文本承载：
    # 布尔答案跳过边界命中检查，布尔校验与防泄漏检查仍生效
    slot = _slot(1, question_type="true_false", answer_boundary="某中文答案边界文本")
    true_false = _question(
        1, question_type="true_false",
        stem="判断：下列关于原子1的陈述成立",
        options=[], answer=True,
    )
    gateway = FakeBatchGateway(scenarios={1: [true_false]})
    result = build_generation_graph(gateway).invoke(_state([slot, _slot(2)]))
    question = next(q for q in result["questions"] if q["item_index"] == 1)
    assert question["quality"]["status"] == "pass"
    assert question["needs_review"] is not True
    assert gateway.retry_payloads == []


def test_exhausted_retries_swap_in_replacement_atom():
    """原子重试耗尽后从同考点未用原子换原子重出，成功则采用替换合同。"""
    bad = _question(1, stem="根据课件第3页的内容，关于原子1的问题", options=["甲"], answer="")
    cards = {
        "C1": {
            "assessable_content": ["原子1", "替换原子文本样例"],
            "answer_boundary": "正确的选项内容",
        },
    }
    units = [{"exam_point_id": "EP1", "unit_id": "U-EP1", "card_ids": ["C1"]}]
    gateway = FakeBatchGateway(scenarios={1: [bad, bad, bad]})
    result = build_generation_graph(gateway).invoke({
        "contract": [_slot(1), _slot(2)],
        "knowledge_cards": cards,
        "units": units,
    })
    question = next(q for q in result["questions"] if q["item_index"] == 1)
    assert question["quality"]["status"] == "pass"
    assert question["needs_review"] is not True
    # 合同溯源已更新为替换原子；原原子不再占用
    assert question["coverage_atom"] == "替换原子文本样例"
    assert question["answer_boundary"] == "正确的选项内容"
    # 换原子调用 1 次（批 1 + 重试 2 + 换原子 1 = 4 次单题/批调用）
    assert len(gateway.retry_payloads) == 3


def test_no_replacement_atom_keeps_needs_review():
    """无可用替换原子（卡片耗尽）时维持 needs_review，不静默成功。"""
    bad = _question(1, stem="根据课件第3页的内容，关于原子1的问题", options=["甲"], answer="")
    cards = {"C1": {"assessable_content": ["原子1"], "answer_boundary": "边界1"}}
    units = [{"exam_point_id": "EP1", "unit_id": "U-EP1", "card_ids": ["C1"]}]
    gateway = FakeBatchGateway(scenarios={1: [bad, bad, bad]})
    result = build_generation_graph(gateway).invoke({
        "contract": [_slot(1), _slot(2)],
        "knowledge_cards": cards,
        "units": units,
    })
    question = next(q for q in result["questions"] if q["item_index"] == 1)
    assert question["quality"]["status"] == "blocker"
    assert question["needs_review"] is True
