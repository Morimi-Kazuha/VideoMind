"""Controlled AgentLoop state and its Planner/Critic contracts."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator

from ._base import DomainModel, normalize_nullable_aliases, tuple_or_empty
from .analysis import AnalysisResult
from .budget import AgentBudgetConfig, BudgetUsage


MAX_PLAN_TASKS = 5
MAX_PLAN_TASK_LENGTH = 500


def java_string_length(value: str) -> int:
    """Count Java ``String.length()`` UTF-16 code units.

    Python counts Unicode scalar values, whereas the Java baseline counts a
    supplementary character as two ``char`` values.  ``surrogatepass`` also
    preserves the one-code-unit behavior of an isolated surrogate should a
    checkpoint contain one.
    """

    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


class AgentPlan(DomainModel):
    """Planner output.

    Java's DTO accepts any parsed list and the service later enforces the
    executable bound of one-to-five non-blank tasks (maximum 500 characters
    each).  Keeping parsing permissive is important for checkpoint repair and
    preserves that two-step Java behavior; :meth:`is_execution_valid` exposes
    the service rule to Python orchestration code.
    """

    understood_goal: str = Field(default="", alias="understoodGoal")
    tasks: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize_nullable(cls, data: Any) -> Any:
        return normalize_nullable_aliases(
            data,
            {"understood_goal": "", "understoodGoal": "", "tasks": ()},
            ("understood_goal", "understoodGoal"),
        )

    @field_validator("understood_goal")
    @classmethod
    def _trim_goal(cls, value: str) -> str:
        return value.strip()

    @field_validator("tasks", mode="before")
    @classmethod
    def _copy_tasks(cls, value: Any) -> tuple[Any, ...]:
        return tuple_or_empty(value)

    def is_execution_valid(self) -> bool:
        """Return the AgentLoopService's 1–5 task structural validity rule."""

        return bool(
            self.understood_goal.strip()
            and 1 <= len(self.tasks) <= MAX_PLAN_TASKS
            and all(
                task.strip()
                and java_string_length(task) <= MAX_PLAN_TASK_LENGTH
                for task in self.tasks
            )
        )

    @property
    def execution_violations(self) -> tuple[str, ...]:
        """Explain structural violations without mutating the parsed plan."""

        violations: list[str] = []
        if not self.understood_goal.strip():
            violations.append("understoodGoal is required")
        if not self.tasks:
            violations.append("at least one task is required")
        if len(self.tasks) > MAX_PLAN_TASKS:
            violations.append("at most five tasks are allowed")
        if any(not task.strip() for task in self.tasks):
            violations.append("tasks must not be blank")
        if any(java_string_length(task) > MAX_PLAN_TASK_LENGTH for task in self.tasks):
            violations.append("tasks must be at most 500 characters")
        return tuple(violations)


class CriticResult(DomainModel):
    """Critic verdict and targeted repair hints."""

    passed: bool = False
    feedback: tuple[str, ...] = ()
    missing_requirements: Annotated[tuple[str, ...], Field(alias="missingRequirements")] = ()
    unsupported_claims: Annotated[tuple[str, ...], Field(alias="unsupportedClaims")] = ()
    required_timestamps: Annotated[tuple[int, ...], Field(alias="requiredTimestamps")] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize_nullable(cls, data: Any) -> Any:
        return normalize_nullable_aliases(
            data,
            {
                "passed": False,
                "feedback": (),
                "missing_requirements": (),
                "missingRequirements": (),
                "unsupported_claims": (),
                "unsupportedClaims": (),
                "required_timestamps": (),
                "requiredTimestamps": (),
            },
            ("missing_requirements", "missingRequirements"),
            ("unsupported_claims", "unsupportedClaims"),
            ("required_timestamps", "requiredTimestamps"),
        )

    @field_validator(
        "feedback",
        "missing_requirements",
        "unsupported_claims",
        "required_timestamps",
        mode="before",
    )
    @classmethod
    def _copy_collections(cls, value: Any) -> tuple[Any, ...]:
        return tuple_or_empty(value)


class AgentState(DomainModel):
    """Explicit AgentLoop state persisted between planner/executor/critic steps."""

    goal: str
    plan: AgentPlan | None = None
    result: AnalysisResult | None = None
    critique: CriticResult | None = None
    round: int = 0

    @field_validator("goal")
    @classmethod
    def _require_goal(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("agent goal is required")
        return value.strip()

    @model_validator(mode="before")
    @classmethod
    def _normalize_primitive_defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        if normalized.get("round") is None:
            normalized["round"] = 0
        return normalized

    @field_validator("round")
    @classmethod
    def _nonnegative_round(cls, value: int) -> int:
        if value < 0:
            raise ValueError("agent round cannot be negative")
        return value


# Java's three records are nested under AgentState.  These aliases preserve
# the original import/attribute shape without forcing Python users into it.
AgentState.AgentPlan = AgentPlan  # type: ignore[attr-defined]
AgentState.CriticResult = CriticResult  # type: ignore[attr-defined]


__all__ = [
    "AgentPlan",
    "AgentBudgetConfig",
    "AgentState",
    "BudgetUsage",
    "CriticResult",
    "MAX_PLAN_TASK_LENGTH",
    "MAX_PLAN_TASKS",
    "java_string_length",
]
