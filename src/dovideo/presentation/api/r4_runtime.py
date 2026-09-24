"""FastAPI production composition for the R4 full-parity path."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from dovideo.application import (
    AgentCheckpointService,
    AgentEvaluationService,
    AnalysisRequest,
    AnalysisStatusQuery,
    DispatchDisposition,
    FollowUpFailure,
    FailedTaskAdminService,
    GroundedFollowUpService,
    ExecutionRecordService,
    EXECUTION_CONTRACT_VERSION_V2,
    HistoricalAgentReplayService,
    HistoricalReplayAccessService,
    ModeRouter,
    TaskDispatchService,
    TaskKey,
)
from dovideo.domain import (
    AnalysisMode,
    TaskEvent,
    TaskStage,
    TaskStatus,
    TaskStatusState,
)
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
from dovideo.infrastructure.r4_runtime import (
    R4AgentTelemetry,
    R4ProviderStack,
    create_r4_provider_stack,
)
from dovideo.infrastructure.x1_config import X1ToolCallingSettings
from dovideo.infrastructure.providers import (
    GroundedFollowUpModelAdapter,
    ModeRouterModelAdapter,
)
from .r2_runtime import ProductionR2Services
from .runtime import R1ServiceError


class ProductionR4Services(ProductionR2Services):
    """R2 storage plus the real R3 transport and R4 provider read surface."""

    def __init__(
        self,
        infrastructure: R2Infrastructure,
        *,
        transport_settings: CeleryTransportSettings | None = None,
        celery_app: Any | None = None,
        tool_settings: X1ToolCallingSettings | None = None,
    ) -> None:
        super().__init__(infrastructure)
        selected_tool_settings = tool_settings or X1ToolCallingSettings.from_environment()
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
        self.provider_telemetry = R4AgentTelemetry(self.trace)
        self.execution_records = ExecutionRecordService(
            infrastructure.execution_record_repository,
            trace_projection=self.provider_telemetry.project_execution,
            execution_contract_version=EXECUTION_CONTRACT_VERSION_V2,
        )
        # X2-D is a separate, providerless read boundary.  It consumes the
        # durable X2-B record and the existing X1 tool ledger reader, but is
        # never routed through submit_analysis/TaskWorker/Celery.
        self.historical_replay = HistoricalAgentReplayService(
            self.execution_records,
            tool_checkpoint=self.checkpoint,
        )
        self.replay_access = HistoricalReplayAccessService(
            self.historical_replay,
            self.execution_records,
            self.media,
        )
        self.providers: R4ProviderStack = create_r4_provider_stack(
            self.checkpoint,
            infrastructure.vector_index,
            self.provider_telemetry,
            self.events,
            tool_settings=selected_tool_settings,
            execution_records=self.execution_records,
        )
        self.tool_settings = selected_tool_settings
        self.mode_router = ModeRouter(
            ModeRouterModelAdapter(self.providers.chat_client)
        )
        self.dispatcher = TaskDispatchService(
            infrastructure.active_marker,
            completion=infrastructure.completion_marker,
            quota=infrastructure.quota,
            lifecycle=self.lifecycle,
            events=self.events,
            transport=self.transport,
        )
        self.failed_task_admin = FailedTaskAdminService(
            infrastructure.failed_task_store,
            self.media,
            self.lifecycle,
            self.checkpoint,
            self.handoff,
            self.submit_analysis,
        )
        self.status_query = AnalysisStatusQuery(
            R3StatusCheckpoint(self.checkpoint, self.lifecycle),
            infrastructure.active_marker,
        )
        self._evaluator = AgentEvaluationService()

    async def route(self, goal: str) -> tuple[AnalysisMode, str]:
        """Use the model router for the production AUTO resolution boundary."""

        mode = await self.mode_router.route(goal)
        reason = {
            AnalysisMode.GENERAL: "使用通用分析模式",
            AnalysisMode.LEARNING: "面向学习与知识整理",
            AnalysisMode.REVIEW: "面向审查与风险识别",
            AnalysisMode.CREATION: "面向后续内容创作",
        }[mode]
        return mode, reason

    async def startup(self) -> None:
        await super().startup()
        await asyncio.to_thread(self.topology.ensure)

    async def shutdown(self) -> None:
        try:
            await self.providers.close()
        finally:
            await super().shutdown()

    async def submit_analysis(
        self,
        media_id: int,
        user_id: int,
        goal: str,
        mode: AnalysisMode,
        *,
        request_id: str | None = None,
    ) -> DispatchDisposition:
        record = await self.media.require_owned(media_id, user_id)
        request = AnalysisRequest(record.to_ref(), goal, mode, request_id=request_id)
        if await self.checkpoint.load_result(request.task_key) is not None:
            return DispatchDisposition.DUPLICATE
        disposition = await self.dispatcher.dispatch(request)
        if disposition is DispatchDisposition.ACCEPTED:
            try:
                self.trace.start(request.task_key)
            except Exception:
                pass
        return disposition

    async def list_failed_tasks(self, *, limit: int = 50, offset: int = 0):
        return await self.failed_task_admin.list_failed(limit=limit, offset=offset)

    async def inspect_failed_task(self, task_id: int):
        return await self.failed_task_admin.inspect(task_id)

    async def replay_failed_task(self, task_id: int, idempotency_key: str):
        return await self.failed_task_admin.replay(task_id, idempotency_key)

    async def status(
        self,
        media_id: int,
        goal: str,
        mode: AnalysisMode,
    ) -> TaskStatus:
        return await self.status_query.current(media_id, goal, mode)

    async def subscribe(self, key: TaskKey) -> AsyncIterator[Any | None]:
        lifecycle = await self.lifecycle.load_lifecycle(key)
        status = await self.status_query.current(key.media_id, key.goal, key.mode)
        attempt = 0 if lifecycle is None else lifecycle.attempt
        from dovideo.application.task_lifecycle import TaskLifecycleEvent

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

    async def follow_up(
        self,
        media_id: int,
        question: str,
        goal: str | None,
        mode: AnalysisMode,
    ) -> str:
        service = GroundedFollowUpService(
            self.checkpoint,
            self.providers.long_context,
            GroundedFollowUpModelAdapter(self.providers.chat_client),
        )
        statuses = {
            "invalid_request": 400,
            "invalid_mode": 400,
            "context_not_ready": 409,
            "no_evidence": 422,
            "evidence_rejected": 422,
            "invalid_response": 502,
            "timeout": 503,
            "provider_failure": 503,
            "retrieval_failure": 503,
            "checkpoint_failure": 503,
            "unexpected": 503,
        }
        try:
            with self.providers.telemetry.isolated_metrics():
                return await service.answer(media_id, question, goal, mode)
        except FollowUpFailure as error:
            status_code = statuses.get(error.category, 503)
            raise R1ServiceError(
                error.safe_message,
                status_code=status_code,
                code=status_code,
            ) from None

    async def evidence_search(self, media_id: int, query: str) -> tuple[object, ...]:
        context = await self.checkpoint.load_context(media_id)
        if context is None:
            return ()
        query_context = context.model_copy(update={"user_goal": query})
        return tuple(
            await self.providers.long_context.search_evidence(media_id, query_context)
        )

    async def evaluation(
        self,
        media_id: int,
        goal: str,
        mode: AnalysisMode,
    ) -> dict[str, object]:
        key = TaskKey(media_id, goal, mode)
        context = await self.checkpoint.load_context(media_id)
        state = await self.checkpoint.load_result(key)
        return self._evaluator.evaluate(context, state)

    async def start_transcription(self, media_id: int, user_id: int) -> None:
        await self.media.require_owned(media_id, user_id)
        raise R1ServiceError("R4 canonical worker performs transcription during analysis", status_code=501)

    async def transcription_status(self, media_id: int, user_id: int) -> TaskStatus:
        await self.media.require_owned(media_id, user_id)
        return TaskStatus(
            state=TaskStatusState.NOT_STARTED,
            result=None,
            message="文字提取由分析任务统一处理",
        )


def create_production_services() -> ProductionR4Services:
    # Validate X1 before opening any production infrastructure client.  This
    # keeps malformed rollout limits a composition/startup failure.
    tool_settings = X1ToolCallingSettings.from_environment()
    return ProductionR4Services(
        create_r2_infrastructure(),
        tool_settings=tool_settings,
    )


__all__ = ["ProductionR4Services", "create_production_services"]
