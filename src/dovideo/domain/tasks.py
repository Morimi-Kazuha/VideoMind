"""Task status, stage, and event contracts used by progress streams."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import field_validator, model_validator

from ._base import DomainModel


class TaskStatusState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class TaskStatus(DomainModel):
    """Current task state and either a result or progress message."""

    state: TaskStatusState | None = None
    result: str | None = None
    message: str | None = None

    @classmethod
    def of(cls, state: TaskStatusState | None, message: str | None) -> "TaskStatus":
        return cls(state=state, result=None, message=message)

    @classmethod
    def completed(cls, result: str | None | Any) -> "TaskStatus":
        """Create a completed status from markdown or an :class:`AgentState`.

        Java overloads this method for ``String`` and ``AgentState``.  Runtime
        dispatch here preserves both call forms while keeping this domain
        layer free of any orchestration behavior.
        """

        # Local import avoids a module cycle (AgentState uses AnalysisResult,
        # while this convenience constructor only needs it at call time).
        from .agent import AgentState

        if isinstance(result, AgentState):
            if result.result is None:
                raise ValueError("agent state result is required")
            markdown = result.result.to_markdown()
            if result.critique is not None and result.critique.passed:
                return cls(
                    state=TaskStatusState.COMPLETED,
                    result=markdown,
                    message="任务完成",
                )
            warning = "分析已完成，但部分结论未通过 Critic 校验，请结合时间戳证据人工核验。"
            return cls(
                state=TaskStatusState.COMPLETED,
                result=f"> **结果提示：** {warning}\n\n{markdown}",
                message=warning,
            )
        return cls(state=TaskStatusState.COMPLETED, result=result, message="任务完成")


class TaskStage(str, Enum):
    QUEUED = "QUEUED"
    CONSUMING = "CONSUMING"
    VIDEO_CONTEXT = "VIDEO_CONTEXT"
    CONTEXT_COMPLETED = "CONTEXT_COMPLETED"
    CHUNKS_COMPLETED = "CHUNKS_COMPLETED"
    RETRIEVAL = "RETRIEVAL"
    AGENT_LOOP = "AGENT_LOOP"
    PLAN_COMPLETED = "PLAN_COMPLETED"
    EXECUTOR_STARTED = "EXECUTOR_STARTED"
    EXECUTOR_COMPLETED = "EXECUTOR_COMPLETED"
    CRITIC_STARTED = "CRITIC_STARTED"
    CRITIC_PASSED = "CRITIC_PASSED"
    CRITIC_RETRY_REQUIRED = "CRITIC_RETRY_REQUIRED"
    EVIDENCE_REFRESHED = "EVIDENCE_REFRESHED"
    ANALYSIS_COMPLETED = "ANALYSIS_COMPLETED"
    ANALYSIS_COMPLETED_WITH_WARNINGS = "ANALYSIS_COMPLETED_WITH_WARNINGS"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    COMPLETED_REUSED = "COMPLETED_REUSED"
    FAILED = "FAILED"
    DEAD_LETTERED = "DEAD_LETTERED"
    MANUAL_REPLAY = "MANUAL_REPLAY"
    REVISION_PENDING = "REVISION_PENDING"
    REVISION_APPLIED = "REVISION_APPLIED"
    TRANSCRIPTION = "TRANSCRIPTION"
    ASR = "ASR"
    DISPATCH_FAILED = "DISPATCH_FAILED"

    @classmethod
    def from_value(cls, value: str | None) -> "TaskStage | None":
        """Java ``TaskStage.from`` behavior: blank/unknown values yield null.

        The Java implementation does not trim before ``valueOf``; this method
        intentionally keeps that detail for replaying old event payloads.
        """

        if value is None or not value.strip():
            return None
        try:
            return cls(value)
        except ValueError:
            return None

    parse = from_value
    from_ = from_value


class TaskEvent(DomainModel):
    """Progress event sent to SSE/Redis consumers."""

    state: TaskStatusState | None = None
    result: str | None = None
    message: str | None = None
    stage: TaskStage | None = None

    @classmethod
    def of(cls, status: TaskStatus, stage: TaskStage | None) -> "TaskEvent":
        return cls(
            state=status.state,
            result=status.result,
            message=status.message,
            stage=stage,
        )

    def terminal(self) -> bool:
        return self.state in (TaskStatusState.COMPLETED, TaskStatusState.FAILED)


# Java nests State under TaskStatus.  Keep the spelling for adapters while
# exporting the explicit Python enum too.
TaskStatus.State = TaskStatusState  # type: ignore[attr-defined]


__all__ = ["TaskEvent", "TaskStage", "TaskStatus", "TaskStatusState"]

