"""Phase 9C read-side task status/event projection tests."""

from __future__ import annotations

import pytest

from dovideo.application import (
    StatusProjectionSnapshot,
    TaskKey,
    TaskLifecycle,
    TaskLifecycleEvent,
    TaskStatusProjection,
)
from dovideo.application.ports import TaskStatusProjectionPort
from dovideo.domain import AnalysisMode, TaskEvent, TaskStage, TaskStatus, TaskStatusState


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


def test_no_event_is_java_not_started_and_port_is_minimal() -> None:
    key = TaskKey(1, "goal", None)
    projection: TaskStatusProjectionPort = TaskStatusProjection()

    status = projection.current(key)

    assert status.state is TaskStatusState.NOT_STARTED
    assert status.message == "尚未提交分析任务"
    assert projection.stage(key) is None
    assert isinstance(projection.snapshot(key), StatusProjectionSnapshot)
    assert TaskStatusProjectionPort.__name__ == "TaskStatusProjectionPort"


def test_queued_event_projects_queued_without_starting_an_attempt() -> None:
    key = TaskKey(2, "goal")
    lifecycle = TaskLifecycle.new(key).queued()
    projection = TaskStatusProjection()

    status = projection.apply(lifecycle.lifecycle_event)

    assert status.state is TaskStatusState.QUEUED
    assert projection.stage(key) is TaskStage.QUEUED
    assert projection.snapshot(key).attempt == 0
    assert not projection.snapshot(key).retryable


def test_processing_events_update_stage_without_changing_state() -> None:
    key = TaskKey(3, "goal")
    started = TaskLifecycle.new(key).begin_attempt(stage=TaskStage.VIDEO_CONTEXT)
    planned = started.processing(stage=TaskStage.PLAN_COMPLETED)
    projection = TaskStatusProjection()

    projection.apply(started.lifecycle_event)
    status = projection.apply(planned.lifecycle_event)

    assert status.state is TaskStatusState.PROCESSING
    assert projection.stage(key) is TaskStage.PLAN_COMPLETED
    assert projection.snapshot(key).attempt == 1
    assert not projection.snapshot(key).terminal


def test_retrying_projects_processing_and_next_processing_is_legal() -> None:
    key = TaskKey(4, "goal")
    started = TaskLifecycle.new(key).begin_attempt()
    retrying = started.retry()
    next_delivery = retrying.begin_attempt(stage=TaskStage.CONSUMING)
    projection = TaskStatusProjection()

    projection.apply(retrying.lifecycle_event)
    assert projection.current(key).state is TaskStatusState.PROCESSING
    assert projection.stage(key) is TaskStage.RETRYING
    assert projection.snapshot(key).retryable

    projection.apply(next_delivery.lifecycle_event)
    snapshot = projection.snapshot(key)
    assert snapshot.status.state is TaskStatusState.PROCESSING
    assert snapshot.stage is TaskStage.CONSUMING
    assert snapshot.attempt == 2
    assert not snapshot.retryable


def test_completed_is_terminal_and_late_processing_cannot_downgrade_it() -> None:
    key = TaskKey(5, "goal")
    completed = TaskLifecycle.new(key).begin_attempt().complete("first result")
    late_processing = _event(
        key,
        TaskStatusState.PROCESSING,
        TaskStage.CONSUMING,
        attempt=2,
        message="late processing",
    )
    conflicting_terminal = _event(
        key,
        TaskStatusState.COMPLETED,
        TaskStage.COMPLETED,
        attempt=2,
        result="different result",
        message="different terminal",
    )
    projection = TaskStatusProjection()

    projection.apply(completed.lifecycle_event)
    projection.apply(late_processing)
    projection.apply(conflicting_terminal)

    status = projection.current(key)
    assert status.state is TaskStatusState.COMPLETED
    assert status.result == "first result"
    assert status.message == "任务完成"
    assert projection.stage(key) is TaskStage.COMPLETED
    assert projection.snapshot(key).attempt == 1


@pytest.mark.parametrize("stage", [TaskStage.FAILED, TaskStage.DEAD_LETTERED])
def test_failed_and_dead_lettered_are_failed_terminal(stage: TaskStage) -> None:
    key = TaskKey(6, stage.value)
    event = _event(
        key,
        TaskStatusState.FAILED,
        stage,
        attempt=1,
        message="terminal failure",
    )
    projection = TaskStatusProjection()

    status = projection.apply(event)

    assert status.state is TaskStatusState.FAILED
    assert status.message == "terminal failure"
    assert projection.stage(key) is stage
    assert projection.snapshot(key).terminal


