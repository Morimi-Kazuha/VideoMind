"""R3 execution and hostile-boundary tests; fixtures are not semantic accuracy."""
import asyncio
import json
from copy import deepcopy
from uuid import uuid4
import pytest
from pydantic import ValidationError
from test_r2c_mixed_follow_up import mixed_harness, MIXED_Q
from test_r2b_direct_follow_up import QUESTION
from test_m1_conversation_memory import ask
from dovideo.application.evidence_reflection import (
    EvidenceReflection, ReflectionMode, AssessmentSuggestion, validate_assessment,
    capture_evidence_reflection,
)
from dovideo.application.follow_up_budget import FollowUpBudgetSettings, FollowUpBudgetMode, capture_follow_up_budget
from dovideo.application.errors import BudgetExceededError, DeadlineExceededError
from dovideo.application.execution_budget import AgentExecutionBudget
from dovideo.application.evidence_projection import source_payload
from dovideo.infrastructure.evidence_reflection import reflection_mode_from_environment
from dovideo.infrastructure.providers.evidence_reflection import EvidenceReflectionModelAdapter
from dovideo.infrastructure.providers import ProviderHttpResponse
from dovideo.domain import VideoEvidenceHit, AnalysisMode

Q = "作者为什么选择 Redis？"


def proposal(question=Q, status="INSUFFICIENT", gap="MISSING_CAUSAL_EVIDENCE"):
    req = {"requirement_key": "question_requirement", "question_span": question, "candidate_references": [0]}
    return {"status": status, "reason_code": {"SUFFICIENT": "SEMANTIC_COVERAGE", "INSUFFICIENT": "EVIDENCE_GAPS", "UNKNOWN": "UNCERTAIN_SEMANTIC_SUPPORT"}[status],
        "covered_requirements": [req] if status == "SUFFICIENT" else [],
        "missing_requirements": [{**req, "reason_code": gap}] if status == "INSUFFICIENT" else [],
        "candidate_references": [0] if status != "UNKNOWN" else []}


def reflected(mode=ReflectionMode.SHADOW, payload=None, error=None, status=200, attempts=1):
    h, http = mixed_harness(attempts=attempts)
    old = http.post
    calls = []
    async def post(url, **kw):
        if "Grade semantic sufficiency" not in kw["json"]["messages"][0]["content"]:
            return await old(url, **kw)
        calls.append(kw["json"])
        if error: raise error
        inp = json.loads(kw["json"]["messages"][-1]["content"])
        raw = proposal(inp["question"]) if payload is None else payload
        return ProviderHttpResponse(status, {"choices": [{"message": {"content": raw if isinstance(raw,str) else json.dumps(raw)}}], "usage": {"total_tokens": 20}})
    http.post = post
    chat = h[0]._model._chat
    h[0]._reflection = EvidenceReflection(EvidenceReflectionModelAdapter(chat), mode)
    h[0]._budget_settings = FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED, 500000)
    return h, http, calls


