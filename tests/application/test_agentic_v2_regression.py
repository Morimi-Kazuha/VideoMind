"""Contract-reviewed synthetic substitutes; no video, network or semantic Gold."""
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from r4_validation_support import harness, insufficient
from dovideo.application.evidence_projection import source_payload
from dovideo.application.evidence_reflection import capture_evidence_reflection
from dovideo.application.retrieval_repair import capture_feedback_retrieval
from dovideo.application.retrieval_budget import capture_retrieval_budget
from dovideo.application.follow_up import FollowUpFailure
from dovideo.application.conversation_memory import ConversationIdentity
from dovideo.domain import AnalysisMode
from badcase_registry import BadCaseRegistry

Q = "作者为什么选择 B？"
GAP = insufficient(Q)

async def ask(h, **kwargs):
    return await h[0].answer(42, Q, "视频学习", AnalysisMode.LEARNING, **kwargs)


@pytest.mark.asyncio
async def test_v2_projection_clipping_is_not_fact_recovery():
    text = "前文介绍。" * 150 + "必要公式为 F(n)=7n+9。"
    h = await harness(Q, GAP, texts=(text, "背景。", "设置。", "原因。"))
    payload = source_payload(0, h[3][0], h[1].observations)
    assert "F(n)=7n+9" in h[1].observations[0].text
    assert "F(n)=7n+9" not in json.dumps(payload, ensure_ascii=False)
    assert len(payload["asrExcerpt"]) <= 400


@pytest.mark.asyncio
async def test_v2_invalid_reflection_union_cannot_authorize_repair():
    raw = dict(GAP, candidate_references=[0])  # union must be empty
    h = await harness(Q, raw)
    with capture_evidence_reflection() as reflection, capture_feedback_retrieval() as repair:
        assert await ask(h)
    assert reflection[0].outcome == "INVALID_RESPONSE"
    assert not repair[0].attempted


@pytest.mark.asyncio
async def test_v2_duplicate_repair_stops_after_one_attempt():
    h = await harness(Q, GAP)
    original = h[0]._retrieval.search_evidence_baseline
    async def duplicate(*a, **kw):
        await original(*a, **kw)
        return h[3]
    h[0]._retrieval.search_evidence_baseline = duplicate
    with capture_feedback_retrieval() as repair, capture_retrieval_budget() as budget:
        assert await ask(h)
    assert repair[0].state == "NO_NEW_EVIDENCE"
    assert repair[0].new_segments == 0
    assert budget[0]["repair_base_calls"] == budget[0]["repair_rounds"] == 1


@pytest.mark.asyncio
async def test_v2_repair_budget_denial_preserves_answer():
    h = await harness(Q, GAP, budget_limit=37000)
    with capture_feedback_retrieval() as repair, capture_retrieval_budget() as budget:
        assert await ask(h)
    assert repair[0].state == "SKIPPED_BUDGET"
    assert budget[0]["repair_base_calls"] == 0


@pytest.mark.asyncio
async def test_v2_empty_rewrite_cannot_become_video_evidence():
    h = await harness(Q, GAP, memory=True)
    cid = str(uuid4())
    await ask(h, user_id=1, conversation_id=cid, request_id=str(uuid4()))
    h[5].standalone_query = ""
    before = h[4]["embedding"]
    identity = ConversationIdentity.create(1, 42, "视频学习", AnalysisMode.LEARNING, cid, h[1])
    before_state = await h[0]._memory.store.load(identity)
    answer = await h[0].answer(42, "它为什么这样选？", "视频学习", AnalysisMode.LEARNING,
        user_id=1, conversation_id=cid, request_id=str(uuid4()))
    assert answer  # safe clarification is the documented recovery
    assert h[4]["embedding"] == before
    assert (await h[0]._memory.store.load(identity)).turns == before_state.turns


@pytest.mark.asyncio
async def test_v2_foreign_source_increment_rejected():
    h = await harness(Q, GAP)
    original = h[0]._retrieval.search_evidence_baseline
    async def foreign(*a, **kw):
        hits = await original(*a, **kw)
        return (hits[0].model_copy(update={"source_revision": "foreign"}),)
    h[0]._retrieval.search_evidence_baseline = foreign
    with capture_feedback_retrieval() as repair:
        assert await ask(h)
    assert repair[0].state == "INVALID_EVIDENCE"
    assert repair[0].new_segments == 0


