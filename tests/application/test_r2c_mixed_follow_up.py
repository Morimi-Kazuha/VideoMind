"""Actual two-stage application execution with fixture HTTP, not model quality."""
import asyncio
import json
from uuid import uuid4
import pytest
from test_m1_conversation_memory import harness, ask, TEXT
from test_r2b_direct_follow_up import enabled, QUESTION, ANSWER
from test_p3_grounded_follow_up import _provenance_fixture
from dovideo.domain import AnalysisMode, VideoChunk
from dovideo.application.follow_up import FollowUpFailure
from dovideo.application.follow_up_outcome import KnowledgeBlock
from dovideo.application.conversation_memory import ConversationIdentity
from dovideo.application.follow_up_budget import FollowUpBudgetSettings, FollowUpBudgetMode, capture_follow_up_budget
from dovideo.application.retrieval_need import RetrievalNeedMode, RetrievalNeedSettings
from dovideo.infrastructure.providers import OpenAICompatibleChatClient, ProviderConfig, ProviderHttpResponse
from dovideo.infrastructure.providers.follow_up import GroundedFollowUpModelAdapter, MixedKnowledgeModelAdapter, DirectFollowUpModelAdapter
from dovideo.infrastructure.providers.conversation_memory import ConversationModelAdapter
from dovideo.infrastructure.retrieval_need import mixed_follow_up_enabled_from_environment, validate_direct_memory_configuration

MIXED_Q = "作者讲的 Redis 机制正确吗？"
BLOCKS = [{"kind": "MODEL_KNOWLEDGE", "text": "Redis 的持久化方式包括 RDB 和 AOF；细节随版本变化。"},
          {"kind": "INFERENCE", "text": "仅凭所给引文不能认定持久化解释完整，需结合配置进一步判断。"}]


