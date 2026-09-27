"""Real chat boundary budget decisions use the existing task usage ledger."""

from __future__ import annotations

import json

import pytest

from dovideo.application import BudgetExceededError, TaskKey
from dovideo.domain import AgentBudgetConfig, AnalysisMode, BudgetUsage
from dovideo.infrastructure.providers import (
    OpenAICompatibleChatClient,
    ProviderConfig,
    ProviderHttpResponse,
)
from dovideo.infrastructure.r4_runtime import R4AgentTelemetry


class TraceStore:
    def __init__(self, tokens: int = 0) -> None:
        self.tokens = tokens
        self.cost = 0.0
        self.diagnostics: list[dict[str, object]] = []
        self.counters: dict[str, int] = {}

    def latest(self, _key):
        return {"traceId": "existing"}

    def current_usage_for_key(self, _key):
        return BudgetUsage(estimatedTokens=self.tokens, estimatedCost=self.cost)

    def add_usage_for_key(self, _key, *, estimated_tokens=0, estimated_cost=0.0):
        self.tokens += int(estimated_tokens)
        self.cost += float(estimated_cost)
        return self.current_usage_for_key(_key)

    def record_structural_for_key(self, _key, diagnostic):
        self.diagnostics.append(dict(diagnostic))

    def increment_for_key(self, _key, metric, amount=1):
        self.counters[metric] = self.counters.get(metric, 0) + amount


class Http:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def post(self, _url, *, headers, json, timeout):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def response(usage=None, *, status=200):
    body = {"choices": [{"message": {"content": "{}"}}]}
    if usage is not None:
        body["usage"] = usage
    return ProviderHttpResponse(status, body)


def bound_client(store: TraceStore, http: Http, *, limit: int, attempts: int = 1):
    telemetry = R4AgentTelemetry(
        store, budget_config=AgentBudgetConfig(maxEstimatedTokens=limit)
    )
    token = telemetry.bind(TaskKey(7, "goal", AnalysisMode.GENERAL))
    client = OpenAICompatibleChatClient(
        ProviderConfig(
            base_url="https://example.test/v1",
            model="deepseek/test",
            max_attempts=attempts,
        ),
        client=http,
        usage_sink=telemetry,
    )
    return telemetry, token, client


@pytest.mark.asyncio
async def test_preflight_denies_call_before_http_when_input_and_output_do_not_fit() -> None:
    store = TraceStore(tokens=4000)
    http = Http(response())
    telemetry, token, client = bound_client(store, http, limit=20_000)
    try:
        with pytest.raises(BudgetExceededError):
            await client.complete(
                [{"role": "user", "content": "private prompt"}], stage="CRITIC"
            )
    finally:
        telemetry.reset(token)
    assert http.calls == 0
    assert store.tokens == 4000
    assert store.counters["modelBudgetDenials"] == 1
    assert store.diagnostics[-1]["allowed"] is False
    assert "private prompt" not in json.dumps(store.diagnostics)


@pytest.mark.asyncio
async def test_reported_usage_is_charged_once_and_replaces_preflight_estimate() -> None:
    store = TraceStore()
    http = Http(response({"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}))
    telemetry, token, client = bound_client(store, http, limit=50_000)
    try:
        assert await client.complete(
            [{"role": "user", "content": "private prompt"}], stage="PLANNER"
        ) == "{}"
    finally:
        telemetry.reset(token)
    assert store.tokens == 150
    usage = [item for item in store.diagnostics if item.get("kind") == "modelCallUsage"]
    assert len(usage) == 1
    assert usage[0]["usageSource"] == "provider"
    assert usage[0]["cumulativeTotal"] == 150
    assert usage[0]["remainingBudget"] == 49_850
    assert "private prompt" not in json.dumps(store.diagnostics)


@pytest.mark.asyncio
async def test_unreported_retry_is_charged_before_second_admission() -> None:
    store = TraceStore()
    http = Http(response(status=503), response())
    telemetry, token, client = bound_client(store, http, limit=4800, attempts=2)
    try:
        with pytest.raises(BudgetExceededError):
            await client.complete(
                [{"role": "user", "content": "x" * 1000}],
                stage="CHUNK_SUMMARY",
            )
    finally:
        telemetry.reset(token)
    assert http.calls == 1
    assert store.tokens >= 500
    assert store.counters["modelBudgetDenials"] == 1
    assert any(
        item.get("usageSource") == "heuristic"
        for item in store.diagnostics
    )


@pytest.mark.asyncio
async def test_provider_reported_overshoot_fails_at_same_call_boundary() -> None:
    store = TraceStore()
    http = Http(response({"prompt_tokens": 100, "completion_tokens": 2400, "total_tokens": 2500}))
    telemetry, token, client = bound_client(store, http, limit=2000)
    try:
        with pytest.raises(BudgetExceededError):
            await client.complete(
                [{"role": "user", "content": "query"}],
                stage="RETRIEVAL_PLANNER",
            )
    finally:
        telemetry.reset(token)
    assert http.calls == 1
    assert store.tokens == 2500
    assert store.counters["modelBudgetOverruns"] == 1


@pytest.mark.asyncio
async def test_cost_only_provider_usage_keeps_cost_and_labels_token_estimate() -> None:
    store = TraceStore()
    http = Http(response({"cost": 0.25}))
    telemetry, token, client = bound_client(store, http, limit=50_000)
    try:
        assert await client.complete(
            [{"role": "user", "content": "private prompt"}], stage="PLANNER"
        ) == "{}"
    finally:
        telemetry.reset(token)
    assert store.tokens > 0
    assert store.cost == 0.25
    usage = [item for item in store.diagnostics if item.get("kind") == "chatUsage"]
    assert len(usage) == 1
    assert usage[0]["usageSource"] == "heuristic_tokens_provider_cost"
    assert usage[0]["providerReportedCost"] == 0.25
