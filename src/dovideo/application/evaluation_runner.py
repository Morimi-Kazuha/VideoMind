"""Offline X3-B evaluation runner and deterministic metric collection.

This module is deliberately an evaluation boundary.  It does not submit a
normal user task, enqueue Celery work, call a provider by itself, or reuse a
historical replay.  A caller supplies a controlled execution adapter that
uses the normal AgentLoop composition and a prepared, immutable source
artifact.  The runner owns the case/trial identity, measurement spans,
failure preservation, and artifact persistence.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import subprocess
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace as dataclass_replace
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from dovideo.domain import (
    AgentState,
    AnalysisMode,
    AnalysisResult,
    ModeProfile,
    VideoContext,
)

from .agent_policy import is_result_valid, missing_section_keys
from .evidence import EvidenceVerificationService
from .evaluation_contracts import (
    DEFAULT_RUNNER_CONFIG_VERSION,
    EVALUATION_CONTRACT_VERSION,
    ColdWarmMarker,
    CostMeasurement,
    CriticMetrics,
    DataClassification,
    DatasetCompleteness,
    DeterministicMetrics,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationContractError,
    EvaluationExecutionInput,
    EvaluationExclusionCategory,
    EvaluationFailureCategory,
    EvaluationQueryCategory,
    EvaluationResultStatus,
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationStrategy,
    ExpectedEvidenceMatchLevel,
    ExpectedEvidenceRef,
    LatencyMeasurement,
    MeasurementState,
    RoutingMeasurement,
    TokenUsageMeasurement,
    TokenStageUsage,
    ToolMetrics,
    WorkingTreeState,
    canonical_json,
    case_result_from_json,
    case_results_from_jsonl,
    validate_case_result_identities,
    validate_dataset,
)
from .mode_profiles import mode_profile_for
from .model_routing import ModelRouteLane
from .routing_experiments import RuleRouterV1, rule_router_signals_from_execution


DEFAULT_RETRIEVAL_K_VALUES = (1, 3, 5)
MAX_TRIAL_COUNT = 100
MAX_TIMEOUT_SECONDS = 86_400.0
MAX_REASON_TEXT = 160
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EvaluationPreflightError(EvaluationContractError):
    """The run cannot safely start with the supplied evaluation inputs."""


class EvaluationDuplicateResultError(EvaluationContractError):
    """A result identity was already persisted with a different digest."""


class EvaluationExecutionError(RuntimeError):
    """Base class for bounded adapter failures used by the failure mapper."""


class EvaluationProviderError(EvaluationExecutionError):
    """A controlled execution adapter observed a provider failure."""


class EvaluationRetrievalError(EvaluationExecutionError):
    """A controlled execution adapter observed a retrieval failure."""


class EvaluationToolError(EvaluationExecutionError):
    """A controlled execution adapter observed a tool failure."""


class EvaluationEvidenceGuardError(EvaluationExecutionError):
    """A controlled execution adapter observed an Evidence Guard failure."""


class EvaluationSchemaError(EvaluationExecutionError):
    """A controlled execution adapter observed invalid final output."""


class _StrictEvaluationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        validate_default=True,
        arbitrary_types_allowed=True,
    )


class EvaluationRunnerConfig(_StrictEvaluationModel):
    """Versioned, bounded configuration for one evaluation run."""

    runner_config_version: str = Field(
        default=DEFAULT_RUNNER_CONFIG_VERSION,
        alias="runnerConfigVersion",
    )
    strategy: EvaluationStrategy = EvaluationStrategy.CURRENT_PRODUCTION
    trial_count: int = Field(default=1, alias="trialCount")
    case_filter: tuple[str, ...] = Field(default=(), alias="caseFilter")
    cold_warm: ColdWarmMarker = Field(
        default=ColdWarmMarker.UNSPECIFIED,
        alias="coldWarm",
    )
    timeout_seconds: float = Field(default=900.0, alias="timeoutSeconds")
    tools_enabled: StrictBool | None = Field(default=None, alias="toolsEnabled")
    critic_enabled: StrictBool | None = Field(default=None, alias="criticEnabled")
    pricing_version: str | None = Field(default=None, alias="pricingVersion")
    artifact_output: str | None = Field(default=None, alias="artifactOutput")
    retrieval_k_values: tuple[int, ...] = Field(
        default=DEFAULT_RETRIEVAL_K_VALUES,
        alias="retrievalKValues",
    )
    max_executions: int | None = Field(default=None, alias="maxExecutions")
    publish: StrictBool = False

    @field_validator("runner_config_version", mode="before")
    @classmethod
    def _version(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("runner_config_version must be non-blank text")
        normalized = value.strip()
        if len(normalized) > 96 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", normalized):
            raise ValueError("runner_config_version has invalid spelling")
        return normalized

    @field_validator("trial_count", mode="before")
    @classmethod
    def _trial_count(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_TRIAL_COUNT:
            raise ValueError(f"trial_count must be an integer in [1, {MAX_TRIAL_COUNT}]")
        return value

    @field_validator("case_filter", mode="before")
    @classmethod
    def _case_filter(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes, bytearray, Mapping)):
            raise ValueError("case_filter must be a collection of case IDs")
        try:
            values = tuple(value)
        except TypeError as error:
            raise ValueError("case_filter must be a collection of case IDs") from error
        output: list[str] = []
        seen: set[str] = set()
        for item in values:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("case_filter IDs must be non-blank text")
            normalized = item.strip()
            if normalized in seen:
                raise ValueError("case_filter contains duplicate case IDs")
            seen.add(normalized)
            output.append(normalized)
        return tuple(output)

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _timeout(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("timeout_seconds must be numeric")
        normalized = float(value)
        if not math.isfinite(normalized) or not 0 < normalized <= MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds is outside its positive bound")
        return normalized

    @field_validator("pricing_version", mode="before")
    @classmethod
    def _pricing_version(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("pricing_version must be non-blank text")
        normalized = value.strip()
        if len(normalized) > 96 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", normalized):
            raise ValueError("pricing_version has invalid spelling")
        return normalized

    @field_validator("artifact_output", mode="before")
    @classmethod
    def _artifact_output(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError("artifact_output must be a non-blank path")
        return str(value)

    @field_validator("retrieval_k_values", mode="before")
    @classmethod
    def _k_values(cls, value: Any) -> tuple[int, ...]:
        if value is None:
            return DEFAULT_RETRIEVAL_K_VALUES
        if isinstance(value, (str, bytes, bytearray, Mapping)):
            raise ValueError("retrieval_k_values must be a collection")
        values = tuple(value)
        if not values:
            raise ValueError("retrieval_k_values must not be empty")
        output: list[int] = []
        for item in values:
            if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
                raise ValueError("retrieval K values must be positive integers")
            if item not in output:
                output.append(item)
        return tuple(sorted(output))

    @field_validator("max_executions", mode="before")
    @classmethod
    def _max_executions(cls, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("max_executions must be a positive integer")
        return value

    def fingerprint(self) -> str:
        """Return the stable SHA-256 identity of the normalized config."""

        from hashlib import sha256

        payload = self.model_dump(mode="json", by_alias=True, exclude_none=False)
        return sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class PricingRateAvailability(str, Enum):
    """Whether the entry has one scalar rate, conditional rates, or no price."""

    AVAILABLE = "AVAILABLE"
    CONDITIONAL = "CONDITIONAL"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class ConditionalPricingRate(_StrictEvaluationModel):
    """One provider-published input/output rate pair under explicit conditions."""

    period: str
    input_cache: str = Field(alias="inputCache")
    input_rate_per_million: float = Field(alias="inputRatePerMillion")
    output_rate_per_million: float = Field(alias="outputRatePerMillion")

    @field_validator("period", mode="before")
    @classmethod
    def _period(cls, value: Any) -> str:
        if value not in {"PEAK", "OFF_PEAK"}:
            raise ValueError("pricing period must be PEAK or OFF_PEAK")
        return value

    @field_validator("input_cache", mode="before")
    @classmethod
    def _input_cache(cls, value: Any) -> str:
        if value not in {"HIT", "MISS", "NOT_APPLICABLE"}:
            raise ValueError("input_cache must be HIT, MISS, or NOT_APPLICABLE")
        return value

    @field_validator("input_rate_per_million", "output_rate_per_million", mode="before")
    @classmethod
    def _conditional_rate(cls, value: Any, info: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{info.field_name} must be numeric")
        normalized = float(value)
        if not math.isfinite(normalized) or normalized < 0:
            raise ValueError(f"{info.field_name} must be finite and non-negative")
        return normalized


class PricingEntry(_StrictEvaluationModel):
    """Provider/model prices, retaining conditions that prevent scalar estimates."""

    provider: str
    model: str
    input_rate_per_million: float | None = Field(alias="inputRatePerMillion")
    output_rate_per_million: float | None = Field(alias="outputRatePerMillion")
    currency: str = "USD"
    rate_availability: PricingRateAvailability = Field(
        default=PricingRateAvailability.AVAILABLE,
        alias="rateAvailability",
    )
    conditional_rates: tuple[ConditionalPricingRate, ...] = Field(
        default=(),
        alias="conditionalRates",
    )
    source_identity: str | None = Field(default=None, alias="sourceIdentity")
    source_reference: str | None = Field(default=None, alias="sourceReference")
    source_date: date | None = Field(default=None, alias="sourceDate")
    effective_date: date | None = Field(default=None, alias="effectiveDate")
    pricing_note: str | None = Field(default=None, alias="pricingNote")

    @field_validator("provider", "model", "currency", mode="before")
    @classmethod
    def _text(cls, value: Any, info: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{info.field_name} must be non-blank text")
        return value.strip()

    @field_validator("input_rate_per_million", "output_rate_per_million", mode="before")
    @classmethod
    def _rate(cls, value: Any, info: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{info.field_name} must be numeric")
        normalized = float(value)
        if not math.isfinite(normalized) or normalized < 0:
            raise ValueError(f"{info.field_name} must be finite and non-negative")
        return normalized

    @field_validator("source_identity", "source_reference", "pricing_note", mode="before")
    @classmethod
    def _optional_source_text(cls, value: Any, info: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{info.field_name} must be non-blank text when supplied")
        return value.strip()

    @model_validator(mode="after")
    def _availability_matches_rates(self) -> "PricingEntry":
        scalar_rates = (
            self.input_rate_per_million is not None
            and self.output_rate_per_million is not None
        )
        if self.rate_availability is PricingRateAvailability.AVAILABLE:
            if not scalar_rates or self.conditional_rates:
                raise ValueError("AVAILABLE pricing requires one scalar rate pair")
        elif self.rate_availability is PricingRateAvailability.CONDITIONAL:
            if scalar_rates or not self.conditional_rates:
                raise ValueError("CONDITIONAL pricing requires only conditional rate rows")
        elif scalar_rates or self.input_rate_per_million is not None or self.output_rate_per_million is not None or self.conditional_rates:
            raise ValueError("NOT_AVAILABLE pricing cannot contain rates")
        return self

    @property
    def identity(self) -> tuple[str, str]:
        return (self.provider, self.model)


class PricingCatalog(_StrictEvaluationModel):
    """Versioned lookup table used only by evaluation cost calculation."""

    pricing_version: str = Field(alias="pricingVersion")
    entries: tuple[PricingEntry, ...] = ()

    @field_validator("pricing_version", mode="before")
    @classmethod
    def _version(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("pricing_version must be non-blank text")
        return value.strip()

    @model_validator(mode="after")
    def _unique_entries(self) -> "PricingCatalog":
        identities = [entry.identity for entry in self.entries]
        if len(set(identities)) != len(identities):
            raise ValueError("pricing catalog contains duplicate provider/model entries")
        return self

    def lookup(self, provider: str | None, model: str | None) -> PricingEntry | None:
        if not provider or not model:
            return None
        for entry in self.entries:
            if entry.provider == provider and entry.model == model:
                return entry
        return None

    def canonical_json(self) -> str:
        return canonical_json(self)

    @property
    def digest(self) -> str:
        from hashlib import sha256

        return sha256(self.canonical_json().encode("utf-8")).hexdigest()


class EvaluationRetrievedEvidence(_StrictEvaluationModel):
    """Bounded provenance projection emitted by retrieval/tool instrumentation."""

    source_revision: str = Field(alias="sourceRevision")
    source_item_id: str | None = Field(default=None, alias="sourceItemId")
    segment_id: str | None = Field(default=None, alias="segmentId")
    source_type: str | None = Field(default=None, alias="sourceType")
    timestamp_ms: int | None = Field(default=None, alias="timestampMs")
    start_ms: int | None = Field(default=None, alias="startMs")
    end_ms: int | None = Field(default=None, alias="endMs")
    rank: int | None = None

    @field_validator("source_revision", mode="before")
    @classmethod
    def _revision(cls, value: Any) -> str:
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value.strip()) is None:
            raise ValueError("source_revision must be a lowercase SHA-256 digest")
        return value.strip()

    @field_validator("source_item_id", "segment_id", "source_type", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("provenance text must be non-blank")
        return value.strip()

    @field_validator("timestamp_ms", "start_ms", "end_ms", "rank", mode="before")
    @classmethod
    def _nonnegative(cls, value: Any, info: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{info.field_name} must be a non-negative integer")
        if info.field_name == "rank" and value == 0:
            raise ValueError("rank must be positive")
        return value

    @model_validator(mode="after")
    def _interval(self) -> "EvaluationRetrievedEvidence":
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("start_ms and end_ms must be supplied together")
        if self.start_ms is not None and self.end_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("retrieved evidence interval must be positive")
        return self


class EvaluationSourceArtifact(_StrictEvaluationModel):
    """Prepared immutable source material for a case evaluation."""

    media_ref: str = Field(alias="mediaRef")
    source_revision: str = Field(alias="sourceRevision")
    context: VideoContext | None = None
    chunks: tuple[Any, ...] = ()
    vectors: tuple[Any, ...] = ()
    source_types: tuple[str, ...] = Field(default=(), alias="sourceTypes")
    artifact_id: str | None = Field(default=None, alias="artifactId")
    available: bool = True
    evaluation_contract_version: str = Field(
        default=EVALUATION_CONTRACT_VERSION,
        alias="evaluationContractVersion",
    )

    @field_validator("media_ref", mode="before")
    @classmethod
    def _media(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip() or ".." in re.split(r"[\\/]", value.strip()):
            raise ValueError("media_ref is invalid")
        return value.strip()

    @field_validator("source_revision", mode="before")
    @classmethod
    def _revision(cls, value: Any) -> str:
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value.strip()) is None:
            raise ValueError("source_revision must be a lowercase SHA-256 digest")
        return value.strip()

    @field_validator("source_types", mode="before")
    @classmethod
    def _source_types(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        return tuple(
            str(getattr(item, "value", item)).strip().upper()
            for item in value
            if str(getattr(item, "value", item)).strip()
        )


@dataclass(frozen=True, slots=True)
class EvaluationExecutionObservation:
    """Non-persisted adapter observation used to build a CaseResult."""

    result: AnalysisResult | None = None
    agent_state: AgentState | None = None
    retrieved_evidence: tuple[EvaluationRetrievedEvidence, ...] = ()
    final_evidence: tuple[EvaluationRetrievedEvidence, ...] = ()
    schema_valid: bool | None = None
    mode_sections_valid: bool | None = None
    evidence_guard_pass: bool | None = None
    evidence_support_rate: float | None = None
    unsupported_claim_rate: float | None = None
    route_decision: RoutingMeasurement = field(default_factory=RoutingMeasurement)
    token_usage: TokenUsageMeasurement = field(default_factory=TokenUsageMeasurement)
    cost: CostMeasurement = field(default_factory=CostMeasurement)
    latency: LatencyMeasurement = field(default_factory=LatencyMeasurement)
    tool_metrics: ToolMetrics = field(default_factory=ToolMetrics)
    critic_metrics: CriticMetrics = field(default_factory=CriticMetrics)
    execution_id: str | None = None
    resolved_model: str | None = None
    provider: str | None = None
    model: str | None = None
    provider_reported_cost: float | None = None
    planner_call_count: int | None = None
    executor_call_count: int | None = None


@runtime_checkable
class EvaluationSourceResolver(Protocol):
    def resolve(self, media_ref: str) -> EvaluationSourceArtifact | None:
        """Return prepared immutable material or ``None`` when unavailable."""


class MappingEvaluationSourceResolver:
    """Small deterministic resolver useful for controlled offline runs/tests."""

    def __init__(self, artifacts: Mapping[str, EvaluationSourceArtifact]) -> None:
        self._artifacts = dict(artifacts)

    def resolve(self, media_ref: str) -> EvaluationSourceArtifact | None:
        return self._artifacts.get(media_ref)


@runtime_checkable
class EvaluationExecutionAdapter(Protocol):
    async def execute(
        self,
        execution_input: Mapping[str, Any],
        *,
        artifact: EvaluationSourceArtifact,
        strategy: EvaluationStrategy,
        trial_index: int,
        timeout_seconds: float,
    ) -> EvaluationExecutionObservation:
        """Execute one fresh case/trial through the controlled application path."""


@runtime_checkable
class EvaluationExecutionStrategy(Protocol):
    name: EvaluationStrategy

    async def execute(
        self,
        execution_input: Mapping[str, Any],
        *,
        artifact: EvaluationSourceArtifact,
        trial_index: int,
        timeout_seconds: float,
    ) -> EvaluationExecutionObservation:
        """Run exactly one fresh evaluation execution."""


@dataclass(frozen=True, slots=True)
class AdapterEvaluationStrategy:
    """Named strategy wrapper; no result/planner/critic state is shared."""

    name: EvaluationStrategy
    adapter: EvaluationExecutionAdapter

    async def execute(
        self,
        execution_input: Mapping[str, Any],
        *,
        artifact: EvaluationSourceArtifact,
        trial_index: int,
        timeout_seconds: float,
    ) -> EvaluationExecutionObservation:
        return await self.adapter.execute(
            execution_input,
            artifact=artifact,
            strategy=self.name,
            trial_index=trial_index,
            timeout_seconds=timeout_seconds,
        )


class CurrentProductionEvaluationStrategy(AdapterEvaluationStrategy):
    def __init__(self, adapter: EvaluationExecutionAdapter) -> None:
        super().__init__(EvaluationStrategy.CURRENT_PRODUCTION, adapter)


class FixedBalancedEvaluationStrategy(AdapterEvaluationStrategy):
    def __init__(self, adapter: EvaluationExecutionAdapter) -> None:
        super().__init__(EvaluationStrategy.FIXED_BALANCED, adapter)


class RuleRouterEvaluationStrategy:
    """Evaluation-only lane dispatch driven by the frozen deterministic router."""

    name = EvaluationStrategy.RULE_ROUTER

    def __init__(
        self,
        lane_adapters: Mapping[ModelRouteLane | str, EvaluationExecutionAdapter],
        *,
        router: RuleRouterV1 | None = None,
    ) -> None:
        normalized: dict[ModelRouteLane, EvaluationExecutionAdapter] = {}
        for raw_lane, adapter in lane_adapters.items():
            try:
                lane = raw_lane if isinstance(raw_lane, ModelRouteLane) else ModelRouteLane(raw_lane)
            except (TypeError, ValueError) as error:
                raise ValueError("RULE_ROUTER lane adapter has an unknown lane") from error
            if lane in normalized:
                raise ValueError("RULE_ROUTER lane adapters contain a duplicate lane")
            if not callable(getattr(adapter, "execute", None)):
                raise TypeError("RULE_ROUTER lane adapters must provide execute()")
            normalized[lane] = adapter
        if set(normalized) != set(ModelRouteLane):
            raise ValueError("RULE_ROUTER requires FAST, BALANCED, and DEEP lane adapters")
        self._lane_adapters = normalized
        self._router = router or RuleRouterV1()

    @property
    def router_digest(self) -> str:
        return self._router.digest

    async def execute(
        self,
        execution_input: Mapping[str, Any],
        *,
        artifact: EvaluationSourceArtifact,
        trial_index: int,
        timeout_seconds: float,
    ) -> EvaluationExecutionObservation:
        signals = rule_router_signals_from_execution(execution_input, artifact)
        lane = self._router.route(signals)
        observation = await self._lane_adapters[lane].execute(
            execution_input,
            artifact=artifact,
            strategy=EvaluationStrategy.RULE_ROUTER,
            trial_index=trial_index,
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(observation, EvaluationExecutionObservation):
            raise TypeError("RULE_ROUTER lane adapter returned an invalid observation")
        observed_lane = observation.route_decision.resolved_lane
        if observed_lane is not None and observed_lane is not lane:
            raise EvaluationExecutionError("RULE_ROUTER lane adapter resolved a different lane")
        route_decision = RoutingMeasurement(
            suggested_lane=lane,
            resolved_lane=lane,
            fallback=False,
            resolved_model_id=(
                observation.route_decision.resolved_model_id
                or observation.model
                or observation.resolved_model
            ),
            measurement_state=MeasurementState.MEASURED,
        )
        return dataclass_replace(observation, route_decision=route_decision)


class AgentLoopEvaluationAdapter:
    """Evaluation-only bridge to the existing production AgentLoop port.

    The adapter receives only ``EvaluationCase.execution_input()``.  Gold
    evidence, required facts, reference answers, and difficulty never enter
    the execution call.  A production composition can inject its already
    prepared context and the normal AgentLoop instance without using Celery.
    """

    def __init__(
        self,
        agent_loop: Any,
        *,
        profile_for: Callable[[AnalysisMode], ModeProfile] = mode_profile_for,
        media_id_resolver: Callable[[str], int | None] | None = None,
        evidence_verifier: EvidenceVerificationService | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        self._agent_loop = agent_loop
        self._profile_for = profile_for
        self._media_id_resolver = media_id_resolver
        self._verifier = evidence_verifier or EvidenceVerificationService()
        self._provider = provider
        self._model = model

    async def execute(
        self,
        execution_input: Mapping[str, Any],
        *,
        artifact: EvaluationSourceArtifact,
        strategy: EvaluationStrategy,
        trial_index: int,
        timeout_seconds: float,
    ) -> EvaluationExecutionObservation:
        del strategy, trial_index, timeout_seconds
        if artifact.context is None:
            raise EvaluationPreflightError("prepared evaluation context is unavailable")
        mode = AnalysisMode.from_request(str(execution_input["mode"]))
        context = artifact.context.model_copy(
            update={"user_goal": str(execution_input["query"])}
        )
        profile = self._profile_for(mode)
        media_id = (
            None
            if self._media_id_resolver is None
            else self._media_id_resolver(str(execution_input["media_ref"]))
        )
        state = await self._agent_loop.run(context, media_id=media_id, profile=profile)
        result = state.result if isinstance(state, AgentState) else getattr(state, "result", None)
        if result is not None and not isinstance(result, AnalysisResult):
            result = AnalysisResult.model_validate(result)
        critique = None if state is None else getattr(state, "critique", None)
        call_count = max(1, int(getattr(state, "round", 0))) if critique is not None else 0
        critic = CriticMetrics(
            first_pass=(bool(critique.passed) if critique is not None and call_count <= 1 else None),
            final_pass=(bool(critique.passed) if critique is not None else None),
            additional_rounds=max(0, call_count - 1) if critique is not None else None,
            critic_call_count=call_count,
            measurement_state=MeasurementState.MEASURED if critique is not None else MeasurementState.NOT_MEASURED,
        )
        schema_valid = is_result_valid(result, profile)
        section_valid = not missing_section_keys(result, profile)
        support_rate: float | None = None
        if result is not None and result.evidence and context is not None:
            support_rate = sum(
                self._verifier.supported(context, evidence) for evidence in result.evidence
            ) / len(result.evidence)
        return EvaluationExecutionObservation(
            result=result,
            agent_state=state if isinstance(state, AgentState) else None,
            schema_valid=schema_valid,
            mode_sections_valid=section_valid,
            evidence_support_rate=support_rate,
            critic_metrics=critic,
            execution_id=getattr(state, "execution_id", None),
            resolved_model=self._model,
            provider=self._provider,
            model=self._model,
            planner_call_count=1 if result is not None else None,
            executor_call_count=(max(1, int(getattr(state, "round", 0))) if result is not None else None),
        )


class EvaluationArtifactWriter:
    """Incremental, duplicate-safe writer for run/results/summary artifacts."""

    def __init__(self, output: str | Path) -> None:
        raw = Path(output)
        if raw.suffix.lower() == ".jsonl":
            self.directory = raw.parent
            self.results_path = raw
        else:
            self.directory = raw
            self.results_path = raw / "results.jsonl"
        self.run_path = self.directory / "run.json"
        self.summary_path = self.directory / "summary.json"

    def ensure_writable(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            probe = self.directory / ".x3-write-probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as error:
            raise EvaluationPreflightError("evaluation artifact output is not writable") from error

    @staticmethod
    def _identity(result: EvaluationCaseResult) -> tuple[str, str, EvaluationStrategy, int]:
        return (result.run_id, result.case_id, result.strategy, result.trial_index)

    def existing_results(self) -> tuple[EvaluationCaseResult, ...]:
        if not self.results_path.exists():
            return ()
        try:
            return case_results_from_jsonl(self.results_path.read_text(encoding="utf-8"))
        except (OSError, EvaluationContractError) as error:
            raise EvaluationPreflightError("existing evaluation JSONL is invalid") from error

    def append_result(self, result: EvaluationCaseResult) -> bool:
        self.ensure_writable()
        try:
            # Reparse the canonical payload so callers cannot bypass the
            # X3-A digest validator with an unvalidated ``model_copy``.
            result = case_result_from_json(result.to_json())
        except EvaluationContractError as error:
            raise EvaluationContractError("evaluation result digest is invalid") from error
        existing = self.existing_results()
        identity = self._identity(result)
        for item in existing:
            if self._identity(item) != identity:
                continue
            if item.result_digest == result.result_digest:
                return False
            raise EvaluationDuplicateResultError(
                "evaluation result identity already has a different digest"
            )
        with self.results_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(result.to_json() + "\n")
            handle.flush()
        return True

    def write_run(self, run: EvaluationRun) -> None:
        self.ensure_writable()
        self.run_path.write_text(run.to_json() + "\n", encoding="utf-8")

    def write_summary(self, summary: "EvaluationSummary") -> None:
        self.ensure_writable()
        self.summary_path.write_text(summary.to_json() + "\n", encoding="utf-8")


class SummaryMetric(_StrictEvaluationModel):
    """A metric aggregate with an explicit denominator."""

    numerator: float | int | None = None
    denominator: float | int | None = None
    value: float | None = None
    measurement_state: MeasurementState = Field(
        default=MeasurementState.NOT_MEASURED,
        alias="measurementState",
    )

    @model_validator(mode="after")
    def _valid(self) -> "SummaryMetric":
        if self.measurement_state is MeasurementState.MEASURED:
            if self.numerator is None or self.denominator is None or self.denominator <= 0:
                raise ValueError("measured summary metrics require a positive denominator")
            if self.value is None:
                raise ValueError("measured summary metrics require value")
        elif self.numerator is not None or self.denominator is not None or self.value is not None:
            raise ValueError("unmeasured summary metrics cannot contain numeric values")
        return self


class CoverageCount(_StrictEvaluationModel):
    count: int
    denominator: int

    @field_validator("count", "denominator", mode="before")
    @classmethod
    def _counts(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("coverage counts must be non-negative integers")
        return value

    @model_validator(mode="after")
    def _bounded(self) -> "CoverageCount":
        if self.count > self.denominator:
            raise ValueError("coverage count cannot exceed denominator")
        return self


class EvaluationSummary(_StrictEvaluationModel):
    """Machine-readable summary; no global quality score is defined."""

    summary_version: str = Field(default="evaluation-summary-v1", alias="summaryVersion")
    run_id: str = Field(alias="runId")
    dataset_version: str = Field(alias="datasetVersion")
    dataset_digest: str = Field(alias="datasetDigest")
    data_classification: DataClassification = Field(
        alias="dataClassification"
    )
    dataset_completeness: DatasetCompleteness = Field(alias="datasetCompleteness")
    planned_count: int = Field(alias="plannedCount")
    executed_count: int = Field(alias="executedCount")
    successful_count: int = Field(alias="successfulCount")
    failed_count: int = Field(alias="failedCount")
    excluded_count: int = Field(alias="excludedCount")
    not_run_count: int = Field(alias="notRunCount")
    coverage_by_mode: dict[str, CoverageCount] = Field(alias="coverageByMode")
    coverage_by_query_category: dict[str, CoverageCount] = Field(alias="coverageByQueryCategory")
    coverage_by_source_type: dict[str, CoverageCount] = Field(alias="coverageBySourceType")
    difficulty_distribution: dict[str, CoverageCount] = Field(alias="difficultyDistribution")
    tool_beneficial_count: int = Field(alias="toolBeneficialCount")
    critic_sensitive_count: int = Field(alias="criticSensitiveCount")
    metrics: dict[str, SummaryMetric] = Field(default_factory=dict)
    caveats: tuple[str, ...] = ()
    status: EvaluationRunStatus = EvaluationRunStatus.COMPLETED

    @field_validator(
        "planned_count",
        "executed_count",
        "successful_count",
        "failed_count",
        "excluded_count",
        "not_run_count",
        "tool_beneficial_count",
        "critic_sensitive_count",
        mode="before",
    )
    @classmethod
    def _nonnegative_counts(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("summary count must be a non-negative integer")
        return value

    @model_validator(mode="after")
    def _counts(self) -> "EvaluationSummary":
        if self.executed_count + self.not_run_count > self.planned_count:
            raise ValueError("summary executed plus not_run exceeds planned")
        if self.successful_count + self.failed_count + self.excluded_count > self.executed_count:
            raise ValueError("summary terminal counts exceed executed")
        return self

    def to_json(self) -> str:
        return canonical_json(self)


@dataclass(frozen=True, slots=True)
class RetrievalMetricResult:
    recall_at_k: Mapping[int, float]
    precision_at_k: Mapping[int, float]
    mrr: float | None
    temporal_hit: bool | None
    temporal_coverage: float | None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class EvidenceMetricResult:
    precision: float | None
    recall: float | None


def _source_type(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", str(value)).upper()


def _candidate_interval(candidate: EvaluationRetrievedEvidence) -> tuple[int, int] | None:
    if candidate.start_ms is not None and candidate.end_ms is not None:
        return candidate.start_ms, candidate.end_ms
    if candidate.timestamp_ms is not None:
        return candidate.timestamp_ms, candidate.timestamp_ms + 1
    return None


def _expected_interval(ref: ExpectedEvidenceRef) -> tuple[int, int] | None:
    if ref.start_ms is not None and ref.end_ms is not None:
        return ref.start_ms, ref.end_ms
    if ref.timestamp_ms is not None:
        return ref.timestamp_ms, ref.timestamp_ms + 1
    return None


def _overlap_ratio(
    expected: tuple[int, int],
    actual: tuple[int, int] | None,
) -> float:
    if actual is None:
        return 0.0
    overlap = max(0, min(expected[1], actual[1]) - max(expected[0], actual[0]))
    return overlap / (expected[1] - expected[0])


def evidence_matches(
    expected: ExpectedEvidenceRef,
    candidate: EvaluationRetrievedEvidence,
) -> bool:
    """Apply the annotation's match level, always requiring revision equality."""

    if expected.source_revision != candidate.source_revision:
        return False
    if expected.source_type is not None and _source_type(expected.source_type) != _source_type(candidate.source_type):
        return False
    if expected.match_level is ExpectedEvidenceMatchLevel.SOURCE_ITEM:
        return expected.source_item_id == candidate.source_item_id
    if expected.match_level is ExpectedEvidenceMatchLevel.SEGMENT:
        return expected.segment_id == candidate.segment_id
    if expected.segment_id is not None and expected.segment_id != candidate.segment_id:
        return False
    return _overlap_ratio(_expected_interval(expected) or (0, 0), _candidate_interval(candidate)) > 0