class MixedHttp:
    def __init__(self, old_chat):
        self.old_chat, self.calls, self.usage_records = old_chat, [], []
        self.payload = {"knowledge_blocks": BLOCKS}
        self.errors, self.statuses, self.callbacks = {}, {}, {}
        self.fact_override = None
        self.usage = {}
    async def post(self, url, **kw):
        payload = kw["json"]
        system, user = payload["messages"][0]["content"], payload["messages"][-1]["content"]
        if "source quotes" in system: stage = "MIXED_KNOWLEDGE"
        elif "You have not viewed" in system: stage = "DIRECT_FOLLOW_UP"
        elif "Resolve references" in user: stage = "QUERY_REWRITE"
        elif "Merge previous summary" in user: stage = "ROLLING_SUMMARY"
        else: stage = "FOLLOW_UP"
        self.calls.append((stage, payload))
        if stage in self.callbacks: await self.callbacks[stage]()
        if stage in self.errors: raise self.errors[stage]
        if stage == "MIXED_KNOWLEDGE": content = self.payload
        elif stage == "DIRECT_FOLLOW_UP": content = {"answer": ANSWER}
        elif stage == "FOLLOW_UP" and self.fact_override is not None: content = self.fact_override
        else: content = await self.old_chat.complete(payload["messages"], stage=stage)
        statuses = self.statuses.get(stage, [200])
        status = statuses[min(sum(s == stage for s, _ in self.calls)-1, len(statuses)-1)]
        usage = self.usage.get(stage, {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20})
        self.usage_records.append(usage)
        return ProviderHttpResponse(status, {"choices": [{"message": {"content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)}}],
            "usage": usage})


def mixed_harness(h=None, attempts=1):
    h, _ = enabled(h)
    http = MixedHttp(h[3])
    chat = OpenAICompatibleChatClient(ProviderConfig(base_url="https://example.test/v1", model="fixture", max_attempts=attempts, retry_delay_seconds=0), client=http)
    h[0]._model = GroundedFollowUpModelAdapter(chat)
    h[0]._direct_model = DirectFollowUpModelAdapter(chat)
    h[0]._mixed_knowledge_model = MixedKnowledgeModelAdapter(chat)
    h[0]._mixed_enabled = True
    h[4].model = ConversationModelAdapter(chat)
    return h, http


def test_mixed_flag_is_server_side_default_off_and_writer_dependency():
    assert mixed_follow_up_enabled_from_environment({}) is False
    assert mixed_follow_up_enabled_from_environment({"DOVIDEO_MIXED_FOLLOW_UP_ENABLED": " true "}) is True
    for bad in ["yes", "", None, True]:
        with pytest.raises(ValueError): mixed_follow_up_enabled_from_environment({"DOVIDEO_MIXED_FOLLOW_UP_ENABLED": bad})
    with pytest.raises(ValueError): validate_direct_memory_configuration(RetrievalNeedSettings(RetrievalNeedMode.ENABLED), harness()[4].store)


@pytest.mark.asyncio
async def test_mixed_verify_before_knowledge_source_separation_memory_and_replay():
    h, http = mixed_harness()
    rid = str(uuid4())
    with capture_follow_up_budget() as records:
        answer = await ask(h, MIXED_Q, rid)
        assert await ask(h, MIXED_Q, rid) == answer
    assert "## 视频事实" in answer and "## 通用知识与推理" in answer and "通用知识：" in answer and "推论：" in answer
    assert "[00:00–00:10] ASR" in answer
    assert [s for s, _ in http.calls] == ["FOLLOW_UP", "MIXED_KNOWLEDGE"]
    assert records[0]["stages"] == {"FOLLOW_UP": 1, "MIXED_KNOWLEDGE": 1} and records[0]["charged_chat_tokens"] == 40
    assert http.calls[1][1]["max_tokens"] == 2048
    fact_prompt = http.calls[0][1]["messages"][-1]["content"]
    assert fact_prompt.startswith("FACT EXTRACTION ONLY") and "conversationContext" not in fact_prompt
    analysis_payload = json.loads(http.calls[1][1]["messages"][-1]["content"])
    assert analysis_payload == {"question": MIXED_Q, "sourceQuotes": [TEXT]}
    assert h[2].queries == [MIXED_Q]
    state = await h[4].store.load(h[5])
    assert state.turns[0].answer_kind == "MIXED" and state.turns[0].source_revision == h[5].source_revision
    with pytest.raises(FollowUpFailure, match="conversation_conflict"): await ask(h, "其他问题", rid)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,on,question,kind,stages", [
    ("OFF", True, MIXED_Q, "VIDEO_GROUNDED", ["FOLLOW_UP"]),
    ("SHADOW", True, MIXED_Q, "VIDEO_GROUNDED", ["FOLLOW_UP"]),
    ("ENABLED", False, MIXED_Q, "VIDEO_GROUNDED", ["FOLLOW_UP"]),
    ("ENABLED", True, "作者讲了什么？", "VIDEO_GROUNDED", ["FOLLOW_UP"]),
    ("ENABLED", True, "Redis 是什么？", "VIDEO_GROUNDED", ["FOLLOW_UP"]),
    ("ENABLED", True, QUESTION, "MODEL_KNOWLEDGE", ["DIRECT_FOLLOW_UP"]),
])
async def test_modes_keep_existing_video_and_direct(mode, on, question, kind, stages):
    h, http = mixed_harness()
    h[0]._retrieval_need_settings = RetrievalNeedSettings(RetrievalNeedMode(mode))
    h[0]._mixed_enabled = on
    await ask(h, question)
    assert [s for s, _ in http.calls] == stages
    assert (await h[4].store.load(h[5])).turns[0].answer_kind == kind
    assert h[2].queries == ([] if kind == "MODEL_KNOWLEDGE" else [question])


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["ASR", "OCR"])
@pytest.mark.parametrize("fault", [None, "timestamp", "revision", "source_items", "quote", "claim", "unsupported_prose"])
async def test_mixed_provenance_and_whole_fact_block_guard(source, fault):
    h = list(harness())
    context, hit, response = _provenance_fixture(2, source=source)
    h[1].context = context
    h[1].chunks = (VideoChunk(start_ms=0, end_ms=300000, segment_summary="fixture", keywords=("Redis",), raw_segments=context.segments),)
    h[5] = ConversationIdentity.create(1, 42, "学习存储技术", AnalysisMode.LEARNING, str(uuid4()), context)
    if fault == "timestamp": response = response.model_copy(update={"evidence": (response.evidence[0].model_copy(update={"timestamp_ms": 99999}),)})
    if fault == "revision": hit = hit.model_copy(update={"source_revision": "other"})
    if fault == "source_items": hit = hit.model_copy(update={"source_item_ids": ("other-item",)})
    if fault == "quote": response = response.model_copy(update={"evidence": (response.evidence[0].model_copy(update={"content": "外部知识"}),)})
    if fault == "claim": response = response.model_copy(update={"answer": "作者支持某新机制", "evidence": (response.evidence[0].model_copy(update={"claim": "作者支持某新机制"}),)})
    if fault == "unsupported_prose": response = response.model_copy(update={"answer": response.answer + " 外部知识保证正确。"})
    async def search(*a, **kw): return (hit,)
    h[2].search_evidence = search
    h, http = mixed_harness(tuple(h))
    http.fact_override = response.model_dump()
    if fault:
        with pytest.raises(FollowUpFailure): await ask(h, MIXED_Q)
        assert [s for s, _ in http.calls] == ["FOLLOW_UP"]
        assert (await h[4].store.load(h[5])).version == 0
    else:
        assert source in await ask(h, MIXED_Q)
        assert [s for s, _ in http.calls] == ["FOLLOW_UP", "MIXED_KNOWLEDGE"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"knowledge_blocks": [{"kind": "INFERENCE", "text": "作者指出新的机制"}]},
    {"knowledge_blocks": [{"kind": "MODEL_KNOWLEDGE", "text": "[01:20] 来源"}]},
    {"knowledge_blocks": [{"kind": "INFERENCE", "text": "The speaker claims a new mechanism."}]},
    {"knowledge_blocks": [{"kind": "INFERENCE", "text": "结论", "sourceItemId": "fake"}]},
    {"knowledge_blocks": BLOCKS, "answer_kind": "VIDEO_VERIFIED"},
    {"knowledge_blocks": []}, {"knowledge_blocks": BLOCKS * 3},
    {"knowledge_blocks": [{"kind": "INFERENCE", "text": "x" * 4001}]},
    'broken', '{"knowledge_blocks":[],"knowledge_blocks":[]}',
])
async def test_knowledge_invalid_fields_citations_and_bounds_fail_without_memory(payload):
    h, http = mixed_harness()
    http.payload = payload
    with pytest.raises(FollowUpFailure, match="invalid_response"): await ask(h, MIXED_Q)
    assert len(http.calls) == 2 and len(h[2].queries) == 1 and (await h[4].store.load(h[5])).version == 0


