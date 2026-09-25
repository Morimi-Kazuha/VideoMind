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
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from dovideo.application import (
    AgentCheckpointService,
    AgentLoopService,
    AnalysisRequest,
    LongVideoContextService,
    ModelRouteLane,
    ModelProfileRegistry,
    ModelRoutingService,
    ModelRoutingPolicy,
    TaskKey,
    VideoChunkingService,
    VideoContextBuilder,
    VideoEvidenceRetrievalService,
    ToolPolicy,
    ToolRegistry,
    VideoReadOnlyToolExecutor,
    ExecutionRecordService,
    EXECUTION_CONTRACT_VERSION_V2,
)
from dovideo.application.ports.checkpoint import ContextCheckpointPort
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


EXPECTED_EMBEDDING_MODEL = "BAAI/bge-m3"
EXPECTED_EMBEDDING_DIMENSION = 1024
_CURRENT_R4_REQUEST: ContextVar[AnalysisRequest | None] = ContextVar(
    "dovideo_r4_current_request",
    default=None,
)


class R4AgentTelemetry:
    """Use the existing Redis trace as both metrics and budget usage sink."""

    def __init__(self, store: RedisAgentTelemetry) -> None:
        self.store = store
        self._model_identifier = ""
        self._current_key: ContextVar[TaskKey | None] = ContextVar(
            "dovideo_r4_telemetry_task",
            default=None,
        )
        self._scoped_counters: ContextVar[dict[str, int] | None] = ContextVar(
            "dovideo_r4_scoped_counters",
            default=None,
        )

    def set_model_identifier(self, model: str | None) -> None:
        value = str(model or "").strip()
        self._model_identifier = value[:128]

    def start(self, key: TaskKey) -> str:
        return self.store.start(key)

    def bind(self, key: TaskKey):
        if not self.store.latest(key):
            self.store.start(key)
        return self._current_key.set(key)

    def reset(self, token: Any) -> None:
        self._current_key.reset(token)

    @contextmanager
    def isolated_metrics(self) -> Iterator[dict[str, int]]:
        """Capture retrieval counters locally without touching task traces.

        Follow-up is not a canonical task execution.  The strict R4 retrieval
        adapter still needs to observe its fallback counters, so keep those
        counters request-local and discard them when the interaction ends.
        ContextVar keeps concurrent API requests isolated.
        """

        counters: dict[str, int] = {}
        token = self._scoped_counters.set(counters)
        try:
            yield counters
        finally:
            self._scoped_counters.reset(token)

    def record(self, key: TaskKey, event: TaskEvent) -> None:
        if self._scoped_counters.get() is not None:
            return
        self.store.record(key, event)

    def latest(self, key: TaskKey) -> dict[str, Any]:
        return self.store.latest(key)

    def project_execution(
        self,
        key: TaskKey,
        *,
        execution_id: str,
        status: str,
        event_type: str | None,
        latest_sequence: int,
        recorded_semantic_events: int,
    ) -> None:
        """Project a durable execution fact into the existing Redis trace."""

        projector = getattr(self.store, "record_execution_projection", None)
        if callable(projector):
            projector(
                key,
                execution_id=execution_id,
                status=status,
                event_type=event_type,
                latest_sequence=latest_sequence,
                recorded_semantic_events=recorded_semantic_events,
            )
            return
        # Test/legacy trace adapters without the additive helper still get a
        # bounded structural diagnostic; they never receive event payloads.
        self.store.record_structural_for_key(
            key,
            {
                "kind": "executionRecord",
                "executionId": str(execution_id)[:96],
                "executionRecordStatus": str(status)[:32],
                "eventType": None if event_type is None else str(event_type)[:64],
                "latestRecordedSequence": max(0, int(latest_sequence)),
                "recordedSemanticEvents": max(0, int(recorded_semantic_events)),
            },
        )

    def _key(self, trace: Any | None = None) -> TaskKey | None:
        candidate = getattr(trace, "task_key", None) if trace is not None else None
        return candidate if isinstance(candidate, TaskKey) else self._current_key.get()

    def increment(
        self,
        metric: str,
        amount: int = 1,
        *,
        trace: Any | None = None,
    ) -> None:
        scoped = self._scoped_counters.get()
        if scoped is not None:
            scoped[metric] = scoped.get(metric, 0) + amount
            return
        key = self._key(trace)
        if key is not None:
            self.store.increment_for_key(key, metric, amount)

    def observe(
        self,
        metric: str,
        value: float,
        *,
        trace: Any | None = None,
    ) -> None:
        if self._scoped_counters.get() is not None:
            return
        key = self._key(trace)
        if key is not None:
            self.store.observe_for_key(key, metric, value)

    def add(
        self,
        estimated_tokens: int | float = 0,
        estimated_cost: float = 0.0,
        *,
        usage: BudgetUsage | Mapping[str, Any] | None = None,
    ) -> BudgetUsage:
        key = self._current_key.get()
        if usage is not None:
            if estimated_tokens != 0 or estimated_cost != 0.0:
                raise TypeError("usage cannot be combined with delta values")
            validated = BudgetUsage.model_validate(usage)
            estimated_tokens = validated.estimated_tokens
            estimated_cost = validated.estimated_cost
        if key is None or self._scoped_counters.get() is not None:
            return BudgetUsage(
                estimatedTokens=max(0, int(float(estimated_tokens))),
                estimatedCost=max(0.0, float(estimated_cost)),
            )
        return self.store.add_usage_for_key(
            key,
            estimated_tokens=estimated_tokens,
            estimated_cost=estimated_cost,
        )

    def current_usage(self) -> BudgetUsage:
        key = self._current_key.get()
        return BudgetUsage() if key is None else self.store.current_usage_for_key(key)

    currentUsage = current_usage

    def counter_value(self, metric: str) -> int:
        scoped = self._scoped_counters.get()
        if scoped is not None:
            return int(scoped.get(metric, 0))
        key = self._current_key.get()
        if key is None:
            return 0
        document = self.store.latest(key)
        counters = document.get("counters", {})
        return int(counters.get(metric, 0)) if isinstance(counters, dict) else 0

    def record_model_transport(
        self,
        stage: str,
        *,
        status_code: int,
        finish_reason: str | None,
        content_present: bool,
        content_chars: int,
    ) -> None:
        if self._scoped_counters.get() is not None:
            return
        key = self._current_key.get()
        if key is None:
            return
        self.store.record_structural_for_key(
            key,
            {
                "kind": "transport",
                "stage": _role_name(stage),
                "model": self._model_identifier,
                "statusCode": int(status_code),
                "finishReason": None if finish_reason is None else str(finish_reason)[:64],
                "contentPresent": bool(content_present),
                "contentChars": max(0, int(content_chars)),
            },
        )

    def record_structured_response(
        self,
        stage: str,
        diagnostic: Mapping[str, Any],
    ) -> None:
        if self._scoped_counters.get() is not None:
            return
        key = self._current_key.get()
        if key is None or not isinstance(diagnostic, Mapping):
            return
        self.store.record_structural_for_key(
            key,
            {
                "kind": "structured",
                "stage": _role_name(stage),
                "model": self._model_identifier,
                "diagnostic": dict(diagnostic),
            },
        )

    def record_model_routing(
        self,
        *,
        enabled: bool,
        lane: str,
        fallback: bool,
        reason: str,
        confidence: float,
    ) -> None:
        """Record additive bounded routing diagnostics in the existing trace."""

        if self._scoped_counters.get() is not None:
            return
        key = self._current_key.get()
        if key is None:
            return
        self.store.record_structural_for_key(
            key,
            {
                "kind": "modelRouting",
                "modelRoutingEnabled": bool(enabled),
                "modelRouteLane": str(lane)[:16],
                "modelRouteFallback": bool(fallback),
                "modelRouteReason": str(reason)[:48],
                "modelRouteConfidence": max(0.0, min(1.0, float(confidence))),
            },
        )

    def record_jev_routing(
        self,
        *,
        status_code: int | None,
        latency_ms: float,
        gateway: str,
        decision_model: str,
        router_model: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        usage_cost_usd: float | None,
        fallback: bool,
    ) -> None:
        """Persist only bounded Jev transport metadata; never raw payloads."""

        if self._scoped_counters.get() is not None:
            return
        key = self._current_key.get()
        if key is None:
            return
        safe_gateway = (
            gateway
            if gateway in {"OPENROUTER", "TYPESAFE_DIRECT"}
            else "UNKNOWN"
        )
        safe_cost = None
        if usage_cost_usd is not None:
            try:
                candidate_cost = float(usage_cost_usd)
            except (TypeError, ValueError, OverflowError):
                candidate_cost = -1.0
            if math.isfinite(candidate_cost) and 0.0 <= candidate_cost <= 1_000_000.0:
                safe_cost = candidate_cost
        self.store.record_structural_for_key(
            key,
            {
                "kind": "jevRoutingTransport",
                "statusCode": None if status_code is None else int(status_code),
                "latencyMs": max(0.0, min(120_000.0, float(latency_ms))),
                "gateway": safe_gateway,
                "decisionModel": str(decision_model)[:128],
                "routerModel": None if router_model is None else str(router_model)[:128],
                "inputTokens": None if input_tokens is None else max(0, int(input_tokens)),
                "outputTokens": None if output_tokens is None else max(0, int(output_tokens)),
                "usageCostUsd": safe_cost,
                "fallback": bool(fallback),
            },
        )

    def record_media_failure(
        self,
        *,
        media_branch: str,
        failure_stage: str,
        error: BaseException,
    ) -> None:
        """Persist only bounded, value-free OCR keyframe failure metadata."""

        if self._scoped_counters.get() is not None:
            return
        key = self._current_key.get()
        if (
            key is None
            or media_branch != "OCR"
            or failure_stage != "KEYFRAME_EXTRACTION"
        ):
            return
        process_started: bool | None = None
        if isinstance(error, SubprocessLaunchError):
            process_started = False
        elif isinstance(
            error,
            (SubprocessExecutionError, SubprocessTimeoutError),
        ):
            process_started = True
        raw_exit_code = getattr(error, "returncode", None)
        exit_code = (
            raw_exit_code
            if isinstance(raw_exit_code, int) and not isinstance(raw_exit_code, bool)
            else None
        )
        self.store.record_structural_for_key(
            key,
            {
                "kind": "mediaFailure",
                "mediaBranch": "OCR",
                "failureStage": "KEYFRAME_EXTRACTION",
                "errorClass": type(error).__name__[:80],
                "processStarted": process_started,
                "exitCode": exit_code,
            },
        )

    def record_executor_structural_attempt(
        self,
        *,
        attempt: int,
        repair_triggered: bool,
        repair_succeeded: bool,
    ) -> None:
        """Persist bounded structural-repair state in the existing trace."""

        if self._scoped_counters.get() is not None:
            return
        key = self._current_key.get()
        if key is None:
            return
        self.store.record_structural_for_key(
            key,
            {
                "kind": "executorStructuralAttempt",
                "stage": "EXECUTOR",
                "model": self._model_identifier,
                "executorStructuralAttempt": max(1, min(2, int(attempt))),
                "executorStructuralRepairTriggered": bool(repair_triggered),
                "executorStructuralRepairSucceeded": bool(repair_succeeded),
            },
        )


