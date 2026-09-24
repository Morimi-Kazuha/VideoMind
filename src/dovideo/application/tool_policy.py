"""Deterministic application policy for restricted model-issued tools."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field, StrictInt, StrictStr, field_validator

from dovideo.domain import AnalysisMode, VideoContext

from .retrieval import MAX_USER_HITS
from .tool_contracts import (
    AgentRole,
    GetContextWindowArguments,
    GetSegmentArguments,
    MAX_CONTEXT_WINDOW_MS,
    MAX_TOOL_RESULT_PAYLOAD_BYTES,
    ModelToolRequest,
    PolicyDecision,
    SearchEvidenceArguments,
    StrictToolModel,
    ToolName,
    ToolPolicyDecision,
    ToolPolicyReasonCode,
)
from .tool_registry import ToolDefinition, ToolRegistry
from .value_objects import TaskKey


DEFAULT_TOOL_CALLS_PER_ROUND = 4
DEFAULT_TOTAL_TOOL_CALLS = 12

_IDENTITY_ARGUMENTS = {
    "call_id",
    "callid",
    "media_id",
    "mediaid",
    "owner",
    "owner_id",
    "ownerid",
    "task_key",
    "taskkey",
    "user_id",
    "userid",
    "mode",
    "agent_role",
    "agentrole",
}


class ToolPolicyContext(StrictToolModel):
    """Trusted context supplied by application orchestration.

    Model payloads are never merged into this object.  ``mode`` and
    ``agent_role`` retain an invalid string such as ``AUTO`` long enough for
    ``ToolPolicy`` to return a typed denial rather than silently applying the
    existing GENERAL fallback.
    """

    task_key: TaskKey
    media_id: StrictInt
    mode: AnalysisMode | StrictStr
    agent_role: AgentRole | StrictStr = AgentRole.EXECUTOR
    current_round: StrictInt = 0
    tool_calls_this_round: StrictInt = 0
    tool_calls_total: StrictInt = 0
    per_round_limit: StrictInt = DEFAULT_TOOL_CALLS_PER_ROUND
    total_limit: StrictInt = DEFAULT_TOTAL_TOOL_CALLS
    media_duration_ms: StrictInt | None = None
    max_result_size: StrictInt = MAX_TOOL_RESULT_PAYLOAD_BYTES
    max_result_items: StrictInt = MAX_USER_HITS

    @field_validator(
        "current_round",
        "tool_calls_this_round",
        "tool_calls_total",
        "per_round_limit",
        "total_limit",
        "max_result_size",
        "max_result_items",
    )
    @classmethod
    def _nonnegative_limits(cls, value: int, info: Any) -> int:
        if value < 0:
            raise ValueError(f"{info.field_name} cannot be negative")
        return value

    @field_validator("max_result_size", "max_result_items")
    @classmethod
    def _positive_limits(cls, value: int, info: Any) -> int:
        if value < 1:
            raise ValueError(f"{info.field_name} must be positive")
        return value

    @field_validator("max_result_size")
    @classmethod
    def _bound_result_size(cls, value: int) -> int:
        if value > MAX_TOOL_RESULT_PAYLOAD_BYTES:
            raise ValueError("max_result_size exceeds the X1-A result bound")
        return value

    @field_validator("media_duration_ms")
    @classmethod
    def _nonnegative_duration(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("media_duration_ms cannot be negative")
        return value


class ToolExecutionContext(ToolPolicyContext):
    """Trusted application context handed to an already-authorized tool.

    ``ToolPolicyContext`` remains the authorization input.  This narrow
    subtype adds only the current authoritative ``VideoContext``; it does not
    accept a model-supplied identity or load another media record.  Keeping
    the subtype preserves the X1-B policy fields and makes existing doubles
    that read ``media_id``/counter attributes remain compatible.
    """

    video_context: VideoContext

    @classmethod
    def from_policy_context(
        cls,
        policy_context: ToolPolicyContext,
        video_context: VideoContext,
    ) -> "ToolExecutionContext":
        if not isinstance(policy_context, ToolPolicyContext):
            raise TypeError("policy_context must be a ToolPolicyContext")
        if not isinstance(video_context, VideoContext):
            raise TypeError("video_context must be a VideoContext")
        return cls(
            task_key=policy_context.task_key,
            media_id=policy_context.media_id,
            mode=policy_context.mode,
            agent_role=policy_context.agent_role,
            current_round=policy_context.current_round,
            tool_calls_this_round=policy_context.tool_calls_this_round,
            tool_calls_total=policy_context.tool_calls_total,
            per_round_limit=policy_context.per_round_limit,
            total_limit=policy_context.total_limit,
            media_duration_ms=policy_context.media_duration_ms,
            max_result_size=policy_context.max_result_size,
            max_result_items=policy_context.max_result_items,
            video_context=video_context,
        )

    @property
    def context(self) -> VideoContext:
        """Compatibility spelling for the authoritative current context."""

        return self.video_context


def _concrete_mode(value: AnalysisMode | str) -> AnalysisMode | None:
    if isinstance(value, AnalysisMode):
        return value
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    try:
        return AnalysisMode[normalized]
    except KeyError:
        return None


def _agent_role(value: AgentRole | str) -> AgentRole | None:
    if isinstance(value, AgentRole):
        return value
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    try:
        return AgentRole[normalized]
    except KeyError:
        return None


class ToolPolicy:
    """Pure allow/deny policy; it never executes or persists a tool call."""

    _allowed_roles = frozenset({AgentRole.EXECUTOR})

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self._registry = registry if registry is not None else ToolRegistry()

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    def evaluate(
        self,
        request: ModelToolRequest | Mapping[str, Any] | Any,
        context: ToolPolicyContext | Any,
    ) -> ToolPolicyDecision:
        """Return a deterministic decision for one untrusted request."""

        parsed_request = self._parse_request(request)
        if parsed_request is None:
            return ToolPolicyDecision.deny(ToolPolicyReasonCode.INVALID_ARGUMENTS)
        definition = self._registry.resolve(parsed_request.tool_name)
        if definition is None:
            return ToolPolicyDecision.deny(ToolPolicyReasonCode.TOOL_NOT_ALLOWED)
        if not isinstance(context, ToolPolicyContext):
            return ToolPolicyDecision.deny(ToolPolicyReasonCode.TASK_IDENTITY_MISMATCH)

        role = _agent_role(context.agent_role)
        if role not in self._allowed_roles:
            return ToolPolicyDecision.deny(ToolPolicyReasonCode.ROLE_NOT_ALLOWED)

        if not isinstance(context.task_key, TaskKey):
            return ToolPolicyDecision.deny(ToolPolicyReasonCode.TASK_IDENTITY_MISMATCH)
        if context.media_id != context.task_key.media_id:
            return ToolPolicyDecision.deny(ToolPolicyReasonCode.MEDIA_IDENTITY_MISMATCH)

        mode = _concrete_mode(context.mode)
        if mode is None or mode is not context.task_key.mode:
            return ToolPolicyDecision.deny(ToolPolicyReasonCode.MODE_NOT_ALLOWED)

        identity_reason = self._identity_argument_reason(parsed_request)
        if identity_reason is not None:
            return ToolPolicyDecision.deny(identity_reason)

        arguments = self._registry.try_validate_arguments(
            parsed_request.tool_name,
            parsed_request.arguments,
        )
        if arguments is None:
            return ToolPolicyDecision.deny(ToolPolicyReasonCode.INVALID_ARGUMENTS)

        argument_reason = self._argument_reason(arguments, context, definition)
        if argument_reason is not None:
            return ToolPolicyDecision.deny(argument_reason)

        if (
            context.tool_calls_this_round >= context.per_round_limit
            or context.tool_calls_total >= context.total_limit
        ):
            return ToolPolicyDecision.deny(ToolPolicyReasonCode.BUDGET_EXHAUSTED)

        return ToolPolicyDecision.allow()

    # Names used by later application callers; all remain pure aliases.
    authorize = evaluate
    evaluate_request = evaluate

    @staticmethod
    def _parse_request(request: Any) -> ModelToolRequest | None:
        try:
            if isinstance(request, ModelToolRequest):
                return request
            if isinstance(request, Mapping):
                return ModelToolRequest.model_validate(request)
        except (TypeError, ValueError):
            return None
        return None

    @staticmethod
    def _identity_argument_reason(
        request: ModelToolRequest,
    ) -> ToolPolicyReasonCode | None:
        for key in request.arguments:
            normalized = key.replace("-", "_").lower()
            if normalized == "media_id" or normalized == "mediaid":
                return ToolPolicyReasonCode.MEDIA_IDENTITY_MISMATCH
            if normalized == "task_key" or normalized == "taskkey":
                return ToolPolicyReasonCode.TASK_IDENTITY_MISMATCH
            if normalized == "mode":
                return ToolPolicyReasonCode.MODE_NOT_ALLOWED
            if normalized == "agent_role" or normalized == "agentrole":
                return ToolPolicyReasonCode.ROLE_NOT_ALLOWED
            if normalized in _IDENTITY_ARGUMENTS:
                return ToolPolicyReasonCode.TASK_IDENTITY_MISMATCH
        return None

    @staticmethod
    def _argument_reason(
        arguments: Any,
        context: ToolPolicyContext,
        definition: ToolDefinition,
    ) -> ToolPolicyReasonCode | None:
        if isinstance(arguments, SearchEvidenceArguments):
            registered_limit = definition.max_result_items
            if registered_limit is None:
                return ToolPolicyReasonCode.RESULT_LIMIT_INVALID
            if not 1 <= arguments.limit <= min(registered_limit, context.max_result_items):
                return ToolPolicyReasonCode.RESULT_LIMIT_INVALID
            return None

        if isinstance(arguments, GetSegmentArguments):
            if not ToolPolicy._timestamp_in_bounds(
                arguments.timestamp_ms,
                context.media_duration_ms,
            ):
                return ToolPolicyReasonCode.TIMESTAMP_OUT_OF_BOUNDS
            return None

        if isinstance(arguments, GetContextWindowArguments):
            if not ToolPolicy._timestamp_in_bounds(
                arguments.timestamp_ms,
                context.media_duration_ms,
            ):
                return ToolPolicyReasonCode.TIMESTAMP_OUT_OF_BOUNDS
            if arguments.before_ms < 0 or arguments.after_ms < 0:
                return ToolPolicyReasonCode.INVALID_ARGUMENTS
            if arguments.before_ms + arguments.after_ms > MAX_CONTEXT_WINDOW_MS:
                return ToolPolicyReasonCode.RESULT_LIMIT_INVALID
            return None

        return ToolPolicyReasonCode.INVALID_ARGUMENTS

    @staticmethod
    def _timestamp_in_bounds(timestamp_ms: int, duration_ms: int | None) -> bool:
        if timestamp_ms < 0:
            return False
        if duration_ms is not None and timestamp_ms >= duration_ms:
            return False
        return True


__all__ = [
    "DEFAULT_TOOL_CALLS_PER_ROUND",
    "DEFAULT_TOTAL_TOOL_CALLS",
    "ToolPolicy",
    "ToolExecutionContext",
    "ToolPolicyContext",
]
