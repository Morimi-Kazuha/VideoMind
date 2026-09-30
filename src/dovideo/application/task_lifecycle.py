"""Phase 9A task identity and lifecycle contracts.

This module is deliberately a pure application contract.  It does not claim
to be a worker, scheduler, queue consumer, lock implementation, or retry
executor.  ``TaskKey`` remains the one source of task identity; the lifecycle
only records the status/stage and delivery-attempt facts that a future worker
will persist and publish.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from dovideo.domain import TaskEvent, TaskStage, TaskStatus, TaskStatusState

from .value_objects import TaskKey


DEFAULT_MAX_ATTEMPTS = 3


class TaskLifecycleError(ValueError):
    """Raised when a lifecycle transition cannot be represented safely."""


@dataclass(frozen=True, slots=True)
class TaskAttempt:
    """Immutable delivery-attempt counter matching the Java consumer policy.

    The counter is zero before the first delivery.  A worker increments it
    when it begins a delivery, so the first actual attempt is ``1`` and the
    default maximum of three is inclusive.
    """

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
        """Whether another delivery slot remains after this attempt."""

        return self.number < self.max_attempts

    @property
    def retryable(self) -> bool:
        """Java-compatible alias for the remaining-attempt decision."""

        return self.can_retry

    def next(self) -> "TaskAttempt":
        if not self.can_retry:
            raise TaskLifecycleError("maximum delivery attempts exhausted")
        return replace(self, number=self.number + 1)


@dataclass(frozen=True, slots=True)
class TaskLifecycleEvent:
    """Task-keyed envelope around the existing Java-compatible ``TaskEvent``.

    The transport-neutral ``TaskEvent`` remains unchanged.  This envelope
    adds the identity and attempt metadata a future worker/event adapter needs,
    without introducing a second key or a Redis/SSE dependency.
    """

    key: TaskKey
    event: TaskEvent
    attempt: int = 0
    retryable: bool = False
    request_id: str | None = None

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
        """Use the Java ``TaskEvent.terminal()`` definition exactly."""

        return self.event.terminal()

    def terminal_event(self) -> bool:
        """Method spelling convenient for adapters ported from Java."""

        return self.terminal


@dataclass(frozen=True, slots=True)
class TaskLifecycle:
    """Pure state machine for one existing :class:`~.value_objects.TaskKey`.

    ``attempt`` is the number of deliveries already started.  A retryable
    failure is represented by ``PROCESSING`` plus ``RETRYING`` and therefore
    remains non-terminal, just as ``VideoAnalysisConsumer`` publishes before
    asking RocketMQ to redeliver.  A terminal failure uses ``FAILED`` and a
    terminal success uses ``COMPLETED``; this follows ``TaskEvent.terminal``.
    """

    key: TaskKey
    status: TaskStatus
    stage: TaskStage | None = None
    attempt: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    retryable: bool = False
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, TaskKey):
            raise TypeError("key must be a TaskKey")
        if not isinstance(self.status, TaskStatus):
            raise TypeError("status must be a TaskStatus")
        if self.stage is not None and not isinstance(self.stage, TaskStage):
            raise TypeError("stage must be a TaskStage or None")
        # Reuse one validated counter rather than duplicating attempt policy.
        TaskAttempt(self.attempt, self.max_attempts)
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be a boolean")
        if self.request_id is not None and (
            not isinstance(self.request_id, str)
            or not self.request_id.strip()
            or len(self.request_id) > 128
        ):
            raise ValueError("request_id must be nonblank text no longer than 128 characters")

    @classmethod
    def new(
        cls,
        key: TaskKey,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        request_id: str | None = None,
    ) -> "TaskLifecycle":
        """Create the not-started lifecycle for an existing task identity."""

        return cls(
            key=key,
            status=TaskStatus.of(TaskStatusState.NOT_STARTED, "尚未提交分析任务"),
            max_attempts=max_attempts,
            request_id=request_id,
        )

    @property
    def identity(self) -> TaskKey:
        """Return the shared ``TaskKey``; no alternate identity is created."""

        return self.key

    @property
    def state(self) -> TaskStatusState | None:
        return self.status.state

    @property
    def terminal(self) -> bool:
        """Exactly mirror Java ``TaskEvent.terminal()``."""

        return self.event.terminal()

    @property
    def can_retry(self) -> bool:
        """Whether a future delivery may still be scheduled."""

        return not self.terminal and TaskAttempt(self.attempt, self.max_attempts).can_retry

    @property
    def event(self) -> TaskEvent:
        return TaskEvent.of(self.status, self.stage)

    @property
    def lifecycle_event(self) -> TaskLifecycleEvent:
        return TaskLifecycleEvent(
            key=self.key,
            event=self.event,
            attempt=self.attempt,
            retryable=self.retryable,
            request_id=self.request_id,
        )

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

    def queued(self, message: str = "任务已进入异步分析队列") -> "TaskLifecycle":
        """Record dispatch acceptance without starting a delivery attempt."""

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
        """Increment the inclusive delivery counter and enter PROCESSING."""

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
        """Change an in-flight stage without changing the delivery count."""

        if self.terminal:
            raise TaskLifecycleError("terminal task cannot become processing")
        return self._with(TaskStatus.of(TaskStatusState.PROCESSING, message), stage)

    def retry(self, message: str = "本次执行失败，等待消息队列重试") -> "TaskLifecycle":
        """Publish the non-terminal retrying state before redelivery."""

        if not self.can_retry:
            raise TaskLifecycleError("task has no retryable delivery remaining")
        return self._with(
            TaskStatus.of(TaskStatusState.PROCESSING, message),
            TaskStage.RETRYING,
            retryable=True,
        )

    # Explicit adapter-friendly alias: the Java consumer calls this branch a
    # retry scheduled state rather than a terminal failure.
    retry_required = retry

    def complete(
        self,
        result: str | None | Any = None,
        message: str = "任务完成",
        *,
        stage: TaskStage = TaskStage.COMPLETED,
    ) -> "TaskLifecycle":
        """Record terminal COMPLETED status and stop future retries."""

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
        """Record a final FAILED state or a retrying non-terminal state."""

        if retryable:
            return self.retry(message)
        return self._with(
            TaskStatus.of(TaskStatusState.FAILED, message),
            stage,
        )

    # Common naming used by application adapters.
    failed = fail


# A descriptive alias helps callers that use the Java phrase “analysis task”
# while preserving one implementation and one TaskKey identity.
AnalysisTaskLifecycle = TaskLifecycle


__all__ = [
    "AnalysisTaskLifecycle",
    "DEFAULT_MAX_ATTEMPTS",
    "TaskAttempt",
    "TaskLifecycle",
    "TaskLifecycleError",
    "TaskLifecycleEvent",
]
