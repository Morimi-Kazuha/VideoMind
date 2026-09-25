"""Frozen, evaluation-only routing rules and quality gates for X3-C."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

from dovideo.domain import AnalysisMode

from .evaluation_contracts import (
    EvaluationCaseResult,
    EvaluationResultStatus,
    canonical_json,
)
from .model_routing import ModelRouteLane


class RuleRouterInputError(ValueError):
    """Runtime routing data is missing, malformed, or crosses the gold boundary."""


class _FrozenArtifact(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        validate_default=True,
    )

    def canonical_json(self) -> str:
        return canonical_json(self)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class RuleRouterFastRule(_FrozenArtifact):
    query_length_max: StrictInt = Field(alias="queryLengthMax")
    mode: AnalysisMode
    media_duration_ms_max: StrictInt = Field(alias="mediaDurationMsMax")
    segment_count_max: StrictInt = Field(alias="segmentCountMax")
    chunk_count_max: StrictInt = Field(alias="chunkCountMax")
    require_any_source_modality: StrictBool = Field(alias="requireAnySourceModality")
    allow_both_modalities: StrictBool = Field(alias="allowBothModalities")
    rationale: str

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, value: Any) -> AnalysisMode:
        try:
            mode = value if isinstance(value, AnalysisMode) else AnalysisMode[str(value).strip().upper()]
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("fast rule mode must be a concrete analysis mode") from error
        if mode.value == "AUTO":
            raise ValueError("fast rule mode must be concrete")
        return mode

    @field_validator(
        "query_length_max",
        "media_duration_ms_max",
        "segment_count_max",
        "chunk_count_max",
    )
    @classmethod
    def _nonnegative_threshold(cls, value: int, info: Any) -> int:
        if value < 0:
            raise ValueError(f"{info.field_name} must be non-negative")
        return value


class RuleRouterDeepRule(_FrozenArtifact):
    query_length_min: StrictInt = Field(alias="queryLengthMin")
    media_duration_ms_min: StrictInt = Field(alias="mediaDurationMsMin")
    segment_count_min: StrictInt = Field(alias="segmentCountMin")
    chunk_count_min: StrictInt = Field(alias="chunkCountMin")
    multimodal_duration_ms_min: StrictInt = Field(alias="multimodalDurationMsMin")
    rationale: str

    @field_validator(
        "query_length_min",
        "media_duration_ms_min",
        "segment_count_min",
        "chunk_count_min",
        "multimodal_duration_ms_min",
    )
    @classmethod
    def _positive_threshold(cls, value: int, info: Any) -> int:
        if value <= 0:
            raise ValueError(f"{info.field_name} must be positive")
        return value


class RuleRouterConfiguration(_FrozenArtifact):
    router_version: str = Field(alias="routerVersion")
    allowed_signals: tuple[str, ...] = Field(alias="allowedSignals")
    forbidden_signals: tuple[str, ...] = Field(alias="forbiddenSignals")
    fast: RuleRouterFastRule = Field(alias="fastRule")
    deep: RuleRouterDeepRule = Field(alias="deepRule")
    fallback_lane: ModelRouteLane = Field(
        default=ModelRouteLane.BALANCED,
        alias="fallbackLane",
    )

    @field_validator("router_version", mode="before")
    @classmethod
    def _version(cls, value: Any) -> str:
        if value != "rule-router-v1":
            raise ValueError("unsupported rule router version")
        return value

    @field_validator("allowed_signals", "forbidden_signals", mode="before")
    @classmethod
    def _signal_lists(cls, value: Any, info: Any) -> tuple[str, ...]:
        if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
            raise ValueError(f"{info.field_name} must be a sequence of signal names")
        normalized = tuple(item.strip() if isinstance(item, str) else "" for item in value)
        if not normalized or any(not item for item in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError(f"{info.field_name} must contain unique non-empty names")
        return normalized


class RuleRouterRuntimeSignals(_FrozenArtifact):
    """The bounded runtime-only projection consumed by rule-router-v1."""

    query_length_chars: StrictInt = Field(alias="queryLengthChars")
    mode: AnalysisMode
    media_duration_ms: StrictInt | None = Field(default=None, alias="mediaDurationMs")
    segment_count: StrictInt = Field(alias="segmentCount")
    chunk_count: StrictInt = Field(alias="chunkCount")
    asr_available: StrictBool = Field(alias="asrAvailable")
    ocr_available: StrictBool = Field(alias="ocrAvailable")

    @field_validator("mode", mode="before")
    @classmethod
    def _concrete_mode(cls, value: Any) -> AnalysisMode:
        try:
            mode = value if isinstance(value, AnalysisMode) else AnalysisMode[str(value).strip().upper()]
        except (KeyError, TypeError, ValueError) as error:
            raise RuleRouterInputError("mode must be a concrete AnalysisMode") from error
        if mode.value == "AUTO":
            raise RuleRouterInputError("AUTO is not a concrete routing mode")
        return mode

    @field_validator(
        "query_length_chars",
        "media_duration_ms",
        "segment_count",
        "chunk_count",
        mode="before",
    )
    @classmethod
    def _bounded_runtime_counts(cls, value: Any, info: Any) -> Any:
        if value is None and info.field_name == "media_duration_ms":
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuleRouterInputError(f"{info.field_name} must be a non-negative integer")
        maximums = {
            "query_length_chars": 500,
            "media_duration_ms": 365 * 24 * 60 * 60 * 1000,
            "segment_count": 1_000_000,
            "chunk_count": 1_000_000,
        }
        if value > maximums[info.field_name]:
            raise RuleRouterInputError(f"{info.field_name} exceeds its routing bound")
        return value

    @field_validator("query_length_chars")
    @classmethod
    def _nonempty_query_length(cls, value: int) -> int:
        if value == 0:
            raise RuleRouterInputError("query_length_chars must be positive")
        return value


def _build_default_rule_configuration() -> RuleRouterConfiguration:
    return RuleRouterConfiguration(
        routerVersion="rule-router-v1",
        allowedSignals=(
            "query_length_chars",
            "mode",
            "media_duration_ms",
            "segment_count",
            "chunk_count",
            "asr_available",
            "ocr_available",
        ),
        forbiddenSignals=(
            "difficulty",
            "query_category",
            "expected_evidence",
            "required_facts",
            "reference_answer",
            "tool_beneficial",
            "critic_sensitive",
            "fixed_lane_result",
            "oracle",
            "jev_suggestion",
            "case_id",
        ),
        fastRule={
            "queryLengthMax": 120,
            "mode": "GENERAL",
            "mediaDurationMsMax": 300_000,
            "segmentCountMax": 5,
            "chunkCountMax": 1,
            "requireAnySourceModality": True,
            "allowBothModalities": False,
            "rationale": (
                "The current pre-outcome workload has query lengths 53-95 characters, so "
                "120 leaves 25 characters of headroom. FAST also requires concrete GENERAL "
                "mode, known duration at most 300000 ms, at most five one-minute segments, "
                "at most one five-minute chunk, and exactly one available ASR/OCR modality. "
                "This keeps the lane to a small, single-chunk runtime workload."
            ),
        },
        deepRule={
            "queryLengthMin": 300,
            "mediaDurationMsMin": 1_800_000,
            "segmentCountMin": 30,
            "chunkCountMin": 6,
            "multimodalDurationMsMin": 900_000,
            "rationale": (
                "A query of 300 characters is 2.5 times the FAST query ceiling and 60 percent "
                "of the 500-character routing bound. Thirty minutes, thirty one-minute "
                "segments, and six five-minute chunks each mark a clearly large source. "
                "Both ASR and OCR on a fifteen-minute source marks substantial multimodal work."
            ),
        },
        fallbackLane=ModelRouteLane.BALANCED,
    )


DEFAULT_RULE_ROUTER_CONFIGURATION = _build_default_rule_configuration()


class RuleRouterV1:
    """A deterministic control router with no access to evaluation annotations."""

    def __init__(self, configuration: RuleRouterConfiguration | None = None) -> None:
        self.configuration = configuration or DEFAULT_RULE_ROUTER_CONFIGURATION

    @property
    def digest(self) -> str:
        return self.configuration.digest

    def route(self, signals: RuleRouterRuntimeSignals) -> ModelRouteLane:
        if not isinstance(signals, RuleRouterRuntimeSignals):
            raise TypeError("signals must be RuleRouterRuntimeSignals")
        config = self.configuration
        deep = config.deep
        if (
            signals.query_length_chars >= deep.query_length_min
            or (
                signals.media_duration_ms is not None
                and signals.media_duration_ms >= deep.media_duration_ms_min
            )
            or signals.segment_count >= deep.segment_count_min
            or signals.chunk_count >= deep.chunk_count_min
            or (
                signals.asr_available
                and signals.ocr_available
                and signals.media_duration_ms is not None
                and signals.media_duration_ms >= deep.multimodal_duration_ms_min
            )
        ):
            return ModelRouteLane.DEEP

        fast = config.fast
        has_source_modality = signals.asr_available or signals.ocr_available
        both_modalities = signals.asr_available and signals.ocr_available
        if (
            signals.query_length_chars <= fast.query_length_max
            and signals.mode is fast.mode
            and signals.media_duration_ms is not None
            and signals.media_duration_ms <= fast.media_duration_ms_max
            and signals.segment_count <= fast.segment_count_max
            and signals.chunk_count <= fast.chunk_count_max
            and (has_source_modality or not fast.require_any_source_modality)
            and (fast.allow_both_modalities or not both_modalities)
        ):
            return ModelRouteLane.FAST
        return config.fallback_lane


def rule_router_signals_from_execution(
    execution_input: Mapping[str, Any],
    artifact: Any,
) -> RuleRouterRuntimeSignals:
    """Derive only runtime signals from the non-gold runner projection/source."""

    expected_keys = {"media_ref", "query", "mode"}
    if not isinstance(execution_input, Mapping) or set(execution_input) != expected_keys:
        raise RuleRouterInputError(
            "RULE_ROUTER accepts only media_ref, query, and mode execution inputs"
        )
    media_ref = execution_input.get("media_ref")
    query = execution_input.get("query")
    if not isinstance(media_ref, str) or not media_ref.strip():
        raise RuleRouterInputError("media_ref must be non-empty runtime text")
    if not isinstance(query, str) or not query.strip():
        raise RuleRouterInputError("query must be non-empty runtime text")

    context = getattr(artifact, "context", None)
    if context is None:
        segments: tuple[Any, ...] = ()
    else:
        raw_segments = getattr(context, "segments", None)
        if raw_segments is None or isinstance(raw_segments, (str, bytes, bytearray)):
            raise RuleRouterInputError("runtime context segments are invalid")
        try:
            segments = tuple(raw_segments)
        except TypeError as error:
            raise RuleRouterInputError("runtime context segments are invalid") from error

    raw_chunks = getattr(artifact, "chunks", ())
    if raw_chunks is None or isinstance(raw_chunks, (str, bytes, bytearray)):
        raise RuleRouterInputError("runtime chunks are invalid")
    try:
        chunks = tuple(raw_chunks)
    except TypeError as error:
        raise RuleRouterInputError("runtime chunks are invalid") from error

    segment_ends: list[int] = []
    asr_available = False
    ocr_available = False
    for segment in segments:
        end_ms = getattr(segment, "end_ms", None)
        if isinstance(end_ms, bool) or not isinstance(end_ms, int) or end_ms <= 0:
            raise RuleRouterInputError("runtime segment end_ms is invalid")
        segment_ends.append(end_ms)
        transcript = getattr(segment, "transcript", "")
        if transcript and (not isinstance(transcript, str) or transcript.strip()):
            asr_available = True
        source_items = getattr(segment, "source_items", ())
        for item in source_items:
            source_type = getattr(item, "source_type", None)
            source_type = getattr(source_type, "value", source_type)
            if source_type == "ASR":
                asr_available = True
            elif source_type == "OCR":
                ocr_available = True
        if getattr(segment, "ocr_texts", ()) or getattr(segment, "evidence_frames", ()):
            ocr_available = True

    if len(chunks) > 1_000_000:
        raise RuleRouterInputError("runtime chunk count exceeds its routing bound")
    duration = max(segment_ends) if segment_ends else None
    if duration is not None and duration > 365 * 24 * 60 * 60 * 1000:
        raise RuleRouterInputError("runtime media duration exceeds its routing bound")

    try:
        return RuleRouterRuntimeSignals(
            queryLengthChars=len(query),
            mode=execution_input["mode"],
            mediaDurationMs=duration,
            segmentCount=len(segments),
            chunkCount=len(chunks),
            asrAvailable=asr_available,
            ocrAvailable=ocr_available,
        )
    except Exception as error:
        if isinstance(error, RuleRouterInputError):
            raise
        raise RuleRouterInputError("runtime routing signals failed validation") from error


class RoutingQualityGateConfiguration(_FrozenArtifact):
    gate_version: str = Field(alias="gateVersion")
    required_status: EvaluationResultStatus = Field(alias="requiredStatus")
    require_schema_valid: StrictBool = Field(alias="requireSchemaValid")
    require_mode_sections_valid: StrictBool = Field(alias="requireModeSectionsValid")
    require_evidence_guard_pass: StrictBool = Field(alias="requireEvidenceGuardPass")
    coverage_metric: str = Field(alias="coverageMetric")
    minimum_required_fact_exact_coverage: float = Field(
        alias="minimumRequiredFactExactCoverage",
        ge=0.0,
        le=1.0,
    )
    known_limitation: str = Field(alias="knownLimitation")

    @field_validator("gate_version", mode="before")
    @classmethod
    def _version(cls, value: Any) -> str:
        if value != "routing-quality-gate-v1":
            raise ValueError("unsupported routing quality gate version")
        return value

    @field_validator("coverage_metric", mode="before")
    @classmethod
    def _coverage_metric(cls, value: Any) -> str:
        if value != "requiredFactExactCoverage":
            raise ValueError("routing quality gate requires requiredFactExactCoverage")
        return value


DEFAULT_ROUTING_QUALITY_GATE_CONFIGURATION = RoutingQualityGateConfiguration(
    gateVersion="routing-quality-gate-v1",
    requiredStatus=EvaluationResultStatus.SUCCESSFUL,
    requireSchemaValid=True,
    requireModeSectionsValid=True,
    requireEvidenceGuardPass=True,
    coverageMetric="requiredFactExactCoverage",
    minimumRequiredFactExactCoverage=0.90,
    knownLimitation=(
        "requiredFactExactCoverage uses normalized lexical exact matching; it is not "
        "a measure of semantic correctness."
    ),
)


class RoutingQualityGateV1:
    """Transparent fail-closed deterministic gate applied after execution."""

    def __init__(
        self,
        configuration: RoutingQualityGateConfiguration | None = None,
    ) -> None:
        self.configuration = configuration or DEFAULT_ROUTING_QUALITY_GATE_CONFIGURATION

    @property
    def digest(self) -> str:
        return self.configuration.digest

    def passes(self, result: EvaluationCaseResult) -> bool:
        if not isinstance(result, EvaluationCaseResult):
            raise TypeError("result must be EvaluationCaseResult")
        config = self.configuration
        metrics = result.deterministic_metrics
        coverage = metrics.required_fact_exact_coverage
        return (
            result.status is config.required_status
            and (not config.require_schema_valid or metrics.schema_valid is True)
            and (
                not config.require_mode_sections_valid
                or metrics.mode_sections_valid is True
            )
            and (
                not config.require_evidence_guard_pass
                or metrics.evidence_guard_pass is True
            )
            and coverage is not None
            and coverage >= config.minimum_required_fact_exact_coverage
        )


__all__ = [
    "DEFAULT_ROUTING_QUALITY_GATE_CONFIGURATION",
    "DEFAULT_RULE_ROUTER_CONFIGURATION",
    "RuleRouterConfiguration",
    "RuleRouterInputError",
    "RuleRouterRuntimeSignals",
    "RuleRouterV1",
    "RoutingQualityGateConfiguration",
    "RoutingQualityGateV1",
    "rule_router_signals_from_execution",
]
