"""Production R3 composition: unchanged R1 API over Celery/RabbitMQ."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from dovideo.application import (
    AgentCheckpointService,
    AnalysisRequest,
    AnalysisStatusQuery,
    DispatchDisposition,
    TaskDispatchService,
    TaskKey,
)
from dovideo.application.ports.tasks import AgentLoopEntryPort
from dovideo.application.worker import TaskWorker
from dovideo.domain import AgentState, AnalysisMode, VideoContext
from dovideo.infrastructure import (
    R2Infrastructure,
    RedisAgentTelemetry,
    create_r2_infrastructure,
)
from dovideo.infrastructure.celery_runtime import (
    R3RedisTaskEventPublisher,
    R3StatusCheckpoint,
)
from dovideo.infrastructure.celery_transport import (
    CeleryTaskTransport,
    CeleryTransportSettings,
    RabbitMQDeadLetterPublisher,
    RabbitMQTopology,
    create_celery_app,
)
from dovideo.infrastructure.persistence.dead_letter_handoff import (
    CheckpointDeadLetterHandoffStore,
)
from dovideo.infrastructure.persistence.task_lifecycle import (
    CheckpointTaskLifecycleStore,
)

from .r2_runtime import ProductionR2Services
from .runtime import R1ServiceError


class _ProductionAgentLoopRequired(AgentLoopEntryPort):
    """Fail closed until a real provider-backed AgentLoop is configured."""

    async def run(
        self,
        context: VideoContext,
        media_id: int | None = None,
        profile: Any | None = None,
    ) -> AgentState:
        del context, media_id, profile
        raise RuntimeError(
            "production AgentLoop is not configured for the R3 transport-only phase"
        )


class ProductionR3Services(ProductionR2Services):
    """R2 storage plus the real Celery/RabbitMQ asynchronous task boundary."""

    def __init__(
        self,
        infrastructure: R2Infrastructure,
        *,
        transport_settings: CeleryTransportSettings | None = None,
        celery_app: Any | None = None,
        agent_loop: AgentLoopEntryPort | None = None,
    ) -> None:
        super().__init__(infrastructure)
        settings = transport_settings or CeleryTransportSettings.from_environment(
            require_production=True
        )
        app = celery_app or create_celery_app(settings)
        self.transport_settings = settings
        self.celery_app = app
        self.topology = RabbitMQTopology(settings)
        self.transport = CeleryTaskTransport(app, settings)
        self.lifecycle = CheckpointTaskLifecycleStore.from_environment(
            infrastructure.checkpoint_repository,
            redis_client=infrastructure.redis_client,
        )
        self.events = R3RedisTaskEventPublisher(
            infrastructure.redis_client,
            self.lifecycle,
            trace=self.trace,
        )
        self.dead_letter = RabbitMQDeadLetterPublisher(
            settings,
            failed_task_store=infrastructure.failed_task_store,
            redis_client=infrastructure.redis_client,
        )
        self.handoff = CheckpointDeadLetterHandoffStore(
            infrastructure.checkpoint_repository
        )
        self.agent_loop = agent_loop or _ProductionAgentLoopRequired()
        self.worker = TaskWorker(
            infrastructure.task_lock,
            infrastructure.active_marker,
            self.lifecycle,
            self.checkpoint,
            self.agent_loop,
            self.checkpoint,
            events=self.events,
            completion=infrastructure.completion_marker,
            dead_letter=self.dead_letter,
            dead_letter_handoff=self.handoff,
        )
        self.dispatcher = TaskDispatchService(
            infrastructure.active_marker,
            completion=infrastructure.completion_marker,
            quota=infrastructure.quota,
            lifecycle=self.lifecycle,
            events=self.events,
            transport=self.transport,
        )
        self.status_query = AnalysisStatusQuery(
            R3StatusCheckpoint(self.checkpoint, self.lifecycle),
            infrastructure.active_marker,
        )

    async def startup(self) -> None:
        await super().startup()
        await asyncio.to_thread(self.topology.ensure)

    async def submit_analysis(
        self,
        media_id: int,
        user_id: int,
        goal: str,
        mode: AnalysisMode,
    ) -> DispatchDisposition:
        record = await self.media.require_owned(media_id, user_id)
        request = AnalysisRequest(record.to_ref(), goal, mode)
        if await self.checkpoint.load_result(request.task_key) is not None:
            return DispatchDisposition.DUPLICATE
        disposition = await self.dispatcher.dispatch(request)
        if disposition is DispatchDisposition.ACCEPTED:
            try:
                self.trace.start(request.task_key)
            except Exception:
                pass
        return disposition

    async def status(
        self,
        media_id: int,
        goal: str,
        mode: AnalysisMode,
    ):
        return await self.status_query.current(media_id, goal, mode)

    async def subscribe(self, key: TaskKey) -> AsyncIterator[Any | None]:
        lifecycle = await self.lifecycle.load_lifecycle(key)
        status = await self.status_query.current(key.media_id, key.goal, key.mode)
        attempt = 0 if lifecycle is None else lifecycle.attempt
        from dovideo.application.task_lifecycle import TaskLifecycleEvent
        from dovideo.domain import TaskEvent

        initial = TaskLifecycleEvent(
            key=key,
            event=TaskEvent.of(status, None if lifecycle is None else lifecycle.stage),
            attempt=attempt,
            retryable=False if lifecycle is None else lifecycle.retryable,
        )
        yield initial
        if initial.terminal:
            return
        seen = 0
        heartbeat_started = time.monotonic()
        while True:
            values = await self.events.read(key)
            for value in values[seen:]:
                seen += 1
                yield value
                if value.terminal:
                    return
            if time.monotonic() - heartbeat_started >= 15.0:
                heartbeat_started = time.monotonic()
                yield None
            await asyncio.sleep(0.5)


def create_production_services() -> ProductionR3Services:
    return ProductionR3Services(create_r2_infrastructure())


__all__ = ["ProductionR3Services", "create_production_services"]
