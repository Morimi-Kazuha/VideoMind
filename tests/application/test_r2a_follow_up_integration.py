"""Existing application roles over the real chat transport with fixture HTTP."""
import json
from uuid import uuid4
import pytest
from dovideo.application.follow_up import FollowUpFailure
from dovideo.application.follow_up_budget import (
    FollowUpBudgetMode as Mode, FollowUpBudgetSettings as Settings,
    capture_follow_up_budget, follow_up_budget_scope, FollowUpBudgetExceededError,
)
from dovideo.application.execution_budget import AgentExecutionBudget
from dovideo.application.errors import DeadlineExceededError
from dovideo.application.retrieval import VideoEvidenceRetrievalService
from dovideo.application.retrieval_need import RetrievalNeedMode, RetrievalNeedSettings, capture_retrieval_need
from dovideo.infrastructure.providers import OpenAICompatibleChatClient, ProviderConfig, ProviderHttpResponse
from dovideo.infrastructure.providers.follow_up import GroundedFollowUpModelAdapter
from dovideo.infrastructure.providers.conversation_memory import ConversationModelAdapter
from dovideo.infrastructure.providers.model import RetrievalPlannerModelAdapter
from test_m1_conversation_memory import harness, ask, TEXT


class FixtureHttp:
    def __init__(self, old_chat, usage=20):
        self.old_chat = old_chat
        self.usage = usage
        self.calls = []

    async def post(self, url, *, headers, json, timeout):
        self.calls.append(json)
        user = json["messages"][-1]["content"]
        if "Resolve references" in user:
            stage = "QUERY_REWRITE"
        elif "Merge previous summary" in user:
            stage = "ROLLING_SUMMARY"
        else:
            stage = "FOLLOW_UP"
        content = await self.old_chat.complete(json["messages"], stage=stage)
        return ProviderHttpResponse(200, {"choices": [{"message": {"content": __import__("json").dumps(content, ensure_ascii=False)}}],
            "usage": {"total_tokens": self.usage}})


def wire(h, settings, usage=20):
    http = FixtureHttp(h[3], usage)
    chat = OpenAICompatibleChatClient(ProviderConfig(base_url="https://example.test/v1", model="test", max_attempts=1), client=http)
    h[0]._model = GroundedFollowUpModelAdapter(chat)
    h[4].model = ConversationModelAdapter(chat)
    h[0]._budget_settings = settings
    return http


@pytest.mark.asyncio
async def test_off_shadow_pair_preserves_exact_answer_prompts_retrieval_and_memory_shape():
    off, shadow = harness(), harness()
    one = wire(off, Settings())
    two = wire(shadow, Settings(Mode.SHADOW, 1))
    shadow[0]._retrieval_need_settings = RetrievalNeedSettings(mode=RetrievalNeedMode.SHADOW)
    answers = []
    with capture_follow_up_budget() as records, capture_retrieval_need() as routing:
        for i in range(10):
            question = "不考虑当前视频，解释 Redis 是什么。" if i == 0 else (
                "它和 MySQL 相比有什么不同？" if i == 1 else f"请解释数据库 {i}")
            baseline = await ask(off, question)
            actual = await ask(shadow, question)
            assert baseline == actual and TEXT in actual and "视频证据" in actual
            answers.append(actual)
    assert len(one.calls) == len(two.calls) == 12
    assert one.calls == two.calls  # provider payloads byte-for-byte equivalent
    assert off[2].queries == shadow[2].queries
    assert len(routing) == 10
    assert routing[0].decision.need.value == "DIRECT_LLM"
    assert len(records) == 10 and sum(r["chat_http_attempts"] for r in records) == 12
    assert sum(r["charged_chat_tokens"] for r in records) == 240
    stages = [stage for h in records for stage in h["stages"]]
    assert set(stages) == {"QUERY_REWRITE", "FOLLOW_UP", "ROLLING_SUMMARY"}
    for h in [off, shadow]:
        state = await h[4].store.load(h[5])
        assert state.summary.source_kinds == []
        assert all(t.answer_kind == "LEGACY_UNKNOWN" for t in state.turns)
        assert "answer_kind" not in state.model_dump_json() and "source_kinds" not in state.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["请解释数据库", "它和 MySQL 相比有什么不同？"])
