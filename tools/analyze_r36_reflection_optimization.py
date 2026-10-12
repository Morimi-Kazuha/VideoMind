"""No-network analysis of frozen R3.5 baseline and R3.6 experiment artifacts."""
import argparse
import json
from pathlib import Path
import statistics

from run_r36_reflection_optimization import ROOT, summarize, classify, complete_json
from run_r3_evidence_reflection import read, save


def analyze(out):
    runs = {name: read(out / name / "results.json") for name in (
        "calibration-none-1024", "holdout-none-1024", "draft-none-1024")}
    rows = [r for group in runs.values() for r in group]
    aggregate = summarize(rows)
    aggregate["cohorts"] = {name: summarize(group) for name, group in runs.items()}
    aggregate["latency_seconds"] = {
        "min": min(r["latency_ms"] for r in rows) / 1000,
        "median": statistics.median(r["latency_ms"] for r in rows) / 1000,
        "max": max(r["latency_ms"] for r in rows) / 1000,
        "observed_over_original_5s": sum(r["latency_ms"] > 5000 for r in rows),
        "scope": "offline end-to-end, 20s limit; no pure provider processing measurement or production P95",
    }
    output = [h["usage"]["completion_tokens"] for r in rows for h in r["http"]]
    aggregate["completion_tokens_range"] = {"min": min(output), "max": max(output)}
    ratios = [r["estimated_input_tokens"] / r["http"][0]["usage"]["prompt_tokens"] for r in rows]
    aggregate["input_estimate_over_reported_ratio_range"] = {"min": min(ratios), "max": max(ratios)}
    aggregate["original_policy_shadow_population_skip_ratio"] = "NOT_MEASURED"
    aggregate["root_cause_scope"] = "All 15 optimized requests report reasoning_tokens=0. Historic baseline reasoning breakdown absent; cannot retrospectively prove every old cap token was reasoning."
    assert aggregate["sent_attempts"] <= 15 and aggregate["charged_ledger_tokens"] <= 45000
    baseline = ROOT / "docs/agentic-rag/r35-validation-v1"
    old5 = read(baseline / "provider-probe-documented-tag/results.json") + read(baseline / "live-frozen-v1/results.json")
    old20 = read(baseline / "longer-wait-diagnostic-v1/results.json")
    pairing = []
    for row in rows:
        a = next((r for r in old5 if r["id"] == row["id"]), None)
        b = next((r for r in old20 if r["id"] == row["id"]), None)
        assert all(r["input_sha256"] == row["input_sha256"] for r in (a, b) if r)
        pairing.append({"id": row["id"], "input_sha256": row["input_sha256"], "split": row["split"],
                        "original_5s": a, "original_20s": b, "none_20s": row})
    save(out / "aggregate.json", aggregate)
    save(out / "paired-baseline.json", pairing)
    # Keep syntax, structural validity, application UNKNOWN and semantic UNKNOWN distinct.
    save(out / "per-case-metrics.json", [{
        "id": r["id"], "split": r["split"], "expected": r["expected_status"],
        "outcome": classify(r), "complete_json": complete_json(r["http"][0]),
        "strict_dto": r["assessment"] is not None, "model_status": r["prediction"] if r["assessment"] else None,
        "application_status": r["prediction"], "usage": r["http"][0]["usage"],
        "reasoning_tokens": r["http"][0]["reasoning_tokens"], "latency_seconds": r["latency_ms"] / 1000,
    } for r in rows])
    print(json.dumps(aggregate, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, default=ROOT / "docs/agentic-rag/r36-validation-v1")
    analyze(p.parse_args().output)