@pytest.mark.asyncio
async def test_v2_terminal_request_never_commits_success():
    h = await harness(Q, GAP, memory=True, budget_limit=1)
    cid = str(uuid4())
    with pytest.raises(FollowUpFailure, match="budget_exceeded"):
        await ask(h, user_id=1, conversation_id=cid, request_id=str(uuid4()))
    identity = ConversationIdentity.create(1, 42, "视频学习", AnalysisMode.LEARNING, cid, h[1])
    state = await h[0]._memory.store.load(identity)
    assert not state.turns and not state.receipts


def test_v2_registry_dedup_preserves_runs_and_review_gate(tmp_path):
    registry = BadCaseRegistry(tmp_path / "registry")
    row = dict(case_id="tc-17", arm="D", error="budget_exceeded", input_hash="frozen")
    metadata = dict(run_id="run-1", source_code_commit="abc")
    for _ in range(2):
        registry.collect(row, artifact=tmp_path / "trace.json", metadata=metadata)
    registry.collect(row, artifact=tmp_path / "trace.json", metadata=dict(metadata, run_id="run-2"))
    assert len(registry.history("tc-17")) == 2
    first = registry.history()[0]
    with pytest.raises(ValueError, match="review"):
        registry.link(first["badcase_id"], test_id="example", review_status="NEEDS_REVIEW", verification={})
    registry.link(first["badcase_id"], test_id="v2-reg-04", review_status="REVIEWED_CONTRACT", verification=dict(run="fixed", passed=True))
    assert registry.history()[0]["verification_results"] == [dict(run="fixed", passed=True)]


def test_v2_manifest_maps_every_contract_to_discovered_test():
    manifest = json.loads((ROOT / "tests/fixtures/agentic-rag/v2/regression-manifest.json").read_text())
    assert len(manifest["cases"]) == 22
    assert all(c["test"] in globals() for c in manifest["cases"])


def test_r1_registry_diagnosis_keeps_original_identity_and_review_history(tmp_path):
    registry = BadCaseRegistry(tmp_path / "registry")
    registry.collect(dict(case_id="fixture", input_hash="input", error="evidence_rejected"),
        artifact=tmp_path / "original.json", metadata=dict(run_id="before", source_code_commit="original-commit"))
    original = registry.history()[0]
    for _ in range(2):
        registry.review(original["badcase_id"], root_cause="TIMESTAMP_MISMATCH", evidence_path=tmp_path / "audit.json",
            resolution_status="CORRECT_GUARD_REJECTION")
    reviewed = registry.history()[0]
    assert reviewed["badcase_id"] == original["badcase_id"] and reviewed["source_code_commit"] == "original-commit"
    assert reviewed["review_status"] == "REVIEWED_CONTRACT" and len(reviewed["root_cause_reviews"]) == 1


@pytest.mark.asyncio
async def test_v2_campaign_ledger_carries_failed_unreported_attempt(tmp_path):
    from argparse import Namespace
    from agentic_v2_evaluation import CampaignHttp
    from dovideo.application.errors import BudgetExceededError
    settings = Namespace(prior_followup_run=None, max_http_attempts=1,
        max_total_tokens=10000, max_reported_cost_usd=2)
    http = CampaignHttp(tmp_path, settings)
    class FailedTransport:
        async def post(self, *a, **kw):
            raise RuntimeError("fixture transport failed")
    http.inner = FailedTransport()
    request = dict(headers={}, json=dict(model="fixture", messages=[dict(content="hello")], max_tokens=100), timeout=1)
    with pytest.raises(RuntimeError):
        await http.post("https://fixture.test/chat", **request)
    snapshot = json.loads((tmp_path / "campaign-ledger.json").read_text())
    assert snapshot["attempts"] == 1 and snapshot["charged_tokens"] > 100
    assert snapshot["cost_state"] == "NOT_MEASURED" and not snapshot["cost_complete"]
    output = tmp_path / "continued"
    output.mkdir()
    settings.prior_followup_run = tmp_path
    continued = CampaignHttp(output, settings)
    with pytest.raises(BudgetExceededError):
        await continued.post("https://fixture.test/chat", **request)
    assert continued.tokens == http.tokens