class _ObservedChatClient:
    """Keep provider role observations in the existing baseline trace."""

    def __init__(self, inner: OpenAICompatibleChatClient, telemetry: R4AgentTelemetry) -> None:
        self.inner = inner
        self.telemetry = telemetry

    async def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        stage: str,
    ) -> str:
        started = time.perf_counter()
        role = _role_name(stage)
        try:
            value = await self.inner.complete(messages, stage=stage)
            self.telemetry.increment("modelCalls")
            self.telemetry.increment(f"{role}Calls")
            return value
        except Exception:
            self.telemetry.increment("modelFailures")
            self.telemetry.increment(f"{role}Failures")
            raise
        finally:
            self.telemetry.observe(
                f"{role}LatencyMs",
                (time.perf_counter() - started) * 1000.0,
            )


class _ObservedEmbedding:
    """Strict BGE-M3 adapter wrapper used only by the R4 composition."""

    def __init__(
        self,
        inner: OpenAICompatibleEmbeddingAdapter,
        telemetry: R4AgentTelemetry,
        *,
        dimension: int = EXPECTED_EMBEDDING_DIMENSION,
    ) -> None:
        self.inner = inner
        self.telemetry = telemetry
        self.dimension = dimension

    async def embed(self, text: str) -> tuple[float, ...]:
        started = time.perf_counter()
        try:
            value = tuple(await self.inner.embed(text))
            if len(value) != self.dimension or any(
                not math.isfinite(float(item)) for item in value
            ):
                raise ValueError("canonical embedding dimension or values are invalid")
            self.telemetry.increment("embeddingCalls")
            return value
        except Exception:
            self.telemetry.increment("embeddingFailures")
            raise
        finally:
            self.telemetry.observe(
                "embeddingLatencyMs",
                (time.perf_counter() - started) * 1000.0,
            )


