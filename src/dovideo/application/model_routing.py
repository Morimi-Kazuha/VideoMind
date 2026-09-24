"""Provider-neutral adaptive model-routing contracts for J1-A.

J1-A deliberately stops at the application decision boundary:

``TaskRoutingContext -> ModelRouterPort -> RoutingSuggestion``
``-> ModelRoutingPolicy -> ModelRoutingDecision -> ModelProfile``

The router is untrusted and may be unavailable.  The deterministic policy is
the authority and resolves every optimization failure to ``BALANCED``.  This
module contains no HTTP client, provider credential, model name, prompt, X2
event, or production AgentLoop wiring.  Those concerns belong to later J1
slices.
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Awaitable, Mapping
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_serializer,
    field_validator,
    model_validator,
)

from dovideo.domain import AnalysisMode, PROVENANCE_VERSION

from .value_objects import TaskKey


MODEL_ROUTING_CONTRACT_VERSION = "model-routing-v1"

MAX_ROUTING_GOAL_LENGTH = 500
MAX_ROUTING_SOURCE_REVISION_LENGTH = 96
MAX_ROUTING_PROVENANCE_VERSION_LENGTH = 96
MAX_ROUTING_MEDIA_DURATION_MS = 365 * 24 * 60 * 60 * 1000
MAX_ROUTING_SEGMENT_COUNT = 1_000_000
MAX_ROUTING_CHUNK_COUNT = 1_000_000
MAX_ROUTING_CLASSIFICATION_ITEMS = 8
MAX_ROUTING_CLASSIFICATION_KEY_LENGTH = 64
MAX_ROUTING_CLASSIFICATION_VALUE_LENGTH = 256
MAX_ROUTING_PROFILE_ID_LENGTH = 64
MAX_ROUTING_RESOLVED_MODEL_ID_LENGTH = 256
MAX_ROUTING_CONTRACT_VERSION_LENGTH = 64
DEFAULT_ROUTING_CONFIDENCE_THRESHOLD = 0.70


class RoutingContractError(ValueError):
    """Base error for invalid trusted routing contracts/configuration."""


class InvalidRoutingContextError(RoutingContractError):
    """The application did not provide a concrete, bounded routing context."""


class UnsupportedRoutingContractError(RoutingContractError):
    """A persisted/reused routing contract is not understood by this build."""


class InvalidRoutingSuggestionError(RoutingContractError):
    """A router returned a response that cannot be treated as a suggestion.

    Infrastructure adapters use this typed boundary so the application
    service can distinguish malformed router data from an unavailable router.
    Both are safe fallbacks, but the reason remains observable without
    exposing provider response content.
    """


class ModelRouteLane(str, Enum):
    """The complete J1 V1 logical routing lane allowlist."""

    FAST = "FAST"
    BALANCED = "BALANCED"
    DEEP = "DEEP"

    @classmethod
    def _missing_(cls, value: object) -> "ModelRouteLane | None":
        if isinstance(value, str):
            normalized = value.strip().upper()
            for member in cls:
                if member.value == normalized:
                    return member
        return None


class ModelRoutingReasonCode(str, Enum):
    """Bounded reasons for the policy's final route decision."""

    ROUTER_ACCEPTED = "ROUTER_ACCEPTED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    ROUTER_UNAVAILABLE = "ROUTER_UNAVAILABLE"
    ROUTER_ERROR = "ROUTER_ERROR"
    INVALID_SUGGESTION = "INVALID_SUGGESTION"
    MODE_NOT_ALLOWED = "MODE_NOT_ALLOWED"
    LANE_DISABLED = "LANE_DISABLED"
    ROUTING_DISABLED = "ROUTING_DISABLED"
    DECISION_REUSED = "DECISION_REUSED"


