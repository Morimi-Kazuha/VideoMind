"""Durable, bounded state for the X1-D1 read-only tool loop.

This module deliberately models operational recovery rather than deterministic
Agent replay.  A ledger records the application-owned logical tool calls and
their monotonic facts; it never stores provider prompts, provider responses,
credentials, or infrastructure objects.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Mapping

from pydantic import StrictBool, StrictInt, StrictStr, field_validator, model_validator

from .tool_contracts import (
    ModelToolRequest,
    PolicyDecision,
    StrictToolModel,
    ToolArguments,
    ToolName,
    ToolPolicyReasonCode,
    ToolResult,
    ToolResultStatus,
    argument_model_for_tool,
)
from .value_objects import TaskKey


MAX_DURABLE_TOOL_REQUEST_BYTES = 16 * 1024
MAX_TOOL_DIGEST_LENGTH = 64


class DurableToolExecutionState(str, Enum):
    """Monotonic facts needed to distinguish the X1-D1 crash windows."""

    REQUESTED = "REQUESTED"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    DENIED = "DENIED"
    RESULT_STORED = "RESULT_STORED"
    CONSUMED = "CONSUMED"


# Descriptive aliases are useful to callers that use ``ToolCallState`` as the
# domain spelling.  They intentionally point to the same enum.
ToolCallState = DurableToolExecutionState
ToolExecutionState = DurableToolExecutionState


class ToolRecoveryError(RuntimeError):
    """Base error for an invalid or unsafe recovered tool record."""


class ToolRecoveryIdentityMismatchError(ToolRecoveryError):
    """A durable tool record does not belong to the current trusted task."""


ToolIdentityMismatchError = ToolRecoveryIdentityMismatchError


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError("tool arguments must be JSON-safe")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_tool_arguments_digest(arguments: ToolArguments | Mapping[str, Any]) -> str:
    """Hash validated normalized arguments using canonical JSON semantics."""

    return hashlib.sha256(_canonical_json(arguments).encode("utf-8")).hexdigest()


# Shorter migration spelling used by a few application adapters.
canonical_arguments_digest = canonical_tool_arguments_digest


def _request_size(request: ModelToolRequest) -> int:
    encoded = _canonical_json(request)
    size = len(encoded.encode("utf-8"))
    if size > MAX_DURABLE_TOOL_REQUEST_BYTES:
        raise ValueError("durable tool request exceeds the bounded size")
    return size


class DurableToolCallState(StrictToolModel):
    """One application-owned logical tool request and its durable facts.

    ``request`` is retained only as a bounded, identity-free recovery snapshot
    so a crash between REQUESTED and policy evaluation does not require a new
    Executor request.  Once policy allows the request, the validated argument
    model and its canonical digest become authoritative.
    """

    task_key: TaskKey
    agent_round: StrictInt
    request_index: StrictInt
    call_id: StrictStr
    tool_name: StrictStr
    request: ModelToolRequest | None = None
    validated_arguments: ToolArguments | None = None
    canonical_args_digest: StrictStr | None = None
    policy_decision: PolicyDecision | None = None
    policy_reason: ToolPolicyReasonCode | None = None
    execution_state: DurableToolExecutionState
    tool_result: ToolResult | None = None
    result_consumed: StrictBool = False

    @property
    def state(self) -> DurableToolExecutionState:
        return self.execution_state

    @property
    def args_digest(self) -> str | None:
        return self.canonical_args_digest

    @property
    def policy_reason_code(self) -> ToolPolicyReasonCode | None:
        return self.policy_reason

    @field_validator("agent_round", "request_index")
    @classmethod
    def _positive_indices(cls, value: int) -> int:
        if isinstance(value, bool) or value < 1:
            raise ValueError("durable tool indices must be positive integers")
        return value

    @field_validator("call_id", "tool_name")
    @classmethod
    def _bounded_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("durable tool identity text must not be blank")
        if len(normalized) > 128:
            raise ValueError("durable tool identity text is too long")
        return normalized

    @field_validator("request")
    @classmethod
    def _bound_request(cls, value: ModelToolRequest | None) -> ModelToolRequest | None:
        if value is not None:
            _request_size(value)
        return value

    @field_validator("canonical_args_digest")
    @classmethod
    def _validate_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if len(normalized) != MAX_TOOL_DIGEST_LENGTH:
            raise ValueError("canonical tool argument digest must be SHA-256")
        if any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("canonical tool argument digest must be hexadecimal")
        return normalized

    @model_validator(mode="after")
    def _validate_state_shape(self) -> "DurableToolCallState":
        if self.request is not None and self.request.tool_name != self.tool_name:
            raise ValueError("durable request and tool_name do not match")

        state = self.execution_state
        if state is DurableToolExecutionState.REQUESTED:
            if self.request is None:
                raise ValueError("REQUESTED tool state requires its request snapshot")
            if self.policy_decision is not None or self.tool_result is not None:
                raise ValueError("REQUESTED tool state cannot carry a decision/result")
            if self.result_consumed:
                raise ValueError("REQUESTED tool state cannot be consumed")
        elif state in {
            DurableToolExecutionState.AUTHORIZED,
            DurableToolExecutionState.EXECUTING,
        }:
            if self.validated_arguments is None or self.canonical_args_digest is None:
                raise ValueError("authorized tool state requires validated arguments")
            if self.policy_decision is not PolicyDecision.ALLOW:
                raise ValueError("authorized tool state requires ALLOW")
            if self.policy_reason is not None or self.tool_result is not None:
                raise ValueError("authorized tool state cannot carry denial/result data")
            self._validate_argument_digest()
            if self.result_consumed:
                raise ValueError("executing tool state cannot be consumed")
        elif state is DurableToolExecutionState.DENIED:
            if self.request is None:
                raise ValueError("DENIED tool state requires its request snapshot")
            if self.policy_decision is not PolicyDecision.DENY:
                raise ValueError("DENIED tool state requires DENY")
            if self.policy_reason is None:
                raise ValueError("DENIED tool state requires a policy reason")
            if self.tool_result is not None or self.result_consumed:
                raise ValueError("DENIED tool state cannot carry a result")
        elif state in {
            DurableToolExecutionState.RESULT_STORED,
            DurableToolExecutionState.CONSUMED,
        }:
            if self.tool_result is None:
                raise ValueError("result state requires a durable ToolResult")
            if state is DurableToolExecutionState.RESULT_STORED and self.result_consumed:
                raise ValueError("RESULT_STORED cannot be marked consumed")
            if state is DurableToolExecutionState.CONSUMED and not self.result_consumed:
                raise ValueError("CONSUMED must set result_consumed=true")
            self._validate_result_identity()
            if self.validated_arguments is not None:
                if self.canonical_args_digest is None:
                    raise ValueError("validated arguments require a canonical digest")
                self._validate_argument_digest()
            if self.tool_result.status is ToolResultStatus.DENIED:
                if self.policy_decision is not PolicyDecision.DENY:
                    raise ValueError("denied ToolResult requires DENY policy state")
                if self.policy_reason is not self.tool_result.reason_code:
                    raise ValueError("denied ToolResult reason does not match policy")
            elif self.policy_decision is PolicyDecision.DENY:
                raise ValueError("non-denied ToolResult cannot carry DENY policy state")
        return self

    def _validate_argument_digest(self) -> None:
        if self.validated_arguments is None or self.canonical_args_digest is None:
            return
        try:
            tool_name = ToolName(self.tool_name)
        except ValueError as error:
            raise ValueError("validated arguments require a registered tool") from error
        expected_type = argument_model_for_tool(tool_name)
        if not isinstance(self.validated_arguments, expected_type):
            raise ValueError("validated arguments do not match tool_name")
        expected_digest = canonical_tool_arguments_digest(self.validated_arguments)
        if expected_digest != self.canonical_args_digest:
            raise ValueError("canonical tool argument digest does not match arguments")

    def _validate_result_identity(self) -> None:
        assert self.tool_result is not None
        if (
            self.tool_result.call_id != self.call_id
            or self.tool_result.tool_name != self.tool_name
        ):
            raise ValueError("durable ToolResult identity does not match ToolCall")

    def transition(
        self,
        state: DurableToolExecutionState,
        **updates: Any,
    ) -> "DurableToolCallState":
        """Return a validated monotonic state replacement."""

        allowed = {
            DurableToolExecutionState.REQUESTED: {
                DurableToolExecutionState.AUTHORIZED,
                DurableToolExecutionState.DENIED,
            },
            DurableToolExecutionState.AUTHORIZED: {
                DurableToolExecutionState.EXECUTING,
            },
            DurableToolExecutionState.EXECUTING: {
                DurableToolExecutionState.RESULT_STORED,
            },
            DurableToolExecutionState.DENIED: {
                DurableToolExecutionState.RESULT_STORED,
            },
            DurableToolExecutionState.RESULT_STORED: {
                DurableToolExecutionState.CONSUMED,
            },
            DurableToolExecutionState.CONSUMED: set(),
        }
        if state is not self.execution_state and state not in allowed[self.execution_state]:
            raise ValueError(
                f"invalid durable tool transition: "
                f"{self.execution_state.value} -> {state.value}"
            )

        data = self.model_dump(mode="python")
        data.update(updates)
        data["execution_state"] = state
        if state is DurableToolExecutionState.RESULT_STORED:
            data["result_consumed"] = False
        elif state is DurableToolExecutionState.CONSUMED:
            data["result_consumed"] = True
        return type(self).model_validate(data)


class DurableToolStateLedger(StrictToolModel):
    """Per-task append-by-identity ledger with derived budget counters."""

    task_key: TaskKey
    records: tuple[DurableToolCallState, ...] = ()

    @model_validator(mode="after")
    def _validate_records(self) -> "DurableToolStateLedger":
        seen_indices: set[int] = set()
        seen_call_ids: set[str] = set()
        normalized = sorted(self.records, key=lambda item: item.request_index)
        for record in normalized:
            if record.task_key != self.task_key:
                raise ValueError("tool ledger record belongs to another task")
            if record.request_index in seen_indices:
                raise ValueError("tool ledger request_index must be unique")
            if record.call_id in seen_call_ids:
                raise ValueError("tool ledger call_id must be unique")
            seen_indices.add(record.request_index)
            seen_call_ids.add(record.call_id)
        object.__setattr__(self, "records", tuple(normalized))
        return self

    @property
    def next_request_index(self) -> int:
        return max((record.request_index for record in self.records), default=0) + 1

    @property
    def request_count(self) -> int:
        return len(self.records)

    def counts_before(self, request_index: int) -> tuple[int, int]:
        """Return (same-round count, total count) before one request."""

        record = next(
            (item for item in self.records if item.request_index == request_index),
            None,
        )
        if record is None:
            raise KeyError(request_index)
        earlier = tuple(
            item for item in self.records if item.request_index < request_index
        )
        return (
            sum(item.agent_round == record.agent_round for item in earlier),
            len(earlier),
        )

    def count_for_round(self, agent_round: int) -> int:
        return sum(item.agent_round == agent_round for item in self.records)

    def latest_for_round(self, agent_round: int) -> DurableToolCallState | None:
        candidates = [
            item for item in self.records if item.agent_round == agent_round
        ]
        return max(candidates, key=lambda item: item.request_index, default=None)

    def with_record(self, record: DurableToolCallState) -> "DurableToolStateLedger":
        if record.task_key != self.task_key:
            raise ToolRecoveryIdentityMismatchError(
                "durable tool record does not match the current task"
            )
        existing = next(
            (
                item
                for item in self.records
                if item.request_index == record.request_index
            ),
            None,
        )
        if existing is not None and record.execution_state is not existing.execution_state:
            allowed = {
                DurableToolExecutionState.REQUESTED: {
                    DurableToolExecutionState.AUTHORIZED,
                    DurableToolExecutionState.DENIED,
                },
                DurableToolExecutionState.AUTHORIZED: {
                    DurableToolExecutionState.EXECUTING,
                },
                DurableToolExecutionState.EXECUTING: {
                    DurableToolExecutionState.RESULT_STORED,
                },
                DurableToolExecutionState.DENIED: {
                    DurableToolExecutionState.RESULT_STORED,
                },
                DurableToolExecutionState.RESULT_STORED: {
                    DurableToolExecutionState.CONSUMED,
                },
                DurableToolExecutionState.CONSUMED: set(),
            }
            if record.execution_state not in allowed[existing.execution_state]:
                raise ValueError(
                    f"invalid durable tool transition: "
                    f"{existing.execution_state.value} -> "
                    f"{record.execution_state.value}"
                )
        if (
            existing is not None
            and existing.execution_state is DurableToolExecutionState.CONSUMED
            and record != existing
        ):
            raise ValueError("durable ToolResult facts are immutable after storage")
        if (
            existing is not None
            and existing.execution_state is record.execution_state
            and record != existing
        ):
            raise ValueError("durable tool facts cannot be rewritten in place")
        records = [
            item for item in self.records if item.request_index != record.request_index
        ]
        records.append(record)
        return type(self).model_validate(
            {"task_key": self.task_key, "records": records}
        )

    def record(self, request_index: int) -> DurableToolCallState:
        for item in self.records:
            if item.request_index == request_index:
                return item
        raise KeyError(request_index)


# The name used in some persistence composition roots is intentionally an
# alias, not a second DTO.
ToolCallStateLedger = DurableToolStateLedger
ToolStateLedger = DurableToolStateLedger


def empty_tool_state_ledger(task_key: TaskKey) -> DurableToolStateLedger:
    return DurableToolStateLedger(task_key=task_key, records=())


__all__ = [
    "DurableToolCallState",
    "DurableToolExecutionState",
    "DurableToolStateLedger",
    "MAX_DURABLE_TOOL_REQUEST_BYTES",
    "ToolCallState",
    "ToolCallStateLedger",
    "ToolStateLedger",
    "ToolExecutionState",
    "ToolIdentityMismatchError",
    "ToolRecoveryError",
    "ToolRecoveryIdentityMismatchError",
    "canonical_arguments_digest",
    "canonical_tool_arguments_digest",
    "empty_tool_state_ledger",
]
