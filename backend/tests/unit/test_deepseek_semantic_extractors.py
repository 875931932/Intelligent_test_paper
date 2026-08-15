from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.model.deepseek_semantic_extractors import (
    DeepSeekExamPointEvidenceClassifier,
    DeepSeekExamPointKnowledgeConsolidator,
    DeepSeekJsonClient,
    DeepSeekModelError,
    DeepSeekSyllabusExtractor,
)
from app.domain.framework.exam_points import ExamPoint, OperationalDetailPolicy, WeightSource
from app.domain.knowledge.relevance import EvidenceDecision, StagingChunk
from app.domain.model_calls import ModelCallContext
from app.db.schema import Base, Course, User, framework_build_runs, model_calls
from app.services.model_call_service import DatabaseModelCallRecorder


class RecordingJsonClient:
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.recorded_payloads: list[dict] = []

    def request_json(
        self,
        *,
        system_prompt,
        payload,
        temperature,
        call_context=None,
        response_validator=None,
    ):
        self.recorded_payloads.append(
            {
                "system": system_prompt,
                "user": payload,
                "temperature": temperature,
                "call_context": call_context,
            }
        )
        response = self.responses.pop(0)
        if response_validator is not None:
            response_validator(response)
        return response


class RecordingModelCalls:
    def __init__(self):
        self.calls: list[dict] = []

    def record(self, **values):
        self.calls.append(values)


class FailingModelCallRecorder:
    def record(self, **values):
        raise ValueError("observability storage unavailable")


def _point() -> ExamPoint:
    return ExamPoint(
        code="rag-diagnosis",
        anchor_key="rag",
        title="检索效果诊断",
        assessment_requirement="能够诊断召回偏差",
        weight_value=100,
        weight_source=WeightSource.ASSESSMENT_SYLLABUS,
        weight_group_id="rag",
        cognitive_targets=["analyze"],
        assessment_orientations=["diagnostic"],
        operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
        retrieval_intent="检索偏差及诊断依据",
        assessment_anchor_keys=["rag"],
        teaching_anchor_keys=["rag-teaching"],
    )


def _decision(*, relevance_class: str = "direct") -> dict:
    return {
        "exam_point_code": "rag-diagnosis",
        "evidence_chunk_id": "e1",
        "relevance_class": relevance_class,
        "support_claim": "切分粒度会影响关键内容召回",
        "evidence_role": "answer_or_rubric_basis",
        "content_kind": "principle",
        "candidate_assessment_unit": {
            "code": "diagnose-retrieval",
            "title": "诊断检索偏差",
            "performance_statement": "分析召回偏差成因",
        },
        "candidate_card_content": {
            "name": "切分粒度影响",
            "performance_statement": "说明切分粒度如何影响召回",
            "assessable_content": ["切分粒度会影响召回"],
        },
        "confidence": 95,
        "source_locator": {"page": 3, "heading_path": ["检索"]},
    }


def test_json_client_uses_injected_http_client_and_records_one_success():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"x-request-id": "req-1"},
            json={
                "choices": [{"message": {"content": json.dumps({"ok": True})}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            },
        )

    recorder = RecordingModelCalls()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = DeepSeekJsonClient(
        api_key="test-key",
        base_url="https://deepseek.invalid/v1",
        model="deepseek-v4-flash",
        max_attempts=1,
        client=http_client,
        recorder=recorder,
    )
    context = ModelCallContext(course_id="course", framework_build_run_id="run", stage="assessment")

    assert client.request_json(
        system_prompt="system",
        payload={"blocks": ["期末考试"]},
        temperature=0.1,
        call_context=context,
    ) == {"ok": True}

    body = json.loads(requests[0].content)
    assert body["response_format"] == {"type": "json_object"}
    assert json.loads(body["messages"][1]["content"]) == {"blocks": ["期末考试"]}
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["status"] == "succeeded"
    assert recorder.calls[0]["request_id"] == "req-1"
    assert recorder.calls[0]["input_tokens"] == 12
    assert recorder.calls[0]["output_tokens"] == 3
    assert "期末考试" not in repr(recorder.calls[0])


def test_json_client_does_not_retry_success_when_recorder_fails(monkeypatch):
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
        )

    monkeypatch.setattr("app.adapters.model.deepseek_gateway.time.sleep", lambda _: None)
    client = DeepSeekJsonClient(
        api_key="test-key",
        max_attempts=2,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        recorder=FailingModelCallRecorder(),
    )

    result = client.request_json(
        system_prompt="system",
        payload={"blocks": ["material"]},
        temperature=0,
        call_context=ModelCallContext(
            course_id="course",
            organization_run_id="run",
            stage="classification",
        ),
    )

    assert result == {"ok": True}
    assert request_count == 1