class _RoutingModel(BaseModel):
    """Strict, immutable, provider-neutral J1 contract base."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        validate_default=True,
        arbitrary_types_allowed=True,
    )


def _bounded_text(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} exceeds its bound")
    return normalized


def _bounded_optional_text(value: Any, field_name: str, maximum: int) -> str:
    if value is None:
        return ""
    return _bounded_text(value, field_name, maximum)


def _finite_confidence(value: Any, field_name: str = "confidence") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise ValueError(f"{field_name} must be between 0.0 and 1.0")
    return normalized


def _nonnegative_bounded_int(value: Any, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value < 0 or value > maximum:
        raise ValueError(f"{field_name} is outside its bound")
    return value


def _coerce_task_key(value: Any) -> TaskKey:
    if isinstance(value, TaskKey):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("task_key must be a trusted TaskKey")
    media_id = value.get("media_id", value.get("mediaId"))
    goal = value.get("goal")
    mode = value.get("mode", AnalysisMode.GENERAL)
    if isinstance(mode, str) and mode.strip().upper() == "AUTO":
        raise ValueError("AUTO cannot enter J1 model routing")
    try:
        return TaskKey(media_id, goal, mode)
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError("task_key is invalid") from error


def _normalize_context_input(data: Any) -> Any:
    if not isinstance(data, Mapping):
        return data
    normalized = dict(data)
    # These aliases make the boundary migration-friendly without introducing
    # a second source of identity authority.
    if "task_key" not in normalized and "taskKey" not in normalized:
        if "task_identity" in normalized:
            normalized["task_key"] = normalized.pop("task_identity")
        elif "taskIdentity" in normalized:
            normalized["task_key"] = normalized.pop("taskIdentity")
    elif "task_key" in normalized and "taskKey" in normalized:
        if _coerce_task_key(normalized["task_key"]) != _coerce_task_key(normalized["taskKey"]):
            raise ValueError("conflicting task_key aliases")
        normalized.pop("taskKey")
    elif "taskKey" in normalized:
        normalized["task_key"] = normalized.pop("taskKey")
    if "media_duration_ms" not in normalized and "mediaDurationMs" not in normalized:
        if "duration_ms" in normalized:
            normalized["media_duration_ms"] = normalized.pop("duration_ms")
        elif "durationMs" in normalized:
            normalized["media_duration_ms"] = normalized.pop("durationMs")

    key_value = normalized.get("task_key", normalized.get("taskKey"))
    if key_value is not None:
        key = _coerce_task_key(key_value)
        normalized["task_key"] = key
        if "mode" not in normalized:
            normalized["mode"] = key.mode
        if "user_goal" not in normalized and "userGoal" not in normalized:
            normalized["user_goal"] = key.goal
    return normalized


class TaskRoutingContext(_RoutingModel):
    """Trusted and bounded signals available before the first Planner call."""

    task_key: TaskKey = Field(alias="taskKey")
    mode: AnalysisMode
    user_goal: str = Field(alias="userGoal")
    media_duration_ms: int | None = Field(default=None, alias="mediaDurationMs")
    segment_count: int = Field(default=0, alias="segmentCount")
    chunk_count: int = Field(default=0, alias="chunkCount")
    asr_available: StrictBool = Field(default=False, alias="asrAvailable")
    ocr_available: StrictBool = Field(default=False, alias="ocrAvailable")
    source_revision: str = Field(default="", alias="sourceRevision")
    source_provenance_version: str = Field(
        default=PROVENANCE_VERSION,
        alias="sourceProvenanceVersion",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        normalized = _normalize_context_input(data)
        if not isinstance(normalized, Mapping):
            return normalized
        normalized = dict(normalized)
        if "task_key" in normalized:
            normalized["task_key"] = _coerce_task_key(normalized["task_key"])
            key = normalized["task_key"]
            normalized.setdefault("mode", key.mode)
            if "user_goal" not in normalized and "userGoal" not in normalized:
                normalized["user_goal"] = key.goal
        return normalized

    @field_validator("task_key", mode="before")
    @classmethod
    def _trusted_task_key(cls, value: Any) -> TaskKey:
        return _coerce_task_key(value)

    @field_validator("mode", mode="before")
    @classmethod
    def _concrete_mode(cls, value: Any) -> AnalysisMode:
        if isinstance(value, str) and value.strip().upper() == "AUTO":
            raise ValueError("AUTO cannot enter J1 model routing")
        try:
            if isinstance(value, AnalysisMode):
                return value
            if isinstance(value, str):
                return AnalysisMode[value.strip().upper()]
            return AnalysisMode(value)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("model routing requires a concrete analysis mode") from error

    @field_validator("user_goal")
    @classmethod
    def _goal(cls, value: str) -> str:
        return _bounded_text(value, "user_goal", MAX_ROUTING_GOAL_LENGTH)

    @field_validator("media_duration_ms", mode="before")
    @classmethod
    def _duration(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return _nonnegative_bounded_int(
            value,
            "media_duration_ms",
            MAX_ROUTING_MEDIA_DURATION_MS,
        )

    @field_validator("segment_count", mode="before")
    @classmethod
    def _segments(cls, value: int) -> int:
        return _nonnegative_bounded_int(value, "segment_count", MAX_ROUTING_SEGMENT_COUNT)

    @field_validator("chunk_count", mode="before")
    @classmethod
    def _chunks(cls, value: int) -> int:
        return _nonnegative_bounded_int(value, "chunk_count", MAX_ROUTING_CHUNK_COUNT)

    @field_validator("source_revision")
    @classmethod
    def _source_revision(cls, value: str) -> str:
        if value == "":
            return value
        return _bounded_text(value, "source_revision", MAX_ROUTING_SOURCE_REVISION_LENGTH)

    @field_validator("source_provenance_version")
    @classmethod
    def _provenance_version(cls, value: str) -> str:
        return _bounded_optional_text(
            value,
            "source_provenance_version",
            MAX_ROUTING_PROVENANCE_VERSION_LENGTH,
        )

    @model_validator(mode="after")
    def _identity_matches(self) -> "TaskRoutingContext":
        if self.mode is not self.task_key.mode:
            raise ValueError("routing mode must match trusted TaskKey mode")
        if self.user_goal != self.task_key.goal:
            raise ValueError("routing user_goal must match trusted TaskKey goal")
        return self

    @field_serializer("task_key")
    def _serialize_task_key(self, value: TaskKey) -> dict[str, Any]:
        return {
            "mediaId": value.media_id,
            "goal": value.goal,
            "mode": value.mode.value,
        }

    @property
    def media_id(self) -> int:
        return self.task_key.media_id


class RoutingSuggestion(_RoutingModel):
    """Untrusted router output; it never contains provider configuration."""

    suggested_lane: ModelRouteLane = Field(alias="suggestedLane")
    confidence: float
    routing_contract_version: str = Field(
        default=MODEL_ROUTING_CONTRACT_VERSION,
        alias="routingContractVersion",
    )
    classification_metadata: dict[str, str] = Field(
        default_factory=dict,
        alias="classificationMetadata",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_metadata_alias(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        normalized = dict(data)
        if "suggested_lane" not in normalized and "suggestedLane" not in normalized:
            if "lane" in normalized:
                normalized["suggested_lane"] = normalized.pop("lane")
        if "classification_metadata" not in normalized and "classificationMetadata" not in normalized:
            if "metadata" in normalized:
                normalized["classification_metadata"] = normalized.pop("metadata")
        return normalized

    @field_validator("suggested_lane", mode="before")
    @classmethod
    def _lane(cls, value: Any) -> ModelRouteLane:
        try:
            return value if isinstance(value, ModelRouteLane) else ModelRouteLane(value)
        except (TypeError, ValueError) as error:
            raise ValueError("suggested_lane is not a supported routing lane") from error

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, value: Any) -> float:
        return _finite_confidence(value)

    @field_validator("routing_contract_version")
    @classmethod
    def _contract_version(cls, value: str) -> str:
        return _bounded_text(
            value,
            "routing_contract_version",
            MAX_ROUTING_CONTRACT_VERSION_LENGTH,
        )

    @field_validator("classification_metadata", mode="before")
    @classmethod
    def _classification_metadata(cls, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("classification_metadata must be an object")
        if len(value) > MAX_ROUTING_CLASSIFICATION_ITEMS:
            raise ValueError("classification_metadata has too many items")
        normalized: dict[str, str] = {}
        forbidden_markers = (
            "apikey",
            "credential",
            "endpoint",
            "model",
            "provider",
            "temperature",
        )
        for key, item in value.items():
            bounded_key = _bounded_text(key, "classification_metadata key", MAX_ROUTING_CLASSIFICATION_KEY_LENGTH)
            normalized_key = bounded_key.replace("_", "").casefold()
            if any(marker in normalized_key for marker in forbidden_markers):
                raise ValueError("classification_metadata contains forbidden configuration")
            normalized[bounded_key] = _bounded_text(
                item,
                "classification_metadata value",
                MAX_ROUTING_CLASSIFICATION_VALUE_LENGTH,
            )
        return dict(sorted(normalized.items()))

    @property
    def metadata(self) -> Mapping[str, str]:
        return self.classification_metadata


ModelRoutingSuggestion = RoutingSuggestion


class ModelRouterPort(Protocol):
    """Provider-neutral proposal port; J1-B may adapt Jev to this port."""

    def route(
        self,
        context: TaskRoutingContext,
    ) -> RoutingSuggestion | Mapping[str, Any] | Awaitable[RoutingSuggestion | Mapping[str, Any]]:
        ...


class ModelRoutingConfiguration(_RoutingModel):
    """Application-owned deterministic policy configuration."""

    adaptive_routing_enabled: StrictBool = Field(
        default=False,
        alias="adaptiveRoutingEnabled",
    )
    confidence_threshold: float = Field(
        default=DEFAULT_ROUTING_CONFIDENCE_THRESHOLD,
        alias="confidenceThreshold",
    )
    fast_enabled: StrictBool = Field(default=True, alias="fastEnabled")
    deep_enabled: StrictBool = Field(default=True, alias="deepEnabled")
    routing_contract_version: str = Field(
        default=MODEL_ROUTING_CONTRACT_VERSION,
        alias="routingContractVersion",
    )

    @model_validator(mode="before")
    @classmethod
    def _enabled_alias(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        normalized = dict(data)
        if "adaptive_routing_enabled" not in normalized and "adaptiveRoutingEnabled" not in normalized:
            if "enabled" in normalized:
                normalized["adaptive_routing_enabled"] = normalized.pop("enabled")
        return normalized

    @field_validator("confidence_threshold", mode="before")
    @classmethod
    def _threshold(cls, value: Any) -> float:
        return _finite_confidence(value, "confidence_threshold")

    @field_validator("routing_contract_version")
    @classmethod
    def _version(cls, value: str) -> str:
        normalized = _bounded_text(
            value,
            "routing_contract_version",
            MAX_ROUTING_CONTRACT_VERSION_LENGTH,
        )
        if normalized != MODEL_ROUTING_CONTRACT_VERSION:
            raise ValueError("unsupported model routing contract version")
        return normalized

    @property
    def enabled_lanes(self) -> frozenset[ModelRouteLane]:
        lanes = {ModelRouteLane.BALANCED}
        if self.fast_enabled:
            lanes.add(ModelRouteLane.FAST)
        if self.deep_enabled:
            lanes.add(ModelRouteLane.DEEP)
        return frozenset(lanes)

    @property
    def enabled(self) -> bool:
        """Compatibility spelling for the adaptive feature flag."""

        return self.adaptive_routing_enabled


class ModelRoutingDecision(_RoutingModel):
    """The policy-owned final logical route, safe to serialize/reuse later."""

    lane: ModelRouteLane
    confidence: float
    fallback_used: StrictBool = Field(alias="fallbackUsed")
    reason_code: ModelRoutingReasonCode = Field(alias="reasonCode")
    routing_contract_version: str = Field(alias="routingContractVersion")
    suggested_lane: ModelRouteLane | None = Field(default=None, alias="suggestedLane")

    @field_validator("lane", "suggested_lane", mode="before")
    @classmethod
    def _decision_lane(cls, value: Any) -> ModelRouteLane | None:
        if value is None:
            return None
        try:
            return value if isinstance(value, ModelRouteLane) else ModelRouteLane(value)
        except (TypeError, ValueError) as error:
            raise ValueError("routing decision lane is invalid") from error

    @field_validator("confidence", mode="before")
    @classmethod
    def _decision_confidence(cls, value: Any) -> float:
        return _finite_confidence(value)

    @field_validator("reason_code", mode="before")
    @classmethod
    def _reason(cls, value: Any) -> ModelRoutingReasonCode:
        try:
            return value if isinstance(value, ModelRoutingReasonCode) else ModelRoutingReasonCode(value)
        except (TypeError, ValueError) as error:
            raise ValueError("routing reason code is invalid") from error

    @field_validator("routing_contract_version")
    @classmethod
    def _decision_version(cls, value: str) -> str:
        return _bounded_text(value, "routing_contract_version", MAX_ROUTING_CONTRACT_VERSION_LENGTH)

    @model_validator(mode="after")
    def _decision_branch(self) -> "ModelRoutingDecision":
        if self.fallback_used and self.lane is not ModelRouteLane.BALANCED:
            raise ValueError("fallback routing decisions must select BALANCED")
        if (
            self.reason_code is ModelRoutingReasonCode.ROUTER_ACCEPTED
            and self.fallback_used
        ):
            raise ValueError("accepted routing decisions cannot be fallbacks")
        return self

    @classmethod
    def balanced(
        cls,
        *,
        confidence: float = 0.0,
        reason_code: ModelRoutingReasonCode,
        routing_contract_version: str = MODEL_ROUTING_CONTRACT_VERSION,
        suggested_lane: ModelRouteLane | None = None,
    ) -> "ModelRoutingDecision":
        return cls(
            lane=ModelRouteLane.BALANCED,
            confidence=_finite_confidence(confidence),
            fallbackUsed=True,
            reasonCode=reason_code,
            routingContractVersion=routing_contract_version,
            suggestedLane=suggested_lane,
        )


RoutingDecision = ModelRoutingDecision


class ModelProfile(_RoutingModel):
    """Logical application profile; it intentionally has no provider fields."""

    profile_id: str = Field(alias="profileId")
    lane: ModelRouteLane

    @field_validator("profile_id")
    @classmethod
    def _profile_id(cls, value: str) -> str:
        return _bounded_text(value, "profile_id", MAX_ROUTING_PROFILE_ID_LENGTH)

    @field_validator("lane", mode="before")
    @classmethod
    def _profile_lane(cls, value: Any) -> ModelRouteLane:
        try:
            return value if isinstance(value, ModelRouteLane) else ModelRouteLane(value)
        except (TypeError, ValueError) as error:
            raise ValueError("model profile lane is invalid") from error


class ModelRouteHistory(_RoutingModel):
    """Bounded historical projection of one policy-owned route decision.

    ``ModelRoutingDecision`` remains the operational decision used by the
    live AgentLoop.  This DTO adds only the application-resolved profile and
    model identities required to explain a historical execution later.  It
    deliberately contains no provider endpoint, credential, prompt, or raw
    router response.
    """

    lane: ModelRouteLane
    confidence: float
    fallback_used: StrictBool = Field(alias="fallbackUsed")
    reason_code: ModelRoutingReasonCode = Field(alias="reasonCode")
    suggested_lane: ModelRouteLane | None = Field(default=None, alias="suggestedLane")
    routing_contract_version: str = Field(alias="routingContractVersion")
    profile_id: str = Field(alias="profileId")
    resolved_model_id: str = Field(alias="resolvedModelId")

    @field_validator("lane", "suggested_lane", mode="before")
    @classmethod
    def _lane(cls, value: Any) -> ModelRouteLane | None:
        if value is None:
            return None
        try:
            return value if isinstance(value, ModelRouteLane) else ModelRouteLane(value)
        except (TypeError, ValueError) as error:
            raise ValueError("historical route lane is invalid") from error

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, value: Any) -> float:
        return _finite_confidence(value)

    @field_validator("reason_code", mode="before")
    @classmethod
    def _reason(cls, value: Any) -> ModelRoutingReasonCode:
        try:
            return value if isinstance(value, ModelRoutingReasonCode) else ModelRoutingReasonCode(value)
        except (TypeError, ValueError) as error:
            raise ValueError("historical route reason is invalid") from error

    @field_validator("routing_contract_version")
    @classmethod
    def _routing_version(cls, value: str) -> str:
        return _bounded_text(
            value,
            "routing_contract_version",
            MAX_ROUTING_CONTRACT_VERSION_LENGTH,
        )

    @field_validator("profile_id")
    @classmethod
    def _profile(cls, value: str) -> str:
        return _bounded_text(value, "profile_id", MAX_ROUTING_PROFILE_ID_LENGTH)

    @field_validator("resolved_model_id")
    @classmethod
    def _resolved_model(cls, value: str) -> str:
        return _bounded_text(
            value,
            "resolved_model_id",
            MAX_ROUTING_RESOLVED_MODEL_ID_LENGTH,
        )

    @model_validator(mode="after")
    def _decision_branch(self) -> "ModelRouteHistory":
        if self.fallback_used and self.lane is not ModelRouteLane.BALANCED:
            raise ValueError("historical fallback routes must select BALANCED")
        if self.reason_code is ModelRoutingReasonCode.ROUTER_ACCEPTED and self.fallback_used:
            raise ValueError("historical accepted routes cannot be fallbacks")
        return self

    @classmethod
    def from_decision(
        cls,
        decision: ModelRoutingDecision,
        profile: ModelProfile,
        resolved_model_id: str,
    ) -> "ModelRouteHistory":
        if not isinstance(decision, ModelRoutingDecision):
            raise TypeError("decision must be a ModelRoutingDecision")
        if not isinstance(profile, ModelProfile):
            raise TypeError("profile must be a ModelProfile")
        if profile.lane is not decision.lane:
            raise RoutingContractError("model profile lane does not match route decision")
        return cls(
            lane=decision.lane,
            confidence=decision.confidence,
            fallbackUsed=decision.fallback_used,
            reasonCode=decision.reason_code,
            suggestedLane=decision.suggested_lane,
            routingContractVersion=decision.routing_contract_version,
            profileId=profile.profile_id,
            resolvedModelId=resolved_model_id,
        )

    def to_decision(self) -> ModelRoutingDecision:
        """Recover the exact policy decision without re-running policy."""

        return ModelRoutingDecision(
            lane=self.lane,
            confidence=self.confidence,
            fallbackUsed=self.fallback_used,
            reasonCode=self.reason_code,
            routingContractVersion=self.routing_contract_version,
            suggestedLane=self.suggested_lane,
        )


_DEFAULT_MODEL_PROFILES = MappingProxyType(
    {
        ModelRouteLane.FAST: ModelProfile(profileId="fast-profile", lane=ModelRouteLane.FAST),
        ModelRouteLane.BALANCED: ModelProfile(
            profileId="balanced-profile",
            lane=ModelRouteLane.BALANCED,
        ),
        ModelRouteLane.DEEP: ModelProfile(profileId="deep-profile", lane=ModelRouteLane.DEEP),
    }
)


class ModelProfileRegistry:
    """Static explicit lane-to-logical-profile mapping."""

    def __init__(
        self,
        profiles: Mapping[ModelRouteLane | str, ModelProfile | Mapping[str, Any]] | None = None,
    ) -> None:
        selected = _DEFAULT_MODEL_PROFILES if profiles is None else profiles
        normalized: dict[ModelRouteLane, ModelProfile] = {}
        for raw_lane, raw_profile in selected.items():
            try:
                lane = raw_lane if isinstance(raw_lane, ModelRouteLane) else ModelRouteLane(raw_lane)
            except (TypeError, ValueError) as error:
                raise RoutingContractError("model profile registry contains an unknown lane") from error
            profile = (
                raw_profile
                if isinstance(raw_profile, ModelProfile)
                else ModelProfile.model_validate(raw_profile)
            )
            if profile.lane is not lane:
                raise RoutingContractError("model profile lane does not match registry lane")
            normalized[lane] = profile
        if set(normalized) != set(ModelRouteLane):
            raise RoutingContractError("model profile registry must contain exactly V1 lanes")
        self._profiles = MappingProxyType(normalized)

    @classmethod
    def default(cls) -> "ModelProfileRegistry":
        return cls()

    def resolve(self, lane: ModelRouteLane | str) -> ModelProfile:
        try:
            normalized = lane if isinstance(lane, ModelRouteLane) else ModelRouteLane(lane)
        except (TypeError, ValueError) as error:
            raise RoutingContractError("model profile lane is unknown") from error
        return self._profiles[normalized]

    get = resolve
    profile_for = resolve
    resolve_profile = resolve

    def lanes(self) -> tuple[ModelRouteLane, ...]:
        return tuple(ModelRouteLane)

    def profiles(self) -> tuple[ModelProfile, ...]:
        return tuple(self._profiles[lane] for lane in ModelRouteLane)

    def register(self, *_: Any, **__: Any) -> None:
        raise RoutingContractError("model profile registry is static")


ModelProfileResolver = ModelProfileRegistry


class ModelRoutingPolicy:
    """Deterministic authority over untrusted router suggestions."""

    def __init__(
        self,
        configuration: ModelRoutingConfiguration | Mapping[str, Any] | None = None,
        *,
        config: ModelRoutingConfiguration | Mapping[str, Any] | None = None,
        adaptive_routing_enabled: bool | None = None,
        enabled: bool | None = None,
        confidence_threshold: float | None = None,
        fast_enabled: bool | None = None,
        deep_enabled: bool | None = None,
    ) -> None:
        if configuration is not None and config is not None:
            raise TypeError("configuration and config are mutually exclusive")
        selected = configuration if configuration is not None else config
        keyword_values = {
            key: value
            for key, value in {
                "adaptive_routing_enabled": adaptive_routing_enabled,
                "enabled": enabled,
                "confidence_threshold": confidence_threshold,
                "fast_enabled": fast_enabled,
                "deep_enabled": deep_enabled,
            }.items()
            if value is not None
        }
        if selected is not None and keyword_values:
            raise TypeError("configuration values cannot be mixed with policy keyword values")
        if selected is None and keyword_values:
            selected = keyword_values
        self.configuration = (
            selected
            if isinstance(selected, ModelRoutingConfiguration)
            else ModelRoutingConfiguration.model_validate(selected or {})
        )

    def validate_context(self, context: TaskRoutingContext | Mapping[str, Any]) -> TaskRoutingContext:
        if isinstance(context, TaskRoutingContext):
            return context
        try:
            return TaskRoutingContext.model_validate(context)
        except Exception as error:
            raise InvalidRoutingContextError("routing context is invalid") from error

    def decide(
        self,
        context: TaskRoutingContext | Mapping[str, Any],
        suggestion: RoutingSuggestion | Mapping[str, Any] | Any,
    ) -> ModelRoutingDecision:
        self.validate_context(context)
        parsed, confidence, suggested_lane = _parse_suggestion(suggestion)

        if not self.configuration.adaptive_routing_enabled:
            return self._balanced(
                ModelRoutingReasonCode.ROUTING_DISABLED,
                confidence=confidence,
                suggested_lane=suggested_lane,
            )
        if parsed is None:
            return self._balanced(
                ModelRoutingReasonCode.INVALID_SUGGESTION,
                confidence=confidence,
                suggested_lane=suggested_lane,
            )
        if parsed.routing_contract_version != self.configuration.routing_contract_version:
            return self._balanced(
                ModelRoutingReasonCode.INVALID_SUGGESTION,
                confidence=parsed.confidence,
                suggested_lane=parsed.suggested_lane,
            )
        if parsed.confidence < self.configuration.confidence_threshold:
            return self._balanced(
                ModelRoutingReasonCode.LOW_CONFIDENCE,
                confidence=parsed.confidence,
                suggested_lane=parsed.suggested_lane,
            )
        if parsed.suggested_lane not in self.configuration.enabled_lanes:
            return self._balanced(
                ModelRoutingReasonCode.LANE_DISABLED,
                confidence=parsed.confidence,
                suggested_lane=parsed.suggested_lane,
            )
        return ModelRoutingDecision(
            lane=parsed.suggested_lane,
            confidence=parsed.confidence,
            fallbackUsed=False,
            reasonCode=ModelRoutingReasonCode.ROUTER_ACCEPTED,
            routingContractVersion=self.configuration.routing_contract_version,
            suggestedLane=parsed.suggested_lane,
        )

    apply = decide
    resolve = decide
    evaluate = decide

    def fallback(
        self,
        reason_code: ModelRoutingReasonCode,
        *,
        suggestion: RoutingSuggestion | Mapping[str, Any] | Any = None,
    ) -> ModelRoutingDecision:
        parsed, confidence, suggested_lane = _parse_suggestion(suggestion)
        if parsed is not None:
            confidence = parsed.confidence
            suggested_lane = parsed.suggested_lane
        return self._balanced(
            reason_code,
            confidence=confidence,
            suggested_lane=suggested_lane,
        )

    def _balanced(
        self,
        reason_code: ModelRoutingReasonCode,
        *,
        confidence: float = 0.0,
        suggested_lane: ModelRouteLane | None = None,
    ) -> ModelRoutingDecision:
        return ModelRoutingDecision.balanced(
            confidence=confidence,
            reason_code=reason_code,
            routing_contract_version=self.configuration.routing_contract_version,
            suggested_lane=suggested_lane,
        )


def _safe_confidence(value: Any) -> float:
    try:
        return _finite_confidence(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_lane(value: Any) -> ModelRouteLane | None:
    try:
        return value if isinstance(value, ModelRouteLane) else ModelRouteLane(value)
    except (TypeError, ValueError):
        return None


def _parse_suggestion(
    value: Any,
) -> tuple[RoutingSuggestion | None, float, ModelRouteLane | None]:
    if value is None:
        return None, 0.0, None
    if isinstance(value, RoutingSuggestion):
        return value, value.confidence, value.suggested_lane
    raw_confidence = value.get("confidence") if isinstance(value, Mapping) else getattr(value, "confidence", None)
    raw_lane = (
        value.get("suggested_lane", value.get("suggestedLane"))
        if isinstance(value, Mapping)
        else getattr(value, "suggested_lane", getattr(value, "suggestedLane", None))
    )
    confidence = _safe_confidence(raw_confidence)
    lane = _safe_lane(raw_lane)
    try:
        parsed = RoutingSuggestion.model_validate(value)
    except Exception:
        return None, confidence, lane
    return parsed, parsed.confidence, parsed.suggested_lane


class ModelRoutingService:
    """One-route application orchestration with stable-decision reuse."""

    def __init__(
        self,
        router: ModelRouterPort | Any | None,
        *,
        policy: ModelRoutingPolicy | None = None,
        configuration: ModelRoutingConfiguration | Mapping[str, Any] | None = None,
        profile_registry: ModelProfileRegistry | None = None,
        profiles: ModelProfileRegistry | None = None,
    ) -> None:
        if policy is not None and configuration is not None:
            raise TypeError("policy and configuration are mutually exclusive")
        self.policy = policy or ModelRoutingPolicy(configuration)
        self.router = router
        self.profile_registry = profile_registry or profiles or ModelProfileRegistry.default()

    async def route(
        self,
        context: TaskRoutingContext | Mapping[str, Any],
        *,
        existing_decision: ModelRoutingDecision | Mapping[str, Any] | None = None,
    ) -> ModelRoutingDecision:
        trusted_context = self.policy.validate_context(context)
        if existing_decision is not None:
            return self._reuse_decision(existing_decision)
        if not self.policy.configuration.adaptive_routing_enabled:
            return self.policy.fallback(ModelRoutingReasonCode.ROUTING_DISABLED)

        route_method = getattr(self.router, "route", None)
        if not callable(route_method):
            return self.policy.fallback(ModelRoutingReasonCode.ROUTER_UNAVAILABLE)
        try:
            raw_suggestion = route_method(trusted_context)
            if inspect.isawaitable(raw_suggestion):
                raw_suggestion = await raw_suggestion
        except InvalidRoutingSuggestionError:
            return self.policy.fallback(ModelRoutingReasonCode.INVALID_SUGGESTION)
        except TimeoutError:
            return self.policy.fallback(ModelRoutingReasonCode.ROUTER_UNAVAILABLE)
        except Exception:
            return self.policy.fallback(ModelRoutingReasonCode.ROUTER_ERROR)
        return self.policy.decide(trusted_context, raw_suggestion)

    route_once = route

    def resolve_profile(self, decision: ModelRoutingDecision | Mapping[str, Any]) -> ModelProfile:
        parsed = self._reuse_decision(decision)
        return self.profile_registry.resolve(parsed.lane)

    async def route_profile(
        self,
        context: TaskRoutingContext | Mapping[str, Any],
        *,
        existing_decision: ModelRoutingDecision | Mapping[str, Any] | None = None,
    ) -> tuple[ModelRoutingDecision, ModelProfile]:
        decision = await self.route(context, existing_decision=existing_decision)
        return decision, self.resolve_profile(decision)

    def _reuse_decision(
        self,
        value: ModelRoutingDecision | Mapping[str, Any],
    ) -> ModelRoutingDecision:
        try:
            decision = (
                value
                if isinstance(value, ModelRoutingDecision)
                else ModelRoutingDecision.model_validate(value)
            )
        except Exception as error:
            raise UnsupportedRoutingContractError("routing decision is invalid") from error
        if decision.routing_contract_version != self.policy.configuration.routing_contract_version:
            raise UnsupportedRoutingContractError("routing decision contract is unsupported")
        # Reuse returns the exact immutable decision.  J1-C may persist and
        # reload this object; J1-A must not introduce a second route or mutate
        # the reason merely because recovery occurred.
        return decision


AdaptiveModelRoutingService = ModelRoutingService
DeterministicModelRoutingService = ModelRoutingService
RoutingPolicy = ModelRoutingPolicy
ModelRouteReasonCode = ModelRoutingReasonCode
RoutingReasonCode = ModelRoutingReasonCode
RouteLane = ModelRouteLane
ModelRoutingConfig = ModelRoutingConfiguration


__all__ = [
    "AdaptiveModelRoutingService",
    "DEFAULT_ROUTING_CONFIDENCE_THRESHOLD",
    "DeterministicModelRoutingService",
    "InvalidRoutingContextError",
    "InvalidRoutingSuggestionError",
    "MAX_ROUTING_CHUNK_COUNT",
    "MAX_ROUTING_CLASSIFICATION_ITEMS",
    "MAX_ROUTING_CLASSIFICATION_KEY_LENGTH",
    "MAX_ROUTING_CLASSIFICATION_VALUE_LENGTH",
    "MAX_ROUTING_CONTRACT_VERSION_LENGTH",
    "MAX_ROUTING_GOAL_LENGTH",
    "MAX_ROUTING_MEDIA_DURATION_MS",
    "MAX_ROUTING_PROFILE_ID_LENGTH",
    "MAX_ROUTING_RESOLVED_MODEL_ID_LENGTH",
    "MAX_ROUTING_PROVENANCE_VERSION_LENGTH",
    "MAX_ROUTING_SEGMENT_COUNT",
    "MAX_ROUTING_SOURCE_REVISION_LENGTH",
    "MODEL_ROUTING_CONTRACT_VERSION",
    "ModelProfile",
    "ModelProfileRegistry",
    "ModelProfileResolver",
    "ModelRouteHistory",
    "ModelRouteLane",
    "ModelRouteReasonCode",
    "ModelRouterPort",
    "ModelRoutingConfiguration",
    "ModelRoutingDecision",
    "ModelRoutingPolicy",
    "ModelRoutingReasonCode",
    "ModelRoutingService",
    "ModelRoutingSuggestion",
    "RoutingContractError",
    "RoutingDecision",
    "RoutingReasonCode",
    "RoutingPolicy",
    "RoutingSuggestion",
    "RouteLane",
    "TaskRoutingContext",
    "UnsupportedRoutingContractError",
    "ModelRoutingConfig",
]
