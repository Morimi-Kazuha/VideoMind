"""Phase 9A pure task identity/lifecycle contract tests."""

from __future__ import annotations

import pytest

from dovideo.application import (
    AnalysisTaskKeys,
    AnalysisTaskLifecycle,
    DEFAULT_MAX_ATTEMPTS,
    TaskAttempt,
    TaskKey,
    TaskLifecycle,
    TaskLifecycleError,
    TaskLifecycleEvent,
)
from dovideo.application.ports import TaskLifecyclePort
from dovideo.domain import AnalysisMode, TaskStage, TaskStatusState


def test_lifecycle_reuses_task_key_and_general_nullable_mode() -> None:
    key = TaskKey(17, "  summarize  ", None)
    lifecycle = TaskLifecycle.new(key)

    assert lifecycle.identity is key
    assert lifecycle.key == TaskKey(17, "summarize", AnalysisMode.GENERAL)
    assert lifecycle.key.mode is AnalysisMode.GENERAL
    assert AnalysisTaskLifecycle is TaskLifecycle
    assert AnalysisTaskKeys.goalDigest(" summarize ", None) == AnalysisTaskKeys.goalDigest(
        "summarize", AnalysisMode.GENERAL
    )


def test_attempt_counter_is_zero_based_before_delivery_and_inclusive_at_three() -> None:
    attempt = TaskAttempt()
    assert attempt.number == 0
    assert attempt.max_attempts == DEFAULT_MAX_ATTEMPTS
    assert attempt.can_retry

    attempt = attempt.next().next().next()
    assert attempt.number == 3
    assert not attempt.can_retry
    with pytest.raises(TaskLifecycleError, match="maximum delivery attempts"):
        attempt.next()


def test_new_and_queued_states_match_java_status_and_stage() -> None:
    lifecycle = TaskLifecycle.new(TaskKey(1, "goal"))
    assert lifecycle.state is TaskStatusState.NOT_STARTED
    assert lifecycle.stage is None
    assert not lifecycle.terminal
    assert not lifecycle.lifecycle_event.terminal

    queued = lifecycle.queued()
    assert queued.state is TaskStatusState.QUEUED
    assert queued.stage is TaskStage.QUEUED
    assert queued.attempt == 0
    assert not queued.terminal
    assert queued.event.message == "任务已进入异步分析队列"


def test_begin_attempt_increments_delivery_attempt_and_processing_is_non_terminal() -> None:
    lifecycle = TaskLifecycle.new(TaskKey(2, "goal")).queued().begin_attempt()

    assert lifecycle.attempt == 1
    assert lifecycle.state is TaskStatusState.PROCESSING
    assert lifecycle.stage is TaskStage.CONSUMING
    assert not lifecycle.terminal

    next_attempt = lifecycle.begin_attempt(stage=TaskStage.AGENT_LOOP)
    assert next_attempt.attempt == 2
    assert next_attempt.stage is TaskStage.AGENT_LOOP


def test_retryable_failure_is_retrying_processing_not_terminal() -> None:
    lifecycle = TaskLifecycle.new(TaskKey(3, "goal")).begin_attempt()
    retrying = lifecycle.fail(retryable=True)

    assert retrying.state is TaskStatusState.PROCESSING
    assert retrying.stage is TaskStage.RETRYING
    assert retrying.retryable
    assert retrying.can_retry
    assert not retrying.terminal
    assert not retrying.event.terminal()


def test_retry_budget_exhaustion_and_final_failure_are_distinct() -> None:
    lifecycle = TaskLifecycle.new(TaskKey(4, "goal")).begin_attempt()
    lifecycle = lifecycle.begin_attempt().begin_attempt()
    assert lifecycle.attempt == 3
    assert not lifecycle.can_retry

    with pytest.raises(TaskLifecycleError, match="no retryable"):
        lifecycle.retry()

    failed = lifecycle.fail(stage=TaskStage.DEAD_LETTERED)
    assert failed.state is TaskStatusState.FAILED
    assert failed.stage is TaskStage.DEAD_LETTERED
    assert failed.terminal
    assert failed.event.terminal()
    assert not failed.retryable


def test_completed_event_is_terminal_and_blocks_future_transitions() -> None:
    completed = TaskLifecycle.new(TaskKey(5, "goal")).begin_attempt().complete("markdown")

    assert completed.state is TaskStatusState.COMPLETED
    assert completed.stage is TaskStage.COMPLETED
    assert completed.status.result == "markdown"
    assert completed.terminal
    assert completed.lifecycle_event.terminal
    with pytest.raises(TaskLifecycleError, match="terminal"):
        completed.begin_attempt()
    with pytest.raises(TaskLifecycleError, match="terminal"):
        completed.queued()


def test_lifecycle_event_keeps_existing_task_event_wire_shape_and_adds_attempt_context() -> None:
    lifecycle = TaskLifecycle.new(TaskKey(6, "goal", "review")).begin_attempt()
    envelope = lifecycle.lifecycle_event

    assert isinstance(envelope, TaskLifecycleEvent)
    assert envelope.key == TaskKey(6, "goal", AnalysisMode.REVIEW)
    assert envelope.attempt == 1
    assert envelope.state is TaskStatusState.PROCESSING
    assert envelope.stage is TaskStage.CONSUMING
    assert not envelope.terminal_event()
    assert envelope.event.model_dump(by_alias=True)["state"] == "PROCESSING"


def test_custom_attempt_limit_is_validated_without_worker_side_effects() -> None:
    lifecycle = TaskLifecycle.new(TaskKey(7, "goal"), max_attempts=1)
    started = lifecycle.begin_attempt()
    assert started.attempt == 1
    assert not started.can_retry
    with pytest.raises(TaskLifecycleError):
        started.fail(retryable=True)
    with pytest.raises(ValueError, match="at least one"):
        TaskLifecycle.new(TaskKey(7, "goal"), max_attempts=0)


def test_lifecycle_port_is_public_contract_only() -> None:
    # Protocol import is the contract check; no concrete queue/worker is
    # intentionally supplied in Phase 9A.
    assert TaskLifecyclePort.__name__ == "TaskLifecyclePort"
