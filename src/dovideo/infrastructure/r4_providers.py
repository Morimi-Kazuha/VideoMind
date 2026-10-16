"""Extracted production providers boundary; legacy exports remain in r4_runtime."""
from __future__ import annotations
import math
import os
import time
from dovideo.application.follow_up_budget import record_request_component
from dovideo.infrastructure.evidence_reflection import reflection_request_settings_from_environment
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from dovideo.application.agent import (AgentLoopService)
from dovideo.application.long_context import (LongVideoContextService)
from dovideo.application.model_routing import (ModelRouteLane, ModelProfileRegistry, ModelRoutingService, ModelRoutingPolicy)
from dovideo.application.chunking import (VideoChunkingService)
from dovideo.application.retrieval import (VideoEvidenceRetrievalService)
from dovideo.application.tool_policy import (ToolPolicy)
from dovideo.application.tool_registry import (ToolRegistry)
from dovideo.application.video_tools import (VideoReadOnlyToolExecutor)
from dovideo.application.execution_record import (ExecutionRecordService)
from dovideo.application.errors import (BudgetExceededError)
from dovideo.application.ports.tasks import AgentLoopEntryPort, TaskEventPublisherPort
from dovideo.domain import VideoChunk
from dovideo.infrastructure.providers import JevModelRouter, OpenAICompatibleChatClient, OpenAICompatibleEmbeddingAdapter, OpenAICompatibleModelAdapter, ModelRequestSettings, ProviderConfig, ProviderConfigurationError
from dovideo.application.retrieval import vector_scope
from dovideo.infrastructure.providers.reranker import RerankerConfig, configured_reranker, SiliconFlowRerankerAdapter
from dovideo.infrastructure.vector.qdrant import QdrantVectorError
from dovideo.infrastructure.model_routing import EffectiveModelProfileIdentity, ModelRoutingProductionSettings, ProductionModelRoutingAgentLoop, provider_config_for_lane
from dovideo.presentation.composition import _agent_budget_from_environment, embedding_provider_config_from_environment
from .x1_config import X1ToolCallingSettings
from .adaptive_retrieval import adaptive_settings_from_environment
from dovideo.application.adaptive_retrieval import AdaptiveRetrievalService
from .r4_telemetry import R4AgentTelemetry, _role_name

EXPECTED_EMBEDDING_MODEL = "BAAI/bge-m3"


EXPECTED_EMBEDDING_DIMENSION = 1024


class R4ProviderFallbackError(RuntimeError):
    """A canonical provider stage failed and the base path used a fallback."""


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
    """Use current chunking while rejecting provider fallback paths."""

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
            raise R4ProviderFallbackError("R4 canonical chunking used a provider fallback")
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


class _ObservedSparse:
    def __init__(self, inner):
        self.inner = inner

    def rank(self, *args, **kwargs):
        record_request_component("bm25")
        return self.inner.rank(*args, **kwargs)