def test_r1_query_projection_keeps_retrieved_formulas_without_larger_cap():
    from r4_validation_support import context_and_chunks, hit_for
    from dovideo.application.follow_up import GroundedFollowUpService
    texts = ("背景说明。" * 150 + "F(n)=7n+11", "实现介绍。" * 150 + "G(n)=9n+17")
    context, chunks = context_and_chunks(texts)
    context = context.model_copy(update={"user_goal": "比较 F(n)=7n+11 与 G(n)=9n+17"})
    hits = tuple(hit_for(s, chunks) for s in context.segments)
    prepared = GroundedFollowUpService._prepare_candidates(context, hits)
    assert len(prepared) == len(hits)
    for candidate, formula in zip(prepared, ("F(n)=7n+11", "G(n)=9n+17")):
        assert formula in candidate.prompt_hit.transcript
        assert candidate.prompt_hit.transcript in candidate.source_hit.transcript
        assert len(candidate.prompt_hit.transcript) <= 400
        assert candidate.prompt_hit.source_item_ids == candidate.source_hit.source_item_ids


def test_r1_ocr_time_mapping_survives_full_asr_observation_budget():
    from dovideo.domain import TemporalObservation, SourceItemIdentity, PROVENANCE_VERSION, VideoEvidenceHit
    from dovideo.domain.provenance import content_digest
    observations = []
    for kind, start, text in (("ASR", 100, "a" * 400), ("OCR", 750, "F(n)=7n+11")):
        item = SourceItemIdentity(source_item_id="item-" + kind, source_revision="rev", segment_id="seg",
            source_type=kind, ordinal=0, timestamp_ms=start, end_ms=900 if kind == "ASR" else None,
            content_digest=content_digest(text), provenance_version=PROVENANCE_VERSION)
        observations.append(TemporalObservation(source_item=item, text=text))
    hit = VideoEvidenceHit(start_ms=0, end_ms=1000, source="ASR+OCR", transcript="a" * 400,
        snippet="", ocr_texts=("F(n)=7n+11",), source_revision="rev", segment_id="seg",
        source_item_ids=tuple(o.source_item.source_item_id for o in observations))
    payload = source_payload(0, hit, observations)
    assert payload["ocrObservationTimestamps"] == [750]
    assert sum(len(o["excerpt"]) for o in payload["sourceObservations"]) <= 400
    foreign = observations[-1].model_copy(update={"source_item": observations[-1].source_item.model_copy(update={"source_revision": "foreign"})})
    assert "ocrObservationTimestamps" not in source_payload(0, hit, (observations[0], foreign))


@pytest.mark.asyncio
async def test_r1_missing_reflection_input_is_not_a_token_budget_denial():
    from dovideo.application.evidence_reflection import EvidenceReflection, ReflectionMode
    class NeverCalled:
        async def assess(self, *a, **kw):
            raise AssertionError("invalid input cannot call grader")
    with capture_evidence_reflection() as observations:
        await EvidenceReflection(NeverCalled(), ReflectionMode.SHADOW).observe(Q, ({"candidateIndex": 0},), messages=None)
    assert observations[0].outcome == "INPUT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_r1_reflection_and_answer_use_same_selected_source_windows():
    from r4_validation_support import context_and_chunks
    from dovideo.application.retrieval_repair import FeedbackRetrievalMode
    question = "比较 F(n)=7n+11 与 G(n)=9n+17"
    context, chunks = context_and_chunks(("说明。" * 220 + "F(n)=7n+11", "背景。" * 220 + "G(n)=9n+17"))
    h = await harness(question, insufficient("F(n)=7n+11"), context=context, chunks=chunks,
        initial_segments=context.segments, mode=FeedbackRetrievalMode.SHADOW)
    await h[0].answer(42, question, "视频学习", AnalysisMode.LEARNING)
    reflection = next(p for p in h[5].payloads if "Grade semantic sufficiency" in p["messages"][0]["content"])
    answer = h[5].payloads[-1]
    packed = json.loads(reflection["messages"][-1]["content"])
    before = packed["retrievedSourceCandidates"]
    for candidate in before:
        if "sourceObservations" in candidate:
            candidate["sourceObservations"] = [dict(zip(packed["sourceObservationColumns"],row,strict=True)) for row in candidate["sourceObservations"]]
    final = json.loads(answer["messages"][-1]["content"].split("Input as JSON:\n")[-1])["retrievedSourceCandidates"]
    assert before == final
    assert "F(n)=7n+11" in before[0]["asrExcerpt"] and "G(n)=9n+17" in before[1]["asrExcerpt"]


