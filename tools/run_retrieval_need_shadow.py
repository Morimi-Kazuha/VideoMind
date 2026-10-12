"""Small R1 classification sidecar runner, following existing tools' JSON artifacts.

No video processing, retrieval or Provider execution. Human-unreviewed draft
and ambiguous cases are excluded from scored contract metrics. Their decisions
and safety checks remain visible. Labels never enter the policy input.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dovideo.application.retrieval_need import NeedInput, RetrievalNeed, RetrievalNeedPolicy, POLICY_VERSION


def metrics(rows):
    names = [route.value for route in RetrievalNeed]
    matrix = {expected: {predicted: 0 for predicted in names} for expected in names}
    for row in rows:
        matrix[row["expected"]][row["decision"]["need"]] += 1
    per_class = {}
    for name in names:
        tp = matrix[name][name]
        predicted = sum(matrix[expected][name] for expected in names)
        support = sum(matrix[name].values())
        per_class[name] = {"precision": tp / predicted if predicted else None,
                           "recall": tp / support if support else None, "support": support}
    return {"sample_count": len(rows), "confusion_matrix": matrix, "per_class": per_class,
            "video_or_mixed_predicted_direct": sum(
                row["expected"] != "DIRECT_LLM" and row["decision"]["need"] == "DIRECT_LLM" for row in rows)}


def evaluate(dataset_path: Path, output: Path, repeats: int = 100):
    if output.exists():
        raise ValueError("output already exists; preserve previous evaluations")
    if not 1 <= repeats <= 1000:
        raise ValueError("repeats must be between 1 and 1000")
    raw = dataset_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    expected_digest = dataset_path.with_suffix(".sha256").read_text(encoding="utf-8").strip()
    if digest != expected_digest:
        raise ValueError("frozen dataset digest mismatch")
    dataset = json.loads(raw)
    if not dataset["frozen"] or dataset["version"] != "retrieval-need-r1-v1":
        raise ValueError("unsupported or unfrozen classification sidecar")
    policy = RetrievalNeedPolicy()
    rows, latencies = [], []
    for case in dataset["cases"]:
        # Explicit projection: expected/review/ambiguity metadata stays out.
        value = NeedInput.model_validate(case["input"])
        samples, decisions = [], []
        for _ in range(repeats):
            started = time.perf_counter()
            decision = policy.decide(value)
            samples.append((time.perf_counter() - started) * 1000)
            decisions.append(decision.model_dump(mode="json"))
        if any(result != decisions[0] for result in decisions):
            raise ValueError("non-deterministic rule decision")
        rows.append({"case_id": case["id"], "cohort": case["cohort"], "expected": case["expected"],
                     "ambiguity": case["ambiguity"], "decision": decisions[0],
                     "median_decision_latency_ms": statistics.median(samples)})
        latencies.extend(samples)
    scored = [row for row in rows if row["cohort"] == "synthetic_contract" and not row["ambiguity"]]
    sources = Counter(row["decision"]["decision_source"] for row in rows)
    reasons = Counter(row["decision"]["reason_code"] for row in rows)
    groups = {}
    for name in sorted({row["cohort"] for row in rows}):
        selected = [row for row in rows if row["cohort"] == name]
        groups[name] = {"count": len(selected), "route_counts": dict(Counter(row["decision"]["need"] for row in selected)),
                        "fallback_count": sum(row["decision"]["decision_source"] == "FALLBACK" for row in selected),
                        "video_safety_direct_count": sum(row["expected"] != "DIRECT_LLM" and row["decision"]["need"] == "DIRECT_LLM" for row in selected)}
    summary = {"dataset_version": dataset["version"], "dataset_sha256": digest, "policy_version": POLICY_VERSION,
               "classification_scope": "DETERMINISTIC_CONTRACT_ONLY; not human-reviewed semantic accuracy",
               "human_reviewed_cases": 0, "semantic_accuracy": "NOT_MEASURED",
               "scored_contract": metrics(scored), "cohorts": groups,
               "total_cases": len(rows), "decision_sources": dict(sources), "reason_codes": dict(reasons),
               "safe_fallback_count": sources["FALLBACK"], "safe_fallback_ratio": sources["FALLBACK"] / len(rows),
               "positive_rule_coverage": sources["RULE"] / len(rows),
               "latency": {"measurement": "policy.decide including DTO revalidation; excludes logging and business path",
                           "samples": len(latencies), "p50_ms": statistics.median(latencies),
                           "p95_ms": sorted(latencies)[int((len(latencies) - 1) * .95)], "max_ms": max(latencies)},
               "additional_classifier_provider_calls": 0, "provider_execution": "NOT_RUN",
               "retrieval_call_change": "separately verified by OFF/SHADOW integration tests"}
    output.mkdir(parents=True, exist_ok=False)
    (output / "run.json").write_text(json.dumps({"kind": "R1_CLASSIFICATION_SIDECAR", "dataset_sha256": digest,
        "policy_version": POLICY_VERSION, "repeats": repeats, "provider_execution": "NOT_RUN"}, indent=2) + "\n", encoding="utf-8")
    (output / "decisions.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "datasets/agentic-rag/retrieval-need-synthetic-v1.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.dataset, args.output, args.repeats), ensure_ascii=False, indent=2))
