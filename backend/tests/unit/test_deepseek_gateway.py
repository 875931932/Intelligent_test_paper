import json

from app.adapters.model.deepseek_gateway import DeepSeekGateway
from app.domain.blueprint.models import PlanItem
from app.domain.generation.coverage import compile_coverage_planning_payload
from app.schemas.generation import compile_question_generation_payload


class FakeResponse:
    def __init__(self, content: dict):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": json.dumps(self._content, ensure_ascii=False)}}]}


def test_gateway_has_separate_whole_paper_planning_prompt(monkeypatch):
    requests = []

    def fake_post(*args, **kwargs):
        requests.append(kwargs["json"])
        return FakeResponse({"directives": []})

    monkeypatch.setattr("app.adapters.model.deepseek_gateway.httpx.post", fake_post)
    gateway = DeepSeekGateway(api_key="test", max_attempts=1)
    item = PlanItem(item_index=1, question_type="fill_blank", score=2, anchor_key="a", unit_id="u", card_id="c")
    payload = compile_coverage_planning_payload([item], {"c": {"performance_statement": "说明概念", "assessable_content": ["基本定义"], "scope_boundary": {}}})

    result = gateway.plan_coverage(payload)

    assert result == {"directives": []}
    assert "全卷" in requests[0]["messages"][0]["content"]
    assert "考查原子" in requests[0]["messages"][0]["content"]


def test_generation_prompt_prefers_common_terms_and_direct_expression(monkeypatch):
    requests = []

    def fake_post(*args, **kwargs):
        requests.append(kwargs["json"])
        return FakeResponse({"stem": "题干", "answer": "答案"})

    monkeypatch.setattr("app.adapters.model.deepseek_gateway.httpx.post", fake_post)
    gateway = DeepSeekGateway(api_key="test", max_attempts=1)
    item = PlanItem(item_index=1, question_type="fill_blank", score=2, anchor_key="a", unit_id="u", card_id="c")
    payload = compile_question_generation_payload(item, {"performance_statement": "说明训练数据作用", "assessable_content": ["训练数据"], "preferred_terms": ["训练数据"], "scope_boundary": {}})

    gateway.generate(payload)

    system_prompt = requests[0]["messages"][0]["content"]
    assert "常用术语" in system_prompt
    assert "括号" in system_prompt