def test_r1_visible_ocr_time_does_not_allow_wrong_timestamp_or_foreign_quote():
    from dovideo.domain import VideoContext, VideoSegment, VideoEvidenceHit, SourceItemIdentity, PROVENANCE_VERSION
    from dovideo.domain.provenance import content_digest
    from dovideo.application.follow_up import GroundedFollowUpService
    from dovideo.infrastructure.providers.follow_up import decode_follow_up_answer
    item = SourceItemIdentity(source_item_id="ocr-item", source_revision="rev", segment_id="seg",
        source_type="OCR", ordinal=0, timestamp_ms=750, end_ms=None,
        content_digest=content_digest("F(n)=7n+11"), provenance_version=PROVENANCE_VERSION)
    segment = VideoSegment(start_ms=0, end_ms=1000, transcript="", ocr_texts=("F(n)=7n+11",),
        segment_id="seg", source_revision="rev", source_items=(item,), provenance_version=PROVENANCE_VERSION)
    context = VideoContext(source="fixture://r1", source_revision="rev", provenance_version=PROVENANCE_VERSION, segments=(segment,))
    hit = VideoEvidenceHit(start_ms=0, end_ms=1000, source="OCR", snippet="", ocr_texts=segment.ocr_texts,
        source_revision="rev", segment_id="seg", source_item_ids=(item.source_item_id,))
    service = GroundedFollowUpService(None, None, None)
    candidates = service._prepare_candidates(context, (hit,))
    raw = dict(answer="F(n)=7n+11", evidence=[dict(candidateIndex=0, timestampMs=0, source="OCR", content="F(n)=7n+11", claim="F(n)=7n+11")])
    assert service._verify_response(context, candidates, decode_follow_up_answer(raw)) is None
    raw["evidence"][0]["timestampMs"] = 750
    assert service._verify_response(context, candidates, decode_follow_up_answer(raw)) is not None
    raw["evidence"][0]["content"] = "G(n)=9n+17"
    assert service._verify_response(context, candidates, decode_follow_up_answer(raw)) is None


def test_r2_smoke_schedule_cannot_expand_four_authorized_requests():
    from agentic_v2_evaluation import followup_schedule
    cases = [dict(caseId=c) for c in ("tc-17", "tc-04", "v2-08", "tc-18")]
    selected = [c["caseId"] for c in cases]
    assert [(c["caseId"], arm) for c, arm in followup_schedule(cases, selected, "D", 4)] == [(c, "D") for c in selected]
    with pytest.raises(ValueError, match="ceiling"):
        followup_schedule(cases, selected, "C,D", 4)
    with pytest.raises(ValueError, match="duplicate"):
        followup_schedule(cases, selected + ["tc-17"], "D", 4)
    with pytest.raises(ValueError, match="unknown"):
        followup_schedule(cases, ["holdout-unknown"], "D", 4)


def test_r2_six_citations_replay_is_rejected_without_silent_truncation():
    from dovideo.infrastructure.providers.follow_up import decode_follow_up_answer, FollowUpModelFailure
    citation = dict(candidateIndex=0, timestampMs=0, source="ASR", content="fixture fact", claim="fixture fact")
    raw = dict(answer="fixture fact", evidence=[dict(citation,timestampMs=i) for i in range(5)])
    assert len(decode_follow_up_answer(raw).evidence) == 5
    raw["evidence"].append(dict(citation))
    with pytest.raises(FollowUpModelFailure, match="invalid_response"):
        decode_follow_up_answer(raw)


def test_r2_asr_quote_relabelled_ocr_replay_is_rejected():
    from r4_validation_support import context_and_chunks, hit_for
    from dovideo.application.follow_up import GroundedFollowUpService
    from dovideo.infrastructure.providers.follow_up import decode_follow_up_answer
    context, chunks = context_and_chunks(("fixture spoken fact",))
    service = GroundedFollowUpService(None, None, None)
    candidates = service._prepare_candidates(context, (hit_for(context.segments[0], chunks),))
    raw = dict(answer="fixture spoken fact", evidence=[dict(candidateIndex=0, timestampMs=0,
        source="ASR", content="fixture spoken fact", claim="fixture spoken fact")])
    assert service._verify_response(context, candidates, decode_follow_up_answer(raw)) is not None
    raw["evidence"][0]["source"] = "OCR"
    assert service._verify_response(context, candidates, decode_follow_up_answer(raw)) is None


