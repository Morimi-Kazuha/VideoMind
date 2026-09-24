"""Strict, provider-neutral contracts for restricted model-issued tools.

X1-A deliberately keeps these contracts separate from the existing AgentLoop
DTOs.  The current Executor still returns ``AnalysisResult`` only; these
models are the static boundary that a later continuation ticket may consume.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from dovideo.domain import AnalysisResult

from .retrieval import MAX_USER_HITS


MAX_TOOL_NAME_LENGTH = 128
MAX_QUERY_LENGTH = 2_000
MAX_CONTEXT_WINDOW_MS = 60_000
MAX_TOOL_RESULT_PAYLOAD_BYTES = 64 * 1024


class ToolName(str, Enum):
    """The complete V1 model-visible tool allowlist."""

    SEARCH_EVIDENCE = "video.search_evidence"
    GET_SEGMENT = "video.get_segment"
    GET_CONTEXT_WINDOW = "video.get_context_window"


V1_TOOL_NAMES = tuple(tool.value for tool in ToolName)
V1_TOOL_ALLOWLIST = frozenset(V1_TOOL_NAMES)


class AgentRole(str, Enum):
    """Roles that may appear in trusted application policy context."""

    PLANNER = "PLANNER"
    EXECUTOR = "EXECUTOR"
    CRITIC = "CRITIC"


class ToolResultStatus(str, Enum):
    """Narrow result envelope states for future read-only tool adapters."""

    SUCCESS = "SUCCESS"
    DENIED = "DENIED"
    FAILED = "FAILED"
    TRUNCATED = "TRUNCATED"


class PolicyDecision(str, Enum):
    """Deterministic authorization outcome."""

    ALLOW = "ALLOW"
    DENY = "DENY"


class ToolPolicyReasonCode(str, Enum):
    """Bounded machine-readable reasons for an X1-A policy denial."""

    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    ROLE_NOT_ALLOWED = "ROLE_NOT_ALLOWED"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    TASK_IDENTITY_MISMATCH = "TASK_IDENTITY_MISMATCH"
    MEDIA_IDENTITY_MISMATCH = "MEDIA_IDENTITY_MISMATCH"
    TIMESTAMP_OUT_OF_BOUNDS = "TIMESTAMP_OUT_OF_BOUNDS"
    MODE_NOT_ALLOWED = "MODE_NOT_ALLOWED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    RESULT_LIMIT_INVALID = "RESULT_LIMIT_INVALID"


class ExecutorTurnKind(str, Enum):
    """Discriminator for the future Executor final/tool-request union."""

    FINAL = "FINAL"
    TOOL_REQUEST = "TOOL_REQUEST"

    @classmethod
    def _missing_(cls, value: object) -> "ExecutorTurnKind | None":
        if isinstance(value, str):
            normalized = value.strip().upper()
            for member in cls:
                if member.value == normalized:
                    return member
        return None


class StrictToolModel(BaseModel):
    """Frozen X1 contract base with fail-closed unknown-field handling."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        validate_default=True,
    )


