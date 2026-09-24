"""Context-local wall-clock budget for the controlled Agent loop.

The Java implementation carries a deadline through the orchestration thread.
Python async tasks need the equivalent context-local state so nested runs do
not accidentally extend an outer deadline.  The operation descriptor keeps
both convenient Java-style class calls and injectable instance clocks:

``AgentExecutionBudget.open(100)``
``AgentExecutionBudget(monotonic=fake_clock).open(100)``
"""

from __future__ import annotations

import math
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Callable

from .errors import BudgetExceededError, DeadlineExceededError


MonotonicClock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class _DeadlineFrame:
    deadline: float
    monotonic: MonotonicClock


class _BudgetScope:
    """Reset one contextvar token exactly once when a scope closes."""

    def __init__(self, state: ContextVar[_DeadlineFrame | None], token: Token[Any]) -> None:
        self._state = state
        self._token = token
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._state.reset(self._token)
        self._closed = True

    def __enter__(self) -> "_BudgetScope":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()


class _BudgetOperation:
    """Bind a budget operation to an instance clock when called on one."""

    def __init__(self, operation: str) -> None:
        self.operation = operation

    def __get__(
        self,
        instance: "AgentExecutionBudget | None",
        owner: type["AgentExecutionBudget"],
    ) -> Callable[..., Any]:
        default_clock = instance._monotonic if instance is not None else owner._default_monotonic
        if self.operation == "open":
            def open_scope(
                max_duration_ms: float,
                *,
                monotonic: MonotonicClock | None = None,
                clock: MonotonicClock | Any | None = None,
            ) -> _BudgetScope:
                candidate = monotonic if monotonic is not None else clock
                selected_clock = (
                    default_clock if candidate is None else _resolve_clock(candidate)
                )
                return owner._open(max_duration_ms, selected_clock)

            return open_scope
        if self.operation == "remaining_ms":
            return lambda: owner._remaining_ms()
        if self.operation == "remaining_seconds":
            return lambda: owner._remaining_seconds()
        if self.operation == "check":
            return lambda stage="Agent": owner._check(stage)
        raise AttributeError(self.operation)


def _resolve_clock(value: MonotonicClock | Any) -> MonotonicClock:
    if callable(value):
        return value
    candidate = getattr(value, "monotonic", None)
    if callable(candidate):
        return candidate
    raise TypeError("monotonic clock must be callable or expose monotonic()")


class AgentExecutionBudget:
    """Carry a nested monotonic deadline through async task context.

    ``remaining_ms()`` and ``remaining_seconds()`` return ``None`` when no
    scope is active.  A scope always uses the earlier of its requested
    deadline and the currently active outer deadline; ``close`` restores the
    exact prior frame, including after an exception or cancellation.
    """

    _state: ContextVar[_DeadlineFrame | None] = ContextVar(
        "dovideo_agent_execution_deadline", default=None
    )
    _default_monotonic: MonotonicClock = time.monotonic

    def __init__(
        self,
        monotonic: MonotonicClock | Any | None = None,
        *,
        clock: MonotonicClock | Any | None = None,
    ) -> None:
        if monotonic is not None and clock is not None:
            raise TypeError("provide only monotonic or clock")
        self._monotonic = _resolve_clock(monotonic or clock or time.monotonic)

    open = _BudgetOperation("open")
    remaining_ms = _BudgetOperation("remaining_ms")
    remaining_seconds = _BudgetOperation("remaining_seconds")
    check = _BudgetOperation("check")

    def scope(self, max_duration_ms: float) -> _BudgetScope:
        """Explicit instance spelling for callers that prefer ``scope``."""

        return self.open(max_duration_ms)

    @classmethod
    def _open(cls, max_duration_ms: float, monotonic: MonotonicClock) -> _BudgetScope:
        if isinstance(max_duration_ms, bool):
            raise ValueError("Agent 执行时长预算必须大于 0")
        try:
            duration = float(max_duration_ms)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("Agent 执行时长预算必须大于 0") from error
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Agent 执行时长预算必须大于 0")
        previous = cls._state.get()
        requested_deadline = monotonic() + duration / 1000.0
        if not math.isfinite(requested_deadline):
            raise ValueError("Agent 执行时长预算必须是有限数值")
        if previous is None or requested_deadline < previous.deadline:
            frame = _DeadlineFrame(requested_deadline, monotonic)
        else:
            frame = previous
        token = cls._state.set(frame)
        return _BudgetScope(cls._state, token)

    @classmethod
    def _remaining_seconds(cls) -> float | None:
        frame = cls._state.get()
        if frame is None:
            return None
        remaining = frame.deadline - frame.monotonic()
        if remaining <= 0:
            raise DeadlineExceededError("Agent 已耗尽执行时长预算")
        return remaining

    @classmethod
    def _remaining_ms(cls) -> int | None:
        remaining = cls._remaining_seconds()
        if remaining is None:
            return None
        # Match Java's ``TimeUnit.NANOSECONDS.toMillis`` floor and its
        # minimum one-millisecond timeout for a still-live deadline.
        return max(1, int(remaining * 1000.0))

    @classmethod
    def _check(cls, stage: str = "Agent") -> None:
        try:
            cls._remaining_seconds()
        except DeadlineExceededError as error:
            raise DeadlineExceededError(f"{stage} 后终止：{error}") from error


# Friendly migration spellings.
DeadlineExceededException = DeadlineExceededError
AgentExecutionBudget.DeadlineExceededError = DeadlineExceededError  # type: ignore[attr-defined]
AgentExecutionBudget.DeadlineExceededException = DeadlineExceededError  # type: ignore[attr-defined]


__all__ = [
    "AgentExecutionBudget",
    "BudgetExceededError",
    "DeadlineExceededError",
    "DeadlineExceededException",
    "MonotonicClock",
]