def test_json_client_preserves_model_failure_when_recorder_fails():
    client = DeepSeekJsonClient(
        api_key="test-key",
        max_attempts=1,
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(401))
        ),
        recorder=FailingModelCallRecorder(),
    )

    with pytest.raises(DeepSeekModelError) as caught:
        client.request_json(
            system_prompt="system",
            payload={"blocks": ["material"]},
            temperature=0,
            call_context=ModelCallContext(
                course_id="course",
                organization_run_id="run",
                stage="classification",
            ),
        )

    assert caught.value.error_code == "deepseek_http_error"


@pytest.mark.parametrize("status_code", [400, 401, 403])
def test_json_client_does_not_retry_permanent_http_errors(status_code, monkeypatch):
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(status_code)

    monkeypatch.setattr("app.adapters.model.deepseek_gateway.time.sleep", lambda _: None)
    client = DeepSeekJsonClient(
        api_key="test-key",
        max_attempts=4,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(DeepSeekModelError):
        client.request_json(
            system_prompt="system",
            payload={"blocks": ["material"]},
            temperature=0,
        )

    assert request_count == 1


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        ({"choices": [{"message": {"content": ""}}]}, "model_empty_response"),
        ({"choices": [{"message": {"content": "not-json"}}]}, "model_non_json_response"),
        ({"choices": [{"message": {"content": "[]"}}]}, "model_non_object_response"),
    ],
)
def test_json_client_preserves_safe_failure_code(payload, error_code):
    recorder = RecordingModelCalls()
    client = DeepSeekJsonClient(
        api_key="test-key",
        base_url="https://deepseek.invalid/v1",
        model="deepseek-v4-flash",
        max_attempts=1,
        client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))),
        recorder=recorder,
    )

    with pytest.raises(DeepSeekModelError) as caught:
        client.request_json(
            system_prompt="system",
            payload={"secret": "material body must not be logged"},
            temperature=0,
            call_context=ModelCallContext(course_id="course", organization_run_id="run", stage="classification"),
        )

    assert caught.value.error_code == error_code
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["status"] == "failed"
    assert recorder.calls[0]["error_code"] == error_code
    assert "material body" not in repr(recorder.calls[0])


def test_json_client_records_terminal_http_status_in_safe_details():
    recorder = RecordingModelCalls()
    client = DeepSeekJsonClient(
        api_key="test-key",
        base_url="https://deepseek.invalid/v1",
        model="deepseek-v4-flash",
        max_attempts=1,
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(503, text="secret upstream body"))
        ),
        recorder=recorder,
    )

    with pytest.raises(DeepSeekModelError):
        client.request_json(
            system_prompt="system",
            payload={"blocks": ["material"]},
            temperature=0,
            call_context=ModelCallContext(course_id="course", organization_run_id="run", stage="classification"),
        )

    details = recorder.calls[0]["details"]
    assert details["final_http_status"] == 503
    assert details["last_error_code"] == "deepseek_http_error"
    assert details["attempts"] == [
        {"attempt": 1, "http_status": 503, "error_code": "deepseek_http_error"}
    ]
    assert "secret upstream body" not in repr(details)


def test_json_client_success_retains_last_retry_error_code(monkeypatch):
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"ok":true}'}}]},
            ),
        ]
    )
    recorder = RecordingModelCalls()
    monkeypatch.setattr("app.adapters.model.deepseek_gateway.time.sleep", lambda _: None)
    client = DeepSeekJsonClient(
        api_key="test-key",
        base_url="https://deepseek.invalid/v1",
        model="deepseek-v4-flash",
        max_attempts=2,
        client=httpx.Client(transport=httpx.MockTransport(lambda _: next(responses))),
        recorder=recorder,
    )

    assert client.request_json(
        system_prompt="system",
        payload={"blocks": ["material"]},
        temperature=0,
        call_context=ModelCallContext(course_id="course", organization_run_id="run", stage="classification"),
    ) == {"ok": True}

    details = recorder.calls[0]["details"]
    assert details["retry_count"] == 1
    assert details["final_http_status"] == 200
    assert details["last_error_code"] == "deepseek_http_error"
    assert details["attempts"] == [
        {"attempt": 1, "http_status": 503, "error_code": "deepseek_http_error"}
    ]


