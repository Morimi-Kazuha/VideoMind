"""Analysis status query use case."""

from __future__ import annotations

from dovideo.domain import AgentState, AnalysisMode, TaskStage, TaskStatus, TaskStatusState

from .ports.checkpoint import AnalysisStatusCheckpointPort
from .ports.tasks import TaskActivityPort
from .value_objects import TaskKey


class AnalysisStatusQuery:
    """Read status from checkpoint/activity ports with Java-compatible precedence.

    The use case intentionally does not know how active leases or checkpoints
    are stored.  It only combines their observations:

    ``terminal result`` → ``active (queued/processing)`` → ``inactive failure``
    → ``not started``.
    """

    def __init__(
        self,
        checkpoint: AnalysisStatusCheckpointPort,
        activity: TaskActivityPort,
    ) -> None:
        self._checkpoint = checkpoint
        self._activity = activity

    async def current(
        self,
        media_id: int,
        goal: str,
        mode: AnalysisMode | str | None = AnalysisMode.GENERAL,
    ) -> TaskStatus:
        """Return the current status for one media/goal/mode task."""

        key = TaskKey(media_id=media_id, goal=goal, mode=_resolve_mode(mode))

        # A newly queued revision owns the current logical status while its
        # predecessor remains readable until a worker actually applies it.
        load_lifecycle = getattr(self._checkpoint, "load_lifecycle", None)
        lifecycle = await load_lifecycle(key) if callable(load_lifecycle) else None
        if lifecycle is not None and (lifecycle.request_id or "").startswith("revision:"):
            if not lifecycle.terminal:
                if await self._activity.is_active(key):
                    return lifecycle.status
                return TaskStatus.of(TaskStatusState.FAILED, "修订任务已中断，可以重新提交")

        # A completed checkpoint wins even if the active marker has not yet
        # expired.  This is the first branch in the Java implementation.
        result = await self._checkpoint.load_result(key)
        if result is not None and result.result is not None:
            return TaskStatus.completed(result)

        stage = await self._checkpoint.load_stage(key)
        if await self._activity.is_active(key):
            state = TaskStatusState.QUEUED if stage is None else TaskStatusState.PROCESSING
            return TaskStatus.of(state, self.status_message(stage))

        if stage is TaskStage.BUDGET_EXHAUSTED:
            return TaskStatus.of(
                TaskStatusState.FAILED,
                "Agent 已达到本次任务预算，请调整目标后重试",
            )
        if stage in (TaskStage.FAILED, TaskStage.DEAD_LETTERED):
            return TaskStatus.of(TaskStatusState.FAILED, "分析失败，请稍后重试")
        return TaskStatus.of(TaskStatusState.NOT_STARTED, "尚未提交分析任务")

    async def stage(
        self,
        media_id: int,
        goal: str,
        mode: AnalysisMode | str | None = AnalysisMode.GENERAL,
    ) -> TaskStage | None:
        """Return the persisted stage, using GENERAL when mode is omitted."""

        key = TaskKey(media_id=media_id, goal=goal, mode=_resolve_mode(mode))
        return await self._checkpoint.load_stage(key)

    @staticmethod
    def status_message(stage: TaskStage | None) -> str:
        """Map a stage to the Java ``AnalysisStatusService`` message."""

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
        return "正在分析视频"

    # A direct alias is convenient for ports/adapters that name the operation
    # after the Java private helper while keeping the public API explicit.
    message_for_stage = status_message


def _resolve_mode(mode: AnalysisMode | str | None) -> AnalysisMode:
    if isinstance(mode, AnalysisMode):
        return mode
    if mode is None:
        return AnalysisMode.GENERAL
    if isinstance(mode, str):
        return AnalysisMode.from_nullable(mode)
    raise TypeError("mode must be an AnalysisMode, string, or None")


# A migration alias for callers that still use the Java service name.  The
# implementation remains a pure application use case and has no Spring-like
# service dependency.
AnalysisStatusService = AnalysisStatusQuery


__all__ = ["AnalysisStatusQuery", "AnalysisStatusService"]

