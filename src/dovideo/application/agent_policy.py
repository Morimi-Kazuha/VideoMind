"""Deterministic Phase 7A validation policy for the controlled Agent.

This module ports the *guards* around Java ``AgentLoopService`` without
starting the Planner/Executor/Critic loop.  The DTO models remain permissive
enough to represent a malformed model response for a later repair call;
callers invoke these pure policy functions before allowing a plan or result
to cross an execution boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from dovideo.domain import (
    AgentPlan,
    AnalysisResult,
    AnalysisSection,
    CriticResult,
    ModeProfile,
    MAX_PLAN_TASK_LENGTH,
    MAX_PLAN_TASKS,
    java_string_length,
)
from dovideo.domain.budget import AgentBudgetConfig, BudgetUsage


class AgentPolicyError(ValueError):
    """Base error for a rejected Agent boundary value."""


class InvalidPlanError(AgentPolicyError):
    """Raised when a plan cannot be sent to the Executor."""


class InvalidResultError(AgentPolicyError):
    """Raised when an Executor result is not structurally complete."""


class InvalidBudgetError(AgentPolicyError):
    """Raised when budget configuration or usage is unsafe."""


def _as_plan(value: AgentPlan | Mapping[str, Any] | None) -> AgentPlan | None:
    if value is None:
        return None
    if isinstance(value, AgentPlan):
        return value
    if isinstance(value, Mapping):
        try:
            return AgentPlan.model_validate(value)
        except ValidationError:
            return None
    return None


def _as_result(value: AnalysisResult | Mapping[str, Any] | None) -> AnalysisResult | None:
    if value is None:
        return None
    if isinstance(value, AnalysisResult):
        return value
    if isinstance(value, Mapping):
        try:
            return AnalysisResult.model_validate(value)
        except ValidationError:
            return None
    return None


def _required_section_keys(profile: ModeProfile | Mapping[str, Any] | None) -> tuple[Any, ...]:
    if profile is None:
        return ()
    if isinstance(profile, Mapping):
        value = profile.get("required_section_keys", profile.get("requiredSectionKeys", ()))
    else:
        value = getattr(profile, "required_section_keys", ())
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        return ()
    try:
        return tuple(value)
    except TypeError:
        return ()


def _section_values(section: AnalysisSection | Mapping[str, Any] | Any) -> tuple[Any, Any]:
    if isinstance(section, Mapping):
        return section.get("key"), section.get("items")
    return getattr(section, "key", None), getattr(section, "items", None)


def plan_violations(plan: AgentPlan | Mapping[str, Any] | None) -> tuple[str, ...]:
    """Return Java-compatible structural violations for one plan."""

    parsed = _as_plan(plan)
    if parsed is None:
        return ("plan is required",)
    return parsed.execution_violations


def is_plan_valid(plan: AgentPlan | Mapping[str, Any] | None) -> bool:
    """Mirror ``AgentLoopService.isPlanValid`` using Java UTF-16 length."""

    parsed = _as_plan(plan)
    if parsed is None or not parsed.understood_goal.strip():
        return False
    if not 1 <= len(parsed.tasks) <= MAX_PLAN_TASKS:
        return False
    return all(
        isinstance(task, str)
        and bool(task.strip())
        and java_string_length(task) <= MAX_PLAN_TASK_LENGTH
        for task in parsed.tasks
    )


def validate_plan(plan: AgentPlan | Mapping[str, Any] | None) -> AgentPlan:
    """Validate and return a plan, matching Java's fail-fast guard."""

    parsed = _as_plan(plan)
    if not is_plan_valid(parsed):
        raise InvalidPlanError("Planner 返回了无效任务列表")
    return parsed


