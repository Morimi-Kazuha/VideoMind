from __future__ import annotations

import json

import pytest

from dovideo.application.value_objects import TaskKey
from dovideo.domain import AgentPlan, AnalysisResult, VideoContext, VideoSegment
from dovideo.infrastructure.providers import (
    ExecutorModelAdapter,
    ModelResponseError,
    OpenAICompatibleChatClient,
    ProviderConfig,
    ProviderHttpResponse,
    STRUCTURED_RESPONSE_MAX_DIAGNOSTIC_BYTES,
    decode_structured_model,
)
from dovideo.infrastructure.media import SubprocessExecutionError
from dovideo.infrastructure.redis_observability import RedisTraceStore
from dovideo.infrastructure.r4_runtime import R4AgentTelemetry


class _Observer:
    def __init__(self) -> None:
        self.structured: list[dict[str, object]] = []
        self.transport: list[dict[str, object]] = []

    def record_structured_response(self, stage: str, diagnostic) -> None:
        self.structured.append({"stage": stage, **dict(diagnostic)})

    def record_model_transport(
        self,
        stage: str,
        *,
        status_code: int,
        finish_reason: str | None,
        content_present: bool,
        content_chars: int,
    ) -> None:
        self.transport.append(
            {
                "stage": stage,
                "statusCode": status_code,
                "finishReason": finish_reason,
                "contentPresent": content_present,
                "contentChars": content_chars,
            }
        )


class _HttpStub:
    def __init__(self, body: object) -> None:
        self.body = body
        self.calls: list[dict[str, object]] = []

    async def post(self, url, *, headers, json, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "json": json})
        return ProviderHttpResponse(200, self.body)


class _MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def sadd(self, key, value):
        current = self.values.setdefault(key, set())
        current.add(value)
        return 1

    def expire(self, key, seconds):
        return True

    def hset(self, key, *, mapping):
        self.hashes[key] = dict(mapping)
        return len(mapping)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))


def _config() -> ProviderConfig:
    return ProviderConfig(
        base_url="https://provider.invalid/v1",
        model="executor-test-model",
        api_key="executor-test-api-key",
        max_attempts=1,
        retry_delay_seconds=0,
    )


def _context() -> VideoContext:
    return VideoContext(
        source="memory://r4-observability",
        user_goal="find evidence",
        segments=(
            VideoSegment(
                start_ms=0,
                end_ms=60_000,
                transcript="raw prompt secret",
            ),
        ),
    )


def _plan() -> AgentPlan:
    return AgentPlan(understoodGoal="find evidence", tasks=("bind evidence",))


def _diagnostic_for(payload: object, observer: _Observer) -> dict[str, object]:
    try:
        decode_structured_model(
            payload,
            AnalysisResult,
            "EXECUTOR",
            diagnostic_observer=observer,
        )
    except ModelResponseError:
        pass
    return observer.structured[-1]


def test_dto_defaults_and_structural_shape_distinguish_absent_null_and_empty() -> None:
    observer = _Observer()

    absent = _diagnostic_for({}, observer)
    assert absent["dtoValidationSuccess"] is True
    assert absent["fields"]["conclusions"] == {
        "present": False,
        "jsonType": "absent",
        "isNull": False,
        "length": None,
    }
    assert "default_for_absent:conclusions" in absent["normalizationActions"]

    null = _diagnostic_for({"conclusions": None, "evidence": None}, observer)
    assert null["fields"]["conclusions"] == {
        "present": True,
        "jsonType": "null",
        "isNull": True,
        "length": None,
    }
    assert "null_normalized:conclusions" in null["normalizationActions"]

    empty = _diagnostic_for({"conclusions": [], "evidence": []}, observer)
    assert empty["fields"]["conclusions"] == {
        "present": True,
        "jsonType": "list",
        "isNull": False,
        "length": 0,
    }
    assert "list_to_tuple:conclusions" in empty["normalizationActions"]


def test_incomplete_and_wrong_named_executor_payloads_construct_default_dto() -> None:
    cases = (
        (
            {},
            "未命名分析",
            {"title", "conclusions", "evidence", "suggestions", "sections"},
        ),
        (
            {"title": "x"},
            "x",
            {"conclusions", "evidence", "suggestions", "sections"},
        ),
        (
            {
                "conclusion": ["semantic value must not be retained"],
                "evidences": [{"content": "semantic value must not be retained"}],
            },
            "未命名分析",
            {"title", "conclusions", "evidence", "suggestions", "sections"},
        ),
    )

    for payload, expected_title, absent_fields in cases:
        observer = _Observer()
        result = decode_structured_model(
            payload,
            AnalysisResult,
            "EXECUTOR",
            diagnostic_observer=observer,
        )

        assert result == AnalysisResult(title=expected_title)
        diagnostic = observer.structured[-1]
        assert diagnostic["dtoValidationSuccess"] is True
        assert diagnostic["extraPolicy"] == "ignore"
        assert all(diagnostic["fields"][field]["present"] is False for field in absent_fields)
        assert all(
            f"default_for_absent:{field}" in diagnostic["normalizationActions"]
            for field in absent_fields
        )

        if "conclusion" in payload:
            assert diagnostic["unknownFields"] == ["conclusion", "evidences"]
            assert "unknown_fields_ignored" in diagnostic["normalizationActions"]


