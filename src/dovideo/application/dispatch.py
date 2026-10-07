"""Transport-neutral analysis dispatch orchestration for Phase 9B.

The service mirrors the Java submission boundary while keeping active-marker,
event, and lifecycle storage behind application ports. Request admission is
owned by the API User/Global Token Bucket. The dispatcher does not
know about Redis, RocketMQ, HTTP, or any concrete queue implementation.
"""

from __future__ import annotations

import asyncio

from dovideo.domain import TaskEvent, TaskStage

from .task_lifecycle import TaskLifecycle
from .value_objects import AnalysisRequest, DispatchDisposition
from .ports.tasks import (
    TaskActiveMarkerPort,
    TaskCompletionPort,
    TaskEventPublisherPort,
    TaskLifecyclePort,
    TaskTransportPort,
)


ACTIVE_TTL_SECONDS = 6 * 60 * 60


class TaskDispatchService:
    """Reserve one task identity and publish its queue-neutral QUEUED event."""

    def __init__(
        self,
        active: TaskActiveMarkerPort,
        *,
        completion: TaskCompletionPort | None = None,
        lifecycle: TaskLifecyclePort | None = None,
        events: TaskEventPublisherPort | None = None,
        transport: TaskTransportPort | None = None,
        active_ttl_seconds: float = ACTIVE_TTL_SECONDS,
    ) -> None:
        self._active = active
        self._completion = completion
        self._lifecycle = lifecycle
        self._events = events
        self._transport = transport
        self._active_ttl_seconds = active_ttl_seconds

    async def dispatch(self, request: AnalysisRequest, *, revision_plan=None, revision_checkpoint=None) -> DispatchDisposition:
        """Return ACCEPTED/DUPLICATE/FAILED; RATE_LIMITED is legacy/reserved."""

        if not isinstance(request, AnalysisRequest):
            return DispatchDisposition.FAILED
        key = request.task_key
        reserved = False
        try:
            if revision_checkpoint is None and self._completion is not None and await self._completion.is_completed(key):
                return DispatchDisposition.DUPLICATE
            reserved = await self._active.reserve(
                key,
                ttl_seconds=self._active_ttl_seconds,
            )
            if not reserved:
                return DispatchDisposition.DUPLICATE
            if revision_checkpoint is not None:
                await revision_checkpoint.stage_revision(
                    key.media_id, key.goal, revision_plan, key.mode, request_id=request.request_id,
                )

            queued = TaskLifecycle.new(key, request_id=request.request_id).queued()
            if self._lifecycle is not None:
                await self._lifecycle.save_lifecycle(queued)
            if self._transport is not None:
                try:
                    await self._transport.enqueue(request)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A broker enqueue failure is a dispatch failure, not a
                    # worker attempt.  Keep the status read-side explicit
                    # when the lifecycle store is available, then release
                    # the reservation so a later submission can recover.
                    if self._lifecycle is not None:
                        try:
                            await self._lifecycle.save_lifecycle(
                                queued.fail(
                                    "任务未能进入消息队列",
                                    stage=TaskStage.DISPATCH_FAILED,
                                )
                            )
                        except Exception:
                            pass
                    raise
            await self._publish_best_effort(key, TaskEvent.of(queued.status, queued.stage))
            return DispatchDisposition.ACCEPTED
        except asyncio.CancelledError:
            if reserved:
                if revision_checkpoint is not None:
                    await self._cancel_revision(revision_checkpoint, key)
                await self._release(key)
            raise
        except Exception:
            if reserved:
                if revision_checkpoint is not None:
                    await self._cancel_revision(revision_checkpoint, key)
                await self._release(key)
            return DispatchDisposition.FAILED

    async def _cancel_revision(self, checkpoint, key) -> None:
        try:
            await checkpoint.cancel_staged_revision(key.media_id, key.goal, key.mode)
        except Exception:
            # A pending revision is never applied without the matching active
            # lifecycle request. Keep releasing the reservation on cache loss.
            pass

    async def _publish_best_effort(self, key, event: TaskEvent) -> None:
        if self._events is None:
            return
        try:
            await self._events.publish(key, event)
        except Exception:
            # Java keeps an accepted MQ submission accepted when notification
            # publication fails; a later worker/status query can recover it.
            return

    async def _release(self, key) -> None:
        try:
            await self._active.release(key)
        except Exception:
            return

    submit = dispatch


# Java-facing naming for adapters that port the service class directly.
AnalysisDispatchService = TaskDispatchService


__all__ = [
    "ACTIVE_TTL_SECONDS",
    "AnalysisDispatchService",
    "TaskDispatchService",
]