class _StrictRetrievalService(VideoEvidenceRetrievalService):
    """Use hybrid candidates while rejecting canonical component fallbacks."""

    def __init__(self, *args: Any, telemetry: R4AgentTelemetry, **kwargs: Any) -> None:
        super().__init__(*args, telemetry=telemetry, **kwargs)
        self._r4_telemetry = telemetry
        self._sparse = _ObservedSparse(self._sparse)

    async def index(self, media_id: int | None, chunks: Any) -> None:
        before = self._r4_telemetry.counter_value("vectorStoreFallbacks")
        normalized = tuple(chunks)
        await super().index(media_id, normalized)
        if self._r4_telemetry.counter_value("vectorStoreFallbacks") > before:
            raise QdrantVectorError("R4 canonical Qdrant indexing failed")
        if media_id is None or not normalized:
            raise ValueError("R4 canonical retrieval requires media identity and indexed chunks")
        try:
            hits = await self._vector_index.search(
                media_id,
                tuple(normalized[0].embedding),
                limit=len(normalized),
                **vector_scope(normalized),
            )
        except BudgetExceededError:
            raise
        except Exception as exc:
            raise QdrantVectorError("R4 canonical Qdrant verification failed") from exc
        if len(hits) < min(2, len(normalized)):
            raise QdrantVectorError("R4 Qdrant returned fewer than the required indexed chunks")
        self._r4_telemetry.increment("vectorStoreVerifications")

    async def retrieve(self, media_id: int | None, goal: str | None, chunks: Any):
        before = self._fallback_snapshot()
        result = await super().retrieve(media_id, goal, chunks)
        self._raise_if_fallback(before)
        if not result:
            raise RuntimeError("R4 canonical retrieval selected no temporal evidence")
        return result

    async def search(self, media_id: int | None, query: str | None, chunks: Any):
        before = self._fallback_snapshot()
        result = await super().search(media_id, query, chunks)
        self._raise_if_fallback(before)
        self._r4_telemetry.observe("evidenceHitCount", len(result))
        return result

    async def _retrieval_intent(self, goal: str | None):
        before = self._r4_telemetry.counter_value("retrievalIntentFallbacks")
        intent = await super()._retrieval_intent(goal)
        if self._r4_telemetry.counter_value("retrievalIntentFallbacks") > before:
            raise R4ProviderFallbackError("R4 canonical retrieval intent used a provider fallback")
        if not intent.semantic_query.strip():
            raise RuntimeError("R4 retrieval planner returned an empty semantic query")
        return intent

    async def _embed(self, text: str) -> tuple[float, ...]:
        before = self._r4_telemetry.counter_value("embeddingFallbacks")
        value = await super()._embed(text)
        if self._r4_telemetry.counter_value("embeddingFallbacks") > before:
            raise R4ProviderFallbackError("R4 canonical retrieval embedding used a fallback")
        if len(value) != EXPECTED_EMBEDDING_DIMENSION:
            raise RuntimeError("R4 retrieval query vector dimension is invalid")
        return value

    async def _dense_candidates(
        self,
        media_id: int | None,
        query_embedding: tuple[float, ...],
        chunks: Any = (),
    ):
        before = self._r4_telemetry.counter_value("vectorStoreFallbacks")
        scores = await super()._dense_candidates(media_id, query_embedding, chunks)
        if self._r4_telemetry.counter_value("vectorStoreFallbacks") > before:
            raise QdrantVectorError("R4 canonical Qdrant search used a fallback")
        # A healthy empty/single-hit query is distinct from a vector outage.
        return scores

    def _fallback_snapshot(self) -> tuple[int, ...]:
        return tuple(
            self._r4_telemetry.counter_value(name)
            for name in (
                "retrievalIntentFallbacks",
                "embeddingFallbacks",
                "vectorStoreFallbacks",
                "sparseFallbacks",
                "rerankerFallbacks",
            )
        )

    def _raise_if_fallback(self, before: tuple[int, ...]) -> None:
        after = self._fallback_snapshot()
        if after[2] > before[2]:
            raise QdrantVectorError("R4 canonical retrieval used a vector store fallback")
        if after[0] > before[0] or after[1] > before[1]:
            raise R4ProviderFallbackError("R4 canonical retrieval used a provider fallback")
        if after[3] > before[3] or after[4] > before[4]:
            raise R4ProviderFallbackError("R4 canonical retrieval used a sparse/reranker fallback")


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
    reranker_adapter: SiliconFlowRerankerAdapter | None = None

    @property
    def routing_enabled(self) -> bool:
        """Whether the opt-in J1 production route wrapper is active."""

        return self.routing_settings.enabled

    async def close(self) -> None:
        closed: set[int] = set()
        errors = []
        for client in (self.chat_client, *self.additional_chat_clients, self.reranker_adapter):
            if client is None:
                continue
            if id(client) in closed:
                continue
            closed.add(id(client))
            try:
                await client.aclose()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]


def _query_rewrite_request_settings_from_environment(environ=None):
    """Optional provider compatibility setting; M1 policy and caps stay frozen."""
    values = os.environ if environ is None else environ
    raw = values.get("DOVIDEO_QUERY_REWRITE_REASONING_EFFORT", "")
    if not isinstance(raw, str):
        raise ProviderConfigurationError("Query rewrite reasoning override must be text")
    value = raw.strip()
    if not value:
        return None
    if value != "none":
        raise ProviderConfigurationError("Query rewrite reasoning override supports only none")
    return ModelRequestSettings(reasoning_effort="none")


def create_r4_provider_stack(
    checkpoint: Any,
    vector_index: Any,
    telemetry: R4AgentTelemetry,
    events: TaskEventPublisherPort | None,
    *,
    tool_settings: X1ToolCallingSettings | None = None,
    execution_records: ExecutionRecordService | None = None,
    routing_settings: ModelRoutingProductionSettings | None = None,
    reranker_config: RerankerConfig | None = None,
    reranker_http_client: Any | None = None,
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
        reflection_request_settings=reflection_request_settings_from_environment(),
        query_rewrite_request_settings=_query_rewrite_request_settings_from_environment(),
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
            provider_config=effective_balanced_config,
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
                    provider_config=lane_config,
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
    reranker = configured_reranker(reranker_config, client=reranker_http_client)
    retrieval = _StrictRetrievalService(
        roles.retrieval_planner,
        embedding,
        vector_index,
        telemetry=telemetry,
        reranker=reranker,
    )
    adaptive_settings = adaptive_settings_from_environment()
    if adaptive_settings.enabled:
        retrieval = AdaptiveRetrievalService(
            retrieval, roles.retrieval_planner, adaptive_settings, telemetry=telemetry,
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
        reranker_adapter=reranker,
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

