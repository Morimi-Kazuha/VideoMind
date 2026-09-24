"""Production composition for J1 adaptive model routing.

This module connects the provider-neutral J1-A contracts to infrastructure
profiles without putting provider identities into routing DTOs.  The wrapper
routes once before the first Planner call, durably saves the logical decision
in the existing checkpoint namespace, records the v2 historical route fact,
and delegates the complete execution to one lane-specific AgentLoop.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from dovideo.application.model_routing import (
    DEFAULT_ROUTING_CONFIDENCE_THRESHOLD,
    InvalidRoutingContextError,
    MODEL_ROUTING_CONTRACT_VERSION,
    ModelRouteHistory,
    ModelRouteLane,
    ModelRoutingConfiguration,
    ModelRoutingDecision,
    ModelRoutingPolicy,
    ModelRoutingReasonCode,
    ModelRoutingService,
    ModelProfileRegistry,
    UnsupportedRoutingContractError,
    TaskRoutingContext,
)
from dovideo.application.execution_record import (
    EXECUTION_CONTRACT_VERSION_V2,
    ExecutionEventType,
    ExecutionRecordStatus,
)
from dovideo.application.ports.tasks import AgentLoopEntryPort
from dovideo.application.value_objects import TaskKey
from dovideo.domain import AnalysisMode, ModeProfile, VideoContext

from .providers.config import ProviderConfig, ProviderConfigurationError
from .providers.jev import JevRouterSettings


MODEL_ROUTING_MAX_MODEL_LENGTH = 256
MODEL_ROUTING_MAX_PROFILE_COUNT = 3


class ModelRoutingConfigurationError(ValueError):
    """Production routing settings are missing or unsafe."""


class ModelRoutingPersistenceError(RuntimeError):
    """A route could not be durably saved before model execution."""


class RoutingProfileUnavailableError(RuntimeError):
    """A stable decision cannot be executed under the current configuration."""


class ModelRoutingHistoryIntegrityError(RuntimeError):
    """Durable route history and operational recovery state disagree."""


class ModelRoutingHistoryPersistenceError(RuntimeError):
    """The historical route fact could not be durably recorded."""


@dataclass(frozen=True, slots=True)
class ModelRoutingProductionSettings:
    """Feature-gated J1-B settings and infrastructure-owned model aliases."""

    enabled: bool = False
    confidence_threshold: float = DEFAULT_ROUTING_CONFIDENCE_THRESHOLD
    fast_model: str | None = None
    balanced_model: str | None = None
    deep_model: str | None = None
    jev: JevRouterSettings | None = None
    fast_enabled: bool | None = None
    deep_enabled: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ModelRoutingConfigurationError("model routing enabled flag must be boolean")
        try:
            configuration = ModelRoutingConfiguration(
                adaptiveRoutingEnabled=self.enabled,
                confidenceThreshold=self.confidence_threshold,
                fastEnabled=True,
                deepEnabled=True,
                routingContractVersion=MODEL_ROUTING_CONTRACT_VERSION,
            )
        except Exception as error:
            raise ModelRoutingConfigurationError(
                "model routing confidence threshold is invalid"
            ) from error
        object.__setattr__(self, "confidence_threshold", configuration.confidence_threshold)
        for name in ("fast_model", "balanced_model", "deep_model"):
            value = _optional_model(getattr(self, name), name)
            object.__setattr__(self, name, value)
        for name in ("fast_enabled", "deep_enabled"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise ModelRoutingConfigurationError(f"{name} must be boolean or None")
        if self.jev is not None and not isinstance(self.jev, JevRouterSettings):
            raise ModelRoutingConfigurationError("jev must be JevRouterSettings")

    @property
    def fast_lane_enabled(self) -> bool:
        return self.fast_model is not None and self.fast_enabled is not False

    @property
    def deep_lane_enabled(self) -> bool:
        return self.deep_model is not None and self.deep_enabled is not False

    @property
    def enabled_lanes(self) -> frozenset[ModelRouteLane]:
        lanes = {ModelRouteLane.BALANCED}
        if self.fast_lane_enabled:
            lanes.add(ModelRouteLane.FAST)
        if self.deep_lane_enabled:
            lanes.add(ModelRouteLane.DEEP)
        return frozenset(lanes)

    def policy_configuration(self) -> ModelRoutingConfiguration:
        return ModelRoutingConfiguration(
            adaptiveRoutingEnabled=self.enabled,
            confidenceThreshold=self.confidence_threshold,
            fastEnabled=self.fast_lane_enabled,
            deepEnabled=self.deep_lane_enabled,
            routingContractVersion=MODEL_ROUTING_CONTRACT_VERSION,
        )

    def model_for(self, lane: ModelRouteLane) -> str | None:
        if lane is ModelRouteLane.FAST:
            return self.fast_model
        if lane is ModelRouteLane.DEEP:
            return self.deep_model
        return self.balanced_model

    def provider_config_for(
        self,
        base: ProviderConfig,
        lane: ModelRouteLane,
    ) -> ProviderConfig | None:
        if not isinstance(base, ProviderConfig):
            raise TypeError("base must be ProviderConfig")
        model = self.model_for(lane)
        if not model:
            return None
        return replace(base, model=model)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        balanced_model: str | None = None,
    ) -> "ModelRoutingProductionSettings":
        values = os.environ if environ is None else environ
        enabled = _boolean(values, "DOVIDEO_MODEL_ROUTING_ENABLED", False)
        if not enabled:
            # Rollback mode intentionally ignores all optional J1 settings,
            # including a stale profile alias or malformed threshold.  The
            # pre-J1 provider configuration remains the sole model authority.
            return cls(enabled=False)
        base_balanced = _first_value(
            values,
            "DOVIDEO_BALANCED_MODEL",
            "DOVIDEO_MODEL_BALANCED",
        ) or balanced_model
        fast_model = _first_value(
            values,
            "DOVIDEO_FAST_MODEL",
            "DOVIDEO_MODEL_FAST",
        )
        deep_model = _first_value(
            values,
            "DOVIDEO_DEEP_MODEL",
            "DOVIDEO_MODEL_DEEP",
        )
        selected = cls(
            enabled=enabled,
            confidence_threshold=_number(
                values,
                "DOVIDEO_MODEL_ROUTING_CONFIDENCE_THRESHOLD",
                DEFAULT_ROUTING_CONFIDENCE_THRESHOLD,
            ),
            fast_model=fast_model,
            balanced_model=base_balanced,
            deep_model=deep_model,
            jev=(
                JevRouterSettings.from_environment(values, required=True)
                if enabled
                else None
            ),
            fast_enabled=_optional_boolean(
                values,
                "DOVIDEO_MODEL_ROUTING_FAST_ENABLED",
                None,
            ),
            deep_enabled=_optional_boolean(
                values,
                "DOVIDEO_MODEL_ROUTING_DEEP_ENABLED",
                None,
            ),
        )
        if selected.enabled and selected.balanced_model is None:
            raise ModelRoutingConfigurationError(
                "BALANCED model must be resolvable when model routing is enabled"
            )
        return selected


ModelRoutingSettings = ModelRoutingProductionSettings


class ProductionModelRoutingAgentLoop:
    """Select one lane and delegate the complete AgentLoop execution."""

    def __init__(
        self,
        lane_agent_loops: Mapping[ModelRouteLane | str, AgentLoopEntryPort],
        routing_service: ModelRoutingService,
        *,
        checkpoint: Any | None = None,
        telemetry: Any | None = None,
        execution_records: Any | None = None,
        resolved_model_ids: Mapping[ModelRouteLane | str, str] | None = None,
        require_durable_recovery: bool = False,
        require_historical_route: bool = False,
    ) -> None:
        if not isinstance(routing_service, ModelRoutingService):
            raise TypeError("routing_service must be ModelRoutingService")
        normalized: dict[ModelRouteLane, AgentLoopEntryPort] = {}
        for raw_lane, loop in lane_agent_loops.items():
            try:
                lane = raw_lane if isinstance(raw_lane, ModelRouteLane) else ModelRouteLane(raw_lane)
            except (TypeError, ValueError) as error:
                raise ModelRoutingConfigurationError("unknown production routing lane") from error
            if not callable(getattr(loop, "run", None)):
                raise TypeError("each production lane must provide run()")
            normalized[lane] = loop
        if ModelRouteLane.BALANCED not in normalized:
            raise ModelRoutingConfigurationError("BALANCED production lane is required")
        if len(normalized) > MODEL_ROUTING_MAX_PROFILE_COUNT:
            raise ModelRoutingConfigurationError("production routing lane set is too large")
        self._lane_agent_loops = MappingProxyType(normalized)
        self._balanced_agent_loop = normalized[ModelRouteLane.BALANCED]
        self._routing_service = routing_service
        self._checkpoint = checkpoint
        self._telemetry = telemetry
        self._execution_records = execution_records
        self._resolved_model_ids = MappingProxyType(
            _normalize_resolved_model_ids(resolved_model_ids)
        )
        self._memory_decisions: dict[TaskKey, ModelRoutingDecision] = {}
        self._durable_recovery = _has_routing_checkpoint(checkpoint)
        if (
            require_durable_recovery
            and routing_service.policy.configuration.adaptive_routing_enabled
            and not self._durable_recovery
        ):
            raise ModelRoutingConfigurationError(
                "enabled production model routing requires durable route checkpoint support"
            )
        if require_historical_route:
            if execution_records is None:
                raise ModelRoutingConfigurationError(
                    "production model routing requires durable execution history"
                )
            if not self._resolved_model_ids:
                raise ModelRoutingConfigurationError(
                    "historical model route requires resolved model identities"
                )
            if not callable(getattr(execution_records, "start_or_resume", None)):
                raise ModelRoutingConfigurationError(
                    "execution history has no start_or_resume operation"
                )
            if not callable(getattr(execution_records, "record_model_route", None)):
                raise ModelRoutingConfigurationError(
                    "execution history has no model route operation"
                )

    @property
    def lane_agent_loops(self) -> Mapping[ModelRouteLane, AgentLoopEntryPort]:
        return self._lane_agent_loops

    @property
    def routing_service(self) -> ModelRoutingService:
        return self._routing_service

    @property
    def durable_recovery(self) -> bool:
        return self._durable_recovery

    @property
    def durable_history(self) -> bool:
        return self._execution_records is not None

    async def run(
        self,
        context: VideoContext,
        media_id: int | None = None,
        profile: ModeProfile | None = None,
    ) -> Any:
        decision, key = await self.route_for_execution(
            context,
            media_id=media_id,
            profile=profile,
        )
        loop = self._lane_agent_loops.get(decision.lane)
        if loop is None:
            # An existing in-flight FAST/DEEP decision must never silently
            # become a newly sampled BALANCED decision after configuration
            # rollback.  This is an explicit fail-closed recovery boundary.
            raise RoutingProfileUnavailableError(
                f"stable model route {decision.lane.value} is unavailable"
            )
        return await loop.run(context, media_id=media_id, profile=profile)

    async def run_once(
        self,
        context: VideoContext,
        media_id: int | None = None,
        saved_state: Any | None = None,
        profile: ModeProfile | None = None,
        **kwargs: Any,
    ) -> Any:
        decision, _key = await self.route_for_execution(
            context,
            media_id=media_id,
            profile=profile,
        )
        loop = self._lane_agent_loops.get(decision.lane)
        if loop is None:
            raise RoutingProfileUnavailableError(
                f"stable model route {decision.lane.value} is unavailable"
            )
        method = getattr(loop, "run_once", None)
        if not callable(method):
            return await loop.run(context, media_id=media_id, profile=profile)
        return await method(
            context,
            media_id=media_id,
            saved_state=saved_state,
            profile=profile,
            **kwargs,
        )

    async def route_for_execution(
        self,
        context: VideoContext,
        *,
        media_id: int | None = None,
        profile: ModeProfile | None = None,
    ) -> tuple[ModelRoutingDecision, TaskKey]:
        key = _task_key_for(context, media_id, profile)
        historical_record = await self._load_execution_record(key)
        historical_route = self._historical_route(historical_record)
        existing = await self._load_decision(key)
        if historical_route is not None:
            decision = historical_route.to_decision()
            self._validate_checkpoint_consistency(existing, decision)
            self._validate_current_profile_identity(historical_route)
            if existing is None and self._routing_service.policy.configuration.adaptive_routing_enabled:
                await self._save_decision(key, decision)
            await self._ensure_historical_route(
                context,
                key,
                decision,
                historical_record=historical_record,
                historical_route=historical_route,
            )
            self._observe(decision)
            return decision, key
        self._validate_route_record_state(historical_record)
        try:
            routing_context = build_task_routing_context(
                context,
                media_id=media_id,
                profile=profile,
            )
        except Exception:
            # A routing optimization must never reject an otherwise valid
            # analysis because a bounded optional signal is unavailable.
            decision = self._routing_service.policy.fallback(
                reason_code=_invalid_reason(),
            )
            if existing is None and self._routing_service.policy.configuration.adaptive_routing_enabled:
                await self._save_decision(key, decision)
            await self._ensure_historical_route(context, key, decision)
            self._observe(decision)
            return decision, key

        if existing is not None:
            try:
                decision = await self._routing_service.route(
                    routing_context,
                    existing_decision=existing,
                )
            except UnsupportedRoutingContractError:
                decision = self._routing_service.policy.fallback(_invalid_reason())
                await self._save_decision(key, decision)
            await self._ensure_historical_route(context, key, decision)
            self._observe(decision)
            return decision, key

        if self._routing_service.policy.configuration.adaptive_routing_enabled:
            decision = await self._routing_service.route(routing_context)
            await self._save_decision(key, decision)
        else:
            # Disabled mode does not create a route checkpoint and therefore
            # preserves the legacy final-only path's observable behavior.
            decision = self._routing_service.policy.fallback(
                reason_code=ModelRoutingReasonCode.ROUTING_DISABLED
            )
        await self._ensure_historical_route(context, key, decision)
        self._observe(decision)
        return decision, key

    async def _load_execution_record(self, key: TaskKey) -> Any | None:
        source = self._execution_records
        if source is None:
            return None
        loader = getattr(source, "load_for_task", None)
        if not callable(loader):
            loader = getattr(source, "latest_for_task", None)
        if not callable(loader):
            raise ModelRoutingHistoryPersistenceError(
                "execution history has no task read operation"
            )
        try:
            value = loader(key)
            if hasattr(value, "__await__"):
                value = await value
            return value
        except ModelRoutingHistoryIntegrityError:
            raise
        except Exception as error:
            raise ModelRoutingHistoryPersistenceError(
                "execution history could not be read"
            ) from error

    @staticmethod
    def _historical_route(record: Any | None) -> ModelRouteHistory | None:
        if record is None:
            return None
        raw_contract = getattr(record, "execution_contract_version", None)
        events = getattr(record, "events", ())
        route_events = []
        for event in tuple(events or ()):
            raw_type = getattr(event, "event_type", None)
            if raw_type is ExecutionEventType.MODEL_ROUTE_RECORDED or str(
                getattr(raw_type, "value", raw_type)
            ) == ExecutionEventType.MODEL_ROUTE_RECORDED.value:
                route_events.append(event)
        if len(route_events) > 1:
            raise ModelRoutingHistoryIntegrityError(
                "execution history contains multiple model route events"
            )
        if not route_events:
            if raw_contract == EXECUTION_CONTRACT_VERSION_V2:
                return None
            if raw_contract is not None:
                raise ModelRoutingHistoryIntegrityError(
                    "route-aware production execution has an incompatible contract"
                )
            return None
        if raw_contract != EXECUTION_CONTRACT_VERSION_V2:
            raise ModelRoutingHistoryIntegrityError(
                "MODEL_ROUTE_RECORDED requires execution contract v2"
            )
        event = route_events[0]
        try:
            payload = getattr(event, "payload", {})
            payload = {
                key: value for key, value in payload.items() if key != "kind"
            }
            route = ModelRouteHistory.model_validate(payload)
            if route.routing_contract_version != MODEL_ROUTING_CONTRACT_VERSION:
                raise ModelRoutingHistoryIntegrityError(
                    "historical model route contract is unsupported"
                )
            event_sequence = getattr(event, "sequence_no", 0)
            if not isinstance(event_sequence, int) or event_sequence <= 1:
                raise ModelRoutingHistoryIntegrityError(
                    "MODEL_ROUTE_RECORDED must follow EXECUTION_STARTED"
                )
            for candidate in tuple(getattr(record, "events", ()) or ()):
                candidate_type = str(
                    getattr(
                        getattr(candidate, "event_type", None),
                        "value",
                        getattr(candidate, "event_type", ""),
                    )
                )
                if candidate_type in {
                    "PLAN_RECORDED",
                    "PLAN_REPAIRED",
                    "PLAN_REPLANNED",
                } and getattr(candidate, "sequence_no", 0) <= event_sequence:
                    raise ModelRoutingHistoryIntegrityError(
                        "MODEL_ROUTE_RECORDED must precede the first plan event"
                    )
            return route
        except Exception as error:
            raise ModelRoutingHistoryIntegrityError(
                "durable model route event is invalid"
            ) from error

    @staticmethod
    def _validate_route_record_state(record: Any | None) -> None:
        if record is None:
            return
        contract = getattr(record, "execution_contract_version", None)
        if contract != EXECUTION_CONTRACT_VERSION_V2:
            raise ModelRoutingHistoryIntegrityError(
                "existing execution history is not route-aware"
            )
        events = tuple(getattr(record, "events", ()) or ())
        if any(
            str(getattr(getattr(event, "event_type", None), "value", getattr(event, "event_type", "")))
            in {
                "PLAN_RECORDED",
                "PLAN_REPAIRED",
                "PLAN_REPLANNED",
                "RETRIEVAL_SELECTED",
                "EXECUTOR_TURN_RECORDED",
                "TOOL_CALL_REFERENCED",
                "CRITIC_RECORDED",
                "EVIDENCE_VERIFICATION_RECORDED",
            }
            for event in events
        ):
            raise ModelRoutingHistoryIntegrityError(
                "v2 execution history is missing its model route event"
            )

    @staticmethod
    def _validate_checkpoint_consistency(
        checkpoint_decision: ModelRoutingDecision | None,
        historical_decision: ModelRoutingDecision,
    ) -> None:
        if checkpoint_decision is None:
            return
        fields = (
            "lane",
            "confidence",
            "fallback_used",
            "reason_code",
            "suggested_lane",
            "routing_contract_version",
        )
        if any(
            getattr(checkpoint_decision, field) != getattr(historical_decision, field)
            for field in fields
        ):
            raise ModelRoutingHistoryIntegrityError(
                "model route checkpoint conflicts with historical event"
            )

    def _validate_current_profile_identity(self, route: ModelRouteHistory) -> None:
        if not self._resolved_model_ids:
            return
        try:
            profile = self._routing_service.resolve_profile(route.to_decision())
        except Exception as error:
            raise RoutingProfileUnavailableError(
                "historical model route profile is unavailable"
            ) from error
        if profile.profile_id != route.profile_id:
            raise ModelRoutingHistoryIntegrityError(
                "historical model route profile identity conflicts"
            )
        current_model = self._resolved_model_ids.get(route.lane)
        if current_model is None:
            raise RoutingProfileUnavailableError(
                f"stable model route {route.lane.value} is unavailable"
            )
        if current_model != route.resolved_model_id:
            raise RoutingProfileUnavailableError(
                "historical model profile mapping is unavailable"
            )

    async def _ensure_historical_route(
        self,
        context: VideoContext,
        key: TaskKey,
        decision: ModelRoutingDecision,
        *,
        historical_record: Any | None = None,
        historical_route: ModelRouteHistory | None = None,
    ) -> None:
        source = self._execution_records
        if source is None:
            return
        record = (
            historical_record
            if historical_record is not None
            else await self._load_execution_record(key)
        )
        route = historical_route or self._historical_route(record)
        if route is not None:
            self._validate_checkpoint_consistency(route.to_decision(), decision)
            self._validate_current_profile_identity(route)
            return
        if record is None:
            starter = getattr(source, "start_or_resume", None)
            if not callable(starter):
                raise ModelRoutingHistoryPersistenceError(
                    "execution history cannot start a route-aware record"
                )
            try:
                value = starter(
                    key,
                    media_identity=context.source,
                    source_revision=_source_revision(context),
                    execution_contract_version=EXECUTION_CONTRACT_VERSION_V2,
                )
                if hasattr(value, "__await__"):
                    value = await value
                record = value
            except ModelRoutingHistoryPersistenceError:
                raise
            except Exception as error:
                raise ModelRoutingHistoryPersistenceError(
                    "route-aware execution history could not be started"
                ) from error
        if getattr(record, "execution_contract_version", None) != EXECUTION_CONTRACT_VERSION_V2:
            raise ModelRoutingHistoryIntegrityError(
                "route-aware execution history has an incompatible contract"
            )
        profile = self._routing_service.resolve_profile(decision)
        resolved_model_id = self._resolved_model_ids.get(decision.lane)
        if not resolved_model_id:
            raise RoutingProfileUnavailableError(
                f"stable model route {decision.lane.value} is unavailable"
            )
        recorder = getattr(source, "record_model_route", None)
        if not callable(recorder):
            raise ModelRoutingHistoryPersistenceError(
                "execution history has no model route write operation"
            )
        try:
            value = recorder(
                record.execution_id,
                decision,
                profile=profile,
                resolved_model_id=resolved_model_id,
            )
            if hasattr(value, "__await__"):
                await value
        except ModelRoutingHistoryIntegrityError:
            raise
        except Exception as error:
            raise ModelRoutingHistoryPersistenceError(
                "MODEL_ROUTE_RECORDED could not be durably saved"
            ) from error

    async def _load_decision(self, key: TaskKey) -> ModelRoutingDecision | None:
        loader = getattr(self._checkpoint, "load_model_routing", None)
        if not callable(loader):
            loader = getattr(self._checkpoint, "loadModelRouting", None)
        if callable(loader):
            value = loader(key)
            if hasattr(value, "__await__"):
                value = await value
            if value is None:
                return None
            return (
                value
                if isinstance(value, ModelRoutingDecision)
                else ModelRoutingDecision.model_validate(value)
            )
        return self._memory_decisions.get(key)

    async def _save_decision(
        self,
        key: TaskKey,
        decision: ModelRoutingDecision,
    ) -> None:
        saver = getattr(self._checkpoint, "save_model_routing", None)
        if not callable(saver):
            saver = getattr(self._checkpoint, "saveModelRouting", None)
        if callable(saver):
            try:
                value = saver(key, decision)
                if hasattr(value, "__await__"):
                    await value
            except Exception as error:
                raise ModelRoutingPersistenceError(
                    "model route could not be durably saved"
                ) from error
        self._memory_decisions[key] = decision

    def _observe(self, decision: ModelRoutingDecision) -> None:
        record = getattr(self._telemetry, "record_model_routing", None)
        if not callable(record):
            return
        try:
            record(
                enabled=self._routing_service.policy.configuration.adaptive_routing_enabled,
                lane=decision.lane.value,
                fallback=decision.fallback_used,
                reason=decision.reason_code.value,
                confidence=decision.confidence,
            )
        except Exception:
            return

    def __getattr__(self, name: str) -> Any:
        # Existing composition/tests may inspect legacy AgentLoop attributes;
        # forwarding keeps the wrapper additive without exposing new route
        # state through the public task/API contract.
        balanced = object.__getattribute__(self, "_balanced_agent_loop")
        return getattr(balanced, name)


def build_task_routing_context(
    context: VideoContext,
    *,
    media_id: int | None,
    profile: ModeProfile | None,
) -> TaskRoutingContext:
    """Project a concrete VideoContext into J1-A's bounded routing state."""

    if not isinstance(context, VideoContext):
        raise InvalidRoutingContextError("routing context source is invalid")
    mode = _concrete_mode(profile)
    key = TaskKey(
        _media_id(media_id),
        context.user_goal,
        mode,
    )
    segments = tuple(context.segments)
    duration = max((segment.end_ms for segment in segments), default=0)
    chunk_values = getattr(context, "chunks", ())
    chunk_count = len(tuple(chunk_values)) if chunk_values is not None else 0
    asr_available = any(
        bool(segment.transcript.strip()) or bool(segment.asr_source_items)
        for segment in segments
    )
    ocr_available = any(
        bool(segment.ocr_texts) or bool(segment.ocr_source_items)
        for segment in segments
    )
    routing_values: dict[str, Any] = {
        "taskKey": key,
        "mode": mode,
        "userGoal": context.user_goal,
        "mediaDurationMs": duration,
        "segmentCount": len(segments),
        "chunkCount": chunk_count,
        "asrAvailable": asr_available,
        "ocrAvailable": ocr_available,
    }
    if context.source_revision.strip() and len(context.source_revision) <= 96:
        routing_values["sourceRevision"] = context.source_revision
    if context.provenance_version.strip() and len(context.provenance_version) <= 96:
        routing_values["sourceProvenanceVersion"] = context.provenance_version
    return TaskRoutingContext(
        **routing_values,
    )


