"""No-network cost/admission scenarios; NOT a production population measurement."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from run_r3_evidence_reflection import read, save
from dovideo.application.follow_up_budget import (
    FollowUpBudgetMode, FollowUpBudgetSettings, follow_up_budget_scope, optional_model_headroom,
    estimate_chat_input,
)
from dovideo.infrastructure.providers.evidence_reflection import EvidenceReflectionModelAdapter

OUT = ROOT / "docs/agentic-rag/r37-validation-v1"


def analyze():
    if (OUT / "offline-budget-and-cost.json").exists():
        raise FileExistsError("do not overwrite offline preparation")
    plan = read(OUT / "experiment-plan.json")
    cases = read(OUT / "frozen-sidecar-v1.json")["cases"]
    rows = []
    # Actual production helper and updated prompt, but synthetic request state.
    for mixed, memory, attempts in ((False, False, 3), (False, True, 3), (True, False, 3), (True, True, 3), (True, True, 1)):
        inp = next(c["input"] for c in cases if c["input"]["mixed"] == mixed)
        messages = EvidenceReflectionModelAdapter.messages(inp["question"], tuple(inp["candidates"]), mixed=mixed)
        downstream = attempts * (30000 + (8000 if mixed else 0) + (12000 if memory else 0))
        needed = estimate_chat_input(messages) + 1024 + downstream
        for limit in (25000, 100000, 160000, needed - 1, needed):
            with follow_up_budget_scope(FollowUpBudgetSettings(FollowUpBudgetMode.SHADOW, limit)):
                admitted = optional_model_headroom(messages, "EVIDENCE_REFLECTION", 1024, downstream)
            rows.append({"mixed": mixed, "memory": memory, "provider_max_attempts": attempts,
                "synthetic_prior_charged_tokens": 0, "synthetic_pending_tokens": 0, "limit": limit,
                "reflection_input_estimate": estimate_chat_input(messages), "reflection_output_reserve": 1024,
                "downstream_reserve": downstream, "minimum_uncharged_limit": needed,
                "headroom_helper_allows": admitted, "production_time_gate_seconds": 55 if mixed else 40})
    metadata = read(OUT / "provider-public-metadata.json")
    endpoint = next(e for e in metadata["endpoints"] if e["tag"] == "siliconflow/fp8")
    incoming = float(endpoint["pricing"]["prompt"])
    outgoing = float(endpoint["pricing"]["completion"])
    output_max = 15 * 1024
    estimated_input = sum(r["estimated_input_tokens"] for r in plan["planned_cases"])
    old = read(ROOT / "docs/agentic-rag/r36-validation-v1/aggregate.json")
    save(OUT / "offline-budget-and-cost.json", {
        "mode": "DETERMINISTIC_SYNTHETIC_REQUEST_STATE_NO_HTTP",
        "production_shadow_admission_rate": "NOT_MEASURED", "production_skip_ratio": "NOT_MEASURED",
        "all_unknown_usage_conservative_tokens": plan["all_unreported_estimate_tokens"],
        "campaign_hard_token_ceiling": 50000, "insufficient_campaign_headroom_policy": "skip remaining cases, no expansion",
        "uncached_price_source": metadata["source"], "input_price_per_token_usd": incoming,
        "output_price_per_token_usd": outgoing,
        "estimated_uncached_cost_if_estimates_match_usage_usd": estimated_input * incoming + output_max * outgoing,
        "price_envelope_50000_tokens_and_15360_output_usd": (50000 - output_max) * incoming + output_max * outgoing,
        "billing_limit_note": "Token approval, not a dollar guarantee; provider Usage and price/caching may differ. No requests sent.",
        "historical_r36_tokens": old["provider_reported_tokens"],
        "historical_r36_cost_usd": old["reported_cost_partial_usd"],
        "downstream_real_usage": "NOT_MEASURED; historical data is reflection-only, not answer/knowledge/summary",
        "production_changed": False, "request_deadline_seconds": 60, "reflection_local_timeout_seconds": 5,
        "stage_inventory": [
            {"stage": "QUERY_REWRITE", "logical_max": 1, "http_max": "A (configured max_attempts, default3)", "output_cap": 512, "local_timeout_seconds": 8, "conditional": "memory/reference resolution"},
            {"stage": "ADAPTIVE_RETRIEVAL_PLANNER", "logical_max": 1, "http_max": 1, "output_cap": 1024, "local_timeout_seconds": "default5, configurable<=10", "conditional": "M2 complex route; existing only"},
            {"stage": "RETRIEVAL_PLANNER", "logical_max": "1 baseline or <=3 M2 subqueries", "http_max": "<=3*A", "output_cap": "profile-dependent; request ledger estimates1024 when no cap", "local_timeout_seconds": "remaining global60s; no separate hard stage deadline", "conditional": "VIDEO/MIXED; no DIRECT"},
            {"stage": "FOLLOW_UP", "logical_max": 1, "http_max": "A", "output_cap": 4096, "local_timeout_seconds": 30, "conditional": "VIDEO or MIXED fact-only answer"},
            {"stage": "MIXED_KNOWLEDGE", "logical_max": 1, "http_max": "A", "output_cap": 2048, "local_timeout_seconds": 30, "conditional": "verified quotes and MIXED route only"},
            {"stage": "ROLLING_SUMMARY", "logical_max": 1, "http_max": "A", "output_cap": 2048, "local_timeout_seconds": "8 model, 10 enclosing append/store", "conditional": "memory compression threshold; cannot erase valid answer on optional failure"},
            {"stage": "EVIDENCE_REFLECTION", "logical_max": 1, "http_max": 1, "output_cap": 1024, "local_timeout_seconds": 5, "conditional": "SHADOW + existing trusted candidates + measurable headroom"},
            {"stage": "QUERY_REPAIR", "logical_max": "NOT_IMPLEMENTED; future at most1", "http_max": "NOT_IMPLEMENTED; recommend1", "output_cap": "NOT_CALIBRATED", "local_timeout_seconds": "NOT_CALIBRATED; must share60s", "conditional": "no permission/token reserve implemented by R3.7"},
        ],
        "scenarios": rows,
        "interpretation": "Conservative admission needs90k/126k VIDEO and114k/150k MIXED downstream with A3, plus reflection and prior charges. MIXED only has5s prior-work allowance at60s. No downstream statistics justify relaxing either gate. Nested30s deadlines share global deadline; worst attempts are bounds, not executable promises.",
        "minimal_calibration_plan": "Default OFF. Later explicit Shadow sample: record numeric stage usage, gate outcomes and remaining time across VIDEO/MIXED, memory and A; preserve missing-usage reservations and60s. Do not send production prompts or change limits in R3.7."})


if __name__ == "__main__":
    analyze()
