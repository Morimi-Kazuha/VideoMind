"""Contract regressions using fixture responses; these do not measure LLM accuracy."""
from argparse import Namespace
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from prepare_r37_reflection_hardening import ReplayChat
from run_r37_reflection_hardening import run
from reflection_prompt_snapshots import R3_SYSTEM, HistoricalReflectionAdapter
from dovideo.application.evidence_reflection import AssessmentSuggestion, EvidenceReflection, ReflectionMode, capture_evidence_reflection
from dovideo.application.follow_up_budget import follow_up_budget_scope, FollowUpBudgetSettings, FollowUpBudgetMode
from dovideo.infrastructure.providers.evidence_reflection import EvidenceReflectionModelAdapter, assessment_output_schema, SYSTEM
from test_r36_reflection_optimization_tools import instrument, response, config, UNKNOWN

OUT = ROOT / "tests/fixtures/agentic-rag/r37"
CASES = {c["id"]: c for c in json.loads((OUT / "frozen-sidecar-v1.json").read_text(encoding="utf-8"))["cases"]}


def sufficient(span, keys=("video_statement",)):
    return {"status": "SUFFICIENT", "reason_code": "SEMANTIC_COVERAGE",
        "covered_requirements": [{"requirement_key": k, "question_span": span, "candidate_references": [0]} for k in keys],
        "missing_requirements": [], "candidate_references": [0]}


def missing(span):
    return {"status": "INSUFFICIENT", "reason_code": "EVIDENCE_GAPS", "covered_requirements": [],
        "missing_requirements": [{"requirement_key": "video_step", "question_span": span,
            "candidate_references": [], "reason_code": "PARTIAL_FACT_COVERAGE"}], "candidate_references": []}


async def assess(case, raw):
    inp = case["input"]
    chat = ReplayChat(json.dumps(raw, ensure_ascii=False))
    result = await EvidenceReflectionModelAdapter(chat).assess(inp["question"], tuple(inp["candidates"]), mixed=inp["mixed"])
    assert chat.calls == 1
    return result


def test_wire_requires_all_fields_without_mutating_dto_schema():
    before = AssessmentSuggestion.model_json_schema()
    wire = assessment_output_schema()
    assert set(wire["required"]) == set(wire["properties"])
    for shape in wire["$defs"].values():
        if shape.get("type") == "object":
            assert set(shape["required"]) == set(shape["properties"])
    assert AssessmentSuggestion.model_json_schema() == before
    assert "candidate_references" not in before["required"]


@pytest.mark.asyncio
@pytest.mark.parametrize("id", ["mixed", "h-mixed-video-boundary", "d-mixed-incomplete"])
async def test_two_known_mixed_failures_and_reverse_case_keep_video_only_contract(id):
    case = CASES[id]
    inp = case["input"]
    payload = json.loads(EvidenceReflectionModelAdapter.messages(inp["question"], tuple(inp["candidates"]), mixed=True)[1]["content"])
    assert payload["route"] == "VIDEO_PLUS_KNOWLEDGE"
    assert set(payload) == {"question", "scope", "route", "retrievedSourceCandidates"}
    assert "assess ONLY whether the candidates reconstruct the video" in SYSTEM
    assert "Do not make all MIXED cases sufficient" in SYSTEM
    assert "NOT to video requirements" in SYSTEM
    span = inp["question"]
    raw = missing(span) if id == "d-mixed-incomplete" else sufficient(span)
    assert (await assess(case, raw)).status == case["expected_status"]


@pytest.mark.asyncio
async def test_shared_original_comparison_span_is_legal_and_invented_one_still_rejected():
    case = CASES["h-comparison-complete"]
    original = case["input"]["question"]
    result = await assess(case, sufficient(original, ("memory_a", "memory_b")))
    assert len(result.covered_requirements) == 2
    raw = sufficient("甲的内存开销")
    with pytest.raises(ValueError, match="anchored"):
        await assess(case, raw)


@pytest.mark.asyncio
async def test_tc17_missing_summary_reference_is_rejected_without_repair():
    case = CASES["tc-17-partial-cache"]
    raw = missing(case["input"]["question"])
    raw["covered_requirements"] = [{"requirement_key": "first_slide", "question_span": "3n+3", "candidate_references": [0]}]
    del raw["candidate_references"]
    with pytest.raises(ValueError, match="union mismatch"):
        await assess(case, raw)
    raw["candidate_references"] = [0]
    assert (await assess(case, raw)).candidate_references == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [True, 1, 8, -1])
