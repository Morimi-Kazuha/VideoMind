from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from dovideo.domain import AgentBudgetConfig, BudgetUsage
from dovideo.presentation.composition import _agent_budget_from_environment


def test_r5_default_keeps_50k_tokens_with_four_minute_execution_window() -> None:
    budget = _agent_budget_from_environment({})
    assert budget.max_estimated_tokens == 50_000
    assert budget.max_duration_ms == 240_000
    assert AgentBudgetConfig().max_duration_ms == 240_000


def test_budget_models_have_java_aliases_and_allow_zero() -> None:
    config = AgentBudgetConfig(
        maxRounds=0,
        maxDurationMs=0,
        maxEstimatedTokens=0,
        maxEstimatedCost=0,
    )
    usage = BudgetUsage(estimatedTokens=0, estimatedCost=0)
    assert config.max_rounds == config.max_duration_ms == config.max_estimated_tokens == 0
    assert usage.estimated_tokens == 0
    assert config.model_dump(by_alias=True)["maxEstimatedCost"] == 0.0
    assert usage.model_dump(by_alias=True)["estimatedTokens"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_rounds", -1),
        ("max_duration_ms", -1),
        ("max_estimated_tokens", -1),
        ("max_estimated_cost", -1.0),
        ("max_estimated_cost", math.nan),
        ("max_estimated_cost", math.inf),
        ("max_estimated_cost", -math.inf),
    ],
)
def test_budget_config_rejects_negative_and_nonfinite_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        AgentBudgetConfig(**{field: value})


@pytest.mark.parametrize("value", [-1, -1.0, math.nan, math.inf, -math.inf])
def test_budget_usage_rejects_negative_and_nonfinite_values(value: object) -> None:
    with pytest.raises(ValidationError):
        BudgetUsage(estimatedCost=value)

    with pytest.raises(ValidationError):
        BudgetUsage(estimatedTokens=value)


def test_explicit_token_switch_defaults_and_invalid_configuration():
    assert _agent_budget_from_environment({}).token_budget_enabled is True
    disabled = _agent_budget_from_environment({"DOVIDEO_AGENT_TOKEN_BUDGET_ENABLED": "false"})
    assert disabled.token_budget_enabled is False
    assert disabled.max_estimated_tokens == 50000
    assert disabled.max_rounds == 2
    assert disabled.max_duration_ms == 240000
    with pytest.raises(ValueError):
        _agent_budget_from_environment({"DOVIDEO_AGENT_TOKEN_BUDGET_ENABLED": "typo"})