class _StrictChunkingService(VideoChunkingService):
    """Reuse the frozen chunking algorithm while rejecting its fallback path."""

    def __init__(self, *args: Any, telemetry: R4AgentTelemetry, **kwargs: Any) -> None:
        super().__init__(*args, telemetry=telemetry, **kwargs)
        self._r4_telemetry = telemetry

    async def build(self, segments: Any) -> tuple[VideoChunk, ...]:
        summary_fallbacks = self._r4_telemetry.counter_value("summaryFallbacks")
        embedding_fallbacks = self._r4_telemetry.counter_value("embeddingFallbacks")
        chunks = await super().build(segments)
        if (
            self._r4_telemetry.counter_value("summaryFallbacks") > summary_fallbacks
            or self._r4_telemetry.counter_value("embeddingFallbacks") > embedding_fallbacks
        ):
            raise RuntimeError("R4 canonical chunking used a provider fallback")
        if not chunks or any(
            len(chunk.embedding) != EXPECTED_EMBEDDING_DIMENSION
            or any(not math.isfinite(float(item)) for item in chunk.embedding)
            for chunk in chunks
        ):
            raise RuntimeError("R4 canonical chunks do not contain BGE-M3 vectors")
        self._r4_telemetry.observe("chunkCount", len(chunks))
        self._r4_telemetry.observe(
            "embeddingVectorCount",
            sum(bool(chunk.embedding) for chunk in chunks),
        )
        return chunks