def test_modes():
    assert reflection_mode_from_environment({}) == ReflectionMode.OFF
    assert reflection_mode_from_environment({"DOVIDEO_EVIDENCE_REFLECTION_MODE":" shadow "}) == ReflectionMode.SHADOW
    for raw in ["ENABLED", "", None, True]:
        with pytest.raises(ValueError): reflection_mode_from_environment({"DOVIDEO_EVIDENCE_REFLECTION_MODE":raw})


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [Q, MIXED_Q, QUESTION])
async def test_off_shadow_same_answers_memory_retrieval_and_payload(question):
    states, answers, payloads = [], [], []
    for mode in [ReflectionMode.OFF, ReflectionMode.SHADOW]:
        h,http,calls = reflected(mode)
        rid = str(uuid4())
        with capture_evidence_reflection() as observations, capture_follow_up_budget() as usage:
            answers.append(await ask(h, question, rid))
            assert await ask(h,question,rid) == answers[-1]
        states.append((await h[4].store.load(h[5])).turns[0].model_dump(exclude={"turn_id", "created_at_ms"}))
        payloads.append([p for _,p in http.calls])
        assert h[2].queries == ([] if question == QUESTION else [question])
        expected = mode == ReflectionMode.SHADOW and question != QUESTION
        assert len(calls) == len(observations) == int(expected)
        assert usage[0]["stages"].get("EVIDENCE_REFLECTION",0) == int(expected)
        if expected:
            assert calls[0]["max_tokens"] == 1024
            assert observations[0].assessment.status == "INSUFFICIENT"
            assert observations[0].assessment.suggestion.missing_requirements[0].question_span == question
            inp = json.loads(calls[0]["messages"][-1]["content"])
            source_input = json.loads(http.calls[0][1]["messages"][-1]["content"].split("Input as JSON:\n")[-1])
            assert inp["retrievedSourceCandidates"] == source_input["retrievedSourceCandidates"]
            assert inp["route"] == ("VIDEO_PLUS_KNOWLEDGE" if question == MIXED_Q else "VIDEO_RAG")
    assert answers[0] == answers[1] and payloads[0] == payloads[1]
    # Timing is deliberately not an answer/Memory field comparison.
    for state in states: state.pop("created_at",None)
    assert states[0] == states[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,wall,outcome", [(10000,60,"SKIPPED_BUDGET"),(500000,10,"SKIPPED_TIME"),(None,60,"SKIPPED_BUDGET")])
async def test_headroom_skips_optional_work(limit,wall,outcome):
    h,_,calls=reflected()
    h[0]._max_wall_seconds=wall
    h[0]._budget_settings=FollowUpBudgetSettings(FollowUpBudgetMode.SHADOW,limit)
    with capture_evidence_reflection() as obs: await ask(h,Q)
    assert not calls and obs[0].outcome==outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("error,outcome", [(TimeoutError(),"TIMEOUT"),(OSError(),"PROVIDER_FAILURE")])
async def test_local_failures_continue_original_answer(error,outcome):
    h,_,calls=reflected(error=error)
    with capture_evidence_reflection() as obs: answer=await ask(h,Q)
    assert answer and len(calls)==1 and obs[0].outcome==outcome and obs[0].assessment.status=="UNKNOWN"


@pytest.mark.asyncio
async def test_transient_failure_has_one_attempt_despite_original_retry_configuration():
    h,http,calls=reflected(status=503,attempts=3)
    with capture_evidence_reflection() as obs: await ask(h,Q)
    assert len(calls)==1 and obs[0].outcome=="PROVIDER_FAILURE"
    assert [s for s,_ in http.calls]==["FOLLOW_UP"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError(),BudgetExceededError("terminal"),DeadlineExceededError("terminal")])
async def test_terminal_errors_never_become_local_failure(error):
    from dovideo.application.follow_up import FollowUpFailure
    h,http,_=reflected(error=error)
    expected = FollowUpFailure if isinstance(error, DeadlineExceededError) else type(error)
    with pytest.raises(expected): await ask(h,Q)
    assert not http.calls and not (await h[4].store.load(h[5])).turns


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["{}",'{"status":"UNKNOWN","status":"SUFFICIENT"}',"x"*12001])
async def test_invalid_output_is_observed_without_changing_answer(raw):
    h,_,_=reflected(payload=raw)
    with capture_evidence_reflection() as obs: await ask(h,Q)
    assert obs[0].outcome=="INVALID_RESPONSE"


@pytest.mark.parametrize("field", ["source_revision","media_id","tools","budget","sourceItemId","evidence_scope"])
def test_control_fields_rejected(field):
    raw=proposal(); raw[field]="forged"
    with pytest.raises(ValidationError): validate_assessment(raw,Q,({},))


@pytest.mark.parametrize("status", ["SUFFICIENT","INSUFFICIENT","UNKNOWN"])
def test_three_statuses(status):
    assert validate_assessment(proposal(status=status),Q,({},)).status==status