@pytest.mark.parametrize("fault", ["absent", "source_item_id", "source_revision", "segment_id", "content_digest", "timestamp_ms"])
def test_r2_ocr_mapping_never_guesses_without_matching_authority(fault):
    from dovideo.domain import TemporalObservation, SourceItemIdentity, VideoEvidenceHit, PROVENANCE_VERSION
    from dovideo.domain.provenance import content_digest
    text = "F(n)=7n+11"
    item = SourceItemIdentity(source_item_id="ocr-item", source_revision="rev", segment_id="seg", source_type="OCR",
        ordinal=0, timestamp_ms=750, end_ms=None, content_digest=content_digest(text), provenance_version=PROVENANCE_VERSION)
    hit = VideoEvidenceHit(start_ms=0, end_ms=1000, source="OCR", snippet="", ocr_texts=(text,),
        source_revision="rev", segment_id="seg", source_item_ids=(item.source_item_id,))
    valid = TemporalObservation(source_item=item, text=text)
    assert source_payload(0, hit, (valid,))["ocrObservationTimestamps"] == [750]
    if fault == "absent":
        observations = ()
    else:
        values = {"source_item_id": "other", "source_revision": "other", "segment_id": "other",
            "content_digest": content_digest("different quote"), "timestamp_ms": 1000}
        observations = (valid.model_copy(update={"source_item": item.model_copy(update={fault: values[fault]})}),)
    assert "ocrObservationTimestamps" not in source_payload(0, hit, observations)


def test_r2_one_window_preserves_both_explicit_facts_when_they_fit():
    from dovideo.application.evidence_projection import bounded_source_hit
    from dovideo.domain import VideoEvidenceHit
    text = "说明。" * 180 + "F(n)=7n+11 G(n)=9n+17"
    hit = VideoEvidenceHit(start_ms=0, end_ms=1000, source="ASR", snippet="", transcript=text)
    projected = bounded_source_hit(hit, "比较 F(n)=7n+11 与 G(n)=9n+17")
    assert "F(n)=7n+11" in projected.transcript and "G(n)=9n+17" in projected.transcript
    assert len(projected.transcript) <= 400 and projected.transcript in text


@pytest.mark.asyncio
async def test_r3_first_literal_uses_complete_cache_and_scoped_answer():
    from r4_validation_support import context_and_chunks
    question = "请定位视频第一次完整写出 F(n)=7n+9 的画面。"
    context, chunks = context_and_chunks(("早期 F(n)=7n+9", "稍后 F(n)=7n+9"))
    h = await harness(question, insufficient(question), context=context, chunks=chunks,
        initial_segments=context.segments[1:])
    answer = await h[0].answer(42, question, "视频学习", AnalysisMode.LEARNING)
    assert "可核验观察" in answer and "0ms" in answer
    assert "不能确认全视频首次" in answer
    assert not h[5].payloads  # deterministic scoped location, no new LLM assertions


@pytest.mark.asyncio
async def test_r3_first_literal_no_exact_source_returns_uncertainty():
    question = "请定位视频首次出现 F(n)=7n+9 的画面。"
    h = await harness(question, insufficient(question), texts=("F(n)=7n-9",))
    answer = await h[0].answer(42, question, "视频学习", AnalysisMode.LEARNING)
    assert "无法确认" in answer and not h[5].payloads


def test_r3_duplicate_citation_is_invalid_without_deduplication():
    from dovideo.infrastructure.providers.follow_up import decode_follow_up_answer, FollowUpModelFailure
    citation = dict(candidateIndex=0, timestampMs=0, source="ASR", content="literal", claim="literal")
    with pytest.raises(FollowUpModelFailure, match="invalid_response"):
        decode_follow_up_answer(dict(answer="literal", evidence=[citation, dict(citation)]))