def present_section_keys(
    result: AnalysisResult | Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Return sections with a nonblank item and key in first-seen order.

    A section with only blank items cannot satisfy a mode artifact contract.
    Keys are trimmed for comparison while required profile keys are
    deliberately kept as supplied, matching ``containsAll`` in the baseline.
    """

    parsed = _as_result(result)
    if parsed is None:
        return ()
    output: list[str] = []
    seen: set[str] = set()
    for section in parsed.sections:
        key, items = _section_values(section)
        if (
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(items, Sequence)
            or isinstance(items, (str, bytes, bytearray))
            or not any(isinstance(item, str) and item.strip() for item in items)
        ):
            continue
        normalized = key.strip()
        if normalized not in seen:
            seen.add(normalized)
            output.append(normalized)
    return tuple(output)


def missing_section_keys(
    result: AnalysisResult | Mapping[str, Any] | None,
    profile: ModeProfile | Mapping[str, Any] | None = None,
) -> tuple[Any, ...]:
    """Return profile-required keys not represented by a usable section."""

    required = _required_section_keys(profile)
    if not required:
        return ()
    present = present_section_keys(result)
    return tuple(key for key in required if key not in present)


def result_violations(
    result: AnalysisResult | Mapping[str, Any] | None,
    profile: ModeProfile | Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return deterministic structural result violations."""

    parsed = _as_result(result)
    if parsed is None:
        return ("result is required",)
    violations: list[str] = []
    if not isinstance(parsed.title, str) or not parsed.title.strip():
        violations.append("title is required")
    if not parsed.conclusions:
        violations.append("at least one conclusion is required")
    if not parsed.evidence:
        violations.append("at least one evidence item is required")
    missing = missing_section_keys(parsed, profile)
    if missing:
        violations.append(
            "missing required sections: " + ", ".join(str(key) for key in missing)
        )
    return tuple(violations)


def is_result_valid(
    result: AnalysisResult | Mapping[str, Any] | None,
    profile: ModeProfile | Mapping[str, Any] | None = None,
) -> bool:
    """Mirror ``AgentLoopService.isResultValid`` for a parsed result."""

    return not result_violations(result, profile)


def validate_result(
    result: AnalysisResult | Mapping[str, Any] | None,
    profile: ModeProfile | Mapping[str, Any] | None = None,
) -> AnalysisResult:
    """Validate and return an Executor result, matching Java's guard."""

    parsed = _as_result(result)
    if not is_result_valid(parsed, profile):
        raise InvalidResultError("Executor 未生成完整结构化结果")
    return parsed


def normalize_critique(
    critique: CriticResult | Mapping[str, Any] | None,
) -> CriticResult:
    """Normalize nullable Critic collections as Java does before a guard."""

    if critique is None:
        return CriticResult(
            passed=False,
            feedback=("Critic 未返回有效结果",),
            missing_requirements=(),
            unsupported_claims=(),
            required_timestamps=(),
        )
    if isinstance(critique, Mapping):
        critique = CriticResult.model_validate(critique)
    if not isinstance(critique, CriticResult):
        raise TypeError("critique must be a CriticResult, mapping, or None")
    return CriticResult(
        passed=critique.passed,
        feedback=tuple(critique.feedback),
        missing_requirements=tuple(critique.missing_requirements),
        unsupported_claims=tuple(critique.unsupported_claims),
        required_timestamps=tuple(critique.required_timestamps),
    )


def enforce_structure_bounds(
    result: AnalysisResult | Mapping[str, Any] | None,
    critique: CriticResult | Mapping[str, Any] | None = None,
    profile: ModeProfile | Mapping[str, Any] | None = None,
) -> CriticResult:
    """Add deterministic structure feedback while preserving Critic details."""

    normalized = normalize_critique(critique)
    feedback = list(normalized.feedback)
    parsed = _as_result(result)
    if parsed is None or not isinstance(parsed.title, str) or not parsed.title.strip():
        feedback.append("补充明确的产物标题")
    if parsed is None or not parsed.conclusions:
        feedback.append("补充覆盖 Planner 任务的核心结论")
    if parsed is None or not parsed.evidence:
        feedback.append("为核心结论补充带时间戳的 ASR 或 OCR 证据")
    missing = missing_section_keys(parsed, profile)
    if missing:
        feedback.append(
            "补充当前分析模式要求的结构化段落: "
            + ", ".join(str(key) for key in missing)
        )
    if feedback == list(normalized.feedback):
        return normalized
    return CriticResult(
        passed=False,
        feedback=tuple(feedback),
        missing_requirements=normalized.missing_requirements,
        unsupported_claims=normalized.unsupported_claims,
        required_timestamps=normalized.required_timestamps,
    )


def _validate_budget_model(value: Any, model: type[Any], error_message: str) -> Any:
    try:
        return value if isinstance(value, model) else model.model_validate(value)
    except (TypeError, ValidationError) as error:
        raise InvalidBudgetError(error_message) from error


def validate_budget_config(config: AgentBudgetConfig | Mapping[str, Any]) -> AgentBudgetConfig:
    """Validate config values before an Agent run is opened."""

    return _validate_budget_model(
        config, AgentBudgetConfig, "Agent 预算配置必须是有限的非负数"
    )


def validate_runtime_budget_config(
    config: AgentBudgetConfig | Mapping[str, Any],
) -> AgentBudgetConfig:
    """Apply the loop's stricter runtime minimums after schema validation.

    Phase 7A intentionally accepts zero as a finite non-negative wire value.
    A live Agent run needs at least one round, one millisecond, and one token;
    a zero cost cap remains the Java-compatible disabled-cost sentinel.
    """

    parsed = validate_budget_config(config)
    if parsed.max_rounds < 1:
        raise InvalidBudgetError("Agent 运行至少需要一轮")
    if parsed.max_duration_ms < 1:
        raise InvalidBudgetError("Agent 执行时长预算必须大于 0")
    if parsed.max_estimated_tokens < 1:
        raise InvalidBudgetError("Agent Token 预算必须大于 0")
    if parsed.max_estimated_cost < 0:
        # Kept explicit even though the schema already rejects this value.
        raise InvalidBudgetError("Agent 成本预算不能为负数")
    return parsed


def validate_budget_usage(usage: BudgetUsage | Mapping[str, Any]) -> BudgetUsage:
    """Validate cumulative usage before comparing it with configured limits."""

    return _validate_budget_model(
        usage, BudgetUsage, "Agent 预算用量必须是有限的非负数"
    )


def is_budget_config_valid(config: AgentBudgetConfig | Mapping[str, Any] | None) -> bool:
    try:
        validate_budget_config(config)  # type: ignore[arg-type]
    except (InvalidBudgetError, TypeError, ValueError):
        return False
    return True


def is_budget_usage_valid(usage: BudgetUsage | Mapping[str, Any] | None) -> bool:
    try:
        validate_budget_usage(usage)  # type: ignore[arg-type]
    except (InvalidBudgetError, TypeError, ValueError):
        return False
    return True


class AgentValidationPolicy:
    """Stateless object facade for callers that prefer service-style methods."""

    is_plan_valid = staticmethod(is_plan_valid)
    validate_plan = staticmethod(validate_plan)
    plan_violations = staticmethod(plan_violations)
    is_result_valid = staticmethod(is_result_valid)
    validate_result = staticmethod(validate_result)
    result_violations = staticmethod(result_violations)
    present_section_keys = staticmethod(present_section_keys)
    missing_section_keys = staticmethod(missing_section_keys)
    normalize_critique = staticmethod(normalize_critique)
    enforce_structure_bounds = staticmethod(enforce_structure_bounds)
    isPlanValid = staticmethod(is_plan_valid)
    validatePlan = staticmethod(validate_plan)
    isResultValid = staticmethod(is_result_valid)
    validateResult = staticmethod(validate_result)
    missingSectionKeys = staticmethod(missing_section_keys)
    enforceStructureBounds = staticmethod(enforce_structure_bounds)


class BudgetValidationPolicy:
    """Stateless facade for budget schema checks."""

    validate_config = staticmethod(validate_budget_config)
    validate_usage = staticmethod(validate_budget_usage)
    validate_runtime_config = staticmethod(validate_runtime_budget_config)
    is_config_valid = staticmethod(is_budget_config_valid)
    is_usage_valid = staticmethod(is_budget_usage_valid)
    validateConfig = staticmethod(validate_budget_config)
    validateUsage = staticmethod(validate_budget_usage)
    validateRuntimeConfig = staticmethod(validate_runtime_budget_config)


# Short aliases are useful to adapters while all aliases share one policy.
AgentPolicy = AgentValidationPolicy
AgentBudgetPolicy = BudgetValidationPolicy


__all__ = [
    "AgentBudgetConfig",
    "AgentBudgetPolicy",
    "AgentPolicy",
    "AgentPolicyError",
    "AgentValidationPolicy",
    "BudgetUsage",
    "BudgetValidationPolicy",
    "InvalidBudgetError",
    "InvalidPlanError",
    "InvalidResultError",
    "MAX_PLAN_TASK_LENGTH",
    "MAX_PLAN_TASKS",
    "enforce_structure_bounds",
    "is_budget_config_valid",
    "is_budget_usage_valid",
    "is_plan_valid",
    "is_result_valid",
    "java_string_length",
    "missing_section_keys",
    "normalize_critique",
    "plan_violations",
    "present_section_keys",
    "result_violations",
    "validate_budget_config",
    "validate_budget_usage",
    "validate_runtime_budget_config",
    "validate_plan",
    "validate_result",
]