def provider_config_for_lane(
    base: ProviderConfig,
    settings: ModelRoutingProductionSettings,
    lane: ModelRouteLane,
) -> ProviderConfig | None:
    """Resolve an infrastructure model profile without changing J1 DTOs."""

    if lane is ModelRouteLane.FAST and not settings.fast_lane_enabled:
        return None
    if lane is ModelRouteLane.DEEP and not settings.deep_lane_enabled:
        return None
    return settings.provider_config_for(base, lane)


def _task_key_for(
    context: VideoContext,
    media_id: int | None,
    profile: ModeProfile | None,
) -> TaskKey:
    return TaskKey(_media_id(media_id), context.user_goal, _concrete_mode(profile))


def _concrete_mode(profile: ModeProfile | None) -> AnalysisMode:
    if profile is None or profile.mode is None:
        return AnalysisMode.GENERAL
    if not isinstance(profile.mode, AnalysisMode):
        raise InvalidRoutingContextError("routing mode is not concrete")
    return profile.mode


def _media_id(value: int | None) -> int:
    if value is None:
        # Direct offline composition has no media identity.  Production
        # TaskWorker always supplies the authorized positive media id.
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRoutingContextError("routing media identity is invalid")
    return value


def _has_routing_checkpoint(checkpoint: Any | None) -> bool:
    if checkpoint is None:
        return False
    return callable(getattr(checkpoint, "load_model_routing", None)) and callable(
        getattr(checkpoint, "save_model_routing", None)
    ) or callable(getattr(checkpoint, "loadModelRouting", None)) and callable(
        getattr(checkpoint, "saveModelRouting", None)
    )


