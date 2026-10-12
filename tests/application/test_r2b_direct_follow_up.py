"""R2B execution contracts; fixture transport is not a real Provider benchmark."""
import asyncio
import json
from uuid import uuid4
import pytest

from test_m1_conversation_memory import harness, ask
from test_m1_first_turn_continuity import continuity_harness
from dovideo.domain import AnalysisMode
from dovideo.application.follow_up import FollowUpFailure, FollowUpModelFailure
from dovideo.application.follow_up_budget import FollowUpBudgetSettings, FollowUpBudgetMode, capture_follow_up_budget
from dovideo.application.retrieval_need import RetrievalNeedSettings, RetrievalNeedMode, NeedDecision
from dovideo.infrastructure.providers.follow_up import DirectFollowUpModelAdapter
from dovideo.infrastructure.providers import OpenAICompatibleChatClient, ProviderConfig, ProviderHttpResponse
from dovideo.infrastructure.retrieval_need import validate_direct_memory_configuration, retrieval_need_settings_from_environment

QUESTION = "不考虑当前视频，解释 Redis 是什么。"
ANSWER = "Redis 是一种内存数据存储，常用于缓存。"


class Http:
    def __init__(self, payload=None, statuses=(200,), error=None, callback=None):
        self.payload = payload if payload is not None else {"answer": ANSWER}
        self.statuses, self.error, self.callback, self.calls = statuses, error, callback, []

    async def post(self, url, **kwargs):
        self.calls.append(kwargs["json"])
        if self.callback: await self.callback()
        if self.error: raise self.error
        status = self.statuses[min(len(self.calls)-1, len(self.statuses)-1)]
        content = self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)
        return ProviderHttpResponse(status, {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 20}})


def enabled(h=None, http=None, writer=True, attempts=1):
    h = h or harness()
    http = http or Http()
    h[4].store.write_sources = writer
    h[0]._retrieval_need_settings = RetrievalNeedSettings(RetrievalNeedMode.ENABLED)
    h[0]._direct_model = DirectFollowUpModelAdapter(OpenAICompatibleChatClient(
        ProviderConfig(base_url="https://example.test/v1", model="fixture", max_attempts=attempts, retry_delay_seconds=0), client=http))
    h[0]._budget_settings = FollowUpBudgetSettings(FollowUpBudgetMode.SHADOW)
    return h, http


def test_configuration_requires_writer_and_retains_default_off():
    assert retrieval_need_settings_from_environment({}).mode == RetrievalNeedMode.OFF
    settings = retrieval_need_settings_from_environment({"DOVIDEO_RETRIEVAL_NEED_MODE": "ENABLED"})
    h = harness()
    with pytest.raises(ValueError, match="source writing"):
        validate_direct_memory_configuration(settings, h[4].store)
    h[4].store.write_sources = True
    validate_direct_memory_configuration(settings, h[4].store)


@pytest.mark.asyncio
async def test_direct_zero_chunks_retrieval_and_source_safe_idempotent_memory():
    h, http = enabled()
    async def forbidden(*a, **k): raise AssertionError("DIRECT cannot read chunks or reach retrieval")
    h[1].load_chunks = forbidden
    h[2].search_evidence = forbidden
    rid = str(uuid4())
    with capture_follow_up_budget() as records:
        assert await ask(h, QUESTION, rid) == ANSWER
        assert await ask(h, QUESTION, rid) == ANSWER
    assert len(http.calls) == 1 and h[3].calls == [] and h[2].queries == []
    assert records[0]["stages"] == {"DIRECT_FOLLOW_UP": 1}
    assert records[0]["charged_chat_tokens"] == 20 and records[0]["component_calls"] == {}
    assert http.calls[0]["max_tokens"] == 2048
    messages = http.calls[0]["messages"]
    assert json.loads(messages[-1]["content"]) == {"question": QUESTION}
    assert h[1].context.segments[0].transcript not in json.dumps(messages, ensure_ascii=False)
    state = await h[4].store.load(h[5])
    assert state.turns[0].answer_kind == "MODEL_KNOWLEDGE"
    assert state.turns[0].source_revision == h[5].source_revision
    with pytest.raises(FollowUpFailure, match="conversation_conflict"):
        await ask(h, "不同问题", rid)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [RetrievalNeedMode.OFF, RetrievalNeedMode.SHADOW])
async def test_off_shadow_keep_exact_original_video_answer(mode):
    base = harness()
    h, http = enabled(writer=False)
    h[0]._retrieval_need_settings = RetrievalNeedSettings(mode)
    assert await ask(h, QUESTION) == await ask(base, QUESTION)
    assert h[2].queries == base[2].queries == [QUESTION]
    assert h[3].calls == base[3].calls and http.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["作者讲了什么？", "作者讲得正确吗？", "Redis 是什么？"])