@pytest.mark.parametrize("fault", ["span","candidate","bool","long","duplicate","too_many","construct"])
def test_strict_requirement_boundaries(fault):
    raw=proposal()
    if fault=="span": raw["missing_requirements"][0]["question_span"]="imagined actor"
    if fault=="candidate": raw["candidate_references"]=[7]; raw["missing_requirements"][0]["candidate_references"]=[7]
    if fault=="bool": raw["candidate_references"]=[True]
    if fault=="long": raw["missing_requirements"][0]["question_span"]="a"*241
    if fault=="duplicate": raw["missing_requirements"]*=2
    if fault=="too_many": raw["missing_requirements"]*=7
    if fault=="construct": raw=AssessmentSuggestion.model_construct(**{**raw,"status":"SUFFICIENT"})
    with pytest.raises((ValueError,TypeError)): validate_assessment(raw,Q,({},))


@pytest.mark.asyncio
async def test_empty_scope_without_model_call_preserves_no_evidence():
    from dovideo.application.follow_up import FollowUpFailure
    h,_,calls=reflected()
    async def empty(*a,**k): return ()
    h[2].search_evidence=empty
    with capture_evidence_reflection() as obs, pytest.raises(FollowUpFailure,match="no_evidence"):
        await ask(h,Q)
    assert not calls and obs[0].assessment.scope_reason=="NO_SUPPORT_IN_RETRIEVED_SCOPE"
    assert obs[0].assessment.status=="UNKNOWN"


def test_clipping_uses_exact_projection():
    hit=VideoEvidenceHit(start_ms=0,end_ms=1000,source="ASR",snippet="topic",transcript="主题"*200+"关键原因",ocr_texts=("a"*400+"关键条件","b","hidden"))
    p=source_payload(0,hit)
    assert "关键原因" not in p["asrExcerpt"] and "关键条件" not in str(p["ocrExcerpts"])
    assert len(p["ocrExcerpts"])==2


@pytest.mark.asyncio
async def test_parallel_capture_and_ledgers_are_request_local():
    async def run(q):
        h,_,_=reflected()
        with capture_evidence_reflection() as obs, capture_follow_up_budget() as usage:
            await ask(h,q)
        return obs,usage
    a,b=await asyncio.gather(run(Q),run(MIXED_Q))
    assert a[0][0].assessment.suggestion.missing_requirements[0].question_span==Q
    assert b[0][0].assessment.suggestion.missing_requirements[0].question_span==MIXED_Q
    assert a[1][0]["charged_chat_tokens"]==40 and b[1][0]["charged_chat_tokens"]==60


@pytest.mark.asyncio
@pytest.mark.parametrize("gap", ["MISSING_COMPARISON_SIDE","MISSING_CAUSAL_EVIDENCE","MISSING_TEMPORAL_CONTEXT","PARTIAL_FACT_COVERAGE","TOPIC_RELATED_BUT_NOT_ANSWERABLE","INSUFFICIENT_SOURCE_TEXT","UNCERTAIN_SEMANTIC_SUPPORT"])
async def test_fixture_gap_codes_survive_real_model_boundary(gap):
    # This checks gap transport/validation, not the model's ability to discover it.
    h,_,_=reflected(payload=proposal(gap=gap))
    with capture_evidence_reflection() as obs: await ask(h,Q)
    assert obs[0].assessment.suggestion.missing_requirements[0].reason_code==gap


@pytest.mark.asyncio
async def test_actual_local_five_second_timeout_preserves_answer():
    h,_,_=reflected()
    async def slow(*a,**k):
        await asyncio.sleep(6)
    h[0]._reflection.model.assess=slow
    with capture_evidence_reflection() as obs: answer=await ask(h,Q)
    assert answer and obs[0].outcome=="TIMEOUT"
    assert 4900 <= obs[0].latency_ms < 6000
