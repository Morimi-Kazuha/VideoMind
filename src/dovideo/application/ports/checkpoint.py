"""Checkpoint reader/writer boundaries split by lifecycle."""

from __future__ import annotations

from typing import Protocol

from dovideo.domain import (
    AgentPlan,
    AgentState,
    TaskStage,
    VideoChunk,
    VideoContext,
)

from ..value_objects import TaskKey
from ..tool_state import DurableToolStateLedger
from ..model_routing import ModelRoutingDecision


class AnalysisStatusCheckpointPort(Protocol):
    """Minimal reader required by the status query use case."""

    async def load_result(self, key: TaskKey) -> AgentState | None:
        ...

    async def load_stage(self, key: TaskKey) -> TaskStage | None:
        ...


class ContextCheckpointPort(Protocol):
    """Content-level context/chunk checkpoint operations."""

    async def load_context(self, media_id: int) -> VideoContext | None:
        ...

    async def save_context(self, media_id: int, context: VideoContext) -> None:
        ...

    async def load_chunks(self, media_id: int) -> tuple[VideoChunk, ...] | None:
        ...

    async def save_chunks(self, media_id: int, chunks: tuple[VideoChunk, ...]) -> None:
        ...


class AgentCheckpointPort(Protocol):
    """Goal-level plan/draft/Critic checkpoint operations."""

    async def load_plan(self, key: TaskKey) -> AgentPlan | None:
        ...

    async def load_critic_state(self, key: TaskKey) -> AgentState | None:
        ...

    async def save_plan(self, key: TaskKey, plan: AgentPlan) -> None:
        ...

    async def save_execution_state(self, key: TaskKey, state: AgentState) -> None:
        ...

    async def save_critic_state(self, key: TaskKey, state: AgentState) -> None:
        ...

    async def save_result(self, key: TaskKey, state: AgentState) -> None:
        ...


class ToolCallCheckpointPort(Protocol):
    """Narrow durable ledger boundary for X1-D1 tool-call recovery.

    The ledger is intentionally separate from the frozen plan/draft/critic
    DTOs.  Implementations should persist it through the existing checkpoint
    repository namespace and treat the durable record as the recovery source
    of truth.
    """

    async def load_tool_state(self, key: TaskKey) -> DurableToolStateLedger | None:
        ...

    async def save_tool_state(
        self,
        key: TaskKey,
        state: DurableToolStateLedger,
    ) -> None:
        ...


class ModelRoutingCheckpointPort(Protocol):
    """Narrow recovery boundary for one stable J1 routing decision.

    The route is stored in the existing checkpoint repository's independent
    namespace. It is deliberately not an X2 execution event or a field on an
    execution record; J1-C owns historical route recording.
    """

    async def load_model_routing(
        self,
        key: TaskKey,
    ) -> ModelRoutingDecision | None:
        ...

    async def save_model_routing(
        self,
        key: TaskKey,
        decision: ModelRoutingDecision,
    ) -> None:
        ...


__all__ = [
    "AgentCheckpointPort",
    "AnalysisStatusCheckpointPort",
    "ContextCheckpointPort",
    "ModelRoutingCheckpointPort",
    "ToolCallCheckpointPort",
]
