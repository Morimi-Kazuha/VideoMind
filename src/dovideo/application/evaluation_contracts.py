"""Evaluation-only contracts for the DOVideo X3-A boundary.

This module deliberately does not execute an Agent, call a Provider, run a
router, or calculate benchmark metrics.  It owns only the immutable data
contracts and the deterministic JSON/JSONL representation used by later X3
runner slices.

The models in this module use ``extra='forbid'`` on purpose.  A golden
annotation is a tested artifact; silently ignoring a misspelled field or
repairing an invalid identity would make the benchmark non-auditable.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from dovideo.domain import AnalysisMode, SourceType, sha256_canonical

from .model_routing import ModelRouteLane, ModelRoutingReasonCode


GOLDEN_DATASET_VERSION = "golden-dataset-v1"
EVALUATION_CONTRACT_VERSION = "evaluation-contract-v1"
DEFAULT_ANNOTATION_VERSION = "annotation-v1"
DEFAULT_RUNNER_CONFIG_VERSION = "runner-config-v1"
MVP_GOLDEN_CASE_COUNT = 16

MAX_CASE_ID_LENGTH = 128
MAX_MEDIA_REF_LENGTH = 512
MAX_QUERY_LENGTH = 4_000
MAX_VERSION_LENGTH = 96
MAX_DESCRIPTION_LENGTH = 4_000
MAX_FACT_ID_LENGTH = 128
MAX_FACT_DESCRIPTION_LENGTH = 4_000
MAX_ANSWER_VARIANT_LENGTH = 8_000
MAX_TAG_LENGTH = 96
MAX_TAGS = 32
MAX_IDENTITY_LENGTH = 512
MAX_MODEL_ID_LENGTH = 256
MAX_REASON_LENGTH = 2_000
MAX_ENVIRONMENT_ITEMS = 64
MAX_ENVIRONMENT_KEY_LENGTH = 96
MAX_ENVIRONMENT_VALUE_LENGTH = 512

RUNTIME_CASE_FIELDS = ("media_ref", "query", "mode")
ANNOTATION_ONLY_CASE_FIELDS = (
    "case_id",
    "dataset_version",
    "source_revision",
    "query_category",
    "expected_evidence_refs",
    "expected_temporal_regions",
    "allowed_source_types",
    "required_facts",
    "acceptable_answer_variants",
    "reference_answer",
    "difficulty",
    "tags",
    "tool_beneficial",
    "critic_sensitive",
    "annotation_version",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_ABSOLUTE_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


class EvaluationContractError(ValueError):
    """Base error for invalid X3 evaluation contracts."""


class EvaluationQueryCategory(str, Enum):
    DIRECT_SPEECH_FACT = "DIRECT_SPEECH_FACT"
    OCR_TEXT_FACT = "OCR_TEXT_FACT"
    TEMPORAL_LOOKUP = "TEMPORAL_LOOKUP"
    SUMMARY = "SUMMARY"
    CROSS_SEGMENT_COMPARISON = "CROSS_SEGMENT_COMPARISON"
    LONG_RANGE_REASONING = "LONG_RANGE_REASONING"
    MULTI_CONSTRAINT_ANALYSIS = "MULTI_CONSTRAINT_ANALYSIS"
    EVIDENCE_HEAVY = "EVIDENCE_HEAVY"
    TOOL_BENEFICIAL_LOOKUP = "TOOL_BENEFICIAL_LOOKUP"


class EvaluationDifficulty(str, Enum):
    EASY = "EASY"
    MEDIUM = "MEDIUM"
    HARD = "HARD"


class ExpectedEvidenceMatchLevel(str, Enum):
    SOURCE_ITEM = "SOURCE_ITEM"
    SEGMENT = "SEGMENT"
    TEMPORAL_REGION = "TEMPORAL_REGION"


class EvaluationStrategy(str, Enum):
    ALWAYS_FAST = "ALWAYS_FAST"
    ALWAYS_BALANCED = "ALWAYS_BALANCED"
    ALWAYS_DEEP = "ALWAYS_DEEP"
    RULE_ROUTER = "RULE_ROUTER"
    JEV_ROUTER = "JEV_ROUTER"
    # X3-B uses one explicitly named production baseline.  The routing
    # strategies above remain the X3-A/X3-C contract values; these two values
    # do not authorize a routing comparison or a Jev call.
    CURRENT_PRODUCTION = "CURRENT_PRODUCTION"
    FIXED_BALANCED = "FIXED_BALANCED"


class MeasurementState(str, Enum):
    MEASURED = "MEASURED"
    NOT_MEASURED = "NOT_MEASURED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNAVAILABLE = "UNAVAILABLE"


class DataClassification(str, Enum):
    MEASURED = "MEASURED"
    SYNTHETIC = "SYNTHETIC"
    ESTIMATED = "ESTIMATED"
    ILLUSTRATIVE = "ILLUSTRATIVE"


class WorkingTreeState(str, Enum):
    CLEAN = "CLEAN"
    DIRTY = "DIRTY"
    UNKNOWN = "UNKNOWN"


class ColdWarmMarker(str, Enum):
    COLD = "COLD"
    WARM = "WARM"
    UNSPECIFIED = "UNSPECIFIED"


class EvaluationRunStatus(str, Enum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    NON_PUBLISHABLE = "NON_PUBLISHABLE"


class EvaluationResultStatus(str, Enum):
    PLANNED = "PLANNED"
    EXECUTED = "EXECUTED"
    SUCCESSFUL = "SUCCESSFUL"
    FAILED = "FAILED"
    EXCLUDED = "EXCLUDED"
    NOT_RUN = "NOT_RUN"


class EvaluationFailureCategory(str, Enum):
    SOURCE_ARTIFACT_MISSING = "SOURCE_ARTIFACT_MISSING"
    PROVENANCE_INVALID = "PROVENANCE_INVALID"
    RETRIEVAL_FAILURE = "RETRIEVAL_FAILURE"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    TIMEOUT = "TIMEOUT"
    SCHEMA_FAILURE = "SCHEMA_FAILURE"
    CRITIC_EXHAUSTION = "CRITIC_EXHAUSTION"
    EVIDENCE_GUARD_FAILURE = "EVIDENCE_GUARD_FAILURE"
    TOOL_FAILURE = "TOOL_FAILURE"
    ROUTER_FAILURE = "ROUTER_FAILURE"
    MISSING_USAGE = "MISSING_USAGE"
    INSTRUMENTATION_FAILURE = "INSTRUMENTATION_FAILURE"


class EvaluationExclusionCategory(str, Enum):
    ARTIFACT_INCOMPATIBILITY = "ARTIFACT_INCOMPATIBILITY"
    SOURCE_REVISION_MISMATCH = "SOURCE_REVISION_MISMATCH"
    CONTRACT_INCOMPATIBILITY = "CONTRACT_INCOMPATIBILITY"
    EVALUATION_INFRASTRUCTURE_INVALID = "EVALUATION_INFRASTRUCTURE_INVALID"


class DatasetCompleteness(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "DATASET_INCOMPLETE"


class _EvaluationModel(BaseModel):
    """Strict, immutable base for evaluation artifacts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        validate_default=True,
        arbitrary_types_allowed=True,
    )


def _tuple_value(value: Any, field_name: str) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise ValueError(f"{field_name} must be a collection")
    try:
        return tuple(value)
    except TypeError as error:
        raise ValueError(f"{field_name} must be a collection") from error


def _text(value: Any, field_name: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{field_name} must not be blank")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} exceeds its bound")
    if "\x00" in normalized or "\r" in normalized or "\n" in normalized:
        raise ValueError(f"{field_name} contains a forbidden control character")
    return normalized


def _optional_text(value: Any, field_name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, field_name, maximum)


