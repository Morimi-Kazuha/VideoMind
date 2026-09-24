"""Application-facing event delivery and status read boundary.

Phase 9B already owns the transport-neutral ``TaskEventPublisherPort`` used
by dispatch and the worker.  Phase 9C owns the in-memory current-status
projection.  This module composes those two existing contracts so a future
transport or presentation adapter can read status and observe events without
knowing the worker or AgentLoop implementation.

The projection is updated before the optional publisher is called.  A
publisher failure is therefore best-effort notification failure: it is
reported in the returned outcome but cannot roll back a completed/failed
status or trigger analysis again.  No event history is retained here.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

from dovideo.domain import TaskEvent, TaskStage, TaskStatus

from .ports.tasks import TaskEventPublisherPort, TaskStatusProjectionPort
from .status_projection import StatusProjectionSnapshot, TaskStatusProjection
from .task_lifecycle import TaskLifecycleEvent
from .value_objects import TaskKey


@dataclass(frozen=True, slots=True)
class EventDeliveryResult:
    """Result of projecting one event and attempting notification delivery.

    ``delivered`` describes only the optional notification publisher.  The
    ``status`` and ``stage`` fields always reflect the projection after its
    duplicate/stale/terminal guards have run.  No exception object is carried
    in this value, keeping provider details out of the application boundary.
    """

    event: TaskLifecycleEvent
    status: TaskStatus
    stage: TaskStage | None
    delivered: bool

    @property
    def delivery_succeeded(self) -> bool:
        """Descriptive alias for adapters that prefer an explicit name."""

        return self.delivered

    @property
    def subscriber_failed(self) -> bool:
        """Whether the optional notification sink was unavailable."""

        return not self.delivered


class TaskEventDeliveryService:
    """Compose existing event publication with the existing status projection.

    ``publisher`` is the already-defined ``TaskEventPublisherPort``.  It may
    be omitted when a caller only needs a local read-side projection.  Reads
    are synchronous like :class:`TaskStatusProjection`; delivery is async like
    the existing worker/dispatch publisher port.
    """

    def __init__(
        self,
        projection: TaskStatusProjectionPort | None = None,
        publisher: TaskEventPublisherPort | None = None,
        *,
        events: TaskEventPublisherPort | None = None,
    ) -> None:
        if publisher is not None and events is not None:
            raise TypeError("publisher and events are mutually exclusive")
        self._projection: TaskStatusProjectionPort = (
            projection if projection is not None else TaskStatusProjection()
        )
        self._publisher = publisher if publisher is not None else events

    @property
    def projection(self) -> TaskStatusProjectionPort:
        """Expose the injected projection without exposing worker internals."""

        return self._projection

    async def deliver(self, event: TaskLifecycleEvent) -> EventDeliveryResult:
        """Project and best-effort publish one existing lifecycle envelope.

        ``CancelledError`` is deliberately not swallowed: cancellation is a
        caller/control-flow signal, not a notification outage.  Ordinary
        publisher exceptions only make ``delivered`` false; the projected
        status remains authoritative for this in-process read side.
        """

        if not isinstance(event, TaskLifecycleEvent):
            raise TypeError("event must be a TaskLifecycleEvent")
        status = self._projection.apply(event)
        delivered = self._publisher is None
        if self._publisher is not None:
            try:
                published = self._publisher.publish(event.key, event.event)
                if inspect.isawaitable(published):
                    await published
                delivered = True
            except asyncio.CancelledError:
                raise
            except Exception:
                # Notification is intentionally best effort.  In particular,
                # do not undo a terminal projection or re-enter AgentLoop.
                delivered = False
        return EventDeliveryResult(
            event=event,
            status=status,
            stage=self._projection.stage(event.key),
            delivered=delivered,
        )

    async def publish(
        self,
        key: TaskKey,
        event: TaskEvent,
        *,
        attempt: int | None = None,
        retryable: bool | None = None,
    ) -> EventDeliveryResult:
        """Implement the existing publisher-port shape and retain metadata.

        Phase 9B publishers provide ``TaskKey`` plus ``TaskEvent`` and do not
        carry attempt metadata.  When metadata is omitted, this adapter uses
        the current projected attempt rather than inventing a token or a new
        lifecycle transition.  Callers that already have a full
        ``TaskLifecycleEvent`` should use :meth:`deliver` to preserve it.
        """

        if not isinstance(key, TaskKey):
            raise TypeError("key must be a TaskKey")
        if not isinstance(event, TaskEvent):
            raise TypeError("event must be a TaskEvent")
        if attempt is None:
            attempt = self._projection.snapshot(key).attempt
        if retryable is None:
            retryable = event.stage is TaskStage.RETRYING
        envelope = TaskLifecycleEvent(
            key=key,
            event=event,
            attempt=attempt,
            retryable=retryable,
        )
        return await self.deliver(envelope)

    async def publish_event(
        self,
        event: TaskLifecycleEvent,
    ) -> EventDeliveryResult:
        """Alias for adapters that name the operation after the envelope."""

        return await self.deliver(event)

    def observe(self, event: TaskLifecycleEvent) -> TaskStatus:
        """Apply an already received event without a notification sink."""

        if not isinstance(event, TaskLifecycleEvent):
            raise TypeError("event must be a TaskLifecycleEvent")
        return self._projection.apply(event)

    def current(self, key: TaskKey) -> TaskStatus:
        """Return current status for a task identity (NOT_STARTED by default)."""

        return self._projection.current(key)

    current_status = current
    status = current

    def stage(self, key: TaskKey) -> TaskStage | None:
        """Return the current projected stage for a task identity."""

        return self._projection.stage(key)

    def snapshot(self, key: TaskKey) -> StatusProjectionSnapshot:
        """Return the guarded current snapshot for a task identity."""

        snapshot = getattr(self._projection, "snapshot", None)
        if snapshot is None:
            # The public projection port intentionally contains only current
            # status/stage.  This fallback keeps custom projections usable.
            status = self._projection.current(key)
            return StatusProjectionSnapshot(key=key, status=status, stage=self.stage(key))
        return snapshot(key)

    def replay(self, events: Any) -> dict[TaskKey, TaskStatus]:
        """Delegate caller-supplied replay without retaining event history."""

        replay = getattr(self._projection, "replay", None)
        if replay is None:
            for event in events:
                self.observe(event)
            return {}
        return replay(events)


# Descriptive aliases keep the boundary discoverable without introducing a
# second delivery service or a second status state machine.
TaskStatusDeliveryBoundary = TaskEventDeliveryService
StatusEventDeliveryService = TaskEventDeliveryService
EventDeliveryBoundary = TaskEventDeliveryService


__all__ = [
    "EventDeliveryBoundary",
    "EventDeliveryResult",
    "StatusEventDeliveryService",
    "TaskEventDeliveryService",
    "TaskStatusDeliveryBoundary",
]
