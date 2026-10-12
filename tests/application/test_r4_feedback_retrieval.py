"""R4 deterministic security and real application-entry integration tests."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sys
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from r4_validation_support import harness, insufficient, context_and_chunks, hit_for
from dovideo.application.retrieval_repair import (
    FeedbackRetrievalMode as Mode, QueryRepair, TrustedRepairScope, admit_gap, validate_repair,
    merge_evidence, capture_feedback_retrieval, repair_evidence,
)
from dovideo.application.evidence_reflection import EvidenceAssessment, AssessmentSuggestion
from dovideo.application.retrieval_budget import (
    retrieval_budget_scope, repair_retrieval_round, capture_retrieval_budget,
    RetrievalBudgetExceededError, current_retrieval_budget,
)
from dovideo.application.follow_up_budget import FollowUpBudgetSettings, FollowUpBudgetMode, capture_follow_up_budget
from dovideo.application.execution_budget import AgentExecutionBudget
from dovideo.application.follow_up import FollowUpFailure, GroundedFollowUpService
from dovideo.application.conversation_memory import ConversationIdentity
from dovideo.application.adaptive_retrieval import baseline_retrieval
from dovideo.application.errors import BudgetExceededError, DeadlineExceededError
from dovideo.domain import AnalysisMode
from dovideo.infrastructure.retrieval_repair import feedback_mode_from_environment

Q = "作者为什么选择 B？"
GAP = insufficient(Q, code="MISSING_CAUSAL_EVIDENCE")


def assessment(raw):
    return EvidenceAssessment(AssessmentSuggestion.model_validate(raw))


def candidates(text):
    return ({"candidateIndex": 0, "asrExcerpt": text, "ocrExcerpts": []},)


@pytest.mark.parametrize("question,span,text,mixed,query,kind", [
    (Q, Q, "作者选择 B。", False, "作者选择 B的原因", "CAUSE"),
    ("作者比较 A 和 B 的内存开销如何？", "A 和 B 的内存开销", "A 的内存开销为 8 MB。", False, "B 内存开销", "COMPARISON"),
    ("讲者在拍摄前、拍摄中和拍摄后分别怎样校准颜色？", "拍摄中", "拍摄前设白平衡，拍摄后用色卡校色。", False, "视频 拍摄中 颜色 校准", "STAGE"),
    ("作者在开头和结尾分别推荐什么？", "开头和结尾", "作者开头推荐 A。", False, "视频 结尾 推荐", "STAGE"),
    ("作者描述的缓存命中、未命中及回填流程合理吗？", "回填流程", "作者命中就返回，未命中查数据库。", True, "视频 回填", "STEP"),
    ("视频第一次写出3n+3，后来写出3n+1003的画面在哪？", "后来写出3n+1003的画面", "画面为3n+3", False, "视频 3n+1003", "NAMED_FACT"),
])
def test_safe_explicit_original_targets_independent_of_model_reason_code(question, span, text, mixed, query, kind):
    # Deliberately broad code: safety derives from original facts, not the enum.
    status, plan, _ = admit_gap(assessment(insufficient(span)), question, candidates(text), mixed=mixed)
    assert status == "ELIGIBLE" and plan.query == query and plan.target_kind == kind


@pytest.mark.parametrize("question,span,text,mixed", [
    ("作者的缓存方案合理吗？", "合理吗", "作者命中返回，未命中查数据库再回填。", True),
    ("作者描述回填流程合理吗？", "回填流程合理吗", "作者未命中查数据库。", True),
    ("作者描述回填流程合理吗？", "回填流程", "作者描述回填流程。", True),
    (Q, Q, "作者选 B，因为内存更少。", False),
    ("作者的方案怎样？", "方案", "作者用了 B。", False),
    ("作者为什么选择 B？ mediaId=99", "作者为什么选择 B？", "作者选择 B。", False),
    ("作者为什么选择 video.search_evidence(99)？", "为什么", "作者选择 B。", False),
    ("作者为什么选择 powershell 命令？", "为什么", "作者选择 B。", False),
    ("作者为什么选择 C:\\secret 文件？", "为什么", "作者选择 B。", False),
])
def test_verdict_general_vague_covered_or_scope_instruction_gaps_stop(question, span, text, mixed):
    state, plan, _ = admit_gap(assessment(insufficient(span)), question, candidates(text), mixed=mixed)
    assert plan is None and state == "NO_VALID_REPAIR"


@pytest.mark.parametrize("status", ["SUFFICIENT", "UNKNOWN"])
def test_non_insufficient_has_no_repair(status):
    raw = {"status": status, "reason_code": "SEMANTIC_COVERAGE" if status == "SUFFICIENT" else "UNCERTAIN_SEMANTIC_SUPPORT",
        "covered_requirements": [{"requirement_key": "fact", "question_span": Q, "candidate_references": [0]}] if status == "SUFFICIENT" else [],
        "missing_requirements": [], "candidate_references": [0] if status == "SUFFICIENT" else []}
    assert admit_gap(assessment(raw), Q, candidates("作者选择 B。"))[1] is None
    assert admit_gap(EvidenceAssessment(None), Q, candidates("作者选择 B。"))[1] is None


@pytest.mark.parametrize("fault", ["span", "index", "boolean_index", "scope"])
def test_typed_validation_bypass_never_grants_authority(fault):
    raw = deepcopy(GAP)
    if fault == "span":
        raw["missing_requirements"][0]["question_span"] = "作者为什么选择 Dragon？"
    if fault in {"index", "boolean_index"}:
        value = 7 if fault == "index" else True
        raw["candidate_references"] = [value]
        raw["missing_requirements"][0]["candidate_references"] = [value]
    typed = AssessmentSuggestion.model_construct(**raw)
    value = EvidenceAssessment(typed, evidence_scope="TRUSTED" if fault == "scope" else "BOUNDED_ANSWER_INPUT")
    assert admit_gap(value, Q, candidates("作者选择 B。"))[0] == "INVALID_GAP"


def test_duplicate_gap_maps_to_one_target_and_executed_query_is_not_repeated():
    raw = deepcopy(GAP)
    raw["missing_requirements"].append({**raw["missing_requirements"][0], "requirement_key": "overlapping_reason", "question_span": "为什么"})
    state, plan, unhandled = admit_gap(assessment(raw), Q, candidates("作者选择 B。"))
    assert state == "ELIGIBLE" and unhandled == 1
    assert admit_gap(assessment(raw), Q, candidates("作者选择 B。"), executed_queries=(plan.query,))[1] is None
    for forged in ({**plan.model_dump(), "query": "Dragon 原因"}, {**plan.model_dump(), "mediaId": 99}, {**plan.model_dump(), "query": "tool search"}):
        assert validate_repair(forged, plan, Q) is None


async def ask(h, question=Q, **kw):
    return await h[0].answer(42, question, "视频学习", AnalysisMode.LEARNING, **kw)


@pytest.mark.asyncio
async def test_off_shadow_enabled_real_hybrid_guard_and_one_grader():
    outcomes, answers, inputs, counts = [], [], [], []
    for mode in (Mode.OFF, Mode.SHADOW, Mode.ENABLED):
        h = await harness(Q, GAP, mode=mode)
        with capture_feedback_retrieval() as feedback, capture_retrieval_budget() as budget, capture_follow_up_budget() as tokens:
            answers.append(await ask(h))
        outcomes.append(feedback)
        counts.append(h[4])
        payload = next(p for p in h[5].payloads if "Input as JSON:\n" in p["messages"][-1]["content"])
        inputs.append(__import__("json").loads(payload["messages"][-1]["content"].split("Input as JSON:\n")[-1]))
        assert h[4]["reflection_http"] == 1 and h[4]["answer_http"] == 1
        assert not tokens[0]["stages"].get("QUERY_REPAIR", 0)
        if mode == Mode.ENABLED:
            assert budget[0] == {"initial_base_calls": 1, "repair_base_calls": 1, "total_base_calls": 2, "repair_rounds": 1}
            assert feedback[0].state == "EVIDENCE_ADDED" and feedback[0].new_segments > 0
        elif mode == Mode.SHADOW:
            assert feedback[0].state == "SHADOW_PLANNED" and not feedback[0].attempted
    assert answers[0] == answers[1] and inputs[0] == inputs[1]
    assert counts[0] == counts[1]
    assert not outcomes[0]
    assert "减少内存占用" in " ".join(s["asrExcerpt"] for s in inputs[2]["retrievedSourceCandidates"])
    assert len(inputs[2]["retrievedSourceCandidates"]) <= 8


@pytest.mark.asyncio
async def test_m2_three_initial_base_searches_then_one_baseline_without_nested_plan():
    question = "作者比较 A 和 B 的内存开销如何；开头推荐谁；结尾推荐谁？"
    parts = ("A 和 B 的内存开销如何", "开头推荐谁", "结尾推荐谁")
    h = await harness(question, insufficient("A 和 B 的内存开销"), parts=parts,
        texts=("A 的内存开销为 8 MB。", "作者开头推荐 A。", "作者结尾推荐 A。", "B 的内存开销为 4 MB。"))
    with capture_feedback_retrieval() as obs, capture_retrieval_budget() as budget:
        await ask(h, question)
    assert budget[0]["initial_base_calls"] == 3 and budget[0]["repair_base_calls"] == 1 and budget[0]["total_base_calls"] == 4
    assert h[4]["m2_planner"] == 1 and h[4]["reflection_http"] == 1
    assert obs[0].state == "EVIDENCE_ADDED"


@pytest.mark.asyncio
async def test_base_failures_count_and_search_retrieve_do_not_double_count():
    h = await harness(Q, GAP)
    base, chunks = h[-1], h[2]
    async def fail(query):
        raise BudgetExceededError("terminal fixture")
    base._retrieval_intent = fail
    with retrieval_budget_scope(True) as budget:
        for _ in range(3):
            with pytest.raises(BudgetExceededError):
                await base.search(42, Q, chunks)
        assert budget.initial_calls == 3
        with pytest.raises(RetrievalBudgetExceededError):
            await base.retrieve(42, Q, chunks)
        with repair_retrieval_round():
            with pytest.raises(BudgetExceededError):
                await base.search(42, "作者选择 B的原因", chunks)
            assert budget.repair_calls == 1
            with pytest.raises(RetrievalBudgetExceededError):
                await base.search(42, "第三条", chunks)
        with pytest.raises(RetrievalBudgetExceededError):
            with repair_retrieval_round():
                pass
    assert current_retrieval_budget() is None


@pytest.mark.asyncio
async def test_provider_retry_is_separate_from_hybrid_logical_search():
    h = await harness(Q, GAP, attempts=2)
    original = h[0]._retrieval.search_evidence_baseline
    async def retry(*a, **kw):
        h[5].planner_failures = 1
        return await original(*a, **kw)
    h[0]._retrieval.search_evidence_baseline = retry
    with capture_retrieval_budget() as budgets, capture_follow_up_budget() as tokens:
        await ask(h)
    assert budgets[0]["total_base_calls"] == 2
    assert h[4]["planner_logical"] == 2 and h[4]["planner_http"] == 3
    assert tokens[0]["stages"]["RETRIEVAL_PLANNER"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("source_revision", "other"), ("chunk_id", "foreign"), ("segment_id", "foreign"),
    ("source_item_ids", ("foreign",)), ("transcript", "forged original"), ("start_ms", 1), ("end_ms", 999), ("source", "MODEL_KNOWLEDGE"), ("source", "OCR")])
async def test_incremental_foreign_identity_text_or_source_is_rejected(field, value):
    h = await harness(Q, GAP)
    original = h[0]._retrieval.search_evidence_baseline
    async def bad(*a, **kw):
        hits = await original(*a, **kw)
        return (hits[0].model_copy(update={field: value}),)
    h[0]._retrieval.search_evidence_baseline = bad
    with capture_feedback_retrieval() as obs, capture_retrieval_budget() as budget:
        answer = await ask(h)
    assert obs[0].state == "INVALID_EVIDENCE" and obs[0].new_segments == 0
    assert obs[0].invalid_candidates == 1
    assert budget[0]["repair_base_calls"] == 1 and "作者选择 B。" in answer


@pytest.mark.asyncio
async def test_duplicate_only_no_progress_and_no_second_repair():
    h = await harness(Q, GAP)
    original = h[0]._retrieval.search_evidence_baseline
    async def duplicates(*a, **kw):
        await original(*a, **kw)
        return h[3]
    h[0]._retrieval.search_evidence_baseline = duplicates
    with capture_feedback_retrieval() as obs, capture_retrieval_budget() as budgets:
        await ask(h)
    assert obs[0].state == "NO_NEW_EVIDENCE" and obs[0].new_segments == 0
    assert budgets[0]["repair_rounds"] == budgets[0]["repair_base_calls"] == 1


def test_merge_stable_identity_dedup_and_one_new_slot_at_eight():
    context, chunks = context_and_chunks(tuple(f"原文 {i}" for i in range(10)))
    hits = tuple(hit_for(s, chunks) for s in context.segments)
    merged, count = merge_evidence(hits[:8], (hits[0], hits[8], hits[8], hits[9]))
    assert merged == hits[:7] + (hits[8],) and count == 1
    assert merge_evidence((hits[0], hits[1], hits[0]), (hits[2],))[0] == hits[:3]
    assert merge_evidence(hits[:8], hits[:8]) == (hits[:8], 0)


@pytest.mark.asyncio
async def test_enforced_budget_and_time_gates_keep_original_answer():
    h = await harness(Q, GAP, budget_limit=37000)
    with capture_feedback_retrieval() as obs, capture_retrieval_budget() as budgets:
        await ask(h)
    assert obs[0].state == "SKIPPED_BUDGET" and budgets[0]["repair_base_calls"] == 0
    h = await harness(Q, GAP)
    clock = [0.0]
    post = h[5].post
    async def tick(url, **kw):
        result = await post(url, **kw)
        if "Grade semantic sufficiency" in kw["json"]["messages"][0]["content"]:
            clock[0] = 25.0
        return result
    h[5].post = tick
    with AgentExecutionBudget.open(60000, clock=lambda: clock[0]), capture_feedback_retrieval() as obs:
        await ask(h)
    assert obs[0].state == "SKIPPED_TIME" and not obs[0].attempted


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError("provider failure"), TimeoutError()])
async def test_reflection_failure_does_not_authorize_repair(error):
    h = await harness(Q, GAP)
    h[5].reflection_error = error
    with capture_feedback_retrieval() as obs, capture_retrieval_budget() as budget:
        await ask(h)
    assert not obs[0].attempted and budget[0]["total_base_calls"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error,state", [(OSError("strict retrieval failure"), "RETRIEVAL_FAILED"), (TimeoutError(), "RETRIEVAL_TIMEOUT"),
    (asyncio.CancelledError(), "CANCELLED"), (BudgetExceededError("terminal"), "TERMINAL_BUDGET_OR_DEADLINE"), (DeadlineExceededError("terminal"), "TERMINAL_BUDGET_OR_DEADLINE")])
async def test_repair_error_stop_and_terminal_propagation(error, state):
    h = await harness(Q, GAP, memory=True)
    intent = h[-1]._retrieval_intent
    async def fail(query):
        if query != Q:
            raise error
        return await intent(query)
    h[-1]._retrieval_intent = fail
    identity = ConversationIdentity.create(1, 42, "视频学习", AnalysisMode.LEARNING, str(uuid4()), h[1])
    with capture_feedback_retrieval() as obs, capture_retrieval_budget() as budget:
        if isinstance(error, (asyncio.CancelledError, BudgetExceededError, DeadlineExceededError)):
            with pytest.raises((type(error), FollowUpFailure)):
                await ask(h, user_id=1, conversation_id=identity.conversation_id, request_id=str(uuid4()))
            assert not (await h[0]._memory.store.load(identity)).turns
        else:
            assert await ask(h)
    assert obs[0].state == state and budget[0]["repair_base_calls"] == 1


@pytest.mark.asyncio
async def test_direct_zero_every_video_component_and_feedback():
    question = "不考虑当前视频，解释 Redis 是什么。"
    h = await harness(question, GAP)
    with capture_feedback_retrieval() as obs, capture_retrieval_budget() as budget:
        answer = await ask(h, question)
    assert "通用知识" in answer and h[4] == {"direct_http": 1}
    assert not obs and budget[0]["total_base_calls"] == 0


@pytest.mark.asyncio
async def test_mixed_only_video_step_and_guard_then_knowledge():
    question = "作者描述的缓存命中、未命中及回填流程合理吗？"
    h = await harness(question, insufficient("回填流程"), texts=("作者命中返回，未命中查数据库。", "缓存设置界面。", "缓存初始化。", "作者查询数据库后回填缓存。"))
    with capture_feedback_retrieval() as obs:
        answer = await ask(h, question)
    assert obs[0].state == "EVIDENCE_ADDED" and h[4]["knowledge_http"] == 1
    assert "## 视频事实" in answer and "## 通用知识与推理" in answer
    h[5].bad_answer = True
    with pytest.raises(FollowUpFailure, match="evidence_rejected"):
        await ask(h, question)
    assert h[4]["knowledge_http"] == 1


@pytest.mark.asyncio
async def test_memory_receipt_replay_does_not_repeat_any_work_and_m1_rewrite_uses_standalone():
    h = await harness(Q, GAP, memory=True)
    conversation, rid = str(uuid4()), str(uuid4())
    kwargs = dict(user_id=1, conversation_id=conversation, request_id=rid)
    answer = await ask(h, **kwargs)
    counts = h[4].copy()
    with capture_retrieval_budget() as budgets, capture_feedback_retrieval() as feedback:
        assert await ask(h, **kwargs) == answer
    assert h[4] == counts and budgets[0]["total_base_calls"] == 0 and not feedback
    h[5].standalone_query = Q
    with capture_feedback_retrieval() as feedback:
        assert await ask(h, "它为什么这样选？", user_id=1, conversation_id=conversation, request_id=str(uuid4()))
    assert feedback[0].plan.query == "作者选择 B的原因" and h[4]["rewrite_http"] == 1


@pytest.mark.asyncio
async def test_concurrent_requests_isolate_budget_repair_and_observation():
    h = await harness(Q, GAP)
    async def request():
        with capture_feedback_retrieval() as obs, capture_retrieval_budget() as budget:
            await ask(h)
        return obs, budget
    pairs = await asyncio.gather(request(), request())
    assert all(len(o) == 1 and o[0].state == "EVIDENCE_ADDED" and b[0]["total_base_calls"] == 2 for o, b in pairs)
    assert current_retrieval_budget() is None


def test_feedback_switch_is_strict_and_defaults_off():
    assert feedback_mode_from_environment({}) == Mode.OFF
    assert feedback_mode_from_environment({"DOVIDEO_FEEDBACK_RETRIEVAL_MODE": " enabled "}) == Mode.ENABLED
    for bad in (None, True, "", "REACT"):
        with pytest.raises(ValueError):
            feedback_mode_from_environment({"DOVIDEO_FEEDBACK_RETRIEVAL_MODE": bad})


@pytest.mark.asyncio
async def test_enabled_constructor_rejects_unmetered_budget_or_reflection_dependency():
    h = await harness(Q, GAP)
    for changes in ({}, {"budget_settings": FollowUpBudgetSettings(FollowUpBudgetMode.SHADOW, 500000)},
        {"reflection_model": None}, {"retrieval": object()}):
        values = dict(reflection_model=h[0]._reflection.model, reflection_mode=h[0]._reflection.mode,
            feedback_mode=Mode.ENABLED, budget_settings=FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED, 500000))
        values.update(changes)
        retrieval = values.pop("retrieval", h[0]._retrieval)
        if not changes:
            values.pop("reflection_mode")
        with pytest.raises(ValueError, match="requires"):
            GroundedFollowUpService(h[0]._checkpoint, retrieval, h[0]._model, **values)


@pytest.mark.asyncio
async def test_real_strict_vector_failure_is_not_empty_evidence_or_success():
    h = await harness(Q, GAP)
    search = h[-1]._vector_index.search
    async def outage(*a, **kw):
        if current_retrieval_budget().repair_rounds:
            raise OSError("strict vector fixture outage")
        return await search(*a, **kw)
    h[-1]._vector_index.search = outage
    with capture_feedback_retrieval() as obs, capture_retrieval_budget() as budget:
        answer = await ask(h)
    assert obs[0].state == "RETRIEVAL_FAILED" and obs[0].new_segments == 0
    assert h[4]["telemetry_vectorStoreFallbacks"] == 1 and budget[0]["repair_base_calls"] == 1
    assert "作者选择 B。" in answer


@pytest.mark.asyncio
async def test_accidental_second_physical_repair_is_terminal_before_second_planner():
    h = await harness(Q, GAP, memory=True)
    base = h[0]._retrieval.search_evidence_baseline
    async def nested(*a, **kw):
        await base(*a, **kw)
        return await base(*a, **kw)
    h[0]._retrieval.search_evidence_baseline = nested
    with capture_retrieval_budget() as budget, pytest.raises(FollowUpFailure, match="budget_exceeded"):
        await ask(h, user_id=1, conversation_id=str(uuid4()), request_id=str(uuid4()))
    assert budget[0]["repair_base_calls"] == 1 and h[4]["planner_logical"] == 2 and not h[4]["answer_http"]


@pytest.mark.asyncio
async def test_unknown_custom_backend_cannot_reset_or_fake_base_meter():
    h = await harness(Q, GAP)
    # A custom ranking override cannot inherit the capability marker merely
    # because it subclasses the service; it might skip the counted entry.
    class Unmetered(type(h[-1])):
        async def _rank(self, *a, **kw):
            return ()
    h[0]._retrieval._retrieval.baseline.__class__ = Unmetered
    assert not h[0]._retrieval.supports_feedback_retrieval_budget


@pytest.mark.asyncio
async def test_complete_source_scope_rejects_bad_chunks_or_original_digest():
    h = await harness(Q, GAP)
    context, chunks = h[1:3]
    for field, value in (("chunk_id", "other"), ("source_revision", "other"), ("chunking_version", "other")):
        foreign = (chunks[0].model_copy(update={field: value}),) + chunks[1:]
        with pytest.raises(ValueError):
            TrustedRepairScope.create(42, context, foreign)
    bad_segment = context.segments[0].model_copy(update={"transcript": "forged text"})
    bad_context = context.model_copy(update={"segments": (bad_segment,) + context.segments[1:]})
    bad_chunks = tuple(c.model_copy(update={"raw_segments": tuple(bad_segment if s.segment_id == bad_segment.segment_id else s for s in c.raw_segments)}) for c in chunks)
    with pytest.raises(ValueError, match="digest"):
        TrustedRepairScope.create(42, bad_context, bad_chunks)


@pytest.mark.asyncio
async def test_empty_initial_evidence_stops_without_repair_or_second_grader():
    h = await harness(Q, GAP)
    search = h[0]._retrieval.search_evidence
    async def empty(*a, **kw):
        await search(*a, **kw)
        return ()
    h[0]._retrieval.search_evidence = empty
    with capture_retrieval_budget() as budgets, pytest.raises(FollowUpFailure, match="no_evidence"):
        await ask(h)
    assert budgets[0]["total_base_calls"] == 1 and h[4]["reflection_http"] == 0 and h[4]["answer_http"] == 0


@pytest.mark.asyncio
async def test_feedback_shadow_memory_state_equal_to_off_and_no_extra_calls():
    states, counts = [], []
    for mode in (Mode.OFF, Mode.SHADOW):
        h = await harness(Q, GAP, mode=mode, memory=True)
        identity = ConversationIdentity.create(1, 42, "视频学习", AnalysisMode.LEARNING, str(uuid4()), h[1])
        await ask(h, user_id=1, conversation_id=identity.conversation_id, request_id=str(uuid4()))
        state = await h[0]._memory.store.load(identity)
        states.append(state.turns[0].model_dump(exclude={"turn_id", "created_at_ms", "created_at"}))
        counts.append(h[4])
    assert states[0] == states[1] and counts[0] == counts[1]


@pytest.mark.asyncio
async def test_critic_x1_baseline_scope_uses_existing_m2_suppression_without_feedback_loop():
    question = "作者比较 A 和 B 的内存开销如何；开头推荐谁；结尾推荐谁？"
    h = await harness(question, insufficient("A 和 B 的内存开销"), parts=("A 和 B 的内存开销如何", "开头推荐谁", "结尾推荐谁"))
    with baseline_retrieval(), capture_feedback_retrieval() as feedback:
        await h[0]._retrieval.search_evidence(42, h[1].model_copy(update={"user_goal": question}), chunks=h[2])
    assert not feedback and h[4]["planner_logical"] == 1 and h[4]["m2_planner"] == 0
    assert current_retrieval_budget() is None
