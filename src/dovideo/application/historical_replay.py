"""Deterministic, read-only replay of one X2-B execution record.

X2-C intentionally has a much smaller dependency graph than the live Agent
loop.  It reads the durable execution record and, only when the historical
record references a tool result, the existing X1 durable tool ledger.  It does
not construct a Planner, retrieval service, Provider adapter, ToolPolicy,
ToolExecutor, checkpoint service, or task dispatcher.

The service projects immutable historical facts into bounded DTOs.  It does
not attempt to re-run decisions or to manufacture missing history.  Unknown
versions, malformed event sequences, missing tool artifacts, and incomplete
terminal histories fail closed with typed errors.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator, model_validator

from dovideo.domain import (
    AgentPlan,
    AnalysisMode,
    AnalysisResult,
    CriticResult,
    PROVENANCE_VERSION,
)
from dovideo.domain._base import DomainModel, tuple_or_empty

from .execution_record import (
    EXECUTION_CONTRACT_VERSION,
    EXECUTION_CONTRACT_VERSION_V1,
    EXECUTION_CONTRACT_VERSION_V2,
    EXECUTION_RECORD_SCHEMA_VERSION,
    MAX_EXECUTION_EVENT_PAYLOAD_BYTES,
    MAX_EXECUTION_EVENTS,
    MAX_EXECUTION_RETRIEVAL_REFS,
    MODE_PROFILE_CONTRACT_VERSION,
    TOOL_POLICY_CONTRACT_VERSION,
    DurableAgentExecutionRecord,
    DurableExecutionEvent,
    ExecutionEventType,
    ExecutionRecordPersistenceError,
    ExecutionRecordStatus,
    ExecutionTaskKey,
)
from .model_routing import (
    MODEL_ROUTING_CONTRACT_VERSION,
    ModelRouteHistory,
)
from .ports.checkpoint import ToolCallCheckpointPort
from .tool_contracts import (
    ExecutorTurnKind,
    PolicyDecision,
    ToolName,
    ToolPolicyReasonCode,
    ToolResult,
    ToolResultStatus,
)
from .tool_state import (
    DurableToolExecutionState,
    DurableToolStateLedger,
)
from .value_objects import TaskKey


MAX_REPLAY_TEXT_LENGTH = 4_096
MAX_REPLAY_DIGEST_LENGTH = 128
MAX_REPLAY_TOOL_CALLS = MAX_EXECUTION_EVENTS


class HistoricalReplayError(RuntimeError):
    """Base class for safe, typed historical replay failures."""

    code = "REPLAY_ERROR"

    def __init__(self, detail: str = "historical replay failed") -> None:
        # Error details are deliberately bounded and never contain payloads,
        # provider output, credentials, or raw tool arguments.
        normalized = " ".join(str(detail).split())[:256]
        super().__init__(f"{self.code}: {normalized or 'historical replay failed'}")
        self.detail = normalized


class ReplayNotFoundError(HistoricalReplayError):
    code = "REPLAY_NOT_FOUND"


class ReplayLegacyUnavailableError(HistoricalReplayError):
    code = "REPLAY_UNAVAILABLE_LEGACY"


class ReplayIncompleteError(HistoricalReplayError):
    code = "REPLAY_INCOMPLETE"


class ReplayIncompatibleVersionError(HistoricalReplayError):
    code = "REPLAY_INCOMPATIBLE_VERSION"


class ReplayIntegrityError(HistoricalReplayError):
    code = "REPLAY_INTEGRITY_ERROR"


class ReplayUnknownEventError(ReplayIntegrityError):
    code = "REPLAY_UNKNOWN_EVENT"


class ReplayArtifactUnavailableError(HistoricalReplayError):
    code = "REPLAY_ARTIFACT_UNAVAILABLE"


class ReplayPersistenceError(HistoricalReplayError):
    code = "REPLAY_PERSISTENCE_ERROR"


def _bounded_text(value: Any, field: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ReplayIntegrityError(f"{field} is not text")
    normalized = value.strip()
    if required and not normalized:
        raise ReplayIntegrityError(f"{field} is blank")
    if len(normalized) > MAX_REPLAY_TEXT_LENGTH:
        raise ReplayIntegrityError(f"{field} exceeds its bound")
    return normalized


def _bounded_optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field)


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReplayIntegrityError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReplayIntegrityError(f"{field} must be a non-negative integer")
    return value


def _bounded_digest(value: Any, field: str) -> str:
    normalized = _bounded_text(value, field)
    if normalized == "unvalidated":
        return normalized
    if len(normalized) != 64 or any(
        character not in "0123456789abcdefABCDEF" for character in normalized
    ):
        raise ReplayIntegrityError(f"{field} is not a SHA-256 digest")
    return normalized.lower()


def _json_value(value: Any) -> Any:
    """Copy a historical payload into deterministic JSON-shaped values."""

    if isinstance(value, Mapping):
        return {
            str(key): _json_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ReplayIntegrityError("payload contains a non-finite number")
        return value
    if hasattr(value, "value") and isinstance(value.value, (str, int)):
        return value.value
    raise ReplayIntegrityError("payload contains a non-JSON value")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            _json_value(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ReplayIntegrityError("historical value is not canonical JSON") from error


_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "apikey",
        "authorization",
        "credentials",
        "credential",
        "embedding",
        "headers",
        "httpbody",
        "password",
        "prompt",
        "providerkey",
        "providerresponse",
        "rawbody",
        "rawproviderresponse",
        "rawresponse",
        "rawquery",
        "secret",
        "systemprompt",
        "token",
    }
)


def _assert_safe_payload(value: Any) -> None:
    """Reject accidental provider/secret material in a corrupt event payload."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized_key = str(key).replace("_", "").lower()
            if normalized_key in _FORBIDDEN_PAYLOAD_KEYS or any(
                marker in normalized_key
                for marker in (
                    "apikey",
                    "providerkey",
                    "secret",
                    "credential",
                    "password",
                    "prompt",
                    "embedding",
                    "authorization",
                    "rawresponse",
                    "rawbody",
                    "httpbody",
                    "providertoken",
                )
            ):
                raise ReplayIntegrityError("historical payload contains forbidden material")
            _assert_safe_payload(nested)
    elif isinstance(value, (tuple, list)):
        for nested in value:
            _assert_safe_payload(nested)


class HistoricalReplayEventView(DomainModel):
    """Bounded, normalized projection of one durable semantic event."""

    sequence_no: int = Field(alias="sequenceNo")
    event_type: ExecutionEventType = Field(alias="eventType")
    logical_event_id: str = Field(alias="logicalEventId")
    agent_round: int = Field(default=0, alias="agentRound")
    stage: str = "AGENT"
    payload: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("sequence_no")
    @classmethod
    def _sequence(cls, value: int) -> int:
        if isinstance(value, bool) or value < 1:
            raise ValueError("sequence_no must be positive")
        return value

    @field_validator("agent_round")
    @classmethod
    def _round(cls, value: int) -> int:
        if isinstance(value, bool) or value < 0:
            raise ValueError("agent_round must be non-negative")
        return value

    @field_validator("logical_event_id", "stage")
    @classmethod
    def _text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > MAX_REPLAY_TEXT_LENGTH:
            raise ValueError("replay event text is blank or too long")
        return normalized


