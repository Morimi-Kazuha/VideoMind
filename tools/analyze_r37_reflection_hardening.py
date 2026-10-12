"""Analyze immutable R3.7 responses; manual semantic review is recorded separately."""
import json
from pathlib import Path
import statistics

from run_r36_reflection_optimization import ROOT, read, save, summarize


def analyze():
    out = ROOT / "docs/agentic-rag/r37-validation-v1"
    if (out / "aggregate.json").exists():
        raise FileExistsError("never overwrite live analysis")
    cohorts = {name: read(out / name / "results.json") for name in ("development-none-1024", "holdout-none-1024")}
    rows = [r for group in cohorts.values() for r in group]
    aggregate = summarize(rows)
    aggregate["cohorts"] = {name: summarize(group) for name, group in cohorts.items()}
    sent = [r for r in rows if r["http_attempts"]]
    latencies = [r["latency_ms"] / 1000 for r in sent]
    aggregate["latency_seconds"] = {"min": min(latencies), "median": statistics.median(latencies), "max": max(latencies),
        "observed_over_original_5s": sum(t > 5 for t in latencies),
        "scope": "offline end-to-end20s, no production gate or pure provider processing timing"}
    fields = {"status", "reason_code", "covered_requirements", "missing_requirements", "candidate_references"}
    raw_objects = [json.loads(h["raw_structured_response"]) for r in sent for h in r["http"] if h["status_code"] == 200]
    aggregate["responses_all_top_fields_present"] = sum(set(raw) == fields for raw in raw_objects)
    aggregate["responses_all_requirement_reference_fields_present"] = sum(all("candidate_references" in req
        for req in raw.get("covered_requirements", []) + raw.get("missing_requirements", [])) for raw in raw_objects)
    aggregate["span_or_reference_contract_failures"] = sum(r["outcome"] == "INVALID_RESPONSE" for r in sent)
    aggregate["validated_unknown"] = sum(r["assessment"] is not None and r["prediction"] == "UNKNOWN" for r in sent)
    aggregate["unavailable_or_invalid_assessments"] = sum(r["assessment"] is None for r in sent)
    aggregate["population_false_sufficient_rate"] = "NOT_MEASURED; only14 developer-labeled cases, no independent human Gold"
    aggregate["production_shadow_admission_rate"] = "NOT_MEASURED"
    aggregate["production_shadow_skip_ratio"] = "NOT_MEASURED"
    aggregate["production_changed"] = False
    aggregate["additional_http_repair_or_retry_calls"] = sum(r["http_attempts"] for r in rows) - len(sent)
    assert aggregate["sent_attempts"] == 15 and sum(r["http_attempts"] for r in rows) == 15
    assert aggregate["charged_ledger_tokens"] <= 50000
    assert aggregate["responses_with_usage"] == 15
    aggregate["input_estimate_over_actual_ratio_range"] = {
        "min": min(r["estimated_input_tokens"] / r["http"][0]["usage"]["prompt_tokens"] for r in sent),
        "max": max(r["estimated_input_tokens"] / r["http"][0]["usage"]["prompt_tokens"] for r in sent)}
    aggregate["completion_tokens_range"] = {
        "min": min(r["http"][0]["usage"]["completion_tokens"] for r in sent),
        "max": max(r["http"][0]["usage"]["completion_tokens"] for r in sent)}
    olddir = ROOT / "docs/agentic-rag/r36-validation-v1"
    old = {r["id"]: r for path in olddir.glob("*-none-1024/results.json") for r in read(path)}
    paired = []
    for row in rows:
        if row["id"] not in old:
            continue
        before = old[row["id"]]
        assert before["input_sha256"] == row["input_sha256"]
        paired.append({"id": row["id"], "input_sha256": row["input_sha256"],
            "r36_outcome": before["outcome"], "r36_prediction": before["prediction"],
            "r37_outcome": row["outcome"], "r37_prediction": row["prediction"],
            "r36_usage": before["http"][0]["usage"], "r37_usage": row["http"][0]["usage"],
            "r36_latency_ms": before["latency_ms"], "r37_latency_ms": row["latency_ms"],
            "scope": "seen development comparison; not randomized or independent accuracy evidence"})
    aggregate["same_input_paired_case_count"] = len(paired)
    save(out / "paired-r36.json", paired)
    save(out / "aggregate.json", aggregate)
    print(json.dumps(aggregate, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    analyze()
