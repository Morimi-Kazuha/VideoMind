"""Actual admission functions with synthetic Usage/time, never production rate."""
import argparse
import asyncio
import json
from pathlib import Path

from r4_validation_support import harness, insufficient
from dovideo.application.evidence_reflection import capture_evidence_reflection
from dovideo.application.retrieval_budget import capture_retrieval_budget
from dovideo.application.retrieval_repair import capture_feedback_retrieval
from dovideo.application.execution_budget import AgentExecutionBudget
from dovideo.domain import AnalysisMode


async def run(output):
    if output.exists():
        raise FileExistsError(output)
    rows = []
    cases = (
        ("video_budget_blocks_repair", False, 100000, 0, 0, "ASSESSED", "SKIPPED_BUDGET"),
        ("video_high_headroom", False, 150000, 0, 0, "ASSESSED", "EVIDENCE_ADDED"),
        ("mixed_high_headroom", True, 150000, 0, 0, "ASSESSED", "EVIDENCE_ADDED"),
        ("mixed_gate_after_grader", True, 150000, 4.5, 6, "ASSESSED", "SKIPPED_TIME"),
        ("video_gate_after_grader", False, 150000, 18, 21, "ASSESSED", "SKIPPED_TIME"),
        ("mixed_budget_blocks_reflection", True, 100000, 0, 0, "SKIPPED_BUDGET", "NOT_ELIGIBLE"),
        ("mixed_time_blocks_reflection", True, 150000, 6, 6, "SKIPPED_TIME", "NOT_ELIGIBLE"),
    )
    for name, mixed, limit, pre, post, expected_r3, expected_r4 in cases:
        question = "作者描述的缓存命中、未命中及回填流程合理吗？" if mixed else "作者为什么选择 B？"
        texts = ("作者命中返回，未命中查数据库。", "设置界面。", "操作说明。", "作者查询数据库后回填缓存。") if mixed else None
        h = await harness(question, insufficient("回填流程" if mixed else question), texts=texts, budget_limit=limit, attempts=3)
        http, clock = h[5], [0.0]
        original_post = http.post
        async def timed(url, **kw):
            result = await original_post(url, **kw)
            system = kw["json"]["messages"][0]["content"]
            if system == "fixture planner" and clock[0] == 0:
                clock[0] = pre
            elif "Grade semantic sufficiency" in system:
                clock[0] = post
            return result
        http.post = timed
        original_clock = AgentExecutionBudget._default_monotonic
        AgentExecutionBudget._default_monotonic = lambda: clock[0]
        try:
            with capture_evidence_reflection() as reflection, capture_feedback_retrieval() as repair, capture_retrieval_budget() as budget:
                await h[0].answer(42, question, "视频学习", AnalysisMode.LEARNING)
        finally:
            AgentExecutionBudget._default_monotonic = original_clock
        assert reflection[0].outcome == expected_r3 and repair[0].state == expected_r4
        rows.append({"id": name, "route": "MIXED" if mixed else "VIDEO", "token_ceiling": limit,
            "configured_provider_max_attempts": 3, "synthetic_elapsed_before_reflection_seconds": pre,
            "synthetic_elapsed_after_reflection_seconds": post, "r3_outcome": reflection[0].outcome,
            "r4_state": repair[0].state, "budget": budget[0], "actual_fixture_http_counts": dict(h[4])})
    output.write_text(json.dumps({"kind": "DETERMINISTIC_ADMISSION_SCENARIOS_NOT_PRODUCTION_STATISTICS",
        "rows": rows, "unchanged_global_deadline_seconds": 60, "unchanged_r3_local_timeout_seconds": 5,
        "unchanged_time_gate_seconds": {"VIDEO": 40, "MIXED": 55},
        "production_admission_rate": "NOT_MEASURED", "production_skip_ratio": "NOT_MEASURED",
        "real_downstream_usage": "NOT_MEASURED", "new_paid_calls": 0}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Validated", len(rows), "synthetic admission scenarios")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output))
