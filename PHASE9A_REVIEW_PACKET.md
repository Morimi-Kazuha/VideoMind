# PHASE 9A REVIEW PACKET
## Target A
### File
`src/dovideo/application/task_lifecycle.py`
### Symbols
`DEFAULT_MAX_ATTEMPTS`, `TaskLifecycleError`, `TaskAttempt`, `TaskLifecycle`, `TaskLifecycle.new`, `identity`, `state`, `terminal`, `can_retry`, `_with`, `queued`, `begin_attempt`, `processing`, `retry`, `complete`, `fail`.

### Source

```python
from dataclasses import dataclass, replace
from typing import Any
from dovideo.domain import TaskEvent, TaskStage, TaskStatus, TaskStatusState
from .value_objects import TaskKey
DEFAULT_MAX_ATTEMPTS = 3
class TaskLifecycleError(ValueError):
    """Raised when a lifecycle transition cannot be represented safely."""
@dataclass(frozen=True, slots=True)
class TaskAttempt:
    number: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    def __post_init__(self) -> None:
        if isinstance(self.number, bool) or not isinstance(self.number, int):
            raise TypeError("attempt number must be an integer")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise TypeError("max_attempts must be an integer")
        if self.number < 0:
            raise ValueError("attempt number must be non-negative")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.number > self.max_attempts:
            raise ValueError("attempt number cannot exceed max_attempts")
    @property
    def can_retry(self) -> bool:
        return self.number < self.max_attempts

    @property
    def retryable(self) -> bool:
        return self.can_retry
    def next(self) -> "TaskAttempt":
        if not self.can_retry:
            raise TaskLifecycleError("maximum delivery attempts exhausted")
        return replace(self, number=self.number + 1)
```

```python
@dataclass(frozen=True, slots=True)
class TaskLifecycle:
    key: TaskKey
    status: TaskStatus
    stage: TaskStage | None = None
    attempt: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    retryable: bool = False
    def __post_init__(self) -> None:
        if not isinstance(self.key, TaskKey):
            raise TypeError("key must be a TaskKey")
        if not isinstance(self.status, TaskStatus):
            raise TypeError("status must be a TaskStatus")
        if self.stage is not None and not isinstance(self.stage, TaskStage):
            raise TypeError("stage must be a TaskStage or None")
        TaskAttempt(self.attempt, self.max_attempts)
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be a boolean")
    @classmethod
    def new(
        cls,
        key: TaskKey,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> "TaskLifecycle":
        return cls(
            key=key,
            status=TaskStatus.of(TaskStatusState.NOT_STARTED, "尚未提交分析任务"),
            max_attempts=max_attempts,
        )
    @property
    def identity(self) -> TaskKey:
        return self.key
    @property
    def state(self) -> TaskStatusState | None:
        return self.status.state
    @property
    def terminal(self) -> bool:
        return self.event.terminal()
    @property
    def can_retry(self) -> bool:
        return not self.terminal and TaskAttempt(self.attempt, self.max_attempts).can_retry
    @property
    def event(self) -> TaskEvent:
        return TaskEvent.of(self.status, self.stage)
    def _with(
        self,
        status: TaskStatus,
        stage: TaskStage | None,
        *,
        attempt: int | None = None,
        retryable: bool = False,
    ) -> "TaskLifecycle":
        return replace(
            self,
            status=status,
            stage=stage,
            attempt=self.attempt if attempt is None else attempt,
            retryable=retryable,
        )
```

```python
    def queued(self, message: str = "任务已进入异步分析队列") -> "TaskLifecycle":
        if self.terminal:
            raise TaskLifecycleError("terminal task cannot be queued")
        return self._with(
            TaskStatus.of(TaskStatusState.QUEUED, message),
            TaskStage.QUEUED,
        )
    def begin_attempt(
        self,
        message: str = "视频分析任务开始执行",
        *,
        stage: TaskStage = TaskStage.CONSUMING,
    ) -> "TaskLifecycle":
        if self.terminal:
            raise TaskLifecycleError("terminal task cannot begin another attempt")
        next_attempt = TaskAttempt(self.attempt, self.max_attempts).next()
        return self._with(
            TaskStatus.of(TaskStatusState.PROCESSING, message),
            stage,
            attempt=next_attempt.number,
        )
    def processing(
        self,
        stage: TaskStage = TaskStage.CONSUMING,
        message: str = "正在分析视频",
    ) -> "TaskLifecycle":
        if self.terminal:
            raise TaskLifecycleError("terminal task cannot become processing")
        return self._with(TaskStatus.of(TaskStatusState.PROCESSING, message), stage)
    def retry(self, message: str = "本次执行失败，等待消息队列重试") -> "TaskLifecycle":
        if not self.can_retry:
            raise TaskLifecycleError("task has no retryable delivery remaining")
        return self._with(
            TaskStatus.of(TaskStatusState.PROCESSING, message),
            TaskStage.RETRYING,
            retryable=True,
        )
    retry_required = retry
    def complete(
        self,
        result: str | None | Any = None,
        message: str = "任务完成",
        *,
        stage: TaskStage = TaskStage.COMPLETED,
    ) -> "TaskLifecycle":
        return self._with(
            TaskStatus(state=TaskStatusState.COMPLETED, result=result, message=message),
            stage,
        )
    def fail(
        self,
        message: str = "分析失败，请稍后重试",
        *,
        retryable: bool = False,
        stage: TaskStage = TaskStage.FAILED,
    ) -> "TaskLifecycle":
        if retryable:
            return self.retry(message)
        return self._with(
            TaskStatus.of(TaskStatusState.FAILED, message),
            stage,
        )
    failed = fail
```