class SearchEvidenceArguments(StrictToolModel):
    """Arguments for a bounded future evidence search wrapper."""

    query: StrictStr
    limit: StrictInt = MAX_USER_HITS

    @field_validator("query")
    @classmethod
    def _require_bounded_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must not be blank")
        if len(normalized) > MAX_QUERY_LENGTH:
            raise ValueError("query is too long")
        return normalized

    @field_validator("limit")
    @classmethod
    def _require_integer_limit(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("limit must be an integer")
        return value


class GetSegmentArguments(StrictToolModel):
    """Timestamp contract for the future segment resolver."""

    timestamp_ms: StrictInt

    @field_validator("timestamp_ms")
    @classmethod
    def _require_integer_timestamp(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("timestamp_ms must be an integer")
        return value


class GetContextWindowArguments(StrictToolModel):
    """Bounded timestamp-centered window contract for X1-C."""

    timestamp_ms: StrictInt
    before_ms: StrictInt = 0
    after_ms: StrictInt = 0

    @field_validator("timestamp_ms", "before_ms", "after_ms")
    @classmethod
    def _require_integer_duration(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("context-window values must be integers")
        return value


ToolArguments = SearchEvidenceArguments | GetSegmentArguments | GetContextWindowArguments


def argument_model_for_tool(tool_name: ToolName) -> type[StrictToolModel]:
    """Return the only argument schema associated with one registered tool."""

    return {
        ToolName.SEARCH_EVIDENCE: SearchEvidenceArguments,
        ToolName.GET_SEGMENT: GetSegmentArguments,
        ToolName.GET_CONTEXT_WINDOW: GetContextWindowArguments,
    }[tool_name]


class ModelToolRequest(StrictToolModel):
    """Untrusted model-issued request envelope.

    Identity fields are intentionally absent.  ``arguments`` remains a raw
    JSON object until the static Registry validates it against the selected
    tool schema; this lets Policy return a typed denial instead of throwing on
    an expected malformed model request.
    """

    tool_name: StrictStr
    arguments: dict[str, Any]

    @field_validator("tool_name")
    @classmethod
    def _require_tool_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("tool_name must not be blank")
        if len(normalized) > MAX_TOOL_NAME_LENGTH:
            raise ValueError("tool_name is too long")
        return normalized

    @field_validator("arguments", mode="before")
    @classmethod
    def _copy_json_object(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("arguments must be a JSON object")
        if not all(isinstance(key, str) for key in value):
            raise ValueError("argument keys must be strings")
        return dict(value)


class ToolCall(StrictToolModel):
    """Application-owned validated call identity.

    ``call_id`` is required input and is never generated while parsing model
    output.  Stable allocation and persistence are intentionally deferred to
    X1-B/X1-D.
    """

    call_id: StrictStr
    tool_name: ToolName
    validated_arguments: ToolArguments

    @field_validator("call_id")
    @classmethod
    def _require_call_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("call_id must not be blank")
        if len(normalized) > MAX_TOOL_NAME_LENGTH:
            raise ValueError("call_id is too long")
        return normalized

    @model_validator(mode="after")
    def _match_argument_schema(self) -> "ToolCall":
        expected = argument_model_for_tool(self.tool_name)
        if not isinstance(self.validated_arguments, expected):
            raise ValueError("validated_arguments do not match tool_name")
        return self


def _json_payload_size(value: Any) -> int:
    """Return the UTF-8 size of a JSON-safe payload or raise a ValueError."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("tool result payload must be JSON-safe") from error
    if len(encoded) > MAX_TOOL_RESULT_PAYLOAD_BYTES:
        raise ValueError("tool result payload exceeds the bounded size")
    return len(encoded)


class ToolResult(StrictToolModel):
    """Bounded typed envelope for a future tool result."""

    call_id: StrictStr
    tool_name: StrictStr
    status: ToolResultStatus
    payload: dict[str, Any] | list[Any] | None = None
    truncated: StrictBool = False
    reason_code: ToolPolicyReasonCode | None = None

    @field_validator("call_id")
    @classmethod
    def _require_result_call_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("call_id must not be blank")
        if len(normalized) > MAX_TOOL_NAME_LENGTH:
            raise ValueError("call_id is too long")
        return normalized

    @field_validator("tool_name")
    @classmethod
    def _require_result_tool_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("tool_name must not be blank")
        if len(normalized) > MAX_TOOL_NAME_LENGTH:
            raise ValueError("tool_name is too long")
        return normalized

    @field_validator("payload", mode="before")
    @classmethod
    def _validate_payload(cls, value: Any) -> dict[str, Any] | list[Any] | None:
        if value is None:
            return None
        if isinstance(value, dict):
            if not all(isinstance(key, str) for key in value):
                raise ValueError("tool result payload keys must be strings")
            copied: dict[str, Any] | list[Any] = dict(value)
        elif isinstance(value, list):
            copied = list(value)
        else:
            raise ValueError("tool result payload must be an object, array, or null")
        _json_payload_size(copied)
        return copied

    @model_validator(mode="after")
    def _validate_status_shape(self) -> "ToolResult":
        if self.status in {ToolResultStatus.DENIED, ToolResultStatus.FAILED}:
            if self.payload is not None or self.truncated:
                raise ValueError("denied/failed results cannot carry a payload")
        elif self.status is ToolResultStatus.TRUNCATED:
            if not self.truncated:
                raise ValueError("truncated results must set truncated=true")
        elif self.truncated:
            raise ValueError("only truncated results may set truncated=true")
        if self.status is not ToolResultStatus.DENIED and self.reason_code is not None:
            raise ValueError("reason_code is only valid for denied results")
        return self


class ToolPolicyDecision(StrictToolModel):
    """Deterministic, non-exceptional policy result."""

    decision: PolicyDecision
    reason_code: ToolPolicyReasonCode | None = None

    @model_validator(mode="after")
    def _validate_reason_shape(self) -> "ToolPolicyDecision":
        if self.decision is PolicyDecision.ALLOW and self.reason_code is not None:
            raise ValueError("allowed policy decisions cannot carry a reason")
        if self.decision is PolicyDecision.DENY and self.reason_code is None:
            raise ValueError("denied policy decisions require a reason")
        return self

    @property
    def allowed(self) -> bool:
        return self.decision is PolicyDecision.ALLOW

    @classmethod
    def allow(cls) -> "ToolPolicyDecision":
        return cls(decision=PolicyDecision.ALLOW)

    @classmethod
    def deny(cls, reason_code: ToolPolicyReasonCode) -> "ToolPolicyDecision":
        return cls(decision=PolicyDecision.DENY, reason_code=reason_code)


class ExecutorTurn(StrictToolModel):
    """Isolated future Executor final/tool-request union.

    This type is intentionally not wired into ``ExecutorPort`` in X1-A.
    """

    kind: ExecutorTurnKind
    final_result: AnalysisResult | None = None
    tool_request: ModelToolRequest | None = None

    @model_validator(mode="after")
    def _require_exactly_one_branch(self) -> "ExecutorTurn":
        if self.kind is ExecutorTurnKind.FINAL:
            if self.final_result is None or self.tool_request is not None:
                raise ValueError("FINAL turn requires only final_result")
        elif self.tool_request is None or self.final_result is not None:
            raise ValueError("TOOL_REQUEST turn requires only tool_request")
        return self


__all__ = [
    "AgentRole",
    "ExecutorTurn",
    "ExecutorTurnKind",
    "GetContextWindowArguments",
    "GetSegmentArguments",
    "MAX_CONTEXT_WINDOW_MS",
    "MAX_QUERY_LENGTH",
    "MAX_TOOL_NAME_LENGTH",
    "MAX_TOOL_RESULT_PAYLOAD_BYTES",
    "ModelToolRequest",
    "PolicyDecision",
    "SearchEvidenceArguments",
    "StrictToolModel",
    "ToolArguments",
    "ToolCall",
    "ToolName",
    "ToolPolicyDecision",
    "ToolPolicyReasonCode",
    "ToolResult",
    "ToolResultStatus",
    "V1_TOOL_ALLOWLIST",
    "V1_TOOL_NAMES",
    "argument_model_for_tool",
]
