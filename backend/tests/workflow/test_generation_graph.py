from app.domain.blueprint.models import PlanItem
from app.workflows.generation_graph import build_generation_graph


class FakeGateway:
    def __init__(self):
        self.payloads = []

    def generate(self, payload):
        self.payloads.append(payload)
        return {"question_type": payload.question_type, "stem": "解释RAG流程", "answer": "先检索再生成", "explanation": "解析", "rubric": [{"point": "说明检索", "score": 2}]}


def test_generation_graph_uses_source_free_payload_and_returns_candidate_questions():
    gateway = FakeGateway()
    graph = build_generation_graph(gateway)
    result = graph.invoke({
        "plan_items": [PlanItem(item_index=1, question_type="short_answer", score=6, anchor_key="rag", unit_id="u1", card_id="c1").model_dump()],
        "knowledge_cards": {"c1": {"name": "RAG", "performance_statement": "能够解释流程", "assessable_content": ["检索和生成"], "scope_boundary": {}, "cognitive_targets": ["apply"], "allowed_question_types": ["short_answer"]}},
    })
    assert len(result["questions"]) == 1
    assert "evidence" not in gateway.payloads[0].model_dump()
    assert result["questions"][0]["quality"]["status"] == "pass"