class _StrictRetrievalService(VideoEvidenceRetrievalService):
    """Reuse hybrid scoring while making canonical Qdrant/provider failures loud."""

    def __init__(self, *args: Any, telemetry: R4AgentTelemetry, **kwargs: Any) -> None:
        super().__init__(*args, telemetry=telemetry, **kwargs)
        self._r4_telemetry = telemetry

    async def index(self, media_id: int | None, chunks: Any) -> None:
        before = self._r4_telemetry.counter_value("vectorStoreFallbacks")
        normalized = tuple(chunks)
        await super().index(media_id, normalized)
        if self._r4_telemetry.counter_value("vectorStoreFallbacks") > before:
            raise QdrantVectorError("R4 canonical Qdrant indexing failed")
        if media_id is None or len(normalized) < 2:
            raise ValueError("R4 canonical retrieval requires at least two chunks")
        try:
            hits = await self._vector_index.search(
                media_id,
                tuple(normalized[0].embedding),
                limit=len(normalized),
            )
        except Exception as exc:
            raise QdrantVectorError("R4 canonical Qdrant verification failed") from exc
        if len(hits) < 2:
            raise QdrantVectorError("R4 Qdrant returned fewer than two indexed chunks")
        self._r4_telemetry.increment("vectorStoreVerifications")

    async def retrieve(self, media_id: int | None, goal: str | None, chunks: Any):
        before = self._fallback_snapshot()
        result = await super().retrieve(media_id, goal, chunks)
        self._raise_if_fallback(before)
        if not result:
            raise RuntimeError("R4 canonical retrieval selected no temporal evidence")
        self._r4_telemetry.observe("retrievedSegmentCount", len(result))
        return result

    async def search(self, media_id: int | None, query: str | None, chunks: Any):
        before = self._fallback_snapshot()
        result = await super().search(media_id, query, chunks)
        self._raise_if_fallback(before)
        self._r4_telemetry.observe("retrievalCandidateCount", len(result))
        return result

    async def _retrieval_intent(self, goal: str | None):
        before = self._r4_telemetry.counter_value("retrievalIntentFallbacks")
        intent = await super()._retrieval_intent(goal)
        if self._r4_telemetry.counter_value("retrievalIntentFallbacks") > before:
            raise RuntimeError("R4 canonical retrieval intent used a provider fallback")
        if not intent.semantic_query.strip():
            raise RuntimeError("R4 retrieval planner returned an empty semantic query")
        return intent

    async def _embed(self, text: str) -> tuple[float, ...]:
        before = self._r4_telemetry.counter_value("embeddingFallbacks")
        value = await super()._embed(text)
        if self._r4_telemetry.counter_value("embeddingFallbacks") > before:
            raise RuntimeError("R4 canonical retrieval embedding used a fallback")
        if len(value) != EXPECTED_EMBEDDING_DIMENSION:
            raise RuntimeError("R4 retrieval query vector dimension is invalid")
        return value

    async def _vector_scores(self, media_id: int | None, query_embedding: tuple[float, ...]):
        before = self._r4_telemetry.counter_value("vectorStoreFallbacks")
        scores = await super()._vector_scores(media_id, query_embedding)
        if self._r4_telemetry.counter_value("vectorStoreFallbacks") > before:
            raise QdrantVectorError("R4 canonical Qdrant search used a fallback")
        if media_id is not None and len(scores) < 2:
            raise QdrantVectorError("R4 Qdrant returned fewer than two candidates")
        self._r4_telemetry.observe("retrievalCandidateCount", len(scores))
        return scores

    def _fallback_snapshot(self) -> tuple[int, int, int]:
        return tuple(
            self._r4_telemetry.counter_value(name)
            for name in (
                "retrievalIntentFallbacks",
                "embeddingFallbacks",
                "vectorStoreFallbacks",
            )
        )

    def _raise_if_fallback(self, before: tuple[int, int, int]) -> None:
        after = self._fallback_snapshot()
        if any(left > right for left, right in zip(after, before)):
            raise RuntimeError("R4 canonical retrieval used a provider fallback")


@dataclass(slots=True)
class R4ProviderStack:
    """One composition of the existing remote roles and long-context path."""

    model_config: ProviderConfig
    embedding_config: ProviderConfig
    chat_client: OpenAICompatibleChatClient
    telemetry: R4AgentTelemetry
    long_context: LongVideoContextService
    agent_loop: AgentLoopEntryPort
    tool_settings: X1ToolCallingSettings
    tool_registry: ToolRegistry
    tool_policy: ToolPolicy
    tool_executor: VideoReadOnlyToolExecutor | None
    execution_records: ExecutionRecordService | None = None
    routing_settings: ModelRoutingProductionSettings = field(
        default_factory=ModelRoutingProductionSettings
    )
    routing_service: ModelRoutingService | None = None
    jev_router: JevModelRouter | None = None
    model_adapters: Mapping[ModelRouteLane, OpenAICompatibleModelAdapter] = field(
        default_factory=dict
    )
    resolved_model_ids: Mapping[ModelRouteLane, str] = field(default_factory=dict)
    effective_model_profiles: Mapping[
        ModelRouteLane, EffectiveModelProfileIdentity
    ] = field(default_factory=dict)
    additional_chat_clients: tuple[OpenAICompatibleChatClient, ...] = ()

    @property
    def routing_enabled(self) -> bool:
        """Whether the opt-in J1 production route wrapper is active."""

        return self.routing_settings.enabled

    async def close(self) -> None:
        closed: set[int] = set()
        for client in (self.chat_client, *self.additional_chat_clients):
            if id(client) in closed:
                continue
            closed.add(id(client))
            await client.aclose()


