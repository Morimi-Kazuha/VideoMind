"""Exercise each HTTP attempt, independent clients and terminal propagation."""
import asyncio
import json
import pytest
from dovideo.application.follow_up_budget import (
    FollowUpBudgetMode as Mode, FollowUpBudgetSettings as Settings,
    FollowUpBudgetExceededError, FollowUpRequestBudget, capture_follow_up_budget,
    follow_up_budget_scope, record_request_component,
)
from dovideo.application.errors import BudgetExceededError, DeadlineExceededError
from dovideo.application.execution_budget import AgentExecutionBudget
from dovideo.infrastructure.follow_up_budget import follow_up_budget_settings_from_environment
from dovideo.infrastructure.providers import OpenAICompatibleChatClient, ProviderConfig
from test_r5_model_call_budget import Http, response, TraceStore, bound_client

MESSAGES = [{"role": "user", "content": "private fixture"}]


def client(http, attempts=1):
    return OpenAICompatibleChatClient(ProviderConfig(base_url="https://example.test/v1", model="test",
        max_attempts=attempts, retry_delay_seconds=0), client=http)


@pytest.mark.parametrize("env", [{"DOVIDEO_FOLLOW_UP_BUDGET_MODE": "BAD"},
    {"DOVIDEO_FOLLOW_UP_BUDGET_MODE": "ENFORCED"},
    {"DOVIDEO_FOLLOW_UP_MAX_CHAT_TOKENS": "0"}, {"DOVIDEO_FOLLOW_UP_MAX_CHAT_TOKENS": "50000.0"}])
def test_invalid_settings_and_no_invented_enforced_limit(env):
    with pytest.raises(ValueError):
        follow_up_budget_settings_from_environment(env)


def test_settings_default_off_and_shadow_without_claiming_calibration():
    assert follow_up_budget_settings_from_environment({}) == Settings()
    assert follow_up_budget_settings_from_environment({"DOVIDEO_FOLLOW_UP_BUDGET_MODE": "SHADOW"}) == Settings(Mode.SHADOW)


@pytest.mark.asyncio
async def test_off_does_no_accounting_or_rejection():
    http = Http(response())
    with capture_follow_up_budget() as records, follow_up_budget_scope(Settings(Mode.OFF, 1)):
        assert await client(http).complete(MESSAGES, stage="FOLLOW_UP") == "{}"
    assert records == [] and http.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["QUERY_REWRITE", "FOLLOW_UP", "RETRIEVAL_PLANNER",
    "ADAPTIVE_RETRIEVAL_PLANNER", "ROLLING_SUMMARY", "FUTURE_REFLECTION"])
