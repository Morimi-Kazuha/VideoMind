from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dovideo.application.evaluation_campaign import (
    CAMPAIGN_STRATEGIES,
    EvaluationCampaignRunner,
)
from dovideo.application.evaluation_contracts import (
    CostMeasurement,
    DeterministicMetrics,
    EvaluationCaseResult,
    EvaluationRun,
    MeasurementState,
    read_dataset,
)
from dovideo.application.evaluation_runner import EvaluationRunnerConfig


def _dataset():
    return read_dataset(Path(__file__).parents[2] / "datasets" / "x3" / "golden-dataset-v2.json")


class _Runner:
    def __init__(self, strategy, calls, cost):
        self.strategy = strategy
        self.calls = calls
        self.cost = cost

    def preflight(self, dataset, config):
        assert config.strategy is self.strategy
        self.calls.append(("preflight", self.strategy.value))

    async def run(self, dataset, config):
        self.calls.append(("run", self.strategy.value))
        case = dataset.case("long-general-001")
        run = EvaluationRun(
            run_id=f"run-{self.strategy.value}",
            dataset_version=dataset.dataset_version,
            dataset_digest=dataset.dataset_digest,
            strategy=self.strategy,
            planned_count=1,
            executed_count=1,
            successful_count=1,
            status="COMPLETED",
        )
        result = EvaluationCaseResult(
            run_id=run.run_id,
            case_id=case.case_id,
            strategy=self.strategy,
            trial_index=0,
            mode=case.mode,
            query_category=case.query_category,
            source_revision=case.source_revision,
            status="SUCCESSFUL",
            deterministic_metrics=DeterministicMetrics(
                schema_valid=True,
                mode_sections_valid=True,
                evidence_guard_pass=True,
                required_fact_exact_coverage=1.0,
                measurement_state=MeasurementState.MEASURED,
            ),
            cost=CostMeasurement(
                provider_reported_cost=self.cost,
                measurement_state=MeasurementState.MEASURED,
            ) if self.cost is not None else CostMeasurement(),
        )
        return SimpleNamespace(run=run, results=(result,))


@pytest.mark.asyncio
async def test_campaign_keeps_five_runs_separate_and_derives_oracle_after_fixed_lanes(tmp_path) -> None:
    calls = []
    costs = [0.2, 0.3, 0.4, 0.5, 0.6]
    runners = {
        strategy: _Runner(strategy, calls, cost)
        for strategy, cost in zip(CAMPAIGN_STRATEGIES, costs, strict=True)
    }
    campaign = EvaluationCampaignRunner(runners, artifact_root=tmp_path, campaign_id="smoke")
    result = await campaign.run(
        _dataset(), EvaluationRunnerConfig(case_filter=("long-general-001",))
    )
    assert calls[:5] == [("preflight", strategy.value) for strategy in CAMPAIGN_STRATEGIES]
    assert calls[5:] == [("run", strategy.value) for strategy in CAMPAIGN_STRATEGIES]
    assert result.status == "COMPLETED"
    assert [run.run.strategy for run in result.runs] == list(CAMPAIGN_STRATEGIES)
    assert len({run.run.run_id for run in result.runs}) == 5
    assert result.oracle["long-general-001:0"] == {
        "status": "SELECTED", "strategy": "ALWAYS_FAST"
    }
    artifact = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    assert len(artifact["runs"]) == 5
    assert artifact["datasetDigest"] == _dataset().dataset_digest
    assert artifact["qualityGateDigest"] == campaign._gate.digest
    with pytest.raises(ValueError, match="already contains artifacts"):
        await campaign.run(_dataset())


@pytest.mark.asyncio
async def test_campaign_does_not_start_when_any_strategy_preflight_fails(tmp_path) -> None:
    calls = []
    runners = {strategy: _Runner(strategy, calls, 1.0) for strategy in CAMPAIGN_STRATEGIES}

    def fail(*_args):
        raise RuntimeError("preflight rejected")

    runners[CAMPAIGN_STRATEGIES[-1]].preflight = fail
    campaign = EvaluationCampaignRunner(runners, artifact_root=tmp_path, campaign_id="bad")
    with pytest.raises(RuntimeError, match="preflight rejected"):
        await campaign.run(_dataset())
    assert not any(kind == "run" for kind, _strategy in calls)
    assert not (tmp_path / "bad" / "campaign.json").exists()


@pytest.mark.asyncio
async def test_campaign_does_not_invent_oracle_cost(tmp_path) -> None:
    calls = []
    runners = {strategy: _Runner(strategy, calls, None) for strategy in CAMPAIGN_STRATEGIES}
    campaign = EvaluationCampaignRunner(runners, artifact_root=tmp_path, campaign_id="no-cost")
    result = await campaign.run(
        _dataset(), EvaluationRunnerConfig(case_filter=("long-general-001",))
    )
    assert result.oracle["long-general-001:0"] == {
        "status": "COST_UNAVAILABLE", "strategy": None
    }