def _version(value: Any, field_name: str) -> str:
    normalized = _text(value, field_name, MAX_VERSION_LENGTH)
    if _VERSION_RE.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} has an invalid version spelling")
    return normalized


def _source_revision(value: Any, field_name: str = "source_revision") -> str:
    normalized = _text(value, field_name, 64)
    if _SHA256_RE.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _identity(value: Any, field_name: str) -> str:
    return _text(value, field_name, MAX_IDENTITY_LENGTH)


def _nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return value


def _positive_int(value: Any, field_name: str) -> int:
    normalized = _nonnegative_int(value, field_name)
    if normalized == 0:
        raise ValueError(f"{field_name} must be positive")
    return normalized


def _finite_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be finite")
    if normalized < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return normalized


def _bounded_ratio(value: Any, field_name: str) -> float:
    normalized = _finite_float(value, field_name)
    if normalized > 1:
        raise ValueError(f"{field_name} must be between 0 and 1")
    return normalized


def _unique_texts(values: tuple[str, ...], field_name: str, maximum: int) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _text(value, field_name, maximum)
        if normalized in seen:
            raise ValueError(f"{field_name} contains a duplicate value")
        seen.add(normalized)
        output.append(normalized)
    return tuple(output)


def _aware_datetime(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


class ExpectedTemporalRegion(_EvaluationModel):
    start_ms: int = Field(alias="startMs")
    end_ms: int = Field(alias="endMs")
    source_type: SourceType | None = Field(default=None, alias="sourceType")
    segment_id: str | None = Field(default=None, alias="segmentId")

    _validate_start = field_validator("start_ms", mode="before")(
        lambda value: _nonnegative_int(value, "start_ms")
    )
    _validate_end = field_validator("end_ms", mode="before")(
        lambda value: _positive_int(value, "end_ms")
    )

    @field_validator("segment_id", mode="before")
    @classmethod
    def _validate_segment(cls, value: Any) -> str | None:
        return None if value is None else _identity(value, "segment_id")

    @model_validator(mode="after")
    def _validate_range(self) -> "ExpectedTemporalRegion":
        if self.end_ms <= self.start_ms:
            raise ValueError("expected temporal region must have start_ms < end_ms")
        return self


class ExpectedEvidenceRef(_EvaluationModel):
    ref_id: str | None = Field(default=None, alias="refId")
    source_revision: str = Field(alias="sourceRevision")
    segment_id: str | None = Field(default=None, alias="segmentId")
    source_item_id: str | None = Field(default=None, alias="sourceItemId")
    source_type: SourceType | None = Field(default=None, alias="sourceType")
    timestamp_ms: int | None = Field(default=None, alias="timestampMs")
    start_ms: int | None = Field(default=None, alias="startMs")
    end_ms: int | None = Field(default=None, alias="endMs")
    match_level: ExpectedEvidenceMatchLevel = Field(
        default=ExpectedEvidenceMatchLevel.SOURCE_ITEM,
        alias="matchLevel",
    )

    @field_validator("ref_id", mode="before")
    @classmethod
    def _validate_ref_id(cls, value: Any) -> str | None:
        return None if value is None else _text(value, "ref_id", MAX_IDENTITY_LENGTH)

    @field_validator("source_revision", mode="before")
    @classmethod
    def _validate_revision(cls, value: Any) -> str:
        return _source_revision(value)

    @field_validator("segment_id", "source_item_id", mode="before")
    @classmethod
    def _validate_identity_fields(cls, value: Any, info: Any) -> str | None:
        return None if value is None else _identity(value, info.field_name)

    @field_validator("timestamp_ms", "start_ms", "end_ms", mode="before")
    @classmethod
    def _validate_times(cls, value: Any, info: Any) -> int | None:
        return None if value is None else _nonnegative_int(value, info.field_name)

    @model_validator(mode="after")
    def _validate_shape(self) -> "ExpectedEvidenceRef":
        if self.match_level is ExpectedEvidenceMatchLevel.SOURCE_ITEM:
            if not self.source_item_id or self.source_type is None:
                raise ValueError("SOURCE_ITEM evidence requires source_item_id and source_type")
        elif self.match_level is ExpectedEvidenceMatchLevel.SEGMENT:
            if not self.segment_id:
                raise ValueError("SEGMENT evidence requires segment_id")
        elif self.match_level is ExpectedEvidenceMatchLevel.TEMPORAL_REGION:
            if self.start_ms is None or self.end_ms is None:
                raise ValueError("TEMPORAL_REGION evidence requires start_ms and end_ms")

        if self.start_ms is not None and self.end_ms is not None:
            if self.end_ms <= self.start_ms:
                raise ValueError("evidence interval must have start_ms < end_ms")
        if self.timestamp_ms is not None and self.start_ms is not None and self.end_ms is not None:
            if not self.start_ms <= self.timestamp_ms < self.end_ms:
                raise ValueError("evidence timestamp must fall inside its interval")
        return self

    def identity_key(self) -> str:
        """Return the stable annotation key used for duplicate detection."""

        return sha256_canonical(self.model_dump(mode="json", by_alias=True))

    def link_keys(self) -> tuple[str, ...]:
        values = [self.identity_key()]
        if self.ref_id:
            values.append(self.ref_id)
        if self.source_item_id:
            values.append(self.source_item_id)
        if self.segment_id:
            values.append(self.segment_id)
        return tuple(values)


class RequiredFact(_EvaluationModel):
    fact_id: str = Field(alias="factId")
    description: str
    required: StrictBool = True
    acceptable_variants: tuple[str, ...] = Field(default=(), alias="acceptableVariants")
    linked_evidence_refs: tuple[str, ...] = Field(default=(), alias="linkedEvidenceRefs")

    @field_validator("fact_id", mode="before")
    @classmethod
    def _validate_fact_id(cls, value: Any) -> str:
        return _text(value, "fact_id", MAX_FACT_ID_LENGTH)

    @field_validator("description", mode="before")
    @classmethod
    def _validate_description(cls, value: Any) -> str:
        return _text(value, "description", MAX_FACT_DESCRIPTION_LENGTH)

    @field_validator("acceptable_variants", "linked_evidence_refs", mode="before")
    @classmethod
    def _tuple_fields(cls, value: Any, info: Any) -> tuple[Any, ...]:
        return _tuple_value(value, info.field_name)

    @field_validator("acceptable_variants")
    @classmethod
    def _validate_variants(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_texts(value, "acceptable_variants", MAX_ANSWER_VARIANT_LENGTH)

    @field_validator("linked_evidence_refs")
    @classmethod
    def _validate_links(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_texts(value, "linked_evidence_refs", MAX_IDENTITY_LENGTH)


class EvaluationExecutionInput(_EvaluationModel):
    """The only case projection intended for production execution."""

    media_ref: str = Field(alias="mediaRef")
    query: str
    mode: AnalysisMode

    @field_validator("media_ref", mode="before")
    @classmethod
    def _validate_media_ref(cls, value: Any) -> str:
        return _media_ref(value)

    @field_validator("query", mode="before")
    @classmethod
    def _validate_query(cls, value: Any) -> str:
        return _text(value, "query", MAX_QUERY_LENGTH)


class EvaluationJudgeInput(_EvaluationModel):
    """A post-execution projection that is allowed to include gold data."""

    case_id: str = Field(alias="caseId")
    query: str
    mode: AnalysisMode
    model_output: Any = Field(alias="modelOutput")
    required_facts: tuple[RequiredFact, ...] = Field(alias="requiredFacts")
    expected_evidence_refs: tuple[ExpectedEvidenceRef, ...] = Field(alias="expectedEvidenceRefs")
    expected_temporal_regions: tuple[ExpectedTemporalRegion, ...] = Field(
        alias="expectedTemporalRegions"
    )
    reference_answer: str | None = Field(default=None, alias="referenceAnswer")


def _media_ref(value: Any) -> str:
    normalized = _text(value, "media_ref", MAX_MEDIA_REF_LENGTH)
    if _ABSOLUTE_WINDOWS_PATH_RE.match(normalized) or normalized.startswith(("/", "\\\\")):
        raise ValueError("media_ref must not be an absolute local path")
    if any(part == ".." for part in re.split(r"[\\/]", normalized)):
        raise ValueError("media_ref must not escape its artifact root")
    return normalized


class EvaluationCase(_EvaluationModel):
    case_id: str = Field(alias="caseId")
    dataset_version: str = Field(alias="datasetVersion")
    media_ref: str = Field(alias="mediaRef")
    source_revision: str = Field(alias="sourceRevision")
    query: str
    mode: AnalysisMode
    query_category: EvaluationQueryCategory = Field(alias="queryCategory")
    expected_evidence_refs: tuple[ExpectedEvidenceRef, ...] = Field(
        default=(), alias="expectedEvidenceRefs"
    )
    expected_temporal_regions: tuple[ExpectedTemporalRegion, ...] = Field(
        default=(), alias="expectedTemporalRegions"
    )
    allowed_source_types: tuple[SourceType, ...] = Field(default=(), alias="allowedSourceTypes")
    required_facts: tuple[RequiredFact, ...] = Field(default=(), alias="requiredFacts")
    acceptable_answer_variants: tuple[str, ...] = Field(
        default=(), alias="acceptableAnswerVariants"
    )
    reference_answer: str | None = Field(default=None, alias="referenceAnswer")
    difficulty: EvaluationDifficulty = EvaluationDifficulty.MEDIUM
    tags: tuple[str, ...] = ()
    tool_beneficial: StrictBool = Field(default=False, alias="toolBeneficial")
    critic_sensitive: StrictBool = Field(default=False, alias="criticSensitive")
    annotation_version: str = Field(
        default=DEFAULT_ANNOTATION_VERSION,
        alias="annotationVersion",
    )

    @field_validator("case_id", mode="before")
    @classmethod
    def _validate_case_id(cls, value: Any) -> str:
        return _text(value, "case_id", MAX_CASE_ID_LENGTH)

    @field_validator("dataset_version", "annotation_version", mode="before")
    @classmethod
    def _validate_versions(cls, value: Any, info: Any) -> str:
        return _version(value, info.field_name)

    @field_validator("media_ref", mode="before")
    @classmethod
    def _validate_media(cls, value: Any) -> str:
        return _media_ref(value)

    @field_validator("source_revision", mode="before")
    @classmethod
    def _validate_source_revision(cls, value: Any) -> str:
        return _source_revision(value)

    @field_validator("query", mode="before")
    @classmethod
    def _validate_query(cls, value: Any) -> str:
        return _text(value, "query", MAX_QUERY_LENGTH)

    @field_validator("expected_evidence_refs", "expected_temporal_regions", "required_facts", "allowed_source_types", mode="before")
    @classmethod
    def _tuple_fields(cls, value: Any, info: Any) -> tuple[Any, ...]:
        return _tuple_value(value, info.field_name)

    @field_validator("acceptable_answer_variants", "tags", mode="before")
    @classmethod
    def _tuple_text_fields(cls, value: Any, info: Any) -> tuple[Any, ...]:
        return _tuple_value(value, info.field_name)

    @field_validator("acceptable_answer_variants")
    @classmethod
    def _validate_answer_variants(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_texts(value, "acceptable_answer_variants", MAX_ANSWER_VARIANT_LENGTH)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_TAGS:
            raise ValueError("tags exceed their bound")
        return _unique_texts(value, "tags", MAX_TAG_LENGTH)

    @field_validator("reference_answer", mode="before")
    @classmethod
    def _validate_reference_answer(cls, value: Any) -> str | None:
        return _optional_text(value, "reference_answer", MAX_ANSWER_VARIANT_LENGTH)

    @model_validator(mode="after")
    def _validate_annotation(self) -> "EvaluationCase":
        evidence_keys: set[str] = set()
        link_keys: set[str] = set()
        for ref in self.expected_evidence_refs:
            key = ref.identity_key()
            if key in evidence_keys:
                raise ValueError("expected_evidence_refs contains a duplicate reference")
            evidence_keys.add(key)
            link_keys.update(ref.link_keys())
            if ref.source_revision != self.source_revision:
                raise ValueError("evidence ref source_revision does not match the case")
            if self.allowed_source_types and ref.source_type is not None:
                if ref.source_type not in self.allowed_source_types:
                    raise ValueError("evidence ref source_type is not allowed by the case")

        fact_ids: set[str] = set()
        for fact in self.required_facts:
            if fact.fact_id in fact_ids:
                raise ValueError("required_facts contains a duplicate fact_id")
            fact_ids.add(fact.fact_id)
            unknown_links = set(fact.linked_evidence_refs).difference(link_keys)
            if unknown_links:
                raise ValueError("required fact links an unknown evidence reference")

        return self

    def execution_input(self) -> dict[str, Any]:
        """Return the non-gold input projection for a production-like runner."""

        projection = {
            "media_ref": self.media_ref,
            "query": self.query,
            "mode": self.mode.value,
        }
        return {field: projection[field] for field in RUNTIME_CASE_FIELDS}

    def judge_input(self, model_output: Any) -> dict[str, Any]:
        """Return the post-execution projection allowed to a future judge."""

        return {
            "case_id": self.case_id,
            "query": self.query,
            "mode": self.mode.value,
            "model_output": model_output,
            "required_facts": [
                fact.model_dump(mode="json", by_alias=False)
                for fact in self.required_facts
            ],
            "expected_evidence_refs": [
                ref.model_dump(mode="json", by_alias=False)
                for ref in self.expected_evidence_refs
            ],
            "expected_temporal_regions": [
                region.model_dump(mode="json", by_alias=False)
                for region in self.expected_temporal_regions
            ],
            "reference_answer": self.reference_answer,
        }


class EvaluationDataset(_EvaluationModel):
    dataset_version: str = Field(default=GOLDEN_DATASET_VERSION, alias="datasetVersion")
    evaluation_contract_version: str = Field(
        default=EVALUATION_CONTRACT_VERSION,
        alias="evaluationContractVersion",
    )
    annotation_version: str = Field(
        default=DEFAULT_ANNOTATION_VERSION,
        alias="annotationVersion",
    )
    created_at: datetime | None = Field(default=None, alias="createdAt")
    description: str = ""
    cases: tuple[EvaluationCase, ...] = ()
    case_count: int | None = Field(default=None, alias="caseCount")
    source_revision_set: tuple[str, ...] = Field(default=(), alias="sourceRevisionSet")
    target_case_count: int = Field(default=MVP_GOLDEN_CASE_COUNT, alias="targetCaseCount")
    completeness: DatasetCompleteness = DatasetCompleteness.INCOMPLETE
    data_classification: DataClassification = Field(
        default=DataClassification.ILLUSTRATIVE,
        alias="dataClassification",
    )
    dataset_digest: str | None = Field(default=None, alias="datasetDigest")

    @field_validator("dataset_version", "evaluation_contract_version", "annotation_version", mode="before")
    @classmethod
    def _validate_versions(cls, value: Any, info: Any) -> str:
        return _version(value, info.field_name)

    @field_validator("created_at", mode="after")
    @classmethod
    def _validate_created_at(cls, value: datetime | None) -> datetime | None:
        return _aware_datetime(value, "created_at")

    @field_validator("description", mode="before")
    @classmethod
    def _validate_description(cls, value: Any) -> str:
        if value is None:
            return ""
        return _text(value, "description", MAX_DESCRIPTION_LENGTH, allow_empty=True)

    @field_validator("cases", "source_revision_set", mode="before")
    @classmethod
    def _tuple_fields(cls, value: Any, info: Any) -> tuple[Any, ...]:
        return _tuple_value(value, info.field_name)

    @field_validator("case_count", "target_case_count", mode="before")
    @classmethod
    def _validate_counts(cls, value: Any, info: Any) -> int | None:
        if value is None and info.field_name == "case_count":
            return None
        return _positive_int(value, info.field_name)

    @field_validator("source_revision_set")
    @classmethod
    def _validate_revision_set(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        revisions = tuple(_source_revision(item, "source_revision_set") for item in value)
        if len(set(revisions)) != len(revisions):
            raise ValueError("source_revision_set contains duplicates")
        return revisions

    @model_validator(mode="after")
    def _validate_dataset(self) -> "EvaluationDataset":
        if not self.cases:
            raise ValueError("evaluation dataset must contain at least one case")
        case_ids: set[str] = set()
        revisions: set[str] = set()
        for case in self.cases:
            if case.case_id in case_ids:
                raise ValueError("evaluation dataset contains duplicate case_id")
            case_ids.add(case.case_id)
            if case.dataset_version != self.dataset_version:
                raise ValueError("case dataset_version does not match dataset")
            if case.annotation_version != self.annotation_version:
                raise ValueError("case annotation_version does not match dataset")
            revisions.add(case.source_revision)

        computed_revisions = tuple(sorted(revisions))
        if self.source_revision_set and self.source_revision_set != computed_revisions:
            raise ValueError("source_revision_set does not match dataset cases")
        if self.case_count is not None and self.case_count != len(self.cases):
            raise ValueError("case_count does not match dataset cases")
        if self.completeness is DatasetCompleteness.COMPLETE and len(self.cases) < self.target_case_count:
            raise ValueError("complete dataset has fewer cases than target_case_count")

        object.__setattr__(self, "case_count", len(self.cases))
        object.__setattr__(self, "source_revision_set", computed_revisions)
        digest = _dataset_digest(self)
        if self.dataset_digest is not None and self.dataset_digest != digest:
            raise ValueError("dataset_digest does not match normalized dataset content")
        object.__setattr__(self, "dataset_digest", digest)
        return self

    def case(self, case_id: str) -> EvaluationCase:
        for item in self.cases:
            if item.case_id == case_id:
                return item
        raise KeyError(case_id)

    def to_json(self) -> str:
        return canonical_json(self)

    @classmethod
    def from_json(cls, payload: str | bytes | bytearray) -> "EvaluationDataset":
        return parse_json(payload, cls)


class EvaluationRun(_EvaluationModel):
    run_id: str = Field(default_factory=lambda: str(uuid4()), alias="runId")
    evaluation_contract_version: str = Field(
        default=EVALUATION_CONTRACT_VERSION,
        alias="evaluationContractVersion",
    )
    dataset_version: str = Field(alias="datasetVersion")
    dataset_digest: str | None = Field(default=None, alias="datasetDigest")
    runner_config_version: str = Field(
        default=DEFAULT_RUNNER_CONFIG_VERSION,
        alias="runnerConfigVersion",
    )
    config_fingerprint: str | None = Field(default=None, alias="configFingerprint")
    pricing_version: str | None = Field(default=None, alias="pricingVersion")
    git_sha: str | None = Field(default=None, alias="gitSha")
    working_tree_state: WorkingTreeState = Field(
        default=WorkingTreeState.UNKNOWN,
        alias="workingTreeState",
    )
    strategy: EvaluationStrategy = EvaluationStrategy.ALWAYS_BALANCED
    trial_index: int = Field(default=0, alias="trialIndex")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    completed_at: datetime | None = Field(default=None, alias="completedAt")
    environment: dict[str, str] = {}
    planned_count: int = Field(default=0, alias="plannedCount")
    executed_count: int = Field(default=0, alias="executedCount")
    successful_count: int = Field(default=0, alias="successfulCount")
    failed_count: int = Field(default=0, alias="failedCount")
    excluded_count: int = Field(default=0, alias="excludedCount")
    not_run_count: int = Field(default=0, alias="notRunCount")
    execution_order: tuple[str, ...] = Field(default=(), alias="executionOrder")
    status: EvaluationRunStatus = EvaluationRunStatus.PLANNED
    data_classification: DataClassification = Field(
        default=DataClassification.ILLUSTRATIVE,
        alias="dataClassification",
    )

    @field_validator("run_id", mode="before")
    @classmethod
    def _validate_run_id(cls, value: Any) -> str:
        return _text(value, "run_id", MAX_IDENTITY_LENGTH)

    @field_validator("evaluation_contract_version", "dataset_version", "runner_config_version", mode="before")
    @classmethod
    def _validate_versions(cls, value: Any, info: Any) -> str:
        return _version(value, info.field_name)

    @field_validator("dataset_digest", "config_fingerprint", mode="before")
    @classmethod
    def _validate_optional_digest(cls, value: Any, info: Any) -> str | None:
        if value is None:
            return None
        return _text(value, info.field_name, 256)

    @field_validator("pricing_version", mode="before")
    @classmethod
    def _validate_pricing_version(cls, value: Any) -> str | None:
        return None if value is None else _version(value, "pricing_version")

    @field_validator("git_sha", mode="before")
    @classmethod
    def _validate_git_sha(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = _text(value, "git_sha", 64).lower()
        if not re.fullmatch(r"[0-9a-f]{7,64}", normalized):
            raise ValueError("git_sha must be a hexadecimal commit identity")
        return normalized

    @field_validator("trial_index", "planned_count", "executed_count", "successful_count", "failed_count", "excluded_count", "not_run_count", mode="before")
    @classmethod
    def _validate_counts(cls, value: Any, info: Any) -> int:
        return _nonnegative_int(value, info.field_name)

    @field_validator("started_at", "completed_at", mode="after")
    @classmethod
    def _validate_times(cls, value: datetime | None, info: Any) -> datetime | None:
        return _aware_datetime(value, info.field_name)

    @field_validator("environment", mode="before")
    @classmethod
    def _validate_environment(cls, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("environment must be an object")
        if len(value) > MAX_ENVIRONMENT_ITEMS:
            raise ValueError("environment has too many items")
        output: dict[str, str] = {}
        for key, item in value.items():
            normalized_key = _text(key, "environment key", MAX_ENVIRONMENT_KEY_LENGTH).lower()
            if any(secret in normalized_key for secret in ("key", "secret", "password", "token", "credential")):
                raise ValueError("environment must not contain credential-like fields")
            output[normalized_key] = _text(item, f"environment[{normalized_key}]", MAX_ENVIRONMENT_VALUE_LENGTH)
        return output

    @field_validator("execution_order", mode="before")
    @classmethod
    def _validate_execution_order(cls, value: Any) -> tuple[str, ...]:
        values = _tuple_value(value, "execution_order")
        output: list[str] = []
        for item in values:
            output.append(_text(item, "execution_order item", MAX_IDENTITY_LENGTH))
        return tuple(output)

    @model_validator(mode="after")
    def _validate_count_relationships(self) -> "EvaluationRun":
        if self.executed_count > self.planned_count:
            raise ValueError("executed_count cannot exceed planned_count")
        if self.not_run_count > self.planned_count:
            raise ValueError("not_run_count cannot exceed planned_count")
        if self.executed_count + self.not_run_count > self.planned_count:
            raise ValueError("executed_count plus not_run_count cannot exceed planned_count")
        if self.successful_count + self.failed_count + self.excluded_count > self.executed_count:
            raise ValueError("terminal counts cannot exceed executed_count")
        if self.completed_at is not None and self.started_at is not None:
            if self.completed_at < self.started_at:
                raise ValueError("completed_at cannot precede started_at")
        return self

    @property
    def publishable(self) -> bool:
        return bool(self.git_sha) and self.working_tree_state is not WorkingTreeState.UNKNOWN

    def to_json(self) -> str:
        return canonical_json(self)

    @classmethod
    def from_json(cls, payload: str | bytes | bytearray) -> "EvaluationRun":
        return parse_json(payload, cls)


class TokenStageUsage(_EvaluationModel):
    input_tokens: int | None = Field(default=None, alias="inputTokens")
    output_tokens: int | None = Field(default=None, alias="outputTokens")
    total_tokens: int | None = Field(default=None, alias="totalTokens")
    provider_reported: bool | None = Field(default=None, alias="providerReported")
    measurement_state: MeasurementState = Field(
        default=MeasurementState.NOT_MEASURED,
        alias="measurementState",
    )

    @field_validator("input_tokens", "output_tokens", "total_tokens", mode="before")
    @classmethod
    def _validate_tokens(cls, value: Any, info: Any) -> int | None:
        return None if value is None else _nonnegative_int(value, info.field_name)

    @model_validator(mode="after")
    def _validate_measurement_state(self) -> "TokenStageUsage":
        values = (self.input_tokens, self.output_tokens, self.total_tokens)
        if any(value is not None for value in values) and self.measurement_state is not MeasurementState.MEASURED:
            raise ValueError("token values require measurement_state=MEASURED")
        return self


class TokenUsageMeasurement(_EvaluationModel):
    input_tokens: int | None = Field(default=None, alias="inputTokens")
    output_tokens: int | None = Field(default=None, alias="outputTokens")
    total_tokens: int | None = Field(default=None, alias="totalTokens")
    provider_reported: bool | None = Field(default=None, alias="providerReported")
    measurement_state: MeasurementState = Field(
        default=MeasurementState.NOT_MEASURED,
        alias="measurementState",
    )
    planner: TokenStageUsage | None = None
    executor: TokenStageUsage | None = None
    critic: TokenStageUsage | None = None
    router: TokenStageUsage | None = None

    @field_validator("input_tokens", "output_tokens", "total_tokens", mode="before")
    @classmethod
    def _validate_tokens(cls, value: Any, info: Any) -> int | None:
        return None if value is None else _nonnegative_int(value, info.field_name)

    @model_validator(mode="after")
    def _validate_measurement_state(self) -> "TokenUsageMeasurement":
        values = (self.input_tokens, self.output_tokens, self.total_tokens)
        if any(value is not None for value in values) and self.measurement_state is not MeasurementState.MEASURED:
            raise ValueError("token values require measurement_state=MEASURED")
        return self


class CostMeasurement(_EvaluationModel):
    provider_reported_cost: float | None = Field(default=None, alias="providerReportedCost")
    calculated_cost: float | None = Field(default=None, alias="calculatedCost")
    currency: str | None = None
    pricing_version: str | None = Field(default=None, alias="pricingVersion")
    measurement_state: MeasurementState = Field(
        default=MeasurementState.NOT_MEASURED,
        alias="measurementState",
    )

    @field_validator("provider_reported_cost", "calculated_cost", mode="before")
    @classmethod
    def _validate_costs(cls, value: Any, info: Any) -> float | None:
        return None if value is None else _finite_float(value, info.field_name)

    @field_validator("currency", mode="before")
    @classmethod
    def _validate_currency(cls, value: Any) -> str | None:
        return None if value is None else _text(value, "currency", 16)

    @field_validator("pricing_version", mode="before")
    @classmethod
    def _validate_pricing(cls, value: Any) -> str | None:
        return None if value is None else _version(value, "pricing_version")

    @model_validator(mode="after")
    def _validate_measurement_state(self) -> "CostMeasurement":
        if (
            self.provider_reported_cost is not None or self.calculated_cost is not None
        ) and self.measurement_state is not MeasurementState.MEASURED:
            raise ValueError("cost values require measurement_state=MEASURED")
        if self.calculated_cost is not None and not self.pricing_version:
            raise ValueError("calculated_cost requires pricing_version")
        return self


class LatencyMeasurement(_EvaluationModel):
    routing_ms: float | None = Field(default=None, alias="routingMs")
    retrieval_ms: float | None = Field(default=None, alias="retrievalMs")
    planner_ms: float | None = Field(default=None, alias="plannerMs")
    executor_ms: float | None = Field(default=None, alias="executorMs")
    critic_ms: float | None = Field(default=None, alias="criticMs")
    tool_ms: float | None = Field(default=None, alias="toolMs")
    total_e2e_ms: float | None = Field(default=None, alias="totalE2eMs")
    cold_warm: ColdWarmMarker = Field(default=ColdWarmMarker.UNSPECIFIED, alias="coldWarm")
    measurement_state: MeasurementState = Field(
        default=MeasurementState.NOT_MEASURED,
        alias="measurementState",
    )

    @field_validator(
        "routing_ms",
        "retrieval_ms",
        "planner_ms",
        "executor_ms",
        "critic_ms",
        "tool_ms",
        "total_e2e_ms",
        mode="before",
    )
    @classmethod
    def _validate_latency(cls, value: Any, info: Any) -> float | None:
        return None if value is None else _finite_float(value, info.field_name)

    @model_validator(mode="after")
    def _validate_measurement_state(self) -> "LatencyMeasurement":
        values = (
            self.routing_ms,
            self.retrieval_ms,
            self.planner_ms,
            self.executor_ms,
            self.critic_ms,
            self.tool_ms,
            self.total_e2e_ms,
        )
        if any(value is not None for value in values) and self.measurement_state is not MeasurementState.MEASURED:
            raise ValueError("latency values require measurement_state=MEASURED")
        return self


class RoutingMeasurement(_EvaluationModel):
    suggested_lane: ModelRouteLane | None = Field(default=None, alias="suggestedLane")
    resolved_lane: ModelRouteLane | None = Field(default=None, alias="resolvedLane")
    confidence: float | None = None
    fallback: bool | None = None
    reason_code: ModelRoutingReasonCode | None = Field(default=None, alias="reasonCode")
    resolved_model_id: str | None = Field(default=None, alias="resolvedModelId")
    measurement_state: MeasurementState = Field(
        default=MeasurementState.NOT_MEASURED,
        alias="measurementState",
    )

    @field_validator("confidence", mode="before")
    @classmethod
    def _validate_confidence(cls, value: Any) -> float | None:
        return None if value is None else _bounded_ratio(value, "confidence")

    @field_validator("resolved_model_id", mode="before")
    @classmethod
    def _validate_model_id(cls, value: Any) -> str | None:
        return None if value is None else _text(value, "resolved_model_id", MAX_MODEL_ID_LENGTH)

    @model_validator(mode="after")
    def _validate_measurement_state(self) -> "RoutingMeasurement":
        values = (
            self.suggested_lane,
            self.resolved_lane,
            self.confidence,
            self.fallback,
            self.reason_code,
            self.resolved_model_id,
        )
        if any(value is not None for value in values) and self.measurement_state is not MeasurementState.MEASURED:
            raise ValueError("routing values require measurement_state=MEASURED")
        return self


class DeterministicMetrics(_EvaluationModel):
    retrieval_k: int | None = Field(default=None, alias="retrievalK")
    retrieval_recall_at_k: float | None = Field(default=None, alias="retrievalRecallAtK")
    retrieval_precision_at_k: float | None = Field(default=None, alias="retrievalPrecisionAtK")
    retrieval_recall_at_k_by_k: dict[str, float] | None = Field(
        default=None,
        alias="retrievalRecallAtKByK",
    )
    retrieval_precision_at_k_by_k: dict[str, float] | None = Field(
        default=None,
        alias="retrievalPrecisionAtKByK",
    )
    mrr: float | None = None
    temporal_hit: bool | None = Field(default=None, alias="temporalHit")
    temporal_coverage: float | None = Field(default=None, alias="temporalCoverage")
    evidence_precision: float | None = Field(default=None, alias="evidencePrecision")
    evidence_recall: float | None = Field(default=None, alias="evidenceRecall")
    evidence_support_rate: float | None = Field(default=None, alias="evidenceSupportRate")
    unsupported_claim_rate: float | None = Field(default=None, alias="unsupportedClaimRate")
    required_fact_coverage: float | None = Field(default=None, alias="requiredFactCoverage")
    required_fact_exact_coverage: float | None = Field(
        default=None,
        alias="requiredFactExactCoverage",
    )
    schema_valid: bool | None = Field(default=None, alias="schemaValid")
    mode_sections_valid: bool | None = Field(default=None, alias="modeSectionsValid")
    evidence_guard_pass: bool | None = Field(default=None, alias="evidenceGuardPass")
    measurement_state: MeasurementState = Field(
        default=MeasurementState.NOT_MEASURED,
        alias="measurementState",
    )

    @field_validator("retrieval_k", mode="before")
    @classmethod
    def _validate_k(cls, value: Any) -> int | None:
        return None if value is None else _positive_int(value, "retrieval_k")

    @field_validator(
        "retrieval_recall_at_k",
        "retrieval_precision_at_k",
        "mrr",
        "temporal_coverage",
        "evidence_precision",
        "evidence_recall",
        "evidence_support_rate",
        "unsupported_claim_rate",
        "required_fact_coverage",
        "required_fact_exact_coverage",
        mode="before",
    )
    @classmethod
    def _validate_ratios(cls, value: Any, info: Any) -> float | None:
        return None if value is None else _bounded_ratio(value, info.field_name)

    @model_validator(mode="after")
    def _validate_measurement_state(self) -> "DeterministicMetrics":
        for field_name, values in (
            ("retrieval_recall_at_k_by_k", self.retrieval_recall_at_k_by_k),
            ("retrieval_precision_at_k_by_k", self.retrieval_precision_at_k_by_k),
        ):
            if values is not None:
                if not values:
                    raise ValueError(f"{field_name} must not be empty")
                for key, value in values.items():
                    if not isinstance(key, str) or not key.isdigit() or int(key) <= 0:
                        raise ValueError(f"{field_name} keys must be positive K values")
                    _bounded_ratio(value, f"{field_name}[{key}]")
        values = (
            self.retrieval_k,
            self.retrieval_recall_at_k,
            self.retrieval_precision_at_k,
            self.mrr,
            self.temporal_hit,
            self.temporal_coverage,
            self.evidence_precision,
            self.evidence_recall,
            self.evidence_support_rate,
            self.unsupported_claim_rate,
            self.required_fact_coverage,
            self.required_fact_exact_coverage,
            self.schema_valid,
            self.mode_sections_valid,
            self.evidence_guard_pass,
        )
        if (
            (any(value is not None for value in values)
             or self.retrieval_recall_at_k_by_k is not None
             or self.retrieval_precision_at_k_by_k is not None)
            and self.measurement_state is not MeasurementState.MEASURED
        ):
            raise ValueError("deterministic metric values require measurement_state=MEASURED")
        return self


class HumanMetrics(_EvaluationModel):
    factual_correctness: float | None = Field(default=None, alias="factualCorrectness")
    completeness: float | None = None
    usefulness: float | None = None
    requirement_satisfaction: float | None = Field(default=None, alias="requirementSatisfaction")
    annotator_reference: str | None = Field(default=None, alias="annotatorReference")
    rubric_version: str | None = Field(default=None, alias="rubricVersion")
    measurement_state: MeasurementState = Field(
        default=MeasurementState.NOT_MEASURED,
        alias="measurementState",
    )

    @field_validator("factual_correctness", "completeness", "usefulness", "requirement_satisfaction", mode="before")
    @classmethod
    def _validate_ratios(cls, value: Any, info: Any) -> float | None:
        return None if value is None else _bounded_ratio(value, info.field_name)

    @field_validator("annotator_reference", mode="before")
    @classmethod
    def _validate_annotator(cls, value: Any) -> str | None:
        return None if value is None else _text(value, "annotator_reference", MAX_IDENTITY_LENGTH)

    @field_validator("rubric_version", mode="before")
    @classmethod
    def _validate_rubric(cls, value: Any) -> str | None:
        return None if value is None else _version(value, "rubric_version")

    @model_validator(mode="after")
    def _validate_measurement_state(self) -> "HumanMetrics":
        values = (
            self.factual_correctness,
            self.completeness,
            self.usefulness,
            self.requirement_satisfaction,
        )
        if any(value is not None for value in values):
            if self.measurement_state is not MeasurementState.MEASURED:
                raise ValueError("human metric values require measurement_state=MEASURED")
            if not self.annotator_reference or not self.rubric_version:
                raise ValueError("human metrics require annotator_reference and rubric_version")
        return self


class JudgeMetrics(_EvaluationModel):
    judge_model: str | None = Field(default=None, alias="judgeModel")
    judge_prompt_version: str | None = Field(default=None, alias="judgePromptVersion")
    judge_rubric_version: str | None = Field(default=None, alias="judgeRubricVersion")
    score: float | None = None
    result: str | None = None
    measurement_state: MeasurementState = Field(
        default=MeasurementState.NOT_MEASURED,
        alias="measurementState",
    )

    @field_validator("judge_model", "result", mode="before")
    @classmethod
    def _validate_text_fields(cls, value: Any, info: Any) -> str | None:
        return None if value is None else _text(value, info.field_name, MAX_REASON_LENGTH)

    @field_validator("judge_prompt_version", "judge_rubric_version", mode="before")
    @classmethod
    def _validate_versions(cls, value: Any, info: Any) -> str | None:
        return None if value is None else _version(value, info.field_name)

    @field_validator("score", mode="before")
    @classmethod
    def _validate_score(cls, value: Any) -> float | None:
        return None if value is None else _finite_float(value, "score")

    @model_validator(mode="after")
    def _validate_measurement_state(self) -> "JudgeMetrics":
        if self.score is not None or self.result is not None:
            if self.measurement_state is not MeasurementState.MEASURED:
                raise ValueError("judge values require measurement_state=MEASURED")
            if not self.judge_model or not self.judge_prompt_version or not self.judge_rubric_version:
                raise ValueError("judge metrics require model, prompt version, and rubric version")
        return self


class ToolMetrics(_EvaluationModel):
    tool_requests: int | None = Field(default=None, alias="toolRequests")
    tool_allowed: int | None = Field(default=None, alias="toolAllowed")
    tool_denied: int | None = Field(default=None, alias="toolDenied")
    tool_executed: int | None = Field(default=None, alias="toolExecuted")
    tool_failures: int | None = Field(default=None, alias="toolFailures")
    tool_result_truncated: int | None = Field(default=None, alias="toolResultTruncated")
    tool_recovered: int | None = Field(default=None, alias="toolRecovered")
    tool_result_reused: int | None = Field(default=None, alias="toolResultReused")
    measurement_state: MeasurementState = Field(
        default=MeasurementState.NOT_MEASURED,
        alias="measurementState",
    )

    @field_validator(
        "tool_requests",
        "tool_allowed",
        "tool_denied",
        "tool_executed",
        "tool_failures",
        "tool_result_truncated",
        "tool_recovered",
        "tool_result_reused",
        mode="before",
    )
    @classmethod
    def _validate_counts(cls, value: Any, info: Any) -> int | None:
        return None if value is None else _nonnegative_int(value, info.field_name)

    @model_validator(mode="after")
    def _validate_measurement_state(self) -> "ToolMetrics":
        values = (
            self.tool_requests,
            self.tool_allowed,
            self.tool_denied,
            self.tool_executed,
            self.tool_failures,
            self.tool_result_truncated,
            self.tool_recovered,
            self.tool_result_reused,
        )
        if any(value is not None for value in values) and self.measurement_state is not MeasurementState.MEASURED:
            raise ValueError("tool metric values require measurement_state=MEASURED")
        return self


class CriticMetrics(_EvaluationModel):
    first_pass: bool | None = Field(default=None, alias="firstPass")
    final_pass: bool | None = Field(default=None, alias="finalPass")
    quality_delta: float | None = Field(default=None, alias="qualityDelta")
    additional_rounds: int | None = Field(default=None, alias="additionalRounds")
    token_overhead: int | None = Field(default=None, alias="tokenOverhead")
    latency_overhead_ms: float | None = Field(default=None, alias="latencyOverheadMs")
    critic_call_count: int | None = Field(default=None, alias="criticCallCount")
    measurement_state: MeasurementState = Field(
        default=MeasurementState.NOT_MEASURED,
        alias="measurementState",
    )

    @field_validator("quality_delta", "latency_overhead_ms", mode="before")
    @classmethod
    def _validate_nonnegative_values(cls, value: Any, info: Any) -> float | None:
        return None if value is None else _finite_float(value, info.field_name)

    @field_validator("additional_rounds", "token_overhead", "critic_call_count", mode="before")
    @classmethod
    def _validate_counts(cls, value: Any, info: Any) -> int | None:
        return None if value is None else _nonnegative_int(value, info.field_name)

    @model_validator(mode="after")
    def _validate_measurement_state(self) -> "CriticMetrics":
        values = (
            self.first_pass,
            self.final_pass,
            self.quality_delta,
            self.additional_rounds,
            self.token_overhead,
            self.latency_overhead_ms,
            self.critic_call_count,
        )
        if any(value is not None for value in values) and self.measurement_state is not MeasurementState.MEASURED:
            raise ValueError("critic metric values require measurement_state=MEASURED")
        return self


class EvaluationCaseResult(_EvaluationModel):
    run_id: str = Field(alias="runId")
    case_id: str = Field(alias="caseId")
    strategy: EvaluationStrategy
    trial_index: int = Field(alias="trialIndex")
    mode: AnalysisMode
    query_category: EvaluationQueryCategory = Field(alias="queryCategory")
    resolved_model: str | None = Field(default=None, alias="resolvedModel")
    route_decision: RoutingMeasurement = Field(
        default_factory=RoutingMeasurement,
        alias="routeDecision",
    )
    source_revision: str = Field(alias="sourceRevision")
    deterministic_metrics: DeterministicMetrics = Field(
        default_factory=DeterministicMetrics,
        alias="deterministicMetrics",
    )
    human_metrics: HumanMetrics = Field(default_factory=HumanMetrics, alias="humanMetrics")
    judge_metrics: JudgeMetrics = Field(default_factory=JudgeMetrics, alias="judgeMetrics")
    token_usage: TokenUsageMeasurement = Field(default_factory=TokenUsageMeasurement, alias="tokenUsage")
    cost: CostMeasurement = Field(default_factory=CostMeasurement)
    latency: LatencyMeasurement = Field(default_factory=LatencyMeasurement)
    tool_metrics: ToolMetrics = Field(default_factory=ToolMetrics, alias="toolMetrics")
    critic_metrics: CriticMetrics = Field(default_factory=CriticMetrics, alias="criticMetrics")
    planner_call_count: int | None = Field(default=None, alias="plannerCallCount")
    executor_call_count: int | None = Field(default=None, alias="executorCallCount")
    cold_warm: ColdWarmMarker = Field(default=ColdWarmMarker.UNSPECIFIED, alias="coldWarm")
    status: EvaluationResultStatus = EvaluationResultStatus.PLANNED
    failure_category: EvaluationFailureCategory | None = Field(
        default=None,
        alias="failureCategory",
    )
    exclusion_category: EvaluationExclusionCategory | None = Field(
        default=None,
        alias="exclusionCategory",
    )
    exclusion_reason: str | None = Field(default=None, alias="exclusionReason")
    execution_order: int | None = Field(default=None, alias="executionOrder")
    underlying_execution_id: str | None = Field(
        default=None,
        alias="underlyingExecutionId",
    )
    result_digest: str | None = Field(default=None, alias="resultDigest")
    data_classification: DataClassification = Field(
        default=DataClassification.ILLUSTRATIVE,
        alias="dataClassification",
    )

    @field_validator("run_id", "case_id", mode="before")
    @classmethod
    def _validate_ids(cls, value: Any, info: Any) -> str:
        return _text(value, info.field_name, MAX_IDENTITY_LENGTH)

    @field_validator("trial_index", mode="before")
    @classmethod
    def _validate_trial(cls, value: Any) -> int:
        return _nonnegative_int(value, "trial_index")

    @field_validator("execution_order", mode="before")
    @classmethod
    def _validate_execution_order(cls, value: Any) -> int | None:
        return None if value is None else _nonnegative_int(value, "execution_order")

    @field_validator("planner_call_count", "executor_call_count", mode="before")
    @classmethod
    def _validate_call_counts(cls, value: Any, info: Any) -> int | None:
        return None if value is None else _nonnegative_int(value, info.field_name)

    @field_validator("resolved_model", mode="before")
    @classmethod
    def _validate_model(cls, value: Any) -> str | None:
        return None if value is None else _text(value, "resolved_model", MAX_MODEL_ID_LENGTH)

    @field_validator("underlying_execution_id", mode="before")
    @classmethod
    def _validate_underlying_execution_id(cls, value: Any) -> str | None:
        return None if value is None else _text(value, "underlying_execution_id", MAX_IDENTITY_LENGTH)

    @field_validator("source_revision", mode="before")
    @classmethod
    def _validate_revision(cls, value: Any) -> str:
        return _source_revision(value)

    @field_validator("exclusion_reason", mode="before")
    @classmethod
    def _validate_exclusion_reason(cls, value: Any) -> str | None:
        return None if value is None else _text(value, "exclusion_reason", MAX_REASON_LENGTH)

    @model_validator(mode="after")
    def _validate_status(self) -> "EvaluationCaseResult":
        if self.status is EvaluationResultStatus.FAILED and self.failure_category is None:
            raise ValueError("FAILED result requires failure_category")
        if self.status is EvaluationResultStatus.EXCLUDED and (
            self.exclusion_category is None or not self.exclusion_reason
        ):
            raise ValueError("EXCLUDED result requires exclusion_category and exclusion_reason")
        if self.status is not EvaluationResultStatus.FAILED and self.failure_category is not None:
            raise ValueError("failure_category is only valid for FAILED results")
        if self.status is not EvaluationResultStatus.EXCLUDED and (
            self.exclusion_category is not None or self.exclusion_reason is not None
        ):
            raise ValueError("exclusion fields are only valid for EXCLUDED results")

        digest = _result_digest(self)
        if self.result_digest is not None and self.result_digest != digest:
            raise ValueError("result_digest does not match normalized result content")
        object.__setattr__(self, "result_digest", digest)
        return self

    def identity(self) -> tuple[str, EvaluationStrategy, int]:
        """Return the stable case × strategy × trial identity."""

        return (self.case_id, self.strategy, self.trial_index)

    def to_json(self) -> str:
        return canonical_json(self)

    @classmethod
    def from_json(cls, payload: str | bytes | bytearray) -> "EvaluationCaseResult":
        return parse_json(payload, cls)


def _normalized_model_payload(model: BaseModel, *, exclude: set[str]) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude=exclude)


def _dataset_digest(dataset: EvaluationDataset) -> str:
    payload = _normalized_model_payload(
        dataset,
        exclude={"case_count", "source_revision_set", "dataset_digest", "created_at"},
    )
    return sha256_canonical(payload)


def _result_digest(result: EvaluationCaseResult) -> str:
    payload = _normalized_model_payload(result, exclude={"result_digest"})
    return sha256_canonical(payload)


def validate_dataset(dataset: EvaluationDataset) -> EvaluationDataset:
    """Validate and return a dataset without repairing any annotation."""

    return EvaluationDataset.model_validate(dataset.model_dump(mode="json", by_alias=True))


def validate_case_result_identities(results: Iterable[EvaluationCaseResult]) -> tuple[EvaluationCaseResult, ...]:
    """Reject duplicate case × strategy × trial rows in a result collection."""

    output = tuple(results)
    seen: set[tuple[str, EvaluationStrategy, int]] = set()
    for result in output:
        identity = result.identity()
        if identity in seen:
            raise ValueError("duplicate EvaluationCaseResult case × strategy × trial identity")
        seen.add(identity)
    return output


def canonical_json(value: BaseModel | Mapping[str, Any] | list[Any]) -> str:
    """Serialize an evaluation payload deterministically as UTF-8 JSON text."""

    if isinstance(value, BaseModel):
        document = value.model_dump(mode="json", by_alias=True, exclude_none=False)
    else:
        document = value
    try:
        return json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise EvaluationContractError("evaluation payload is not JSON serializable") from error


def parse_json(payload: str | bytes | bytearray, target_type: type[Any]) -> Any:
    try:
        document = json.loads(payload)
    except Exception as error:
        raise EvaluationContractError("evaluation JSON is invalid") from error
    try:
        return target_type.model_validate(document)
    except Exception as error:
        raise EvaluationContractError("evaluation JSON failed contract validation") from error


def dataset_to_json(dataset: EvaluationDataset) -> str:
    return canonical_json(dataset)


def dataset_from_json(payload: str | bytes | bytearray) -> EvaluationDataset:
    return parse_json(payload, EvaluationDataset)


def run_to_json(run: EvaluationRun) -> str:
    return canonical_json(run)


def run_from_json(payload: str | bytes | bytearray) -> EvaluationRun:
    return parse_json(payload, EvaluationRun)


def case_result_to_json(result: EvaluationCaseResult) -> str:
    return canonical_json(result)


def case_result_from_json(payload: str | bytes | bytearray) -> EvaluationCaseResult:
    return parse_json(payload, EvaluationCaseResult)


def case_results_to_jsonl(results: Iterable[EvaluationCaseResult]) -> str:
    validated = validate_case_result_identities(results)
    return "".join(case_result_to_json(item) + "\n" for item in validated)


def case_results_from_jsonl(payload: str | bytes | bytearray) -> tuple[EvaluationCaseResult, ...]:
    text = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else payload
    results: list[EvaluationCaseResult] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise EvaluationContractError(f"blank JSONL line at {line_number}")
        try:
            results.append(case_result_from_json(line))
        except EvaluationContractError as error:
            raise EvaluationContractError(
                f"invalid EvaluationCaseResult at JSONL line {line_number}"
            ) from error
    return validate_case_result_identities(results)


def write_dataset(path: str | Path, dataset: EvaluationDataset) -> None:
    Path(path).write_text(dataset_to_json(dataset) + "\n", encoding="utf-8")


def read_dataset(path: str | Path) -> EvaluationDataset:
    return dataset_from_json(Path(path).read_text(encoding="utf-8"))


def write_run(path: str | Path, run: EvaluationRun) -> None:
    Path(path).write_text(run_to_json(run) + "\n", encoding="utf-8")


def read_run(path: str | Path) -> EvaluationRun:
    return run_from_json(Path(path).read_text(encoding="utf-8"))


def write_case_results(path: str | Path, results: Iterable[EvaluationCaseResult]) -> None:
    Path(path).write_text(case_results_to_jsonl(results), encoding="utf-8")


def read_case_results(path: str | Path) -> tuple[EvaluationCaseResult, ...]:
    return case_results_from_jsonl(Path(path).read_text(encoding="utf-8"))


class EvaluationJsonCodec:
    """Small typed facade for later X3 runner slices."""

    dumps = staticmethod(canonical_json)
    loads_dataset = staticmethod(dataset_from_json)
    loads_run = staticmethod(run_from_json)
    loads_case_result = staticmethod(case_result_from_json)
    dumps_jsonl = staticmethod(case_results_to_jsonl)
    loads_jsonl = staticmethod(case_results_from_jsonl)


# Friendly aliases for callers that prefer the ticket terminology.
ExpectedEvidenceMatch = ExpectedEvidenceMatchLevel
EvaluationArtifactClassification = DataClassification
ResultStatus = EvaluationResultStatus
FailureCategory = EvaluationFailureCategory
ExclusionCategory = EvaluationExclusionCategory
EvaluationMode = AnalysisMode
QueryCategory = EvaluationQueryCategory
Difficulty = EvaluationDifficulty
MatchLevel = ExpectedEvidenceMatchLevel
RunStatus = EvaluationRunStatus
CaseResultStatus = EvaluationResultStatus
StageTokenUsage = TokenStageUsage
EvaluationMetricState = MeasurementState


__all__ = [
    "ColdWarmMarker",
    "CostMeasurement",
    "CriticMetrics",
    "DataClassification",
    "DatasetCompleteness",
    "DEFAULT_ANNOTATION_VERSION",
    "DEFAULT_RUNNER_CONFIG_VERSION",
    "DeterministicMetrics",
    "EVALUATION_CONTRACT_VERSION",
    "EvaluationArtifactClassification",
    "EvaluationCase",
    "EvaluationCaseResult",
    "EvaluationContractError",
    "EvaluationDataset",
    "EvaluationDifficulty",
    "EvaluationExecutionInput",
    "EvaluationFailureCategory",
    "EvaluationExclusionCategory",
    "EvaluationMetricState",
    "EvaluationMode",
    "EvaluationJsonCodec",
    "EvaluationJudgeInput",
    "EvaluationQueryCategory",
    "EvaluationResultStatus",
    "EvaluationRun",
    "EvaluationRunStatus",
    "EvaluationStrategy",
    "ExpectedEvidenceMatch",
    "ExpectedEvidenceMatchLevel",
    "ExpectedEvidenceRef",
    "ExpectedTemporalRegion",
    "ExclusionCategory",
    "Difficulty",
    "FailureCategory",
    "GOLDEN_DATASET_VERSION",
    "HumanMetrics",
    "JudgeMetrics",
    "LatencyMeasurement",
    "MatchLevel",
    "MeasurementState",
    "MVP_GOLDEN_CASE_COUNT",
    "RequiredFact",
    "ResultStatus",
    "RunStatus",
    "RoutingMeasurement",
    "TokenStageUsage",
    "TokenUsageMeasurement",
    "ToolMetrics",
    "WorkingTreeState",
    "QueryCategory",
    "RUNTIME_CASE_FIELDS",
    "ANNOTATION_ONLY_CASE_FIELDS",
    "CaseResultStatus",
    "StageTokenUsage",
    "canonical_json",
    "case_result_from_json",
    "case_result_to_json",
    "case_results_from_jsonl",
    "case_results_to_jsonl",
    "dataset_from_json",
    "dataset_to_json",
    "parse_json",
    "read_case_results",
    "read_dataset",
    "read_run",
    "run_from_json",
    "run_to_json",
    "validate_case_result_identities",
    "validate_dataset",
    "write_case_results",
    "write_dataset",
    "write_run",
]
