"""R4 production composition over the frozen R2/R3 application boundaries.

R4 adds only the missing composition: real media processing, the configured
remote semantic provider, and the existing long-video/AgentLoop services.  It
does not define a second task worker, retrieval algorithm, or agent loop.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dovideo.application.follow_up_budget import estimate_chat_input, record_request_component
from dovideo.infrastructure.evidence_reflection import reflection_request_settings_from_environment
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from dovideo.application.checkpoint_service import (AgentCheckpointService)
from dovideo.application.agent import (AgentLoopService)
from dovideo.application.value_objects import (AnalysisRequest, TaskKey, TranscriptSpan)
from dovideo.application.long_context import (LongVideoContextService)
from dovideo.application.model_routing import (ModelRouteLane, ModelProfileRegistry, ModelRoutingService, ModelRoutingPolicy)
from dovideo.application.chunking import (VideoChunkingService)
from dovideo.application.context import (VideoContextBuilder)
from dovideo.application.retrieval import (VideoEvidenceRetrievalService)
from dovideo.application.tool_policy import (ToolPolicy)
from dovideo.application.tool_registry import (ToolRegistry)
from dovideo.application.video_tools import (VideoReadOnlyToolExecutor)
from dovideo.application.execution_record import (ExecutionRecordService, EXECUTION_CONTRACT_VERSION_V2)
from dovideo.application.errors import (BudgetExceededError)
from dovideo.application.ports.checkpoint import ContextCheckpointPort
from dovideo.application.content_context import ContentContextKey, ContentContextPreparation
from dovideo.application.ports.tasks import AgentLoopEntryPort, TaskEventPublisherPort
from dovideo.domain import (
    AgentState,
    AnalysisMode,
    BudgetUsage,
    CriticResult,
    TaskEvent,
    TaskStage,
    TaskStatus,
    TaskStatusState,
    VideoChunk,
    VideoContext,
    VideoSegment,
    AgentBudgetConfig,
)
from dovideo.infrastructure.media import (
    AudioSegmenter,
    AsyncSubprocessRunner,
    FfprobeDurationAdapter,
    FFmpegKeyframeExtractor,
    LocalWhisperTranscriptionAdapter,
    MediaBranchOrchestrator,
    OcrBatchService,
    PillowDifferenceHash,
    SegmentedTranscriptionService,
    SubprocessExecutionError,
    SubprocessLaunchError,
    SubprocessTimeoutError,
    TesseractOcrAdapter,
)
from dovideo.infrastructure.persistence.dead_letter_handoff import (
    CheckpointDeadLetterHandoffStore,
)
from dovideo.infrastructure.persistence.sqlalchemy import FailedTaskRecord
from dovideo.infrastructure.persistence.task_lifecycle import (
    CheckpointTaskLifecycleStore,
)
from dovideo.infrastructure.providers import (
    JevModelRouter,
    OpenAICompatibleChatClient,
    OpenAICompatibleEmbeddingAdapter,
    OpenAICompatibleModelAdapter,
    ModelRequestSettings,
    ProviderConfig,
    ProviderConfigurationError,
)
from dovideo.application.retrieval import vector_scope
from dovideo.infrastructure.providers.reranker import RerankerConfig, configured_reranker, SiliconFlowRerankerAdapter
from dovideo.infrastructure.redis_observability import RedisAgentTelemetry
from dovideo.infrastructure.vector.qdrant import QdrantVectorError
from dovideo.infrastructure.model_routing import (
    EffectiveModelProfileIdentity,
    ModelRoutingProductionSettings,
    ProductionModelRoutingAgentLoop,
    provider_config_for_lane,
)

from dovideo.presentation.composition import (
    AnalysisSettings,
    _agent_budget_from_environment,
    _prepend_tool_directory,
    embedding_provider_config_from_environment,
)

from .celery_runtime import R3RedisTaskEventPublisher
from .celery_transport import (
    CeleryTransportSettings,
    RabbitMQDeadLetterPublisher,
    RabbitMQTopology,
)
from .r2_config import R2Infrastructure, create_r2_infrastructure
from .x1_config import X1ToolCallingSettings
from .adaptive_retrieval import adaptive_settings_from_environment
from dovideo.application.adaptive_retrieval import AdaptiveRetrievalService
from .content_context import RedisContentBuildLock, pipeline_contract, sha256_file
from .persistence.content_context import SqlAlchemyContentArtifacts


from .r4_telemetry import (R4AgentTelemetry, _role_name)
from .r4_providers import (EXPECTED_EMBEDDING_DIMENSION, EXPECTED_EMBEDDING_MODEL, R4ProviderFallbackError, R4ProviderStack, _ObservedChatClient, _ObservedEmbedding, _ObservedSparse, _StrictChunkingService, _StrictRetrievalService, _model_client_for, _query_rewrite_request_settings_from_environment, create_r4_provider_stack)
from .r4_media import (R4MediaPipeline, R4RequestContextCheckpoint, _CURRENT_R4_REQUEST, _WhisperSegmentTranscriber, bind_r4_request, reset_r4_request)

@dataclass(slots=True)
class R4WorkerRuntime:
    """Real Celery worker composition for the canonical R4 delivery."""

    settings: CeleryTransportSettings
    infrastructure: R2Infrastructure
    lifecycle: CheckpointTaskLifecycleStore
    checkpoint: AgentCheckpointService
    events: R3RedisTaskEventPublisher
    telemetry: R4AgentTelemetry
    provider: R4ProviderStack
    dead_letter: RabbitMQDeadLetterPublisher
    handoff: CheckpointDeadLetterHandoffStore
    worker: Any
    topology: RabbitMQTopology
    initialized: bool = False
    tool_settings: X1ToolCallingSettings = field(
        default_factory=X1ToolCallingSettings
    )

    @classmethod
    def from_environment(
        cls,
        *,
        settings: CeleryTransportSettings | None = None,
        infrastructure: R2Infrastructure | None = None,
        tool_settings: X1ToolCallingSettings | None = None,
    ) -> "R4WorkerRuntime":
        from dovideo.application.worker import TaskWorker

        selected_tool_settings = tool_settings or X1ToolCallingSettings.from_environment()
        selected_settings = settings or CeleryTransportSettings.from_environment(
            require_production=True
        )
        selected_infrastructure = infrastructure or create_r2_infrastructure()
        try:
            checkpoint = AgentCheckpointService(selected_infrastructure.checkpoint_repository)
            lifecycle = CheckpointTaskLifecycleStore.from_environment(
                selected_infrastructure.checkpoint_repository,
                redis_client=selected_infrastructure.redis_client,
            )
            baseline_trace = RedisAgentTelemetry(selected_infrastructure.redis_client)
            telemetry = R4AgentTelemetry(
                baseline_trace, budget_config=_agent_budget_from_environment()
            )
            execution_records = ExecutionRecordService(
                selected_infrastructure.execution_record_repository,
                trace_projection=telemetry.project_execution,
                execution_contract_version=EXECUTION_CONTRACT_VERSION_V2,
            )
            events = R3RedisTaskEventPublisher(
                selected_infrastructure.redis_client,
                lifecycle,
                trace=telemetry,
            )
            provider = create_r4_provider_stack(
                checkpoint,
                selected_infrastructure.vector_index,
                telemetry,
                events,
                tool_settings=selected_tool_settings,
                execution_records=execution_records,
            )
            media_settings = AnalysisSettings.from_environment(
                embedding_mode="remote",
                project_root=Path(__file__).resolve().parents[3],
            )
            pipeline = R4MediaPipeline(
                selected_infrastructure,
                media_settings,
                telemetry,
                events,
            )
            context_checkpoint = R4RequestContextCheckpoint(checkpoint, pipeline, prepare_chunks=provider.long_context.prepare_chunks)
            dead_letter = RabbitMQDeadLetterPublisher(
                selected_settings,
                failed_task_store=selected_infrastructure.failed_task_store,
                redis_client=selected_infrastructure.redis_client,
            )
            handoff = CheckpointDeadLetterHandoffStore(
                selected_infrastructure.checkpoint_repository
            )
            worker = TaskWorker(
                selected_infrastructure.task_lock,
                selected_infrastructure.active_marker,
                lifecycle,
                context_checkpoint,
                provider.agent_loop,
                checkpoint,
                events=events,
                completion=selected_infrastructure.completion_marker,
                dead_letter=dead_letter,
                dead_letter_handoff=handoff,
                execution_records=execution_records,
            )
            return cls(
                settings=selected_settings,
                infrastructure=selected_infrastructure,
                lifecycle=lifecycle,
                checkpoint=checkpoint,
                events=events,
                telemetry=telemetry,
                provider=provider,
                dead_letter=dead_letter,
                handoff=handoff,
                worker=worker,
                topology=RabbitMQTopology(selected_settings),
                tool_settings=selected_tool_settings,
            )
        except BaseException:
            if infrastructure is None:
                selected_infrastructure.close()
            raise


    async def initialize(self) -> None:
        if self.initialized:
            return
        await self.infrastructure.initialize()
        await asyncio.to_thread(self.topology.ensure)
        self.initialized = True

    async def process(self, request: AnalysisRequest):
        await self.initialize()
        request_token = bind_r4_request(request)
        telemetry_token = self.telemetry.bind(request.task_key)
        try:
            return await self.worker.handle(request)
        finally:
            self.telemetry.reset(telemetry_token)
            reset_r4_request(request_token)

    async def record_poison(self, payload: Any, error: BaseException) -> None:
        await self.initialize()
        errors: list[BaseException] = []
        try:
            await asyncio.to_thread(self._record_poison_failure, payload, error)
        except BaseException as exc:
            errors.append(exc)
        try:
            await self.dead_letter.publish_poison(payload, error)
        except BaseException as exc:
            errors.append(exc)
        if errors:
            from .celery_runtime import PoisonMessageUnresolved

            raise PoisonMessageUnresolved(
                "poison message durable record or DLQ publication failed"
            ) from errors[0]

    def _record_poison_failure(self, payload: Any, error: BaseException) -> None:
        media_id = 0
        mode = "GENERAL"
        if isinstance(payload, dict):
            candidate = payload.get("mediaId")
            if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
                media_id = candidate
            mode = str(payload.get("mode", "GENERAL")).strip().upper() or "GENERAL"
            if mode not in {"GENERAL", "LEARNING", "REVIEW", "CREATION"}:
                mode = "GENERAL"
        self.infrastructure.failed_task_store.record(
            FailedTaskRecord(
                media_id=media_id,
                action="INVALID_TRANSPORT",
                mode=mode,
                content_hash="invalid-transport",
                user_goal="invalid transport envelope",
                attempt_count=0,
                error_type=type(error).__name__[:128],
                error_message="transport envelope validation failed",
                status="DEAD_LETTER_PENDING",
            )
        )

    async def close(self) -> None:
        try:
            await self.provider.close()
        finally:
            self.infrastructure.close()


__all__ = [
    "EXPECTED_EMBEDDING_DIMENSION",
    "EXPECTED_EMBEDDING_MODEL",
    "R4AgentTelemetry",
    "R4ProviderFallbackError",
    "R4MediaPipeline",
    "R4ProviderStack",
    "R4RequestContextCheckpoint",
    "R4WorkerRuntime",
    "bind_r4_request",
    "create_r4_provider_stack",
    "reset_r4_request",
]