### Why included
Shows zero-based attempt state, inclusive max boundary, retry capacity and all state transitions, including terminal and invalid-transition error branches.

## Target B
### File
`src/dovideo/application/task_lifecycle.py` (`TaskLifecycleEvent`); `src/dovideo/application/ports/tasks.py` (`TaskLifecyclePort`).

### Symbols
`TaskLifecycleEvent`, `state`, `stage`, `terminal`, `terminal_event`, `TaskLifecyclePort.load_lifecycle`, `TaskLifecyclePort.save_lifecycle`.

### Source

```python
@dataclass(frozen=True, slots=True)
class TaskLifecycleEvent:
    key: TaskKey
    event: TaskEvent
    attempt: int = 0
    retryable: bool = False
    def __post_init__(self) -> None:
        if not isinstance(self.key, TaskKey):
            raise TypeError("key must be a TaskKey")
        if not isinstance(self.event, TaskEvent):
            raise TypeError("event must be a TaskEvent")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int):
            raise TypeError("attempt must be an integer")
        if self.attempt < 0:
            raise ValueError("attempt must be non-negative")
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be a boolean")
    @property
    def state(self) -> TaskStatusState | None:
        return self.event.state
    @property
    def stage(self) -> TaskStage | None:
        return self.event.stage
    @property
    def terminal(self) -> bool:
        return self.event.terminal()
    def terminal_event(self) -> bool:
        return self.terminal
```

```python
class TaskLifecyclePort(Protocol):
    """Read/write lifecycle snapshots for a future worker adapter."""
    async def load_lifecycle(self, key: TaskKey) -> TaskLifecycle | None:
        ...
    async def save_lifecycle(self, lifecycle: TaskLifecycle) -> None:
        ...
```

### Why included
The envelope reuses the existing `TaskKey` and Java-shaped `TaskEvent`; the port exposes only an async, transport-neutral future worker seam.

## Target C
### File
`tests/application/test_task_lifecycle_9a.py`

### Symbols
All Phase 9A lifecycle tests: identity/GENERAL normalization, attempt 0→1..3, max boundary, retry/non-terminal, terminal success/failure, invalid transitions, event semantics, custom limits, and public port contract.

### Source

```python
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
```

### Why included
This is the complete targeted test file and records the actual Phase 9A identity, attempt, terminal, invalid-transition, and event assertions.

## Java Evidence
### File
`DOVideo-AI/server/src/main/java/com/example/server/consumer/VideoAnalysisConsumer.java`; `.../dto/TaskEvent.java`; `.../dto/TaskStatus.java`.

### Symbols
`MAX_DELIVERY_ATTEMPTS`, `onMessage` attempt increment/retry/dead-letter branch, `TaskEvent.terminal()`, and `TaskStatus.State`.

### Source

```java
private static final int MAX_DELIVERY_ATTEMPTS = 3;
Long currentAttempt = redisTemplate.opsForValue().increment(attemptsKey);
attempt = currentAttempt == null ? 1 : currentAttempt;
redisTemplate.expire(attemptsKey, ACTIVE_TTL);
taskEventService.publishAnalysis(mediaId, msg.getUserGoal(), mode,
        TaskStatus.of(TaskStatus.State.PROCESSING, "视频分析任务开始执行"),
        TaskStage.CONSUMING);
```

```java
boolean permanent = isPermanentFailure(e);
if (!permanent && acquired && attempt > 0 && attempt < MAX_DELIVERY_ATTEMPTS) {
    retrying = true;
    redisTemplate.expire(activeKey, ACTIVE_TTL);
    saveStage(mediaId, msg.getUserGoal(), mode, TaskStage.RETRYING);
    taskEventService.publishAnalysis(mediaId, msg.getUserGoal(), mode,
            TaskStatus.of(TaskStatus.State.PROCESSING, "本次执行失败，等待消息队列重试"),
            TaskStage.RETRYING);
    log.warn("video_analysis_retry_scheduled mediaId={} attempt={}", mediaId, attempt, e);
    throw new IllegalStateException("视频分析消费失败，交由 RocketMQ 重试", e);
}
if (acquired && (permanent || attempt >= MAX_DELIVERY_ATTEMPTS)) {
    rocketMQTemplate.convertAndSend(deadLetterTopic, msg);
    saveStage(mediaId, msg.getUserGoal(), mode, TaskStage.DEAD_LETTERED);
    taskEventService.publishAnalysis(mediaId, msg.getUserGoal(), mode,
            TaskStatus.of(TaskStatus.State.FAILED, "分析失败，已进入人工处理队列"),
            TaskStage.DEAD_LETTERED);
    return;
}
```

```java
public record TaskEvent(TaskStatus.State state, String result, String message, TaskStage stage) {
    public static TaskEvent of(TaskStatus status, TaskStage stage) {
        return new TaskEvent(status.state(), status.result(), status.message(), stage);
    }

    public boolean terminal() {
        return state == TaskStatus.State.COMPLETED || state == TaskStatus.State.FAILED;
    }
}
```

```java
public record TaskStatus(State state, String result, String message) {
    public enum State {
        NOT_STARTED,
        QUEUED,
        PROCESSING,
        COMPLETED,
        FAILED
    }
}
```

### Why included
These are the narrow Java facts used by the Phase 9A contract: inclusive attempts, retrying as non-terminal processing, and terminal event states.