async def test_shadow_covers_all_chat_stages_without_usage_sink(stage):
    http = Http(response({"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}))
    with capture_follow_up_budget() as records, follow_up_budget_scope(Settings(Mode.SHADOW, 1)):
        assert await client(http).complete(MESSAGES, stage=stage) == "{}"
    data = records[0]
    assert data["chat_http_attempts"] == 1 and data["charged_chat_tokens"] == 20
    assert data["reserved_chat_tokens"] == 0 and data["hypothetical_denials"] == 1
    assert data["events"][1]["usage_source"] == "PROVIDER"
    assert data["estimated_sent_chat_tokens"] == data["events"][1]["reservation"]
    assert data["usage_sources"] == {"PROVIDER": 1}
    assert "private fixture" not in json.dumps(data)


@pytest.mark.asyncio
async def test_enforced_rejection_before_http_and_across_independent_clients():
    one, two = Http(response({"total_tokens": 600})), Http(response())
    with capture_follow_up_budget() as records, follow_up_budget_scope(Settings(Mode.ENFORCED, 1100)):
        await client(one).complete(MESSAGES, stage="QUERY_REWRITE")
        with pytest.raises(FollowUpBudgetExceededError):
            await client(two).complete(MESSAGES, stage="QUERY_REWRITE")
    assert one.calls == 1 and two.calls == 0
    assert records[0]["charged_chat_tokens"] == 600
    assert records[0]["chat_http_attempts"] == 1


def test_parallel_reservations_double_settlement_and_foreign_receipts_are_rejected():
    budget = FollowUpRequestBudget(Settings(Mode.ENFORCED, 100))
    first = budget.admit(MESSAGES, "QUERY_REWRITE", 50)
    with pytest.raises(FollowUpBudgetExceededError):
        budget.admit(MESSAGES, "QUERY_REWRITE", 50)
    budget.finish(first, sent=True, usage={"total_tokens": 30})
    assert budget.charged_tokens == 30 and budget.reserved_tokens == 0
    for owner in [budget, FollowUpRequestBudget(Settings(Mode.SHADOW))]:
        with pytest.raises(ValueError):
            owner.finish(first, sent=True, usage={"total_tokens": 30})
    assert budget.charged_tokens == 30


@pytest.mark.asyncio
async def test_missing_usage_retry_consumes_reservation_before_next_attempt():
    http = Http(response(status=503), response())
    with capture_follow_up_budget() as records, follow_up_budget_scope(Settings(Mode.ENFORCED, 1000)):
        with pytest.raises(FollowUpBudgetExceededError):
            await client(http, 2).complete(MESSAGES, stage="QUERY_REWRITE")
    assert http.calls == 1
    event = records[0]["events"][1]
    assert event["usage_source"] == "ESTIMATE" and event["tokens"] == event["reservation"]
    assert records[0]["reserved_chat_tokens"] == 0


@pytest.mark.asyncio
async def test_successful_retry_actual_usage_is_charged_once_per_sent_attempt():
    http = Http(response({"total_tokens": 10}, status=503), response({"total_tokens": 20}))
    with capture_follow_up_budget() as records, follow_up_budget_scope(Settings(Mode.SHADOW)):
        await client(http, 2).complete(MESSAGES, stage="QUERY_REWRITE")
    assert http.calls == 2
    assert records[0]["charged_chat_tokens"] == 30 and records[0]["chat_http_attempts"] == 2


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": 1}, {"total_tokens": True},
    {"total_tokens": -1}, {"total_tokens": float("nan")}, {"total_tokens": 10**1000}])
def test_missing_invalid_partial_usage_never_releases_all_budget(usage):
    budget = FollowUpRequestBudget(Settings(Mode.SHADOW))
    receipt = budget.admit(MESSAGES, "QUERY_REWRITE", 512)
    reserve = budget.reserved_tokens
    budget.finish(receipt, sent=True, usage=usage)
    assert budget.charged_tokens == reserve and budget.reserved_tokens == 0


@pytest.mark.asyncio
async def test_reported_overrun_is_terminal_before_next_provider_call():
    http = Http(response({"total_tokens": 9999}), response())
    with follow_up_budget_scope(Settings(Mode.ENFORCED, 1000)):
        with pytest.raises(FollowUpBudgetExceededError):
            await client(http).complete(MESSAGES, stage="QUERY_REWRITE")
    assert http.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError(), TimeoutError("provider-local"), BudgetExceededError("terminal")])
async def test_terminal_exceptions_identity_survives_estimated_settlement(error):
    http = Http(error, response())
    with capture_follow_up_budget() as records, follow_up_budget_scope(Settings(Mode.ENFORCED, 1000)):
        with pytest.raises(type(error)) as caught:
            await client(http, 2).complete(MESSAGES, stage="QUERY_REWRITE")
    assert caught.value is error and http.calls == 1
    assert records[0]["charged_chat_tokens"] > 512 and records[0]["reserved_chat_tokens"] == 0


