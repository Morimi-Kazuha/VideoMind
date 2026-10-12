"""R3.7 bounded diagnostic; fresh approval is required before --live.

Historical R3.6 keeps its original instructions. This runner verifies the full
current system message and subtracts development spend before the holdout.
"""
import asyncio
import hashlib

from run_r36_reflection_optimization import parser, read, run as run_historical, selected_cases
from dovideo.infrastructure.providers.evidence_reflection import EvidenceReflectionModelAdapter, SYSTEM


async def run(args, *, injected_http=None, injected_config=None):
    if args.effort != "none" or args.timeout != 20 or args.cohort not in {"calibration", "holdout"}:
        raise ValueError("R3.7 fixes none/1024/20s and two frozen cohorts")
    plan, cases = selected_cases(args, prompt_system=SYSTEM)
    system = EvidenceReflectionModelAdapter.messages("", ())[0]["content"]
    if hashlib.sha256(system.encode()).hexdigest() != plan["full_system_sha256"]:
        raise ValueError("frozen wire schema or system changed")
    if args.max_calls > plan["max_proposed_attempts"] or args.max_total_tokens > plan["proposed_token_ceiling"]:
        raise ValueError("limits exceed frozen campaign")
    from run_r36_reflection_optimization import digest
    planned = {r["id"]: r for r in plan["planned_cases"]}
    for case in cases:
        inp = case["input"]
        messages = EvidenceReflectionModelAdapter.messages(inp["question"], tuple(inp["candidates"]), mixed=inp["mixed"])
        if digest(messages) != planned[case["id"]]["messages_sha256"]:
            raise ValueError("frozen model input changed")
    if args.cohort == "holdout":
        if args.calibration_result is None:
            raise ValueError("holdout requires calibration proof")
        summary = read(args.calibration_result / "summary.json")
        controls = read(args.calibration_result / "configuration.json")
        if controls["max_calls"] != args.max_calls or controls["campaign_token_ceiling"] != args.max_total_tokens:
            raise ValueError("campaign limits must match development authorization")
        # These limits are campaign-wide, never fresh per cohort.
        args.max_calls -= summary["sent_attempts"]
        args.max_total_tokens -= summary["charged_ledger_tokens"]
    return await run_historical(args, injected_http=injected_http, injected_config=injected_config,
                                adapter_type=EvidenceReflectionModelAdapter, prompt_system=SYSTEM)


if __name__ == "__main__":
    p = parser()
    p.set_defaults(plan=__import__("pathlib").Path(__file__).resolve().parents[1] /
                   "docs/agentic-rag/r37-validation-v1/experiment-plan.json")
    asyncio.run(run(p.parse_args()))
