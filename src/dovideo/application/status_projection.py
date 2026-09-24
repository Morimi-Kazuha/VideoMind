"""Small read-side projection for task status events.

The Java ``AnalysisStatusService`` reads the current result/stage and active
marker; it does not persist or replay an event history.  This module is the
Python-side transport-neutral bridge for consumers that already receive the
existing ``TaskLifecycleEvent`` envelope.  It keeps only an in-memory current
snapshot and deliberately makes no exactly-once or durable-history claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from dovideo.domain import TaskEvent, TaskStage, TaskStatus, TaskStatusState

from .task_lifecycle import TaskLifecycleEvent
from .value_objects import TaskKey


_TERMINAL_COMPLETED_STAGES = frozenset(
    {
        TaskStage.COMPLETED,
        TaskStage.COMPLETED_REUSED,
        TaskStage.ANALYSIS_COMPLETED,
        TaskStage.ANALYSIS_COMPLETED_WITH_WARNINGS,
    }
)
_TERMINAL_FAILED_STAGES = frozenset(
    {TaskStage.FAILED, TaskStage.DEAD_LETTERED}
)

# This is only a same-attempt stale-update guard.  There is no timestamp or
# event id in the Java/Python envelope, so it is intentionally not presented
# as a distributed ordering or exactly-once algorithm.
_STAGE_ORDER: dict[TaskStage | None, int] = {
    None: 0,
    TaskStage.QUEUED: 1,
    TaskStage.CONSUMING: 2,
    TaskStage.VIDEO_CONTEXT: 3,
    TaskStage.CONTEXT_COMPLETED: 4,
    TaskStage.CHUNKS_COMPLETED: 5,
    TaskStage.RETRIEVAL: 6,
    TaskStage.AGENT_LOOP: 7,
    TaskStage.PLAN_COMPLETED: 8,
    TaskStage.EXECUTOR_STARTED: 9,
    TaskStage.EXECUTOR_COMPLETED: 10,
    TaskStage.CRITIC_STARTED: 11,
    TaskStage.CRITIC_RETRY_REQUIRED: 12,
    TaskStage.EVIDENCE_REFRESHED: 13,
    TaskStage.RETRYING: 50,
    TaskStage.CRITIC_PASSED: 14,
    TaskStage.ANALYSIS_COMPLETED: 90,
    TaskStage.ANALYSIS_COMPLETED_WITH_WARNINGS: 90,
    TaskStage.COMPLETED: 90,
    TaskStage.COMPLETED_REUSED: 90,
    TaskStage.BUDGET_EXHAUSTED: 90,
    TaskStage.FAILED: 90,
    TaskStage.DEAD_LETTERED: 90,
}


@dataclass(frozen=True, slots=True)
class StatusProjectionSnapshot:
    """Current projected status plus the existing lifecycle metadata."""

    key: TaskKey
    status: TaskStatus
    stage: TaskStage | None = None
    attempt: int = 0
    retryable: bool = False

    @property
    def terminal(self) -> bool:
        return self.status.state in (
            TaskStatusState.COMPLETED,
            TaskStatusState.FAILED,
        )


class TaskStatusProjection:
    """Project existing task events into one current status per ``TaskKey``.

    The class is intentionally synchronous and in-memory.  It is suitable
    for an adapter or subscriber, while durable status remains the existing
    checkpoint/active-marker boundary used by ``AnalysisStatusQuery``.
    """

    def __init__(self) -> None:
        self._snapshots: dict[TaskKey, StatusProjectionSnapshot] = {}

    def current(self, key: TaskKey) -> TaskStatus:
        """Return the current status, using Java's NOT_STARTED default."""

        return self.snapshot(key).status

    current_status = current
    status = current

    def stage(self, key: TaskKey) -> TaskStage | None:
        """Return the last projected stage, or ``None`` before any event."""

        return self.snapshot(key).stage

    def snapshot(self, key: TaskKey) -> StatusProjectionSnapshot:
        key = _require_key(key)
        return self._snapshots.get(key, _not_started(key))

    def apply(self, event: TaskLifecycleEvent) -> TaskStatus:
        """Apply one event with duplicate and stale-transition guards."""

        if not isinstance(event, TaskLifecycleEvent):
            raise TypeError("event must be a TaskLifecycleEvent")
        key = event.key
        previous = self.snapshot(key)
        incoming_stage = event.stage
        incoming_status = _status_from_event(event.event)

        # The first terminal snapshot wins.  This keeps a late processing or
        # conflicting terminal event from downgrading/changing a completed
        # task when the transport provides no event id or timestamp.
        if previous.terminal:
            return previous.status

        incoming_terminal = incoming_status.state in (
            TaskStatusState.COMPLETED,
            TaskStatusState.FAILED,
        )
        if not incoming_terminal and event.attempt < previous.attempt:
            return previous.status

        if not incoming_terminal and event.attempt == previous.attempt:
            old_order = _STAGE_ORDER.get(previous.stage, 0)
            new_order = _STAGE_ORDER.get(incoming_stage, 0)
            # RETRYING -> the next PROCESSING event is a legal redelivery
            # transition even when an adapter carries the same attempt value.
            retry_to_processing = (
                previous.stage is TaskStage.RETRYING
                and incoming_status.state is TaskStatusState.PROCESSING
                and incoming_stage is not TaskStage.RETRYING
            )
            if new_order < old_order and not retry_to_processing:
                return previous.status

        snapshot = StatusProjectionSnapshot(
            key=key,
            status=incoming_status,
            stage=incoming_stage,
            attempt=event.attempt,
            retryable=(
                False
                if incoming_terminal
                else event.retryable or incoming_stage is TaskStage.RETRYING
            ),
        )
        self._snapshots[key] = snapshot
        return snapshot.status

    apply_event = apply
    project = apply

    def replay(
        self,
        events: Iterable[TaskLifecycleEvent],
    ) -> dict[TaskKey, TaskStatus]:
        """Apply caller-supplied events and return projected statuses.

        This is a convenience for deterministic offline replay only.  Events
        are not retained or persisted by this class.
        """

        for event in events:
            self.apply(event)
        return {key: snapshot.status for key, snapshot in self._snapshots.items()}

    def replay_one(
        self,
        key: TaskKey,
        events: Iterable[TaskLifecycleEvent],
    ) -> TaskStatus:
        """Replay events and return one selected task's current status."""

        key = _require_key(key)
        self.replay(events)
        return self.current(key)

    def keys(self) -> tuple[TaskKey, ...]:
        """Return known identities as an immutable in-memory view."""

        return tuple(self._snapshots)


