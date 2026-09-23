import pytest

from app.adapters.model.llm_gateway import LLMGateway, LLMJsonClient, LLMModelError
from app.schemas.generation import BatchGenerationPayload, BatchQuestionSpec


class FakeJsonClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request_json(self, *, system_prompt, payload, temperature,
                     call_context=None, response_validator=None):
        self.calls.append({"system_prompt": system_prompt, "payload": payload, "temperature": temperature})
        if response_validator:
            response_validator(self.response)
        return self.response


def _payload(question_count=2):
    specs = [
        BatchQuestionSpec(
            item_index=i, question_type="single_choice", score=2, difficulty="medium",
            cognitive_level="understand", coverage_atom=f"原子{i}", answer_boundary=f"边界{i}",
            question_template="单选模板", output_schema={},
        )
        for i in range(1, question_count + 1)
    ]
    return BatchGenerationPayload(
        batch_id="B01", exam_point_ids=["EP1"], questions=specs,
        batch_instruction="指令", output_schema={"type": "array"},
    )


def test_generate_batch_returns_validated_list():
    client = FakeJsonClient({
        "questions": [
            {"item_index": 1, "stem": "题一", "options": ["A", "B", "C", "D"], "answer": "A"},
            {"item_index": 2, "stem": "题二", "options": ["A", "B", "C", "D"], "answer": "B"},
        ]
    })
    gateway = LLMGateway(api_key="k", json_client=client)
    questions = gateway.generate_batch(_payload())
    assert [q["item_index"] for q in questions] == [1, 2]
    assert len(client.calls) == 1
    assert "questions" in client.calls[0]["system_prompt"]


def test_generate_batch_rejects_non_list_response():
    client = FakeJsonClient({"item_index": 1})
    gateway = LLMGateway(api_key="k", json_client=client)
    with pytest.raises(LLMModelError):
        gateway.generate_batch(_payload(question_count=1))


def test_generate_batch_rejects_questions_not_list():
    client = FakeJsonClient({"questions": "不是数组"})
    gateway = LLMGateway(api_key="k", json_client=client)
    with pytest.raises(LLMModelError):
        gateway.generate_batch(_payload(question_count=1))


def test_generate_batch_rejects_missing_item_index():
    client = FakeJsonClient({"questions": [{"stem": "缺编号", "answer": "A"}]})
    gateway = LLMGateway(api_key="k", json_client=client)
    with pytest.raises(LLMModelError):
        gateway.generate_batch(_payload(question_count=1))


def test_generate_batch_rejects_index_set_mismatch():
    client = FakeJsonClient({
        "questions": [
            {"item_index": 1, "stem": "题一", "answer": "A"},
            {"item_index": 99, "stem": "多余题", "answer": "B"},
        ]
    })
    gateway = LLMGateway(api_key="k", json_client=client)
    with pytest.raises(LLMModelError):
        gateway.generate_batch(_payload(question_count=1))


def test_generate_batch_rejects_duplicate_indexes():
    client = FakeJsonClient({
        "questions": [
            {"item_index": 1, "stem": "题一", "answer": "A"},
            {"item_index": 1, "stem": "重复", "answer": "B"},
        ]
    })
    gateway = LLMGateway(api_key="k", json_client=client)
    with pytest.raises(LLMModelError):
        gateway.generate_batch(_payload(question_count=2))


def test_default_json_client_still_constructible():
    # 无硬编码默认端点/模型：必须显式给 base_url/model，否则构造期即失败。
    gateway = LLMGateway(
        api_key="k",
        base_url="https://llm.invalid/v1",
        model="generic-model",
    )
    assert isinstance(gateway.json_client, LLMJsonClient)