def _temporal_coverage(
    expected_refs: Sequence[ExpectedEvidenceRef],
    expected_regions: Sequence[Any],
    candidates: Sequence[EvaluationRetrievedEvidence],
    *,
    source_revision: str | None = None,
) -> tuple[bool | None, float | None]:
    regions: list[tuple[int, int, str | None, str | None, str | None]] = []
    for ref in expected_refs:
        if ref.match_level is ExpectedEvidenceMatchLevel.TEMPORAL_REGION:
            interval = _expected_interval(ref)
            if interval is not None:
                regions.append(
                    (interval[0], interval[1], ref.segment_id, _source_type(ref.source_type), ref.source_revision)
                )
    for region in expected_regions:
        regions.append(
            (
                region.start_ms,
                region.end_ms,
                region.segment_id,
                _source_type(region.source_type),
                source_revision,
            )
        )
    if not regions:
        return None, None
    covered = 0.0
    total = 0
    hit = False
    unique_regions: list[tuple[int, int, str | None, str | None, str | None]] = []
    for region in regions:
        if region not in unique_regions:
            unique_regions.append(region)
    for start, end, segment_id, source_type, revision in unique_regions:
        expected_interval = (start, end)
        best = 0.0
        for candidate in candidates:
            if candidate.segment_id and segment_id and candidate.segment_id != segment_id:
                continue
            if source_type and _source_type(candidate.source_type) != source_type:
                continue
            if revision is not None and candidate.source_revision != revision:
                continue
            best = max(best, _overlap_ratio(expected_interval, _candidate_interval(candidate)))
        if best > 0:
            hit = True
        covered += min(1.0, best) * (end - start)
        total += end - start
    return hit, (covered / total if total else None)


