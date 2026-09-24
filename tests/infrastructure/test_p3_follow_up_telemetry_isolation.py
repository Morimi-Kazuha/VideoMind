from __future__ import annotations

import pytest

from dovideo.application import TaskKey
from dovideo.domain import BudgetUsage
from dovideo.infrastructure.r4_runtime import (
    R4AgentTelemetry,
    _StrictRetrievalService,
)


class _TraceStore:
    def __init__(self) -> None:
        self.writes: list[tuple[object, ...]] = []

    def latest(self, _key):
        return {"counters": {}}

    def start(self, key):
        self.writes.append(("start", key))
        return "trace"

    def record(self, key, event):
        self.writes.append(("record", key, event))

    def increment_for_key(self, key, metric, amount):
        self.writes.append(("increment", key, metric, amount))

    def observe_for_key(self, key, metric, value):
        self.writes.append(("observe", key, metric, value))

    def add_usage_for_key(self, key, **values):
        self.writes.append(("usage", key, values))
        return BudgetUsage()

    def record_structural_for_key(self, key, value):
        self.writes.append(("structural", key, value))


class _FailingPlanner:
    async def plan_retrieval(self, _question):
        raise RuntimeError("offline test provider failure")


class _UnusedEmbedding:
    async def embed(self, _query):
        raise AssertionError("embedding must not run after planner fallback")


class _UnusedVectorIndex:
    async def search(self, *_args, **_kwargs):
        raise AssertionError("vector search must not run after planner fallback")

    async def upsert(self, *_args, **_kwargs):
        raise AssertionError("indexing is not part of follow-up")


def _bound_telemetry() -> tuple[R4AgentTelemetry, _TraceStore, TaskKey]:
    store = _TraceStore()
    telemetry = R4AgentTelemetry(store)  # type: ignore[arg-type]
    key = TaskKey(19, "existing task", "GENERAL")
    token = telemetry.bind(key)
    assert store.writes == []
    telemetry.reset(token)
    return telemetry, store, key


def test_follow_up_scoped_telemetry_never_writes_to_canonical_trace() -> None:
    telemetry, store, key = _bound_telemetry()
    token = telemetry.bind(key)
    try:
        with telemetry.isolated_metrics() as counters:
            telemetry.increment("retrievalIntentFallbacks")
            telemetry.observe("retrievalCandidateCount", 3)
            telemetry.add(estimated_tokens=10, estimated_cost=0.25)
            telemetry.record(key, object())  # type: ignore[arg-type]
            telemetry.record_model_transport(
                "FOLLOW_UP",
                status_code=200,
                finish_reason="stop",
                content_present=True,
                content_chars=20,
            )
            telemetry.record_structured_response("FOLLOW_UP", {"valid": True})
            assert telemetry.counter_value("retrievalIntentFallbacks") == 1
            assert counters["retrievalIntentFallbacks"] == 1
    finally:
        telemetry.reset(token)

    assert store.writes == []


@pytest.mark.asyncio
async def test_strict_r4_retrieval_still_rejects_fallback_with_local_counters() -> None:
    telemetry, store, _key = _bound_telemetry()
    retrieval = _StrictRetrievalService(
        _FailingPlanner(),
        _UnusedEmbedding(),
        _UnusedVectorIndex(),
        telemetry=telemetry,
    )

    with telemetry.isolated_metrics() as counters:
        with pytest.raises(RuntimeError, match="provider fallback"):
            await retrieval._retrieval_intent("question about the video")

    assert counters["retrievalIntentFallbacks"] == 1
    assert store.writes == []