async def test_denial_in_answer_or_rewrite_does_not_continue_call_or_commit_and_releases_lease(question):
    h = harness()
    await ask(h)
    before = await h[4].store.load(h[5])
    http = wire(h, Settings(Mode.ENFORCED, 1))
    previous_searches = len(h[2].queries)
    with pytest.raises(FollowUpFailure) as caught:
        await ask(h, question)
    assert caught.value.category == "budget_exceeded"
    assert http.calls == [] and await h[4].store.load(h[5]) == before
    if question.startswith("它"):
        assert len(h[2].queries) == previous_searches
    assert await h[4].store.acquire(h[5], "after-denial")
    await h[4].store.release(h[5], "after-denial")


@pytest.mark.asyncio
async def test_summary_denial_is_terminal_and_not_optional_fallback_or_partial_commit():
    h = harness()
    for i in range(9):
        await ask(h, f"请解释数据库 {i}")
    before = await h[4].store.load(h[5])
    http = wire(h, Settings(Mode.ENFORCED, 20000), usage=18000)
    with pytest.raises(FollowUpFailure) as caught:
        await ask(h, "请解释数据库 10")
    assert caught.value.category == "budget_exceeded"
    assert len(http.calls) == 1  # summary denied before HTTP
    assert await h[4].store.load(h[5]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", [False, True])
async def test_retrieval_planner_terminal_does_not_fallback_to_embedding(deadline):
    class Http:
        calls = 0
        async def post(self, *a, **k):
            self.calls += 1
            raise AssertionError("denial must precede HTTP")
    class Embed:
        calls = 0
        async def embed(self, text):
            self.calls += 1
            return (.1, .2)
    http, embed = Http(), Embed()
    chat = OpenAICompatibleChatClient(ProviderConfig(base_url="https://example.test/v1", model="test"), client=http)
    retrieval = VideoEvidenceRetrievalService(RetrievalPlannerModelAdapter(chat), embed, object())
    clock = [0.0]
    with follow_up_budget_scope(Settings(Mode.ENFORCED, 1)), AgentExecutionBudget.open(100, monotonic=lambda: clock[0]):
        if deadline:
            clock[0] = 1
        with pytest.raises(DeadlineExceededError if deadline else FollowUpBudgetExceededError):
            await retrieval.search(42, "Redis", harness()[1].chunks)
    assert http.calls == 0 and embed.calls == 0


@pytest.mark.asyncio
async def test_real_retrieval_and_adaptive_adapters_share_the_request_budget():
    class Http:
        calls = 0
        async def post(self, url, *, headers, json, timeout):
            self.calls += 1
            if self.calls == 1:
                content = '{"semanticQuery":"Redis","keywords":["Redis"],"visualKeywords":[]}'
            else:
                content = '{"retrieval_route":"SINGLE_HYBRID","reason_code":"SINGLE_FACT","sub_queries":[]}'
            return ProviderHttpResponse(200, {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 17}})
    http = Http()
    chat = OpenAICompatibleChatClient(ProviderConfig(base_url="https://example.test/v1", model="test"), client=http)
    planner = RetrievalPlannerModelAdapter(chat)
    with capture_follow_up_budget() as records, follow_up_budget_scope(Settings(Mode.SHADOW)):
        assert (await planner.plan_retrieval("Redis")).semantic_query == "Redis"
        await planner.suggest_retrieval("Redis")
    assert http.calls == records[0]["chat_http_attempts"] == 2
    assert records[0]["charged_chat_tokens"] == 34
    assert records[0]["stages"] == {"RETRIEVAL_PLANNER": 1, "ADAPTIVE_RETRIEVAL_PLANNER": 1}
