"""Group five independent X3 routing runs without changing run semantics."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .evaluation_contracts import (
    EvaluationRunStatus,
    EvaluationStrategy,
    validate_dataset,
)
from .evaluation_runner import EvaluationRunResult, EvaluationRunner, EvaluationRunnerConfig
from .routing_experiments import RoutingQualityGateV1


CAMPAIGN_STRATEGIES = (
    EvaluationStrategy.ALWAYS_FAST,
    EvaluationStrategy.ALWAYS_BALANCED,
    EvaluationStrategy.ALWAYS_DEEP,
    EvaluationStrategy.RULE_ROUTER,
    EvaluationStrategy.JEV_ROUTER,
)
FIXED_STRATEGIES = CAMPAIGN_STRATEGIES[:3]


@dataclass(frozen=True, slots=True)
class EvaluationCampaignResult:
    campaign_id: str
    dataset_version: str
    dataset_digest: str
    status: str
    runs: tuple[EvaluationRunResult, ...]
    oracle: Mapping[str, Mapping[str, Any]]
    artifact_path: Path


class EvaluationCampaignRunner:
    """Preflight, execute, and persist strategy-specific EvaluationRuns."""

    def __init__(
        self,
        runners: Mapping[EvaluationStrategy, EvaluationRunner],
        *,
        artifact_root: str | Path,
        campaign_id: str | None = None,
        quality_gate: RoutingQualityGateV1 | None = None,
    ) -> None:
        if set(runners) != set(CAMPAIGN_STRATEGIES):
            raise ValueError("campaign requires exactly the five routing strategies")
        self._runners = dict(runners)
        self._artifact_root = Path(artifact_root)
        self._campaign_id = campaign_id or uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", self._campaign_id):
            raise ValueError("campaign ID must be a single path component")
        self._gate = quality_gate or RoutingQualityGateV1()

    async def run(
        self,
        dataset: Any,
        config: EvaluationRunnerConfig | None = None,
    ) -> EvaluationCampaignResult:
        validated = validate_dataset(dataset)
        base = config or EvaluationRunnerConfig()
        directory = self._artifact_root / self._campaign_id
        artifact_path = directory / "campaign.json"
        if directory.exists() and any(directory.iterdir()):
            raise ValueError("campaign directory already contains artifacts")
        configs = {
            strategy: EvaluationRunnerConfig.model_validate(
                {
                    **base.model_dump(mode="python", by_alias=True),
                    "strategy": strategy,
                    "artifactOutput": str(directory / strategy.value),
                }
            )
            for strategy in CAMPAIGN_STRATEGIES
        }
        # Validate all five lanes before the first measured execution.
        for strategy in CAMPAIGN_STRATEGIES:
            self._runners[strategy].preflight(validated, configs[strategy])

        started_at = datetime.now(timezone.utc).isoformat()
        results: list[EvaluationRunResult] = []
        oracle: dict[str, dict[str, Any]] = {}

        def persist(status: str, *, error_type: str | None = None) -> None:
            payload = {
                "campaignId": self._campaign_id,
                "datasetVersion": validated.dataset_version,
                "datasetDigest": validated.dataset_digest,
                "startedAt": started_at,
                "status": status,
                "strategyOrder": [strategy.value for strategy in CAMPAIGN_STRATEGIES],
                "qualityGateVersion": self._gate.configuration.gate_version,
                "qualityGateDigest": self._gate.digest,
                "runs": [
                    {"strategy": result.run.strategy.value, "runId": result.run.run_id,
                     "status": result.run.status.value}
                    for result in results
                ],
                "oracle": oracle,
                "errorType": error_type,
            }
            directory.mkdir(parents=True, exist_ok=True)
            temporary = artifact_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, artifact_path)

        persist("RUNNING")
        try:
            for strategy in CAMPAIGN_STRATEGIES:
                result = await self._runners[strategy].run(validated, configs[strategy])
                if result.run.strategy is not strategy:
                    raise RuntimeError("campaign runner returned a different strategy")
                results.append(result)
                if strategy is EvaluationStrategy.ALWAYS_DEEP:
                    oracle = _derive_oracle(tuple(results), self._gate)
                persist("RUNNING")
                if result.run.status is not EvaluationRunStatus.COMPLETED:
                    raise RuntimeError("strategy run is partial; campaign cannot advance")
        except Exception as error:
            persist("PARTIAL", error_type=type(error).__name__)
            raise
        status = "COMPLETED"
        persist(status)
        return EvaluationCampaignResult(
            self._campaign_id, validated.dataset_version,
            validated.dataset_digest, status, tuple(results), oracle, artifact_path
        )


def _derive_oracle(
    fixed_runs: tuple[EvaluationRunResult, ...], gate: RoutingQualityGateV1
) -> dict[str, dict[str, Any]]:
    by_case_trial: dict[str, list[Any]] = {}
    for run in fixed_runs:
        for result in run.results:
            key = f"{result.case_id}:{result.trial_index}"
            by_case_trial.setdefault(key, []).append(result)
    oracle: dict[str, dict[str, Any]] = {}
    for key, results in by_case_trial.items():
        if {result.strategy for result in results} != set(FIXED_STRATEGIES):
            oracle[key] = {"status": "INCOMPLETE_FIXED_LANES", "strategy": None}
            continue
        passing = [result for result in results if gate.passes(result)]
        if not passing:
            oracle[key] = {"status": "NO_PASSING_LANE", "strategy": None}
            continue
        costs = [
            result.cost.provider_reported_cost
            if result.cost.provider_reported_cost is not None
            else result.cost.calculated_cost
            for result in passing
        ]
        if any(cost is None for cost in costs):
            oracle[key] = {"status": "COST_UNAVAILABLE", "strategy": None}
            continue
        best = min(zip(costs, passing), key=lambda pair: pair[0])[1]
        oracle[key] = {"status": "SELECTED", "strategy": best.strategy.value}
    return oracle


__all__ = [
    "CAMPAIGN_STRATEGIES", "FIXED_STRATEGIES", "EvaluationCampaignResult",
    "EvaluationCampaignRunner",
]