def calculate_retrieval_metrics(
    expected_refs: Sequence[ExpectedEvidenceRef],
    retrieved: Sequence[EvaluationRetrievedEvidence],
    *,
    k_values: Sequence[int] = DEFAULT_RETRIEVAL_K_VALUES,
    expected_temporal_regions: Sequence[Any] = (),
    source_revision: str | None = None,
) -> RetrievalMetricResult:
    """Calculate deterministic provenance-backed Recall@K, Precision@K, MRR."""

    ordered = sorted(
        enumerate(retrieved),
        key=lambda pair: (pair[1].rank if pair[1].rank is not None else pair[0], pair[0]),
    )
    candidates = [item for _, item in ordered]
    refs = tuple(expected_refs)
    recall: dict[int, float] = {}
    precision: dict[int, float] = {}
    for raw_k in k_values:
        k = int(raw_k)
        top = candidates[:k]
        if not refs:
            continue
        matched_refs = sum(
            any(evidence_matches(ref, candidate) for candidate in top)
            for ref in refs
        )
        relevant = sum(any(evidence_matches(ref, candidate) for ref in refs) for candidate in top)
        recall[k] = matched_refs / len(refs)
        precision[k] = relevant / len(top) if top else 0.0
    mrr: float | None = None
    if refs:
        for index, candidate in enumerate(candidates, start=1):
            if any(evidence_matches(ref, candidate) for ref in refs):
                mrr = 1.0 / index
                break
        if mrr is None:
            mrr = 0.0
    temporal_hit, temporal_coverage = _temporal_coverage(
        refs, expected_temporal_regions, candidates, source_revision=source_revision
    )
    return RetrievalMetricResult(recall, precision, mrr, temporal_hit, temporal_coverage)