StatusProjection = TaskStatusProjection
TaskEventProjection = TaskStatusProjection


def _require_key(key: TaskKey) -> TaskKey:
    if not isinstance(key, TaskKey):
        raise TypeError("key must be a TaskKey")
    return key


def _not_started(key: TaskKey) -> StatusProjectionSnapshot:
    return StatusProjectionSnapshot(
        key=key,
        status=TaskStatus.of(TaskStatusState.NOT_STARTED, "尚未提交分析任务"),
    )


def _status_from_event(event: TaskEvent) -> TaskStatus:
    """Normalize event stage/state to the Java status-state vocabulary."""

    stage = event.stage
    state = event.state
    if state is TaskStatusState.COMPLETED or stage in _TERMINAL_COMPLETED_STAGES:
        return TaskStatus(
            state=TaskStatusState.COMPLETED,
            result=event.result,
            message=event.message or "任务完成",
        )
    if state is TaskStatusState.FAILED or stage in _TERMINAL_FAILED_STAGES:
        return TaskStatus.of(
            TaskStatusState.FAILED,
            event.message or _message_for_stage(stage),
        )
    if state is TaskStatusState.QUEUED or stage is TaskStage.QUEUED:
        return TaskStatus.of(
            TaskStatusState.QUEUED,
            event.message or _message_for_stage(TaskStage.QUEUED),
        )
    if state is TaskStatusState.PROCESSING or stage is not None:
        return TaskStatus.of(
            TaskStatusState.PROCESSING,
            event.message or _message_for_stage(stage),
        )
    return TaskStatus.of(TaskStatusState.NOT_STARTED, "尚未提交分析任务")


def _message_for_stage(stage: TaskStage | None) -> str:
    """Copy the Java ``AnalysisStatusService.statusMessage`` mapping."""

    if stage is None or stage is TaskStage.QUEUED:
        return "任务已排队"
    if stage in (TaskStage.VIDEO_CONTEXT, TaskStage.CONTEXT_COMPLETED):
        return "正在解析视频语音和关键画面"
    if stage is TaskStage.CHUNKS_COMPLETED:
        return "正在检索与目标相关的视频证据"
    if stage is TaskStage.PLAN_COMPLETED:
        return "Planner 已完成任务拆解"
    if stage in (TaskStage.EXECUTOR_STARTED, TaskStage.EXECUTOR_COMPLETED):
        return "Executor 正在生成结构化产物"
    if stage is TaskStage.CRITIC_STARTED:
        return "Critic 正在核验结论和证据"
    if stage in (TaskStage.CRITIC_RETRY_REQUIRED, TaskStage.EVIDENCE_REFRESHED):
        return "正在根据 Critic 反馈补充证据"
    if stage is TaskStage.RETRYING:
        return "任务执行异常，正在自动重试"
    if stage is TaskStage.BUDGET_EXHAUSTED:
        return "Agent 已达到本次任务预算，请调整目标后重试"
    if stage in _TERMINAL_FAILED_STAGES:
        return "分析失败，请稍后重试"
    return "正在分析视频"


__all__ = [
    "StatusProjection",
    "StatusProjectionSnapshot",
    "TaskEventProjection",
    "TaskStatusProjection",
]
