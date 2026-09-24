"""合同驱动生成图测试：分批并行、批内互见、单题重试、合并终检。"""
import logging

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


# ---------------------------------------------------------------------------
# 异常可观测性：三处 except Exception 不得静默吞掉
# ---------------------------------------------------------------------------

def test_batch_call_exception_is_logged_not_swallowed(caplog):
    """批调用异常必须落 WARNING（含 batch 与错误摘要）——
    此前静默吞掉，线上只有 needs_review、无从判断模型是否被调用。"""
    class ExplodingGateway:
        def generate_batch(self, payload):
            raise RuntimeError("模型服务不可用")

    with caplog.at_level(logging.WARNING, logger="generation.graph"):
        result = build_generation_graph(ExplodingGateway()).invoke(
            _state([_slot(1), _slot(2)]),
        )
    warnings = [
        r for r in caplog.records
        if r.name == "generation.graph" and r.levelno >= logging.WARNING
    ]
    assert warnings, "批调用失败必须产生 WARNING 日志"
    joined = "\n".join(r.getMessage() for r in warnings)
    assert "RuntimeError" in joined
    assert "模型服务不可用" in joined
    assert "batch" in joined
    # 行为不回退：仍然全员 needs_review
    for q in result["questions"]:
        assert q["needs_review"] is True


def test_retry_call_exception_is_logged(caplog):
    """单题重试调用异常必须落 WARNING 且带题位与尝试次数。"""
    bad = _question(1, stem="根据课件第3页的内容，关于原子1的问题", options=["甲"], answer="")

    class ExplodeOnRetryGateway(FakeBatchGateway):
        def generate_batch(self, payload):
            if len(payload.questions) == 1:
                raise RuntimeError("重试时模型服务不可用")
            return super().generate_batch(payload)

    gateway = ExplodeOnRetryGateway(scenarios={1: [bad, bad, bad]})
    with caplog.at_level(logging.WARNING, logger="generation.graph"):
        build_generation_graph(gateway).invoke(
            _state([_slot(1), _slot(2)]),
        )
    warnings = [
        r for r in caplog.records
        if r.name == "generation.graph" and r.levelno >= logging.WARNING
    ]
    joined = "\n".join(r.getMessage() for r in warnings)
    assert "重试" in joined
    assert "item_index=1" in joined
    assert "RuntimeError" in joined


def test_swap_call_exception_is_logged(caplog):
    """换原子兜底调用异常必须落 WARNING，与重试失败可区分。"""
    bad = _question(1, stem="根据课件第3页的内容，关于原子1的问题", options=["甲"], answer="")
    cards = {
        "C1": {
            "assessable_content": ["原子1", "替换原子文本样例"],
            "answer_boundary": "正确的选项内容",
        },
    }
    units = [{"exam_point_id": "EP1", "unit_id": "U-EP1", "card_ids": ["C1"]}]

    class ExplodeOnSwapGateway(FakeBatchGateway):
        def generate_batch(self, payload):
            spec = payload.questions[0]
            if spec.coverage_atom == "替换原子文本样例":
                raise RuntimeError("换原子时模型服务不可用")
            return super().generate_batch(payload)

    gateway = ExplodeOnSwapGateway()
    gateway.scenarios = {1: [bad, bad, bad]}
    with caplog.at_level(logging.WARNING, logger="generation.graph"):
        result = build_generation_graph(gateway).invoke({
            "contract": [_slot(1), _slot(2)],
            "knowledge_cards": cards,
            "units": units,
        })
    warnings = [
        r for r in caplog.records
        if r.name == "generation.graph" and r.levelno >= logging.WARNING
    ]
    joined = "\n".join(r.getMessage() for r in warnings)
    assert "换原子" in joined
    assert "RuntimeError" in joined
    question = next(q for q in result["questions"] if q["item_index"] == 1)
    assert question["needs_review"] is True


def test_batch_completion_logs_summary(caplog):
    """批次完成输出 INFO 汇总（题数/模型调用数），便于核对批粒度消耗。"""
    gateway = FakeBatchGateway()
    with caplog.at_level(logging.INFO, logger="generation.graph"):
        build_generation_graph(gateway).invoke(_state([_slot(1), _slot(2)]))
    infos = [
        r for r in caplog.records
        if r.name == "generation.graph" and r.levelno == logging.INFO
    ]
    assert any("批次完成" in r.getMessage() for r in infos), [
        r.getMessage() for r in caplog.records
    ]
    summary = next(r.getMessage() for r in infos if "批次完成" in r.getMessage())
    assert "calls=" in summary


# ---------------------------------------------------------------------------
# 同章回补防线：anchor 取自合同、占用面取全卷、防线链不再区分"缺失/不合格"
# ---------------------------------------------------------------------------

def _surplus_ep2_slots(ep2_anchor: str) -> list[dict]:
    """EP1 三题独立成批 + EP2 一题（另一并行批次的富余考点）。"""
    return (
        [_slot(i, "EP1", anchor_key="ch1") for i in (1, 2, 3)]
        + [_slot(4, "EP2", anchor_key=ep2_anchor, card_id="C4",
                 coverage_atom="EP2契约原子", answer_boundary="EP2契约边界")]
    )