def calculate_evidence_metrics(
    expected_refs: Sequence[ExpectedEvidenceRef],
    final_evidence: Sequence[EvaluationRetrievedEvidence],
) -> EvidenceMetricResult:
    """Compare final output evidence with gold, separately from Evidence Guard."""

    refs = tuple(expected_refs)
    evidence = tuple(final_evidence)
    precision = None
    recall = None
    if evidence:
        precision = sum(any(evidence_matches(ref, item) for ref in refs) for item in evidence) / len(evidence)
    if refs:
        recall = sum(any(evidence_matches(ref, item) for item in evidence) for ref in refs) / len(refs)
    return EvidenceMetricResult(precision, recall)


def _normalize_fact_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        char
        for char in normalized
        if not unicodedata.category(char).startswith(("P", "Z", "C"))
    )


def calculate_required_fact_exact_coverage(
    required_facts: Sequence[Any],
    output_text: str,
) -> float | None:
    """Match only exact/normalized variants; this is not semantic correctness."""

    required = tuple(fact for fact in required_facts if getattr(fact, "required", True))
    if not required:
        return None
    normalized_output = _normalize_fact_text(output_text)
    matched = 0
    for fact in required:
        variants = tuple(getattr(fact, "acceptable_variants", ())) or (
            getattr(fact, "description", ""),
        )
        if any(_normalize_fact_text(variant) in normalized_output for variant in variants if variant):
            matched += 1
    return matched / len(required)


