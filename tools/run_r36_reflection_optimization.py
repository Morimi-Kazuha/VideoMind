"""Bounded offline R3.6 experiment using the existing chat/DTO/budget path.

No production defaults are changed. Live runs require explicit new limits;
the operator must obtain authorization before using --live.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from run_r3_evidence_reflection import read, save
from reflection_validation_support import load_chat_environment, public_config, RecordingHttp
from dovideo.application.evidence_reflection import EvidenceAssessment, validate_assessment
from dovideo.application.errors import BudgetExceededError, DeadlineExceededError
from dovideo.application.execution_budget import AgentExecutionBudget
from dovideo.application.follow_up_budget import (
    FollowUpBudgetMode, FollowUpBudgetSettings, capture_follow_up_budget,
    estimate_chat_input, follow_up_budget_scope, optional_model_headroom,
)
from dovideo.infrastructure.providers.config import ModelRequestSettings, ProviderConfig
from dovideo.infrastructure.providers.model import OpenAICompatibleChatClient
from reflection_prompt_snapshots import HistoricalReflectionAdapter as EvidenceReflectionModelAdapter, R3_SYSTEM as SYSTEM
from dovideo.infrastructure.providers.follow_up import _unique_object


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ExperimentHttp(RecordingHttp):
    """Record only public wire controls and numeric usage, never reasoning text."""

    async def post(self, url, **kwargs):
        payload = kwargs["json"]
        if payload.get("max_tokens") != 1024:
            raise ValueError("experiment cap must match the existing 1024 admission reserve")
        expected_policy = {"only": ["siliconflow/fp8"], "allow_fallbacks": False,
                           "require_parameters": True, "data_collection": "deny", "zdr": True}
        if payload.get("provider") != expected_policy or payload.get("model") != "deepseek/deepseek-v4.1-flash":
            raise ValueError("experiment model/provider policy changed")
        controls = {k: payload[k] for k in ("model", "reasoning_effort", "max_tokens", "response_format", "provider") if k in payload}
        controls["messages_sha256"] = digest(payload["messages"])
        start = len(self.records)
        try:
            response = await super().post(url, **kwargs)
            body = response.body
            if isinstance(body, dict):
                # These are public response identifiers, not reasoning fields.
                for name in ("model", "provider"):
                    if isinstance(body.get(name), str):
                        self.records[start]["returned_" + name] = body[name][:200]
                value = self.records[start].get("reasoning_tokens")
                if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 10**9:
                    self.records[start]["reasoning_tokens"] = None
            return response
        finally:
            if len(self.records) > start:
                self.records[start]["sent_parameters"] = controls


def complete_json(record):
    raw = record.get("raw_structured_response")
    if not isinstance(raw, str):
        return False
    try:
        return isinstance(json.loads(raw, object_pairs_hook=_unique_object), dict)
    except (ValueError, TypeError):
        return False


def classify(row):
    http = row["http"]
    if any(h.get("refusal") for h in http):
        return "MODEL_REFUSAL"
    if any(h.get("finish_reason") == "length" for h in http):
        return "OUTPUT_TOKEN_LIMIT"
    if any(h.get("status_code") in {400, 422} for h in http):
        return "PROVIDER_PARAMETER_REJECTION"
    if any(h.get("status_code") == 404 for h in http):
        return "PROVIDER_ROUTING_REJECTION"
    return row["outcome"]


def summarize(rows):
    sent = [r for r in rows if r["http_attempts"]]
    responded = [r for r in sent if any(h.get("status_code") is not None for h in r["http"])]
    successful = [r for r in sent if any(h.get("status_code") == 200 for h in r["http"])]
    valid = [r for r in responded if r["assessment"]]
    usages = [h["usage"] for r in rows for h in r["http"] if h.get("usage")]
    details = [h["reasoning_tokens"] for r in rows for h in r["http"] if h.get("reasoning_tokens") is not None]
    checked = [r for r in valid if r["expected_status"] is not None]
    denominator = len(responded)
    return {
        "sent_attempts": len(sent), "completed_http_responses": denominator,
        "http_200": len(successful), "complete_json": sum(complete_json(h) for r in responded for h in r["http"]),
        "strict_dto_valid": len(valid),
        "valid_assessment_rate_completed_requests": len(valid) / denominator if denominator else None,
        "valid_assessment_rate_all_sent": len(valid) / len(sent) if sent else None,
        "http_success_rate_all_sent": len(successful) / len(sent) if sent else None,
        "complete_json_rate_completed_requests": sum(complete_json(h) for r in responded for h in r["http"]) / denominator if denominator else None,
        "outcomes": dict(Counter(classify(r) for r in rows)),
        "timeout_rate_all_sent": sum(r["outcome"] == "TIMEOUT" for r in sent) / len(sent) if sent else None,
        "completion_cap_rate_completed_requests": sum(classify(r) == "OUTPUT_TOKEN_LIMIT" for r in responded) / denominator if denominator else None,
        "parameter_rejection_rate_completed_requests": sum(classify(r) == "PROVIDER_PARAMETER_REJECTION" for r in responded) / denominator if denominator else None,
        "provider_reported_tokens": sum(u.get("total_tokens", 0) for u in usages),
        "provider_input_tokens": sum(u.get("prompt_tokens", 0) for u in usages),
        "provider_completion_tokens": sum(u.get("completion_tokens", 0) for u in usages),
        "numeric_reasoning_tokens": sum(details) if details else None,
        "responses_with_reasoning_count": len(details), "responses_with_usage": len(usages),
        "reported_cost_partial_usd": sum(u.get("cost", 0) for u in usages),
        "unreported_billing": "NOT_MEASURED" if len(usages) < len(sent) else "ALL_SENT_USAGE_REPORTED",
        "charged_ledger_tokens": sum((r["budget"] or {}).get("charged_chat_tokens", 0) for r in rows),
        "validated_statuses": dict(Counter(r["prediction"] for r in valid)),
        "developer_label_confusion": [{"expected": a, "predicted": b, "count": n} for (a, b), n in Counter((r["expected_status"], r["prediction"]) for r in checked).items()],
        "false_sufficient_observed": sum(r["expected_status"] == "INSUFFICIENT" and r["prediction"] == "SUFFICIENT" for r in checked),
        "false_insufficient_observed": sum(r["expected_status"] == "SUFFICIENT" and r["prediction"] == "INSUFFICIENT" for r in checked),
        "semantic_accuracy_scope": "Developer literal checks only; strict DTO validity is not semantic correctness; drafts excluded; no generalized accuracy",
    }


def selected_cases(args, *, prompt_system=SYSTEM):
    plan = read(args.plan)
    path = args.plan.parent / "frozen-sidecar-v1.json"
    if hashlib.sha256(path.read_bytes()).hexdigest() != plan["frozen_sidecar_sha256"]:
        raise ValueError("frozen sidecar changed")
    if hashlib.sha256(prompt_system.encode()).hexdigest() != plan["prompt_sha256"]:
        raise ValueError("frozen prompt changed")
    ids = plan[args.cohort + "_ids"]
    lookup = {c["id"]: c for c in read(path)["cases"]}
    return plan, [lookup[id] for id in ids]


async def run(args, *, injected_http=None, injected_config=None, adapter_type=EvidenceReflectionModelAdapter, prompt_system=SYSTEM):
    if not args.live and injected_http is None:
        raise ValueError("--live and fresh user-authorized limits are required; no automatic baseline rerun")
    if type(args.max_calls) is not int or not 1 <= args.max_calls <= 15:
        raise ValueError("explicit max_calls must be in [1,15]")
    if type(args.max_total_tokens) is not int or not 1024 <= args.max_total_tokens <= 50000:
        raise ValueError("explicit token ceiling must be in [1024,50000]")
    if args.timeout not in {5, 20} or args.effort not in {"none", "low"}:
        raise ValueError("unsupported experiment setting")
    settings = ModelRequestSettings(reasoning_effort=args.effort, max_tokens=1024)
    plan, cases = selected_cases(args, prompt_system=prompt_system)
    if args.cohort != "calibration":
        proof = getattr(args, "calibration_result", None)
        if proof is None:
            raise ValueError("holdout/draft requires a completed calibration proof")
        summary = read(proof / "summary.json")
        controls = read(proof / "configuration.json")
        if (not summary.get("reasoning_effect_verified_zero") or summary.get("strict_dto_valid", 0) < 4
                or controls.get("model_settings") != settings.request_fields()
                or controls.get("frozen_sidecar_sha256") != plan["frozen_sidecar_sha256"]):
            raise ValueError("calibration has not verified this setting and sufficient DTO availability")
    # Cohorts are separate immutable runs; preserve prior artifacts.
    args.output.mkdir(parents=True, exist_ok=False)
    sources = load_chat_environment(args.env_file)
    import os
    os.environ["DOVIDEO_MODEL_PROVIDER_TAG"] = "siliconflow/fp8"
    config = injected_config or ProviderConfig.from_environment(required=True)
    if config.transport != "openrouter" or config.model != plan["model"] or config.provider_only != ("siliconflow/fp8",):
        raise ValueError("same configured model/pinned provider required")
    config = replace(config, max_attempts=1, timeout_seconds=args.timeout)
    http = injected_http or ExperimentHttp(config.api_key)
    adapter = adapter_type(OpenAICompatibleChatClient(config, request_settings=settings, client=http))
    save(args.output / "configuration.json", {
        "provider": public_config(config), "sources": sources,
        "model_settings": settings.request_fields(), "timeout_seconds": args.timeout,
        "cohort": args.cohort, "max_calls": args.max_calls, "campaign_token_ceiling": args.max_total_tokens,
        "prompt_sha256": plan["prompt_sha256"], "frozen_sidecar_sha256": plan["frozen_sidecar_sha256"],
        "mode": "OFFLINE_DIAGNOSTIC_NO_DOWNSTREAM_ANSWER", "production_modified": False,
        "original_request_deadline_seconds": 60, "note": "Not a production Shadow gate or R4 run",
    })
    rows, attempts, charged, streak, stop = [], 0, 0, 0, None
    for case in cases:
        inp = case["input"]
        candidates = tuple(inp["candidates"])
        messages = adapter.messages(inp["question"], candidates, mixed=inp["mixed"])
        remaining = args.max_total_tokens - charged
        before = len(http.records)
        started = time.perf_counter()
        assessment = EvidenceAssessment(None)
        outcome, terminal, usage, entry_time, after_time = stop or "MODEL_UNAVAILABLE", None, None, None, None
        if not stop:
            with capture_follow_up_budget() as budgets:
                try:
                    with AgentExecutionBudget.open(60000), follow_up_budget_scope(FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED, max(1, remaining))):
                        entry_time = AgentExecutionBudget.remaining_seconds()
                        if not candidates:
                            outcome = "NO_CANDIDATES"
                        elif not optional_model_headroom(messages, "EVIDENCE_REFLECTION", 1024, 0):
                            outcome = "SKIPPED_BUDGET"
                        else:
                            async with asyncio.timeout(args.timeout):
                                proposal = await adapter.assess(inp["question"], candidates, mixed=inp["mixed"])
                            assessment = EvidenceAssessment(validate_assessment(proposal, inp["question"], candidates))
                            outcome = "ASSESSED"
                        after_time = AgentExecutionBudget.remaining_seconds()
                except (BudgetExceededError, DeadlineExceededError) as exc:
                    terminal, outcome = type(exc).__name__, "TERMINAL_BUDGET_OR_DEADLINE"
                except TimeoutError:
                    outcome = "TIMEOUT"
                except (ValueError, TypeError):
                    outcome = "INVALID_RESPONSE"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    outcome = "PROVIDER_FAILURE"
            usage = budgets[0] if budgets else None
        records = http.records[before:]
        attempts += len(records)
        charged += usage["charged_chat_tokens"] if usage else 0
        row = {
            "id": case["id"], "split": case["split"], "label_origin": case["label_origin"],
            "expected_status": case["expected_status"], "expected_gap": case["expected_gap"],
            "input_sha256": digest(inp), "review": case["review"],
            "outcome": outcome, "prediction": assessment.status,
            "assessment": assessment.suggestion.model_dump() if assessment.suggestion else None,
            "terminal": terminal, "request_remaining_seconds_at_entry": entry_time,
            "request_remaining_seconds_after": after_time, "latency_ms": (time.perf_counter() - started) * 1000,
            "estimated_input_tokens": estimate_chat_input(messages), "output_reserve": 1024,
            "campaign_tokens_remaining_before": remaining, "budget": usage,
            "http_attempts": len(records), "http": records,
        }
        rows.append(row)
        save(args.output / "results.json", rows)
        print(case["id"], classify(row), assessment.status, f"{row['latency_ms']:.0f}ms", flush=True)
        streak = streak + 1 if records and outcome != "ASSESSED" else 0
        if any(h.get("status_code") is not None and h["status_code"] != 200 for h in records):
            stop = "SKIPPED_PROVIDER_REJECTION"
        elif any(h.get("returned_provider") and h["returned_provider"].lower() != "siliconflow" for h in records):
            stop = "SKIPPED_PROVIDER_IDENTITY_MISMATCH"
        elif args.effort == "none" and any(h.get("reasoning_tokens") is not None and h["reasoning_tokens"] != 0 for h in records):
            stop = "SKIPPED_REASONING_EFFECT_MISMATCH"
        elif streak >= 2:
            stop = "SKIPPED_REPEATED_UNAVAILABLE"
        if terminal or charged >= args.max_total_tokens:
            stop = "SKIPPED_CAMPAIGN_BUDGET"
        elif attempts >= args.max_calls:
            stop = "SKIPPED_CALL_LIMIT"
    summary = summarize(rows)
    summary["stop_reason"] = stop
    summary["reasoning_effect_verified_zero"] = bool(attempts and summary["responses_with_reasoning_count"] == attempts and summary["numeric_reasoning_tokens"] == 0)
    save(args.output / "summary.json", summary)
    return summary


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--live", action="store_true")
    p.add_argument("--plan", type=Path, default=ROOT / "docs/agentic-rag/r36-validation-v1/experiment-plan.json")
    p.add_argument("--cohort", required=True, choices=["calibration", "holdout", "draft"])
    p.add_argument("--calibration-result", type=Path, help="Verified calibration directory; required before holdout/draft")
    p.add_argument("--effort", required=True, choices=["none", "low"])
    p.add_argument("--timeout", type=int, choices=[5, 20], default=20)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--env-file", action="append", type=Path, default=[])
    p.add_argument("--max-calls", type=int, required=True)
    p.add_argument("--max-total-tokens", type=int, required=True)
    return p


if __name__ == "__main__":
    asyncio.run(run(parser().parse_args()))