def _surplus_cards() -> dict:
    # C1~C3 的原子全部被 EP1 自己的合同占用（同考点无富余原子，换原子必失败）；
    # C4 含一个被 EP2 合同签约的原子 + 一个真正富余的原子
    return {
        "C1": {"assessable_content": ["原子1"], "answer_boundary": "边界1"},
        "C2": {"assessable_content": ["原子2"], "answer_boundary": "边界2"},
        "C3": {"assessable_content": ["原子3"], "answer_boundary": "边界3"},
        "C4": {"assessable_content": ["EP2契约原子", "EP2富余原子"],
               "answer_boundary": "EP2富余边界"},
    }


def _surplus_units() -> list[dict]:
    # 生产链路（generation_runner_service._load_generation_context）的 units 形状：
    # 只有 unit_id / exam_point_id / card_ids，**不携带 anchor_key**
    return [
        {"unit_id": "U-EP1", "exam_point_id": "EP1", "card_ids": ["C1", "C2", "C3"]},
        {"unit_id": "U-EP2", "exam_point_id": "EP2", "card_ids": ["C4"]},
    ]


def test_backfill_rescues_present_but_failing_question():
    """第三道防线此前只在"题目缺失"时生效：题目存在但重试与换原子全部失守
    就直接标 needs_review，与"单题重试 → 换原子 → 同章回补"的防线链注释矛盾。
    同时锁定 anchor 来源：生产 units 不带 anchor_key，同章信息必须取自合同。"""
    bad = _question(1, stem="根据课件第3页的内容，关于原子1的问题", options=["甲"], answer="")
    gateway = FakeBatchGateway(scenarios={1: [bad, bad, bad]})
    result = build_generation_graph(gateway).invoke({
        "contract": _surplus_ep2_slots(ep2_anchor="ch1"),
        "knowledge_cards": _surplus_cards(),
        "units": _surplus_units(),
    })
    question = next(q for q in result["questions"] if q["item_index"] == 1)
    assert question["quality"]["status"] == "pass"
    assert question.get("needs_review") is not True
    # 同考点三个原子全被占用 → 换原子失败 → 回补到同章 EP2 的富余原子
    assert question["coverage_atom"] == "EP2富余原子"
    assert question["backfilled_from"]["from_exam_point_id"] == "EP1"
    assert question["backfilled_from"]["to_exam_point_id"] == "EP2"
    assert result["final_check"]["passed"] is True, result["final_check"]


def test_backfill_never_crosses_chapters_without_contract_anchor():
    """同章红线必须 fail-closed：EP2 属于另一章（ch2）时绝不回补过去。
    此前 anchor 缺失（生产 units 无 anchor_key）被当作"不限章"放行，
    同章约束在真实运行中静默失效、跨章挤占考纲章节权重。"""
    bad = _question(1, stem="根据课件第3页的内容，关于原子1的问题", options=["甲"], answer="")
    gateway = FakeBatchGateway(scenarios={1: [bad, bad, bad]})
    result = build_generation_graph(gateway).invoke({
        "contract": _surplus_ep2_slots(ep2_anchor="ch2"),
        "knowledge_cards": _surplus_cards(),
        "units": _surplus_units(),
    })
    question = next(q for q in result["questions"] if q["item_index"] == 1)
    assert question["needs_review"] is True
    assert question["quality"]["status"] == "blocker"
    assert "backfilled_from" not in question


def test_backfill_never_takes_atoms_contracted_by_other_batches():
    """回补目标考点属于并行批次：其已签约原子（EP2契约原子）不得被占用。
    此前占用面只含本批题位，回补会偷走并行批次的合同原子 → 全卷重复原子、
    终检 atom_uniqueness 挂红（且生产链路 runner 不消费 final_check，静默入库）。"""
    class DropOriginalAtomGateway(FakeBatchGateway):
        """凡以原原子"原子1"出的题（批调用/单题重试）一律丢弃，
        迫使防线走到同章回补；回补载荷带新原子，正常返回。"""

        def generate_batch(self, payload):
            questions = super().generate_batch(payload)
            if any(spec.coverage_atom == "原子1" for spec in payload.questions):
                return [q for q in questions if q.get("item_index") != 1]
            return questions

    gateway = DropOriginalAtomGateway()
    result = build_generation_graph(gateway).invoke({
        "contract": _surplus_ep2_slots(ep2_anchor="ch1"),
        "knowledge_cards": _surplus_cards(),
        "units": _surplus_units(),
    })
    question = next(q for q in result["questions"] if q["item_index"] == 1)
    assert question["coverage_atom"] == "EP2富余原子"
    assert question.get("needs_review") is not True
    atoms = [q["coverage_atom"] for q in result["questions"]]
    assert len(atoms) == len(set(atoms))
    assert result["final_check"]["passed"] is True, result["final_check"]