@pytest.mark.asyncio
async def test_r3_generation_contract_carries_dto_schema_and_channel_rule():
    from dovideo.infrastructure.providers.follow_up import GroundedFollowUpModelAdapter
    from dovideo.application.mode_profiles import mode_profile_for
    from dovideo.domain import VideoEvidenceHit, GroundedFollowUpAnswer
    class Chat:
        async def complete(self, messages, *, stage):
            self.messages = messages
            return dict(answer="spoken", evidence=[dict(candidateIndex=0, timestampMs=0,
                source="ASR", content="spoken", claim="spoken")])
    chat = Chat()
    await GroundedFollowUpModelAdapter(chat).answer("比较四项事实", original_goal="视频学习",
        profile=mode_profile_for(AnalysisMode.LEARNING), prior_analysis=None,
        sources=(VideoEvidenceHit(start_ms=0,end_ms=1000,source="ASR+OCR",snippet="",transcript="spoken",ocr_texts=("displayed",)),))
    prompt = chat.messages[-1]["content"]
    request = json.loads(prompt.split("Input as JSON:\n")[-1])
    assert request["outputSchema"] == GroundedFollowUpAnswer.model_json_schema(by_alias=True)
    assert "Never relabel ASR text as OCR" in prompt
    assert "incomplete" in prompt and "duplicate" in prompt


def test_r3_reflection_wire_table_is_lossless_and_smaller():
    from dovideo.infrastructure.providers.evidence_reflection import EvidenceReflectionModelAdapter
    candidate = dict(candidateIndex=0,startMs=0,endMs=1000,source="ASR+OCR",asrExcerpt="spoken",
        ocrExcerpts=["F(n)=7n+9"],ocrObservationTimestamps=[750],
        sourceObservations=[dict(source="ASR",timestampMs=i,endMs=i+1,excerpt="x") for i in range(16)])
    messages = EvidenceReflectionModelAdapter.messages(Q,(candidate,))
    packed = json.loads(messages[-1]["content"])
    unpacked = dict(packed["retrievedSourceCandidates"][0])
    unpacked["sourceObservations"] = [dict(zip(packed["sourceObservationColumns"],r,strict=True)) for r in unpacked["sourceObservations"]]
    assert unpacked == candidate
    legacy = json.dumps(dict(question=Q,scope="BOUNDED_ANSWER_INPUT",route="VIDEO_RAG",retrievedSourceCandidates=[candidate]),ensure_ascii=False,separators=(",",":"))
    assert len(messages[-1]["content"]) < len(legacy)


@pytest.mark.parametrize("fault", ["correct_asr", "correct_ocr", "wrong_channel", "similar_formula", "authoritative_formula", "foreign_revision", "missing_source", "wrong_ocr_time", "same_text_explicit_channel"])
def test_r3_channel_identity_remains_explicit_and_verified(fault):
    from dovideo.domain import VideoContext, VideoSegment, VideoEvidenceHit, TemporalObservation, SourceItemIdentity, PROVENANCE_VERSION
    from dovideo.domain.provenance import content_digest
    from dovideo.application.follow_up import GroundedFollowUpService
    from dovideo.infrastructure.providers.follow_up import decode_follow_up_answer, FollowUpModelFailure
    asr = "F(n)=7n+9"
    ocr = asr if fault == "same_text_explicit_channel" else "F(n)=7n-9"
    items = tuple(SourceItemIdentity(source_item_id=channel,source_revision="rev",segment_id="seg",source_type=channel,
        ordinal=0,timestamp_ms=0 if channel=="ASR" else 750,end_ms=1000 if channel=="ASR" else None,
        content_digest=content_digest(text),provenance_version=PROVENANCE_VERSION) for channel,text in [("ASR",asr),("OCR",ocr)])
    segment = VideoSegment(start_ms=0,end_ms=1000,transcript=asr,ocr_texts=(ocr,),segment_id="seg",source_revision="rev",source_items=items,provenance_version=PROVENANCE_VERSION)
    context = VideoContext(source="fixture://channels",source_revision="rev",provenance_version=PROVENANCE_VERSION,segments=(segment,),
        observations=tuple(TemporalObservation(source_item=i,text=t) for i,t in zip(items,(asr,ocr),strict=True)))
    hit = VideoEvidenceHit(start_ms=0,end_ms=1000,source="ASR+OCR",snippet="",transcript=asr,ocr_texts=(ocr,),source_revision="rev",segment_id="seg",source_item_ids=("ASR","OCR"))
    if fault == "foreign_revision":hit=hit.model_copy(update={"source_revision":"other"})
    if fault == "authoritative_formula":hit=hit.model_copy(update={"ocr_texts":(asr,)})
    service=GroundedFollowUpService(None,None,None);candidates=service._prepare_candidates(context,(hit,))
    quote = ocr if fault in {"correct_ocr","wrong_ocr_time","same_text_explicit_channel"} else asr
    source = "ASR" if fault in {"correct_asr","foreign_revision"} else "OCR"
    timestamp = 0 if fault in {"wrong_ocr_time","wrong_channel"} else 750
    evidence=dict(candidateIndex=0,timestampMs=timestamp,source=source,content=quote,claim=quote)
    if fault == "missing_source":
        evidence.pop("source")
        with pytest.raises(FollowUpModelFailure):decode_follow_up_answer(dict(answer=quote,evidence=[evidence]))
        return
    result=service._verify_response(context,candidates,decode_follow_up_answer(dict(answer=quote,evidence=[evidence])))
    assert (result is not None) == (fault in {"correct_asr","correct_ocr","same_text_explicit_channel"})