def test_json_client_terminal_transport_error_does_not_retain_prior_response_metadata(monkeypatch):
    responses = iter(
        [
            httpx.Response(
                503,
                headers={"x-request-id": "req-stale"},
                json={"usage": {"prompt_tokens": 91, "completion_tokens": 17}},
            ),
            httpx.ConnectError("connection failed"),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        outcome = next(responses)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    recorder = RecordingModelCalls()
    monkeypatch.setattr("app.adapters.model.deepseek_gateway.time.sleep", lambda _: None)
    client = DeepSeekJsonClient(
        api_key="test-key",
        max_attempts=2,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        recorder=recorder,
    )

    with pytest.raises(DeepSeekModelError):
        client.request_json(
            system_prompt="system",
            payload={"blocks": ["material"]},
            temperature=0,
            call_context=ModelCallContext(
                course_id="course",
                organization_run_id="run",
                stage="classification",
            ),
        )

    recorded = recorder.calls[0]
    assert recorded["request_id"] is None
    assert recorded["input_tokens"] is None
    assert recorded["output_tokens"] is None
    assert recorded["details"]["final_http_status"] is None
    assert recorded["details"]["attempts"][0]["http_status"] == 503


def test_semantic_schema_failure_records_one_final_failed_model_call():
    recorder = RecordingModelCalls()
    client = DeepSeekJsonClient(
        api_key="test-key",
        base_url="https://deepseek.invalid/v1",
        model="deepseek-v4-flash",
        max_attempts=1,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"x-request-id": "req-bad"},
                    json={"choices": [{"message": {"content": "{}"}}]},
                )
            )
        ),
        recorder=recorder,
    )

    with pytest.raises(DeepSeekModelError) as caught:
        DeepSeekSyllabusExtractor(client).extract_assessment(
            ["期末考试"],
            call_context=ModelCallContext(
                course_id="course",
                framework_build_run_id="run",
                stage="assessment_syllabus_extraction",
            ),
        )

    assert caught.value.error_code == "model_schema_validation_failed"
    assert [(item["status"], item["error_code"]) for item in recorder.calls] == [
        ("failed", "model_schema_validation_failed")
    ]
    assert recorder.calls[0]["request_id"] == "req-bad"
    assert recorder.calls[0]["details"]["validation"]["invalid_fields"]


def test_json_client_redacts_arbitrary_validator_failure_from_persisted_metadata():
    recorder = RecordingModelCalls()
    client = DeepSeekJsonClient(
        api_key="test-key",
        max_attempts=1,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": '{"ok": true}'}}]},
                )
            )
        ),
        recorder=recorder,
    )

    def reject_response(_: dict) -> None:
        raise DeepSeekModelError(
            "validator_custom_failure",
            "SECRET validator message",
            details={
                "attempt_count": 999,
                "secret": "SECRET detail",
                "invalid_fields": ["SECRET field"],
            },
        )

    with pytest.raises(DeepSeekModelError):
        client.request_json(
            system_prompt="system",
            payload={"blocks": ["material"]},
            temperature=0,
            call_context=ModelCallContext(
                course_id="course",
                organization_run_id="run",
                stage="classification",
            ),
            response_validator=reject_response,
        )

    recorded = recorder.calls[0]
    assert recorded["error_code"] == "model_validation_failed"
    assert recorded["error_message"] == "model response validation failed"
    assert recorded["details"]["attempt_count"] == 1
    assert "validation" not in recorded["details"]
    assert "SECRET" not in repr(recorded)


