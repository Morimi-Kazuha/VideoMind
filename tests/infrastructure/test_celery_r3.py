"""R3 Celery/RabbitMQ transport contract tests without a live broker."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
    assert app.conf.worker_prefetch_multiplier == 1


def test_authenticated_principal_survives_celery_roundtrip_without_changing_task_key() -> None:
    request = replace(_request(), user_id=73)

    message = CeleryAnalysisEnvelope.from_request(request).as_message()
    restored = CeleryAnalysisEnvelope.model_validate(message).to_request()

    assert message["userId"] == 73
    assert restored == request
    assert restored.task_key == _request().task_key
    assert restored.request_id == _request().request_id


def test_legacy_celery_message_without_principal_remains_readable() -> None:
    message = CeleryAnalysisEnvelope.from_request(_request()).as_message()
    message.pop("userId")

    restored = CeleryAnalysisEnvelope.model_validate(message).to_request()

    assert restored == _request()
    assert restored.user_id is None


@pytest.mark.parametrize("principal", [True, False, "73", 73.0, 0, -1, [], {}])
def test_analysis_request_rejects_invalid_principal(principal) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(_request(), user_id=principal)


@pytest.mark.parametrize("principal", [True, False, "73", 73.0, 0, -1, [], {}])
def test_celery_envelope_rejects_invalid_principal_without_coercion(principal) -> None:
    message = CeleryAnalysisEnvelope.from_request(_request()).as_message()

    with pytest.raises(ValueError):
        CeleryAnalysisEnvelope.model_validate({**message, "userId": principal})


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [3, 4])
async def test_production_submission_transports_only_owned_media_and_authenticated_principal(version) -> None:
    from dovideo.presentation.api.r3_runtime import ProductionR3Services
    from dovideo.presentation.api.r4_runtime import ProductionR4Services

    service_type = ProductionR3Services if version == 3 else ProductionR4Services
    services = service_type.__new__(service_type)
    trusted_media = MediaRef(9, "minio://private/owner-73.mp4", content_hash="a" * 64)
    services.media = SimpleNamespace(require_owned=AsyncMock(
        return_value=SimpleNamespace(to_ref=lambda: trusted_media),
    ))
    services.checkpoint = SimpleNamespace(load_result=AsyncMock(return_value=None))
    services.dispatcher = SimpleNamespace(dispatch=AsyncMock(return_value=DispatchDisposition.ACCEPTED))
    services.trace = SimpleNamespace(start=Mock())

    assert await services.submit_analysis(9, 73, "current goal", AnalysisMode.GENERAL) is DispatchDisposition.ACCEPTED

    services.media.require_owned.assert_awaited_once_with(9, 73)
    request = services.dispatcher.dispatch.await_args.args[0]
    assert request.user_id == 73
    assert request.media == trusted_media

    services.media.require_owned.side_effect = PermissionError("not owned")
    services.dispatcher.dispatch.reset_mock()
    with pytest.raises(PermissionError):
        await services.submit_analysis(9, 74, "current goal", AnalysisMode.GENERAL)
    services.dispatcher.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_production_revision_preserves_authenticated_principal_after_ownership_check() -> None:
    from dovideo.presentation.api.r2_runtime import ProductionR2Services

    services = ProductionR2Services.__new__(ProductionR2Services)
    services.media = SimpleNamespace(require_owned=AsyncMock(return_value=SimpleNamespace(to_ref=lambda: _request().media)))
    services.dispatcher = SimpleNamespace(dispatch=AsyncMock(return_value=DispatchDisposition.ACCEPTED))
    services.checkpoint = object()
    feedback = SimpleNamespace(
        media_id=9, goal="previous goal", corrected_goal="current goal",
        mode=AnalysisMode.GENERAL, corrected_tasks=(),
    )

    assert await services.revise_analysis(feedback, 73) is DispatchDisposition.ACCEPTED

    services.media.require_owned.assert_awaited_once_with(9, 73)
    request = services.dispatcher.dispatch.await_args.args[0]
    assert request.user_id == 73
    assert request.goal == "current goal"
    assert request.request_id.startswith("revision:")


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


def test_registered_task_translates_worker_retry_without_own_attempt_policy(monkeypatch) -> None:
    runtime = _TaskRuntime("RETRY")
    app = create_celery_app(_settings())
    task = register_analysis_task(app, runtime_factory=lambda: runtime)

    assert task.max_retries is None
    def retry(**kwargs):
        raise Retry()
    monkeypatch.setattr(task, "retry", retry)
    with pytest.raises(Retry):
        task.run(CeleryAnalysisEnvelope.from_request(_request()).as_message())


def test_registered_task_acknowledges_stale_delivery_without_retry() -> None:
    runtime = _TaskRuntime("STALE")
    app = create_celery_app(_settings())
    task = register_analysis_task(app, runtime_factory=lambda: runtime)

    result = task.run(CeleryAnalysisEnvelope.from_request(_request()).as_message())

    assert result["disposition"] == "STALE"
    assert runtime.calls == [_request()]


@pytest.mark.parametrize("disposition, countdown", [("LOCKED", 5.0), ("RETRY", 0.25)])
def test_contention_uses_delayed_transport_retry_without_business_attempt_policy(monkeypatch, disposition, countdown):
    app = create_celery_app(_settings())
    task = register_analysis_task(app, runtime_factory=lambda: _TaskRuntime(disposition))
    calls = []
    def retry(**kwargs):
        calls.append(kwargs)
        raise Retry()
    monkeypatch.setattr(task, "retry", retry)
    with pytest.raises(Retry): task.run(CeleryAnalysisEnvelope.from_request(_request()).as_message())
    assert calls[0]["countdown"] == countdown
    assert calls[0]["max_retries"] is None


def test_lease_loss_uses_recovery_retry_and_default_composition_is_r4(monkeypatch):
    from dovideo.application.task_lease import TaskLeaseLost
    from dovideo.infrastructure.r4_runtime import R4WorkerRuntime
    class Runtime:
        async def process(self, request): raise TaskLeaseLost("not owner")
    selected = []
    def factory(*, settings):
        selected.append(settings)
        return Runtime()
    monkeypatch.setattr(R4WorkerRuntime, "from_environment", factory)
    app = create_celery_app(_settings())
    task = register_analysis_task(app)
    calls = []
    def retry(**kwargs):
        calls.append(kwargs)
        raise Retry()
    monkeypatch.setattr(task, "retry", retry)
    with pytest.raises(Retry): task.run(CeleryAnalysisEnvelope.from_request(_request()).as_message())
    assert selected == [_settings()]
    assert calls[0]["max_retries"] is None and calls[0]["countdown"] == 0.25


@pytest.mark.parametrize("delay", [0, -1, float("nan"), float("inf"), True])
def test_locked_delay_rejects_invalid_or_hot_loop_configuration(delay):
    with pytest.raises(RuntimeError):
        CeleryTransportSettings(_settings().broker_url, locked_countdown_seconds=delay)


def test_locked_delay_environment_override():
    settings = CeleryTransportSettings.from_environment({
        "DOVIDEO_PROFILE": "production", "DOVIDEO_BROKER_URL": _settings().broker_url,
        "DOVIDEO_CELERY_LOCKED_COUNTDOWN_SECONDS": "12",
    })
    assert settings.locked_countdown_seconds == 12


@pytest.mark.parametrize("publication_fails", [False, True])
def test_poison_handoff_retries_and_requeues_if_publication_fails(monkeypatch, publication_fails):
    from celery.exceptions import Reject
    class Runtime:
        async def record_poison(self, payload, error):
            raise OSError("private database details")
    task = register_analysis_task(create_celery_app(_settings()), runtime_factory=Runtime)
    calls = []
    def retry(**kwargs):
        calls.append(kwargs)
        if publication_fails:
            raise OSError("private broker details")
        raise Retry()
    monkeypatch.setattr(task, "retry", retry)
    with pytest.raises(Reject if publication_fails else Retry) as caught:
        task.run({"untrusted": "secret-payload"})
    assert calls[0]["max_retries"] is None
    assert calls[0]["countdown"] == _settings().retry_countdown_seconds
    assert "secret" not in str(calls[0]["exc"])
    if publication_fails:
        assert caught.value.requeue is True