class HistoricalSourceReference(DomainModel):
    """Only the bounded source identity retained by X2-B retrieval history."""

    segment_id: str = Field(default="", alias="segmentId")
    chunk_id: str = Field(default="", alias="chunkId")
    source_revision: str = Field(default="", alias="sourceRevision")
    start_ms: int | None = Field(default=None, alias="startMs")
    end_ms: int | None = Field(default=None, alias="endMs")
    score: float | None = None
    source_item_ids: tuple[str, ...] = Field(default=(), alias="sourceItemIds")

    @field_validator("segment_id", "chunk_id", "source_revision")
    @classmethod
    def _text(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) > MAX_REPLAY_TEXT_LENGTH:
            raise ValueError("source reference text is too long")
        return normalized

    @field_validator("source_item_ids", mode="before")
    @classmethod
    def _items(cls, value: Any) -> tuple[Any, ...]:
        return tuple_or_empty(value)

    @field_validator("source_item_ids")
    @classmethod
    def _bounded_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 8:
            raise ValueError("too many source item references")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("source item reference is blank")
            result.append(item.strip()[:128])
        return tuple(result)

    @model_validator(mode="after")
    def _bounds(self) -> "HistoricalSourceReference":
        for name in ("start_ms", "end_ms"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or value < 0):
                raise ValueError("source timestamps must be non-negative")
        if self.start_ms is not None and self.end_ms is not None and self.end_ms < self.start_ms:
            raise ValueError("source reference end precedes start")
        return self


class HistoricalRetrievalSelection(DomainModel):
    """One recorded retrieval selection, with no query or vector payload."""

    purpose: str = ""
    selected: tuple[HistoricalSourceReference, ...] = ()
    selected_count: int = Field(default=0, alias="selectedCount")
    budget_truncated: bool = Field(default=False, alias="budgetTruncated")
    query_digest: str | None = Field(default=None, alias="queryDigest")

    @field_validator("purpose")
    @classmethod
    def _purpose(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) > 64:
            raise ValueError("retrieval purpose is too long")
        return normalized

    @field_validator("selected", mode="before")
    @classmethod
    def _selected(cls, value: Any) -> tuple[Any, ...]:
        return tuple_or_empty(value)

    @field_validator("selected_count")
    @classmethod
    def _count(cls, value: int) -> int:
        if isinstance(value, bool) or value < 0 or value > MAX_EXECUTION_RETRIEVAL_REFS:
            raise ValueError("retrieval selection count is out of bounds")
        return value

    @field_validator("query_digest")
    @classmethod
    def _query_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if len(normalized) > MAX_REPLAY_DIGEST_LENGTH:
            raise ValueError("retrieval digest is too long")
        return normalized

    @model_validator(mode="after")
    def _selection_count(self) -> "HistoricalRetrievalSelection":
        if self.selected_count != len(self.selected):
            raise ValueError("retrieval selectedCount does not match selected refs")
        return self


