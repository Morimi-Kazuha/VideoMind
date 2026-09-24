"""Static application-layer registry for the X1-A tool allowlist."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from .retrieval import MAX_USER_HITS
from .tool_contracts import (
    ModelToolRequest,
    ToolArguments,
    ToolCall,
    ToolName,
    GetContextWindowArguments,
    GetSegmentArguments,
    SearchEvidenceArguments,
)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Static metadata needed by Policy without an execution adapter."""

    name: ToolName
    argument_model: type[BaseModel]
    max_result_items: int | None = None


_V1_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        ToolName.SEARCH_EVIDENCE,
        SearchEvidenceArguments,
        max_result_items=MAX_USER_HITS,
    ),
    ToolDefinition(ToolName.GET_SEGMENT, GetSegmentArguments),
    ToolDefinition(ToolName.GET_CONTEXT_WINDOW, GetContextWindowArguments),
)
_V1_BY_NAME = {definition.name.value: definition for definition in _V1_DEFINITIONS}


class ToolRegistryPort(Protocol):
    """Minimal provider/infrastructure-free Registry boundary."""

    def resolve(self, tool_name: str | ToolName) -> ToolDefinition | None:
        ...

    def names(self) -> tuple[str, ...]:
        ...


class ToolRegistry:
    """Explicit, immutable V1 registry.

    There is intentionally no public ``register`` method.  Model payloads
    can only select one of the three definitions compiled into this module.
    """

    def names(self) -> tuple[str, ...]:
        return tuple(definition.name.value for definition in _V1_DEFINITIONS)

    @property
    def registered_names(self) -> tuple[str, ...]:
        return self.names()

    def resolve(self, tool_name: str | ToolName) -> ToolDefinition | None:
        if isinstance(tool_name, ToolName):
            key = tool_name.value
        elif isinstance(tool_name, str):
            key = tool_name
        else:
            return None
        return _V1_BY_NAME.get(key)

    # Familiar lookup spellings keep the boundary easy to use without adding
    # a second registry abstraction.
    get = resolve
    definition = resolve

    def validate_arguments(
        self,
        tool_name: str | ToolName,
        arguments: Mapping[str, Any] | BaseModel,
    ) -> ToolArguments:
        """Parse one request against its registered strict argument schema."""

        definition = self.resolve(tool_name)
        if definition is None:
            raise LookupError(f"unknown tool: {tool_name}")
        if isinstance(arguments, definition.argument_model):
            return arguments  # type: ignore[return-value]
        try:
            parsed = definition.argument_model.model_validate(arguments)
        except (TypeError, ValidationError) as error:
            raise ValueError("tool arguments do not match the registered schema") from error
        return parsed  # type: ignore[return-value]

    def try_validate_arguments(
        self,
        tool_name: str | ToolName,
        arguments: Mapping[str, Any] | BaseModel,
    ) -> ToolArguments | None:
        """Return parsed arguments or ``None`` for an expected denial path."""

        try:
            return self.validate_arguments(tool_name, arguments)
        except (LookupError, TypeError, ValueError, ValidationError):
            return None

    def create_call(
        self,
        request: ModelToolRequest,
        *,
        call_id: str,
    ) -> ToolCall:
        """Create application-owned identity only when the caller supplies it."""

        if not isinstance(request, ModelToolRequest):
            raise TypeError("request must be a ModelToolRequest")
        definition = self.resolve(request.tool_name)
        if definition is None:
            raise LookupError(f"unknown tool: {request.tool_name}")
        validated = self.validate_arguments(request.tool_name, request.arguments)
        return ToolCall(
            call_id=call_id,
            tool_name=definition.name,
            validated_arguments=validated,
        )


__all__ = ["ToolDefinition", "ToolRegistry", "ToolRegistryPort"]