def _normalize_resolved_model_ids(
    values: Mapping[ModelRouteLane | str, str] | None,
) -> dict[ModelRouteLane, str]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise ModelRoutingConfigurationError("resolved model identities must be a mapping")
    normalized: dict[ModelRouteLane, str] = {}
    for raw_lane, raw_model in values.items():
        try:
            lane = raw_lane if isinstance(raw_lane, ModelRouteLane) else ModelRouteLane(raw_lane)
        except (TypeError, ValueError) as error:
            raise ModelRoutingConfigurationError(
                "resolved model identity has an unknown lane"
            ) from error
        if not isinstance(raw_model, str) or not raw_model.strip():
            raise ModelRoutingConfigurationError(
                "resolved model identity must be non-empty text"
            )
        normalized_model = raw_model.strip()
        if len(normalized_model) > MODEL_ROUTING_MAX_MODEL_LENGTH:
            raise ModelRoutingConfigurationError(
                "resolved model identity exceeds its bound"
            )
        normalized[lane] = normalized_model
    return normalized


def _source_revision(context: VideoContext) -> str:
    value = getattr(context, "source_revision", "")
    if isinstance(value, str) and value.strip():
        return value.strip()
    for segment in tuple(getattr(context, "segments", ()) or ()):
        value = getattr(segment, "source_revision", "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ModelRoutingHistoryPersistenceError(
        "route-aware execution requires a source revision"
    )