async def test_video_mixed_ambiguous_retain_retrieval(question):
    h, http = enabled()
    assert "视频证据" in await ask(h, question)
    assert h[2].queries == [question] and http.calls == []
    assert (await h[4].store.load(h[5])).turns[0].answer_kind == "VIDEO_GROUNDED"


@pytest.mark.asyncio
async def test_writer_disabled_defers_to_video_no_untagged_direct():
    h, http = enabled(writer=False)
    assert "视频证据" in await ask(h, QUESTION)
    assert http.calls == [] and h[2].queries == [QUESTION]
    assert (await h[4].store.load(h[5])).turns[0].answer_kind == "LEGACY_UNKNOWN"


@pytest.mark.asyncio
async def test_rewrite_and_invalid_or_forged_policy_cannot_authorize_direct():
    h, http = enabled(continuity_harness())
    assert "视频证据" in await ask(h, "第二点呢？")
    assert http.calls == [] and h[3].calls[0][0] == "QUERY_REWRITE"
    class Forged:
        def decide(self, value):
            return NeedDecision(need="DIRECT_LLM", reason_code="EXPLICIT_GENERAL_KNOWLEDGE", decision_source="RULE")
    h[0]._retrieval_need_policy = Forged()
    assert "视频证据" in await ask(h, "作者讲了什么？")
    assert http.calls == []
    class Broken:
        def decide(self, value): raise ValueError("invalid")
    h[0]._retrieval_need_policy = Broken()
    assert "视频证据" in await ask(h, QUESTION)
    assert http.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"answer": "在 01:20 给出了结论"}, {"answer": ANSWER, "answer_kind": "VIDEO_GROUNDED"},
    {"answer": ANSWER, "evidence": []}, {"answer": "视频中作者说是缓存"}, '{"answer":"a","answer":"b"}', 'broken', {"answer": ""}])
async def test_invalid_direct_output_never_saved_or_retrieved(payload):
    h, http = enabled(http=Http(payload))
    with pytest.raises(FollowUpFailure, match="invalid_response"):
        await ask(h, QUESTION)
    assert len(http.calls) == 1 and h[2].queries == []
    assert (await h[4].store.load(h[5])).version == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error,category", [(TimeoutError(), "timeout"), (asyncio.CancelledError(), None)])
async def test_timeout_cancel_are_terminal_and_release_lease(error, category):
    h, http = enabled(http=Http(error=error))
    with pytest.raises(FollowUpFailure if category else asyncio.CancelledError) as caught:
        await ask(h, QUESTION)
    if category: assert caught.value.category == category
    assert h[2].queries == [] and (await h[4].store.load(h[5])).version == 0
    assert await h[4].store.acquire(h[5], "released")
    await h[4].store.release(h[5], "released")


@pytest.mark.asyncio
async def test_budget_admission_before_http_and_retry_usage():
    h, http = enabled(http=Http(statuses=(503, 200)), attempts=2)
    with capture_follow_up_budget() as records:
        assert await ask(h, QUESTION) == ANSWER
    assert len(http.calls) == 2 and records[0]["charged_chat_tokens"] == 40
    h, http = enabled()
    h[0]._budget_settings = FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED, 1)
    with pytest.raises(FollowUpFailure, match="budget_exceeded"):
        await ask(h, QUESTION)
    assert http.calls == [] and h[2].queries == [] and (await h[4].store.load(h[5])).version == 0


@pytest.mark.asyncio
async def test_revision_change_and_cas_failures_keep_existing_semantics():
    h, http = enabled()
    async def change():
        h[1].context = h[1].context.model_copy(update={"source": "changed"})
    http.callback = change
    with pytest.raises(FollowUpFailure, match="conversation_conflict"):
        await ask(h, QUESTION)
    assert (await h[4].store.load(h[5])).version == 0
    h, http = enabled()
    async def fail(*a, **kw): return False
    h[4].store.commit = fail
    assert "暂未保存" in await ask(h, QUESTION)
    assert (await h[4].store.load(h[5])).version == 0


@pytest.mark.asyncio
async def test_alternating_sources_summary_and_rollback_writer_dependency():
    h, http = enabled()
    for i in range(10):
        await ask(h, QUESTION if i % 2 == 0 else "作者讲了什么？")
    state = await h[4].store.load(h[5])
    assert set(state.summary.source_kinds) == {"MODEL_KNOWLEDGE", "VIDEO_GROUNDED"}
    assert [t.answer_kind for t in state.turns] == ["MODEL_KNOWLEDGE", "VIDEO_GROUNDED"] * 3
    h[0]._retrieval_need_settings = RetrievalNeedSettings()
    assert "视频证据" in await ask(h, "作者讲了什么？")
    saved = await h[4].store.load(h[5])
    h[4].store.write_sources = False
    assert "暂未保存" in await ask(h, "作者讲了什么？")
    assert await h[4].store.load(h[5]) == saved


