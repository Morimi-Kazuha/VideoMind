"""Task activity, dispatch, and event-publishing ports."""

from __future__ import annotations

from typing import Protocol

from dovideo.domain import (
    AgentState,
    ModeProfile,
    TaskEvent,
    TaskStage,
    TaskStatus,
    VideoContext,
)

from ..task_lifecycle import TaskLifecycle, TaskLifecycleEvent
from ..dead_letter_handoff import PendingDeadLetterHandoff
from ..value_objects import AnalysisRequest, DispatchDisposition, TaskKey


class TaskActivityPort(Protocol):
    """Observe whether a task's active lease/idempotency marker exists."""

    async def is_active(self, key: TaskKey) -> bool:
        ...


class TaskDispatchPort(Protocol):
    """Submit an analysis request to a future worker boundary."""

    async def dispatch(self, request: AnalysisRequest) -> DispatchDisposition:
        ...


class TaskTransportPort(Protocol):
    """Enqueue one JSON-safe request at the production transport boundary.

    The port intentionally has no Celery, RabbitMQ, retry-count, or broker
    acknowledgement vocabulary.  ``TaskWorker`` remains the owner of
    business attempts; a concrete transport only accepts the request for
    delivery.
    """

    async def enqueue(self, request: AnalysisRequest) -> None:
        ...


class TaskEventPublisherPort(Protocol):
    """Publish a domain event; transport (Redis/SSE/etc.) stays outside."""

    async def publish(self, key: TaskKey, event: TaskEvent) -> None:
        ...


class TaskStatusProjectionPort(Protocol):
    """Minimal synchronous read-side projection seam.

    Java exposes current status rather than an event-history repository, so
    this port contains only apply/current/stage operations and no persistence
    or replay-store methods.
    """

    def apply(self, event: TaskLifecycleEvent) -> TaskStatus:
        ...

    def current(self, key: TaskKey) -> TaskStatus:
        ...

    def stage(self, key: TaskKey) -> TaskStage | None:
        ...


StatusProjectionPort = TaskStatusProjectionPort

# Phase 9D names the two existing boundaries from the consumer perspective;
# aliases avoid introducing a second event or status contract.
TaskEventDeliveryPort = TaskEventPublisherPort
TaskStatusReadPort = TaskStatusProjectionPort


class TaskLifecyclePort(Protocol):
    """Read/write lifecycle snapshots for a future worker adapter.

    The Phase 9A contract intentionally has no queue, lock, Redis, or
    database methods.  Implementations belong to later worker/persistence
    slices and must use ``lifecycle.key`` as the existing task identity.
    """

    async def load_lifecycle(self, key: TaskKey) -> TaskLifecycle | None:
        ...

    async def save_lifecycle(self, lifecycle: TaskLifecycle) -> None:
        ...


class TaskActiveMarkerPort(Protocol):
    """Atomic active-marker/TTL seam; implementations stay provider-neutral."""

    async def reserve(self, key: TaskKey, *, ttl_seconds: float) -> bool:
        ...

    async def is_active(self, key: TaskKey) -> bool:
        ...

    async def refresh(self, key: TaskKey, *, ttl_seconds: float) -> None:
        ...

    async def release(self, key: TaskKey) -> None:
        ...


class TaskCompletionPort(Protocol):
    """Completed-marker seam used to make duplicate submissions harmless."""

    async def is_completed(self, key: TaskKey) -> bool:
        ...

    async def mark_completed(self, key: TaskKey, *, ttl_seconds: float) -> None:
        ...

    async def clear_completed(self, key: TaskKey) -> None:
        ...


class TaskLockPort(Protocol):
    """Owner-safe renewable per-task lock with an opaque token."""

    @property
    def lease_seconds(self) -> float | None:
        """Finite lease duration; None only for process-local nonexpiring locks."""
        ...

    async def acquire(self, key: TaskKey) -> object | None:
        ...

    async def release(self, key: TaskKey, token: object) -> None:
        ...

    async def refresh(self, key: TaskKey, token: object) -> bool:
        """True only when this owner atomically extended its own lease."""
        ...


class TaskDeadLetterPort(Protocol):
    """Dead-letter publication seam; no broker type leaks into application."""

    async def publish(
        self,
        request: AnalysisRequest,
        *,
        attempt: int,
        error: BaseException,
    ) -> None:
        ...


class TaskDeadLetterHandoffPort(Protocol):
    """Durable pending dead-letter handoff storage.

    The port is deliberately narrower than a checkpoint repository.  A
    persistence adapter may implement it using the existing Phase 8
    checkpoint namespace, while the worker remains unaware of its store or
    cache technology.
    """

    async def save_pending(self, handoff: PendingDeadLetterHandoff) -> None:
        ...

    async def load_pending(self, key: TaskKey) -> PendingDeadLetterHandoff | None:
        ...

    async def clear_pending(self, key: TaskKey) -> None:
        ...


class TaskResultPort(Protocol):
    """Minimal result checkpoint seam for worker recovery and persistence."""

    async def load_result(self, key: TaskKey) -> AgentState | None:
        ...

    async def save_result(self, key: TaskKey, state: AgentState) -> None:
        ...


class AgentLoopEntryPort(Protocol):
    """Existing AgentLoop entry point, kept free of queue/transport concerns."""

    async def run(
        self,
        context: VideoContext,
        media_id: int | None = None,
        profile: ModeProfile | None = None,
    ) -> AgentState:
        ...


__all__ = [
    "TaskActivityPort",
    "AgentLoopEntryPort",
    "TaskActiveMarkerPort",
    "TaskCompletionPort",
    "TaskDeadLetterPort",
    "TaskDeadLetterHandoffPort",
    "TaskDispatchPort",
    "TaskTransportPort",
    "TaskEventPublisherPort",
    "TaskEventDeliveryPort",
    "TaskStatusProjectionPort",
    "StatusProjectionPort",
    "TaskStatusReadPort",
    "TaskLifecyclePort",
    "TaskLockPort",
    "TaskResultPort",
]