def _optional_model(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ModelRoutingConfigurationError(f"{name} must be text or None")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > MODEL_ROUTING_MAX_MODEL_LENGTH:
        raise ModelRoutingConfigurationError(f"{name} exceeds its bound")
    return normalized


def _first_value(values: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = values.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _first_value(values, name)
    if raw is None:
        return default
    normalized = raw.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ModelRoutingConfigurationError(f"{name} must be boolean")


def _optional_boolean(
    values: Mapping[str, str],
    name: str,
    default: bool | None,
) -> bool | None:
    raw = _first_value(values, name)
    if raw is None:
        return default
    return _boolean(values, name, bool(default))


def _number(values: Mapping[str, str], name: str, default: float) -> float:
    raw = _first_value(values, name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError, OverflowError) as error:
        raise ModelRoutingConfigurationError(f"{name} must be numeric") from error


def _invalid_reason() -> ModelRoutingReasonCode:
    return ModelRoutingReasonCode.INVALID_SUGGESTION


__all__ = [
    "MODEL_ROUTING_MAX_MODEL_LENGTH",
    "ModelRoutingConfigurationError",
    "ModelRoutingHistoryIntegrityError",
    "ModelRoutingHistoryPersistenceError",
    "ModelRoutingPersistenceError",
    "ModelRoutingProductionSettings",
    "ModelRoutingSettings",
    "ProductionModelRoutingAgentLoop",
    "RoutingProfileUnavailableError",
    "build_task_routing_context",
    "provider_config_for_lane",
]
