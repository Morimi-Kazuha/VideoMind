"""Small local telemetry implementations for media branch tests/composition."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from threading import Lock

from dovideo.application.value_objects import TraceContext


class NullTelemetry:
    """No-op telemetry suitable as the default for pure adapter use."""

    def increment(
        self,
        metric: str,
        amount: int = 1,
        *,
        trace: TraceContext | None = None,
    ) -> None:
        del metric, amount, trace

    def observe(
        self,
        metric: str,
        value: float,
        *,
        trace: TraceContext | None = None,
    ) -> None:
        del metric, value, trace


class InMemoryTelemetry:
    """Thread-safe counters useful for deterministic adapter tests."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._observations: Counter[str] = Counter()
        self._lock = Lock()

    def increment(
        self,
        metric: str,
        amount: int = 1,
        *,
        trace: TraceContext | None = None,
    ) -> None:
        del trace
        with self._lock:
            self._counts[metric] += amount

    def observe(
        self,
        metric: str,
        value: float,
        *,
        trace: TraceContext | None = None,
    ) -> None:
        del trace
        with self._lock:
            self._observations[metric] += value

    @property
    def counts(self) -> Mapping[str, int]:
        with self._lock:
            return dict(self._counts)

    @property
    def observations(self) -> Mapping[str, float]:
        with self._lock:
            return dict(self._observations)

    def count(self, metric: str) -> int:
        with self._lock:
            return self._counts[metric]


__all__ = ["InMemoryTelemetry", "NullTelemetry"]