@pytest.mark.parametrize("fault", ["valid", "foreign_revision", "digest", "time_outside", "missing_observations", "larger_literal", "equivalent_not_literal", "ambiguous_identity"])
def test_r3_literal_location_never_guesses_scope_or_equivalence(fault):
    from r4_validation_support import context_and_chunks
    from dovideo.application.temporal_location import first_literal_observations
    context,_=context_and_chunks(("F(n)=7n+9",))
    observation=context.observations[0];item=observation.source_item
    if fault in {"foreign_revision","digest","time_outside"}:
        field,value={"foreign_revision":("source_revision","other"),"digest":("content_digest","wrong"),"time_outside":("timestamp_ms",60001)}[fault]
        changed=observation.model_copy(update={"source_item":item.model_copy(update={field:value})})
        context=context.model_copy(update={"observations":(changed,)})
    elif fault=="missing_observations":context=context.model_copy(update={"observations":()})
    elif fault in {"larger_literal","equivalent_not_literal"}:
        context,_=context_and_chunks(("F(n)=7n+90" if fault=="larger_literal" else "F(n)=9+7n",))
    elif fault=="ambiguous_identity":
        other=item.model_copy(update={"source_item_id":"other"})
        segment=context.segments[0].model_copy(update={"source_items":(item,other)})
        context=context.model_copy(update={"segments":(segment,),"observations":(observation,observation.model_copy(update={"source_item":other}))})
    matches=first_literal_observations(context,("F(n)=7n+9",))
    assert bool(matches)==(fault=="valid")


def test_r3_semantic_first_question_cannot_fall_through_to_model():
    from dovideo.application.temporal_location import first_location_literals
    assert first_location_literals("请定位首次讲到增长阶的画面") == ()
    assert first_location_literals("请解释 F(n)=7n+9") is None
    assert first_location_literals("作者为什么首次写出 F(n)=7n+9？") is None


def test_r3_empty_evidence_and_six_facts_are_not_silently_accepted():
    from dovideo.infrastructure.providers.follow_up import decode_follow_up_answer, FollowUpModelFailure
    from r4_validation_support import context_and_chunks, hit_for
    from dovideo.application.follow_up import GroundedFollowUpService
    facts=[f"fact {i}" for i in range(6)]
    evidence=[dict(candidateIndex=i,timestampMs=i*600000,source="ASR",content=f,claim=f) for i,f in enumerate(facts)]
    response=decode_follow_up_answer(dict(answer="\n".join(facts[:4]),evidence=evidence[:4]))
    context,chunks=context_and_chunks(tuple(facts[:4]));service=GroundedFollowUpService(None,None,None)
    candidates=service._prepare_candidates(context,tuple(hit_for(s,chunks) for s in context.segments))
    verified=service._verify_response(context,candidates,response)
    assert verified is not None and {v.content for v in verified}==set(facts[:4])
    bad=[dict(item) for item in evidence[:4]];bad[-1]["content"]="unprovided fact"
    assert service._verify_response(context,candidates,decode_follow_up_answer(dict(answer=response.answer,evidence=bad))) is None
    for values in [[],evidence]:
        with pytest.raises(FollowUpModelFailure):decode_follow_up_answer(dict(answer="\n".join(facts),evidence=values))


