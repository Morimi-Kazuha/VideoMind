"""Deterministic experiment-contract tests; all HTTP is replaced, no API use."""
import asyncio
from argparse import Namespace
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from run_r36_reflection_optimization import ExperimentHttp, run, summarize, complete_json, selected_cases
from dovideo.infrastructure.providers.config import ModelRequestSettings, ProviderConfig, ProviderConfigurationError
from dovideo.infrastructure.providers.http import ProviderHttpResponse
from dovideo.infrastructure.providers.model import OpenAICompatibleChatClient
from dovideo.infrastructure.providers.evidence_reflection import EvidenceReflectionModelAdapter
from dovideo.application.evidence_reflection import EvidenceReflection, ReflectionMode, capture_evidence_reflection
from dovideo.application.follow_up_budget import capture_follow_up_budget, follow_up_budget_scope, FollowUpBudgetSettings, FollowUpBudgetMode


UNKNOWN = {"status": "UNKNOWN", "reason_code": "UNCERTAIN_SEMANTIC_SUPPORT", "covered_requirements": [], "missing_requirements": [], "candidate_references": []}


def config():
    return ProviderConfig(base_url="https://openrouter.ai/api/v1", model="deepseek/deepseek-v4.1-flash",
                          api_key="fixture-secret", transport="openrouter", provider_only=("siliconflow/fp8",),
                          provider_data_collection="deny", provider_zdr=True)


def response(content=None, *, reasoning=0, finish="stop", status=200):
    return ProviderHttpResponse(status, {"model": config().model, "provider": "SiliconFlow",
        "choices": [{"finish_reason": finish, "message": {"content": content, "reasoning": "PRIVATE THOUGHTS", "reasoning_details": ["PRIVATE THOUGHTS"]}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200,
                  "completion_tokens_details": {"reasoning_tokens": reasoning}}})


class Http:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def post(self, url, **kwargs):
        self.calls.append(kwargs["json"])
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def instrument(result):
    http = ExperimentHttp("fixture-secret")
    http.client = Http(result)
    return http


def args(tmp_path, **changes):
    base = dict(live=False, max_calls=6, max_total_tokens=20000, timeout=20, effort="none",
                plan=ROOT / "tests/fixtures/agentic-rag/r36/experiment-plan.json",
                cohort="calibration", output=tmp_path / "result", env_file=[])
    base.update(changes)
    return Namespace(**base)


@pytest.mark.asyncio
async def test_live_without_fresh_explicit_intent_does_not_load_config_or_create_output(tmp_path):
    a = args(tmp_path)
    with pytest.raises(ValueError, match="fresh user-authorized"):
        await run(a)
    assert not a.output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("max_calls", 0), ("max_calls", 16), ("max_total_tokens", True), ("effort", "minimal"), ("timeout", 60)])
async def test_invalid_limits_fail_before_network(tmp_path, field, value):
    a = args(tmp_path, **{field: value})
    http = instrument(response(json.dumps(UNKNOWN)))
    with pytest.raises(ValueError):
        await run(a, injected_http=http, injected_config=config())
    assert not a.output.exists() and not http.records


@pytest.mark.asyncio
async def test_none_wire_usage_and_no_reasoning_text_with_real_budgeted_adapter(tmp_path):
    http = instrument(response(json.dumps(UNKNOWN)))
    result = await run(args(tmp_path), injected_http=http, injected_config=config())
    assert result["strict_dto_valid"] == 6 and result["reasoning_effect_verified_zero"]
    assert result["charged_ledger_tokens"] == 7200
    rows = json.loads((tmp_path / "result/results.json").read_text(encoding="utf-8"))
    for row in rows:
        admission = next(e for e in row["budget"]["events"] if e["kind"] == "admission")
        assert admission["output_reserve"] == row["output_reserve"] == 1024
        assert row["http"][0]["sent_parameters"]["reasoning_effort"] == "none"
        assert row["assessment"]["status"] == "UNKNOWN"
    saved = json.dumps(http.records)
    assert "PRIVATE THOUGHTS" not in saved and "fixture-secret" not in saved
    assert all(h["sent_parameters"]["provider"]["allow_fallbacks"] is False for h in http.records)


@pytest.mark.asyncio
async def test_parameter_rejection_stops_no_retry_or_default_effort_fallback(tmp_path):
    http = instrument(response(None, status=400))
    result = await run(args(tmp_path), injected_http=http, injected_config=config())
    assert len(http.client.calls) == 1
    assert result["outcomes"]["PROVIDER_PARAMETER_REJECTION"] == 1
    assert result["outcomes"]["SKIPPED_PROVIDER_REJECTION"] == 5
    assert result["strict_dto_valid"] == 0


