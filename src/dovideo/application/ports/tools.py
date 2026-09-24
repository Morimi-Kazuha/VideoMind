"""Application boundary for injected, non-provider tool execution."""

from __future__ import annotations

from typing import Protocol

from ..tool_contracts import ToolCall, ToolResult
from ..tool_policy import ToolExecutionContext


class ToolExecutorPort(Protocol):
    """Execute one already-authorized ToolCall.

    X1-C supplies the read-only video implementation behind this seam;
    the call receives the remaining Agent deadline and cannot replace the
    trusted policy context.
    """

    async def execute(
        self,
        tool_call: ToolCall,
        trusted_context: ToolExecutionContext,
        remaining_deadline: float | None = None,
    ) -> ToolResult:
        ...


__all__ = ["ToolExecutionContext", "ToolExecutorPort"]