def test_syllabus_extractor_requires_final_exam_points_and_sends_only_supplied_outline():
    client = RecordingJsonClient(
        [
            {
                "anchors": [{"key": "rag", "title": "RAG", "exam_weight": 100, "alignment_keys": ["rag-teaching"]}],
                "exam_points": [
                    {
                        "code": "rag-diagnosis",
                        "anchor_key": "rag",
                        "title": "检索效果诊断",
                        "assessment_requirement": "能够诊断召回偏差",
                        "weight_value": 100,
                        "weight_source": "assessment_syllabus",
                        "weight_group_id": "rag",
                        "cognitive_targets": ["analyze"],
                        "assessment_orientations": ["diagnostic"],
                        "operational_detail_policy": "supporting_only",
                        "retrieval_intent": "检索偏差及诊断依据",
                        "teaching_anchor_keys": ["rag-teaching"],
                    }
                ],
                "final_exam_rules": {"scope": "final_exam_only"},
            }
        ]
    )
    extractor = DeepSeekSyllabusExtractor(client)

    result = extractor.extract_assessment(
        ["课程封面", "平时成绩30%", "期末考试：RAG占100%"],
        call_context=ModelCallContext(course_id="course", framework_build_run_id="run", stage="assessment"),
    )

    assert result.exam_points == [_point()]
    request = client.recorded_payloads[0]
    assert request["user"] == {"blocks": ["课程封面", "平时成绩30%", "期末考试：RAG占100%"]}
    assert "期末考试" in request["system"]
    assert "平时" in request["system"]
    assert "supporting_only" in request["system"]


def test_syllabus_extractor_validates_teaching_topic_schema():
    client = RecordingJsonClient(
        [
            {
                "teaching_topics": [
                    {
                        "key": "rag-teaching",
                        "title": "检索增强生成",
                        "depth": "analyze",
                        "requirements": ["分析检索偏差"],
                    }
                ]
            }
        ]
    )

    topics = DeepSeekSyllabusExtractor(client).extract_teaching(["教学内容与要求"])

    assert topics[0].key == "rag-teaching"
    assert "行政内容" in client.recorded_payloads[0]["system"]


def test_syllabus_extractor_rejects_unknown_teaching_topic_fields():
    client = RecordingJsonClient(
        [
            {
                "teaching_topics": [
                    {
                        "key": "rag-teaching",
                        "title": "检索增强生成",
                        "depth": "analyze",
                        "requirements": ["分析检索偏差"],
                        "requirement": "must not be ignored",
                    }
                ]
            }
        ]
    )

    with pytest.raises(DeepSeekModelError) as caught:
        DeepSeekSyllabusExtractor(client).extract_teaching(["教学内容与要求"])

    assert caught.value.error_code == "model_schema_validation_failed"


@pytest.mark.parametrize(
    "point_override",
    [
        {"weight_source": "teacher_confirmed"},
        {"cognitive_targets": None},
        {"assessment_orientations": None},
        {"operational_detail_policy": None},
        {"teaching_anchor_keys": None},
    ],
)
def test_assessment_response_requires_strict_exam_point_fields(point_override):
    point = {
        "code": "rag-diagnosis",
        "anchor_key": "rag",
        "title": "检索效果诊断",
        "assessment_requirement": "能够诊断召回偏差",
        "weight_value": 100,
        "weight_source": "assessment_syllabus",
        "weight_group_id": "rag",
        "cognitive_targets": ["analyze"],
        "assessment_orientations": ["diagnostic"],
        "operational_detail_policy": "supporting_only",
        "retrieval_intent": "检索偏差及诊断依据",
        "teaching_anchor_keys": ["rag-teaching"],
    }
    key, value = next(iter(point_override.items()))
    if value is None:
        point.pop(key)
    else:
        point[key] = value
    extractor = DeepSeekSyllabusExtractor(
        RecordingJsonClient(
            [
                {
                    "anchors": [
                        {
                            "key": "rag",
                            "title": "RAG",
                            "exam_weight": 100,
                            "alignment_keys": ["rag-teaching"],
                        }
                    ],
                    "exam_points": [point],
                    "final_exam_rules": {},
                }
            ]
        )
    )

    with pytest.raises(DeepSeekModelError) as caught:
        extractor.extract_assessment(["期末考试"])

    assert caught.value.error_code == "model_schema_validation_failed"


