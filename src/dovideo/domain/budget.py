"""Immutable budget configuration and usage contracts for the Agent.

The Java loop receives four budget values from configuration and reads a
two-field ``AgentTelemetry.BudgetUsage`` record after each stage.  These
models deliberately keep the wire shape separate from the later loop: they
validate values at the boundary, but do not decide when a run should stop.
"""

from __future__ import annotations

import math
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator

from ._base import DomainModel, reject_alias_conflicts


def _finite_nonnegative(value: Any, field_name: str) -> Any:
    """Validate one numeric budget value without silently accepting booleans."""

    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(
            f"{field_name} must be a finite non-negative number"
        ) from error
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return value


class AgentBudgetConfig(DomainModel):
    """The bounded AgentLoop configuration.

    Zero is accepted for every field because the Python boundary's frozen
    contract is non-negative and finite.  The eventual orchestrator may give
    zero a control-flow meaning (for example, no rounds); this schema does not
    implement that later loop policy.
    """

    max_rounds: Annotated[int, Field(alias="maxRounds")] = 2
    max_duration_ms: Annotated[int, Field(alias="maxDurationMs")] = 240_000
    max_estimated_tokens: Annotated[int, Field(alias="maxEstimatedTokens")] = 50_000
    max_estimated_cost: Annotated[float, Field(alias="maxEstimatedCost")] = 0.0

    @model_validator(mode="before")
    @classmethod
    def _reject_alias_conflicts(cls, data: Any) -> Any:
        return reject_alias_conflicts(
            data,
            ("max_rounds", "maxRounds"),
            ("max_duration_ms", "maxDurationMs"),
            ("max_estimated_tokens", "maxEstimatedTokens"),
            ("max_estimated_cost", "maxEstimatedCost"),
        )

    @field_validator(
        "max_rounds",
        "max_duration_ms",
        "max_estimated_tokens",
        "max_estimated_cost",
        mode="before",
    )
    @classmethod
    def _validate_budget_value(cls, value: Any, info: Any) -> Any:
        return _finite_nonnegative(value, info.field_name)


class BudgetUsage(DomainModel):
    """Cumulative estimated token and cost usage for one Agent trace."""

    estimated_tokens: Annotated[int, Field(alias="estimatedTokens")] = 0
    estimated_cost: Annotated[float, Field(alias="estimatedCost")] = 0.0

    @model_validator(mode="before")
    @classmethod
    def _reject_alias_conflicts(cls, data: Any) -> Any:
        return reject_alias_conflicts(
            data,
            ("estimated_tokens", "estimatedTokens"),
            ("estimated_cost", "estimatedCost"),
        )

    @field_validator("estimated_tokens", "estimated_cost", mode="before")
    @classmethod
    def _validate_usage_value(cls, value: Any, info: Any) -> Any:
        return _finite_nonnegative(value, info.field_name)


# Descriptive aliases make the small model convenient for adapters without
# introducing additional, subtly different schemas.
AgentExecutionBudgetConfig = AgentBudgetConfig
ExecutionBudgetConfig = AgentBudgetConfig
AgentUsage = BudgetUsage


__all__ = [
    "AgentBudgetConfig",
    "AgentExecutionBudgetConfig",
    "AgentUsage",
    "BudgetUsage",
    "ExecutionBudgetConfig",
]