def calculate_cost(
    token_usage: TokenUsageMeasurement,
    catalog: PricingCatalog | None,
    *,
    provider: str | None,
    model: str | None,
    provider_reported_cost: float | None = None,
) -> CostMeasurement:
    """Calculate only from measured input/output tokens and data pricing."""

    entry = None if catalog is None else catalog.lookup(provider, model)
    currency = None if entry is None else entry.currency
    calculated = None
    if (
        entry is not None
        and entry.rate_availability is PricingRateAvailability.AVAILABLE
        and entry.input_rate_per_million is not None
        and entry.output_rate_per_million is not None
        and token_usage.measurement_state is MeasurementState.MEASURED
        and token_usage.input_tokens is not None
        and token_usage.output_tokens is not None
    ):
        calculated = (
            token_usage.input_tokens * entry.input_rate_per_million
            + token_usage.output_tokens * entry.output_rate_per_million
        ) / 1_000_000.0
    if provider_reported_cost is None and calculated is None:
        return CostMeasurement()
    return CostMeasurement(
        provider_reported_cost=provider_reported_cost,
        calculated_cost=calculated,
        currency=currency,
        pricing_version=(None if calculated is None or catalog is None else catalog.pricing_version),
        measurement_state=MeasurementState.MEASURED,
    )


@dataclass(frozen=True, slots=True)
class GitIdentity:
    sha: str | None
    working_tree_state: WorkingTreeState


@dataclass(frozen=True, slots=True)
class EvaluationPreflight:
    dataset: Any
    config: EvaluationRunnerConfig
    selected_cases: tuple[EvaluationCase, ...]
    git: GitIdentity
    writer: EvaluationArtifactWriter | None


@dataclass(frozen=True, slots=True)
class EvaluationRunResult:
    run: EvaluationRun
    results: tuple[EvaluationCaseResult, ...]
    summary: EvaluationSummary
    preflight: EvaluationPreflight


