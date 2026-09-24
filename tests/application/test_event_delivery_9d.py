"""Focused Phase 9D event/status delivery-boundary tests."""

from __future__ import annotations

import asyncio

import pytest

from dovideo.application import (
    EventDeliveryResult,
    TaskEventDeliveryService,
    TaskKey,
    TaskLifecycle,
    TaskLifecycleEvent,
    TaskStatusProjection,
)
from dovideo.application.ports import (
    TaskEventDeliveryPort,
    TaskEventPublisherPort,
    TaskStatusProjectionPort,
    TaskStatusReadPort,
)
from dovideo.domain import TaskEvent, TaskStage, TaskStatusState


class RecordingPublisher:
    """Provider-neutral fake for the existing publisher port."""

    def __init__(self, *, fail: bool = False, cancel: bool = False) -> None:
        self.fail = fail
        self.cancel = cancel
        self.calls: list[tuple[TaskKey, TaskEvent]] = []

    async def publish(self, key: TaskKey, event: TaskEvent) -> None:
        self.calls.append((key, event))
        if self.cancel:
            raise asyncio.CancelledError()
        if self.fail:
            raise RuntimeError("notification unavailable")


def _event(
    key: TaskKey,
    state: TaskStatusState,
    stage: TaskStage | None,
    *,
    attempt: int = 0,
    message: str | None = None,
    result: str | None = None,
    retryable: bool = False,
) -> TaskLifecycleEvent:
    return TaskLifecycleEvent(
        key=key,
        event=TaskEvent(
            state=state,
            result=result,
            message=message,
            stage=stage,
        ),
        attempt=attempt,
        retryable=retryable,
    )


def test_public_seams_reuse_existing_ports_without_transport_types() -> None:
    assert TaskEventDeliveryPort is TaskEventPublisherPort
    assert TaskStatusReadPort is TaskStatusProjectionPort
    service = TaskEventDeliveryService()
    assert isinstance(service.projection, TaskStatusProjection)
    assert not hasattr(service, "agent_loop")
    assert not hasattr(service, "worker")


@pytest.mark.asyncio
async def test_outer_consumer_reads_current_status_and_stage_without_worker_internals() -> None:
    key = TaskKey(901, "  explain the opening  ", None)
    publisher = RecordingPublisher()
    service = TaskEventDeliveryService(
        projection=TaskStatusProjection(),
        publisher=publisher,
    )

    before = service.current(key)
    assert before.state is TaskStatusState.NOT_STARTED
    assert service.stage(key) is None

    outcome = await service.deliver(TaskLifecycle.new(key).queued().lifecycle_event)

    assert isinstance(outcome, EventDeliveryResult)
    assert outcome.delivered
    assert outcome.status.state is TaskStatusState.QUEUED
    assert service.current(key).state is TaskStatusState.QUEUED
    assert service.stage(key) is TaskStage.QUEUED
    assert publisher.calls[0][0] == key
    assert publisher.calls[0][1].stage is TaskStage.QUEUED


@pytest.mark.asyncio
async def test_existing_publisher_shape_projects_processing_retry_and_terminal_stages() -> None:
    key = TaskKey(902, "goal")
    publisher = RecordingPublisher()
    service = TaskEventDeliveryService(publisher=publisher)

    processing = await service.publish(
        key,
        TaskEvent(
            state=TaskStatusState.PROCESSING,
            message="working",
            stage=TaskStage.EXECUTOR_STARTED,
        ),
        attempt=1,
    )
    retrying = await service.publish(
        key,
        TaskEvent(
            state=TaskStatusState.PROCESSING,
            message="retry",
            stage=TaskStage.RETRYING,
        ),
        attempt=1,
    )
    resumed = await service.publish(
        key,
        TaskEvent(
            state=TaskStatusState.PROCESSING,
            message="resumed",
            stage=TaskStage.CONSUMING,
        ),
        attempt=2,
    )
    terminal = await service.publish(
        key,
        TaskEvent(
            state=TaskStatusState.FAILED,
            message="failed",
            stage=TaskStage.DEAD_LETTERED,
        ),
        attempt=3,
    )

    assert processing.status.state is TaskStatusState.PROCESSING
    assert retrying.status.state is TaskStatusState.PROCESSING
    assert retrying.stage is TaskStage.RETRYING
    assert resumed.stage is TaskStage.CONSUMING
    assert terminal.status.state is TaskStatusState.FAILED
    assert service.stage(key) is TaskStage.DEAD_LETTERED
    assert service.snapshot(key).attempt == 3
    assert service.snapshot(key).terminal