def test_r3_literal_boundaries_do_not_join_adjacent_ocr_lines():
    from r4_validation_support import context_and_chunks
    from dovideo.application.temporal_location import first_literal_observations
    from dovideo.domain import TemporalObservation
    context,_=context_and_chunks(("F(n)=7n+9\nNext slide", "F(n)=7n+9"))
    segments, observations = [], []
    for segment, observation in zip(context.segments,context.observations,strict=True):
        # OCR owns the complete multiline frame; ASR authority is line-based.
        item=observation.source_item.model_copy(update={"source_type":"OCR","end_ms":None})
        segments.append(segment.model_copy(update={"transcript":"","ocr_texts":(observation.text,),"source_items":(item,)}))
        observations.append(TemporalObservation(source_item=item,text=observation.text))
    context=context.model_copy(update={"segments":tuple(segments),"observations":tuple(observations)})
    assert first_literal_observations(context,("F(n)=7n+9",))[0][1].source_item.timestamp_ms==0


def test_r3_ocr_frame_mapping_preserves_formula_operators():
    from dovideo.domain import VideoEvidenceHit,TemporalObservation,SourceItemIdentity,PROVENANCE_VERSION,content_digest
    texts=("F(n)=7n-9","F(n)=7n+9")
    items=[SourceItemIdentity(source_item_id=str(i),source_revision="rev",segment_id="seg",source_type="OCR",ordinal=i,
        timestamp_ms=750+i*100,end_ms=None,content_digest=content_digest(t),provenance_version=PROVENANCE_VERSION) for i,t in enumerate(texts)]
    observations=tuple(TemporalObservation(source_item=i,text=t) for i,t in zip(items,texts,strict=True))
    hit=VideoEvidenceHit(start_ms=0,end_ms=1000,source="OCR",snippet="",ocr_texts=(texts[1],),source_revision="rev",segment_id="seg",source_item_ids=("0","1"))
    assert source_payload(0,hit,observations)["ocrObservationTimestamps"]==[850]


@pytest.mark.asyncio
@pytest.mark.parametrize("source,claim,answer,delivered", [
    ("H(k)=4k+7", "H(k)=4k+7", "H(k)=4k+7", True),
    ("H(k)=4k+7", "H(k)=4k-7", "H(k)=4k-7", False),
    ("H(k)=4k+7", "H(k)=4k+7", "-H(k)=4k+7", False),
    ("H(k)=4k+7", "Hk=4k+7", "Hk=4k+7", False),
    ("H(k)=4k+7", "h(k)=4k+7", "h(k)=4k+7", False),
    ("H(k)=4k-7", "H(k)=4k-7", "H(k)=4k-7", True),
])
async def test_final_gate_mixed_cannot_mutate_verified_quote(source,claim,answer,delivered):
    from dovideo.domain import GroundedFollowUpAnswer
    from dovideo.application.follow_up_outcome import KnowledgeBlock
    question="作者对 H(k)=4k+7 的解释正确吗？"
    h=await harness(question,insufficient(question),texts=(source,),budget_limit=50000)
    class Fact:
        async def answer(self,*a,**kw):
            assert kw.get("fact_only") is True
            return GroundedFollowUpAnswer.model_validate(dict(answer=answer,evidence=[dict(
                candidateIndex=0,timestampMs=0,source="ASR",content=source,claim=claim)]))
    class Knowledge:
        def __init__(self):self.received=[]
        async def analyze(self,question,facts):
            self.received.append(facts)
            return (KnowledgeBlock(kind="INFERENCE",text="需要按规模条件分析。"),)
    knowledge=Knowledge();h[0]._model=Fact();h[0]._mixed_knowledge_model=knowledge
    if delivered:
        assert await h[0].answer(42,question,"视频学习",AnalysisMode.LEARNING)
        assert knowledge.received==[(source,)]
    else:
        with pytest.raises(FollowUpFailure,match="evidence_rejected"):
            await h[0].answer(42,question,"视频学习",AnalysisMode.LEARNING)
        assert not knowledge.received