@pytest.mark.asyncio
async def test_reasoning_nonzero_under_none_stops_instead_of_claiming_effect(tmp_path):
    http = instrument(response(json.dumps(UNKNOWN), reasoning=100))
    result = await run(args(tmp_path), injected_http=http, injected_config=config())
    assert len(http.records) == 1 and not result["reasoning_effect_verified_zero"]
    assert result["outcomes"]["SKIPPED_REASONING_EFFECT_MISMATCH"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("content,finish", [(None, "length"), ('{"status":"SUFFICIENT"', "length"), ('{"status":"UNKNOWN","status":"SUFFICIENT"}', "stop"), (json.dumps({**UNKNOWN, "evidence_scope": "TRUSTED"}), "stop")])
async def test_null_truncated_duplicate_or_extra_field_is_never_legal(tmp_path, content, finish):
    http = instrument(response(content, finish=finish))
    result = await run(args(tmp_path), injected_http=http, injected_config=config())
    assert result["strict_dto_valid"] == 0 and len(http.records) == 2
    rows = json.loads((tmp_path / "result/results.json").read_text(encoding="utf-8"))
    assert all(r["prediction"] == "UNKNOWN" and r["assessment"] is None for r in rows)
    assert result["outcomes"]["SKIPPED_REPEATED_UNAVAILABLE"] == 4


@pytest.mark.asyncio
async def test_timeout_estimated_charge_call_limit_and_cancel_propagation(tmp_path):
    http = instrument(TimeoutError())
    result = await run(args(tmp_path, max_calls=1), injected_http=http, injected_config=config())
    assert result["timeout_rate_all_sent"] == 1 and result["charged_ledger_tokens"] > 1024
    assert len(http.records) == 1 and result["completed_http_responses"] == 0
    cancel_http = instrument(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await run(args(tmp_path, output=tmp_path / "cancel"), injected_http=cancel_http, injected_config=config())
    assert len(cancel_http.records) == 1


@pytest.mark.asyncio
async def test_budget_refusal_precedes_http_and_sidecar_labels_are_not_in_prompt(tmp_path):
    http = instrument(response(json.dumps(UNKNOWN)))
    result = await run(args(tmp_path, max_total_tokens=1024), injected_http=http, injected_config=config())
    assert not http.records and result["charged_ledger_tokens"] == 0
    _, cases = selected_cases(args(tmp_path))
    inp = cases[0]["input"]
    payload = json.loads(EvidenceReflectionModelAdapter.messages(inp["question"], tuple(inp["candidates"]), mixed=inp["mixed"])[1]["content"])
    assert set(payload) == {"question", "scope", "route", "retrievedSourceCandidates"}
    assert "expected_status" not in payload and "necessary_facts" not in payload


@pytest.mark.asyncio
async def test_holdout_requires_matching_verified_calibration_and_preserves_freeze(tmp_path):
    http = instrument(response(json.dumps(UNKNOWN)))
    a = args(tmp_path, cohort="holdout")
    with pytest.raises(ValueError, match="calibration proof"):
        await run(a, injected_http=http, injected_config=config())
    assert not a.output.exists()
    await run(args(tmp_path, output=tmp_path / "cal"), injected_http=http, injected_config=config())
    hold = instrument(response(json.dumps(UNKNOWN)))
    result = await run(args(tmp_path, cohort="holdout", calibration_result=tmp_path / "cal"), injected_http=hold, injected_config=config())
    assert result["sent_attempts"] == 6
    assert len({h["sent_parameters"]["messages_sha256"] for h in hold.records}) == 6


@pytest.mark.asyncio
async def test_original_stage_defaults_unchanged_and_off_never_calls():
    http = Http(response(json.dumps(UNKNOWN)))
    chat = OpenAICompatibleChatClient(config(), client=http)
    expected = {"EVIDENCE_REFLECTION": 1024, "FOLLOW_UP": 4096, "DIRECT_FOLLOW_UP": 2048,
                "MIXED_KNOWLEDGE": 2048, "QUERY_REWRITE": 512, "ADAPTIVE_RETRIEVAL_PLANNER": 1024}
    for stage, cap in expected.items():
        await chat.complete([{"role": "user", "content": "Return JSON"}], stage=stage)
        assert http.calls[-1]["max_tokens"] == cap and "reasoning_effort" not in http.calls[-1]
    with capture_evidence_reflection() as captured:
        await EvidenceReflection(EvidenceReflectionModelAdapter(chat), ReflectionMode.OFF).observe("q", ())
    assert len(http.calls) == len(expected) and not captured
    assert ModelRequestSettings().request_fields() == {}