@pytest.mark.asyncio
async def test_typed_construct_bypass_is_revalidated_at_application_factory():
    h, http = mixed_harness()
    class Bad:
        async def analyze(self, *a): return (KnowledgeBlock.model_construct(kind="INFERENCE", text="视频中展示新事实"),)
    h[0]._mixed_knowledge_model = Bad()
    with pytest.raises(FollowUpFailure, match="invalid_response"): await ask(h, MIXED_Q)
    assert (await h[4].store.load(h[5])).version == 0


@pytest.mark.asyncio
async def test_budget_prevents_knowledge_http_after_video_and_retry_accounting():
    h, http = mixed_harness()
    h[0]._budget_settings = FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED, 20000)
    http.usage["FOLLOW_UP"] = {"total_tokens": 19000}
    with pytest.raises(FollowUpFailure, match="budget_exceeded"): await ask(h, MIXED_Q)
    assert [s for s, _ in http.calls] == ["FOLLOW_UP"] and (await h[4].store.load(h[5])).version == 0
    h, http = mixed_harness(attempts=2)
    http.statuses["MIXED_KNOWLEDGE"] = [503, 200]
    with capture_follow_up_budget() as records: await ask(h, MIXED_Q)
    assert records[0]["charged_chat_tokens"] == 60 and records[0]["stages"] == {"FOLLOW_UP": 1, "MIXED_KNOWLEDGE": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,error", [("FOLLOW_UP", TimeoutError()), ("MIXED_KNOWLEDGE", TimeoutError()), ("MIXED_KNOWLEDGE", asyncio.CancelledError())])
async def test_terminal_stage_failures_release_lease_no_complete_mixed(stage, error):
    h, http = mixed_harness()
    http.errors[stage] = error
    with pytest.raises(asyncio.CancelledError if isinstance(error, asyncio.CancelledError) else FollowUpFailure): await ask(h, MIXED_Q)
    assert (await h[4].store.load(h[5])).version == 0 and len(h[2].queries) == 1
    assert await h[4].store.acquire(h[5], "released")
    await h[4].store.release(h[5], "released")


@pytest.mark.asyncio
async def test_no_evidence_strict_failure_and_writer_unavailable_do_not_mix():
    for fault in ["empty", "strict", "writer"]:
        h, http = mixed_harness()
        async def search(*a, **kw):
            if fault == "strict": raise RuntimeError("strict backend failure")
            return ()
        if fault == "writer":
            h[4].store.write_sources = False
            assert "视频证据" in await ask(h, MIXED_Q)
            assert [s for s, _ in http.calls] == ["FOLLOW_UP"]
        else:
            h[2].search_evidence = search
            with pytest.raises(FollowUpFailure, match="no_evidence" if fault == "empty" else "retrieval_failure"): await ask(h, MIXED_Q)
            assert http.calls == []
        assert all(t.answer_kind != "MIXED" for t in (await h[4].store.load(h[5])).turns)


@pytest.mark.asyncio
async def test_three_sources_summary_rewrite_rollback_and_writer_loss():
    h, http = mixed_harness()
    for i in range(10): await ask(h, [MIXED_Q, QUESTION, "作者讲了什么？"][i % 3])
    state = await h[4].store.load(h[5])
    assert set(state.summary.source_kinds) == {"MIXED", "MODEL_KNOWLEDGE", "VIDEO_GROUNDED"}
    assert set(t.answer_kind for t in state.turns) == {"MIXED", "MODEL_KNOWLEDGE", "VIDEO_GROUNDED"}
    before = len(h[2].queries)
    await ask(h, "它为什么更快？")
    assert len(h[2].queries) == before + 1 and http.calls[-2][0] == "QUERY_REWRITE"
    assert "讨论上下文" not in json.dumps(http.calls[-1][1], ensure_ascii=False)
    h[0]._mixed_enabled = False
    assert "## 通用知识" not in await ask(h, MIXED_Q)
    assert any(t.answer_kind == "MIXED" for t in (await h[4].store.load(h[5])).turns)
    h[0]._mixed_enabled = True
    async def disable(): h[4].store.write_sources = False
    http.callbacks["MIXED_KNOWLEDGE"] = disable
    with pytest.raises(FollowUpFailure, match="memory_unavailable"): await ask(h, MIXED_Q)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["revision", "cas", "deleted"])