class EvaluationRunner:
    """Run one safe strategy over fresh case/trial executions."""

    _SUPPORTED_STRATEGIES = frozenset(
        {
            EvaluationStrategy.CURRENT_PRODUCTION,
            EvaluationStrategy.FIXED_BALANCED,
            EvaluationStrategy.ALWAYS_BALANCED,
            EvaluationStrategy.RULE_ROUTER,
        }
    )

    def __init__(
        self,
        execution_adapter: EvaluationExecutionAdapter | None = None,
        source_resolver: EvaluationSourceResolver | None = None,
        *,
        adapter: EvaluationExecutionAdapter | None = None,
        strategy: EvaluationExecutionStrategy | None = None,
        pricing_catalog: PricingCatalog | None = None,
        clock: Callable[[], float] = time.monotonic,
        repo_root: str | Path | None = None,
        run_id: str | None = None,
    ) -> None:
        selected_adapter = execution_adapter if execution_adapter is not None else adapter
        if selected_adapter is None and strategy is None:
            raise TypeError("execution_adapter or strategy is required")
        self._adapter = selected_adapter
        self._strategy = strategy
        self._source_resolver = source_resolver
        self._pricing_catalog = pricing_catalog
        self._clock = clock
        self._repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
        self._run_id = run_id

    def preflight(
        self,
        dataset: Any,
        config: EvaluationRunnerConfig | None = None,
    ) -> EvaluationPreflight:
        selected_config = config or EvaluationRunnerConfig()
        try:
            validated_dataset = validate_dataset(dataset)
        except Exception as error:
            raise EvaluationPreflightError("evaluation dataset failed contract validation") from error
        if selected_config.strategy not in self._SUPPORTED_STRATEGIES:
            raise EvaluationPreflightError(
                "X3-B supports one current/fixed baseline; routing matrix strategies are deferred"
            )
        if (
            selected_config.strategy is EvaluationStrategy.RULE_ROUTER
            and self._strategy is None
        ):
            raise EvaluationPreflightError(
                "RULE_ROUTER requires an explicit evaluation-only lane dispatch strategy"
            )
        all_ids = {case.case_id for case in validated_dataset.cases}
        unknown = set(selected_config.case_filter).difference(all_ids)
        if unknown:
            raise EvaluationPreflightError("case_filter contains an unknown case ID")
        selected_cases = tuple(
            case
            for case in validated_dataset.cases
            if not selected_config.case_filter or case.case_id in selected_config.case_filter
        )
        if not selected_cases:
            raise EvaluationPreflightError("evaluation case selection is empty")
        # Pricing is optional for quality evaluation.  A requested but
        # unavailable version makes cost NOT_MEASURED; it must not discard a
        # valid quality result or block the run.
        if selected_config.publish and self._git_identity().sha is None:
            raise EvaluationPreflightError("publish mode requires a Git commit identity")
        writer = (
            None
            if selected_config.artifact_output is None
            else EvaluationArtifactWriter(selected_config.artifact_output)
        )
        if writer is not None:
            writer.ensure_writable()
        if self._strategy is not None and self._strategy.name is not selected_config.strategy:
            raise EvaluationPreflightError("strategy object does not match runner config")
        return EvaluationPreflight(
            validated_dataset,
            selected_config,
            selected_cases,
            self._git_identity(),
            writer,
        )

    def _git_identity(self) -> GitIdentity:
        try:
            sha_process = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if sha_process.returncode != 0:
                return GitIdentity(None, WorkingTreeState.UNKNOWN)
            sha = sha_process.stdout.strip().lower()
            if not re.fullmatch(r"[0-9a-f]{7,64}", sha):
                return GitIdentity(None, WorkingTreeState.UNKNOWN)
            status_process = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            state = (
                WorkingTreeState.CLEAN
                if status_process.returncode == 0 and not status_process.stdout.strip()
                else WorkingTreeState.DIRTY
                if status_process.returncode == 0
                else WorkingTreeState.UNKNOWN
            )
            return GitIdentity(sha, state)
        except (OSError, subprocess.SubprocessError):
            return GitIdentity(None, WorkingTreeState.UNKNOWN)

    async def run(
        self,
        dataset: Any,
        config: EvaluationRunnerConfig | None = None,
    ) -> EvaluationRunResult:
        preflight = self.preflight(dataset, config)
        selected_config = preflight.config
        strategy = self._strategy
        if strategy is None:
            if self._adapter is None:  # pragma: no cover - constructor guard
                raise EvaluationPreflightError("execution adapter is unavailable")
            if selected_config.strategy is EvaluationStrategy.CURRENT_PRODUCTION:
                strategy = CurrentProductionEvaluationStrategy(self._adapter)
            else:
                strategy = AdapterEvaluationStrategy(selected_config.strategy, self._adapter)
        started_at = datetime.now(timezone.utc)
        planned_count = len(preflight.selected_cases) * selected_config.trial_count
        run = EvaluationRun(
            run_id=self._run_id or _new_run_id(),
            evaluation_contract_version=EVALUATION_CONTRACT_VERSION,
            dataset_version=preflight.dataset.dataset_version,
            dataset_digest=preflight.dataset.dataset_digest,
            runner_config_version=selected_config.runner_config_version,
            config_fingerprint=selected_config.fingerprint(),
            pricing_version=(
                selected_config.pricing_version
                or (None if self._pricing_catalog is None else self._pricing_catalog.pricing_version)
            ),
            git_sha=preflight.git.sha,
            working_tree_state=preflight.git.working_tree_state,
            strategy=selected_config.strategy,
            started_at=started_at,
            environment={
                "runner": "x3-b",
                "dataset_completeness": preflight.dataset.completeness.value,
            },
            planned_count=planned_count,
            status=EvaluationRunStatus.RUNNING,
            data_classification=preflight.dataset.data_classification,
        )
        if preflight.writer is not None:
            preflight.writer.write_run(run)

        results: list[EvaluationCaseResult] = []
        order: list[str] = []
        execution_index = 0
        stop_after = selected_config.max_executions
        stopped_early = False
        for case in preflight.selected_cases:
            for trial_index in range(selected_config.trial_count):
                if stop_after is not None and execution_index >= stop_after:
                    stopped_early = True
                    break
                identity_text = f"{case.case_id}:{selected_config.strategy.value}:{trial_index}"
                order.append(identity_text)
                execution_index += 1
                result = await self._run_one(
                    run,
                    case,
                    selected_config,
                    strategy,
                    trial_index,
                    execution_index,
                )
                if preflight.writer is not None:
                    inserted = preflight.writer.append_result(result)
                    if not inserted:
                        for existing in preflight.writer.existing_results():
                            if EvaluationArtifactWriter._identity(existing) == EvaluationArtifactWriter._identity(result):
                                result = existing
                                break
                results.append(result)
            if stopped_early:
                break

        successful_count = sum(item.status is EvaluationResultStatus.SUCCESSFUL for item in results)
        failed_count = sum(item.status is EvaluationResultStatus.FAILED for item in results)
        excluded_count = sum(item.status is EvaluationResultStatus.EXCLUDED for item in results)
        executed_count = successful_count + failed_count + excluded_count
        not_run_count = planned_count - executed_count
        final_status = EvaluationRunStatus.PARTIAL if not_run_count else EvaluationRunStatus.COMPLETED
        completed_at = datetime.now(timezone.utc)
        run = run.model_copy(
            update={
                "completed_at": completed_at,
                "execution_order": tuple(order),
                "executed_count": executed_count,
                "successful_count": successful_count,
                "failed_count": failed_count,
                "excluded_count": excluded_count,
                "not_run_count": not_run_count,
                "status": final_status,
            }
        )
        summary = build_evaluation_summary(run, preflight.dataset, preflight.selected_cases, results)
        if selected_config.pricing_version is not None and (
            self._pricing_catalog is None
            or self._pricing_catalog.pricing_version != selected_config.pricing_version
        ):
            summary = summary.model_copy(
                update={
                    "caveats": summary.caveats + ("pricing unavailable; cost NOT_MEASURED",),
                }
            )
        if preflight.writer is not None:
            preflight.writer.write_run(run)
            preflight.writer.write_summary(summary)
        return EvaluationRunResult(run, tuple(results), summary, preflight)

    def run_sync(self, dataset: Any, config: EvaluationRunnerConfig | None = None) -> EvaluationRunResult:
        return asyncio.run(self.run(dataset, config))

    async def _run_one(
        self,
        run: EvaluationRun,
        case: EvaluationCase,
        config: EvaluationRunnerConfig,
        strategy: EvaluationExecutionStrategy,
        trial_index: int,
        execution_order: int,
    ) -> EvaluationCaseResult:
        start = self._clock()
        artifact: EvaluationSourceArtifact | None = None
        try:
            if self._source_resolver is None:
                return self._excluded(
                    run, case, config, trial_index, execution_order,
                    EvaluationExclusionCategory.EVALUATION_INFRASTRUCTURE_INVALID,
                    "prepared source resolver is unavailable",
                    start,
                )
            try:
                resolved = self._source_resolver.resolve(case.media_ref)
                if inspect.isawaitable(resolved):
                    artifact = await resolved
                else:
                    artifact = resolved
            except Exception:
                return self._excluded(
                    run, case, config, trial_index, execution_order,
                    EvaluationExclusionCategory.EVALUATION_INFRASTRUCTURE_INVALID,
                    "prepared source resolver failed before execution",
                    start,
                )
            if artifact is None or not artifact.available:
                return self._excluded(
                    run, case, config, trial_index, execution_order,
                    EvaluationExclusionCategory.ARTIFACT_INCOMPATIBILITY,
                    "prepared source artifact is unavailable",
                    start,
                )
            if artifact.media_ref != case.media_ref:
                return self._excluded(
                    run, case, config, trial_index, execution_order,
                    EvaluationExclusionCategory.ARTIFACT_INCOMPATIBILITY,
                    "prepared artifact media identity does not match case",
                    start,
                )
            if artifact.source_revision != case.source_revision:
                return self._excluded(
                    run, case, config, trial_index, execution_order,
                    EvaluationExclusionCategory.SOURCE_REVISION_MISMATCH,
                    "prepared artifact source revision does not match case",
                    start,
                )
            if artifact.evaluation_contract_version != EVALUATION_CONTRACT_VERSION:
                return self._excluded(
                    run, case, config, trial_index, execution_order,
                    EvaluationExclusionCategory.CONTRACT_INCOMPATIBILITY,
                    "prepared artifact contract is unsupported",
                    start,
                )
            observation = await asyncio.wait_for(
                strategy.execute(
                    case.execution_input(),
                    artifact=artifact,
                    trial_index=trial_index,
                    timeout_seconds=config.timeout_seconds,
                ),
                timeout=config.timeout_seconds,
            )
            if not isinstance(observation, EvaluationExecutionObservation):
                raise EvaluationExecutionError("execution adapter returned an unsupported observation")
            return self._successful_or_failed(
                run, case, config, trial_index, execution_order, artifact, observation, start
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return self._failed(
                run, case, config, trial_index, execution_order,
                _map_failure_category(error), start,
            )

    def _base_kwargs(
        self,
        run: EvaluationRun,
        case: EvaluationCase,
        config: EvaluationRunnerConfig,
        trial_index: int,
        execution_order: int,
        start: float,
    ) -> dict[str, Any]:
        elapsed = max(0.0, (self._clock() - start) * 1000.0)
        latency = LatencyMeasurement(
            total_e2e_ms=elapsed,
            cold_warm=config.cold_warm,
            measurement_state=MeasurementState.MEASURED,
        )
        tool_metrics = ToolMetrics(
            measurement_state=(
                MeasurementState.NOT_APPLICABLE
                if config.tools_enabled is False
                else MeasurementState.NOT_MEASURED
            )
        )
        critic_metrics = CriticMetrics(
            measurement_state=(
                MeasurementState.NOT_APPLICABLE
                if config.critic_enabled is False
                else MeasurementState.NOT_MEASURED
            )
        )
        return {
            "run_id": run.run_id,
            "case_id": case.case_id,
            "strategy": config.strategy,
            "trial_index": trial_index,
            "mode": case.mode,
            "query_category": case.query_category,
            "source_revision": case.source_revision,
            "latency": latency,
            "tool_metrics": tool_metrics,
            "critic_metrics": critic_metrics,
            "planner_call_count": None,
            "executor_call_count": None,
            "cold_warm": config.cold_warm,
            "execution_order": execution_order,
            "data_classification": run.data_classification,
        }

    def _excluded(
        self,
        run: EvaluationRun,
        case: EvaluationCase,
        config: EvaluationRunnerConfig,
        trial_index: int,
        execution_order: int,
        category: EvaluationExclusionCategory,
        reason: str,
        start: float,
    ) -> EvaluationCaseResult:
        values = self._base_kwargs(run, case, config, trial_index, execution_order, start)
        values.update(
            status=EvaluationResultStatus.EXCLUDED,
            exclusion_category=category,
            exclusion_reason=reason[:MAX_REASON_TEXT],
        )
        return EvaluationCaseResult(**values)

    def _failed(
        self,
        run: EvaluationRun,
        case: EvaluationCase,
        config: EvaluationRunnerConfig,
        trial_index: int,
        execution_order: int,
        category: EvaluationFailureCategory,
        start: float,
    ) -> EvaluationCaseResult:
        values = self._base_kwargs(run, case, config, trial_index, execution_order, start)
        values.update(
            status=EvaluationResultStatus.FAILED,
            failure_category=category,
        )
        return EvaluationCaseResult(**values)

    def _successful_or_failed(
        self,
        run: EvaluationRun,
        case: EvaluationCase,
        config: EvaluationRunnerConfig,
        trial_index: int,
        execution_order: int,
        artifact: EvaluationSourceArtifact,
        observation: EvaluationExecutionObservation,
        start: float,
    ) -> EvaluationCaseResult:
        values = self._base_kwargs(run, case, config, trial_index, execution_order, start)
        result = observation.result
        profile = mode_profile_for(case.mode)
        schema_valid = observation.schema_valid
        if schema_valid is None:
            schema_valid = result is not None and is_result_valid(result, profile)
        mode_valid = observation.mode_sections_valid
        if mode_valid is None:
            mode_valid = result is not None and not missing_section_keys(result, profile)
        final_evidence = observation.final_evidence
        if not final_evidence and result is not None:
            final_evidence = tuple(_analysis_evidence_projection(item, artifact) for item in result.evidence)
        retrieval = calculate_retrieval_metrics(
            case.expected_evidence_refs,
            observation.retrieved_evidence,
            k_values=config.retrieval_k_values,
            expected_temporal_regions=case.expected_temporal_regions,
            source_revision=case.source_revision,
        )
        evidence = calculate_evidence_metrics(case.expected_evidence_refs, final_evidence)
        support_rate = observation.evidence_support_rate
        if support_rate is None and result is not None and artifact.context is not None and result.evidence:
            verifier = EvidenceVerificationService()
            support_rate = sum(verifier.supported(artifact.context, item) for item in result.evidence) / len(result.evidence)
        fact_coverage = (
            None
            if result is None
            else calculate_required_fact_exact_coverage(case.required_facts, result.to_markdown())
        )
        metric_values: dict[str, Any] = {
            "schema_valid": schema_valid,
            "mode_sections_valid": mode_valid,
            "evidence_guard_pass": observation.evidence_guard_pass,
            "evidence_support_rate": support_rate,
            "evidence_precision": evidence.precision,
            "evidence_recall": evidence.recall,
            "required_fact_exact_coverage": fact_coverage,
            "unsupported_claim_rate": observation.unsupported_claim_rate,
        }
        if retrieval.recall_at_k:
            primary_k = max(retrieval.recall_at_k)
            metric_values.update(
                retrieval_k=primary_k,
                retrieval_recall_at_k=retrieval.recall_at_k[primary_k],
                retrieval_precision_at_k=retrieval.precision_at_k[primary_k],
                retrieval_recall_at_k_by_k={str(k): v for k, v in retrieval.recall_at_k.items()},
                retrieval_precision_at_k_by_k={str(k): v for k, v in retrieval.precision_at_k.items()},
                mrr=retrieval.mrr,
            )
        if retrieval.temporal_hit is not None:
            metric_values.update(
                temporal_hit=retrieval.temporal_hit,
                temporal_coverage=retrieval.temporal_coverage,
            )
        metric_values = {key: value for key, value in metric_values.items() if value is not None}
        deterministic = DeterministicMetrics(
            **metric_values,
            measurement_state=(MeasurementState.MEASURED if metric_values else MeasurementState.NOT_MEASURED),
        )
        latency = _runner_latency(observation.latency, self._clock, start, config.cold_warm)
        tool_metrics = observation.tool_metrics
        if config.tools_enabled is False:
            tool_metrics = ToolMetrics(measurement_state=MeasurementState.NOT_APPLICABLE)
        critic_metrics = observation.critic_metrics
        if config.critic_enabled is False:
            critic_metrics = CriticMetrics(measurement_state=MeasurementState.NOT_APPLICABLE)
        route_decision = observation.route_decision
        if (
            config.strategy in {
                EvaluationStrategy.FIXED_BALANCED,
                EvaluationStrategy.ALWAYS_BALANCED,
            }
            and route_decision.measurement_state is MeasurementState.NOT_MEASURED
        ):
            route_decision = RoutingMeasurement(measurement_state=MeasurementState.NOT_APPLICABLE)
        token_usage = observation.token_usage
        if (
            config.strategy in {
                EvaluationStrategy.CURRENT_PRODUCTION,
                EvaluationStrategy.FIXED_BALANCED,
                EvaluationStrategy.ALWAYS_BALANCED,
            }
            and token_usage.router is None
        ):
            token_usage = token_usage.model_copy(
                update={
                    "router": TokenStageUsage(
                        measurement_state=MeasurementState.NOT_APPLICABLE
                    )
                }
            )
        cost = observation.cost
        if cost.measurement_state is not MeasurementState.MEASURED:
            pricing_catalog = self._pricing_catalog
            if config.pricing_version is not None and (
                pricing_catalog is None
                or pricing_catalog.pricing_version != config.pricing_version
            ):
                pricing_catalog = None
            cost = calculate_cost(
                observation.token_usage,
                pricing_catalog,
                provider=observation.provider,
                model=observation.model or observation.resolved_model,
                provider_reported_cost=observation.provider_reported_cost,
            )
        failure: EvaluationFailureCategory | None = None
        if result is None:
            failure = EvaluationFailureCategory.SCHEMA_FAILURE
        elif not schema_valid:
            failure = EvaluationFailureCategory.SCHEMA_FAILURE
        elif observation.evidence_guard_pass is False:
            failure = EvaluationFailureCategory.EVIDENCE_GUARD_FAILURE
        elif (
            config.critic_enabled is not False
            and critic_metrics.final_pass is False
        ):
            failure = EvaluationFailureCategory.CRITIC_EXHAUSTION
        values.update(
            status=(EvaluationResultStatus.FAILED if failure is not None else EvaluationResultStatus.SUCCESSFUL),
            failure_category=failure,
            resolved_model=observation.resolved_model,
            route_decision=route_decision,
            deterministic_metrics=deterministic,
            token_usage=token_usage,
            cost=cost,
            latency=latency,
            tool_metrics=tool_metrics,
            critic_metrics=critic_metrics,
            planner_call_count=observation.planner_call_count,
            executor_call_count=observation.executor_call_count,
            underlying_execution_id=observation.execution_id,
        )
        return EvaluationCaseResult(**values)


def _new_run_id() -> str:
    from uuid import uuid4

    return str(uuid4())


def _runner_latency(
    adapter_latency: LatencyMeasurement,
    clock: Callable[[], float],
    start: float,
    cold_warm: ColdWarmMarker,
) -> LatencyMeasurement:
    elapsed = max(0.0, (clock() - start) * 1000.0)
    updates: dict[str, Any] = {
        "total_e2e_ms": elapsed,
        "cold_warm": cold_warm,
        "measurement_state": MeasurementState.MEASURED,
    }
    if adapter_latency.measurement_state is MeasurementState.MEASURED:
        for field_name in (
            "routing_ms",
            "retrieval_ms",
            "planner_ms",
            "executor_ms",
            "critic_ms",
            "tool_ms",
        ):
            value = getattr(adapter_latency, field_name)
            if value is not None:
                updates[field_name] = value
    return LatencyMeasurement(**updates)


def _analysis_evidence_projection(
    evidence: Any,
    artifact: EvaluationSourceArtifact,
) -> EvaluationRetrievedEvidence:
    start_ms = None
    end_ms = None
    if artifact.context is not None:
        for segment in artifact.context.segments:
            if segment.start_ms <= evidence.timestamp_ms < segment.end_ms:
                start_ms, end_ms = segment.start_ms, segment.end_ms
                break
    return EvaluationRetrievedEvidence(
        source_revision=evidence.source_revision or artifact.source_revision,
        source_item_id=(evidence.source_item_id or (evidence.source_item_ids[0] if evidence.source_item_ids else None)),
        segment_id=evidence.segment_id or None,
        source_type=evidence.source or None,
        timestamp_ms=evidence.timestamp_ms,
        start_ms=start_ms,
        end_ms=end_ms,
    )


def _map_failure_category(error: Exception) -> EvaluationFailureCategory:
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return EvaluationFailureCategory.TIMEOUT
    if isinstance(error, EvaluationProviderError):
        return EvaluationFailureCategory.PROVIDER_FAILURE
    if isinstance(error, EvaluationRetrievalError):
        return EvaluationFailureCategory.RETRIEVAL_FAILURE
    if isinstance(error, EvaluationToolError):
        return EvaluationFailureCategory.TOOL_FAILURE
    if isinstance(error, EvaluationEvidenceGuardError):
        return EvaluationFailureCategory.EVIDENCE_GUARD_FAILURE
    if isinstance(error, EvaluationSchemaError):
        return EvaluationFailureCategory.SCHEMA_FAILURE
    name = type(error).__name__.casefold()
    if "retriev" in name:
        return EvaluationFailureCategory.RETRIEVAL_FAILURE
    if "provider" in name or "openai" in name or "http" in name:
        return EvaluationFailureCategory.PROVIDER_FAILURE
    if "tool" in name:
        return EvaluationFailureCategory.TOOL_FAILURE
    if "schema" in name or "result" in name or "validation" in name:
        return EvaluationFailureCategory.SCHEMA_FAILURE
    return EvaluationFailureCategory.INSTRUMENTATION_FAILURE


def _metric_average(values: Sequence[float | int | bool]) -> SummaryMetric:
    if not values:
        return SummaryMetric()
    numeric = [1.0 if value is True else 0.0 if value is False else float(value) for value in values]
    denominator = len(numeric)
    numerator = sum(numeric)
    return SummaryMetric(
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator,
        measurement_state=MeasurementState.MEASURED,
    )


def _coverage(
    selected_cases: Sequence[EvaluationCase],
    attribute: Callable[[EvaluationCase], str],
) -> dict[str, CoverageCount]:
    counts = Counter(attribute(case) for case in selected_cases)
    denominator = len(selected_cases)
    return {key: CoverageCount(count=value, denominator=denominator) for key, value in sorted(counts.items())}


def build_evaluation_summary(
    run: EvaluationRun,
    dataset: Any,
    selected_cases: Sequence[EvaluationCase],
    results: Sequence[EvaluationCaseResult],
) -> EvaluationSummary:
    """Aggregate only measured values and preserve explicit denominators."""

    metrics: dict[str, SummaryMetric] = {}

    def collect(name: str, getter: Callable[[EvaluationCaseResult], Any]) -> None:
        values: list[Any] = []
        for result in results:
            value = getter(result)
            if value is not None:
                values.append(value)
        metrics[name] = _metric_average(values)

    for name, getter in (
        ("retrieval_recall_at_k", lambda item: item.deterministic_metrics.retrieval_recall_at_k),
        ("retrieval_precision_at_k", lambda item: item.deterministic_metrics.retrieval_precision_at_k),
        ("mrr", lambda item: item.deterministic_metrics.mrr),
        ("temporal_hit", lambda item: item.deterministic_metrics.temporal_hit),
        ("temporal_coverage", lambda item: item.deterministic_metrics.temporal_coverage),
        ("evidence_precision", lambda item: item.deterministic_metrics.evidence_precision),
        ("evidence_recall", lambda item: item.deterministic_metrics.evidence_recall),
        ("evidence_support_rate", lambda item: item.deterministic_metrics.evidence_support_rate),
        ("required_fact_exact_coverage", lambda item: item.deterministic_metrics.required_fact_exact_coverage),
        ("schema_valid", lambda item: item.deterministic_metrics.schema_valid),
        ("mode_sections_valid", lambda item: item.deterministic_metrics.mode_sections_valid),
        ("evidence_guard_pass", lambda item: item.deterministic_metrics.evidence_guard_pass),
        ("planner_call_count", lambda item: item.planner_call_count),
        ("executor_call_count", lambda item: item.executor_call_count),
        ("critic_call_count", lambda item: item.critic_metrics.critic_call_count),
    ):
        collect(name, getter)
    for k in sorted(
        {
            int(key)
            for result in results
            for key in result.deterministic_metrics.retrieval_recall_at_k_by_k or {}
        }
    ):
        collect(
            f"retrieval_recall_at_{k}",
            lambda item, k=k: None
            if item.deterministic_metrics.retrieval_recall_at_k_by_k is None
            else item.deterministic_metrics.retrieval_recall_at_k_by_k.get(str(k)),
        )
        collect(
            f"retrieval_precision_at_{k}",
            lambda item, k=k: None
            if item.deterministic_metrics.retrieval_precision_at_k_by_k is None
            else item.deterministic_metrics.retrieval_precision_at_k_by_k.get(str(k)),
        )

    coverage_by_mode = _coverage(selected_cases, lambda case: case.mode.value)
    coverage_by_category = _coverage(selected_cases, lambda case: case.query_category.value)
    difficulty = _coverage(selected_cases, lambda case: case.difficulty.value)
    source_counts: Counter[str] = Counter()
    for case in selected_cases:
        types = {
            getattr(ref.source_type, "value", str(ref.source_type))
            for ref in case.expected_evidence_refs
            if ref.source_type is not None
        }
        for source_type in types:
            source_counts[source_type] += 1
    source_coverage = {
        key: CoverageCount(count=value, denominator=len(selected_cases))
        for key, value in sorted(source_counts.items())
    }
    caveats: list[str] = []
    if dataset.completeness is DatasetCompleteness.INCOMPLETE:
        caveats.extend(("DATASET_INCOMPLETE", "OCR provenance coverage incomplete"))
    if dataset.data_classification is not DataClassification.MEASURED:
        caveats.append(f"data_classification={dataset.data_classification.value}")
    return EvaluationSummary(
        run_id=run.run_id,
        dataset_version=dataset.dataset_version,
        dataset_digest=dataset.dataset_digest,
        data_classification=dataset.data_classification,
        dataset_completeness=dataset.completeness,
        planned_count=run.planned_count,
        executed_count=run.executed_count,
        successful_count=run.successful_count,
        failed_count=run.failed_count,
        excluded_count=run.excluded_count,
        not_run_count=run.not_run_count,
        coverage_by_mode=coverage_by_mode,
        coverage_by_query_category=coverage_by_category,
        coverage_by_source_type=source_coverage,
        difficulty_distribution=difficulty,
        tool_beneficial_count=sum(case.tool_beneficial for case in selected_cases),
        critic_sensitive_count=sum(case.critic_sensitive for case in selected_cases),
        metrics=metrics,
        caveats=tuple(caveats),
        status=run.status,
    )


__all__ = [
    "AdapterEvaluationStrategy",
    "AgentLoopEvaluationAdapter",
    "ConditionalPricingRate",
    "CoverageCount",
    "CurrentProductionEvaluationStrategy",
    "DEFAULT_RETRIEVAL_K_VALUES",
    "EvaluationArtifactWriter",
    "EvaluationDuplicateResultError",
    "EvaluationExecutionAdapter",
    "EvaluationExecutionError",
    "EvaluationExecutionObservation",
    "EvaluationExecutionStrategy",
    "EvaluationPreflight",
    "EvaluationPreflightError",
    "EvaluationRetrievedEvidence",
    "EvaluationRunResult",
    "EvaluationRunner",
    "EvaluationRunnerConfig",
    "EvaluationSourceArtifact",
    "EvaluationSourceResolver",
    "EvaluationSummary",
    "EvaluationToolError",
    "EvidenceMetricResult",
    "FixedBalancedEvaluationStrategy",
    "MappingEvaluationSourceResolver",
    "PricingCatalog",
    "PricingEntry",
    "PricingRateAvailability",
    "RetrievalMetricResult",
    "RuleRouterEvaluationStrategy",
    "SummaryMetric",
    "calculate_cost",
    "calculate_evidence_metrics",
    "calculate_required_fact_exact_coverage",
    "calculate_retrieval_metrics",
    "evidence_matches",
    "build_evaluation_summary",
]