@pytest.mark.asyncio
async def test_concurrent_direct_video_requests_isolate_budget_and_source():
    direct, http = enabled()
    video, _ = enabled()
    async def run(h, question):
        with capture_follow_up_budget() as records:
            answer = await ask(h, question)
        return answer, records
    d, v = await asyncio.gather(run(direct, QUESTION), run(video, "作者讲了什么？"))
    assert d[0] == ANSWER and "视频证据" in v[0]
    assert d[1][0]["stages"] == {"DIRECT_FOLLOW_UP": 1} and v[1][0]["stages"] == {}
    assert (await direct[4].store.load(direct[5])).turns[0].answer_kind == "MODEL_KNOWLEDGE"
    assert (await video[4].store.load(video[5])).turns[0].answer_kind == "VIDEO_GROUNDED"


@pytest.mark.asyncio
async def test_same_conversation_concurrent_duplicate_lease_and_replay():
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold():
        entered.set()
        await release.wait()
    h, http = enabled(http=Http(callback=hold))
    rid = str(uuid4())
    first = asyncio.create_task(ask(h, QUESTION, rid))
    await entered.wait()
    try:
        with pytest.raises(FollowUpFailure, match="conversation_conflict"):
            await ask(h, QUESTION, rid)
    finally:
        release.set()
    assert await first == ANSWER
    assert await ask(h, QUESTION, rid) == ANSWER
    assert len(http.calls) == 1 and len((await h[4].store.load(h[5])).turns) == 1


@pytest.mark.asyncio
async def test_provider_failure_and_expired_request_deadline_do_not_save():
    h, http = enabled(http=Http(statuses=(503,)), attempts=2)
    with pytest.raises(FollowUpFailure, match="provider_failure"):
        await ask(h, QUESTION)
    assert len(http.calls) == 2 and h[2].queries == [] and (await h[4].store.load(h[5])).version == 0
    h, http = enabled()
    async def delayed(): await asyncio.sleep(0.03)
    http.callback = delayed
    h[0]._max_wall_seconds = 0.01
    with pytest.raises(FollowUpFailure, match="timeout"):
        await ask(h, QUESTION)
    assert (await h[4].store.load(h[5])).version == 0 and h[2].queries == []


@pytest.mark.asyncio
async def test_stateless_zero_chunks_and_context_readiness():
    h, http = enabled(writer=False)
    async def forbidden(*a, **kw): raise AssertionError("no chunks")
    h[1].load_chunks = forbidden
    assert await h[0].answer(42, QUESTION, None, AnalysisMode.LEARNING) == ANSWER
    assert h[2].queries == [] and len(http.calls) == 1
    h[1].context = h[1].context.model_copy(update={"segments": ()})
    with pytest.raises(FollowUpFailure, match="context_not_ready"):
        await h[0].answer(42, QUESTION, None, AnalysisMode.LEARNING)
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_writer_lost_during_generation_cannot_save_legacy_direct():
    h, http = enabled()
    async def disable(): h[4].store.write_sources = False
    http.callback = disable
    with pytest.raises(FollowUpFailure, match="memory_unavailable"):
        await ask(h, QUESTION)
    assert (await h[4].store.load(h[5])).version == 0


@pytest.mark.asyncio
async def test_deterministic_ab_uses_real_hybrid_and_component_spies():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("r2b_comparison", Path(__file__).resolve().parents[2] / "tools/run_r2b_direct_comparison.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for question in [QUESTION, "作者如何比较 Redis 和 MySQL？", "作者讲得正确吗？"]:
        case = {"input": {"original_question": question}}
        a = await module.run_case(case, RetrievalNeedMode.OFF)
        b = await module.run_case(case, RetrievalNeedMode.ENABLED)
        assert a["calls"]["hybrid_search"] > 0 and a["calls"]["embedding"] > 0
        assert a["calls"]["vector_query_spy"] > 0 and a["calls"]["bm25"] > 0 and a["calls"]["reranker"] > 0
        if question == QUESTION:
            assert not any(b["calls"].values()) and b["kind"] == "MODEL_KNOWLEDGE"
        else:
            assert a["calls"] == b["calls"] and b["kind"] == "VIDEO_GROUNDED"