def test_duplicate_events_are_idempotent_and_terminal_repetition_is_stable() -> None:
    key = TaskKey(7, "goal")
    processing = _event(
        key,
        TaskStatusState.PROCESSING,
        TaskStage.EXECUTOR_STARTED,
        attempt=1,
        message="working",
    )
    first_terminal = _event(
        key,
        TaskStatusState.FAILED,
        TaskStage.DEAD_LETTERED,
        attempt=1,
        message="first failure",
    )
    repeated_terminal = _event(
        key,
        TaskStatusState.FAILED,
        TaskStage.FAILED,
        attempt=1,
        message="second failure",
    )
    projection = TaskStatusProjection()

    projection.apply(processing)
    before = projection.snapshot(key)
    projection.apply(processing)
    assert projection.snapshot(key) == before

    projection.apply(first_terminal)
    projection.apply(repeated_terminal)
    assert projection.current(key).message == "first failure"
    assert projection.stage(key) is TaskStage.DEAD_LETTERED


def test_same_attempt_stale_nonterminal_stage_is_ignored() -> None:
    key = TaskKey(8, "goal")
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
    projection = TaskStatusProjection()

    projection.apply(newer)
    projection.apply(stale)

    assert projection.stage(key) is TaskStage.EXECUTOR_COMPLETED
    assert projection.current(key).message == "newer"


def test_replay_projects_completed_failed_and_retry_flows_without_history_store() -> None:
    completed_key = TaskKey(9, "complete", AnalysisMode.REVIEW)
    failed_key = TaskKey(10, "failed")
    retry_key = TaskKey(11, "retry", AnalysisMode.LEARNING)
    retry_started = TaskLifecycle.new(retry_key).begin_attempt()
    retrying = retry_started.retry()
    retry_processing = retrying.begin_attempt(stage=TaskStage.VIDEO_CONTEXT)
    events = [
        TaskLifecycle.new(completed_key).begin_attempt().complete("done").lifecycle_event,
        _event(
            failed_key,
            TaskStatusState.FAILED,
            TaskStage.DEAD_LETTERED,
            attempt=3,
            message="failed",
        ),
        retrying.lifecycle_event,
        retry_processing.lifecycle_event,
    ]

    projection = TaskStatusProjection()
    projected = projection.replay(events)

    assert projected[completed_key].state is TaskStatusState.COMPLETED
    assert projected[failed_key].state is TaskStatusState.FAILED
    assert projected[retry_key].state is TaskStatusState.PROCESSING
    assert projection.stage(retry_key) is TaskStage.VIDEO_CONTEXT
    assert projection.snapshot(retry_key).attempt == 2


def test_general_nullable_mode_keeps_one_task_identity() -> None:
    normalized = TaskKey(12, "  goal  ", None)
    explicit = TaskKey(12, "goal", AnalysisMode.GENERAL)
    projection = TaskStatusProjection()

    projection.apply(
        _event(
            normalized,
            TaskStatusState.QUEUED,
            TaskStage.QUEUED,
            message="queued",
        )
    )

    assert projection.current(explicit).state is TaskStatusState.QUEUED
    assert projection.keys() == (explicit,)


def test_processing_can_reach_completed_or_failed_terminal_state() -> None:
    completed_key = TaskKey(13, "complete")
    failed_key = TaskKey(14, "fail")
    projection = TaskStatusProjection()

    projection.apply(_event(completed_key, TaskStatusState.PROCESSING, TaskStage.CONSUMING, attempt=1))
    projection.apply(
        _event(
            completed_key,
            TaskStatusState.COMPLETED,
            TaskStage.COMPLETED,
            attempt=1,
            result="result",
        )
    )
    projection.apply(_event(failed_key, TaskStatusState.PROCESSING, TaskStage.CONSUMING, attempt=1))
    projection.apply(
        _event(
            failed_key,
            TaskStatusState.FAILED,
            TaskStage.FAILED,
            attempt=1,
        )
    )

    assert projection.current(completed_key).state is TaskStatusState.COMPLETED
    assert projection.current(failed_key).state is TaskStatusState.FAILED
