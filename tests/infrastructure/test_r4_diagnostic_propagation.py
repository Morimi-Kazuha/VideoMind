"""Offline R4 proof that Critic DTO diagnostics reach the failure boundary."""

from __future__ import annotations

import json

import pytest

from dovideo.application import AnalysisRequest, MediaRef, PendingDeadLetterHandoff
from dovideo.domain import (
    AgentPlan,
    AnalysisEvidence,
    AnalysisMode,
    AnalysisResult,
    VideoContext,
    VideoSegment,
)
from dovideo.infrastructure.celery_transport import (
    CeleryTransportSettings,
    RabbitMQDeadLetterPublisher,
)
from dovideo.infrastructure.providers import (
    CriticModelAdapter,
    ModelResponseError,
    OpenAICompatibleChatClient,
    ProviderConfig,
    ProviderHttpResponse,
)
from dovideo.infrastructure.r4_runtime import _ObservedChatClient


class _OfflineHttpClient:
    def __init__(self, body: object) -> None:
        self.body = body
        self.calls = 0

    async def post(self, url, *, headers, json, timeout):
        del url, headers, json, timeout
        self.calls += 1
        return ProviderHttpResponse(200, self.body)


class _OfflineTelemetry:
    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.observations: dict[str, float] = {}

    def increment(self, metric: str, amount: int = 1, **_) -> None:
        self.counters[metric] = self.counters.get(metric, 0) + amount

    def observe(self, metric: str, value: float, **_) -> None:
        self.observations[metric] = value


class _OfflineFailedTaskStore:
    def __init__(self) -> None:
        self.records = []

    def record(self, value):
        self.records.append(value)
        return value


class _OfflineDeadLetterPublisher(RabbitMQDeadLetterPublisher):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.published: dict[str, object] | None = None

    def _publish_message(self, message, *, message_id: str) -> None:
        self.published = message
        self.message_id = message_id


def _context() -> VideoContext:
    return VideoContext(
        source="memory://offline-r4",
        user_goal="check the evidence",
        segments=(
            VideoSegment(
                start_ms=0,
                end_ms=1_000,
                transcript="opening evidence",
                ocr_texts=("slide",),
            ),
        ),
    )


def _plan() -> AgentPlan:
    return AgentPlan(understood_goal="check the evidence", tasks=("check",))


def _result() -> AnalysisResult:
    return AnalysisResult(
        title="draft",
        conclusions=("opening evidence",),
        evidence=(
            AnalysisEvidence(
                timestamp_ms=0,
                source="ASR",
                content="opening evidence",
                claim="opening evidence",
            ),
        ),
    )


def _request() -> AnalysisRequest:
    return AnalysisRequest(
        MediaRef(901, "minio://media/901.mp4", filename="901.mp4"),
        "check the evidence",
        AnalysisMode.GENERAL,
        request_id="offline-r4-diagnostic",
    )


@pytest.mark.asyncio
async def test_critic_dto_diagnostic_survives_r4_observation_and_failure_boundary() -> None:
    synthetic_value = "offline synthetic feedback must not escape"
    malformed_critic_json = json.dumps(
        {
            "passed": False,
            "feedback": synthetic_value,
            "missingRequirements": [],
            "unsupportedClaims": [],
            "requiredTimestamps": [],
        }
    )
    http = _OfflineHttpClient(
        {"choices": [{"message": {"content": malformed_critic_json}}]}
    )
    telemetry = _OfflineTelemetry()
    config = ProviderConfig(
        base_url="https://offline.invalid/v1",
        model="offline-test-model",
        retry_delay_seconds=0,
        max_attempts=1,
    )
    observed_chat = _ObservedChatClient(
        OpenAICompatibleChatClient(config, client=http),
        telemetry,
    )

    with pytest.raises(ModelResponseError) as caught:
        await CriticModelAdapter(observed_chat).critique(
            _context(),
            _plan(),
            _result(),
        )

    error = caught.value
    assert http.calls == 2
    assert telemetry.counters == {
        "modelCalls": 2,
        "CRITICCalls": 1,
        "CRITIC_REPAIRCalls": 1,
    }
    assert isinstance(error.diagnostic, str)

    request = _request()
    handoff = PendingDeadLetterHandoff.from_request(
        request,
        attempt=3,
        error=error,
    )
    assert handoff.error_type == "ModelResponseError"
    assert "critic_dto_validation=" in handoff.error_message
    assert synthetic_value not in handoff.error_message

    failed_store = _OfflineFailedTaskStore()
    publisher = _OfflineDeadLetterPublisher(
        CeleryTransportSettings("amqp://127.0.0.1:5672/%2F"),
        failed_task_store=failed_store,
    )
    publisher._publish_business_failure(request, 3, error)

    assert publisher.published is not None
    failure_document = publisher.published["error"]
    assert isinstance(failure_document, dict)
    assert failure_document["type"] == "ModelResponseError"
    assert "CRITIC_REPAIR response did not match its DTO" in failure_document["message"]
    assert failure_document["diagnostic"] == error.diagnostic

    prefix, serialized = failure_document["diagnostic"].split("=", 1)
    assert prefix == "critic_dto_validation"
    diagnostic = json.loads(serialized)
    assert diagnostic["payload_type"] == "dict"
    assert diagnostic["field_types"]["feedback"] == "str"
    assert diagnostic["errors"][0]["loc"] == ["feedback"]
    assert diagnostic["errors"][0]["type"] == "value_error"
    assert diagnostic["errors"][0]["msg"] == "Value error, expected a collection"

    assert failed_store.records[0].error_type == "ModelResponseError"
    assert "critic_dto_validation=" in failed_store.records[0].error_message
    report_json = json.dumps(publisher.published, ensure_ascii=False)
    assert synthetic_value not in report_json
    assert malformed_critic_json not in report_json