@pytest.mark.asyncio
async def test_concurrent_requests_have_independent_totals_and_nested_scope_cannot_extend():
    async def run(total):
        with capture_follow_up_budget() as records, follow_up_budget_scope(Settings(Mode.SHADOW)) as outer:
            await asyncio.sleep(0)
            with follow_up_budget_scope(Settings(Mode.ENFORCED, 10**6)) as nested:
                assert nested is outer
                await client(Http(response({"total_tokens": total}))).complete(MESSAGES, stage="QUERY_REWRITE")
        return records[0]
    one, two = await asyncio.gather(run(7), run(19))
    assert one["charged_chat_tokens"] == 7 and two["charged_chat_tokens"] == 19
    clock = [0.0]
    with AgentExecutionBudget.open(100, monotonic=lambda: clock[0]), follow_up_budget_scope(Settings(Mode.SHADOW)):
        clock[0] = .05
        with AgentExecutionBudget.open(10000, monotonic=lambda: clock[0]):
            assert AgentExecutionBudget.remaining_ms() == pytest.approx(50)
            clock[0] = .2
            http = Http(response())
            with pytest.raises(DeadlineExceededError) as caught:
                await client(http).complete(MESSAGES, stage="RETRIEVAL_PLANNER")
            assert isinstance(caught.value, BudgetExceededError)
            assert http.calls == 0


@pytest.mark.asyncio
async def test_scoped_usage_and_components_never_mutate_canonical_task_trace():
    store, http = TraceStore(123), Http(response({"total_tokens": 20}))
    telemetry, token, chat = bound_client(store, http, limit=50000)
    try:
        with telemetry.isolated_metrics(), capture_follow_up_budget() as records, follow_up_budget_scope(Settings(Mode.SHADOW)):
            await chat.complete(MESSAGES, stage="FOLLOW_UP")
            record_request_component("embedding", usage={"total_tokens": 99})
            record_request_component("qdrant_query")
            record_request_component("bm25")
            record_request_component("reranker", usage={"total_tokens": 70})
        assert store.tokens == 123 and store.diagnostics == [] and store.counters == {}
        assert records[0]["charged_chat_tokens"] == 20
        assert records[0]["reported_component_tokens"] == {"embedding": 99, "reranker": 70}
    finally:
        telemetry.reset(token)


def test_observation_logger_failure_cannot_replace_result(monkeypatch):
    import dovideo.application.follow_up_budget as module
    monkeypatch.setattr(module._logger, "info", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sink")))
    with follow_up_budget_scope(Settings(Mode.SHADOW)):
        pass


@pytest.mark.asyncio
async def test_nonchat_real_provider_attempts_and_usage_are_separate_from_chat():
    from dovideo.infrastructure.providers import OpenAICompatibleEmbeddingAdapter, ProviderHttpResponse
    from dovideo.infrastructure.providers.reranker import RerankerConfig, SiliconFlowRerankerAdapter
    from dovideo.infrastructure.r4_runtime import _ObservedSparse
    from dovideo.application.sparse_retrieval import BM25Retriever
    from test_reranker import DOCS, BODY
    from test_qdrant import FakeJsonClient, _existing_collection
    from dovideo.infrastructure import QdrantVectorIndex, JsonHttpResponse
    embed_http = Http(ProviderHttpResponse(503, {}), ProviderHttpResponse(200,
        {"data": [{"embedding": [.1, .2]}], "usage": {"total_tokens": 99}}))
    embed = OpenAICompatibleEmbeddingAdapter(ProviderConfig(base_url="https://example.test/v1", model="embed",
        max_attempts=2, retry_delay_seconds=0), client=embed_http)
    rerank_http = Http(ProviderHttpResponse(200, {**BODY, "usage": {"total_tokens": 70}}))
    rerank = SiliconFlowRerankerAdapter(RerankerConfig(enabled=True, api_key="fixture"), client=rerank_http)
    vector_http = FakeJsonClient([_existing_collection(), JsonHttpResponse(200, {"result": {"points": []}})])
    vector = QdrantVectorIndex(base_url="https://example.test", client=vector_http)
    with capture_follow_up_budget() as records, follow_up_budget_scope(Settings(Mode.SHADOW)):
        assert await embed.embed("private") == (.1, .2)
        await rerank.rerank("private", DOCS)
        assert await vector.search(42, (.1, .2), limit=1) == ()
        _ObservedSparse(BM25Retriever()).rank("Redis", ["Redis memory"], limit=1)
    assert records[0]["component_calls"] == {"embedding": 2, "reranker": 1, "qdrant_query": 1, "bm25": 1}
    assert records[0]["reported_component_tokens"] == {"embedding": 99, "reranker": 70}
    assert records[0]["charged_chat_tokens"] == records[0]["chat_http_attempts"] == 0