def create_r4_provider_stack(
    checkpoint: Any,
    vector_index: Any,
    telemetry: R4AgentTelemetry,
    events: TaskEventPublisherPort | None,
    *,
    tool_settings: X1ToolCallingSettings | None = None,
    execution_records: ExecutionRecordService | None = None,
    routing_settings: ModelRoutingProductionSettings | None = None,
    model_routing_settings: ModelRoutingProductionSettings | None = None,
    jev_http_client: object | None = None,
    model_http_clients: Mapping[ModelRouteLane | str, object] | None = None,
) -> R4ProviderStack:
    """Build the configured provider path without making a network request."""

    selected_tool_settings = tool_settings or X1ToolCallingSettings.from_environment()
    model_config = ProviderConfig.from_environment(required=True)
    if model_config is None:
        raise ProviderConfigurationError("R4 model provider configuration is required")
    embedding_config = embedding_provider_config_from_environment(required=True)
    configured_embedding = (embedding_config.embedding_model or embedding_config.model).strip()
    if configured_embedding.casefold() != EXPECTED_EMBEDDING_MODEL.casefold():
        raise ProviderConfigurationError(
            "R4 canonical embedding model must be BAAI/bge-m3"
        )

    if routing_settings is not None and model_routing_settings is not None:
        raise TypeError("routing_settings and model_routing_settings are mutually exclusive")
    selected_routing_settings = (
        routing_settings
        if routing_settings is not None
        else model_routing_settings
        if model_routing_settings is not None
        else ModelRoutingProductionSettings.from_environment(
            balanced_model=model_config.model,
        )
    )
    if not isinstance(selected_routing_settings, ModelRoutingProductionSettings):
        raise TypeError("routing settings must be ModelRoutingProductionSettings")
    if selected_routing_settings.enabled:
        if selected_routing_settings.jev is None:
            raise ProviderConfigurationError(
                "Jev routing configuration is required when model routing is enabled"
            )
        selected_routing_settings.jev.validate_for_use()
    if selected_routing_settings.enabled and selected_routing_settings.balanced_model is None:
        selected_routing_settings = replace(
            selected_routing_settings,
            balanced_model=model_config.model,
        )

    effective_balanced_config = (
        provider_config_for_lane(
            model_config,
            selected_routing_settings,
            ModelRouteLane.BALANCED,
        )
        if selected_routing_settings.enabled
        else model_config
    ) or model_config

    balanced_request_settings = (
        selected_routing_settings.request_settings_for(ModelRouteLane.BALANCED)
        if selected_routing_settings.enabled
        else ModelRequestSettings()
    )

    chat_client = OpenAICompatibleChatClient(
        effective_balanced_config,
        request_settings=balanced_request_settings,
        client=_model_client_for(model_http_clients, ModelRouteLane.BALANCED),
        usage_sink=telemetry,
        response_observer=telemetry,
    )
    telemetry.set_model_identifier(effective_balanced_config.model)
    chat = _ObservedChatClient(chat_client, telemetry)
    roles = OpenAICompatibleModelAdapter(chat, diagnostic_observer=telemetry)
    model_adapters: dict[ModelRouteLane, OpenAICompatibleModelAdapter] = {
        ModelRouteLane.BALANCED: roles,
    }
    additional_chat_clients: list[OpenAICompatibleChatClient] = []
    resolved_model_ids: dict[ModelRouteLane, str] = {
        ModelRouteLane.BALANCED: effective_balanced_config.model,
    }
    effective_model_profiles: dict[ModelRouteLane, EffectiveModelProfileIdentity] = {
        ModelRouteLane.BALANCED: selected_routing_settings.effective_profile_identity(
            ModelRouteLane.BALANCED,
            resolved_model_id=effective_balanced_config.model,
        )
    }
    if selected_routing_settings.enabled:
        for lane in (ModelRouteLane.FAST, ModelRouteLane.DEEP):
            lane_config = provider_config_for_lane(
                model_config,
                selected_routing_settings,
                lane,
            )
            if lane_config is None:
                continue
            lane_client = OpenAICompatibleChatClient(
                lane_config,
                request_settings=selected_routing_settings.request_settings_for(lane),
                client=_model_client_for(model_http_clients, lane),
                usage_sink=telemetry,
                response_observer=telemetry,
            )
            additional_chat_clients.append(lane_client)
            resolved_model_ids[lane] = lane_config.model
            effective_model_profiles[lane] = (
                selected_routing_settings.effective_profile_identity(
                    lane,
                    resolved_model_id=lane_config.model,
                )
            )
            lane_chat = _ObservedChatClient(lane_client, telemetry)
            model_adapters[lane] = OpenAICompatibleModelAdapter(
                lane_chat,
                diagnostic_observer=telemetry,
            )
    embedding = _ObservedEmbedding(
        OpenAICompatibleEmbeddingAdapter(embedding_config),
        telemetry,
    )
    retrieval = _StrictRetrievalService(
        roles.retrieval_planner,
        embedding,
        vector_index,
        telemetry=telemetry,
    )
    chunking = _StrictChunkingService(
        roles.chunk_summary,
        embedding,
        telemetry=telemetry,
    )
    long_context = LongVideoContextService(
        chunking,
        retrieval,
        checkpoint=checkpoint,
        telemetry=telemetry,
    )
    tool_registry = ToolRegistry()
    tool_policy = ToolPolicy(tool_registry)
    tool_executor = (
        VideoReadOnlyToolExecutor(
            long_context=long_context,
            max_result_bytes=selected_tool_settings.tool_result_limit_bytes,
        )
        if selected_tool_settings.enabled
        else None
    )
    def build_agent(lane_roles: OpenAICompatibleModelAdapter) -> AgentLoopService:
        agent_options: dict[str, Any] = {
            "context_service": long_context,
            "planner": lane_roles.planner,
            "executor": lane_roles.executor,
            "critic": lane_roles.critic,
            "checkpoint": checkpoint,
            "event_publisher": events,
            "telemetry": telemetry,
            "usage_source": telemetry,
            "budget_config": _agent_budget_from_environment(),
            # The checkpoint is also retained in disabled mode as a read-only
            # rollback guard. No ledger is created unless X1 is enabled.
            "tool_checkpoint": checkpoint,
            "tool_calling_enabled": selected_tool_settings.enabled,
            "tool_request_limit_per_round": selected_tool_settings.per_round_limit,
            "tool_request_limit_total": selected_tool_settings.total_limit,
        }
        if selected_tool_settings.enabled:
            if tool_executor is None:  # pragma: no cover - construction guard
                raise RuntimeError("X1 tool executor was not constructed")
            agent_options.update(
                {
                    "executor_turn": lane_roles.executor,
                    "tool_executor": tool_executor,
                    "tool_policy": tool_policy,
                }
            )
        if execution_records is not None:
            agent_options["execution_record_service"] = execution_records
        return AgentLoopService(**agent_options)

    lane_agents: dict[ModelRouteLane, AgentLoopEntryPort] = {
        ModelRouteLane.BALANCED: build_agent(roles),
    }
    for lane, lane_roles in model_adapters.items():
        if lane is not ModelRouteLane.BALANCED:
            lane_agents[lane] = build_agent(lane_roles)

    routing_service: ModelRoutingService | None = None
    jev_router: JevModelRouter | None = None
    agent: AgentLoopEntryPort = lane_agents[ModelRouteLane.BALANCED]
    if selected_routing_settings.enabled:
        jev_router = JevModelRouter(
            selected_routing_settings.jev,  # type: ignore[arg-type]
            client=jev_http_client,
            observer=telemetry,
        )
        routing_service = ModelRoutingService(
            jev_router,
            policy=ModelRoutingPolicy(selected_routing_settings.policy_configuration()),
            profile_registry=ModelProfileRegistry.default(),
        )
    elif execution_records is not None:
        # Disabled J1 still records the actual BALANCED model selection as a
        # v2 historical fact.  The router is None and the policy returns
        # ROUTING_DISABLED without any Jev call.
        routing_service = ModelRoutingService(
            None,
            policy=ModelRoutingPolicy(selected_routing_settings.policy_configuration()),
            profile_registry=ModelProfileRegistry.default(),
        )
    if routing_service is not None and (
        selected_routing_settings.enabled or execution_records is not None
    ):
        agent = ProductionModelRoutingAgentLoop(
            lane_agents,
            routing_service,
            checkpoint=checkpoint,
            telemetry=telemetry,
            execution_records=execution_records,
            resolved_model_ids=resolved_model_ids,
            require_durable_recovery=selected_routing_settings.enabled,
            require_historical_route=execution_records is not None,
        )
    return R4ProviderStack(
        model_config=effective_balanced_config,
        embedding_config=embedding_config,
        chat_client=chat_client,
        telemetry=telemetry,
        long_context=long_context,
        agent_loop=agent,
        tool_settings=selected_tool_settings,
        tool_registry=tool_registry,
        tool_policy=tool_policy,
        tool_executor=tool_executor,
        execution_records=execution_records,
        routing_settings=selected_routing_settings,
        routing_service=routing_service,
        jev_router=jev_router,
        model_adapters=model_adapters,
        resolved_model_ids=resolved_model_ids,
        effective_model_profiles=MappingProxyType(effective_model_profiles),
        additional_chat_clients=tuple(additional_chat_clients),
    )


