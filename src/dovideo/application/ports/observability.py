"""Telemetry, trace, clock, and ID-generation boundaries."""

from __future__ import annotations

from datetime import datetime
from collections.abc import Mapping
from typing import Protocol

from dovideo.domain import BudgetUsage

from ..value_objects import TaskKey, TraceContext


class TelemetryPort(Protocol):
    """Low-latency metric hooks; implementations may be in-memory or remote."""

    def increment(self, metric: str, amount: int = 1, *, trace: TraceContext | None = None) -> None:
        ...

    def observe(self, metric: str, value: float, *, trace: TraceContext | None = None) -> None:
        ...


class AgentBudgetUsagePort(Protocol):
    """Read cumulative model usage without coupling the loop to telemetry."""

    def current_usage(self) -> BudgetUsage | Mapping[str, object]:
        ...


class TracePort(Protocol):
    """Lifecycle for a trace crossing async role/adapter calls."""

    async def start(self, key: TaskKey) -> TraceContext:
        ...

    async def annotate(self, trace: TraceContext, event: str) -> None:
        ...

    async def finish(self, trace: TraceContext, *, success: bool) -> None:
        ...


class ClockPort(Protocol):
    """Injectable wall/monotonic clocks for timeout and deterministic tests."""

    def now(self) -> datetime:
        ...

    def monotonic(self) -> float:
        ...


class IdPort(Protocol):
    """Injectable opaque ID generator."""

    def new_id(self) -> str:
        ...


__all__ = [
    "AgentBudgetUsagePort",
    "ClockPort",
    "IdPort",
    "TelemetryPort",
    "TracePort",
]