async def test_post_analysis_revision_cas_and_delete_preserve_source_safety(fault):
    h, http = mixed_harness()
    async def change():
        if fault == "revision": h[1].context = h[1].context.model_copy(update={"source": "changed"})
    http.callbacks["MIXED_KNOWLEDGE"] = change
    if fault == "cas":
        async def commit(*a, **kw): return False
        h[4].store.commit = commit
        assert "暂未保存" in await ask(h, MIXED_Q)
    else:
        if fault == "deleted":
            async def denied(*a):
                if len(http.calls) >= 2: raise PermissionError("deleted")
            h[0]._access_check = denied
        with pytest.raises(PermissionError if fault == "deleted" else FollowUpFailure): await ask(h, MIXED_Q)
    assert (await h[4].store.load(h[5])).version == 0


@pytest.mark.asyncio
async def test_mixed_concurrency_isolates_stages_and_duplicate_receipts():
    mixed, mhttp = mixed_harness()
    direct, dhttp = mixed_harness()
    async def run(h, q):
        with capture_follow_up_budget() as records: answer = await ask(h, q)
        return answer, records[0]
    m, d = await asyncio.gather(run(mixed, MIXED_Q), run(direct, QUESTION))
    assert m[1]["stages"] == {"FOLLOW_UP": 1, "MIXED_KNOWLEDGE": 1}
    assert d[1]["stages"] == {"DIRECT_FOLLOW_UP": 1}
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold():
        entered.set()
        await release.wait()
    h, http = mixed_harness()
    http.callbacks["MIXED_KNOWLEDGE"] = hold
    rid = str(uuid4())
    first = asyncio.create_task(ask(h, MIXED_Q, rid))
    await entered.wait()
    try:
        with pytest.raises(FollowUpFailure, match="conversation_conflict"): await ask(h, MIXED_Q, rid)
    finally: release.set()
    answer = await first
    assert await ask(h, MIXED_Q, rid) == answer and len(http.calls) == 2


@pytest.mark.asyncio
async def test_mixed_first_list_rewrite_is_continuity_only_and_model_failure_has_no_retry_loop():
    from test_m1_first_turn_continuity import continuity_harness
    h, http = mixed_harness(continuity_harness())
    assert "## 通用知识与推理" in await ask(h, "第二点是否合理？")
    assert [s for s, _ in http.calls] == ["QUERY_REWRITE", "FOLLOW_UP", "MIXED_KNOWLEDGE"]
    assert "conversationContext" not in http.calls[1][1]["messages"][-1]["content"]
    h, http = mixed_harness(attempts=2)
    http.statuses["MIXED_KNOWLEDGE"] = [503]
    with pytest.raises(FollowUpFailure, match="provider_failure"): await ask(h, MIXED_Q)
    assert [s for s, _ in http.calls] == ["FOLLOW_UP", "MIXED_KNOWLEDGE", "MIXED_KNOWLEDGE"] and len(h[2].queries) == 1
    assert (await h[4].store.load(h[5])).version == 0