class _WhisperSegmentTranscriber:
    """Adapt the existing local Whisper adapter to segmented ASR."""

    def __init__(self, adapter: LocalWhisperTranscriptionAdapter) -> None:
        self.adapter = adapter

    async def transcribe_segment(
        self,
        audio_path: Path,
        *,
        trace_id: str | None = None,
    ) -> str:
        spans = await self.adapter.transcribe_path(audio_path, trace_id=trace_id)
        return " ".join(span.text for span in spans if span.text.strip())


class R4MediaPipeline:
    """Download one MinIO object and run the existing real media pipeline."""

    def __init__(
        self,
        infrastructure: R2Infrastructure,
        settings: AnalysisSettings,
        telemetry: R4AgentTelemetry,
        events: TaskEventPublisherPort,
    ) -> None:
        self.infrastructure = infrastructure
        self.settings = settings
        self.telemetry = telemetry
        self.events = events
        self._whisper: LocalWhisperTranscriptionAdapter | None = None

    async def build_context(self, request: AnalysisRequest) -> VideoContext:
        local_path = await self._download(request)
        try:
            _prepend_tool_directory(self.settings.ffmpeg_executable)
            runner = AsyncSubprocessRunner(
                default_timeout=self.settings.process_timeout_seconds
            )
            duration = await FfprobeDurationAdapter(
                runner,
                executable=self.settings.ffprobe_executable,
            ).probe(local_path)
            self.telemetry.observe("mediaDurationSeconds", duration.seconds)
            if duration.seconds <= 300.0:
                raise ValueError("R4 representative media must exceed five minutes")

            await self._publish(
                request,
                TaskStage.VIDEO_CONTEXT,
                "正在对 MinIO 视频执行 FFmpeg 媒体处理",
            )
            await self._publish(request, TaskStage.ASR, "正在运行本地 Whisper ASR")
            await self._publish(request, TaskStage.TRANSCRIPTION, "正在生成时间戳转录")
            observations = await MediaBranchOrchestrator(
                AudioSegmenter(
                    runner,
                    executable=self.settings.ffmpeg_executable,
                    timeout=self.settings.process_timeout_seconds,
                ),
                SegmentedTranscriptionService(
                    _WhisperSegmentTranscriber(self._whisper_adapter())
                ),
                FFmpegKeyframeExtractor(
                    runner,
                    executable=self.settings.ffmpeg_executable,
                    timeout=self.settings.process_timeout_seconds,
                ),
                OcrBatchService(
                    TesseractOcrAdapter(
                        runner,
                        executable=self.settings.tesseract_executable,
                    ),
                    PillowDifferenceHash(),
                    telemetry=self.telemetry,
                ),
                telemetry=self.telemetry,
                total_timeout_seconds=self.settings.media_timeout_seconds,
            ).collect(
                str(local_path),
                media_identity=(
                    request.media.content_hash or f"media-id:{request.media.media_id}"
                ),
                parent=self.infrastructure.settings.media_workspace / "r4-media-workspaces",
            )
            self.telemetry.observe("asrSpanCount", len(observations.asr.observations))
            self.telemetry.observe("ocrObservationCount", len(observations.ocr.observations))
            if not observations.asr.observations and not observations.ocr.observations:
                raise ValueError("R4 media produced no usable ASR or OCR observations")

            context = VideoContextBuilder().build(
                request.media.source,
                request.goal,
                observations,
                media_content_identity=(
                    request.media.content_hash
                    or f"media-id:{request.media.media_id}"
                ),
            )
            if len(context.segments) < 2:
                raise ValueError("R4 long media did not produce multiple temporal windows")
            self.telemetry.observe("videoContextWindowCount", len(context.segments))
            await self._publish(
                request,
                TaskStage.CONTEXT_COMPLETED,
                "VideoContext 时间窗口已从真实媒体观察构建",
            )
            return context
        finally:
            try:
                await asyncio.to_thread(local_path.unlink, True)
            except OSError:
                pass

    def _whisper_adapter(self) -> LocalWhisperTranscriptionAdapter:
        if self._whisper is None:
            self._whisper = LocalWhisperTranscriptionAdapter.from_openai_whisper(
                self.settings.whisper_model,
                model_root=self.settings.whisper_model_root,
                device=self.settings.whisper_device,
                language=self.settings.whisper_language,
            )
        return self._whisper

    async def _download(self, request: AnalysisRequest) -> Path:
        source = request.media.source
        parsed = urlsplit(source)
        bucket = parsed.netloc
        object_name = parsed.path.lstrip("/")
        if parsed.scheme != "minio" or bucket != self.infrastructure.settings.minio_bucket:
            raise ValueError("R4 worker requires a media object owned by MinIO")
        if not object_name or ".." in object_name or "\\" in object_name:
            raise ValueError("R4 media object name is invalid")
        target_dir = self.infrastructure.settings.media_workspace / "r4-inputs"
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        suffix = Path(request.media.filename or "media.mp4").suffix.lower() or ".mp4"
        target = target_dir / f"media-{request.media.media_id}-{uuid4().hex}{suffix}"

        def read_object() -> None:
            response = self.infrastructure.minio_client.get_object(bucket, object_name)
            try:
                with target.open("wb") as output:
                    while True:
                        piece = response.read(1024 * 1024)
                        if not piece:
                            break
                        output.write(piece)
            finally:
                close = getattr(response, "close", None)
                release = getattr(response, "release_conn", None)
                if callable(close):
                    close()
                if callable(release):
                    release()

        try:
            await asyncio.to_thread(read_object)
            if (await asyncio.to_thread(target.stat)).st_size <= 0:
                raise ValueError("R4 MinIO media object is empty")
            return target
        except BaseException:
            try:
                await asyncio.to_thread(target.unlink, True)
            except OSError:
                pass
            raise

    async def _publish(self, request: AnalysisRequest, stage: TaskStage, message: str) -> None:
        try:
            await self.events.publish(
                request.task_key,
                TaskEvent.of(TaskStatus.of(TaskStatusState.PROCESSING, message), stage),
            )
        except Exception:
            pass