async def test_illegal_indices_end_as_unknown_without_second_grader_call(bad):
    case = CASES["mixed"]
    inp = case["input"]
    raw = sufficient(inp["question"])
    raw["candidate_references"] = [bad]
    raw["covered_requirements"][0]["candidate_references"] = [bad]
    chat = ReplayChat(json.dumps(raw))
    adapter = EvidenceReflectionModelAdapter(chat)
    with capture_evidence_reflection() as observations, follow_up_budget_scope(FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED, 20000)):
        result = await EvidenceReflection(adapter, ReflectionMode.SHADOW).observe(inp["question"], tuple(inp["candidates"]),
            mixed=True, messages=adapter.messages(inp["question"], tuple(inp["candidates"]), mixed=True))
    assert result is None and chat.calls == 1
    assert observations[0].assessment.status == "UNKNOWN"
    assert observations[0].outcome == "INVALID_RESPONSE"
    # SHADOW's observation has no free query/tool authority.
    assert not hasattr(observations[0].assessment, "query")


def test_historical_prompt_and_wire_hashes_remain_exact():
    old = json.loads((ROOT / "tests/fixtures/agentic-rag/r36/experiment-plan.json").read_text())
    assert hashlib.sha256(R3_SYSTEM.encode()).hexdigest() == old["prompt_sha256"]
    replay = json.loads((OUT / "historical-replay.json").read_text(encoding="utf-8"))
    assert replay["strict_valid"] == 13 and replay["invalid"] == 2 and replay["historical_wire_hashes_match"]
    assert {r["id"] for r in replay["rows"] if r["error"]} == {"h-comparison-complete", "tc-17-partial-cache"}
    assert all(r["network_calls"] == 0 for r in replay["rows"])
    inp = CASES["mixed"]["input"]
    assert HistoricalReflectionAdapter.messages(inp["question"], tuple(inp["candidates"]), mixed=True)[1] == EvidenceReflectionModelAdapter.messages(inp["question"], tuple(inp["candidates"]), mixed=True)[1]


def args(tmp_path, **changes):
    values = dict(live=False, effort="none", timeout=20, cohort="calibration", max_calls=15,
        max_total_tokens=50000, plan=OUT / "experiment-plan.json", output=tmp_path / "dev", env_file=[], calibration_result=None)
    values.update(changes)
    return Namespace(**values)


@pytest.mark.asyncio
async def test_r37_no_live_intent_no_output_or_http(tmp_path):
    a = args(tmp_path)
    with pytest.raises(ValueError, match="fresh user-authorized"):
        await run(a)
    assert not a.output.exists()


@pytest.mark.asyncio
async def test_both_cohorts_share_15_calls_and_50000_token_ceiling(tmp_path):
    http = instrument(response(json.dumps(UNKNOWN)))
    dev = await run(args(tmp_path), injected_http=http, injected_config=config())
    assert dev["sent_attempts"] == 9 and dev["charged_ledger_tokens"] == 10800
    hold = instrument(response(json.dumps(UNKNOWN)))
    summary = await run(args(tmp_path, cohort="holdout", output=tmp_path / "hold", calibration_result=tmp_path / "dev"), injected_http=hold, injected_config=config())
    assert summary["sent_attempts"] == 6
    controls = json.loads((tmp_path / "hold/configuration.json").read_text())
    assert controls["max_calls"] == 6 and controls["campaign_token_ceiling"] == 39200
    assert len(http.records) + len(hold.records) == 15
    assert "PRIVATE THOUGHTS" not in json.dumps(hold.records)


@pytest.mark.asyncio
async def test_full_schema_change_detected_before_http(tmp_path, monkeypatch):
    monkeypatch.setattr("run_r37_reflection_hardening.EvidenceReflectionModelAdapter.messages", lambda *a, **kw: ({"role": "system", "content": "changed schema"},))
    http = instrument(response(json.dumps(UNKNOWN)))
    with pytest.raises(ValueError, match="wire schema"):
        await run(args(tmp_path), injected_http=http, injected_config=config())
    assert not http.records and not (tmp_path / "dev").exists()


def test_new_holdout_topics_labels_and_tails_cannot_enter_messages():
    plan = json.loads((OUT / "experiment-plan.json").read_text())
    assert len(plan["holdout_ids"]) == 6 and not set(plan["holdout_ids"]) & set(plan["calibration_ids"])
    for id in plan["holdout_ids"]:
        case = CASES[id]
        inp = case["input"]
        messages = EvidenceReflectionModelAdapter.messages(inp["question"], tuple(inp["candidates"]), mixed=inp["mixed"])
        assert "Redis" not in messages[1]["content"] and "时间复杂度" not in messages[1]["content"]
        assert "expected_status" not in messages[1]["content"] and "necessary_facts" not in messages[1]["content"]
        if case.get("raw_segment_tail_only"):
            assert case["raw_segment_tail_only"] not in messages[1]["content"]