def test_provider_envelope_unwrap_preserves_semantic_object_shape() -> None:
    observer = _Observer()
    result = decode_structured_model(
        {
            "choices": [
                {"message": {"content": '{"title":"x","conclusions":[]}'}}
            ]
        },
        AnalysisResult,
        "EXECUTOR",
        diagnostic_observer=observer,
    )

    assert result == AnalysisResult(title="x")
    diagnostic = observer.structured[-1]
    assert diagnostic["topLevelType"] == "dict"
    assert diagnostic["fields"]["title"]["present"] is True
    assert diagnostic["fields"]["conclusions"] == {
        "present": True,
        "jsonType": "list",
        "isNull": False,
        "length": 0,
    }


def test_unknown_fields_validation_errors_and_malformed_json_are_value_free() -> None:
    observer = _Observer()
    secret = "conclusion secret that must not be retained"

    unknown = _diagnostic_for({"unrecognized": secret}, observer)
    assert unknown["unknownFields"] == ["unrecognized"]
    assert "unknown_fields_ignored" in unknown["normalizationActions"]
    assert secret not in json.dumps(unknown, ensure_ascii=False)

    invalid = _diagnostic_for({"conclusions": secret}, observer)
    encoded_invalid = json.dumps(invalid, ensure_ascii=False)
    assert invalid["dtoValidationSuccess"] is False
    assert invalid["validationErrors"][0]["loc"] == ["conclusions"]
    assert secret not in encoded_invalid

    malformed = _diagnostic_for("not-json raw secret", observer)
    encoded_malformed = json.dumps(malformed, ensure_ascii=False)
    assert malformed["jsonDecodeSuccess"] is False
    assert malformed["errorType"] == "json_object_not_found"
    assert secret not in encoded_malformed
    assert "not-json raw secret" not in encoded_malformed

    oversized = _diagnostic_for(
        {f"unknown-{index}-{'x' * 500}": index for index in range(100)},
        observer,
    )
    assert len(oversized["unknownFields"]) <= 32
    assert len(json.dumps(oversized, ensure_ascii=False).encode("utf-8")) <= STRUCTURED_RESPONSE_MAX_DIAGNOSTIC_BYTES


@pytest.mark.asyncio
async def test_transport_and_executor_shape_observation_excludes_keys_prompts_and_text() -> None:
    observer = _Observer()
    result_payload = {
        "title": "result",
        "conclusions": ["response secret"],
        "evidence": [
            {
                "timestampMs": 0,
                "source": "ASR",
                "content": "response secret",
                "claim": "response secret",
            }
        ],
    }
    http = _HttpStub(
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(result_payload)},
                }
            ]
        }
    )
    client = OpenAICompatibleChatClient(
        _config(),
        client=http,
        response_observer=observer,
    )
    result = await ExecutorModelAdapter(
        client,
        diagnostic_observer=observer,
    ).execute(_context(), _plan())

    assert result.title == "result"
    assert observer.transport == [
        {
            "stage": "EXECUTOR",
            "statusCode": 200,
            "finishReason": "stop",
            "contentPresent": True,
            "contentChars": len(json.dumps(result_payload)),
        }
    ]
    structural = observer.structured[-1]
    assert structural["fields"]["conclusions"]["length"] == 1
    assert structural["fields"]["evidence"]["length"] == 1
    serialized = json.dumps(
        {"structured": observer.structured, "transport": observer.transport},
        ensure_ascii=False,
    )
    assert "executor-test-api-key" not in serialized
    assert "Authorization" not in serialized
    assert "raw prompt secret" not in serialized
    assert "response secret" not in serialized


def test_r4_telemetry_reuses_existing_trace_and_bounds_structural_records() -> None:
    redis = _MemoryRedis()
    trace = RedisTraceStore(redis)
    telemetry = R4AgentTelemetry(trace)
    key = TaskKey(99, "offline observability", "GENERAL")
    trace.start(key)
    token = telemetry.bind(key)
    try:
        telemetry.set_model_identifier("executor-test-model")
        for _ in range(140):
            telemetry.record_structured_response(
                "EXECUTOR",
                {
                    "fields": {"conclusions": {"present": False}},
                    "unknownFields": [],
                },
            )
        telemetry.record_structured_response(
            "EXECUTOR",
            {"unknownFields": ["x" * 10000]},
        )
        telemetry.record_media_failure(
            media_branch="OCR",
            failure_stage="KEYFRAME_EXTRACTION",
            error=SubprocessExecutionError(
                "raw stderr private-source.mp4",
                command=("ffmpeg", "private-source.mp4"),
                stdout="raw stdout secret",
                stderr="raw stderr secret",
                returncode=64,
            ),
        )
        telemetry.record_executor_structural_attempt(
            attempt=2,
            repair_triggered=True,
            repair_succeeded=True,
        )
    finally:
        telemetry.reset(token)

    document = trace.latest(key)
    records = document["responseDiagnostics"]
    assert len(records) == 128
    assert any(item.get("truncated") is True for item in records)
    assert records[-1] == {
        "kind": "executorStructuralAttempt",
        "stage": "EXECUTOR",
        "model": "executor-test-model",
        "executorStructuralAttempt": 2,
        "executorStructuralRepairTriggered": True,
        "executorStructuralRepairSucceeded": True,
    }
    media_failure = next(item for item in records if item.get("kind") == "mediaFailure")
    assert media_failure == {
        "kind": "mediaFailure",
        "mediaBranch": "OCR",
        "failureStage": "KEYFRAME_EXTRACTION",
        "errorClass": "SubprocessExecutionError",
        "processStarted": True,
        "exitCode": 64,
    }
    serialized = json.dumps(document, ensure_ascii=False)
    assert "response secret" not in serialized
    assert "private-source.mp4" not in serialized
    assert "raw stderr secret" not in serialized
    assert "raw stdout secret" not in serialized