def test_database_model_call_recorder_persists_only_redacted_metadata(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'model-calls.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="owner", display_name="Owner", role="teacher"))
        session.flush()
        session.add(Course(id="course", owner_id="owner", slug="course", name="Course"))
        session.flush()
        session.execute(
            framework_build_runs.insert().values(
                id="run",
                course_id="course",
                status="running",
                input_snapshot={},
            )
        )
        session.commit()

        DatabaseModelCallRecorder(sessionmaker(bind=engine)).record(
            context=ModelCallContext(
                course_id="course",
                framework_build_run_id="run",
                stage="assessment_syllabus_extraction",
            ),
            provider="deepseek",
            model="deepseek-v4-flash",
            status="failed",
            prompt_hash="a" * 64,
            input_tokens=10,
            output_tokens=2,
            duration_ms=30,
            error_code="model_empty_response",
            error_message="model returned empty content",
            details={"attempt_count": 1},
        )

        row = session.execute(select(model_calls)).mappings().one()
        assert row["framework_build_run_id"] == "run"
        assert row["error_code"] == "model_empty_response"
        assert row["details"] == {"attempt_count": 1}
        assert "blocks" not in row
    engine.dispose()


def test_database_model_call_recorder_rejects_borrowed_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'borrowed-session.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        with pytest.raises(TypeError, match="session factory"):
            DatabaseModelCallRecorder(session)
    engine.dispose()


def test_database_model_call_recorder_does_not_commit_callers_pending_transaction(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'transaction-ownership.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as setup:
        setup.add(User(id="owner", display_name="Owner", role="teacher"))
        setup.flush()
        setup.add(Course(id="course", owner_id="owner", slug="course", name="Course"))
        setup.flush()
        setup.execute(
            framework_build_runs.insert().values(
                id="run",
                course_id="course",
                status="running",
                input_snapshot={},
            )
        )
        setup.commit()

    factory = sessionmaker(bind=engine)
    with Session(engine) as caller:
        owner = caller.get(User, "owner")
        owner.display_name = "Pending change"
        DatabaseModelCallRecorder(factory).record(
            context=ModelCallContext(course_id="course", framework_build_run_id="run", stage="assessment"),
            provider="deepseek",
            model="deepseek-v4-flash",
            status="succeeded",
            prompt_hash="a" * 64,
            input_tokens=1,
            output_tokens=1,
            duration_ms=1,
            error_code=None,
            error_message=None,
        )
        caller.rollback()

    with Session(engine) as check:
        assert check.get(User, "owner").display_name == "Owner"
        assert check.scalar(select(model_calls.c.status)) == "succeeded"
    engine.dispose()


def test_material_classifier_sends_one_exam_point_and_one_file_with_locators():
    client = RecordingJsonClient(
        [{"exam_point_code": "rag-diagnosis", "material_version_id": "material-v1", "decisions": [_decision()]}]
    )
    classifier = DeepSeekExamPointEvidenceClassifier(client)
    chunk = StagingChunk(
        id="e1",
        material_version_id="material-v1",
        content="检索失败可能由切分粒度不当造成",
        locator={"page": 3, "heading_path": ["检索"]},
    )

    result = classifier.classify(
        exam_point=_point(),
        material_version_id="material-v1",
        chunks=[chunk],
        call_context=ModelCallContext(course_id="course", organization_run_id="run", stage="classification"),
    )

    request = client.recorded_payloads[-1]["user"]
    assert request["exam_point"]["code"] == "rag-diagnosis"
    assert request["material_version_id"] == "material-v1"
    assert {item["material_version_id"] for item in request["chunks"]} == {"material-v1"}
    assert request["chunks"][0]["locator"]["page"] == 3
    assert "all_exam_points" not in request
    assert result.decisions[0].evidence_chunk_id == "e1"
    assert "相关背景" in client.recorded_payloads[-1]["system"]
    assert "考点边界之外" in client.recorded_payloads[-1]["system"]


def test_classifier_rejects_evidence_from_another_pair():
    bad = _decision()
    bad["evidence_chunk_id"] = "outside"
    classifier = DeepSeekExamPointEvidenceClassifier(
        RecordingJsonClient(
            [{"exam_point_code": "rag-diagnosis", "material_version_id": "material-v1", "decisions": [bad]}]
        )
    )

    with pytest.raises(DeepSeekModelError) as caught:
        classifier.classify(
            exam_point=_point(),
            material_version_id="material-v1",
            chunks=[StagingChunk(id="e1", material_version_id="material-v1", content="content")],
        )

    assert caught.value.error_code == "model_output_scope_violation"


def test_consolidator_receives_only_one_point_admitted_decisions_and_keeps_sources_out_of_card_body():
    client = RecordingJsonClient(
        [
            {
                "exam_point_code": "rag-diagnosis",
                "assessment_units": [
                    {
                        "code": "diagnose-retrieval",
                        "title": "诊断检索偏差",
                        "performance_statement": "分析召回偏差成因",
                        "exam_point_code": "rag-diagnosis",
                        "cards": [
                            {
                                "name": "切分粒度影响",
                                "performance_statement": "说明切分粒度如何影响召回",
                                "assessable_content": ["切分粒度会影响召回"],
                                "evidence_chunk_ids": ["e1"],
                                "prompt_material": ["可结合检索场景设问"],
                            }
                        ],
                    }
                ],
                "source_locations": [{"evidence_chunk_id": "e1", "page": 3}],
            }
        ]
    )
    consolidator = DeepSeekExamPointKnowledgeConsolidator(client)
    decision = EvidenceDecision.model_validate(_decision())

    units = consolidator.consolidate(exam_point=_point(), admitted_decisions=[decision])

    request = client.recorded_payloads[-1]["user"]
    assert request["exam_point"]["code"] == "rag-diagnosis"
    assert {item["exam_point_code"] for item in request["admitted_decisions"]} == {"rag-diagnosis"}
    assert request["admitted_decisions"][0]["evidence_chunk_id"] == "e1"
    card = units[0].cards[0]
    assert card.evidence_chunk_ids == ["e1"]
    assert "source_locator" not in card.model_dump(mode="json")


def test_consolidator_rejects_fact_without_direct_evidence_coverage():
    client = RecordingJsonClient(
        [
            {
                "exam_point_code": "rag-diagnosis",
                "assessment_units": [
                    {
                        "code": "diagnose-retrieval",
                        "title": "诊断检索偏差",
                        "performance_statement": "分析召回偏差成因",
                        "exam_point_code": "rag-diagnosis",
                        "cards": [
                            {
                                "name": "额外事实",
                                "performance_statement": "说明额外事实",
                                "assessable_content": ["模型没有依据的新事实"],
                                "evidence_chunk_ids": ["e1"],
                            }
                        ],
                    }
                ],
            }
        ]
    )
    consolidator = DeepSeekExamPointKnowledgeConsolidator(client)

    with pytest.raises(DeepSeekModelError) as caught:
        consolidator.consolidate(
            exam_point=_point(),
            admitted_decisions=[EvidenceDecision.model_validate(_decision())],
        )

    assert caught.value.error_code == "model_output_evidence_gap"


def test_consolidator_rejects_active_unit_without_knowledge_cards():
    client = RecordingJsonClient(
        [
            {
                "exam_point_code": "rag-diagnosis",
                "assessment_units": [
                    {
                        "code": "empty-unit",
                        "title": "空单元",
                        "performance_statement": "没有卡片",
                        "exam_point_code": "rag-diagnosis",
                        "cards": [],
                    }
                ],
            }
        ]
    )

    with pytest.raises(DeepSeekModelError) as caught:
        DeepSeekExamPointKnowledgeConsolidator(client).consolidate(
            exam_point=_point(),
            admitted_decisions=[EvidenceDecision.model_validate(_decision())],
        )

    assert caught.value.error_code == "model_output_evidence_gap"


def test_consolidator_rejects_active_card_with_empty_assessable_content():
    client = RecordingJsonClient(
        [
            {
                "exam_point_code": "rag-diagnosis",
                "assessment_units": [
                    {
                        "code": "diagnose-retrieval",
                        "title": "诊断检索偏差",
                        "performance_statement": "分析召回偏差成因",
                        "exam_point_code": "rag-diagnosis",
                        "cards": [
                            {
                                "name": "空事实",
                                "performance_statement": "没有可评分事实",
                                "assessable_content": [],
                                "evidence_chunk_ids": ["e1"],
                            }
                        ],
                    }
                ],
            }
        ]
    )

    with pytest.raises(DeepSeekModelError) as caught:
        DeepSeekExamPointKnowledgeConsolidator(client).consolidate(
            exam_point=_point(),
            admitted_decisions=[EvidenceDecision.model_validate(_decision())],
        )

    assert caught.value.error_code == "model_schema_validation_failed"
