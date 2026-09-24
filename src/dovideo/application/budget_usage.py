"""Provider-neutral cumulative Agent token/cost usage tracking."""

from __future__ import annotations

from collections.abc import Mapping
from threading import RLock
from typing import Any

from dovideo.domain import BudgetUsage

from .agent_policy import validate_budget_usage
from .ports.observability import AgentBudgetUsagePort


class InMemoryAgentBudgetUsage:
    """Small thread-safe usage tracker suitable for adapters and tests.

    The tracker records provider-reported cumulative values; it deliberately
    never estimates tokens from prompts or results.
    """

    def __init__(self, usage: BudgetUsage | Mapping[str, Any] | None = None) -> None:
        self._lock = RLock()
        self._usage = validate_budget_usage(usage or BudgetUsage())

    def record(
        self,
        usage: BudgetUsage | Mapping[str, Any] | None = None,
        *,
        estimated_tokens: int | float | None = None,
        estimated_cost: float | None = None,
    ) -> BudgetUsage:
        """Replace cumulative usage after validating every supplied value."""

        if usage is not None and (
            estimated_tokens is not None or estimated_cost is not None
        ):
            raise TypeError("record accepts usage or keyword values, not both")
        with self._lock:
            if usage is None:
                current = self._usage
                usage = {
                    "estimatedTokens": (
                        current.estimated_tokens
                        if estimated_tokens is None
                        else estimated_tokens
                    ),
                    "estimatedCost": (
                        current.estimated_cost
                        if estimated_cost is None
                        else estimated_cost
                    ),
                }
            self._usage = validate_budget_usage(usage)
            return self._usage

    def add(
        self,
        estimated_tokens: int | float | BudgetUsage | Mapping[str, Any] = 0,
        estimated_cost: float = 0.0,
        *,
        usage: BudgetUsage | Mapping[str, Any] | None = None,
    ) -> BudgetUsage:
        """Add provider-reported non-negative deltas to cumulative usage."""

        if usage is not None:
            if estimated_tokens != 0 or estimated_cost != 0.0:
                raise TypeError("add accepts usage or delta values, not both")
            delta = validate_budget_usage(usage)
        elif isinstance(estimated_tokens, (BudgetUsage, Mapping)):
            if estimated_cost != 0.0:
                raise TypeError("mapping usage cannot be combined with estimated_cost")
            delta = validate_budget_usage(estimated_tokens)
        else:
            delta = validate_budget_usage(
                {
                    "estimatedTokens": estimated_tokens,
                    "estimatedCost": estimated_cost,
                }
            )
        with self._lock:
            current = self._usage
            # Re-parse the sum so an overflow to infinity cannot be retained.
            self._usage = validate_budget_usage(
                {
                    "estimatedTokens": current.estimated_tokens + delta.estimated_tokens,
                    "estimatedCost": current.estimated_cost + delta.estimated_cost,
                }
            )
            return self._usage

    def current(self) -> BudgetUsage:
        with self._lock:
            return self._usage

    def current_usage(self) -> BudgetUsage:
        return self.current()

    # Java/provider-friendly spellings.
    currentUsage = current_usage


AgentBudgetUsage = InMemoryAgentBudgetUsage
BudgetUsageTracker = InMemoryAgentBudgetUsage


__all__ = [
    "AgentBudgetUsage",
    "AgentBudgetUsagePort",
    "BudgetUsageTracker",
    "InMemoryAgentBudgetUsage",
]