class R4RequestContextCheckpoint(ContextCheckpointPort):
    """Build fresh context on first delivery, then use durable R2 checkpoints."""

    def __init__(
        self,
        delegate: ContextCheckpointPort,
        pipeline: R4MediaPipeline,
    ) -> None:
        self.delegate = delegate
        self.pipeline = pipeline

    async def load_context(self, media_id: int) -> VideoContext | None:
        context = await self.delegate.load_context(media_id)
        request = _CURRENT_R4_REQUEST.get()
        if context is None and request is not None and request.media.media_id == media_id:
            context = await self.pipeline.build_context(request)
            await self.delegate.save_context(media_id, context)
        if context is None or request is None or request.media.media_id != media_id:
            return context
        if context.user_goal == request.goal:
            return context
        return context.model_copy(update={"user_goal": request.goal})

    async def save_context(self, media_id: int, context: VideoContext) -> None:
        await self.delegate.save_context(media_id, context)

    async def load_chunks(self, media_id: int):
        return await self.delegate.load_chunks(media_id)

    async def save_chunks(self, media_id: int, chunks):
        await self.delegate.save_chunks(media_id, chunks)


def bind_r4_request(request: AnalysisRequest):
    if not isinstance(request, AnalysisRequest):
        raise TypeError("request must be an AnalysisRequest")
    return _CURRENT_R4_REQUEST.set(request)


