"""R3 Celery/RabbitMQ transport contract tests without a live broker."""

from __future__ import annotations

import pytest
from celery.exceptions import Retry

from dovideo.application import (
    AnalysisRequest,
    DispatchDisposition,
    MediaRef,
    TaskDispatchService,
)
from dovideo.application.worker import WorkerDisposition
from dovideo.domain import (
    AgentState,
    AnalysisMode,
    AnalysisResult,
    TaskStage,
    VideoContext,
    VideoSegment,
)
from dovideo.infrastructure.celery_runtime import (
    R3RequestContextCheckpoint,
    bind_r3_request,
    reset_r3_request,
)
from dovideo.infrastructure.celery_transport import (
    CeleryAnalysisEnvelope,
    CeleryTaskTransport,
    CeleryTransportSettings,
    TransportEnqueueError,
    create_celery_app,
)
from dovideo.infrastructure.celery_tasks import register_analysis_task


def _request() -> AnalysisRequest:
    return AnalysisRequest(
        MediaRef(9, "minio://media/9.mp4", filename="9.mp4"),
        "find the opening evidence",
        AnalysisMode.GENERAL,
        request_id="r3-test",
    )


def _settings() -> CeleryTransportSettings:
    return CeleryTransportSettings("amqp://dovideo:password@127.0.0.1:5672/%2F")


class _Active:
    def __init__(self) -> None:
        self.values: set[object] = set()
        self.released: list[object] = []

    async def reserve(self, key, *, ttl_seconds: float) -> bool:
        del ttl_seconds
        if key in self.values:
            return False
        self.values.add(key)
        return True

    async def release(self, key) -> None:
        self.released.append(key)
        self.values.discard(key)


class _Lifecycle:
    def __init__(self) -> None:
        self.values = {}

    async def save_lifecycle(self, lifecycle) -> None:
        self.values[lifecycle.key] = lifecycle


class _Transport:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[AnalysisRequest] = []

    async def enqueue(self, request: AnalysisRequest) -> None:
        self.requests.append(request)
        if self.error is not None:
            raise self.error


class _Events:
    async def publish(self, key, event) -> None:
        del key, event
        raise RuntimeError("notification unavailable")


class _ContextCheckpoint:
    async def load_context(self, media_id: int):
        return VideoContext(
            source=f"memory://{media_id}",
            userGoal="",
            segments=(VideoSegment(startMs=0, endMs=1000, transcript="evidence"),),
        )

    async def save_context(self, media_id: int, context: VideoContext) -> None:
        del media_id, context

    async def load_chunks(self, media_id: int):
        del media_id
        return None

    async def save_chunks(self, media_id: int, chunks) -> None:
        del media_id, chunks


@pytest.mark.asyncio
async def test_dispatch_enqueues_after_reservation_and_fails_open_on_broker_error() -> None:
    active = _Active()
    lifecycle = _Lifecycle()
    transport = _Transport()
    service = TaskDispatchService(
        active,
        lifecycle=lifecycle,
        transport=transport,
    )

    assert await service.dispatch(_request()) is DispatchDisposition.ACCEPTED
    assert transport.requests == [_request()]
    assert active.values == {_request().task_key}
    assert lifecycle.values[_request().task_key].stage is TaskStage.QUEUED

    failing_active = _Active()
    failing_lifecycle = _Lifecycle()
    failing_transport = _Transport(RuntimeError("broker unavailable"))
    failing = TaskDispatchService(
        failing_active,
        lifecycle=failing_lifecycle,
        transport=failing_transport,
    )
    assert await failing.dispatch(_request()) is DispatchDisposition.FAILED
    assert not failing_active.values
    assert failing_lifecycle.values[_request().task_key].stage is TaskStage.DISPATCH_FAILED


@pytest.mark.asyncio
async def test_accepted_enqueue_is_not_rewritten_by_later_event_failure() -> None:
    active = _Active()
    lifecycle = _Lifecycle()
    transport = _Transport()
    service = TaskDispatchService(
        active,
        lifecycle=lifecycle,
        events=_Events(),
        transport=transport,
    )

    assert await service.dispatch(_request()) is DispatchDisposition.ACCEPTED
    assert active.values == {_request().task_key}
    assert lifecycle.values[_request().task_key].stage is TaskStage.QUEUED
    assert transport.requests == [_request()]


@pytest.mark.asyncio
async def test_celery_transport_reports_unavailable_broker_without_local_fallback() -> None:
    class UnavailableApp:
        def send_task(self, *args, **kwargs) -> None:
            del args, kwargs
            raise OSError("broker unavailable")

    transport = CeleryTaskTransport(UnavailableApp(), _settings())
    with pytest.raises(TransportEnqueueError):
        await transport.enqueue(_request())


def test_envelope_is_bounded_json_and_celery_disallows_pickle() -> None:
    envelope = CeleryAnalysisEnvelope.from_request(_request())
    message = envelope.as_message()
    assert message["mediaId"] == 9
    assert message["requestId"] == "r3-test"
    assert CeleryAnalysisEnvelope.model_validate(message).to_request() == _request()

    with pytest.raises(ValueError):
        CeleryAnalysisEnvelope.model_validate({**message, "unknown": True})
    app = create_celery_app(_settings())
    assert app.conf.task_serializer == "json"
    assert tuple(app.conf.accept_content) == ("json",)
    assert app.conf.result_serializer == "json"
    assert app.conf.task_acks_late is True
    assert app.conf.task_acks_on_failure_or_timeout is False
    assert app.conf.task_reject_on_worker_lost is True


@pytest.mark.asyncio
async def test_r3_context_checkpoint_rebinds_request_goal_without_rewriting_shared_context() -> None:
    request = _request()
    adapter = R3RequestContextCheckpoint(_ContextCheckpoint())
    token = bind_r3_request(request)
    try:
        loaded = await adapter.load_context(request.media.media_id)
    finally:
        reset_r3_request(token)

    assert loaded is not None
    assert loaded.user_goal == request.goal
    unbound = await adapter.load_context(request.media.media_id)
    assert unbound is not None and unbound.user_goal == ""


class _TaskRuntime:
    def __init__(self, disposition: str) -> None:
        self.disposition = disposition
        self.calls = []

    async def process(self, request):
        self.calls.append(request)
        return type(
            "Outcome",
            (),
            {
                "disposition": WorkerDisposition(self.disposition),
                "attempt": 1,
                "recovered": False,
            },
        )()

    async def record_poison(self, payload, error):
        del payload, error


def test_registered_task_acknowledges_terminal_worker_outcome() -> None:
    runtime = _TaskRuntime("COMPLETED")
    app = create_celery_app(_settings())
    task = register_analysis_task(app, runtime_factory=lambda: runtime)

    result = task.run(CeleryAnalysisEnvelope.from_request(_request()).as_message())

    assert result == {
        "disposition": "COMPLETED",
        "businessAttempt": 1,
        "recovered": False,
    }
    assert runtime.calls == [_request()]


def test_registered_task_translates_worker_retry_without_own_attempt_policy() -> None:
    runtime = _TaskRuntime("RETRY")
    app = create_celery_app(_settings())
    task = register_analysis_task(app, runtime_factory=lambda: runtime)

    with pytest.raises((Retry, RuntimeError)):
        task.run(CeleryAnalysisEnvelope.from_request(_request()).as_message())