class HistoricalExecutorTurn(DomainModel):
    """Executor fact without raw tool arguments or provider output."""

    kind: ExecutorTurnKind
    agent_round: int = Field(alias="agentRound")
    request_index: int | None = Field(default=None, alias="requestIndex")
    call_id: str | None = Field(default=None, alias="callId")
    tool_name: str | None = Field(default=None, alias="toolName")
    args_digest: str | None = Field(default=None, alias="argsDigest")
    final_result: AnalysisResult | None = Field(default=None, alias="finalResult")

    @field_validator("agent_round")
    @classmethod
    def _round(cls, value: int) -> int:
        if isinstance(value, bool) or value < 0:
            raise ValueError("executor round must be non-negative")
        return value

    @field_validator("request_index")
    @classmethod
    def _request_index(cls, value: int | None) -> int | None:
        if value is not None and (isinstance(value, bool) or value < 1):
            raise ValueError("executor request index must be positive")
        return value

    @field_validator("call_id", "tool_name")
    @classmethod
    def _identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 128:
            raise ValueError("executor identity is blank or too long")
        return normalized

    @field_validator("args_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_digest(value, "executor argsDigest")

    @model_validator(mode="after")
    def _branch(self) -> "HistoricalExecutorTurn":
        if self.kind is ExecutorTurnKind.FINAL:
            if self.final_result is None or any(
                value is not None
                for value in (self.request_index, self.call_id, self.tool_name, self.args_digest)
            ):
                raise ValueError("historical FINAL turn has the wrong branch")
        elif (
            self.final_result is not None
            or self.request_index is None
            or self.call_id is None
            or self.tool_name is None
            or self.args_digest is None
        ):
            raise ValueError("historical TOOL_REQUEST turn has the wrong branch")
        return self


class HistoricalReplayToolCall(DomainModel):
    """Recorded tool reference joined to its existing X1 durable result."""

    agent_round: int = Field(alias="agentRound")
    request_index: int = Field(alias="requestIndex")
    call_id: str = Field(alias="callId")
    tool_name: str = Field(alias="toolName")
    args_digest: str = Field(alias="argsDigest")
    policy_decision: PolicyDecision | None = Field(default=None, alias="policyDecision")
    reason_code: ToolPolicyReasonCode | None = Field(default=None, alias="reasonCode")
    result_status: ToolResultStatus = Field(alias="resultStatus")
    ledger_reference: str = Field(alias="ledgerReference")
    tool_result: ToolResult | None = Field(default=None, alias="toolResult")

    @field_validator("agent_round")
    @classmethod
    def _round(cls, value: int) -> int:
        if isinstance(value, bool) or value < 0:
            raise ValueError("tool round must be non-negative")
        return value

    @field_validator("request_index")
    @classmethod
    def _request_index(cls, value: int) -> int:
        if isinstance(value, bool) or value < 1:
            raise ValueError("tool request index must be positive")
        return value

    @field_validator("call_id", "tool_name", "ledger_reference")
    @classmethod
    def _text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 192:
            raise ValueError("tool identity is blank or too long")
        return normalized

    @field_validator("args_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _bounded_digest(value, "tool argsDigest")


class HistoricalEvidenceReference(DomainModel):
    """Bounded evidence identity retained by an evidence-guard event."""

    timestamp_ms: int = Field(default=0, alias="timestampMs")
    source_revision: str = Field(alias="sourceRevision")
    segment_id: str = Field(default="", alias="segmentId")
    source_item_ids: tuple[str, ...] = Field(default=(), alias="sourceItemIds")

    @field_validator("timestamp_ms")
    @classmethod
    def _timestamp(cls, value: int) -> int:
        if isinstance(value, bool) or value < 0:
            raise ValueError("evidence timestamp must be non-negative")
        return value

    @field_validator("source_revision", "segment_id")
    @classmethod
    def _text(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) > MAX_REPLAY_TEXT_LENGTH:
            raise ValueError("evidence reference text is too long")
        return normalized

    @field_validator("source_item_ids", mode="before")
    @classmethod
    def _items(cls, value: Any) -> tuple[Any, ...]:
        return tuple_or_empty(value)

    @field_validator("source_item_ids")
    @classmethod
    def _bounded_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 8:
            raise ValueError("too many evidence source item references")
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("evidence source item reference is blank")
        return tuple(item.strip()[:128] for item in value)


class HistoricalEvidenceVerification(DomainModel):
    """Historical Evidence Guard outcome, not a re-run of the guard."""

    passed: bool
    feedback: tuple[str, ...] = ()
    missing_requirements: tuple[str, ...] = Field(default=(), alias="missingRequirements")
    unsupported_claims: tuple[str, ...] = Field(default=(), alias="unsupportedClaims")
    required_timestamps: tuple[int, ...] = Field(default=(), alias="requiredTimestamps")
    source_revision: str = Field(alias="sourceRevision")
    evidence_references: tuple[HistoricalEvidenceReference, ...] = Field(
        default=(), alias="evidenceReferences"
    )

    @field_validator(
        "feedback",
        "missing_requirements",
        "unsupported_claims",
        "required_timestamps",
        "evidence_references",
        mode="before",
    )
    @classmethod
    def _collections(cls, value: Any) -> tuple[Any, ...]:
        return tuple_or_empty(value)

    @field_validator("source_revision")
    @classmethod
    def _source_revision(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("evidence verification source revision is required")
        return normalized

    @field_validator("required_timestamps")
    @classmethod
    def _timestamps(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if len(value) > MAX_EXECUTION_RETRIEVAL_REFS:
            raise ValueError("too many required evidence timestamps")
        for timestamp in value:
            if isinstance(timestamp, bool) or timestamp < 0:
                raise ValueError("required evidence timestamps must be non-negative")
        return value


class HistoricalReplayFailure(DomainModel):
    """Bounded failure classification from a terminal failed record."""

    classification: str = "EXECUTION_FAILED"
    error_type: str = Field(default="", alias="errorType")
    retryable: bool = False

    @field_validator("classification", "error_type")
    @classmethod
    def _text(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) > 96:
            raise ValueError("failure classification is too long")
        return normalized


class HistoricalReplayState(DomainModel):
    """Bounded in-memory state accumulated strictly in sequence order."""

    execution_id: str = Field(alias="executionId")
    task_key: ExecutionTaskKey = Field(alias="taskKey")
    mode: AnalysisMode
    source_revision: str = Field(alias="sourceRevision")
    historical_model_route: ModelRouteHistory | None = Field(
        default=None,
        alias="historicalModelRoute",
    )
    plans: tuple[AgentPlan, ...] = ()
    retrievals: tuple[HistoricalRetrievalSelection, ...] = ()
    executor_turns: tuple[HistoricalExecutorTurn, ...] = ()
    tool_calls: tuple[HistoricalReplayToolCall, ...] = ()
    critics: tuple[CriticResult, ...] = ()
    evidence_verifications: tuple[HistoricalEvidenceVerification, ...] = ()
    final_result: AnalysisResult | None = None
    failure: HistoricalReplayFailure | None = None

    @model_validator(mode="after")
    def _bounded(self) -> "HistoricalReplayState":
        for collection in (
            self.plans,
            self.retrievals,
            self.executor_turns,
            self.tool_calls,
            self.critics,
            self.evidence_verifications,
        ):
            if len(collection) > MAX_EXECUTION_EVENTS:
                raise ValueError("historical replay state exceeds its bound")
        return self


class HistoricalReplayResult(DomainModel):
    """Public deterministic projection returned by X2-C."""

    execution_id: str = Field(alias="executionId")
    task_key: ExecutionTaskKey = Field(alias="taskKey")
    media_identity: str = Field(alias="mediaIdentity")
    source_revision: str = Field(alias="sourceRevision")
    source_provenance_version: str = Field(alias="sourceProvenanceVersion")
    mode: AnalysisMode
    record_schema_version: str = Field(alias="recordSchemaVersion")
    execution_contract_version: str = Field(alias="executionContractVersion")
    mode_profile_version: str = Field(alias="modeProfileVersion")
    tool_policy_version: str = Field(alias="toolPolicyVersion")
    status: ExecutionRecordStatus
    events_replayed: int = Field(alias="eventsReplayed")
    last_sequence_no: int = Field(alias="lastSequenceNo")
    historical_model_route: ModelRouteHistory | None = Field(
        default=None,
        alias="historicalModelRoute",
    )
    events: tuple[HistoricalReplayEventView, ...] = ()
    state: HistoricalReplayState
    historical_plans: tuple[AgentPlan, ...] = Field(default=(), alias="historicalPlans")
    historical_plan: AgentPlan | None = Field(default=None, alias="historicalPlan")
    historical_retrievals: tuple[HistoricalRetrievalSelection, ...] = Field(
        default=(), alias="historicalRetrievals"
    )
    historical_executor_turns: tuple[HistoricalExecutorTurn, ...] = Field(
        default=(), alias="historicalExecutorTurns"
    )
    historical_tool_calls: tuple[HistoricalReplayToolCall, ...] = Field(
        default=(), alias="historicalToolCalls"
    )
    historical_critic_results: tuple[CriticResult, ...] = Field(
        default=(), alias="historicalCriticResults"
    )
    historical_evidence_outcomes: tuple[HistoricalEvidenceVerification, ...] = Field(
        default=(), alias="historicalEvidenceOutcomes"
    )
    historical_final_result: AnalysisResult | None = Field(
        default=None, alias="historicalFinalResult"
    )
    failure: HistoricalReplayFailure | None = None

    @field_validator("events_replayed", "last_sequence_no")
    @classmethod
    def _counts(cls, value: int) -> int:
        if isinstance(value, bool) or value < 0 or value > MAX_EXECUTION_EVENTS:
            raise ValueError("replay count is out of bounds")
        return value

    @model_validator(mode="after")
    def _projection_matches_state(self) -> "HistoricalReplayResult":
        if (
            self.state.execution_id != self.execution_id
            or self.state.task_key != self.task_key
            or self.state.mode is not self.mode
            or self.state.source_revision != self.source_revision
            or self.state.historical_model_route != self.historical_model_route
        ):
            raise ValueError("replay state identity does not match result header")
        if self.events_replayed != len(self.events):
            raise ValueError("eventsReplayed does not match events")
        if self.last_sequence_no != (self.events[-1].sequence_no if self.events else 0):
            raise ValueError("lastSequenceNo does not match events")
        if self.historical_plans != self.state.plans:
            raise ValueError("historical plans do not match replay state")
        if self.historical_retrievals != self.state.retrievals:
            raise ValueError("historical retrievals do not match replay state")
        if self.historical_executor_turns != self.state.executor_turns:
            raise ValueError("historical executor turns do not match replay state")
        if self.historical_tool_calls != self.state.tool_calls:
            raise ValueError("historical tool calls do not match replay state")
        if self.historical_critic_results != self.state.critics:
            raise ValueError("historical critics do not match replay state")
        if self.historical_evidence_outcomes != self.state.evidence_verifications:
            raise ValueError("historical evidence outcomes do not match replay state")
        if self.historical_final_result != self.state.final_result:
            raise ValueError("historical final result does not match replay state")
        if self.failure != self.state.failure:
            raise ValueError("historical failure does not match replay state")
        if self.historical_plan != (self.historical_plans[-1] if self.historical_plans else None):
            raise ValueError("historicalPlan does not select the latest recorded plan")
        return self

    @property
    def historical_failure(self) -> HistoricalReplayFailure | None:
        """Compatibility spelling for the terminal historical failure."""

        return self.failure

    @property
    def current_plan(self) -> AgentPlan | None:
        """Latest plan in the in-memory replay state."""

        return self.historical_plan


class HistoricalAgentReplayService:
    """Read and replay one durable execution record without live dependencies."""

    def __init__(
        self,
        execution_records: Any | None = None,
        *,
        execution_record_service: Any | None = None,
        execution_record_repository: Any | None = None,
        tool_checkpoint: ToolCallCheckpointPort | None = None,
    ) -> None:
        sources = [
            source
            for source in (
                execution_records,
                execution_record_service,
                execution_record_repository,
            )
            if source is not None
        ]
        if len(sources) != 1:
            raise ValueError("exactly one execution-record source is required")
        self._execution_records = sources[0]
        self._tool_checkpoint = tool_checkpoint

    async def replay(
        self,
        execution_id: str,
        expected_task_key: TaskKey | ExecutionTaskKey | None = None,
        *,
        task_key: TaskKey | ExecutionTaskKey | None = None,
    ) -> HistoricalReplayResult:
        """Replay one execution by durable ID; never create or mutate state."""

        if expected_task_key is not None and task_key is not None:
            if _coerce_task_key(expected_task_key) != _coerce_task_key(task_key):
                raise ReplayIntegrityError("conflicting trusted task keys")
        trusted_key = task_key if task_key is not None else expected_task_key
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise ReplayNotFoundError("execution id is blank")
        record = await self._load_record(execution_id.strip())
        if record is None:
            # A missing X2-B record is deliberately not promoted from legacy
            # checkpoint state.  The caller receives a typed unavailable
            # result and the replay service performs no checkpoint reads.
            raise ReplayNotFoundError("durable execution record was not found")
        return await self._replay_record(
            record,
            trusted_key=trusted_key,
            expected_execution_id=execution_id.strip(),
        )

    replay_execution = replay

    async def _load_record(self, execution_id: str) -> Any:
        loader = getattr(self._execution_records, "load", None)
        if not callable(loader):
            loader = getattr(self._execution_records, "get", None)
        if not callable(loader):
            raise ReplayPersistenceError("execution-record source has no read operation")
        try:
            value = loader(execution_id)
            if inspect.isawaitable(value):
                value = await value
            return value
        except HistoricalReplayError:
            raise
        except ExecutionRecordPersistenceError as error:
            raise ReplayPersistenceError("execution-record read failed") from error
        except Exception as error:
            raise ReplayPersistenceError("execution-record read failed") from error

    async def _replay_record(
        self,
        record: Any,
        *,
        trusted_key: TaskKey | ExecutionTaskKey | None,
        expected_execution_id: str,
    ) -> HistoricalReplayResult:
        record = self._coerce_record(record)
        task_key = self._validate_header(
            record,
            trusted_key=trusted_key,
            expected_execution_id=expected_execution_id,
        )
        events = self._ordered_events(record)
        status = self._validate_terminal_history(record, events)

        views: list[HistoricalReplayEventView] = []
        plans: list[AgentPlan] = []
        retrievals: list[HistoricalRetrievalSelection] = []
        executor_turns: list[HistoricalExecutorTurn] = []
        tool_calls: list[HistoricalReplayToolCall] = []
        critics: list[CriticResult] = []
        evidence_outcomes: list[HistoricalEvidenceVerification] = []
        historical_model_route: ModelRouteHistory | None = None
        route_event_sequence: int | None = None
        first_plan_sequence: int | None = None
        final_result: AnalysisResult | None = None
        completion_final_result: AnalysisResult | None = None
        completion_final_critic: CriticResult | None = None
        completion_final_evidence: Mapping[str, Any] | None = None
        failure: HistoricalReplayFailure | None = None
        tool_requests: dict[int, HistoricalExecutorTurn] = {}
        tool_request_sequences: dict[int, int] = {}
        tool_reference_sequences: dict[int, int] = {}

        for event in events:
            payload = self._event_payload(event)
            event_type = ExecutionEventType(event.event_type)
            views.append(
                HistoricalReplayEventView(
                    sequenceNo=event.sequence_no,
                    eventType=event_type.value,
                    logicalEventId=event.logical_event_id,
                    agentRound=event.agent_round,
                    stage=event.stage,
                    payload=payload,
                )
            )

            if event_type is ExecutionEventType.EXECUTION_STARTED:
                self._parse_started(payload, record, task_key)
            elif event_type is ExecutionEventType.MODEL_ROUTE_RECORDED:
                if historical_model_route is not None:
                    raise ReplayIntegrityError(
                        "execution contains multiple MODEL_ROUTE_RECORDED events"
                    )
                historical_model_route = self._parse_model_route(payload)
                route_event_sequence = event.sequence_no
            elif event_type in {
                ExecutionEventType.PLAN_RECORDED,
                ExecutionEventType.PLAN_REPAIRED,
                ExecutionEventType.PLAN_REPLANNED,
            }:
                if first_plan_sequence is None:
                    first_plan_sequence = event.sequence_no
                if payload.get("kind") not in {None, event_type.value}:
                    raise ReplayIntegrityError("plan event kind is inconsistent")
                plans.append(self._parse_plan(payload))
            elif event_type is ExecutionEventType.RETRIEVAL_SELECTED:
                retrievals.append(self._parse_retrieval(payload, record.source_revision))
            elif event_type is ExecutionEventType.EXECUTOR_TURN_RECORDED:
                turn = self._parse_executor_turn(payload, event.agent_round, record.source_revision)
                executor_turns.append(turn)
                if turn.kind is ExecutorTurnKind.TOOL_REQUEST:
                    assert turn.request_index is not None
                    if turn.request_index in tool_requests:
                        raise ReplayIntegrityError("duplicate historical tool request index")
                    tool_requests[turn.request_index] = turn
                    tool_request_sequences[turn.request_index] = event.sequence_no
                else:
                    assert turn.final_result is not None
                    final_result = turn.final_result
            elif event_type is ExecutionEventType.TOOL_CALL_REFERENCED:
                reference = self._parse_tool_reference(payload, event.agent_round)
                if reference.request_index in tool_reference_sequences:
                    raise ReplayIntegrityError("duplicate historical tool reference")
                tool_reference_sequences[reference.request_index] = event.sequence_no
                tool_calls.append(reference)
            elif event_type is ExecutionEventType.CRITIC_RECORDED:
                critics.append(self._parse_critic(payload))
            elif event_type is ExecutionEventType.EVIDENCE_VERIFICATION_RECORDED:
                evidence_outcomes.append(
                    self._parse_evidence(payload, record.source_revision)
                )
            elif event_type is ExecutionEventType.EXECUTION_COMPLETED:
                (
                    completion_final_result,
                    completion_final_critic,
                    completion_final_evidence,
                ) = self._parse_completion(payload, record.source_revision)
            elif event_type is ExecutionEventType.EXECUTION_FAILED:
                failure = self._parse_failure(payload)
            else:  # pragma: no cover - enum exhaustiveness guard
                raise ReplayUnknownEventError("unsupported historical event")

        historical_tool_calls = await self._resolve_tool_references(
            record,
            task_key,
            tool_calls,
            tool_requests,
            tool_request_sequences,
            tool_reference_sequences,
        )
        self._validate_cross_event_history(
            record=record,
            status=status,
            plans=plans,
            retrievals=retrievals,
            executor_turns=executor_turns,
            tool_calls=historical_tool_calls,
            tool_requests=tool_requests,
            critics=critics,
            evidence_outcomes=evidence_outcomes,
            historical_model_route=historical_model_route,
            route_event_sequence=route_event_sequence,
            first_plan_sequence=first_plan_sequence,
            final_result=final_result,
            completion_final_result=completion_final_result,
            completion_final_critic=completion_final_critic,
            completion_final_evidence=completion_final_evidence,
            failure=failure,
        )

        state = HistoricalReplayState(
            executionId=record.execution_id,
            taskKey=record.task_key,
            mode=record.mode,
            sourceRevision=record.source_revision,
            historicalModelRoute=historical_model_route,
            plans=tuple(plans),
            retrievals=tuple(retrievals),
            executor_turns=tuple(executor_turns),
            tool_calls=tuple(historical_tool_calls),
            critics=tuple(critics),
            evidence_verifications=tuple(evidence_outcomes),
            final_result=final_result,
            failure=failure,
        )
        latest_plan = plans[-1] if plans else None
        return HistoricalReplayResult(
            executionId=record.execution_id,
            taskKey=record.task_key,
            mediaIdentity=record.media_identity,
            sourceRevision=record.source_revision,
            sourceProvenanceVersion=record.source_provenance_version,
            mode=record.mode,
            recordSchemaVersion=record.record_schema_version,
            executionContractVersion=record.execution_contract_version,
            modeProfileVersion=record.mode_profile_version,
            toolPolicyVersion=record.tool_policy_version,
            status=status,
            eventsReplayed=len(views),
            lastSequenceNo=views[-1].sequence_no,
            historicalModelRoute=historical_model_route,
            events=tuple(views),
            state=state,
            historicalPlans=tuple(plans),
            historicalPlan=latest_plan,
            historicalRetrievals=tuple(retrievals),
            historicalExecutorTurns=tuple(executor_turns),
            historicalToolCalls=tuple(historical_tool_calls),
            historicalCriticResults=tuple(critics),
            historicalEvidenceOutcomes=tuple(evidence_outcomes),
            historicalFinalResult=final_result,
            failure=failure,
        )

    def _validate_header(
        self,
        record: Any,
        *,
        trusted_key: TaskKey | ExecutionTaskKey | None,
        expected_execution_id: str,
    ) -> TaskKey:
        if not isinstance(record, DurableAgentExecutionRecord):
            raise ReplayIntegrityError("durable execution header is invalid")

        if not isinstance(record.execution_id, str) or not record.execution_id.strip():
            raise ReplayIntegrityError("execution id is blank")
        if record.execution_id != expected_execution_id:
            raise ReplayIntegrityError("loaded record identity does not match request")
        if record.record_schema_version != EXECUTION_RECORD_SCHEMA_VERSION:
            raise ReplayIncompatibleVersionError("record schema version is unsupported")
        if record.execution_contract_version not in {
            EXECUTION_CONTRACT_VERSION_V1,
            EXECUTION_CONTRACT_VERSION_V2,
        }:
            raise ReplayIncompatibleVersionError("execution contract version is unsupported")
        if record.source_provenance_version != PROVENANCE_VERSION:
            raise ReplayIncompatibleVersionError("source provenance version is unsupported")
        if record.mode_profile_version != MODE_PROFILE_CONTRACT_VERSION:
            raise ReplayIncompatibleVersionError("mode profile version is unsupported")
        if record.tool_policy_version != TOOL_POLICY_CONTRACT_VERSION:
            raise ReplayIncompatibleVersionError("tool policy version is unsupported")
        if not isinstance(record.media_identity, str) or not record.media_identity.strip():
            raise ReplayIntegrityError("media identity is blank")
        if not isinstance(record.source_revision, str) or not record.source_revision.strip():
            raise ReplayIntegrityError("source revision is blank")
        try:
            key = _coerce_task_key(record.task_key)
            mode = AnalysisMode(record.mode)
        except Exception as error:
            raise ReplayIntegrityError("execution task identity is invalid") from error
        if mode is not key.mode:
            raise ReplayIntegrityError("record mode does not match task key")
        if trusted_key is not None and _coerce_task_key(trusted_key) != key:
            raise ReplayIntegrityError("trusted task identity does not match record")
        return key

    @staticmethod
    def _coerce_record(record: Any) -> DurableAgentExecutionRecord:
        """Normalize a repository DTO without imposing DB event ordering.

        X2-B repositories normally return ``DurableAgentExecutionRecord``.
        This small structural adapter also accepts a JSON-shaped repository
        result, but deliberately uses ``model_construct`` for the outer record
        so a shuffled event collection reaches X2-C's explicit sequence
        validator instead of being rejected by a DTO constructor first.
        """

        if isinstance(record, DurableAgentExecutionRecord):
            return record
        if isinstance(record, Mapping):
            value = lambda name, alias=None, default=None: record.get(
                name,
                record.get(alias, default) if alias is not None else default,
            )
        else:
            value = lambda name, alias=None, default=None: getattr(
                record,
                name,
                getattr(record, alias, default) if alias is not None else default,
            )
        try:
            task_key = value("task_key", "taskKey")
            task_key = (
                task_key
                if isinstance(task_key, ExecutionTaskKey)
                else ExecutionTaskKey.model_validate(task_key)
            )
            raw_mode = value("mode", default=task_key.mode)
            mode = raw_mode if isinstance(raw_mode, AnalysisMode) else AnalysisMode(raw_mode)
            created_at = value("created_at", "createdAt")
            if not isinstance(created_at, datetime):
                created_at = datetime.fromisoformat(str(created_at))
            completed_at = value("completed_at", "completedAt")
            if completed_at is not None and not isinstance(completed_at, datetime):
                completed_at = datetime.fromisoformat(str(completed_at))
            status = value("status", default=ExecutionRecordStatus.STARTED)
            status = status if isinstance(status, ExecutionRecordStatus) else ExecutionRecordStatus(status)
            raw_events = value("events", default=())
            if not isinstance(raw_events, Sequence) or isinstance(
                raw_events, (str, bytes, bytearray)
            ):
                raise ValueError("events are not a sequence")
            return DurableAgentExecutionRecord.model_construct(
                record_schema_version=value(
                    "record_schema_version", "recordSchemaVersion"
                ),
                execution_contract_version=value(
                    "execution_contract_version", "executionContractVersion"
                ),
                execution_id=value("execution_id", "executionId"),
                task_key=task_key,
                media_identity=value("media_identity", "mediaIdentity"),
                source_revision=value("source_revision", "sourceRevision"),
                source_provenance_version=value(
                    "source_provenance_version", "sourceProvenanceVersion"
                ),
                mode=mode,
                mode_profile_version=value("mode_profile_version", "modeProfileVersion"),
                tool_policy_version=value("tool_policy_version", "toolPolicyVersion"),
                worker_attempt=value("worker_attempt", "workerAttempt"),
                request_id=value("request_id", "requestId"),
                parent_execution_id=value("parent_execution_id", "parentExecutionId"),
                status=status,
                replayable=value("replayable", default=False),
                created_at=created_at,
                completed_at=completed_at,
                events=tuple(raw_events),
            )
        except HistoricalReplayError:
            raise
        except Exception as error:
            raise ReplayIntegrityError("durable execution header is invalid") from error

    def _ordered_events(self, record: Any) -> tuple[DurableExecutionEvent, ...]:
        raw_events = getattr(record, "events", None)
        if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes, bytearray)):
            raise ReplayIntegrityError("execution events are not a sequence")
        if not raw_events or len(raw_events) > MAX_EXECUTION_EVENTS:
            raise ReplayIncompleteError("execution event history is empty or too large")
        normalized: list[DurableExecutionEvent] = []
        seen_sequences: set[int] = set()
        seen_logical_ids: set[str] = set()
        execution_id = getattr(record, "execution_id", None)
        for raw_event in raw_events:
            try:
                event = (
                    raw_event
                    if isinstance(raw_event, DurableExecutionEvent)
                    else DurableExecutionEvent.model_validate(raw_event)
                )
                event_type = ExecutionEventType(event.event_type)
            except ValueError as error:
                raise ReplayUnknownEventError("historical event type is unknown") from error
            except Exception as error:
                raise ReplayIntegrityError("historical event is invalid") from error
            if not isinstance(event.execution_id, str) or event.execution_id != execution_id:
                raise ReplayIntegrityError("event belongs to another execution")
            if (
                isinstance(event.sequence_no, bool)
                or not isinstance(event.sequence_no, int)
                or event.sequence_no < 1
            ):
                raise ReplayIntegrityError("historical event sequence is invalid")
            if (
                not isinstance(event.logical_event_id, str)
                or not event.logical_event_id.strip()
                or len(event.logical_event_id) > 192
            ):
                raise ReplayIntegrityError("historical logical event id is invalid")
            if event.sequence_no in seen_sequences:
                raise ReplayIntegrityError("historical event sequence is duplicated")
            if event.logical_event_id in seen_logical_ids:
                raise ReplayIntegrityError("historical logical event is duplicated")
            self._validate_event_stage(event_type, event.stage, event.agent_round)
            _assert_safe_payload(event.payload)
            encoded = _canonical_json(event.payload)
            if len(encoded.encode("utf-8")) > MAX_EXECUTION_EVENT_PAYLOAD_BYTES:
                raise ReplayIntegrityError("historical event payload exceeds its bound")
            seen_sequences.add(event.sequence_no)
            seen_logical_ids.add(event.logical_event_id)
            normalized.append(event)
        normalized.sort(key=lambda item: item.sequence_no)
        expected = tuple(range(1, len(normalized) + 1))
        if tuple(event.sequence_no for event in normalized) != expected:
            raise ReplayIntegrityError("historical event sequence is not contiguous")
        return tuple(normalized)

    @staticmethod
    def _validate_event_stage(
        event_type: ExecutionEventType,
        stage: str,
        agent_round: int,
    ) -> None:
        if not isinstance(stage, str) or not stage.strip():
            raise ReplayIntegrityError("historical event stage is blank")
        if isinstance(agent_round, bool) or not isinstance(agent_round, int) or agent_round < 0:
            raise ReplayIntegrityError("historical event round is invalid")
        expected_stage = {
            ExecutionEventType.EXECUTION_STARTED: "AGENT",
            ExecutionEventType.MODEL_ROUTE_RECORDED: "ROUTING",
            ExecutionEventType.PLAN_RECORDED: "PLANNER",
            ExecutionEventType.PLAN_REPAIRED: "PLANNER",
            ExecutionEventType.PLAN_REPLANNED: "PLANNER",
            ExecutionEventType.RETRIEVAL_SELECTED: "RETRIEVAL",
            ExecutionEventType.EXECUTOR_TURN_RECORDED: "EXECUTOR",
            ExecutionEventType.TOOL_CALL_REFERENCED: "TOOL",
            ExecutionEventType.CRITIC_RECORDED: "CRITIC",
            ExecutionEventType.EVIDENCE_VERIFICATION_RECORDED: "EVIDENCE_GUARD",
            ExecutionEventType.EXECUTION_COMPLETED: "AGENT",
            ExecutionEventType.EXECUTION_FAILED: "AGENT",
        }[event_type]
        if stage.strip().upper() != expected_stage:
            raise ReplayIntegrityError("historical event stage is inconsistent")

    @staticmethod
    def _validate_terminal_history(
        record: Any,
        events: tuple[DurableExecutionEvent, ...],
    ) -> ExecutionRecordStatus:
        try:
            status = ExecutionRecordStatus(record.status)
        except Exception as error:
            raise ReplayIntegrityError("execution record status is unknown") from error
        if status in {ExecutionRecordStatus.STARTED, ExecutionRecordStatus.CANCELLED}:
            raise ReplayIncompleteError("execution record is not terminal")
        if getattr(record, "completed_at", None) is None:
            raise ReplayIncompleteError("terminal execution has no completion time")
        if events[0].event_type is not ExecutionEventType.EXECUTION_STARTED:
            raise ReplayIntegrityError("execution history does not start with EXECUTION_STARTED")
        if sum(event.event_type is ExecutionEventType.EXECUTION_STARTED for event in events) != 1:
            raise ReplayIntegrityError("execution history has multiple start events")
        completed = [
            event for event in events if event.event_type is ExecutionEventType.EXECUTION_COMPLETED
        ]
        failed = [
            event for event in events if event.event_type is ExecutionEventType.EXECUTION_FAILED
        ]
        if status is ExecutionRecordStatus.COMPLETED:
            if not getattr(record, "replayable", False):
                raise ReplayIncompleteError("completed execution is not marked replayable")
            if len(completed) != 1 or completed[0] is not events[-1] or failed:
                raise ReplayIntegrityError("completed terminal event is inconsistent")
        elif status is ExecutionRecordStatus.FAILED:
            if len(failed) != 1 or failed[0] is not events[-1] or completed:
                raise ReplayIntegrityError("failed terminal event is inconsistent")
        return status

    @staticmethod
    def _event_payload(event: DurableExecutionEvent) -> dict[str, Any]:
        if not isinstance(event.payload, Mapping):
            raise ReplayIntegrityError("historical event payload is not an object")
        value = _json_value(event.payload)
        if not isinstance(value, dict):  # pragma: no cover - narrowed above
            raise ReplayIntegrityError("historical event payload is not an object")
        return value

    @staticmethod
    def _parse_started(
        payload: Mapping[str, Any],
        record: Any,
        task_key: TaskKey,
    ) -> None:
        checks = (
            ("mediaId", task_key.media_id),
            ("mode", task_key.mode.value),
            ("sourceRevision", record.source_revision),
            ("sourceProvenanceVersion", record.source_provenance_version),
            ("recordSchemaVersion", record.record_schema_version),
            ("executionContractVersion", record.execution_contract_version),
        )
        for field, expected in checks:
            if field in payload and payload[field] != expected:
                raise ReplayIntegrityError("start event identity is inconsistent")

    @staticmethod
    def _parse_model_route(payload: Mapping[str, Any]) -> ModelRouteHistory:
        if payload.get("kind") not in {
            None,
            ExecutionEventType.MODEL_ROUTE_RECORDED.value,
        }:
            raise ReplayIntegrityError("model route event kind is inconsistent")
        try:
            route_payload = {
                key: value for key, value in payload.items() if key != "kind"
            }
            route = ModelRouteHistory.model_validate(route_payload)
        except Exception as error:
            raise ReplayIntegrityError("historical model route is invalid") from error
        if route.routing_contract_version != MODEL_ROUTING_CONTRACT_VERSION:
            raise ReplayIncompatibleVersionError(
                "historical model route contract is unsupported"
            )
        return route

    @staticmethod
    def _parse_plan(payload: Mapping[str, Any]) -> AgentPlan:
        value = payload.get("plan")
        if not isinstance(value, Mapping):
            raise ReplayIntegrityError("plan event has no AgentPlan")
        try:
            return AgentPlan.model_validate(value)
        except Exception as error:
            raise ReplayIntegrityError("historical AgentPlan is invalid") from error

    @staticmethod
    def _parse_retrieval(
        payload: Mapping[str, Any],
        source_revision: str,
    ) -> HistoricalRetrievalSelection:
        selected = payload.get("selected", ())
        if not isinstance(selected, (list, tuple)):
            raise ReplayIntegrityError("retrieval selection is not a bounded list")
        if len(selected) > MAX_EXECUTION_RETRIEVAL_REFS:
            raise ReplayIntegrityError("retrieval selection exceeds its bound")
        normalized_refs: list[HistoricalSourceReference] = []
        for raw in selected:
            if not isinstance(raw, Mapping):
                raise ReplayIntegrityError("retrieval reference is not an object")
            try:
                reference = HistoricalSourceReference.model_validate(raw)
            except Exception as error:
                raise ReplayIntegrityError("retrieval reference is invalid") from error
            if reference.segment_id or reference.chunk_id or reference.source_item_ids:
                if not reference.source_revision:
                    raise ReplayArtifactUnavailableError(
                        "retrieval reference has no stable source revision"
                    )
                if reference.source_revision != source_revision:
                    raise ReplayIntegrityError("retrieval reference source revision mismatches header")
            else:
                raise ReplayArtifactUnavailableError(
                    "retrieval reference has no stable source identity"
                )
            normalized_refs.append(reference)
        selected_count = payload.get("selectedCount", len(normalized_refs))
        if selected_count != len(normalized_refs):
            raise ReplayIntegrityError("retrieval selectedCount is inconsistent")
        try:
            return HistoricalRetrievalSelection(
                purpose=payload.get("purpose", ""),
                selected=tuple(normalized_refs),
                selectedCount=selected_count,
                budgetTruncated=payload.get("budgetTruncated", False),
                queryDigest=payload.get("queryDigest"),
            )
        except Exception as error:
            if isinstance(error, HistoricalReplayError):
                raise
            raise ReplayIntegrityError("retrieval selection is invalid") from error

    @staticmethod
    def _parse_executor_turn(
        payload: Mapping[str, Any],
        agent_round: int,
        source_revision: str,
    ) -> HistoricalExecutorTurn:
        try:
            kind = ExecutorTurnKind(payload.get("kind"))
        except Exception as error:
            raise ReplayIntegrityError("executor turn kind is unknown") from error
        if kind is ExecutorTurnKind.FINAL:
            if any(
                payload.get(field) is not None
                for field in ("requestIndex", "callId", "toolName", "argsDigest")
            ):
                raise ReplayIntegrityError("FINAL executor turn contains tool metadata")
            result_payload = payload.get("result")
            if not isinstance(result_payload, Mapping):
                raise ReplayIntegrityError("FINAL executor turn has no AnalysisResult")
            try:
                result = AnalysisResult.model_validate(result_payload)
            except Exception as error:
                raise ReplayIntegrityError("historical AnalysisResult is invalid") from error
            HistoricalAgentReplayService._validate_result_provenance(result, source_revision)
            try:
                return HistoricalExecutorTurn(
                    kind=kind,
                    agentRound=agent_round,
                    finalResult=result,
                )
            except Exception as error:
                raise ReplayIntegrityError("FINAL executor turn is invalid") from error
        request_index = _positive_int(payload.get("requestIndex"), "executor requestIndex")
        call_id = _bounded_text(payload.get("callId"), "executor callId")
        tool_name = _bounded_text(payload.get("toolName"), "executor toolName")
        try:
            ToolName(tool_name)
        except Exception as error:
            raise ReplayIntegrityError("executor tool is outside the static allowlist") from error
        args_digest = _bounded_digest(payload.get("argsDigest"), "executor argsDigest")
        try:
            return HistoricalExecutorTurn(
                kind=kind,
                agentRound=agent_round,
                requestIndex=request_index,
                callId=call_id,
                toolName=tool_name,
                argsDigest=args_digest,
            )
        except Exception as error:
            raise ReplayIntegrityError("TOOL_REQUEST executor turn is invalid") from error

    @staticmethod
    def _parse_tool_reference(
        payload: Mapping[str, Any],
        agent_round: int,
    ) -> HistoricalReplayToolCall:
        request_index = _positive_int(payload.get("requestIndex"), "tool requestIndex")
        call_id = _bounded_text(payload.get("callId"), "tool callId")
        tool_name = _bounded_text(payload.get("toolName"), "tool toolName")
        try:
            ToolName(tool_name)
            policy_decision = (
                None
                if payload.get("policyDecision") is None
                else PolicyDecision(payload.get("policyDecision"))
            )
            reason_code = (
                None
                if payload.get("reasonCode") is None
                else ToolPolicyReasonCode(payload.get("reasonCode"))
            )
            result_status = ToolResultStatus(payload.get("resultStatus"))
        except Exception as error:
            raise ReplayIntegrityError("tool reference enum or allowlist value is invalid") from error
        ledger_reference = _bounded_text(
            payload.get("ledgerReference", f"tool-call:{call_id}"),
            "tool ledgerReference",
        )
        try:
            return HistoricalReplayToolCall(
                agentRound=agent_round,
                requestIndex=request_index,
                callId=call_id,
                toolName=tool_name,
                argsDigest=_bounded_digest(payload.get("argsDigest"), "tool argsDigest"),
                policyDecision=policy_decision,
                reasonCode=reason_code,
                resultStatus=result_status,
                ledgerReference=ledger_reference,
            )
        except Exception as error:
            raise ReplayIntegrityError("tool reference is invalid") from error

    @staticmethod
    def _parse_critic(payload: Mapping[str, Any]) -> CriticResult:
        value = payload.get("critic")
        if not isinstance(value, Mapping):
            raise ReplayIntegrityError("critic event has no CriticResult")
        try:
            return CriticResult.model_validate(value)
        except Exception as error:
            raise ReplayIntegrityError("historical CriticResult is invalid") from error

    @staticmethod
    def _parse_evidence(
        payload: Mapping[str, Any],
        source_revision: str,
    ) -> HistoricalEvidenceVerification:
        if payload.get("sourceRevision") != source_revision:
            raise ReplayIntegrityError("evidence event source revision mismatches header")
        try:
            result = HistoricalEvidenceVerification.model_validate(payload)
        except Exception as error:
            raise ReplayIntegrityError("historical Evidence Guard outcome is invalid") from error
        for reference in result.evidence_references:
            if reference.source_revision != source_revision:
                raise ReplayIntegrityError("evidence reference source revision mismatches header")
        return result

    @staticmethod
    def _parse_completion(
        payload: Mapping[str, Any],
        source_revision: str,
    ) -> tuple[AnalysisResult | None, CriticResult | None, Mapping[str, Any] | None]:
        final_result_payload = payload.get("finalResult")
        final_result: AnalysisResult | None
        if final_result_payload is None:
            final_result = None
        elif isinstance(final_result_payload, Mapping):
            try:
                final_result = AnalysisResult.model_validate(final_result_payload)
            except Exception as error:
                raise ReplayIntegrityError("completion AnalysisResult is invalid") from error
            HistoricalAgentReplayService._validate_result_provenance(final_result, source_revision)
        else:
            raise ReplayIntegrityError("completion finalResult is malformed")

        final_critic_payload = payload.get("finalCritic")
        final_critic: CriticResult | None
        if final_critic_payload is None:
            final_critic = None
        elif isinstance(final_critic_payload, Mapping):
            try:
                final_critic = CriticResult.model_validate(final_critic_payload)
            except Exception as error:
                raise ReplayIntegrityError("completion CriticResult is invalid") from error
        else:
            raise ReplayIntegrityError("completion finalCritic is malformed")

        final_evidence = payload.get("finalEvidenceVerification")
        if final_evidence is not None and not isinstance(final_evidence, Mapping):
            raise ReplayIntegrityError("completion Evidence Guard projection is malformed")
        return final_result, final_critic, final_evidence

    @staticmethod
    def _parse_failure(payload: Mapping[str, Any]) -> HistoricalReplayFailure:
        try:
            return HistoricalReplayFailure(
                classification=payload.get("classification", "EXECUTION_FAILED"),
                errorType=payload.get("errorType", ""),
                retryable=payload.get("retryable", False),
            )
        except Exception as error:
            raise ReplayIntegrityError("failure event is invalid") from error

    async def _resolve_tool_references(
        self,
        record: Any,
        task_key: TaskKey,
        references: list[HistoricalReplayToolCall],
        requests: Mapping[int, HistoricalExecutorTurn],
        request_sequences: Mapping[int, int],
        reference_sequences: Mapping[int, int],
    ) -> list[HistoricalReplayToolCall]:
        if len(references) > MAX_REPLAY_TOOL_CALLS:
            raise ReplayIntegrityError("historical tool call count exceeds its bound")
        if not references:
            return []
        if self._tool_checkpoint is None:
            raise ReplayArtifactUnavailableError("historical tool ledger is unavailable")
        loader = getattr(self._tool_checkpoint, "load_tool_state", None)
        if not callable(loader):
            raise ReplayArtifactUnavailableError("historical tool ledger has no read operation")
        try:
            ledger = loader(task_key)
            if inspect.isawaitable(ledger):
                ledger = await ledger
        except Exception as error:
            raise ReplayArtifactUnavailableError("historical tool ledger read failed") from error
        if ledger is None:
            raise ReplayArtifactUnavailableError("historical tool ledger is missing")
        try:
            ledger = (
                ledger
                if isinstance(ledger, DurableToolStateLedger)
                else DurableToolStateLedger.model_validate(ledger)
            )
        except Exception as error:
            raise ReplayArtifactUnavailableError("historical tool ledger is invalid") from error
        if ledger.task_key != task_key:
            raise ReplayIntegrityError("historical tool ledger task identity mismatches record")
        if len(ledger.records) > MAX_REPLAY_TOOL_CALLS:
            raise ReplayArtifactUnavailableError("historical tool ledger exceeds its bound")

        resolved: list[HistoricalReplayToolCall] = []
        for reference in references:
            request = requests.get(reference.request_index)
            if request is None:
                raise ReplayIntegrityError("tool reference has no recorded executor request")
            if reference_sequences[reference.request_index] <= request_sequences[reference.request_index]:
                raise ReplayIntegrityError("tool reference precedes its executor request")
            if (
                request.call_id != reference.call_id
                or request.tool_name != reference.tool_name
                or request.agent_round != reference.agent_round
                or (
                    reference.args_digest != "unvalidated"
                    and request.args_digest != reference.args_digest
                )
            ):
                raise ReplayIntegrityError("tool request and reference identity mismatch")
            try:
                state = ledger.record(reference.request_index)
            except KeyError as error:
                raise ReplayArtifactUnavailableError("referenced tool ledger state is missing") from error
            resolved.append(self._join_tool_state(reference, state, task_key))
        return resolved

    @staticmethod
    def _join_tool_state(
        reference: HistoricalReplayToolCall,
        state: Any,
        task_key: TaskKey,
    ) -> HistoricalReplayToolCall:
        if not hasattr(state, "execution_state"):
            raise ReplayArtifactUnavailableError("tool ledger state is malformed")
        if state.execution_state not in {
            DurableToolExecutionState.RESULT_STORED,
            DurableToolExecutionState.CONSUMED,
        }:
            raise ReplayArtifactUnavailableError("referenced tool result is not durably stored")
        if (
            getattr(state, "task_key", None) != task_key
            or state.agent_round != reference.agent_round
            or state.call_id != reference.call_id
            or state.request_index != reference.request_index
            or state.tool_name != reference.tool_name
        ):
            raise ReplayIntegrityError("tool ledger identity mismatches event reference")
        result = state.tool_result
        if not isinstance(result, ToolResult):
            raise ReplayArtifactUnavailableError("referenced durable ToolResult is missing")
        if result.call_id != reference.call_id or result.tool_name != reference.tool_name:
            raise ReplayIntegrityError("durable ToolResult identity mismatches event reference")
        if result.status is not reference.result_status:
            raise ReplayIntegrityError("durable ToolResult status mismatches event reference")
        canonical_digest = getattr(state, "canonical_args_digest", None)
        if reference.args_digest == "unvalidated":
            if canonical_digest is not None:
                raise ReplayIntegrityError("unvalidated tool reference has a durable digest")
        elif canonical_digest != reference.args_digest:
            raise ReplayIntegrityError("tool argument digest mismatches durable ledger")
        state_decision = getattr(state, "policy_decision", None)
        state_reason = getattr(state, "policy_reason", None)
        if state_decision is not reference.policy_decision:
            raise ReplayIntegrityError("tool policy decision mismatches durable ledger")
        if state_reason is not reference.reason_code:
            raise ReplayIntegrityError("tool policy reason mismatches durable ledger")
        if reference.ledger_reference != f"tool-call:{reference.call_id}":
            raise ReplayIntegrityError("tool ledger reference is not canonical")
        return reference.model_copy(update={"tool_result": result})

    @staticmethod
    def _validate_result_provenance(result: AnalysisResult, source_revision: str) -> None:
        for evidence in result.evidence:
            if evidence.source_revision != source_revision:
                raise ReplayIntegrityError("historical result evidence source revision mismatches header")
            if evidence.source_provenance_version not in {"", PROVENANCE_VERSION}:
                raise ReplayIncompatibleVersionError("historical evidence provenance version is unsupported")

    @staticmethod
    def _validate_cross_event_history(
        *,
        record: Any,
        status: ExecutionRecordStatus,
        plans: list[AgentPlan],
        retrievals: list[HistoricalRetrievalSelection],
        executor_turns: list[HistoricalExecutorTurn],
        tool_calls: list[HistoricalReplayToolCall],
        tool_requests: Mapping[int, HistoricalExecutorTurn],
        critics: list[CriticResult],
        evidence_outcomes: list[HistoricalEvidenceVerification],
        historical_model_route: ModelRouteHistory | None,
        route_event_sequence: int | None,
        first_plan_sequence: int | None,
        final_result: AnalysisResult | None,
        completion_final_result: AnalysisResult | None,
        completion_final_critic: CriticResult | None,
        completion_final_evidence: Mapping[str, Any] | None,
        failure: HistoricalReplayFailure | None,
    ) -> None:
        if len(tool_calls) != len(tool_requests):
            raise ReplayIncompleteError("historical tool request has no durable result reference")

        if record.execution_contract_version == EXECUTION_CONTRACT_VERSION_V1:
            if historical_model_route is not None:
                raise ReplayIntegrityError(
                    "v1 execution history contains a model route event"
                )
        elif record.execution_contract_version == EXECUTION_CONTRACT_VERSION_V2:
            if historical_model_route is None or route_event_sequence is None:
                raise ReplayIncompleteError(
                    "v2 execution history has no MODEL_ROUTE_RECORDED event"
                )
            if route_event_sequence <= 1:
                raise ReplayIntegrityError(
                    "MODEL_ROUTE_RECORDED must follow EXECUTION_STARTED"
                )
            if (
                first_plan_sequence is not None
                and route_event_sequence >= first_plan_sequence
            ):
                raise ReplayIntegrityError(
                    "MODEL_ROUTE_RECORDED must precede the first plan event"
                )
            expected_profile_ids = {
                "FAST": "fast-profile",
                "BALANCED": "balanced-profile",
                "DEEP": "deep-profile",
            }
            if historical_model_route.profile_id != expected_profile_ids.get(
                historical_model_route.lane.value
            ):
                raise ReplayIntegrityError(
                    "historical model route profile does not match its lane"
                )
        else:  # pragma: no cover - header validation normally catches this
            raise ReplayIncompatibleVersionError(
                "execution contract version is unsupported"
            )

        if status is ExecutionRecordStatus.FAILED:
            if failure is None:
                raise ReplayIntegrityError("failed record has no failure projection")
            return

        if not plans:
            raise ReplayIncompleteError("completed record has no historical plan")
        if not plans[-1].is_execution_valid():
            raise ReplayIntegrityError("completed record has no executable historical plan")
        if not retrievals:
            raise ReplayIncompleteError("completed record has no historical retrieval selection")
        if not executor_turns or final_result is None:
            raise ReplayIncompleteError("completed record has no historical final Executor turn")
        if not critics:
            raise ReplayIncompleteError("completed record has no historical Critic outcome")
        if not evidence_outcomes:
            raise ReplayIncompleteError("completed record has no historical Evidence Guard outcome")
        if completion_final_result is None or completion_final_result != final_result:
            raise ReplayIntegrityError("completion result does not match historical Executor result")
        if completion_final_critic is None or completion_final_critic != critics[-1]:
            raise ReplayIntegrityError("completion Critic does not match historical Critic")
        if completion_final_evidence is None:
            raise ReplayIncompleteError("completion has no Evidence Guard projection")
        if completion_final_evidence.get("passed") != evidence_outcomes[-1].passed:
            raise ReplayIntegrityError("completion Evidence Guard verdict is inconsistent")
        required_timestamps = completion_final_evidence.get("requiredTimestamps", ())
        if tuple(required_timestamps or ()) != evidence_outcomes[-1].required_timestamps:
            raise ReplayIntegrityError("completion Evidence Guard timestamps are inconsistent")


def _coerce_task_key(value: TaskKey | ExecutionTaskKey) -> TaskKey:
    if isinstance(value, TaskKey):
        return value
    if isinstance(value, ExecutionTaskKey):
        return value.to_task_key()
    try:
        return ExecutionTaskKey.model_validate(value).to_task_key()
    except Exception as error:
        raise ReplayIntegrityError("trusted task identity is invalid") from error


# Public aliases make the application boundary discoverable without creating
# a second implementation or a second persistence abstraction.
HistoricalReplayService = HistoricalAgentReplayService
DeterministicHistoricalReplayService = HistoricalAgentReplayService
ReplayIncompatibleError = ReplayIncompatibleVersionError
ReplayUnavailableLegacyError = ReplayLegacyUnavailableError


__all__ = [
    "DeterministicHistoricalReplayService",
    "HistoricalAgentReplayService",
    "HistoricalEvidenceReference",
    "HistoricalEvidenceVerification",
    "HistoricalExecutorTurn",
    "HistoricalReplayError",
    "HistoricalReplayEventView",
    "HistoricalReplayFailure",
    "HistoricalReplayResult",
    "HistoricalReplayService",
    "HistoricalReplayState",
    "HistoricalReplayToolCall",
    "HistoricalRetrievalSelection",
    "HistoricalSourceReference",
    "ReplayArtifactUnavailableError",
    "ReplayIncompleteError",
    "ReplayIncompatibleVersionError",
    "ReplayIncompatibleError",
    "ReplayIntegrityError",
    "ReplayLegacyUnavailableError",
    "ReplayNotFoundError",
    "ReplayPersistenceError",
    "ReplayUnknownEventError",
    "ReplayUnavailableLegacyError",
]
