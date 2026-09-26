"""Validate a completed X3 campaign and derive bounded, read-only comparisons."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import median

from dovideo.application.evaluation_campaign import CAMPAIGN_STRATEGIES
from dovideo.application.evaluation_contracts import (
    EvaluationResultStatus,
    EvaluationRunStatus,
    EvaluationStrategy,
    WorkingTreeState,
    read_case_results,
    read_dataset,
    read_run,
)
from dovideo.application.routing_experiments import RoutingQualityGateV1


FIXED_FOR_LANE = {
    "FAST": EvaluationStrategy.ALWAYS_FAST,
    "BALANCED": EvaluationStrategy.ALWAYS_BALANCED,
    "DEEP": EvaluationStrategy.ALWAYS_DEEP,
}


def analyze(campaign_dir: Path, dataset_path: Path, freeze_path: Path) -> dict:
    """Reject incomplete/mismatched artifacts before deriving descriptive metrics."""
    campaign = json.loads((campaign_dir / "campaign.json").read_text(encoding="utf-8"))
    run_freeze = json.loads((campaign_dir / "freeze.json").read_text(encoding="utf-8"))
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    dataset = read_dataset(dataset_path)
    gate = RoutingQualityGateV1()
    order = [strategy.value for strategy in CAMPAIGN_STRATEGIES]
    if (campaign["status"] != "COMPLETED" or campaign["strategyOrder"] != order
            or campaign["campaignId"] != campaign_dir.name):
        raise ValueError("campaign is incomplete or has a different strategy order")
    if freeze["status"] != "BENCHMARK_READY":
        raise ValueError("supplied benchmark freeze is not ready")
    if campaign["datasetVersion"] != dataset.dataset_version or campaign["datasetDigest"] != dataset.dataset_digest:
        raise ValueError("campaign dataset identity differs from supplied dataset")
    if campaign["qualityGateDigest"] != gate.digest or freeze["qualityGateDigest"] != gate.digest:
        raise ValueError("campaign quality gate differs from frozen gate")
    for key in ("sha", "datasetDigest", "sourceRevision", "pricingDigest", "qualityGateDigest", "ruleRouterDigest", "jevModel"):
        if run_freeze[key] != freeze[key]:
            raise ValueError(f"campaign freeze differs from benchmark freeze: {key}")
    expected_profiles = {lane: profile["fingerprint"] for lane, profile in freeze["profiles"].items()}
    if run_freeze["modelProfileFingerprints"] != expected_profiles:
        raise ValueError("model-profile fingerprints differ from benchmark freeze")
    if freeze["datasetDigest"] != dataset.dataset_digest or freeze["sourceRevision"] != dataset.source_revision_set[0]:
        raise ValueError("benchmark freeze differs from supplied dataset")
    if len(campaign["runs"]) != len(order):
        raise ValueError("campaign does not contain five runs")

    expected_cases = {case.case_id for case in dataset.cases}
    by_strategy = {}
    run_summaries = {}
    for strategy, manifest in zip(CAMPAIGN_STRATEGIES, campaign["runs"], strict=True):
        if manifest["strategy"] != strategy.value or manifest["status"] != "COMPLETED":
            raise ValueError(f"campaign run manifest invalid for {strategy.value}")
        directory = campaign_dir / strategy.value
        run = read_run(directory / "run.json")
        results = read_case_results(directory / "results.jsonl")
        if (run.status is not EvaluationRunStatus.COMPLETED or run.strategy is not strategy
                or run.run_id != manifest["runId"] or run.git_sha != freeze["sha"]
                or run.working_tree_state is not WorkingTreeState.CLEAN
                or run.dataset_digest != dataset.dataset_digest
                or run.dataset_version != dataset.dataset_version
                or run.pricing_version != freeze["pricingVersion"]
                or run.planned_count != len(expected_cases)
                or run.executed_count != len(expected_cases)
                or run.excluded_count != 0 or run.not_run_count != 0
                or run.trial_index != 0):
            raise ValueError(f"frozen run identity invalid for {strategy.value}")
        if len(results) != len(expected_cases) or {item.case_id for item in results} != expected_cases:
            raise ValueError(f"case coverage invalid for {strategy.value}")
        if any(item.run_id != run.run_id or item.strategy is not strategy or item.trial_index != 0
               or item.source_revision != freeze["sourceRevision"] or not item.result_digest
               for item in results):
            raise ValueError(f"case identity invalid for {strategy.value}")
        if tuple(run.execution_order) != tuple(
            f"{item.case_id}:{strategy.value}:0" for item in results
        ):
            raise ValueError(f"execution order invalid for {strategy.value}")
        if run.successful_count != sum(item.status is EvaluationResultStatus.SUCCESSFUL for item in results):
            raise ValueError(f"success count invalid for {strategy.value}")
        if run.failed_count != sum(item.status is EvaluationResultStatus.FAILED for item in results):
            raise ValueError(f"failure count invalid for {strategy.value}")
        by_strategy[strategy] = {item.case_id: item for item in results}
        costs = [item.cost.provider_reported_cost for item in results if item.cost.provider_reported_cost is not None]
        tokens = [item.token_usage.total_tokens for item in results if item.token_usage.total_tokens is not None]
        latencies = [item.latency.total_e2e_ms for item in results if item.latency.total_e2e_ms is not None]
        successful = [item for item in results if item.status is EvaluationResultStatus.SUCCESSFUL]
        retrieval_recalls = [item.deterministic_metrics.retrieval_recall_at_k for item in successful
                             if item.deterministic_metrics.retrieval_recall_at_k is not None]
        evidence_recalls = [item.deterministic_metrics.evidence_recall for item in successful
                            if item.deterministic_metrics.evidence_recall is not None]
        evidence_support = [item.deterministic_metrics.evidence_support_rate for item in successful
                            if item.deterministic_metrics.evidence_support_rate is not None]
        run_summaries[strategy.value] = {
            "runId": run.run_id,
            "cases": len(results),
            "agentSuccesses": len(successful),
            "failures": dict(sorted(Counter(item.failure_category.value for item in results if item.failure_category).items())),
            "qualityGatePasses": sum(gate.passes(item) for item in results),
            "successfulWithSchemaModeEvidencePass": sum(
                item.deterministic_metrics.schema_valid is True
                and item.deterministic_metrics.mode_sections_valid is True
                and item.deterministic_metrics.evidence_guard_pass is True
                for item in successful
            ),
            "successfulWithRequiredFactExactCoverageAtLeast090": sum(
                item.deterministic_metrics.required_fact_exact_coverage is not None
                and item.deterministic_metrics.required_fact_exact_coverage >= 0.90
                for item in successful
            ),
            "routeCounts": dict(sorted(Counter(
                item.route_decision.resolved_lane.value if item.route_decision.resolved_lane else "UNKNOWN"
                for item in results
            ).items())),
            "routeReasons": dict(sorted(Counter(
                item.route_decision.reason_code.value if item.route_decision.reason_code else "NONE"
                for item in results
            ).items())),
            "providerCostKnownCases": len(costs),
            "providerCostSubtotalUsd": round(sum(costs), 12),
            "providerCostComplete": len(costs) == len(results),
            "tokenKnownCases": len(tokens),
            "tokenSubtotal": sum(tokens),
            "latencyKnownCases": len(latencies),
            "medianObservedLatencyMs": median(latencies) if latencies else None,
            "successfulRetrievalRecallAtKKnownCases": len(retrieval_recalls),
            "medianSuccessfulRetrievalRecallAtK": median(retrieval_recalls) if retrieval_recalls else None,
            "successfulEvidenceRecallKnownCases": len(evidence_recalls),
            "medianSuccessfulEvidenceRecall": median(evidence_recalls) if evidence_recalls else None,
            "successfulEvidenceSupportKnownCases": len(evidence_support),
            "medianSuccessfulEvidenceSupportRate": median(evidence_support) if evidence_support else None,
            "successfulCriticFinalPasses": sum(item.critic_metrics.final_pass is True for item in successful),
            "successfulPlannerCallCountKnownCases": sum(item.planner_call_count is not None for item in successful),
            "successfulExecutorCallCountKnownCases": sum(item.executor_call_count is not None for item in successful),
        }

    expected_oracle = {f"{case_id}:0" for case_id in expected_cases}
    if set(campaign["oracle"]) != expected_oracle:
        raise ValueError("oracle does not cover all dataset cases")
    for case_id in expected_cases:
        passing = [by_strategy[strategy][case_id] for strategy in CAMPAIGN_STRATEGIES[:3]
                   if gate.passes(by_strategy[strategy][case_id])]
        if not passing:
            expected = {"status": "NO_PASSING_LANE", "strategy": None}
        else:
            costs = [item.cost.provider_reported_cost
                     if item.cost.provider_reported_cost is not None else item.cost.calculated_cost
                     for item in passing]
            if any(cost is None for cost in costs):
                expected = {"status": "COST_UNAVAILABLE", "strategy": None}
            else:
                best = min(zip(costs, passing), key=lambda pair: pair[0])[1]
                expected = {"status": "SELECTED", "strategy": best.strategy.value}
        if campaign["oracle"][f"{case_id}:0"] != expected:
            raise ValueError(f"oracle differs from fixed-lane results: {case_id}")
    oracle_counts = dict(sorted(Counter(item["status"] for item in campaign["oracle"].values()).items()))

    paired = {}
    for router in (EvaluationStrategy.RULE_ROUTER, EvaluationStrategy.JEV_ROUTER):
        pairs = []
        for case_id, routed in by_strategy[router].items():
            lane = routed.route_decision.resolved_lane
            if lane is None:
                raise ValueError(f"resolved lane missing for {router.value}: {case_id}")
            fixed = by_strategy[FIXED_FOR_LANE[lane.value]][case_id]
            pairs.append((routed, fixed))
        cost_pairs = [(r.cost.provider_reported_cost, f.cost.provider_reported_cost) for r, f in pairs
                      if r.cost.provider_reported_cost is not None and f.cost.provider_reported_cost is not None]
        latency_pairs = [(r.latency.total_e2e_ms, f.latency.total_e2e_ms) for r, f in pairs
                         if r.latency.total_e2e_ms is not None and f.latency.total_e2e_ms is not None]
        paired[router.value] = {
            "matchedCases": len(pairs),
            "routedAgentSuccesses": sum(r.status is EvaluationResultStatus.SUCCESSFUL for r, _ in pairs),
            "selectedFixedLaneAgentSuccesses": sum(f.status is EvaluationResultStatus.SUCCESSFUL for _, f in pairs),
            "routedOnlyAgentSuccesses": sum(r.status is EvaluationResultStatus.SUCCESSFUL and f.status is not EvaluationResultStatus.SUCCESSFUL for r, f in pairs),
            "fixedOnlyAgentSuccesses": sum(f.status is EvaluationResultStatus.SUCCESSFUL and r.status is not EvaluationResultStatus.SUCCESSFUL for r, f in pairs),
            "routedQualityGatePasses": sum(gate.passes(r) for r, _ in pairs),
            "selectedFixedLaneQualityGatePasses": sum(gate.passes(f) for _, f in pairs),
            "pairedCostKnownCases": len(cost_pairs),
            "reportedCostDeltaSubtotalUsd": round(sum(r - f for r, f in cost_pairs), 12),
            "pairedLatencyKnownCases": len(latency_pairs),
            "medianObservedLatencyDeltaMs": median(r - f for r, f in latency_pairs) if latency_pairs else None,
        }

    return {
        "analysisClassification": "DERIVED_FROM_MEASURED_CASE_ARTIFACTS",
        "campaignId": campaign["campaignId"],
        "benchmarkFreezeSha": freeze["sha"],
        "datasetVersion": dataset.dataset_version,
        "datasetDigest": dataset.dataset_digest,
        "datasetCompleteness": dataset.completeness.value,
        "sourceRevision": freeze["sourceRevision"],
        "oracleCounts": oracle_counts,
        "runs": run_summaries,
        "routingAblations": paired,
        "interpretationLimit": "One trial per case; paired executions are separate stochastic runs. Cost deltas cover only pairs with both provider costs. No causal routing-quality conclusion follows when the frozen gate has no passing lane.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("campaign_dir", type=Path)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    analysis = analyze(args.campaign_dir, args.dataset, args.freeze)
    payload = json.dumps(analysis, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