def test_x2b_execution_projection_is_additive_and_payload_free() -> None:
    redis = _MemoryRedis()
    trace = RedisTraceStore(redis)
    key = TaskKey(99, "execution history", "GENERAL")
    trace.start(key)

    trace.record_execution_projection(
        key,
        execution_id="execution-99",
        status="COMPLETED",
        event_type="EXECUTION_COMPLETED",
        latest_sequence=11,
        recorded_semantic_events=11,
    )

    document = trace.latest(key)
    assert document["executionId"] == "execution-99"
    assert document["executionRecordStatus"] == "COMPLETED"
    assert document["latestRecordedSequence"] == 11
    assert document["recordedSemanticEvents"] == 11
    assert document["lastRecordedEventType"] == "EXECUTION_COMPLETED"
    serialized = json.dumps(document, ensure_ascii=False)
    assert "ToolResult body" not in serialized
    assert "prompt" not in serialized
    assert "transcript" not in serialized

@pytest.mark.asyncio
async def test_revision_budget_starts_fresh_and_retry_keeps_sent_usage() -> None:
    from types import SimpleNamespace
    from dovideo.application import AnalysisRequest, MediaRef
    from dovideo.domain import AgentBudgetConfig
    from dovideo.infrastructure.r4_runtime import (
        R4RequestContextCheckpoint, bind_r4_request, reset_r4_request,
    )

    store = RedisTraceStore(_MemoryRedis())
    telemetry = R4AgentTelemetry(store, budget_config=AgentBudgetConfig())
    v1 = AnalysisRequest(MediaRef(99, 'memory://99'), 'goal', request_id='v1')
    v2 = AnalysisRequest(v1.media, 'goal', request_id='revision:v2')
    key = v1.task_key
    v1_trace = store.start_for_request(key, v1.request_id)
    store.add_usage_for_key(key, estimated_tokens=17594)

    class Checkpoint:
        async def load_context(self, media_id):
            return VideoContext(source='memory://99', user_goal='goal')

    boundary = R4RequestContextCheckpoint(Checkpoint(), SimpleNamespace(telemetry=telemetry))
    bound = telemetry.bind(key)
    request_token = bind_r4_request(v2)
    try:
        await boundary.load_context(99)
        v2_trace = store.latest(key)['traceId']
        assert v2_trace != v1_trace
        admission = telemetry.admit_model_call(stage='CRITIC', model='test',
            messages=[{'role': 'user', 'content': 'x' * 12260}], attempt=1,
            max_output_tokens=None)
        assert admission['cumulativeBefore'] == 0
        assert admission['allowed'] is True
        store.add_usage_for_key(key, estimated_tokens=6130)
        await boundary.load_context(99)
        assert store.latest(key)['traceId'] == v2_trace
        assert store.current_usage_for_key(key).estimated_tokens == 6130
        historical = store._document(store.client.hgetall(store._trace_key(v1_trace)))
        assert historical['estimatedTokens'] == 17594
    finally:
        reset_r4_request(request_token)
        telemetry.reset(bound)


@pytest.mark.asyncio
async def test_foreign_context_load_cannot_reset_current_request_budget() -> None:
    from types import SimpleNamespace
    from dovideo.application import AnalysisRequest, MediaRef
    from dovideo.infrastructure.r4_runtime import (
        R4RequestContextCheckpoint, bind_r4_request, reset_r4_request,
    )
    store = RedisTraceStore(_MemoryRedis())
    request = AnalysisRequest(MediaRef(99, 'memory://99'), 'goal', request_id='current')
    trace_id = store.start_for_request(request.task_key, request.request_id)
    store.add_usage_for_key(request.task_key, estimated_tokens=1234)
    class Checkpoint:
        async def load_context(self, media_id): return None
    boundary = R4RequestContextCheckpoint(Checkpoint(), SimpleNamespace(telemetry=R4AgentTelemetry(store)))
    token = bind_r4_request(request)
    try:
        assert await boundary.load_context(100) is None
        assert store.latest(request.task_key)['traceId'] == trace_id
        assert store.current_usage_for_key(request.task_key).estimated_tokens == 1234
    finally:
        reset_r4_request(token)
