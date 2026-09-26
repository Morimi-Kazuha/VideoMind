from pathlib import Path
import sys

from dovideo.application.evaluation_contracts import MeasurementState

sys.path.insert(0, str(Path(__file__).parents[2] / "tools"))
from run_x3_campaign import _reported_cost, _usage


def test_campaign_usage_reports_exact_stages_and_partial_fields() -> None:
    usage = _usage([
        {"stage": "PLANNER", "model": "a", "inputTokens": 10,
         "outputTokens": 3, "totalTokens": 13, "providerReportedCost": None},
        {"stage": "EXECUTOR", "model": "a", "inputTokens": None,
         "outputTokens": None, "totalTokens": 8, "providerReportedCost": None},
    ])
    assert usage.total_tokens == 21
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.planner.input_tokens == 10
    assert usage.executor.total_tokens == 8
    assert usage.critic.measurement_state is MeasurementState.NOT_MEASURED


def test_campaign_usage_never_truncates_fractional_provider_tokens() -> None:
    usage = _usage([
        {"stage": "PLANNER", "model": "a", "inputTokens": 1.5,
         "outputTokens": 2, "totalTokens": 3.5, "providerReportedCost": None},
    ])
    assert usage.input_tokens is None
    assert usage.total_tokens is None
    assert usage.planner.output_tokens == 2


def test_campaign_cost_preserves_complete_failure_usage_without_estimating_partial_billing() -> None:
    rows = [
        {"providerReportedCost": 0.001},
        {"providerReportedCost": 0.002},
    ]
    complete = _reported_cost(rows)
    assert complete.measurement_state is MeasurementState.MEASURED
    assert complete.provider_reported_cost == 0.003
    assert _reported_cost(rows + [{"providerReportedCost": None}]).measurement_state is MeasurementState.NOT_MEASURED