@pytest.mark.asyncio
async def test_delivery_failure_does_not_change_completed_status_or_attempt() -> None:
    key = TaskKey(903, "terminal")
    publisher = RecordingPublisher(fail=True)
    service = TaskEventDeliveryService(publisher=publisher)

    await service.deliver(
        _event(
            key,
            TaskStatusState.PROCESSING,
            TaskStage.EXECUTOR_COMPLETED,
            attempt=1,
            message="draft",
        )
    )
    outcome = await service.deliver(
        _event(
            key,
            TaskStatusState.COMPLETED,
            TaskStage.COMPLETED,
            attempt=1,
            result="done",
        )
    )

    assert not outcome.delivered
    assert outcome.subscriber_failed
    assert service.current(key).state is TaskStatusState.COMPLETED
    assert service.current(key).result == "done"
    assert service.stage(key) is TaskStage.COMPLETED
    assert service.snapshot(key).attempt == 1
    assert len(publisher.calls) == 2


@pytest.mark.asyncio
async def test_duplicate_stale_and_late_events_keep_projection_terminal_guard() -> None:
    key = TaskKey(904, "guards")
    service = TaskEventDeliveryService()
    newer = _event(
        key,
        TaskStatusState.PROCESSING,
        TaskStage.EXECUTOR_COMPLETED,
        attempt=1,
        message="newer",
    )
    stale = _event(
        key,
        TaskStatusState.PROCESSING,
        TaskStage.CONSUMING,
        attempt=1,
        message="stale",
    )

    await service.deliver(newer)
    first = service.snapshot(key)
    await service.deliver(newer)
    await service.deliver(stale)
    assert service.snapshot(key) == first

    await service.deliver(
        _event(
            key,
            TaskStatusState.COMPLETED,
            TaskStage.COMPLETED,
            attempt=1,
            result="first",
        )
    )
    await service.deliver(
        _event(
            key,
            TaskStatusState.PROCESSING,
            TaskStage.CONSUMING,
            attempt=2,
            message="late",
        )
    )
    assert service.current(key).state is TaskStatusState.COMPLETED
    assert service.current(key).result == "first"
    assert service.stage(key) is TaskStage.COMPLETED


@pytest.mark.asyncio
async def test_failed_terminal_and_general_nullable_identity_are_observable() -> None:
    service = TaskEventDeliveryService()
    nullable = TaskKey(905, "goal", None)
    explicit_general = TaskKey(905, "goal")

    await service.deliver(
        _event(
            nullable,
            TaskStatusState.FAILED,
            TaskStage.FAILED,
            attempt=1,
            message="failed",
        )
    )

    assert service.current(explicit_general).state is TaskStatusState.FAILED
    assert service.stage(explicit_general) is TaskStage.FAILED
    assert service.snapshot(explicit_general).terminal


@pytest.mark.asyncio
async def test_cancellation_from_notification_sink_is_not_misreported_as_success() -> None:
    key = TaskKey(906, "cancel")
    service = TaskEventDeliveryService(publisher=RecordingPublisher(cancel=True))

    with pytest.raises(asyncio.CancelledError):
        await service.deliver(
            _event(key, TaskStatusState.PROCESSING, TaskStage.CONSUMING, attempt=1)
        )

    # Projection happens before delivery and remains available to a caller
    # handling cancellation; no worker/model call exists in this boundary.
    assert service.current(key).state is TaskStatusState.PROCESSING
    assert service.snapshot(key).attempt == 1


def test_caller_supplied_replay_remains_non_persistent() -> None:
    key = TaskKey(907, "replay")
    service = TaskEventDeliveryService()
    projected = service.replay(
        [
            _event(key, TaskStatusState.QUEUED, TaskStage.QUEUED),
            _event(key, TaskStatusState.PROCESSING, TaskStage.CONSUMING, attempt=1),
        ]
    )

    assert projected[key].state is TaskStatusState.PROCESSING
    assert service.stage(key) is TaskStage.CONSUMING
    # A fresh boundary has no durable/event-history replay source.
    assert TaskEventDeliveryService().current(key).state is TaskStatusState.NOT_STARTED