def reset_r4_request(token: Any) -> None:
    _CURRENT_R4_REQUEST.reset(token)


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
        checkpoint = AgentCheckpointService(selected_infrastructure.checkpoint_repository)
        lifecycle = CheckpointTaskLifecycleStore.from_environment(
            selected_infrastructure.checkpoint_repository,
            redis_client=selected_infrastructure.redis_client,
        )
        baseline_trace = RedisAgentTelemetry(selected_infrastructure.redis_client)
        telemetry = R4AgentTelemetry(baseline_trace)
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
        context_checkpoint = R4RequestContextCheckpoint(checkpoint, pipeline)
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
        await self.provider.close()
        self.infrastructure.close()


def _role_name(stage: str) -> str:
    normalized = str(stage).upper()
    if normalized in {"PLANNER", "PLANNER_REPAIR", "REPLANNER"}:
        return "PLANNER"
    if normalized == "EXECUTOR":
        return "EXECUTOR"
    if normalized == "CRITIC":
        return "CRITIC"
    if normalized == "RETRIEVAL_PLANNER":
        return "RETRIEVAL_PLANNER"
    if normalized == "CHUNK_SUMMARY":
        return "CHUNK_SUMMARY"
    return normalized or "MODEL"


def _model_client_for(
    clients: Mapping[ModelRouteLane | str, object] | None,
    lane: ModelRouteLane,
) -> object | None:
    """Resolve an injected fake/client without exposing it to application DTOs."""

    if clients is None:
        return None
    value = clients.get(lane)
    if value is None:
        value = clients.get(lane.value)
    return value


__all__ = [
    "EXPECTED_EMBEDDING_DIMENSION",
    "EXPECTED_EMBEDDING_MODEL",
    "R4AgentTelemetry",
    "R4MediaPipeline",
    "R4ProviderStack",
    "R4RequestContextCheckpoint",
    "R4WorkerRuntime",
    "bind_r4_request",
    "create_r4_provider_stack",
    "reset_r4_request",
]
